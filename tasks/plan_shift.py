"""Сервис заявок на сдвиг плана.

Заявка висит у заместителей отдела исполнителя. Если никто не отреагировал
за 2 часа — уходит руководителю отдела. Если он молчит 4 часа — начальнику завода.
Первый ответивший решает.
"""
import calendar
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.conf import settings as dj_settings
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from .models import PlanShiftRequest, PlanShiftStep, Task, TaskLog

logger = logging.getLogger(__name__)
User = get_user_model()

# Таймауты по уровням (None = висит, пока не ответят)
LEVEL_TIMEOUTS = {
    PlanShiftRequest.Level.DEPUTY:  timedelta(hours=2),
    PlanShiftRequest.Level.MANAGER: timedelta(hours=4),
    PlanShiftRequest.Level.PLANT:   None,
}

LEVEL_ROLE_CODES = {
    PlanShiftRequest.Level.DEPUTY:  'deputy',
    PlanShiftRequest.Level.MANAGER: 'manager',
    PlanShiftRequest.Level.PLANT:   'plant_head',
}

LEVEL_TITLES = {
    PlanShiftRequest.Level.DEPUTY:  'заместители отдела',
    PlanShiftRequest.Level.MANAGER: 'руководитель отдела',
    PlanShiftRequest.Level.PLANT:   'начальник завода',
}


# ─────────────────────────────────────────────────────────────
#  Поиск согласующих
# ─────────────────────────────────────────────────────────────

def find_reviewers(department, level):
    """Активные пользователи нужной роли и отдела.

    Для уровня PLANT отдел игнорируется — начальник завода один на всех.
    """
    role_code = LEVEL_ROLE_CODES.get(level)
    if not role_code:
        return []

    qs = User.objects.filter(is_active=True, role__code=role_code)
    if level != PlanShiftRequest.Level.PLANT:
        if department is None:
            return []
        qs = qs.filter(department=department)
    return list(qs)


def pick_start_level(department):
    """Первый уровень, у которого есть согласующие.

    Если замов нет — сразу MANAGER. Если и его нет — PLANT.
    Если никого — None (согласовывать не с кем).
    """
    for level in (
            PlanShiftRequest.Level.DEPUTY,
            PlanShiftRequest.Level.MANAGER,
            PlanShiftRequest.Level.PLANT,
    ):
        if find_reviewers(department, level):
            return level
    return None


# ─────────────────────────────────────────────────────────────
#  Что сдвинется
# ─────────────────────────────────────────────────────────────

def build_affected_list(source_task):
    """Открытые задачи того же исполнителя, кроме самой source_task."""
    executor = source_task.executor
    if not executor:
        return []
    return list(
        Task.objects
        .filter(executor=executor)
        .exclude(pk=source_task.pk)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
    )


# ─────────────────────────────────────────────────────────────
#  Создание заявки
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def create_request(source_task, reason):
    """Создаёт заявку на сдвиг. Возвращает PlanShiftRequest или None.

    None — если согласовывать не с кем (нет ролей в отделе). Тогда сдвиг
    применяется немедленно.
    """
    from tasks.views import _urgent_shift_days   # локальный импорт — избегаем цикла

    executor = source_task.executor
    department = getattr(executor, 'department', None) if executor else None
    shift_days = _urgent_shift_days(source_task)

    level = pick_start_level(department) if department else None
    if level is None:
        # Некому согласовывать — применяем сразу
        _apply_shift_now(source_task, shift_days)
        return None

    reviewers = find_reviewers(department, level)
    now = timezone.now()
    timeout = LEVEL_TIMEOUTS.get(level)
    escalate_at = now + timeout if timeout else now + timedelta(days=3650)

    request = PlanShiftRequest.objects.create(
        source_task=source_task,
        initiated_by=source_task.requester,
        reason=reason,
        shift_days=shift_days,
        department=department,
        status=PlanShiftRequest.Status.PENDING,
        current_level=level,
        escalate_at=escalate_at,
    )
    request.affected_tasks.set(build_affected_list(source_task))
    request.recipients.set(reviewers)

    for r in reviewers:
        PlanShiftStep.objects.create(
            request=request, level=level, reviewer=r,
        )

    # Задача-виновник уходит в ожидание согласования
    source_task.status = Task.Status.PENDING_APPROVAL
    source_task.save(update_fields=['status'])

    TaskLog.objects.create(
        task=source_task,
        kind=TaskLog.Kind.SHIFT_REQUESTED,
        author=source_task.requester,
        shift_days=shift_days,
        comment=f'Запрошен сдвиг +{shift_days} дн. Обоснование: {reason[:200]}',
    )

    req_id = request.pk
    transaction.on_commit(
        lambda: _notify_reviewers(
            PlanShiftRequest.objects.get(pk=req_id),
            reviewers, level, escalated=False,
        )
    )

    return request


