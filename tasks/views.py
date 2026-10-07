import csv
import math
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST
from accounts.models import Department
from comms.models import Attachment
from comms.services import (
    notify_task_created,
    notify_task_started,
    notify_shop_started,
)
from core.models import TaskType

from . import services
from .models import Task, TaskBranch, TaskLog, TaskProgress, Order, WeekCommit
from .utils import format_spent, parse_plan, norm_hours, norm_hours_for
from .services_shift import recompute_shift_for_executor

User = get_user_model()

MAX_DEPTH = 6


# ─────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────

def _parse_date(s):
    """Дата из ISO или ДД.ММ.ГГГГ; пусто/мусор → None."""
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_manager(user):
    return user.is_boss


def _can_see(user, t):
    if user.is_superuser or user.is_admin_role:
        return True
    if t.executor_id == user.id or t.requester_id == user.id:
        return True
    if (
            user.role
            and user.role.can_manage
            and user.department_id
            and t.executor_id
            and t.executor.department_id == user.department_id
    ):
        return True
    return False


def _can_decompose(user, t):
    """Может ли пользователь создать подзадачу через task_create?parent=...

    Разрешено только для ОТКРЫТЫХ задач (не DONE/CANCELLED):
      - исполнитель — только к своим;
      - is_boss (руководитель/админ/суперюзер) — к любым.

    Для закрытых задач используется _can_create_rework и view
    task_rework_create — это управленческое действие, а не техническое.
    """
    if t.status in (Task.Status.DONE, Task.Status.CANCELLED):
        return False
    if user.is_boss:
        return True
    return t.executor_id == user.id


def _can_create_rework(user, task):
    """Кто может зафиксировать доработку по закрытой задаче.

    Право имеют только:
      - суперюзер;
      - админ портала (role.can_admin);
      - руководитель ОТДЕЛА ИСПОЛНИТЕЛЯ (role.can_manage + совпадение
        department_id с отделом исполнителя задачи).

    Постановщик, сам исполнитель, руководитель другого отдела и
    начальник завода — НЕ могут. Это управленческое решение: заказчик
    идёт к руководителю отдела, где работал исполнитель, тот оформляет
    доработку.

    Начальник завода (role.can_plant без can_admin) намеренно исключён:
    он смотрит свод по всем отделам, но не фиксирует доработки ни по
    чьим задачам — это дело руководителя конкретного отдела.
    """
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser or user.is_admin_role:
        return True
    # Начальник завода (can_plant без can_admin) — только смотрит,
    # не фиксирует. Проверка идёт после is_admin_role, поэтому
    # админ с ролью can_admin + can_plant в неё уже не попадёт.
    if user.can_plant:
        return False
    if (
            user.role
            and user.role.can_manage
            and user.department_id
            and task.executor_id
            and task.executor.department_id == user.department_id
    ):
        return True
    return False


def _due_view(iso):
    if not iso:
        return ''
    try:
        return datetime.strptime(iso, '%Y-%m-%d').strftime('%d.%m.%Y')
    except ValueError:
        return iso


def _local_date(value):
    return timezone.localtime(value).date() if value else None


def _state_of(t):
    """Цветовое состояние узла дерева."""
    if t.status == Task.Status.DONE:
        return 'done'
    if t.status == Task.Status.CANCELLED:
        return 'cancel'
    if t.status == Task.Status.REVIEW:
        return 'review'
    if t.due and t.due < timezone.localdate():
        return 'over'
    return 'work'

def _executor_vacation_warning(executor, start_due=None, due=None):
    """Предупреждение, если исполнитель в отпуске на даты задачи.

    Возвращает строку с текстом или None, если пересечения нет.

    Логика:
      - если у исполнителя статус VACATION без дат → «в отпуске»;
      - если задан период vacation_from..vacation_to и он пересекается
        с [start_due..due] → «задача пересекается с отпуском»;
      - если у задачи нет дат, считаем «сегодня» как точку.

    Не блокирует создание: иногда задачу ставят заранее, чтобы
    человек взял её после отпуска.
    """
    if executor is None:
        return None

    today = timezone.localdate()
    task_start = start_due or due or today
    task_end = due or start_due or today

    # Нормализуем границы (start ≤ end)
    if task_end < task_start:
        task_start, task_end = task_end, task_start

    # 1. Статус VACATION без дат — просто отметка.
    if (
            executor.employment_status == executor.EmploymentStatus.VACATION
            and not (executor.vacation_from or executor.vacation_to)
    ):
        return (
            f'⚠ {executor.full_name} в отпуске / на больничном. '
            f'Период не задан — уточните, когда он вернётся.'
        )

    # 2. Даты отпуска заданы частично или полностью.
    if executor.vacation_from or executor.vacation_to:
        v_from = executor.vacation_from or task_start
        v_to = executor.vacation_to or task_end

        # Пересечение интервалов [task_start..task_end] и [v_from..v_to]
        if not (task_end < v_from or task_start > v_to):
            period = executor.vacation_label
            if period:
                return (
                    f'⚠ {executor.full_name} в отпуске {period} — '
                    f'задача пересекается с отпуском.'
                )
            return (
                f'⚠ {executor.full_name} в отпуске — '
                f'задача пересекается с отпуском.'
            )

    return None

def _urgent_shift_days(urgent_task):
    """Сколько рабочих дней «съедает» срочная задача.

    Считаем по объёму работы (plan_hours), а не по календарю:
    сколько смена-дней исполнителя уйдёт на эту задачу.
    Норма берётся из отдела исполнителя, либо глобальная.
    """
    norm = norm_hours_for(urgent_task.executor)
    hours = urgent_task.plan_hours or 0

    if hours <= 0:
        return 1  # срочная без оценки = минимум 1 день

    return max(1, int(math.ceil(hours / norm)))

def _correct_urgent_shift(urgent_task):
    """Коррекция сдвига срочной после её закрытия или отмены.

    Логика:
      - planned_shift_days — сколько срочная «съела» при создании.
      - fact_days — сколько фактически потратила (по accumulated_hours).
      - correction = fact_days - planned_shift_days.
      - Досдвигаем/откатываем credit исполнителя на эту разницу.
      - Идемпотентна: после применения сбрасывает planned_shift_days=0,
        повторный вызов — no-op.

    Возвращает (число затронутых задач, 'extend'|'revert'|'none').
    """
    planned = urgent_task.planned_shift_days or 0
    if planned <= 0:
        # Срочная без применённого сдвига (отменена до approve, не срочная
        # или уже скорректирована). Ничего не делаем.
        return 0, 'none'

    executor = urgent_task.executor
    if executor is None:
        return 0, 'none'

    norm = norm_hours_for(executor)
    fact_hours = urgent_task.accumulated_hours or 0
    fact_days = int(math.ceil(fact_hours / norm)) if fact_hours > 0 else 0

    correction = fact_days - planned
    if correction == 0:
        urgent_task.planned_shift_days = 0
        urgent_task.save(update_fields=['planned_shift_days'])
        return 0, 'none'

    executor.shift_credit_days = (executor.shift_credit_days or 0) + correction
    executor.save(update_fields=['shift_credit_days'])

    urgent_task.planned_shift_days = 0
    urgent_task.save(update_fields=['planned_shift_days'])

    affected = recompute_shift_for_executor(
        executor,
        actor=urgent_task.requester,
        source_task=urgent_task,
        exclude_task_pk=urgent_task.pk,
    )

    action_word = 'Досдвиг' if correction > 0 else 'Откат'
    TaskLog.objects.create(
        task=urgent_task,
        kind=TaskLog.Kind.DUE,
        author=urgent_task.requester,
        source_task=None,
        shift_days=correction,
        comment=(
            f'{action_word} {correction:+d} раб. дн. '
            f'(срочная «{urgent_task.title}»: '
            f'план {planned} раб. дн., факт {fact_days} раб. дн., '
            f'{fact_hours:.1f} ч)'
        ),
    )

    return affected, ('extend' if correction > 0 else 'revert')


def _auto_pause_in_progress(executor, urgent_task):
    """Авто-пауза задач исполнителя, которые сейчас in_progress.

    Нужно при получении срочной, чтобы фокус автоматически переключился.
    """
    active = list(
        Task.objects.filter(
            executor=executor,
            status=Task.Status.IN_PROGRESS,
        ).exclude(pk=urgent_task.pk)
    )

    paused = 0
    for t in active:
        try:
            services.pause(t)
        except Exception:
            t.status = Task.Status.PAUSED
            t.save(update_fields=['status'])

        TaskLog.objects.create(
            task=t,
            kind=TaskLog.Kind.DUE,
            author=urgent_task.requester,
            source_task=urgent_task,
            comment=(
                f'Авто-пауза: получена срочная задача «{urgent_task.title}»'
            ),
        )
        paused += 1

    return paused


# ─────────────────────────────────────────────────────────────
# Цепочки этапов (TaskBranch)
# ─────────────────────────────────────────────────────────────

def _next_stage(task):
    """Следующий этап в той же ветке, если есть."""
    if not task.branch_id or task.stage_order is None:
        return None
    return (
        Task.objects
        .filter(
            branch_id=task.branch_id,
            stage_order__gt=task.stage_order,
        )
        .exclude(status=Task.Status.CANCELLED)
        .order_by('stage_order')
        .first()
    )


def _prev_stage(task):
    """Предыдущий этап в той же ветке, если есть."""
    if not task.branch_id or task.stage_order is None:
        return None
    return (
        Task.objects
        .filter(
            branch_id=task.branch_id,
            stage_order__lt=task.stage_order,
        )
        .order_by('-stage_order')
        .first()
    )


def _activate_stage(task):
    """Разблокировать задачу-этап: убрать флаг blocked_by_stage."""
    if not task.blocked_by_stage:
        return False
    task.blocked_by_stage = False
    task.save(update_fields=['blocked_by_stage'])
    try:
        notify_task_created(task)
    except Exception:
        pass
    return True


def _on_stage_closed(task, actor):
    """Закрытие этапа: активировать следующий или запросить сдвиг у руководителя.

    Возвращает:
      {'activated': Task|None, 'blocked': Task|None, 'reason': str, 'shift': int}
    reason: 'last_stage' | 'overdue' | 'ok' | 'already_active'
    """
    nxt = _next_stage(task)
    if nxt is None:
        return {'activated': None, 'blocked': None, 'reason': 'last_stage', 'shift': 0}

    today = timezone.localdate()
    finished = _local_date(task.finished_at) or today

    if task.due and finished > task.due:
        shift = (finished - task.due).days
        nxt.blocked_by_stage = True
        nxt.pending_shift_days = shift
        nxt.pending_shift_from_task = task
        nxt.save(update_fields=[
            'blocked_by_stage', 'pending_shift_days', 'pending_shift_from_task',
        ])
        TaskLog.objects.create(
            task=nxt,
            kind=TaskLog.Kind.DUE,
            author=actor,
            source_task=task,
            shift_days=shift,
            comment=(
                f'Предыдущий этап «{task.title}» просрочен на {shift} дн '
                f'({task.due} → {finished}). Руководителю нужно решить: '
                f'сдвинуть этап или активировать как есть.'
            ),
        )

        # ── Уведомить постановщика и руководителей отдела исполнителя ──
        try:
            from comms.services import notify
            from comms.models import Notification

            recipients = []

            if task.requester_id and task.requester and task.requester.is_active:
                recipients.append(task.requester)

            if task.executor_id and task.executor and task.executor.department_id:
                bosses = User.objects.filter(
                    department_id=task.executor.department_id,
                    role__can_manage=True,
                    is_active=True,
                )
                recipients.extend(bosses)

            # Дедупликация (постановщик мог оказаться и руководителем отдела)
            seen = set()
            unique_recipients = []
            for u in recipients:
                if u and u.pk and u.pk not in seen:
                    seen.add(u.pk)
                    unique_recipients.append(u)

            if unique_recipients:
                cabinet_url = reverse('manager_cabinet') + '#pending-shift'
                notify(
                    unique_recipients,
                    f'⚠ Этап «{task.title}» просрочен на {shift} д. '
                    f'Следующий этап «{nxt.title}» ждёт решения.',
                    cabinet_url,
                    Notification.Kind.ACTION,
                )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                '_on_stage_closed: notify failed for task %s', task.pk,
            )

        return {'activated': None, 'blocked': nxt, 'reason': 'overdue', 'shift': shift}

    if _activate_stage(nxt):
        return {'activated': nxt, 'blocked': None, 'reason': 'ok', 'shift': 0}
    return {'activated': nxt, 'blocked': None, 'reason': 'already_active', 'shift': 0}


def _branch_chain(task):
    """Полный список этапов ветки для отображения в карточке задачи."""
    if not task.branch_id:
        return None

    stages = list(
        Task.objects
        .filter(branch_id=task.branch_id)
        .exclude(status=Task.Status.CANCELLED)
        .select_related('executor')
        .order_by('stage_order')
    )
    if not stages:
        return None

    items = []
    for i, t in enumerate(stages, start=1):
        items.append({
            'task': t,
            'index': i,
            'is_current': t.pk == task.pk,
        })
    return {
        'branch': task.branch,
        'items': items,
        'total': len(stages),
        'done_count': sum(1 for t in stages if t.status == Task.Status.DONE),
    }


def _parse_period(request):
    """Период по пресету или произвольным датам (GET p, from, to)."""
    p = request.GET.get('p', '30')
    today = timezone.localdate()

    if p == 'all':
        return None, None, p

    if p == 'custom':
        f = request.GET.get('from', '')
        t = request.GET.get('to', '')
        since = _parse_date(f)
        until = _parse_date(t)
        return since, until, p

    days = {'7': 7, '30': 30, '90': 90}.get(p, 30)
    return today - timedelta(days=days - 1), today, p


def _collect_tree(user, flt, since=None, until=None):
    qs = (
        Task.objects
        .filter(blocked_by_stage=False)
        .select_related('executor', 'requester', 'parent')
    )
    # Фильтруем по дате в Python — единый источник правды (timezone.localtime).
    # SQL-фильтр по created_at__date использует таймзону БД и может «терять»
    # задачи на границе суток при разнице с TIME_ZONE.
    tasks = list(qs.order_by('-created_at')[:5000])

    if since or until:
        tasks = [
            t for t in tasks
            if (
                    (since is None or _local_date(t.created_at) >= since)
                    and (until is None or _local_date(t.created_at) <= until)
            )
        ]

    by_id = {t.pk: t for t in tasks}
    children = {}
    for t in tasks:
        children.setdefault(t.parent_id, []).append(t)

    def matches(t):
        if flt == 'mine':
            return t.executor_id == user.id or t.requester_id == user.id
        if flt == 'open':
            return t.status not in (Task.Status.DONE, Task.Status.CANCELLED)
        return True

    memo = {}

    def has_match(t):
        if t.pk in memo:
            return memo[t.pk]
        res = matches(t) or any(has_match(c) for c in children.get(t.pk, []))
        memo[t.pk] = res
        return res

    roll = {}

    def rollup(t):
        if t.pk in roll:
            return roll[t.pk]
        cnt = done = 0
        plan = fact = 0.0
        for c in children.get(t.pk, []):
            cc, dd, pp, ff = rollup(c)
            cnt += cc + 1
            done += dd + (1 if c.status == Task.Status.DONE else 0)
            plan += pp + (c.plan_hours or 0)
            fact += ff + (c.accumulated_hours or 0)
        res = (cnt, done, plan, fact)
        roll[t.pk] = res
        return res

    def build(t, depth):
        node = {
            'task': t,
            'children': [],
            'roll': rollup(t),
            'state': _state_of(t),
        }
        if depth < MAX_DEPTH:
            for c in sorted(children.get(t.pk, []), key=lambda x: x.created_at):
                if has_match(c):
                    node['children'].append(build(c, depth + 1))
        return node

    roots = []
    for t in sorted(tasks, key=lambda x: x.created_at):
        if not _can_see(user, t):
            continue
        parent = by_id.get(t.parent_id)
        if t.parent_id is not None and parent is not None and _can_see(user, parent):
            continue
        if has_match(t):
            roots.append(build(t, 0))

    return roots


# ─────────────────────────────────────────────────────────────
# Реестр задач: единая точка /tasks/?view=tree|timeline|calendar
# ─────────────────────────────────────────────────────────────

REGISTRY_VIEWS = ('tree', 'timeline', 'calendar')


@login_required
def tasks_registry(request):
    """Единая точка входа в реестр задач.

    Разные виды (дерево, хронология, календарь) — один URL,
    параметр ?view=. По умолчанию — дерево.

    Старые URL'ы /tasks/tree/, /tasks/timeline/, /tasks/calendar/
    остаются и редиректят сюда с нужным view.
    """
    view = (request.GET.get('view') or 'tree').strip()
    if view not in REGISTRY_VIEWS:
        view = 'tree'

    if view == 'tree':
        return _render_tree(request)
    if view == 'timeline':
        return _render_timeline(request)
    return _render_calendar(request)