# ─────────────────────────────────────────────────────────────
#  Эскалация
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def escalate(request):
    """Переход на следующий уровень. Возвращает True, если эскалировали."""
    if request.status != PlanShiftRequest.Status.PENDING:
        return False

    now = timezone.now()

    # Всех на текущем уровне помечаем как проигнорировавших
    PlanShiftStep.objects.filter(
        request=request,
        level=request.current_level,
        decision=PlanShiftStep.Decision.PENDING,
    ).update(decision=PlanShiftStep.Decision.TIMEOUT, decided_at=now)

    # Следующий уровень
    if request.current_level == PlanShiftRequest.Level.DEPUTY:
        next_level = PlanShiftRequest.Level.MANAGER
    elif request.current_level == PlanShiftRequest.Level.MANAGER:
        next_level = PlanShiftRequest.Level.PLANT
    else:
        # Уже на PLANT — оставляем висеть, пока не ответят
        return False

    reviewers = find_reviewers(request.department, next_level)

    # Если на этом уровне никого — пытаемся перескочить дальше
    if not reviewers and next_level == PlanShiftRequest.Level.MANAGER:
        next_level = PlanShiftRequest.Level.PLANT
        reviewers = find_reviewers(request.department, next_level)

    if not reviewers:
        # Совсем никого — висит, но помечаем escalate_at далеко, чтобы
        # не дёргать команду каждый раз
        request.escalate_at = now + timedelta(days=3650)
        request.save(update_fields=['escalate_at'])
        return False

    timeout = LEVEL_TIMEOUTS.get(next_level)
    escalate_at = now + timeout if timeout else now + timedelta(days=3650)

    request.current_level = next_level
    request.escalate_at = escalate_at
    request.save(update_fields=['current_level', 'escalate_at'])
    request.recipients.set(reviewers)

    for r in reviewers:
        PlanShiftStep.objects.create(
            request=request, level=next_level, reviewer=r,
        )

    req_id = request.pk
    transaction.on_commit(
        lambda: _notify_reviewers(
            PlanShiftRequest.objects.get(pk=req_id),
            reviewers, next_level, escalated=True,
        )
    )
    return True


# ─────────────────────────────────────────────────────────────
#  Решения
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def approve(request, user, note=''):
    """Первый ответивший одобряет сдвиг — заявка закрывается."""
    if request.status != PlanShiftRequest.Status.PENDING:
        return False

    now = timezone.now()

    PlanShiftStep.objects.filter(
        request=request, reviewer=user,
        decision=PlanShiftStep.Decision.PENDING,
    ).update(
        decision=PlanShiftStep.Decision.APPROVED,
        decided_at=now, note=note,
    )

    # Остальных на текущем уровне — как проигнорировавших
    PlanShiftStep.objects.filter(
        request=request, level=request.current_level,
        decision=PlanShiftStep.Decision.PENDING,
    ).exclude(reviewer=user).update(
        decision=PlanShiftStep.Decision.TIMEOUT, decided_at=now,
    )

    request.status = PlanShiftRequest.Status.APPROVED
    request.resolved_at = now
    request.resolved_by = user
    request.decision_note = note
    request.save(update_fields=[
        'status', 'resolved_at', 'resolved_by', 'decision_note',
    ])

    _apply_shift(request)

    source = request.source_task
    if source.status == Task.Status.PENDING_APPROVAL:
        source.status = Task.Status.NEW
        source.save(update_fields=['status'])

    _notify_decision(request, user, 'approved', note)
    return True