def _redirect_registry(request, view):
    """Редирект на /tasks/?view=<view> с сохранением остальных GET-параметров."""
    qs = request.GET.copy()
    qs['view'] = view
    return redirect(f"{reverse('tasks_registry')}?{qs.urlencode()}")


@login_required
def task_tree(request):
    """Старый URL → реестр в виде дерева."""
    return _redirect_registry(request, 'tree')


@login_required
def timeline(request):
    """Старый URL → реестр в виде хронологии."""
    return _redirect_registry(request, 'timeline')


@login_required
def calendar_view(request):
    """Старый URL → реестр в виде календаря."""
    return _redirect_registry(request, 'calendar')


# ─────────────────────────────────────────────────────────────
# Дерево задач
# ─────────────────────────────────────────────────────────────

def _render_tree(request):
    """Реестр задач в виде дерева. Вызывается из tasks_registry."""
    flt = request.GET.get('f', 'open')
    if flt not in ('open', 'all', 'mine'):
        flt = 'open'

    since, until, p = _parse_period(request)
    roots = _collect_tree(request.user, flt, since, until)

    return render(request, 'tasks/tree.html', {
        'roots': roots,
        'flt': flt,
        'p': p,
        'from_iso': since.isoformat() if since else '',
        'to_iso': until.isoformat() if until else '',
        'from_view': _due_view(since.isoformat() if since else ''),
        'to_view': _due_view(until.isoformat() if until else ''),
        'orders': Order.objects.all(),
        'registry_view': 'tree',
    })


@login_required
def tree_export(request):
    """CSV ветки для планёрки с периодом: BOM + ';' для Excel."""
    flt = request.GET.get('f', 'open')
    if flt not in ('open', 'all', 'mine'):
        flt = 'open'

    since, until, p = _parse_period(request)
    roots = _collect_tree(request.user, flt, since, until)

    fname = 'svod_tree'
    if since:
        fname += f'_{since.isoformat()}'
    if until:
        fname += f'_{until.isoformat()}'

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{fname}.csv"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')
    writer.writerow([
        'Уровень', 'Задача', 'Статус', 'Масштаб', 'Исполнитель',
        'Подразделение', 'Постановщик', 'Создана', 'Срок начала',
        'План, ч', 'Факт, ч', 'Подзадач закрыто',
    ])

    def walk(node, depth):
        t = node['task']
        cnt, done, plan, fact = node['roll']
        writer.writerow([
            depth,
            t.title,
            t.get_status_display(),
            t.get_scale_display(),
            t.executor.full_name,
            t.executor.department.name if t.executor.department else '',
            t.requester.full_name,
            _local_date(t.created_at).isoformat(),
            t.due.strftime('%d.%m.%Y') if t.due else '',
            f'{(t.plan_hours or 0):.2f}',
            f'{(t.accumulated_hours or 0):.2f}',
            f'{done} из {cnt}' if cnt else '',
        ])
        for c in node['children']:
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)

    return response


# ─────────────────────────────────────────────────────────────
# Карточка задачи
# ─────────────────────────────────────────────────────────────

@login_required
def task_detail(request, pk):
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    if not _can_see(user, task):
        raise PermissionDenied('Нет доступа к этой задаче.')

    task.spent_text = format_spent(task.spent_hours)

    children = list(task.children.select_related('executor').all())
    for c in children:
        c.spent_text = format_spent(c.spent_hours)

    sessions = task.sessions.order_by('-finished_at')
    progress = task.progress.select_related('author').all()

    can_comment = (task.executor_id == user.id) or user.is_boss

    open_st = (Task.Status.DONE, Task.Status.CANCELLED)
    can_decompose = _can_decompose(user, task)
    can_create_rework = (
            task.status == Task.Status.DONE
            and _can_create_rework(user, task)
    )
    can_close = user.is_boss and task.status not in open_st
    can_cancel = user.is_boss and task.status not in open_st

    # Может ли текущий пользователь вносить сверхурочные по этой задаче:
    #  - суперюзер/админ портала — всегда;
    #  - руководитель отдела исполнителя — только если исполнитель из его отдела;
    #  - начальник завода — нет (только смотрит свод).
    exec_dept_id = task.executor.department_id if task.executor_id else None
    can_create_overtime = (
            user.is_superuser
            or user.is_admin_role
            or (
                    user.is_boss
                    and user.department_id
                    and exec_dept_id
                    and user.department_id == exec_dept_id
            )
    )

    shop_open = None
    shop_total = 0.0
    for s in task.shop_sessions.all():
        if s.finished_at:
            shop_total += s.duration_hours
        else:
            shop_open = s
            shop_total += (timezone.now() - s.started_at).total_seconds() / 3600

    # ── Сверхурочные по этой задаче ──
    from .models import OvertimeRecord
    overtime_records = list(
        OvertimeRecord.objects
        .filter(task=task)
        .select_related('user', 'created_by')
        .order_by('-date', '-created_at')
    )
    overtime_total_hours = round(
        sum(r.hours or 0 for r in overtime_records), 1
    )

    can_shop = task.executor_id == user.id and task.status not in open_st

    # Соседи по ветке
    prev_stage = _prev_stage(task) if task.branch_id else None
    next_stage = _next_stage(task) if task.branch_id else None

    return render(request, 'tasks/detail.html', {
        'task': task,
        'children': children,
        'sessions': sessions,
        'progress': progress,
        'can_comment': can_comment,
        'can_decompose': can_decompose,
        'can_create_rework': can_create_rework,
        'can_create_overtime': can_create_overtime,
        'can_close': can_close,
        'can_cancel': can_cancel,
        'can_edit': task.requester_id == user.id or user.is_boss,
        'shop_open': shop_open,
        'shop_total': shop_total,
        'can_shop': can_shop,
        'overtime_records': overtime_records,
        'overtime_total_hours': overtime_total_hours,
        'orders': Order.objects.all(),
        'prev_stage': prev_stage,
        'next_stage': next_stage,
        'chain': _branch_chain(task),
        'today': timezone.localdate(),
    })


@login_required
def task_progress_add(request, pk):
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    if request.method == 'POST' and (task.executor_id == user.id or user.is_boss):
        text = request.POST.get('text', '').strip()

        if text:
            note = TaskProgress.objects.create(task=task, author=user, text=text[:2000])

            attach_ids = [
                x for x in (request.POST.get('attach_ids') or '').split(',')
                if x.isdigit()
            ]
            if attach_ids:
                Attachment.objects.filter(
                    pk__in=attach_ids,
                    owner=user,
                    task__isnull=True,
                    message__isnull=True,
                    progress__isnull=True,
                ).update(progress=note)

            messages.success(request, 'Запись добавлена.')
        else:
            messages.error(request, 'Пустая запись не добавлена.')

    return redirect(request.META.get('HTTP_REFERER', f'/tasks/task/{pk}/'))


# ─────────────────────────────────────────────────────────────
# Действия с задачей
# ─────────────────────────────────────────────────────────────

def _close_shop_sessions(task, actor=None):
    """Закрыть активные цеховые сессии по задаче.

    Когда задача уходит из активного состояния (review / accept / close /
    cancel / rework) — нельзя оставлять «висящие» открытые сессии.
    Иначе пользователь видит «🏭 в цехе» по уже закрытой задаче,
    а integrity-проверка ругается на старые открытые ShopSession.
    """
    from .models import ShopSession

    now = timezone.now()
    for s in ShopSession.objects.filter(task=task, finished_at__isnull=True):
        s.finished_at = now
        s.duration_hours = (now - s.started_at).total_seconds() / 3600
        s.save(update_fields=['finished_at', 'duration_hours'])