@transaction.atomic
def reject(request, user, note=''):
    """Отклонение — задача-виновник отменяется."""
    if request.status != PlanShiftRequest.Status.PENDING:
        return False

    now = timezone.now()

    PlanShiftStep.objects.filter(
        request=request, reviewer=user,
        decision=PlanShiftStep.Decision.PENDING,
    ).update(
        decision=PlanShiftStep.Decision.REJECTED,
        decided_at=now, note=note,
    )
    PlanShiftStep.objects.filter(
        request=request, level=request.current_level,
        decision=PlanShiftStep.Decision.PENDING,
    ).exclude(reviewer=user).update(
        decision=PlanShiftStep.Decision.TIMEOUT, decided_at=now,
    )

    request.status = PlanShiftRequest.Status.REJECTED
    request.resolved_at = now
    request.resolved_by = user
    request.decision_note = note
    request.save(update_fields=[
        'status', 'resolved_at', 'resolved_by', 'decision_note',
    ])

    source = request.source_task
    source.status = Task.Status.CANCELLED
    source.finished_at = now
    # credit ещё не применялся (сдвиг не одобрялся), но на всякий случай
    # сбрасываем planned_shift_days, чтобы _correct_urgent_shift не сработал.
    source.planned_shift_days = 0
    source.save(update_fields=['status', 'finished_at', 'planned_shift_days'])

    TaskLog.objects.create(
        task=source, kind=TaskLog.Kind.SHIFT_REJECTED,
        author=user,
        comment=note or 'Сдвиг отклонён, задача отменена.',
    )

    _notify_decision(request, user, 'rejected', note)
    return True


# ─────────────────────────────────────────────────────────────
#  Внутреннее
# ─────────────────────────────────────────────────────────────

def _apply_shift(request):
    """Применяет одобренный сдвиг и авто-паузу."""
    from tasks.views import _auto_pause_in_progress
    from tasks.services_shift import recompute_shift_for_executor

    source = request.source_task
    days = request.shift_days
    executor = source.executor

    _auto_pause_in_progress(executor, source)

    # Учитываем срочную в нагрузке исполнителя и пересчитываем сдвиг.
    executor.shift_credit_days = (executor.shift_credit_days or 0) + days
    executor.save(update_fields=['shift_credit_days'])

    source.planned_shift_days = days
    source.save(update_fields=['planned_shift_days'])

    shifted = recompute_shift_for_executor(
        executor,
        actor=request.resolved_by,
        source_task=source,
        exclude_task_pk=source.pk,
    )

    TaskLog.objects.create(
        task=source, kind=TaskLog.Kind.SHIFT_APPROVED,
        author=request.resolved_by,
        source_task=None,
        shift_days=days,
        comment=f'Сдвиг +{days} раб. дн. одобрен. Затронуто задач: {shifted}.',
    )


def _apply_shift_now(source_task, days):
    """Применяет сдвиг без согласования (нет согласующих в отделе)."""
    from tasks.views import _auto_pause_in_progress
    from tasks.services_shift import recompute_shift_for_executor

    executor = source_task.executor

    _auto_pause_in_progress(executor, source_task)

    executor.shift_credit_days = (executor.shift_credit_days or 0) + days
    executor.save(update_fields=['shift_credit_days'])

    source_task.planned_shift_days = days
    source_task.status = Task.Status.NEW
    source_task.save(update_fields=['planned_shift_days', 'status'])

    shifted = recompute_shift_for_executor(
        executor,
        actor=source_task.requester,
        source_task=source_task,
        exclude_task_pk=source_task.pk,
    )

    TaskLog.objects.create(
        task=source_task, kind=TaskLog.Kind.SHIFT_APPROVED,
        author=source_task.requester,
        shift_days=days,
        comment=f'Сдвиг +{days} раб. дн. применён автоматически '
                f'(в отделе нет согласующих). Затронуто: {shifted}.',
    )


def _request_url(request):
    try:
        return reverse('shift_request_detail', args=[request.pk])
    except Exception:
        return f'/manager/shift-requests/{request.pk}/'


def _notify_reviewers(request, reviewers, level, escalated=False):
    """Notification + email. При эскалации — перечисляем, кто проигнорировал."""
    from comms.services import notify
    from comms.models import Notification

    title = f'Согласование сдвига: {LEVEL_TITLES.get(level, level)}'

    ignored_note = ''
    if escalated:
        ignored = (
            PlanShiftStep.objects
            .filter(request=request, decision=PlanShiftStep.Decision.TIMEOUT)
            .select_related('reviewer')
        )
        names = ', '.join(s.reviewer.full_name for s in ignored if s.reviewer)
        if names:
            ignored_note = f'⚠️ Проигнорировали: {names}. '

    text = (f'{ignored_note}Сдвиг +{request.shift_days} дн — '
            f'«{request.source_task.title}»')

    notify(reviewers, text, _request_url(request), Notification.Kind.ACTION)

    # Email
    base = getattr(dj_settings, 'SITE_URL', '') or ''
    url = base.rstrip('/') + _request_url(request) if base else _request_url(request)
    subject = f'[СВОД] {title}'

    body = (
        f'{title}\n\n'
        f'Задача-виновник: {request.source_task.title}\n'
        f'Исполнитель: {request.source_task.executor.full_name}\n'
        f'Сдвиг: +{request.shift_days} дн.\n'
        f'Обоснование: {request.reason}\n\n'
    )
    if ignored_note:
        body += ignored_note + '\n\n'
    body += f'Открыть: {url}\n'

    for r in reviewers:
        if not r.email or not getattr(r, 'email_notifications', True):
            continue
        try:
            send_mail(subject, body, dj_settings.DEFAULT_FROM_EMAIL,
                      [r.email], fail_silently=True)
        except Exception:
            logger.exception('shift request email failed for %s', r.email)


def _notify_decision(request, user, decision, note):
    """Уведомляет постановщика об исходе."""
    from comms.services import notify
    from comms.models import Notification

    source = request.source_task
    recipient = source.requester
    if not recipient:
        return

    if decision == 'approved':
        text = (f'✅ Сдвиг +{request.shift_days} дн. по задаче '
                f'«{source.title}» одобрен ({user.full_name}).')
        kind = Notification.Kind.INFO
    elif decision == 'squeezed':
        text = (f'📐 По «{source.title}» ужали сроки без сдвига '
                f'({user.full_name}).')
        kind = Notification.Kind.INFO
    else:
        text = (f'❌ Сдвиг по «{source.title}» отклонён ({user.full_name}). '
                f'Задача отменена.')
        kind = Notification.Kind.ALERT

    notify([recipient], text, _request_url(request), kind)


    # ─────────────────────────────────────────────────────────────
#  Ужать сроки
# ─────────────────────────────────────────────────────────────

MIN_SPLIT_HOURS = 6   # ниже этого разбивать не имеет смысла


def find_reassign_candidates(source_task):
    """Кандидаты на перекидывание задач у того же исполнителя.

    Возвращает список словарей:
        {'user', 'free_hours', 'committed', 'capacity', 'same_role'}
    Кандидаты — только внутри отдела исполнителя, с совпадающей ролью,
    с положительным свободным бюджетом.
    """
    from tasks.utils import norm_hours, work_days_between
    from datetime import date

    executor = source_task.executor
    if not executor or not executor.department_id:
        return []

    today = timezone.localdate()
    last_day = calendar.monthrange(today.year, today.month)[1]
    month_end = date(today.year, today.month, last_day)

    norm = norm_hours()
    work_days = work_days_between(today, month_end) or 0
    capacity = norm * max(work_days, 1)

    # Открытые задачи кандидатов
    base_qs = (
        User.objects
        .filter(
            is_active=True,
            department_id=executor.department_id,
        )
        .exclude(pk=executor.pk)
    )
    # Роль совпадает (или у обоих роль пуста)
    if executor.role_id:
        base_qs = base_qs.filter(role_id=executor.role_id)

    candidates = []
    for u in base_qs:
        committed = (
                Task.objects
                .filter(executor=u)
                .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
                .aggregate(s=Sum('plan_hours'))['s'] or 0
        )
        free = round(capacity - committed, 2)
        candidates.append({
            'user': u,
            'committed': round(committed, 2),
            'capacity': round(capacity, 2),
            'free_hours': max(0, free),
        })

    # Сортируем по свободному бюджету — сначала самые свободные
    candidates.sort(key=lambda c: -c['free_hours'])
    return candidates


def find_chain_tasks(source_task):
    """Задачи той же ветки, которые идут после source_task и заблокированы.

    Их можно активировать досрочно — «параллелить», чтобы ужать общий срок.
    """
    if not source_task.branch_id or source_task.stage_order is None:
        return []
    return list(
        Task.objects
        .filter(
            branch_id=source_task.branch_id,
            stage_order__gt=source_task.stage_order,
            blocked_by_stage=True,
        )
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .select_related('executor')
        .order_by('stage_order')
    )


def can_split(source_task):
    """Можно ли разбить задачу на две подзадачи.

    PENDING_APPROVAL намеренно не блокирует: это состояние, в котором
    задача находится во время ужима. Завершённые и отменённые — нельзя.
    """
    return (
            source_task.plan_hours
            and source_task.plan_hours >= MIN_SPLIT_HOURS
            and source_task.status not in (
                Task.Status.DONE,
                Task.Status.CANCELLED,
            )
    )