@login_required
def task_action(request, pk, action):
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    own = task.executor_id == user.id
    mgr = _is_manager(user) or task.requester_id == user.id

    if request.method != 'POST':
        messages.error(request, 'Действия с задачей доступны только кнопкой на странице задачи.')
        return redirect(request.META.get('HTTP_REFERER', '/'))

    # Задача ждёт согласования сдвига — почти все действия запрещены
    if task.status == Task.Status.PENDING_APPROVAL:
        allowed = {'cancel'}   # отменить может только постановщик/босс
        if action not in allowed:
            messages.warning(
                request,
                'Задача ожидает согласования сдвига плана. '
                'Работа начнётся после решения руководителя.'
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

    if action in ('start', 'resume') and own:
        if task.status not in (
                Task.Status.NEW,
                Task.Status.PAUSED,
                Task.Status.REWORK,
        ):
            messages.warning(
                request,
                f'Начать задачу в статусе «{task.get_status_display()}» нельзя.',
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

        prev_status = task.status
        services.start_or_resume(task, user)
        notify_task_started(task, user, resumed=(prev_status == Task.Status.PAUSED))

    elif action == 'pause' and own:
        if task.status != Task.Status.IN_PROGRESS:
            messages.warning(
                request,
                'Поставить на паузу можно только задачу в работе.',
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

        services.pause(task)

    elif action == 'review' and own:
        if task.status not in (
                Task.Status.IN_PROGRESS,
                Task.Status.PAUSED,
                Task.Status.REWORK,
        ):
            messages.warning(
                request,
                'На проверку можно отправить задачу в работе, '
                'на паузе или на доработке.',
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

        services.submit_for_review(task)
        _close_shop_sessions(task, user)

    elif action == 'close' and user.is_boss:
        if task.status in (Task.Status.DONE, Task.Status.CANCELLED):
            messages.warning(request, 'Задача уже закрыта или отменена.')
            return redirect(request.META.get('HTTP_REFERER', '/'))

        services.close_task(task)
        _close_shop_sessions(task, user)
        TaskLog.objects.create(task=task, kind=TaskLog.Kind.CLOSE, author=user)
        # ...остальное без изменений

        # Срочная — корректируем сдвиг
        if task.priority == 'urgent':
            affected, kind = _correct_urgent_shift(task)
            if affected:
                direction = 'откатили' if kind == 'revert' else 'досдвинули'
                messages.info(
                    request,
                    f'Коррекция срочной: {direction} сроки {affected} задач.',
                )

        # Цепочки этапов: запустить следующий этап ветки
        stage_result = _on_stage_closed(task, user)
        if stage_result['reason'] == 'overdue':
            messages.warning(
                request,
                f'Этап «{task.title}» просрочен. '
                f'Следующий этап «{stage_result["blocked"].title}» ждёт '
                f'подтверждения сдвига у руководителя.',
            )
        elif stage_result['activated'] is not None and stage_result['reason'] == 'ok':
            messages.info(
                request,
                f'Запущен следующий этап: «{stage_result["activated"].title}».',
            )

    elif action == 'cancel' and user.is_boss:
        if task.status in (Task.Status.DONE, Task.Status.CANCELLED):
            messages.error(request, 'Задача уже закрыта или отменена.')
        else:
            from .models import TimeSession
            _close_shop_sessions(task, user)

            if task.session_started_at:
                delta_h = (timezone.now() - task.session_started_at).total_seconds() / 3600
                TimeSession.objects.create(
                    task=task,
                    executor=task.executor,
                    started_at=task.session_started_at,
                    finished_at=timezone.now(),
                    duration_hours=delta_h,
                    status_at_close=task.status,
                    comment='Сеанс закрыт отменой задачи',
                )
                task.accumulated_hours = (task.accumulated_hours or 0) + delta_h
                task.session_started_at = None

            task.status = Task.Status.CANCELLED
            task.finished_at = timezone.now()
            task.save(update_fields=[
                'status', 'finished_at', 'accumulated_hours', 'session_started_at',
            ])
            TaskLog.objects.create(
                task=task,
                kind=TaskLog.Kind.CANCEL,
                author=user,
                comment=request.POST.get('comment', '')[:500],
            )
            messages.success(request, f'Задача «{task.title}» отменена.')

            # Если отменяем срочную — откатить сдвиг с учётом потраченного времени
            if task.priority == 'urgent':
                affected, kind = _correct_urgent_shift(task)
                if affected:
                    messages.info(
                        request,
                        f'Срочная отменена, сроки {affected} задач скорректированы.',
                    )
            # Цепочки этапов при отмене НЕ запускаются: следующий этап
            # продолжает ждать предыдущий, пока его не закроют.

    elif action == 'accept' and mgr:
        if task.status != Task.Status.REVIEW:
            messages.warning(
                request,
                'Принять можно только задачу на проверке.',
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

        services.manager_accept(task)
        _close_shop_sessions(task, user)
        TaskLog.objects.create(task=task, kind=TaskLog.Kind.ACCEPT, author=user)

        # Срочная — корректируем сдвиг
        if task.priority == 'urgent':
            affected, kind = _correct_urgent_shift(task)
            if affected:
                direction = 'откатили' if kind == 'revert' else 'досдвинули'
                messages.info(
                    request,
                    f'Коррекция срочной: {direction} сроки {affected} задач.',
                )

        # Цепочки этапов: запустить следующий этап ветки
        stage_result = _on_stage_closed(task, user)
        if stage_result['reason'] == 'overdue':
            messages.warning(
                request,
                f'Этап «{task.title}» просрочен. '
                f'Следующий этап «{stage_result["blocked"].title}» ждёт '
                f'подтверждения сдвига у руководителя.',
            )
        elif stage_result['activated'] is not None and stage_result['reason'] == 'ok':
            messages.info(
                request,
                f'Запущен следующий этап: «{stage_result["activated"].title}».',
            )

    elif action == 'rework' and mgr:
        if task.status != Task.Status.REVIEW:
            messages.warning(
                request,
                'Вернуть на доработку можно только задачу на проверке.',
            )
            return redirect(request.META.get('HTTP_REFERER', '/'))

        reason = request.POST.get('comment', '').strip()[:500]
        task._rework_comment = reason
        services.manager_rework(task)
        _close_shop_sessions(task, user)
        TaskLog.objects.create(
            task=task,
            kind=TaskLog.Kind.REWORK,
            author=user,
            comment=reason,
        )

    elif action == 'shop_start' and own:
        from .models import ShopSession
        from django.db import transaction, IntegrityError

        try:
            with transaction.atomic():
                open_any = ShopSession.objects.select_for_update().filter(
                    executor=user, finished_at__isnull=True
                ).first()

                if open_any:
                    messages.error(
                        request,
                        f'Вы уже в цехе по задаче «{open_any.task.title}» — сначала вернитесь.',
                    )
                elif task.status == Task.Status.CANCELLED:
                    messages.error(
                        request,
                        'Задача отменена — выход в цех невозможен.',
                    )
                elif task.status == Task.Status.DONE:
                    messages.warning(
                        request,
                        'Задача закрыта. Попросите руководителя зафиксировать '
                        'доработку — она появится отдельной подзадачей.',
                    )
                else:
                    ShopSession.objects.create(task=task, executor=user)
                    notify_shop_started(task, user)
                    messages.success(
                        request,
                        'Вы в цехе: основной таймер задачи не остановлен, цеховой таймер идёт.',
                    )
        except IntegrityError:
            messages.error(request, 'Сеанс уже создан. Обновите страницу.')

    elif action == 'shop_stop' and own:
        from .models import ShopSession

        s = ShopSession.objects.filter(
            task=task, executor=user, finished_at__isnull=True
        ).first()

        if not s:
            messages.error(request, 'Открытого сеанса в цехе по этой задаче нет.')
        else:
            s.finished_at = timezone.now()
            s.duration_hours = (s.finished_at - s.started_at).total_seconds() / 3600
            s.save(update_fields=['finished_at', 'duration_hours'])
            messages.success(request, f'Возврат из цеха: в цехе {s.duration_hours:.2f} ч.')

    else:
        messages.error(request, 'Это действие сейчас недоступно.')

    return redirect(request.META.get('HTTP_REFERER', '/'))


# ─────────────────────────────────────────────────────────────
# Создание задачи
# ─────────────────────────────────────────────────────────────

@login_required
def task_create(request):
    """Задачи ставят все; подзадачу создаёт исполнитель родительской задачи или руководитель."""
    user = request.user
    departments = Department.objects.all()
    users_data = [
        {'id': u.pk, 'name': u.full_name, 'dept': u.department_id}
        for u in User.objects.filter(
            is_active=True,
            employment_status__in=[
                User.EmploymentStatus.ACTIVE,
                User.EmploymentStatus.VACATION,
                User.EmploymentStatus.MATERNITY,
            ],
        ).order_by('full_name')
    ]

    parent = None
    parent_id = request.GET.get('parent') or request.POST.get('parent') or ''
    if parent_id.isdigit():
        candidate = Task.objects.filter(pk=int(parent_id)).first()
        if candidate:
            if _can_decompose(user, candidate):
                parent = candidate
            else:
                messages.error(
                    request,
                    'Разбивать на подзадачи можно только свои открытые задачи '
                    '(или будучи руководителем). Доработку к закрытой задаче '
                    'фиксирует руководитель отдела исполнителя.',
                )
                return redirect('task_detail', pk=candidate.pk)

    # Если есть родитель — унаследуем его заказ и ветку
    forced_order = parent.order if parent and parent.order_id else None
    forced_branch = parent.branch if parent and parent.branch_id else None

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        start_raw = request.POST.get('start_due', '').strip()
        due_raw = request.POST.get('due', '').strip()
        priority = request.POST.get('priority', 'medium')
        scale = request.POST.get('scale', 's')
        kind = request.POST.get('kind', 'work')

        # Типовая задача (если выбрана в форме «Быстрый выбор»)
        _type_pk = (request.POST.get('type') or '').strip()
        task_type = (
            TaskType.objects.filter(pk=int(_type_pk)).first()
            if _type_pk.isdigit() else None
        )

        errors = []

        outside_order = request.POST.get('outside_order') == '1'
        order_pk = (request.POST.get('order') or '').strip()

        # Подзадача — заказ наследуется от родителя
        if forced_order:
            order = forced_order
        elif outside_order:
            order = None
        else:
            order = Order.objects.filter(pk=order_pk).first() if order_pk.isdigit() else None
            if not order:
                errors.append('Выберите заказ или отметьте «Не касается заказа».')

        # ── Этап (ветка) ──
        outside_chain = request.POST.get('outside_chain') == '1'
        branch_pk = (request.POST.get('branch') or '').strip()

        branch = None
        stage_order = None

        if forced_branch:
            # Подзадача наследует ветку родителя
            branch = forced_branch
            if not outside_chain:
                last = (
                    Task.objects
                    .filter(branch=branch)
                    .order_by('-stage_order')
                    .first()
                )
                stage_order = (last.stage_order + 1) if last and last.stage_order else 1
        elif order and branch_pk.isdigit() and not outside_chain:
            branch = TaskBranch.objects.filter(pk=int(branch_pk), order=order).first()
            if branch:
                last = (
                    Task.objects
                    .filter(branch=branch)
                    .order_by('-stage_order')
                    .first()
                )
                stage_order = (last.stage_order + 1) if last and last.stage_order else 1
        elif order and branch_pk.isdigit() and outside_chain:
            # Вне цепочки — ветка есть, stage_order пусто
            branch = TaskBranch.objects.filter(pk=int(branch_pk), order=order).first()

        valid_scales = [c[0] for c in Task.Scale.choices]
        valid_kinds = [c[0] for c in Task.Kind.choices]

        if scale not in valid_scales:
            scale = 's'
        if kind not in valid_kinds:
            kind = 'work'

        plan = parse_plan(request.POST.get('plan', ''), scale, norm_hours())

        if not title:
            errors.append('Укажите название задачи.')
        if not plan or plan <= 0:
            if scale in ('l', 'xl'):
                plan = 0.0
            else:
                errors.append('Время на задачу: например 2, или 30 мин, или 1:15')

        start_due = _parse_date(start_raw)
        if start_raw and start_due is None:
            errors.append('Срок начала: выберите дату в календаре')

        due = _parse_date(due_raw)
        if due_raw and due is None:
            errors.append('Срок: выберите дату в календаре')

        external_due_raw = request.POST.get('external_due', '').strip()
        external_due = _parse_date(external_due_raw)
        if external_due_raw and external_due is None:
            errors.append('Формальный срок: выберите дату в календаре')

        if start_due and due and start_due > due:
            errors.append('Срок начала не может быть позже дедлайна.')

        if due and external_due and due > external_due:
            messages.warning(
                request,
                'Внутренний срок позже формального — исполнитель не успеет '
                'к обещанной дате, даже если уложится в свой дедлайн.',
            )

        exec_ids = [
            x for x in (request.POST.get('exec_ids') or '').split(',')
            if x.isdigit()
        ]
        executors = list(User.objects.filter(pk__in=exec_ids, is_active=True))
        if not executors:
            errors.append('Отметьте исполнителей: кнопкой подразделения или галочками в списке.')

        executor = executors[0] if len(executors) == 1 else None

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            attach_ids = [
                x for x in (request.POST.get('attach_ids') or '').split(',')
                if x.isdigit()
            ]

            if executor is not None:
                new_task = Task.objects.create(
                    title=title, plan_hours=plan,
                    start_due=start_due, due=due,
                    original_start_due=start_due, original_due=due,
                    external_due=external_due,
                    priority=priority, scale=scale, kind=kind, parent=parent,
                    order=order, branch=branch, stage_order=stage_order,
                    task_type=task_type,
                    executor=executor, requester=user,
                    body=request.POST.get('body', ''),
                )
                notify_task_created(new_task)

                # F.3b — предупреждение, если исполнитель в отпуске
                vac_warn = _executor_vacation_warning(
                    new_task.executor,
                    start_due=new_task.start_due,
                    due=new_task.due,
                )
                if vac_warn:
                    messages.warning(request, vac_warn)

                if attach_ids:
                    Attachment.objects.filter(
                        pk__in=attach_ids, owner=user,
                        task__isnull=True, message__isnull=True,
                        progress__isnull=True,
                    ).update(task=new_task)

                if priority == 'urgent' and new_task.due:
                    from tasks import plan_shift
                    reason = (request.POST.get('urgent_reason') or '').strip()
                    if not reason:
                        reason = 'Срочно (обоснование не указано)'

                    req = plan_shift.create_request(new_task, reason)
                    if req:
                        messages.info(
                            request,
                            f'Срочная задача создана и ждёт согласования сдвига '
                            f'у {req.get_current_level_display()}.'
                        )
                    else:
                        messages.info(
                            request,
                            'Срочно: сдвиг применён автоматически '
                            '(в отделе нет согласующих).'
                        )

                messages.success(
                    request,
                    'Задача создана.' if not parent
                    else f'Подзадача к «{parent.title}» создана.',
                )
            else:
                from django.db import transaction

                with transaction.atomic():
                    group = Task.objects.create(
                        title=title, plan_hours=plan,
                        start_due=start_due, due=due,
                        original_start_due=start_due, original_due=due,
                        external_due=external_due,
                        priority=priority, scale=scale, kind=kind, parent=parent,
                        order=order, branch=branch, stage_order=stage_order,
                        task_type=task_type,
                        executor=executors[0],
                        requester=user,
                        body=request.POST.get('body', ''),
                    )
                    if attach_ids:
                        Attachment.objects.filter(
                            pk__in=attach_ids, owner=user,
                            task__isnull=True, message__isnull=True,
                            progress__isnull=True,
                        ).update(task=group)

                    for ex in executors:
                        child = Task.objects.create(
                            title=title, plan_hours=plan,
                            start_due=start_due, due=due,
                            original_start_due=start_due, original_due=due,
                            external_due=external_due,
                            priority=priority, scale=scale, kind=kind,
                            parent=group,
                            order=order, branch=branch,
                            stage_order=stage_order,
                            task_type=task_type,
                            executor=ex,
                            requester=user,
                            body=request.POST.get('body', ''),
                        )
                        notify_task_created(child)

                        # F.3b — предупреждение по каждому исполнителю
                        vac_warn = _executor_vacation_warning(
                            child.executor,
                            start_due=child.start_due,
                            due=child.due,
                        )
                        if vac_warn:
                            messages.warning(request, vac_warn)

                if priority == 'urgent':
                    from tasks import plan_shift
                    reason = (request.POST.get('urgent_reason') or '').strip()
                    if not reason:
                        reason = 'Срочно (обоснование не указано)'

                    for ex in executors:
                        new_child = Task.objects.filter(
                            parent=group, executor=ex,
                        ).order_by('-pk').first()
                        if not new_child or not new_child.due:
                            continue
                        plan_shift.create_request(new_child, reason)

                    messages.info(
                        request,
                        f'Срочная групповая задача: заявки на сдвиг созданы '
                        f'({len(executors)} исполнителей).'
                    )

                messages.success(
                    request,
                    f'Групповая задача создана: {len(executors)} исполнителя(ей).',
                )

            me = request.user
            me.last_department = executors[0].department if executors else None
            me.last_executor = executors[0] if len(executors) == 1 else None
            me.save(update_fields=['last_department', 'last_executor'])

            return redirect('dashboard')

        initial_start_due = start_due.strftime('%Y-%m-%d') if start_due else ''
        initial_due = due.strftime('%Y-%m-%d') if due else ''
        initial_external_due = (
            external_due.strftime('%Y-%m-%d') if external_due else ''
        )
        initial_order = order_pk
        initial_dep_id = (
            executors[0].department_id
            if executors and executors[0].department_id
            else ''
        )
        initial_exec_ids = ','.join(exec_ids)
        initial_kind = kind
    else:
        initial_start_due = ''
        initial_due = ''
        initial_external_due = ''
        initial_order = (request.GET.get('order') or '').strip()
        initial_dep_id = user.last_department_id or ''
        initial_exec_ids = (
            str(user.last_executor_id)
            if (
                    user.last_executor_id
                    and user.last_executor
                    and user.last_executor.department_id == user.last_department_id
            )
            else ''
        )
        initial_kind = 'work'

    return render(request, 'staff/task_form.html', {
        'types': TaskType.objects.all(),
        'users_data': users_data,
        'depts_data': [{'id': d.pk, 'name': d.name} for d in departments],
        'scales': Task.Scale.choices,
        'kinds': Task.Kind.choices,
        'orders': Order.objects.order_by('number'),
        'initial_order': int(initial_order) if initial_order.isdigit() else None,
        'initial_start_due': initial_start_due,
        'initial_start_due_view': _due_view(initial_start_due),
        'initial_due': initial_due,
        'initial_due_view': _due_view(initial_due),
        'initial_external_due': initial_external_due,
        'initial_external_due_view': _due_view(initial_external_due),
        'initial_dep_id': initial_dep_id,
        'initial_exec_ids': initial_exec_ids,
        'initial_kind': initial_kind,
        'parent_task': parent,
        'norm': norm_hours(),
        'recent_executors': (
            User.objects
            .filter(issued_tasks__requester=user, is_active=True)
            .exclude(pk=user.pk)
            .annotate(cnt=Count('issued_tasks'))
            .order_by('-cnt')[:5]
        ),
        'frequent_types': (
            TaskType.objects
            .filter(tasks__requester=user, is_active=True)
            .annotate(cnt=Count('tasks'))
            .order_by('-cnt')[:5]
        ),
        'departments': (
            Department.objects
            .filter(users__is_active=True)
            .distinct()
            .order_by('name')
        ),
        'branches_by_order': {
            str(o.pk): list(
                TaskBranch.objects.filter(order=o).values('pk', 'name').order_by('name')
            )
            for o in Order.objects.all()
        },
    })


# ─────────────────────────────────────────────────────────────
# Правка задачи
# ─────────────────────────────────────────────────────────────

@login_required
def task_edit(request, pk):
    """Правка плана/срока/приоритета постановщиком или руководителем — с журналом."""
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    if not (task.requester_id == user.id or user.is_boss):
        raise PermissionDenied('Изменять может постановщик или руководитель.')

    if request.method == 'POST':
        old_plan, old_due, old_prio = task.plan_hours, task.due, task.priority
        old_start = task.start_due
        old_external = task.external_due

        new_plan = parse_plan(request.POST.get('plan', ''), task.scale, norm_hours())
        if new_plan:
            task.plan_hours = new_plan

        due_raw = request.POST.get('due', '').strip()
        if due_raw:
            parsed = _parse_date(due_raw)
            if parsed:
                task.due = parsed

        start_raw = request.POST.get('start_due', '').strip()
        if start_raw:
            parsed = _parse_date(start_raw)
            if parsed:
                task.start_due = parsed

        external_raw = request.POST.get('external_due', '').strip()
        if external_raw:
            parsed = _parse_date(external_raw)
            if parsed:
                task.external_due = parsed
        elif 'external_due' in request.POST:
            # поле было в форме, но очищено
            task.external_due = None

        prio = request.POST.get('priority', task.priority)
        if prio in [c[0] for c in Task.Priority.choices]:
            task.priority = prio

        changes = []

        if task.plan_hours != old_plan:
            c = f'план {old_plan:.2f} → {task.plan_hours:.2f} ч'
            changes.append(c)
            TaskLog.objects.create(task=task, kind=TaskLog.Kind.PLAN, author=user, comment=c)

        if task.due != old_due:
            c = f'срок {old_due or "—"} → {task.due or "—"}'
            changes.append(c)
            TaskLog.objects.create(task=task, kind=TaskLog.Kind.DUE, author=user, comment=c)

        if task.start_due != old_start:
            c = f'начало {old_start or "—"} → {task.start_due or "—"}'
            changes.append(c)
            TaskLog.objects.create(task=task, kind=TaskLog.Kind.DUE, author=user, comment=c)

        if task.priority != old_prio:
            c = f'приоритет {old_prio} → {task.priority}'
            changes.append(c)
            TaskLog.objects.create(task=task, kind=TaskLog.Kind.PRIO, author=user, comment=c)

        if task.external_due != old_external:
            c = f'формальный срок {old_external or "—"} → {task.external_due or "—"}'
            changes.append(c)
            TaskLog.objects.create(
                task=task, kind=TaskLog.Kind.DUE, author=user, comment=c,
            )

        task.save()
        messages.success(
            request,
            ('Изменено: ' + '; '.join(changes)) if changes else 'Без изменений.',
        )

    return redirect('task_detail', pk=pk)


# ─────────────────────────────────────────────────────────────
# План недели
# ─────────────────────────────────────────────────────────────

@login_required
def week_toggle(request, pk):
    """Взять / снять задачу в план недели (только свои)."""
    task = get_object_or_404(Task, pk=pk)

    if task.executor_id != request.user.id:
        messages.error(request, 'В план недели можно брать только свои задачи.')
    elif request.method == 'POST':
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        wc = WeekCommit.objects.filter(
            user=request.user, task=task, week_start=monday
        ).first()

        if wc:
            wc.delete()
            messages.info(request, 'Убрано из плана недели.')
        else:
            WeekCommit.objects.create(user=request.user, task=task, week_start=monday)
            messages.success(request, 'Добавлено в план недели.')

    return redirect(request.META.get('HTTP_REFERER', '/'))


# ─────────────────────────────────────────────────────────────
# Поиск
# ─────────────────────────────────────────────────────────────

@login_required
def search(request):
    """Глобальный поиск: задачи, люди, заказы."""
    q = request.GET.get('q', '').strip()
    tasks = people = orders = []

    if len(q) >= 2:
        tasks = list(
            Task.objects.filter(title__icontains=q)
            .select_related('executor')
            .order_by('-created_at')[:20]
        )
        people = list(
            User.objects.filter(full_name__icontains=q, is_active=True)[:10]
        )
        orders = list(
            Order.objects.filter(
                Q(number__icontains=q) | Q(product__icontains=q)
            ).order_by('number')[:10]
        )

    return render(request, 'tasks/search.html', {
        'q': q, 'tasks': tasks, 'people': people, 'orders': orders,
    })


# ─────────────────────────────────────────────────────────────
# Календарь
# ─────────────────────────────────────────────────────────────

def _render_calendar(request):
    """Календарь сроков на месяц. Вызывается из tasks_registry."""
    import calendar as cal_mod
    from datetime import date

    user = request.user
    today = timezone.localdate()

    try:
        y = int(request.GET.get('y', today.year))
        m = int(request.GET.get('m', today.month))
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        y, m = today.year, today.month

    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

    qs = (
        Task.objects.filter(due__gte=first, due__lt=nxt)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .select_related('executor')
    )

    if not (user.is_boss or user.is_admin_role):
        qs = qs.filter(Q(executor=user) | Q(requester=user))

    by_day = {}
    for t in qs:
        by_day.setdefault(t.due.day, []).append(t)

    cal_mod.setfirstweekday(0)
    weeks = []
    for week in cal_mod.monthcalendar(y, m):
        weeks.append([
            {'day': d, 'tasks': by_day.get(d, []) if d else []}
            for d in week
        ])

    prev_m = m - 1 or 12
    prev_y = y if m > 1 else y - 1
    next_m = m + 1 if m < 12 else 1
    next_y = y if m < 12 else y + 1

    return render(request, 'tasks/calendar.html', {
        'weeks': weeks, 'y': y, 'm': m, 'today': today,
        'month_name': first.strftime('%B %Y'),
        'prev': {'y': prev_y, 'm': prev_m},
        'next': {'y': next_y, 'm': next_m},
        'registry_view': 'calendar',
    })


# ─────────────────────────────────────────────────────────────
# Хронология
# ─────────────────────────────────────────────────────────────

def _render_timeline(request):
    """Хронология: месяц/квартал/год; полосы «начало → дедлайн».

    Вызывается из tasks_registry. Права проверяются здесь же:
    хронология — только boss/admin.
    """
    import calendar as cal_mod
    from datetime import date

    user = request.user
    if not (user.is_boss or user.is_admin_role):
        raise PermissionDenied('Хронология доступна руководителю и администратору.')

    today = timezone.localdate()
    # Источник данных: 'live' (текущие задачи) или 'snapshot' (история из снимков)
    source = (request.GET.get('source') or 'live').strip()
    if source not in ('live', 'snapshot'):
        source = 'live'

    try:
        y = int(request.GET.get('y', today.year))
        m = int(request.GET.get('m', today.month))
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        y, m = today.year, today.month

    period = request.GET.get('period', 'month')
    if period not in ('month', 'quarter', 'year'):
        period = 'month'

    show_closed = request.GET.get('closed') == '1'

    if period == 'month':
        start = date(y, m, 1)
        end = date(y, m, cal_mod.monthrange(y, m)[1])
        period_label = start.strftime('%B %Y')
    elif period == 'quarter':
        q = (m - 1) // 3
        qm = q * 3 + 1
        start = date(y, qm, 1)
        em = qm + 2
        end = date(y, em, cal_mod.monthrange(y, em)[1])
        period_label = f'Квартал {q + 1} · {y}'
    else:
        start = date(y, 1, 1)
        end = date(y, 12, 31)
        period_label = f'{y} год'

    total = (end - start).days + 1

    cols = []
    if period == 'month':
        for dnum in range(1, end.day + 1):
            d = date(y, m, dnum)
            cols.append({'label': dnum, 'weekend': d.weekday() >= 5, 'today': d == today})
    elif period == 'quarter':
        cur = start - timedelta(days=start.weekday())
        while cur <= end:
            cols.append({
                'label': cur.strftime('%d.%m'),
                'weekend': False,
                'today': cur <= today <= cur + timedelta(days=6),
            })
            cur += timedelta(days=7)
    else:
        names = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
                 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']
        for mm in range(1, 13):
            cols.append({
                'label': names[mm - 1],
                'weekend': False,
                'today': mm == today.month and y == today.year,
            })

    # Фильтр по scope
    scope = user.scope

    # ─── Ветка 1: живая хронология ───
    if source == 'live':
        qs = Task.objects.select_related(
            'executor', 'requester', 'executor__department'
        )
        if scope == 'department' and user.department_id:
            qs = qs.filter(executor__department_id=user.department_id)

        if not show_closed:
            qs = qs.exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])

        rows = []
        for t in qs:
            s = t.start_due or _local_date(t.created_at)
            e = t.due or t.start_due or s
            if s > e:
                s, e = e, s
            if e < start or s > end:
                continue
            s = max(s, start)
            e = min(e, end)

            if t.status == Task.Status.DONE:
                cls = 'done'
            elif t.status == Task.Status.CANCELLED:
                cls = 'cancel'
            else:
                over = bool(t.due and t.due < today)
                cls = 'late' if over else ('hot' if t.priority in ('high', 'urgent') else '')

            left_pct = (s - start).days / total * 100
            width_pct = ((e - s).days + 1) / total * 100

            rows.append({
                'task': t,
                'cls': cls,
                'left_num': left_pct,
                'bar_style': f'left:{left_pct:.2f}%;width:{max(width_pct, 0.6):.2f}%',
                'source': 'live',
            })

    # ─── Ветка 2: хронология по снимкам ───
    else:
        # Берём последний снимок за каждый день периода для каждой задачи
        from tasks.models import TimelineSnapshot

        snaps_qs = (
            TimelineSnapshot.objects
            .filter(snapshot_date__gte=start, snapshot_date__lte=end)
            .select_related(
                'task', 'executor', 'executor__department',
                'task__requester',
            )
        )
        if scope == 'department' and user.department_id:
            snaps_qs = snaps_qs.filter(executor__department_id=user.department_id)

        if show_closed:
            snaps_qs = snaps_qs.filter(
                status__in=[Task.Status.DONE, Task.Status.CANCELLED]
            )
        else:
            snaps_qs = snaps_qs.exclude(
                status__in=[Task.Status.DONE, Task.Status.CANCELLED]
            )

        # Для каждой задачи — последний снимок в периоде
        by_task = {}
        for snap in snaps_qs.order_by('task_id', '-snapshot_date'):
            if snap.task_id not in by_task:
                by_task[snap.task_id] = snap

        rows = []
        for snap in by_task.values():
            s = snap.start_date or start
            e = snap.due_date or snap.start_date or s
            if s > e:
                s, e = e, s
            if e < start or s > end:
                continue
            s = max(s, start)
            e = min(e, end)

            if snap.status == Task.Status.DONE:
                cls = 'done'
            elif snap.status == Task.Status.CANCELLED:
                cls = 'cancel'
            else:
                over = bool(snap.due_date and snap.due_date < today)
                cls = 'late' if over else ('hot' if snap.priority in ('high', 'urgent') else '')

            left_pct = (s - start).days / total * 100
            width_pct = ((e - s).days + 1) / total * 100

            rows.append({
                'task': snap.task,
                'cls': cls,
                'left_num': left_pct,
                'bar_style': f'left:{left_pct:.2f}%;width:{max(width_pct, 0.6):.2f}%',
                'source': 'snapshot',
                'snap': snap,
            })

    prev_m = m - 1 or 12
    prev_y = y if m > 1 else y - 1
    next_m = m + 1 if m < 12 else 1
    next_y = y if m < 12 else y + 1

    return render(request, 'tasks/timeline.html', {
        'page_title': 'Хронология',
        'period_label': period_label,
        'y': y,
        'm': m,
        'period': period,
        'source': source,
        'cols': cols,
        'rows': rows,
        'prev': {'y': prev_y, 'm': prev_m},
        'next': {'y': next_y, 'm': next_m},
        'show_closed': show_closed,
        'closed_q': '1' if show_closed else '0',
        'registry_view': 'timeline',
    })


@login_required
def timeline_export(request):
    """CSV-выгрузка хронологии за месяц/квартал/год."""
    import calendar as cal_mod
    from datetime import date

    user = request.user
    if not (user.is_boss or user.is_admin_role):
        raise PermissionDenied('Хронология доступна руководителю и администратору.')

    today = timezone.localdate()

    try:
        y = int(request.GET.get('y', today.year))
        m = int(request.GET.get('m', today.month))
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        y, m = today.year, today.month

    period = request.GET.get('period', 'month')
    if period not in ('month', 'quarter', 'year'):
        period = 'month'

    if period == 'month':
        start = date(y, m, 1)
        end = date(y, m, cal_mod.monthrange(y, m)[1])
        label = start.strftime('%B %Y')
    elif period == 'quarter':
        q = (m - 1) // 3
        qm = q * 3 + 1
        start = date(y, qm, 1)
        em = qm + 2
        end = date(y, em, cal_mod.monthrange(y, em)[1])
        label = f'Квартал {q + 1} · {y}'
    else:
        start = date(y, 1, 1)
        end = date(y, 12, 31)
        label = f'{y} год'

    show_closed = request.GET.get('closed') == '1'
    source = (request.GET.get('source') or 'live').strip()
    if source not in ('live', 'snapshot'):
        source = 'live'

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="timeline_{y}_{period}_{source}.csv"'
    response.write('\ufeff')

    w = csv.writer(response, delimiter=';')
    w.writerow([f'ХРОНОЛОГИЯ ({source}): {label}'])

    if source == 'live':
        qs = Task.objects.select_related('executor', 'requester', 'executor__department')

        scope = user.scope
        if scope == 'department' and user.department_id:
            qs = qs.filter(executor__department_id=user.department_id)

        if show_closed:
            qs = qs.filter(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        else:
            qs = qs.exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])

        w.writerow([
            'Задача', 'Исполнитель', 'Постановщик', 'Приоритет', 'Статус',
            'Начало', 'Дедлайн', 'План, ч', 'Факт, ч',
        ])

        for t in qs.order_by('start_due', 'due'):
            s = t.start_due or _local_date(t.created_at)
            e = t.due or t.start_due or s
            if s > e:
                s, e = e, s
            if e < start or s > end:
                continue
            w.writerow([
                t.title,
                t.executor.full_name,
                t.requester.full_name,
                t.get_priority_display(),
                t.get_status_display(),
                s.strftime('%d.%m.%Y'),
                e.strftime('%d.%m.%Y'),
                f'{t.plan_hours or 0:.1f}',
                f'{t.accumulated_hours or 0:.1f}',
            ])
    else:
        from tasks.models import TimelineSnapshot

        snaps_qs = (
            TimelineSnapshot.objects
            .filter(snapshot_date__gte=start, snapshot_date__lte=end)
            .select_related('task', 'executor', 'executor__department', 'task__requester')
        )
        scope = user.scope
        if scope == 'department' and user.department_id:
            snaps_qs = snaps_qs.filter(executor__department_id=user.department_id)

        if show_closed:
            snaps_qs = snaps_qs.filter(
                status__in=[Task.Status.DONE, Task.Status.CANCELLED]
            )
        else:
            snaps_qs = snaps_qs.exclude(
                status__in=[Task.Status.DONE, Task.Status.CANCELLED]
            )

        by_task = {}
        for snap in snaps_qs.order_by('task_id', '-snapshot_date'):
            if snap.task_id not in by_task:
                by_task[snap.task_id] = snap

        w.writerow([
            'Дата снимка', 'Задача', 'Исполнитель', 'Постановщик', 'Приоритет',
            'Статус', 'Начало', 'Дедлайн', 'Было дедлайн', 'План, ч',
            'Накоплено, ч', 'Списано за день, ч', 'Смена статуса',
        ])

        for snap in by_task.values():
            w.writerow([
                snap.snapshot_date.strftime('%d.%m.%Y'),
                snap.task.title,
                snap.executor.full_name,
                snap.task.requester.full_name if snap.task.requester else '',
                snap.get_priority_display(),
                snap.get_status_display(),
                snap.start_date.strftime('%d.%m.%Y') if snap.start_date else '',
                snap.due_date.strftime('%d.%m.%Y') if snap.due_date else '',
                snap.due_before.strftime('%d.%m.%Y') if snap.due_before else '',
                f'{snap.plan_hours or 0:.1f}',
                f'{snap.accumulated_hours or 0:.1f}',
                f'{snap.hours_today or 0:.1f}',
                'да' if snap.status_changed else 'нет',
            ])

    return response


# ─────────────────────────────────────────────────────────────
# Сдвиг сроков
# ─────────────────────────────────────────────────────────────

@login_required
def task_shift(request, pk):
    """Сдвиг сроков задачи на N дней (хронология). Журналируется."""
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    if request.method != 'POST':
        return redirect(request.META.get('HTTP_REFERER', '/'))

    if not (
            task.executor_id == user.id
            or task.requester_id == user.id
            or user.is_boss
    ):
        messages.error(request, 'Сдвиг недоступен: вы не исполнитель и не постановщик.')
        return redirect(request.META.get('HTTP_REFERER', '/'))

    try:
        days = int(request.POST.get('days', '0'))
    except ValueError:
        days = 0

    if days == 0 or abs(days) > 60:
        messages.error(request, 'Сдвиг: укажите от 1 до 60 дней.')
    elif not task.start_due and not task.due:
        messages.error(request, 'У задачи нет сроков для сдвига.')
    else:
        if task.start_due:
            task.start_due = task.start_due + timedelta(days=days)
        if task.due:
            task.due = task.due + timedelta(days=days)

        task.save(update_fields=['start_due', 'due'])

        c = f'сдвиг {days:+d} дн: начало {task.start_due or "—"}, дедлайн {task.due or "—"}'
        TaskLog.objects.create(task=task, kind=TaskLog.Kind.DUE, author=user, comment=c)
        messages.success(request, f'Сдвиг {days:+d} дн: дедлайн {task.due or "—"}.')

    return redirect(request.META.get('HTTP_REFERER', '/'))


@login_required
def stage_shift_resolve(request, pk, action):
    """Реакция руководителя на просрочку этапа.

    action: 'shift'  — сдвинуть сроки этапа на pending_shift_days и активировать
            'skip'   — активировать без сдвига
            'defer'  — оставить этап заблокированным (отложить решение)
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    task = get_object_or_404(Task, pk=pk)
    back = request.META.get('HTTP_REFERER') or reverse('manager_cabinet')

    if request.method != 'POST':
        return redirect(back)

    if task.pending_shift_days is None:
        messages.error(request, 'Для этого этапа нет запроса на сдвиг.')
        return redirect(back)

    shift = task.pending_shift_days
    source = task.pending_shift_from_task

    if action == 'shift':
        delta = timedelta(days=shift)
        if task.start_due:
            task.start_due = task.start_due + delta
        if task.due:
            task.due = task.due + delta
        task.pending_shift_days = None
        task.pending_shift_from_task = None
        task.blocked_by_stage = False
        task.save(update_fields=[
            'start_due', 'due',
            'pending_shift_days', 'pending_shift_from_task', 'blocked_by_stage',
        ])
        TaskLog.objects.create(
            task=task,
            kind=TaskLog.Kind.DUE,
            author=user,
            source_task=source,
            shift_days=shift,
            comment=(
                f'Сдвиг +{shift} дн по решению руководителя '
                f'(просрочка этапа «{source.title if source else "—"}»).'
            ),
        )
        try:
            notify_task_created(task)
        except Exception:
            pass
        messages.success(
            request,
            f'Этап «{task.title}» сдвинут на +{shift} дн и активирован.',
        )

    elif action == 'skip':
        task.pending_shift_days = None
        task.pending_shift_from_task = None
        task.blocked_by_stage = False
        task.save(update_fields=[
            'pending_shift_days', 'pending_shift_from_task', 'blocked_by_stage',
        ])
        TaskLog.objects.create(
            task=task,
            kind=TaskLog.Kind.DUE,
            author=user,
            source_task=source,
            comment='Сдвиг не применён, этап активирован по решению руководителя.',
        )
        try:
            notify_task_created(task)
        except Exception:
            pass
        messages.info(request, f'Этап «{task.title}» активирован без сдвига.')

    elif action == 'defer':
        messages.info(request, 'Решение отложено. Этап остаётся заблокированным.')

    else:
        messages.error(request, 'Неизвестное действие.')

    return redirect(back)


# ─────────────────────────────────────────────────────────────
# Доработка закрытой задачи
# ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def task_rework_create(request, pk):
    """Создать подзадачу-доработку к закрытой задаче.

    Доступно только руководителю ОТДЕЛА ИСПОЛНИТЕЛЯ, суперюзеру или
    админу портала. Постановщик, сам исполнитель и руководитель другого
    отдела — не могут: доработка — управленческое решение.

    Создаёт подзадачу с автозаполнением из parent: тот же исполнитель,
    заказ, ветка, вид работы. План/срок — опционально задаются в модале.
    """
    parent = get_object_or_404(Task, pk=pk)

    if not _can_create_rework(request.user, parent):
        raise PermissionDenied(
            'Зафиксировать доработку может только руководитель отдела '
            'исполнителя или администратор портала.'
        )

    if parent.status != Task.Status.DONE:
        messages.warning(
            request,
            'Зафиксировать доработку можно только для закрытой задачи.',
        )
        return redirect('task_detail', pk=parent.pk)

    plan_raw = (request.POST.get('plan') or '').strip()
    due_raw = (request.POST.get('due') or '').strip()
    reason = (request.POST.get('reason') or '').strip()[:500]

    norm = norm_hours_for(parent.executor)
    plan = parse_plan(plan_raw, 's', norm) if plan_raw else 0
    if plan is None:
        plan = 0

    due = _parse_date(due_raw)

    task = Task.objects.create(
        title=f'[доработка] {parent.title}',
        plan_hours=plan,
        start_due=None,
        due=due,
        original_start_due=None,
        original_due=due,
        priority=parent.priority,
        scale=parent.scale,
        kind=parent.kind,
        parent=parent,
        order=parent.order,
        branch=parent.branch,
        stage_order=None,       # доработка не часть цепочки этапов
        blocked_by_stage=False,
        task_type=parent.task_type,
        executor=parent.executor,
        requester=request.user,
        body=(
                f'Доработка к задаче #{parent.pk} «{parent.title}».'
                + (f'\n\nОбоснование: {reason}' if reason else '')
        ),
    )

    try:
        notify_task_created(task)
    except Exception:
        pass

    TaskLog.objects.create(
        task=parent,
        kind=TaskLog.Kind.CLOSE,
        author=request.user,
        comment=f'Создана доработка #{task.pk}' + (f': {reason}' if reason else ''),
    )

    messages.success(
        request,
        f'Доработка создана: подзадача #{task.pk}.',
    )
    return redirect('task_detail', pk=task.pk)