@transaction.atomic
def apply_squeeze(request, user, actions, note=''):
    """Применяет выбранные варианты ужимания.

    actions = {
        'reassign': {task_id: new_user_id, ...},
        'unblock': [task_id, task_id, ...],
        'split': {'first_hours': 10, 'second_days': 3} | None,
    }
    """
    # _shift_other_tasks больше не используется — сдвиг идёт через
    # recompute_shift_for_executor (services_shift.py).

    source = request.source_task

    # 1. Перекидывание задач на других исполнителей
    moved = 0
    for task_id, new_user_id in (actions.get('reassign') or {}).items():
        t = Task.objects.filter(pk=task_id, executor=source.executor).first()
        new_user = User.objects.filter(pk=new_user_id, is_active=True).first()
        if not t or not new_user:
            continue
        old_name = t.executor.full_name
        t.executor = new_user
        t.save(update_fields=['executor'])
        TaskLog.objects.create(
            task=t, kind=TaskLog.Kind.PLAN, author=user,
            comment=f'Перекинуто с «{old_name}» на «{new_user.full_name}» '
                    f'для ужима плана задачи #{source.pk}.',
        )
        moved += 1

    # 2. Снятие блокировки у следующих этапов
    unblocked = 0
    for task_id in (actions.get('unblock') or []):
        t = Task.objects.filter(pk=task_id, branch_id=source.branch_id).first()
        if not t or not t.blocked_by_stage:
            continue
        t.blocked_by_stage = False
        t.save(update_fields=['blocked_by_stage'])
        TaskLog.objects.create(
            task=t, kind=TaskLog.Kind.DUE, author=user,
            comment=f'Разблокировано досрочно для ужима плана задачи #{source.pk}.',
        )
        # Уведомим исполнителя
        try:
            from comms.services import notify_task_created
            notify_task_created(t)
        except Exception:
            pass
        unblocked += 1

    # 3. Разбиение source_task
    split_info = actions.get('split')
    split_made = False
    if split_info and can_split(source):
        first_hours = float(split_info.get('first_hours') or 0)
        second_days = int(split_info.get('second_days') or 0)
        total = source.plan_hours or 0

        if 0 < first_hours < total and second_days > 0:
            second_hours = round(total - first_hours, 2)
            parent = source.parent  # сохраняем иерархию

            # Создаём «хвост» — отложенную часть
            from datetime import timedelta
            new_due = (source.due + timedelta(days=second_days)) if source.due else None

            tail = Task.objects.create(
                title=f'[остаток] {source.title}',
                plan_hours=second_hours,
                start_due=None,
                due=new_due,
                priority=source.priority,
                scale=source.scale,
                kind=source.kind,
                parent=parent or source,   # привязка
                order=source.order,
                branch=source.branch,
                stage_order=source.stage_order,
                executor=source.executor,
                requester=source.requester,
                body=f'Остаток по задаче #{source.pk} — '
                     f'отложено из-за ужима плана.',
            )
            # Уменьшаем план основной задачи
            source.plan_hours = first_hours
            source.save(update_fields=['plan_hours'])

            TaskLog.objects.create(
                task=source, kind=TaskLog.Kind.PLAN, author=user,
                comment=f'Разбито на {first_hours} ч сейчас + '
                        f'{second_hours} ч отложено (#{tail.pk}, +{second_days} дн).',
            )
            TaskLog.objects.create(
                task=tail, kind=TaskLog.Kind.PLAN, author=user,
                comment=f'Остаток от задачи #{source.pk}.',
            )
            split_made = True

    # Закрываем заявку
    now = timezone.now()
    request.status = PlanShiftRequest.Status.SQUEEZED
    request.resolved_at = now
    request.resolved_by = user
    request.decision_note = (
            f'Ужали: перекинуто {moved} задач, разблокировано {unblocked} этапов'
            + (', source разбит' if split_made else '')
            + (f'. {note}' if note else '')
    )
    request.save(update_fields=[
        'status', 'resolved_at', 'resolved_by', 'decision_note',
    ])

    # Source возвращается в работу
    if source.status == Task.Status.PENDING_APPROVAL:
        source.status = Task.Status.NEW
        source.save(update_fields=['status'])

    # Пометить шаг согласования
    PlanShiftStep.objects.filter(
        request=request, level=request.current_level,
        decision=PlanShiftStep.Decision.PENDING,
    ).update(
        decision=PlanShiftStep.Decision.SQUEEZED,
        decided_at=now,
        note=note,
    )
    PlanShiftStep.objects.filter(
        request=request, reviewer=user,
        decision=PlanShiftStep.Decision.SQUEEZED,
    ).update(decided_at=now)

    # Уведомить постановщика
    _notify_decision(request, user, 'squeezed', request.decision_note)

    return {
        'moved': moved,
        'unblocked': unblocked,
        'split': split_made,
    }
