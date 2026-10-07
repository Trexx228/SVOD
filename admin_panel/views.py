from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction, models
from django.db.models import Q, Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.urls import reverse


from datetime import datetime
import csv
import io
import re

from accounts.models import Department, Role, SupportTicket
from core.models import TaskType, Norm
from .models import LoginEvent, AuditLog
from .audit import log_action
from tasks.models import Task

User = get_user_model()


def _admin_required(user):
    if not (user.is_superuser or user.is_admin_role):
        raise PermissionDenied('Раздел доступен только администраторам.')


def _boss_required(user):
    """Руководитель или админ."""
    if not (user.is_superuser or user.is_boss):
        raise PermissionDenied('Раздел доступен руководителям.')


def _apply_boss_scope(qs, user, dept_path):
    if user.is_superuser or user.can_plant or user.is_admin_role:
        return qs
    if user.department_id:
        return qs.filter(**{dept_path: user.department_id})
    return qs.none()


def _is_narrow_scope(user):
    return bool(
        not user.is_superuser
        and not user.can_plant
        and not user.is_admin_role
        and user.is_boss
        and user.department_id
    )


def _is_admin_user(u):
    return bool(u.is_superuser or (u.role and u.role.can_admin))


def _active_admin_count(exclude_user_ids=None, exclude_role_ids=None):
    qs = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__can_admin=True)
    )
    if exclude_user_ids:
        qs = qs.exclude(pk__in=exclude_user_ids)
    if exclude_role_ids:
        qs = qs.exclude(
            Q(role_id__in=exclude_role_ids) & Q(is_superuser=False)
        )
    return qs.count()


def _ci_filter(q, *fields):
    q = (q or '').strip()
    if not q:
        return Q()
    pattern = re.escape(q)
    cond = Q()
    for f in fields:
        cond |= Q(**{f'{f}__iregex': pattern})
    return cond


def _guard_deactivation(user, request):
    if user.pk == request.user.pk:
        return 'Нельзя деактивировать самого себя.'
    if _is_admin_user(user) and _active_admin_count(exclude_user_ids=[user.pk]) == 0:
        return 'Это последний активный администратор. Сначала назначьте другого.'
    return None


def _check_single_admin_alert():
    from .alerts import raise_alert, resolve_alert
    from .models import AdminAlert

    cnt = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__can_admin=True)
    ).count()

    if cnt < 2:
        raise_alert(
            kind=AdminAlert.Kind.SINGLE_ADMIN,
            key='single_admin',
            severity=AdminAlert.Severity.DANGER,
            title='В системе один активный администратор',
            message='Назначьте резервного админа — иначе можно потерять доступ.',
            url='/settings/users/',
        )
    else:
        resolve_alert('single_admin')


# ═════════════════════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛИ
# ═════════════════════════════════════════════════════════════

@login_required
def users_list(request):
    _admin_required(request.user)

    # 1. Читаем параметры из GET
    q = request.GET.get('q', '').strip()
    dept_pk = request.GET.get('dept', '').strip()
    role_pk = request.GET.get('role', '').strip()
    active_filter = request.GET.get('active', '')
    status_filter = request.GET.get('status', '').strip()

    # 2. Создаём базовый qs
    qs = (
        User.objects
        .select_related('department', 'role')
        .order_by('full_name')
    )

    # 3. Фильтр по статусу занятости
    if status_filter == 'dismissed':
        qs = qs.filter(employment_status=User.EmploymentStatus.DISMISSED)
    elif status_filter == 'vacation':
        # «В отпуске сейчас»: либо статус VACATION (ручная отметка),
        # либо период vacation_from..vacation_to покрывает сегодня.
        today = timezone.localdate()
        qs = qs.filter(
            Q(employment_status=User.EmploymentStatus.VACATION)
            | Q(vacation_from__lte=today, vacation_to__gte=today)
        )
    elif status_filter == 'all':
        pass
    else:
        qs = qs.filter(
            employment_status__in=[
                User.EmploymentStatus.ACTIVE,
                User.EmploymentStatus.VACATION,
                User.EmploymentStatus.MATERNITY,
            ]
        )

    # 4. Остальные фильтры (уже были)
    if q:
        qs = qs.filter(_ci_filter(q, 'full_name', 'email'))
    if dept_pk.isdigit():
        qs = qs.filter(department_id=int(dept_pk))
    if role_pk.isdigit():
        qs = qs.filter(role_id=int(role_pk))
    if active_filter == '1':
        qs = qs.filter(is_active=True)
    elif active_filter == '0':
        qs = qs.filter(is_active=False)

    # 5. Пагинация и контекст — как было
    total = qs.count()
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'admin_panel/users.html', {
        'users': page.object_list,
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'departments': Department.objects.all().order_by('name'),
        'roles': Role.objects.all().order_by('name'),
        'q': q,
        'dept_pk': dept_pk,
        'role_pk': role_pk,
        'active_filter': active_filter,
        'status_filter': status_filter,
    })


@login_required
def users_bulk(request):
    _admin_required(request.user)
    if request.method != 'POST':
        return redirect('settings_users')

    ids = request.POST.getlist('ids')
    action = (request.POST.get('bulk_action') or '').strip()

    if not ids:
        messages.error(request, 'Никого не выбрано.')
        return redirect('settings_users')

    targets = list(User.objects.filter(pk__in=ids))
    if not targets:
        messages.error(request, 'Выбранные пользователи не найдены.')
        return redirect('settings_users')

    if action == 'activate':
        changed = 0
        for u in targets:
            if not u.is_active:
                u.is_active = True
                u.save(update_fields=['is_active'])
                log_action(request, AuditLog.Action.TOGGLE, u, changes={'is_active': True})
                changed += 1

                _check_single_admin_alert()

        messages.success(request, f'Активировано: {changed} из {len(targets)}.')


    elif action == 'dismiss':
        changed = 0
        skipped = []
        for u in targets:
            err = _guard_deactivation(u, request)
            if err:
                skipped.append(u.full_name)
                continue
            if u.is_active or not u.is_dismissed:
                u.dismiss(reason='Массовое увольнение')
                log_action(
                    request, AuditLog.Action.UPDATE, u,
                    changes={'employment_status': 'dismissed', 'source': 'bulk'},
                )
                changed += 1
        msg = f'Уволено: {changed}.'
        if skipped:
            msg += ' Пропущено: ' + ', '.join(skipped) + '.'
        messages.success(request, msg)

    elif action == 'set_role':
        role_pk = (request.POST.get('bulk_role') or '').strip()
        if role_pk == 'none':
            # «Сбросить» теперь = «Пользователь» (не пустая роль)
            new_role = User.get_default_role()
        elif role_pk.isdigit():
            new_role = Role.objects.filter(pk=int(role_pk)).first()
            if not new_role:
                messages.error(request, 'Роль не найдена.')
                return redirect('settings_users')
        else:
            messages.error(request, 'Роль не выбрана.')
            return redirect('settings_users')

        new_can_admin = bool(new_role and new_role.can_admin)

        # Считаем будущий счётчик админов СРАЗУ с учётом всей пачки.
        target_ids = [u.pk for u in targets]

        # Кто в пачке потеряет админку
        losing_admin_ids = [
            u.pk for u in targets
            if _is_admin_user(u) and not (u.is_superuser or new_can_admin)
        ]

        # Кто останется админом ВНЕ пачки
        outside_admins = User.objects.filter(is_active=True).filter(
            Q(is_superuser=True) | Q(role__can_admin=True)
        ).exclude(pk__in=target_ids).count()

        # Кто в пачке останется админом: суперюзеры + (если new_role.can_admin) все
        su_in_batch = sum(1 for u in targets if u.is_superuser and u.is_active)
        admins_in_batch_after = len(targets) if new_can_admin else su_in_batch

        total_admins_after = outside_admins + admins_in_batch_after

        if losing_admin_ids and total_admins_after == 0:
            if request.user.pk in target_ids and request.user.pk in losing_admin_ids:
                messages.error(
                    request,
                    'Вы последний активный администратор. Нельзя снять с себя '
                    'админскую роль. Сначала назначьте нового администратора.',
                )
            else:
                messages.error(
                    request,
                    'Нельзя снять админку с последнего активного администратора.',
                )
            return redirect('settings_users')

        for u in targets:
            u.role = new_role
            u.save(update_fields=['role'])
            log_action(request, AuditLog.Action.UPDATE, u,
                       changes={'role_id': new_role.pk if new_role else None})
        if new_role:
            messages.success(request, f'Роль «{new_role.name}» назначена: {len(targets)}.')
        else:
            messages.success(request, f'Роль сброшена у {len(targets)}.')

    elif action == 'set_department':
        dept_pk = (request.POST.get('bulk_department') or '').strip()
        if dept_pk == 'none':
            for u in targets:
                u.department = None
                u.save(update_fields=['department'])
                log_action(request, AuditLog.Action.UPDATE, u, changes={'department_id': None})
            messages.success(request, f'Подразделение сброшено у {len(targets)}.')
        elif dept_pk.isdigit():
            dept = Department.objects.filter(pk=int(dept_pk)).first()
            if not dept:
                messages.error(request, 'Подразделение не найдено.')
                return redirect('settings_users')
            for u in targets:
                u.department = dept
                u.save(update_fields=['department'])
                log_action(request, AuditLog.Action.UPDATE, u, changes={'department_id': dept.pk})
            messages.success(request, f'Подразделение «{dept.name}» назначено: {len(targets)}.')
        else:
            messages.error(request, 'Подразделение не выбрано.')

    else:
        messages.error(request, 'Действие не выбрано.')

    return redirect('settings_users')


@login_required
def users_export(request):
    _admin_required(request.user)

    q = request.GET.get('q', '').strip()
    dept_pk = request.GET.get('dept', '').strip()
    role_pk = request.GET.get('role', '').strip()
    active_filter = request.GET.get('active', '')

    qs = (
        User.objects
        .select_related('department', 'role')
        .order_by('full_name')
    )

    if q:
        qs = qs.filter(_ci_filter(q, 'full_name', 'email'))
    if dept_pk.isdigit():
        qs = qs.filter(department_id=int(dept_pk))
    if role_pk.isdigit():
        qs = qs.filter(role_id=int(role_pk))
    if active_filter == '1':
        qs = qs.filter(is_active=True)
    elif active_filter == '0':
        qs = qs.filter(is_active=False)

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = (
            'attachment; filename="users_'
            + datetime.now().strftime('%Y%m%d_%H%M')
            + '.csv"'
    )
    # BOM для корректной кириллицы в Excel
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')
    writer.writerow([
        'ФИО', 'Email', 'Подразделение', 'Роль',
        'Код роли', 'Активен', 'Суперпользователь',
        'Дата регистрации', 'Последняя активность',
    ])

    for u in qs:
        writer.writerow([
            u.full_name,
            u.email,
            u.department.name if u.department else '',
            u.role.name if u.role else '',
            u.role.code if u.role else '',
            'да' if u.is_active else 'нет',
            'да' if u.is_superuser else 'нет',
            u.date_joined.strftime('%d.%m.%Y %H:%M') if u.date_joined else '',
            u.last_activity_at.strftime('%d.%m.%Y %H:%M') if u.last_activity_at else '',
        ])

    log_action(
        request, AuditLog.Action.UPDATE, request.user,
        changes={'export': 'users_csv', 'rows': qs.count()},
    )
    return response


@login_required
def tasks_export(request):
    _admin_required(request.user)

    from tasks.models import Task

    status = request.GET.get('status', '').strip()
    priority = request.GET.get('priority', '').strip()
    executor_pk = request.GET.get('executor', '').strip()
    requester_pk = request.GET.get('requester', '').strip()
    due_from = request.GET.get('due_from', '').strip()
    due_to = request.GET.get('due_to', '').strip()

    qs = (
        Task.objects
        .select_related('executor', 'requester', 'order', 'branch', 'task_type')
        .order_by('-created_at')
    )

    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    if executor_pk.isdigit():
        qs = qs.filter(executor_id=int(executor_pk))
    if requester_pk.isdigit():
        qs = qs.filter(requester_id=int(requester_pk))
    if due_from:
        qs = qs.filter(due__gte=due_from)
    if due_to:
        qs = qs.filter(due__lte=due_to)

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = (
            'attachment; filename="tasks_'
            + datetime.now().strftime('%Y%m%d_%H%M')
            + '.csv"'
    )
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')
    writer.writerow([
        'ID', 'Задача', 'Статус', 'Приоритет', 'Масштаб', 'Вид',
        'План, ч', 'Накоплено, ч', 'Срок начала', 'Срок',
        'Исполнитель', 'Постановщик',
        'Заказ', 'Ветка', 'Этап', 'Типовая',
        'Создана', 'Завершена',
    ])

    for t in qs:
        writer.writerow([
            t.pk,
            t.title,
            t.get_status_display(),
            t.get_priority_display(),
            t.get_scale_display(),
            t.get_kind_display(),
            f'{t.plan_hours:.2f}'.replace('.', ','),
            f'{t.accumulated_hours:.2f}'.replace('.', ','),
            t.start_due.strftime('%d.%m.%Y') if t.start_due else '',
            t.due.strftime('%d.%m.%Y') if t.due else '',
            t.executor.full_name if t.executor else '',
            t.requester.full_name if t.requester else '',
            t.order.number if t.order else '',
            t.branch.name if t.branch else '',
            t.stage_order if t.stage_order else '',
            t.task_type.name if t.task_type else '',
            t.created_at.strftime('%d.%m.%Y %H:%M') if t.created_at else '',
            t.finished_at.strftime('%d.%m.%Y %H:%M') if t.finished_at else '',
        ])

    log_action(
        request, AuditLog.Action.UPDATE, request.user,
        changes={'export': 'tasks_csv', 'rows': qs.count()},
    )
    return response


@login_required
def tasks_admin_list(request):
    _admin_required(request.user)

    from tasks.models import Task

    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    priority = request.GET.get('priority', '').strip()
    executor_pk = request.GET.get('executor', '').strip()
    requester_pk = request.GET.get('requester', '').strip()
    due_from = request.GET.get('due_from', '').strip()
    due_to = request.GET.get('due_to', '').strip()

    qs = (
        Task.objects
        .select_related('executor', 'requester', 'task_type', 'order')
        .order_by('-created_at')
    )

    if q:
        qs = qs.filter(_ci_filter(q, 'title', 'body'))
    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    if executor_pk.isdigit():
        qs = qs.filter(executor_id=int(executor_pk))
    if requester_pk.isdigit():
        qs = qs.filter(requester_id=int(requester_pk))
    if due_from:
        qs = qs.filter(due__gte=due_from)
    if due_to:
        qs = qs.filter(due__lte=due_to)

    total = qs.count()
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'admin_panel/tasks_admin_list.html', {
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'q': q,
        'status': status,
        'priority': priority,
        'executor_pk': executor_pk,
        'requester_pk': requester_pk,
        'due_from': due_from,
        'due_to': due_to,
        'status_choices': Task.Status.choices,
        'priority_choices': Task.Priority.choices,
        'users': User.objects.filter(is_active=True).order_by('full_name'),
        'today': timezone.localdate(),
    })


@login_required
def tasks_bulk(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_tasks_page')

    from datetime import date as _date
    from django.utils import timezone
    from tasks.models import Task, TaskLog

    ids = request.POST.getlist('ids')
    action = (request.POST.get('bulk_action') or '').strip()

    if not ids:
        messages.error(request, 'Ничего не выбрано.')
        return redirect('settings_tasks_page')

    targets = list(Task.objects.select_related('executor').filter(pk__in=ids))
    if not targets:
        messages.error(request, 'Задачи не найдены.')
        return redirect('settings_tasks_page')

    now = timezone.now()
    changed = 0

    if action == 'set_priority':
        prio = (request.POST.get('bulk_priority') or '').strip()
        if prio not in dict(Task.Priority.choices):
            messages.error(request, 'Некорректный приоритет.')
            return redirect('settings_tasks_page')
        for t in targets:
            if t.priority != prio:
                old = t.priority
                t.priority = prio
                t.save(update_fields=['priority'])
                TaskLog.objects.create(
                    task=t, kind=TaskLog.Kind.PRIO, author=request.user,
                    comment=f'Массовая смена приоритета: {old} → {prio}',
                )
                changed += 1

    elif action == 'set_due':
        due_str = (request.POST.get('bulk_due') or '').strip()
        try:
            y, m, d = map(int, due_str.split('-'))
            new_due = _date(y, m, d)
        except (ValueError, AttributeError):
            messages.error(request, 'Некорректная дата (нужен формат ГГГГ-ММ-ДД).')
            return redirect('settings_tasks_page')
        for t in targets:
            if t.due != new_due:
                old = t.due
                t.due = new_due
                t.save(update_fields=['due'])
                TaskLog.objects.create(
                    task=t, kind=TaskLog.Kind.DUE, author=request.user,
                    comment=f'Массовая смена срока: {old or "—"} → {new_due}',
                )
                changed += 1

    elif action == 'set_executor':
        ex_pk = (request.POST.get('bulk_executor') or '').strip()
        if not ex_pk.isdigit():
            messages.error(request, 'Исполнитель не выбран.')
            return redirect('settings_tasks_page')
        new_executor = User.objects.filter(pk=int(ex_pk), is_active=True).first()
        if not new_executor:
            messages.error(request, 'Исполнитель не найден или отключён.')
            return redirect('settings_tasks_page')
        for t in targets:
            if t.executor_id != new_executor.pk:
                old = t.executor.full_name if t.executor else '—'
                t.executor = new_executor
                t.save(update_fields=['executor'])
                TaskLog.objects.create(
                    task=t, kind=TaskLog.Kind.PLAN, author=request.user,
                    comment=f'Массовая смена исполнителя: {old} → {new_executor.full_name}',
                )
                changed += 1

    elif action == 'cancel':
        for t in targets:
            if t.status != Task.Status.CANCELLED:
                t.status = Task.Status.CANCELLED
                t.finished_at = now
                t.session_started_at = None
                t.save(update_fields=['status', 'finished_at', 'session_started_at'])
                TaskLog.objects.create(
                    task=t, kind=TaskLog.Kind.CANCEL, author=request.user,
                    comment='Массовая отмена',
                )
                changed += 1

    else:
        messages.error(request, 'Действие не выбрано.')
        return redirect('settings_tasks_page')

    log_action(request, AuditLog.Action.UPDATE, request.user, changes={
        'bulk': action,
        'ids': [t.pk for t in targets],
        'changed': changed,
    })
    messages.success(request, f'Изменено: {changed} из {len(targets)}.')
    return redirect('settings_tasks_page')


@login_required
def user_edit(request, pk):
    _admin_required(request.user)
    user = get_object_or_404(User, pk=pk)

    if request.method == 'POST':
        user.full_name = (request.POST.get('full_name') or '').strip()[:150]
        email = (request.POST.get('email') or '').strip().lower()

        errors = []
        if not email:
            errors.append('Email обязателен.')
        else:
            try:
                validate_email(email)
            except ValidationError:
                errors.append('Некорректный email.')
            else:
                if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                    errors.append('Пользователь с таким email уже есть.')

        new_is_active = request.POST.get('is_active') == '1'
        dept_pk = request.POST.get('department') or ''
        new_department = Department.objects.filter(pk=dept_pk).first() if dept_pk.isdigit() else None
        role_pk = request.POST.get('role') or ''
        new_role = Role.objects.filter(pk=role_pk).first() if role_pk.isdigit() else None
        new_role_id = new_role.pk if new_role else None

        is_self = user.pk == request.user.pk
        role_changes = user.role_id != new_role_id

        # 1) Себя нельзя деактивировать
        if is_self and user.is_active and not new_is_active:
            errors.append('Нельзя деактивировать самого себя.')

        # 2) Если меняется роль — проверяем, что админы останутся
        if role_changes:
            new_can_admin = bool(new_role and new_role.can_admin)
            loses_admin = _is_admin_user(user) and not (user.is_superuser or new_can_admin)
            if loses_admin and _active_admin_count(exclude_user_ids=[user.pk]) == 0:
                if is_self:
                    errors.append(
                        'Вы последний активный администратор. '
                        'Сначала назначьте нового администратора, потом меняйте свою роль.'
                    )
                else:
                    errors.append(
                        'Это последний активный администратор. Нельзя снять админскую роль.'
                    )

        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect('settings_user_edit', pk=user.pk)

        before = {
            'full_name': user.full_name,
            'email': user.email,
            'department_id': user.department_id,
            'role_id': user.role_id,
            'is_active': user.is_active,
        }
        user.email = email
        user.department = new_department
        user.role = new_role
        user.is_active = new_is_active
        user.save()

        after = {
            'full_name': user.full_name,
            'email': user.email,
            'department_id': user.department_id,
            'role_id': user.role_id,
            'is_active': user.is_active,
        }
        diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
        log_action(request, AuditLog.Action.UPDATE, user, changes=diff or None)

        _check_single_admin_alert()

        messages.success(request, f'Профиль «{user.full_name}» обновлён.')
        return redirect('settings_users')


    last_logins = list(
        LoginEvent.objects.filter(user=user).order_by('-created_at')[:10]
    )
    logins_total = LoginEvent.objects.filter(user=user, success=True).count()
    logins_failed = LoginEvent.objects.filter(user=user, success=False).count()

    task_stats = {
        'total': Task.objects.filter(executor=user).count(),
        'in_progress': Task.objects.filter(executor=user, status=Task.Status.IN_PROGRESS).count(),
        'review': Task.objects.filter(executor=user, status=Task.Status.REVIEW).count(),
        'done': Task.objects.filter(executor=user, status=Task.Status.DONE).count(),
        'overdue': Task.objects.filter(
            executor=user,
            due__lt=timezone.localdate(),
        ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED]).count(),
        'issued': Task.objects.filter(requester=user).count(),
    }

    return render(request, 'admin_panel/user_edit.html', {
        'edit_user': user,
        'departments': Department.objects.all().order_by('name'),
        'roles': Role.objects.all().order_by('name'),
        'last_logins': last_logins,
        'logins_total': logins_total,
        'logins_failed': logins_failed,
        'task_stats': task_stats,
    })


@login_required
def user_new(request):
    _admin_required(request.user)

    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        full_name = (request.POST.get('full_name') or '').strip()[:150]
        password = request.POST.get('password') or ''

        errors = []
        if not email:
            errors.append('Email обязателен.')
        else:
            try:
                validate_email(email)
            except ValidationError:
                errors.append('Некорректный email.')
            else:
                if User.objects.filter(email__iexact=email).exists():
                    errors.append('Пользователь с таким email уже есть.')
        if not full_name:
            errors.append('ФИО обязательно.')

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            u = User.objects.create_user(
                email=email, password=password or None, full_name=full_name,
            )
            dept_pk = request.POST.get('department') or ''
            if dept_pk.isdigit():
                u.department = Department.objects.filter(pk=dept_pk).first()

            role_pk = request.POST.get('role') or ''
            if role_pk.isdigit():
                u.role = Role.objects.filter(pk=role_pk).first()
            else:
                # Если роль не выбрана — назначаем «Пользователь»
                u.role = User.get_default_role()

            u.is_active = request.POST.get('is_active') == '1'
            u.save()
            log_action(request, AuditLog.Action.CREATE, u, changes={
                'email': u.email, 'full_name': u.full_name,
                'department_id': u.department_id, 'role_id': u.role_id,
                'is_active': u.is_active,
            })

            _check_single_admin_alert()

            messages.success(request, f'Пользователь «{full_name}» создан.')
            return redirect('settings_users')

    return render(request, 'admin_panel/user_edit.html', {
        'edit_user': None,
        'departments': Department.objects.all().order_by('name'),
        'roles': Role.objects.all().order_by('name'),
    })


@login_required
def user_toggle(request, pk):
    _admin_required(request.user)
    if request.method != 'POST':
        return redirect('settings_users')

    user = get_object_or_404(User, pk=pk)

    # Активация — всегда ок
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=['is_active'])
        log_action(request, AuditLog.Action.TOGGLE, user, changes={'is_active': True})
        messages.success(request, f'Пользователь «{user.full_name}» активирован.')
        return redirect('settings_users')

    # Деактивация — через защиту
    err = _guard_deactivation(user, request)
    if err:
        messages.error(request, err)
    else:
        user.is_active = False
        user.save(update_fields=['is_active'])
        log_action(request, AuditLog.Action.TOGGLE, user, changes={'is_active': False})
        messages.success(request, f'Пользователь «{user.full_name}» деактивирован.')
        _check_single_admin_alert()

    return redirect('settings_users')


@login_required
def user_detail(request, pk):
    _admin_required(request.user)

    from datetime import timedelta
    from django.db.models import Sum
    from django.utils import timezone
    from tasks.models import Task, TimeSession, ShopSession, TaskLog
    from .models import LoginEvent

    user = get_object_or_404(User.objects.select_related('department', 'role'), pk=pk)

    now = timezone.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # ── KPI по задачам ──
    task_stats = {
        'total': Task.objects.filter(executor=user).count(),
        'new': Task.objects.filter(executor=user, status=Task.Status.NEW).count(),
        'in_progress': Task.objects.filter(executor=user, status=Task.Status.IN_PROGRESS).count(),
        'review': Task.objects.filter(executor=user, status=Task.Status.REVIEW).count(),
        'rework': Task.objects.filter(executor=user, status=Task.Status.REWORK).count(),
        'done': Task.objects.filter(executor=user, status=Task.Status.DONE).count(),
        'cancelled': Task.objects.filter(executor=user, status=Task.Status.CANCELLED).count(),
        'overdue': Task.objects.filter(
            executor=user, due__lt=timezone.localdate(),
        ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED]).count(),
        'issued': Task.objects.filter(requester=user).count(),
        'done_week': Task.objects.filter(
            executor=user, status=Task.Status.DONE,
            finished_at__gte=week_ago,
        ).count(),
        'done_month': Task.objects.filter(
            executor=user, status=Task.Status.DONE,
            finished_at__gte=month_ago,
        ).count(),
    }

    # ── Часы ──
    work_hours_total = (
            TimeSession.objects.filter(executor=user)
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )
    work_hours_month = (
            TimeSession.objects.filter(executor=user, finished_at__gte=month_ago)
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )
    shop_hours_total = (
            ShopSession.objects.filter(executor=user, finished_at__isnull=False)
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )
    shop_hours_month = (
            ShopSession.objects.filter(
                executor=user, finished_at__gte=month_ago, finished_at__isnull=False,
            ).aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )

    # Активная сессия
    active_task = Task.objects.filter(
        executor=user, session_started_at__isnull=False,
    ).first()
    active_shop = ShopSession.objects.filter(
        executor=user, finished_at__isnull=True,
    ).first()

    # ── Последние сессии ──
    last_work_sessions = list(
        TimeSession.objects
        .select_related('task')
        .filter(executor=user)
        .order_by('-finished_at')[:20]
    )
    last_shop_sessions = list(
        ShopSession.objects
        .select_related('task')
        .filter(executor=user)
        .order_by('-started_at')[:20]
    )

    # ── Последние задачи ──
    last_tasks = list(
        Task.objects
        .select_related('requester', 'order')
        .filter(executor=user)
        .order_by('-created_at')[:15]
    )
    last_issued = list(
        Task.objects
        .select_related('executor')
        .filter(requester=user)
        .order_by('-created_at')[:15]
    )

    # ── Входы ──
    logins = list(
        LoginEvent.objects
        .filter(user=user)
        .order_by('-created_at')[:20]
    )
    logins_total = LoginEvent.objects.filter(user=user, success=True).count()
    logins_failed = LoginEvent.objects.filter(user=user, success=False).count()

    # ── Аудит ──
    audit = list(
        AuditLog.objects
        .filter(actor=user)
        .order_by('-created_at')[:20]
    )

    # ── Логи задач ──
    task_logs = list(
        TaskLog.objects
        .select_related('task')
        .filter(author=user)
        .order_by('-created_at')[:15]
    )

    # ── Кандидаты для переназначения — по scope смотрящего ──
    is_full_scope = (
            request.user.is_superuser
            or request.user.can_plant
            or request.user.is_admin_role
    )

    if is_full_scope:
        reassign_candidates = User.objects.filter(is_active=True)
    elif request.user.is_boss and request.user.department_id:
        reassign_candidates = User.objects.filter(
            is_active=True,
            department_id=request.user.department_id,
        )
    else:
        reassign_candidates = User.objects.none()

    reassign_candidates = reassign_candidates.exclude(
        pk=user.pk
    ).order_by('full_name')

    return render(request, 'admin_panel/user_detail.html', {
        'profile_user': user,
        'task_stats': task_stats,
        'work_hours_total': work_hours_total,
        'work_hours_month': work_hours_month,
        'shop_hours_total': shop_hours_total,
        'shop_hours_month': shop_hours_month,
        'active_task': active_task,
        'active_shop': active_shop,
        'last_work_sessions': last_work_sessions,
        'last_shop_sessions': last_shop_sessions,
        'last_tasks': last_tasks,
        'last_issued': last_issued,
        'logins': logins,
        'logins_total': logins_total,
        'logins_failed': logins_failed,
        'audit': audit,
        'task_logs': task_logs,
        'all_active_users': reassign_candidates,
    })


# ═════════════════════════════════════════════════════════════
#  РОЛИ
# ═════════════════════════════════════════════════════════════

@login_required
def roles_list(request):
    _admin_required(request.user)
    roles = Role.objects.annotate(user_count=Count('users')).order_by('name')
    return render(request, 'admin_panel/roles.html', {'roles': roles})


@login_required
def role_edit(request, pk=None):
    _admin_required(request.user)
    role = get_object_or_404(Role, pk=pk) if pk else None

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:100]
        code = (request.POST.get('code') or '').strip()[:50]

        errors = []
        if not name:
            errors.append('Название обязательно.')
        if not code:
            errors.append('Код обязателен.')

        if name:
            qs_name = Role.objects.filter(name__iexact=name)
            if role:
                qs_name = qs_name.exclude(pk=role.pk)
            if qs_name.exists():
                errors.append('Роль с таким названием уже существует.')

        if code:
            qs_code = Role.objects.filter(code=code)
            if role:
                qs_code = qs_code.exclude(pk=role.pk)
            if qs_code.exists():
                errors.append('Роль с таким кодом уже существует.')

        new_can_admin = request.POST.get('can_admin') == '1'

        # Снятие can_admin с роли: проверить, что админы останутся
        if role and role.can_admin and not new_can_admin:
            if _active_admin_count(exclude_role_ids=[role.pk]) == 0:
                errors.append(
                    'Нельзя снять «Права администратора»: это последняя роль '
                    'с админскими правами. Назначьте другую роль-админа.'
                )

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            is_new = role is None
            if is_new:
                role = Role()
            role.name = name
            role.code = code
            role.can_manage = request.POST.get('can_manage') == '1'
            role.can_admin = new_can_admin
            role.can_plant = request.POST.get('can_plant') == '1'
            role.can_view_kpi = request.POST.get('can_view_kpi') == '1'
            role.can_view_sessions = request.POST.get('can_view_sessions') == '1'
            role.can_view_online = request.POST.get('can_view_online') == '1'
            role.can_view_problems = request.POST.get('can_view_problems') == '1'
            role.can_view_task_logs = request.POST.get('can_view_task_logs') == '1'
            role.can_view_orders = request.POST.get('can_view_orders') == '1'
            role.can_view_analytics = request.POST.get('can_view_analytics') == '1'
            role.can_export = request.POST.get('can_export') == '1'
            role.is_developer = request.POST.get('is_developer') == '1'
            role.description = (request.POST.get('description') or '').strip()[:200]
            role.save()
            log_action(
                request,
                AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                role,
                changes={
                    'name': role.name, 'code': role.code,
                    'can_manage': role.can_manage, 'can_admin': role.can_admin,
                    'can_plant': role.can_plant,
                    'can_view_kpi': role.can_view_kpi,
                    'can_view_sessions': role.can_view_sessions,
                    'can_view_online': role.can_view_online,
                    'can_view_problems': role.can_view_problems,
                    'can_view_task_logs': role.can_view_task_logs,
                    'can_view_orders': role.can_view_orders,
                    'can_view_analytics': role.can_view_analytics,
                    'can_export': role.can_export,
                    'is_developer': role.is_developer,
                },
            )

            _check_single_admin_alert()

            messages.success(request, f'Роль «{name}» сохранена.')
            return redirect('settings_roles')

    # Контекст для страницы
    users_count = 0
    users_active_count = 0
    role_users = []
    would_lose_admin = []
    other_admin_roles = []

    if role:
        role_users_qs = (
            User.objects
            .select_related('department')
            .filter(role=role)
            .order_by('full_name')
        )
        users_count = role_users_qs.count()
        users_active_count = role_users_qs.filter(is_active=True).count()
        role_users = list(role_users_qs[:10])

        if role.can_admin:
            # Кто потеряет админку, если снять can_admin с этой роли
            would_lose_admin = list(
                role_users_qs.filter(is_active=True).filter(
                    is_superuser=False,
                )[:10]
            )

        other_admin_roles = list(
            Role.objects.filter(can_admin=True).exclude(pk=role.pk).order_by('name')
        )

    return render(request, 'admin_panel/role_edit.html', {
        'role': role,
        'users_count': users_count,
        'users_active_count': users_active_count,
        'role_users': role_users,
        'would_lose_admin': would_lose_admin,
        'other_admin_roles': other_admin_roles,
        'all_roles': Role.objects.exclude(pk=role.pk if role else None).order_by('name'),
    })


@login_required
def role_delete(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_roles')

    role = get_object_or_404(Role, pk=pk)
    if role.users.exists():
        messages.error(
            request,
            f'Нельзя удалить: у роли {role.users.count()} пользователей. '
            f'Сначала переназначьте их на другую роль.',
        )
        return redirect('settings_role_reassign', pk=role.pk)

    name = role.name
    log_action(request, AuditLog.Action.DELETE, role)
    role.delete()
    messages.success(request, f'Роль «{name}» удалена.')
    return redirect('settings_roles')


# ═════════════════════════════════════════════════════════════
#  ПОДРАЗДЕЛЕНИЯ
# ═════════════════════════════════════════════════════════════

@login_required
def departments_list(request):
    _admin_required(request.user)
    deps = Department.objects.annotate(user_count=Count('users')).order_by('name')
    return render(request, 'admin_panel/departments.html', {'departments': deps})


@login_required
def department_edit(request, pk=None):
    _admin_required(request.user)
    dept = get_object_or_404(Department, pk=pk) if pk else None

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:150]
        if not name:
            messages.error(request, 'Название обязательно.')
        else:
            exists = Department.objects.filter(name__iexact=name)
            if dept:
                exists = exists.exclude(pk=dept.pk)
            if exists.exists():
                messages.error(request, 'Подразделение с таким названием уже есть.')
            else:
                is_new = dept is None
                if is_new:
                    dept = Department()
                dept.name = name
                dept.save()
                log_action(
                    request,
                    AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                    dept,
                    changes={'name': dept.name},
                )
                messages.success(request, f'Подразделение «{name}» сохранено.')
                return redirect('settings_departments')

    return render(request, 'admin_panel/department_edit.html', {'dept': dept})


@login_required
def department_delete(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_departments')

    dept = get_object_or_404(Department, pk=pk)
    if dept.users.exists():
        messages.error(
            request,
            f'Нельзя удалить: {dept.users.count()} сотрудников привязано. Сначала переназначьте.',
        )
    else:
        name = dept.name
        log_action(request, AuditLog.Action.DELETE, dept)
        dept.delete()
        messages.success(request, f'Подразделение «{name}» удалено.')

    return redirect('settings_departments')


# ═════════════════════════════════════════════════════════════
#  ТИПОВЫЕ ЗАДАЧИ
# ═════════════════════════════════════════════════════════════

@login_required
def task_types_list(request):
    _admin_required(request.user)
    types = TaskType.objects.all().order_by('name')
    return render(request, 'admin_panel/task_types.html', {'types': types})


@login_required
def task_type_edit(request, pk=None):
    _admin_required(request.user)
    tt = get_object_or_404(TaskType, pk=pk) if pk else None

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:200]
        try:
            plan_hours = float((request.POST.get('plan_hours') or '0').replace(',', '.'))
        except ValueError:
            plan_hours = -1
        try:
            due_days = int(request.POST.get('due_days') or 0)
        except ValueError:
            due_days = -1

        errors = []
        if not name:
            errors.append('Название обязательно.')
        if plan_hours < 0:
            errors.append('План часов — неотрицательное число.')
        if due_days < 1:
            errors.append('Срок — целое число дней, минимум 1.')

        if name:
            qs = TaskType.objects.filter(name__iexact=name)
            if tt:
                qs = qs.exclude(pk=tt.pk)
            if qs.exists():
                errors.append('Тип с таким названием уже есть.')

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            is_new = tt is None
            if is_new:
                tt = TaskType()
            tt.name = name
            tt.plan_hours = plan_hours
            tt.due_days = due_days
            tt.is_active = request.POST.get('is_active') == '1'
            tt.save()
            log_action(
                request,
                AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                tt,
                changes={'name': tt.name, 'plan_hours': tt.plan_hours,
                         'due_days': tt.due_days, 'is_active': tt.is_active},
            )
            messages.success(request, f'Типовая задача «{name}» сохранена.')
            return redirect('settings_task_types')

    return render(request, 'admin_panel/task_type_edit.html', {'tt': tt})


@login_required
def task_type_delete(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_task_types')

    tt = get_object_or_404(TaskType, pk=pk)
    name = tt.name
    linked = tt.tasks.count()
    log_action(request, AuditLog.Action.DELETE, tt, changes={'linked_tasks': linked})
    tt.delete()
    if linked:
        messages.warning(
            request,
            f'Типовая задача «{name}» удалена. У {linked} задач ссылка на тип сброшена.',
        )
    else:
        messages.success(request, f'Типовая задача «{name}» удалена.')
    return redirect('settings_task_types')


# ═════════════════════════════════════════════════════════════
#  НОРМЫ ЧАСОВ
# ═════════════════════════════════════════════════════════════

@login_required
def norms_list(request):
    _admin_required(request.user)
    norms = Norm.objects.all().order_by('-id')
    return render(request, 'admin_panel/norms.html', {'norms': norms})


@login_required
def norm_edit(request, pk=None):
    _admin_required(request.user)
    norm = get_object_or_404(Norm, pk=pk) if pk else None

    if request.method == 'POST':
        try:
            hours = float((request.POST.get('hours_per_day') or '0').replace(',', '.'))
        except ValueError:
            hours = -1

        if hours < 0:
            messages.error(request, 'Норма часов — неотрицательное число.')
        else:
            is_new = norm is None
            if is_new:
                norm = Norm()
            norm.hours_per_day = hours
            norm.note = (request.POST.get('note') or '').strip()[:200]
            norm.save()
            log_action(
                request,
                AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                norm,
                changes={'hours_per_day': norm.hours_per_day, 'note': norm.note},
            )
            messages.success(request, 'Норма часов сохранена.')
            return redirect('settings_norms')

    return render(request, 'admin_panel/norm_edit.html', {'norm': norm})


@login_required
def norm_delete(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_norms')

    norm = get_object_or_404(Norm, pk=pk)
    log_action(request, AuditLog.Action.DELETE, norm)
    norm.delete()
    messages.success(request, 'Норма часов удалена.')
    return redirect('settings_norms')


# ═════════════════════════════════════════════════════════════
#  ЖУРНАЛ АУДИТА
# ═════════════════════════════════════════════════════════════

@login_required
def audit_list(request):
    _admin_required(request.user)

    q = request.GET.get('q', '').strip()
    action = request.GET.get('action', '').strip()
    model = request.GET.get('model', '').strip()

    qs = AuditLog.objects.select_related('actor').order_by('-created_at')

    if q:
        qs = qs.filter(_ci_filter(q, 'target_repr', 'actor__full_name', 'actor__email'))
    if action:
        qs = qs.filter(action=action)
    if model:
        qs = qs.filter(target_model=model)

    models_qs = (
        AuditLog.objects.values_list('target_model', flat=True)
        .distinct().order_by('target_model')
    )

    total = qs.count()
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'admin_panel/audit.html', {
        'page': page,
        'q': q,
        'action': action,
        'model': model,
        'action_choices': AuditLog.Action.choices,
        'model_choices': models_qs,
        'total': total,
    })

@login_required
def logins_list(request):
    _admin_required(request.user)

    from .models import LoginEvent

    q = request.GET.get('q', '').strip()
    result = request.GET.get('result', '').strip()
    only_failed = result == 'failed'
    only_ok = result == 'ok'

    qs = LoginEvent.objects.select_related('user').order_by('-created_at')

    if q:
        qs = qs.filter(_ci_filter(
            q, 'user__full_name', 'user__email', 'email_attempted', 'ip',
        ))
    if only_failed:
        qs = qs.filter(success=False)
    elif only_ok:
        qs = qs.filter(success=True)

    total = qs.count()
    failed_count = LoginEvent.objects.filter(success=False).count()
    ok_count = LoginEvent.objects.filter(success=True).count()

    paginator = Paginator(qs, 100)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'admin_panel/logins.html', {
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'q': q,
        'result': result,
        'failed_count': failed_count,
        'ok_count': ok_count,
    })


@login_required
def alerts_list(request):
    _admin_required(request.user)

    from .models import AdminAlert

    show = request.GET.get('show', '').strip()
    qs = AdminAlert.objects.all()
    if show == 'unseen':
        qs = qs.filter(seen=False)

    total = qs.count()
    unseen_count = AdminAlert.objects.filter(seen=False).count()

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'admin_panel/alerts.html', {
        'page': page,
        'total': total,
        'unseen_count': unseen_count,
        'show': show,
    })


@login_required
def alerts_mark_seen(request, pk):
    _admin_required(request.user)
    if request.method != 'POST':
        return redirect('settings_alerts')

    from .models import AdminAlert
    AdminAlert.objects.filter(pk=pk).update(seen=True)
    return redirect('settings_alerts')


@login_required
def alerts_mark_all_seen(request):
    _admin_required(request.user)
    if request.method != 'POST':
        return redirect('settings_alerts')

    from .models import AdminAlert
    AdminAlert.objects.filter(seen=False).update(seen=True)
    messages.success(request, 'Все уведомления отмечены прочитанными.')
    return redirect('settings_alerts')


@login_required
def online_list(request):
    """Старый /settings/online/ → /manager/online/.

    После патча 6.2 логика живёт в manager.views.online_page.
    Старый URL сохранён как редирект.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    return redirect(f"{reverse('manager_online')}?{qs.urlencode()}")


@login_required
def online_recompute(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_online')

    from io import StringIO
    from django.core.management import call_command

    buf = StringIO()
    try:
        call_command('compute_idle_since', stdout=buf)
        output = buf.getvalue().strip() or 'Готово.'
        messages.success(request, f'Пересчёт выполнен. {output}')
    except Exception as e:
        messages.error(request, f'Ошибка пересчёта: {e}')

    return redirect('settings_online')


@login_required
def settings_dashboard(request):
    # Boss без admin — в свой кабинет (там всё операционное).
    if not (request.user.is_superuser or request.user.is_admin_role):
        if request.user.is_boss:
            return redirect('manager_cabinet')
        raise PermissionDenied('Раздел доступен только администраторам.')

    from datetime import timedelta
    from django.utils import timezone
    from tasks.models import Task
    from .models import LoginEvent

    today = timezone.localdate()
    now = timezone.now()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    # ── Общие счётчики ──
    users_total = User.objects.count()
    users_active = User.objects.filter(is_active=True).count()
    users_inactive = users_total - users_active
    users_no_role = User.objects.filter(is_active=True, role__isnull=True).count()
    users_no_dept = User.objects.filter(is_active=True, department__isnull=True).count()

    users_admins = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__can_admin=True)
    ).count()

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    users_dismissed_month = User.objects.filter(
        employment_status=User.EmploymentStatus.DISMISSED,
        dismissed_at__gte=month_start.date(),
    ).count()

    roles_total = Role.objects.count()
    departments_total = Department.objects.count()
    task_types_total = TaskType.objects.count()
    task_types_active = TaskType.objects.filter(is_active=True).count()
    norms_total = Norm.objects.count()
    current_norm = Norm.objects.order_by('-id').first()

    tasks_in_progress = Task.objects.filter(status=Task.Status.IN_PROGRESS).count()
    tasks_overdue = Task.objects.filter(
        due__lt=today,
    ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED]).count()

    # ── Проблемные зоны ──
    overdue_tasks = list(
        Task.objects
        .select_related('executor', 'requester')
        .filter(due__lt=today)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('due')[:10]
    )

    # Счётчики для карточек
    open_tickets = SupportTicket.objects.filter(done=False).count()

    from datetime import timedelta as _td
    _now = timezone.now()
    problem_count = (
        Task.objects.exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .filter(
            Q(due__isnull=True)
            | Q(plan_hours__lte=0)
            | Q(status=Task.Status.IN_PROGRESS, session_started_at__isnull=True)
            | Q(session_started_at__lt=_now - _td(hours=12))
            | Q(status=Task.Status.REVIEW, finished_at__lt=_now - _td(days=7))
            | Q(status=Task.Status.REWORK, finished_at__lt=_now - _td(days=7))
            | Q(due__lt=today - _td(days=30))
            | Q(order__isnull=False, branch__isnull=True)
            | Q(plan_hours__gt=100)
        )
        .distinct()
        .count()
    )

    failed_logins_24h = LoginEvent.objects.filter(
        success=False, created_at__gte=last_24h,
    ).count()

    failed_logins_top = list(
        LoginEvent.objects
        .filter(success=False, created_at__gte=last_7d)
        .values('email_attempted', 'ip')
        .order_by()
    )
    # Группируем в Python — SQLite не любит group by + order
    fail_counter = {}
    for row in failed_logins_top:
        key = (row['email_attempted'] or '—', row['ip'] or '—')
        fail_counter[key] = fail_counter.get(key, 0) + 1
    failed_logins_top = sorted(
        [{'email': k[0], 'ip': k[1], 'count': v} for k, v in fail_counter.items()],
        key=lambda x: -x['count'],
    )[:5]

    recent_audit = list(
        AuditLog.objects.select_related('actor').order_by('-created_at')[:10]
    )

    # ── Сегодня в цифрах ──
    from tasks.models import TimeSession, ShopSession

    day_start = timezone.datetime.combine(
        today, timezone.datetime.min.time(),
    )
    day_start = timezone.make_aware(day_start, timezone.get_current_timezone())
    day_end = day_start + timedelta(days=1)

    today_stats = {
        'logins_ok': LoginEvent.objects.filter(
            success=True, created_at__gte=day_start, created_at__lt=day_end,
        ).count(),
        'logins_fail': LoginEvent.objects.filter(
            success=False, created_at__gte=day_start, created_at__lt=day_end,
        ).count(),
        'tasks_created': Task.objects.filter(
            created_at__gte=day_start, created_at__lt=day_end,
        ).count(),
        'tasks_done': Task.objects.filter(
            finished_at__gte=day_start, finished_at__lt=day_end,
            status=Task.Status.DONE,
        ).count(),
        'tasks_cancelled': Task.objects.filter(
            finished_at__gte=day_start, finished_at__lt=day_end,
            status=Task.Status.CANCELLED,
        ).count(),
        'sessions_count': TimeSession.objects.filter(
            finished_at__gte=day_start, finished_at__lt=day_end,
        ).count(),
        'sessions_hours': (
                TimeSession.objects.filter(
                    finished_at__gte=day_start, finished_at__lt=day_end,
                ).aggregate(s=models.Sum('duration_hours'))['s'] or 0.0
        ),
        'shop_sessions_count': ShopSession.objects.filter(
            started_at__gte=day_start, started_at__lt=day_end,
        ).count(),
        'shop_sessions_hours': (
                ShopSession.objects.filter(
                    started_at__gte=day_start, started_at__lt=day_end,
                    finished_at__isnull=False,
                ).aggregate(s=models.Sum('duration_hours'))['s'] or 0.0
        ),
        'active_sessions_now': Task.objects.filter(
            session_started_at__isnull=False,
        ).count(),
    }

    from .onboarding import collect_onboarding
    onboarding_checks, onboarding_progress = collect_onboarding()

    return render(request, 'admin_panel/dashboard.html', {
        # общие
        'users_total': users_total,
        'users_active': users_active,
        'users_inactive': users_inactive,
        'users_admins': users_admins,
        'users_no_role': users_no_role,
        'users_no_dept': users_no_dept,
        'roles_total': roles_total,
        'departments_total': departments_total,
        'task_types_total': task_types_total,
        'task_types_active': task_types_active,
        'norms_total': norms_total,
        'current_norm': current_norm,
        'tasks_in_progress': tasks_in_progress,
        'tasks_overdue': tasks_overdue,
        'recent_audit': recent_audit,
        # проблемные
        'overdue_tasks': overdue_tasks,
        'failed_logins_24h': failed_logins_24h,
        'failed_logins_top': failed_logins_top,
        'today': today,
        'today_stats': today_stats,
        'onboarding_checks': onboarding_checks,
        'onboarding_progress': onboarding_progress,
        'open_tickets': open_tickets,
        'problem_count': problem_count,
        'users_dismissed_month': users_dismissed_month,
    })


# ═════════════════════════════════════════════════════════════
#  ИМПОРТ ПОЛЬЗОВАТЕЛЕЙ ИЗ CSV
# ═════════════════════════════════════════════════════════════

_IMPORT_COLUMN_ALIASES = {
    'full_name':    {'фио', 'full_name', 'ф.и.о.', 'имя', 'сотрудник'},
    'email':        {'email', 'почта', 'e-mail', 'мейл'},
    'department':   {'подразделение', 'department', 'отдел'},
    'role_code':    {'роль', 'role', 'role_code', 'код роли', 'код'},
    'is_active':    {'активен', 'is_active', 'активный', 'активна'},
}


def _import_normalize_header(name):
    return (name or '').strip().lower().replace('ё', 'е')


def _import_detect_delimiter(sample):
    return ';' if sample.count(';') >= sample.count(',') else ','


def _import_decode(raw):
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError('Не удалось определить кодировку файла (utf-8 / cp1251).')


def _import_parse_bool(value):
    v = (value or '').strip().lower()
    if v in {'', '—', '-'}:
        return None
    if v in {'да', '1', 'true', 'yes', 'y', 'активен', 'активный'}:
        return True
    if v in {'нет', '0', 'false', 'no', 'n', 'отключён', 'отключен', 'выкл'}:
        return False
    return None


def _import_map_headers(fieldnames):
    """Возвращает {canonical: original_header}."""
    mapping = {}
    for fn in fieldnames or []:
        norm = _import_normalize_header(fn)
        for canon, aliases in _IMPORT_COLUMN_ALIASES.items():
            if norm in aliases and canon not in mapping:
                mapping[canon] = fn
                break
    return mapping


@login_required
def users_import(request):
    _boss_required(request.user)

    if request.method == 'POST':
        upload = request.FILES.get('csv_file')
        mode = request.POST.get('mode') or 'create_only'
        if mode not in {'create_only', 'update'}:
            mode = 'create_only'

        if not upload:
            messages.error(request, 'Файл не выбран.')
            return redirect('settings_users_import')

        try:
            text = _import_decode(upload.read())
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('settings_users_import')

        sample = text[:4096]
        delimiter = _import_detect_delimiter(sample)
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headers = _import_map_headers(reader.fieldnames)

        if 'email' not in headers or 'full_name' not in headers:
            messages.error(
                request,
                'В файле должны быть колонки «ФИО» и «Email». '
                f'Найдены: {reader.fieldnames}',
            )
            return redirect('settings_users_import')

        # Кэш справочников — чтобы не дёргать БД на каждой строке
        dept_by_name = {d.name.strip().lower(): d for d in Department.objects.all()}
        role_by_code = {r.code.lower(): r for r in Role.objects.all()}

        seen_emails = set()
        rows = []
        report = {'create': 0, 'update': 0, 'skip': 0, 'error': 0}

        for i, raw_row in enumerate(reader, start=2):  # 2 = первая строка данных после заголовка
            full_name = (raw_row.get(headers['full_name']) or '').strip()[:150]
            email = (raw_row.get(headers['email']) or '').strip().lower()

            row = {
                'row_num': i,
                'full_name': full_name,
                'email': email,
                'department_id': None,
                'department_name': '',
                'role_id': None,
                'role_code': '',
                'is_active': None,
                'status': 'error',
                'message': '',
            }

            # Валидация
            if not full_name:
                row['message'] = 'Пустое ФИО.'
                report['error'] += 1
                rows.append(row)
                continue

            if not email:
                row['message'] = 'Пустой email.'
                report['error'] += 1
                rows.append(row)
                continue

            try:
                validate_email(email)
            except ValidationError:
                row['message'] = 'Некорректный email.'
                report['error'] += 1
                rows.append(row)
                continue

            if email in seen_emails:
                row['message'] = 'Дубликат email внутри файла.'
                report['error'] += 1
                rows.append(row)
                continue
            seen_emails.add(email)

            # Подразделение
            if 'department' in headers:
                d_name = (raw_row.get(headers['department']) or '').strip()
                if d_name:
                    dept = dept_by_name.get(d_name.lower())
                    if dept:
                        row['department_id'] = dept.pk
                        row['department_name'] = dept.name
                    else:
                        row['department_name'] = d_name + ' (не найдено)'

            # Роль
            if 'role_code' in headers:
                r_code = (raw_row.get(headers['role_code']) or '').strip().lower()
                if r_code:
                    role = role_by_code.get(r_code)
                    if role:
                        row['role_id'] = role.pk
                        row['role_code'] = role.code
                    else:
                        row['role_code'] = r_code + ' (не найдено)'

            # Активность
            if 'is_active' in headers:
                row['is_active'] = _import_parse_bool(raw_row.get(headers['is_active']))

            # Статус
            exists = User.objects.filter(email__iexact=email).exists()
            if exists:
                if mode == 'update':
                    row['status'] = 'update'
                    report['update'] += 1
                else:
                    row['status'] = 'skip'
                    row['message'] = 'Уже есть в системе — пропущен.'
                    report['skip'] += 1
            else:
                row['status'] = 'create'
                report['create'] += 1

            rows.append(row)

        request.session['users_import'] = {
            'rows': rows,
            'mode': mode,
            'report': report,
        }
        return render(request, 'admin_panel/users_import.html', {
            'stage': 'preview',
            'rows': rows,
            'report': report,
            'mode': mode,
            'total': len(rows),
        })

    # GET — форма загрузки
    request.session.pop('users_import', None)
    return render(request, 'admin_panel/users_import.html', {
        'stage': 'upload',
    })


@login_required
def users_import_apply(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_users_import')

    payload = request.session.get('users_import')
    if not payload:
        messages.error(request, 'Нет данных для импорта. Загрузите файл заново.')
        return redirect('settings_users_import')

    rows = payload.get('rows') or []

    created = 0
    updated = 0
    skipped = 0
    failed = []

    with transaction.atomic():
        for row in rows:
            status = row.get('status')
            if status == 'error' or status == 'skip':
                skipped += 1
                continue

            email = row['email']
            full_name = row['full_name']
            dept = Department.objects.filter(pk=row['department_id']).first() if row.get('department_id') else None
            role = Role.objects.filter(pk=row['role_id']).first() if row.get('role_id') else None
            is_active = row.get('is_active')

            try:
                if status == 'create':
                    u = User.objects.create_user(
                        email=email, password=None, full_name=full_name,
                    )
                    if dept:
                        u.department = dept
                    # Если роль не указана в CSV — «Пользователь»
                    u.role = role if role else User.get_default_role()
                    if is_active is not None:
                        u.is_active = is_active
                    u.save()
                    log_action(request, AuditLog.Action.CREATE, u, changes={
                        'email': u.email, 'full_name': u.full_name,
                        'department_id': u.department_id, 'role_id': u.role_id,
                        'is_active': u.is_active, 'source': 'csv_import',
                    })
                    created += 1

                elif status == 'update':
                    u = User.objects.filter(email__iexact=email).first()
                    if not u:
                        failed.append(f'стр. {row["row_num"]}: пользователь исчез')
                        continue
                    before = {
                        'full_name': u.full_name,
                        'department_id': u.department_id,
                        'role_id': u.role_id,
                        'is_active': u.is_active,
                    }
                    u.full_name = full_name
                    u.department = dept
                    u.role = role
                    if is_active is not None:
                        u.is_active = is_active
                    u.save()
                    after = {
                        'full_name': u.full_name,
                        'department_id': u.department_id,
                        'role_id': u.role_id,
                        'is_active': u.is_active,
                    }
                    diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
                    log_action(request, AuditLog.Action.UPDATE, u,
                               changes={**(diff or {}), 'source': 'csv_import'})
                    updated += 1

            except Exception as e:
                failed.append(f'стр. {row["row_num"]} ({email}): {e}')

    request.session.pop('users_import', None)

    msg = f'Импорт завершён. Создано: {created}, обновлено: {updated}, пропущено: {skipped}.'
    if failed:
        msg += ' Ошибок: ' + str(len(failed)) + '.'
        for line in failed[:10]:
            messages.warning(request, line)
    messages.success(request, msg)
    return redirect('settings_users')


@login_required
def global_search(request):
    _admin_required(request.user)

    from tasks.models import Task

    q = (request.GET.get('q') or '').strip()
    results = {
        'users': [],
        'roles': [],
        'departments': [],
        'task_types': [],
        'tasks': [],
    }
    counts = {k: 0 for k in results}

    if q:
        users_qs = (
            User.objects
            .select_related('department', 'role')
            .filter(_ci_filter(q, 'full_name', 'email'))
            .order_by('full_name')
        )
        counts['users'] = users_qs.count()
        results['users'] = list(users_qs[:20])

        roles_qs = Role.objects.filter(
            _ci_filter(q, 'name', 'code', 'description')
        ).order_by('name')
        counts['roles'] = roles_qs.count()
        results['roles'] = list(roles_qs[:20])

        deps_qs = Department.objects.filter(_ci_filter(q, 'name')).order_by('name')
        counts['departments'] = deps_qs.count()
        results['departments'] = list(deps_qs[:20])

        tts_qs = TaskType.objects.filter(_ci_filter(q, 'name')).order_by('name')
        counts['task_types'] = tts_qs.count()
        results['task_types'] = list(tts_qs[:20])

        tasks_qs = (
            Task.objects
            .select_related('executor', 'requester')
            .filter(_ci_filter(q, 'title', 'body'))
            .order_by('-created_at')
        )
        counts['tasks'] = tasks_qs.count()
        results['tasks'] = list(tasks_qs[:20])

    return render(request, 'admin_panel/search.html', {
        'q': q,
        'results': results,
        'counts': counts,
    })


@login_required
def role_reassign(request, pk):
    _admin_required(request.user)

    role = get_object_or_404(Role, pk=pk)
    users_qs = User.objects.filter(role=role).order_by('full_name')
    users_count = users_qs.count()
    users_active_count = users_qs.filter(is_active=True).count()
    users_preview = list(users_qs[:20])

    other_roles = Role.objects.exclude(pk=role.pk).order_by('name')

    if request.method == 'POST':
        target_pk = (request.POST.get('target_role') or '').strip()

        if target_pk == '':
            messages.error(request, 'Выберите целевую роль.')
            return redirect('settings_role_reassign', pk=role.pk)

        if target_pk == 'none':
            target_role = None
        elif target_pk.isdigit():
            target_role = Role.objects.filter(pk=int(target_pk)).first()
            if not target_role:
                messages.error(request, 'Целевая роль не найдена.')
                return redirect('settings_role_reassign', pk=role.pk)
        else:
            messages.error(request, 'Некорректный выбор.')
            return redirect('settings_role_reassign', pk=role.pk)

        # Защита: если переназначаем последнюю админскую роль — нельзя
        # на роль, которая не даёт админку
        if role.can_admin:
            new_can_admin = bool(target_role and target_role.can_admin)
            if not new_can_admin:
                # сколько админов останется после смены
                if _active_admin_count(exclude_role_ids=[role.pk]) == 0:
                    messages.error(
                        request,
                        'Нельзя переназначить всех с этой роли: она последняя '
                        'с админскими правами, а целевая роль их не даёт.',
                    )
                    return redirect('settings_role_reassign', pk=role.pk)

        with transaction.atomic():
            for u in users_qs:
                before_role_id = u.role_id
                u.role = target_role
                u.save(update_fields=['role'])
                log_action(
                    request, AuditLog.Action.UPDATE, u,
                    changes={'role_id': [before_role_id, target_role.pk if target_role else None],
                             'source': 'role_reassign'},
                )

                _check_single_admin_alert()

        target_name = target_role.name if target_role else 'без роли'
        messages.success(
            request,
            f'Переназначено {users_count} пользователей: '
            f'«{role.name}» → «{target_name}».',
        )
        return redirect('settings_roles')

    return render(request, 'admin_panel/role_reassign.html', {
        'role': role,
        'users_count': users_count,
        'users_active_count': users_active_count,
        'users_preview': users_preview,
        'other_roles': other_roles,
    })


@login_required
def system_info(request):
    _admin_required(request.user)

    import platform
    from pathlib import Path
    from django.conf import settings as dj_settings
    from django.db import connection

    # ── Версии ──
    versions = {
        'python': platform.python_version(),
        'python_impl': platform.python_implementation(),
        'django': __import__('django').get_version(),
        'platform': f'{platform.system()} {platform.release()}',
    }

    # ── БД ──
    db_info = {'engine': connection.vendor, 'name': '', 'size': '—', 'tables': 0}
    try:
        db_path = Path(connection.settings_dict.get('NAME') or '')
        db_info['name'] = db_path.name or str(db_path)
        if db_path.exists():
            size = db_path.stat().st_size
            for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
                if size < 1024:
                    db_info['size'] = f'{size:.1f} {unit}'
                    break
                size /= 1024
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            db_info['tables'] = cur.fetchone()[0]
    except Exception as e:
        db_info['error'] = str(e)

    # ── AD ──
    ad_info = {
        'enabled': getattr(dj_settings, 'AD_ENABLED', False),
        'server': getattr(dj_settings, 'AD_SERVER', '') or '—',
        'domain': getattr(dj_settings, 'AD_DOMAIN', '') or '—',
        'user_base': getattr(dj_settings, 'AD_USER_BASE', '') or '—',
        'mail_domain': getattr(dj_settings, 'AD_MAIL_DOMAIN', '') or '—',
        'group_map': getattr(dj_settings, 'AD_GROUP_MAP', {}) or {},
    }

    ad_check = None
    if request.GET.get('check_ad') == '1' and ad_info['enabled']:
        try:
            from ldap3 import Server
            server = Server(ad_info['server'], get_info='NO_INFO', connect_timeout=3)
            server.check_availability() if hasattr(server, 'check_availability') else None
            ad_check = {'ok': True, 'message': 'Сервер отвечает.'}
        except Exception as e:
            ad_check = {'ok': False, 'message': f'{type(e).__name__}: {e}'}

    # ── Настройки ──
    env_info = {
        'DEBUG': bool(dj_settings.DEBUG),
        'TIME_ZONE': dj_settings.TIME_ZONE,
        'LANGUAGE_CODE': dj_settings.LANGUAGE_CODE,
        'USE_TZ': bool(dj_settings.USE_TZ),
        'ALLOWED_HOSTS': list(dj_settings.ALLOWED_HOSTS),
        'AUTH_USER_MODEL': dj_settings.AUTH_USER_MODEL,
        'ADMIN_URL': '/admin/',
        'AUTH_BACKENDS': list(dj_settings.AUTHENTICATION_BACKENDS),
    }

    # ── Счётчики ──
    from tasks.models import Task
    counters = {
        'users': User.objects.count(),
        'users_active': User.objects.filter(is_active=True).count(),
        'roles': Role.objects.count(),
        'departments': Department.objects.count(),
        'task_types': TaskType.objects.count(),
        'norms': Norm.objects.count(),
        'tasks': Task.objects.count(),
        'tasks_active': Task.objects.exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED]
        ).count(),
    }

    from .models import AuditLog, LoginEvent, AdminAlert
    counters['audit_records'] = AuditLog.objects.count()
    counters['login_events'] = LoginEvent.objects.count()
    counters['alerts_unseen'] = AdminAlert.objects.filter(seen=False).count()

    return render(request, 'admin_panel/system_info.html', {
        'versions': versions,
        'db_info': db_info,
        'ad_info': ad_info,
        'ad_check': ad_check,
        'env_info': env_info,
        'counters': counters,
    })

# ═════════════════════════════════════════════════════════════
#  ОБСЛУЖИВАНИЕ
# ═════════════════════════════════════════════════════════════

@login_required
def system_cleanup(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_system')

    from io import StringIO
    from django.core.management import call_command

    buf = StringIO()
    try:
        call_command('cleanup_old_records', stdout=buf)
        output = buf.getvalue().strip() or 'Готово.'
        messages.success(request, f'Очистка выполнена. {output}')
    except Exception as e:
        messages.error(request, f'Ошибка очистки: {e}')

    return redirect('settings_system')


# ═════════════════════════════════════════════════════════════
#  ЭКСПОРТ ВСЕЙ АДМИНКИ (ZIP)
# ═════════════════════════════════════════════════════════════

def _csv_bytes(header, rows):
    """Собирает CSV-строку с BOM и ; — для Excel."""
    buf = io.StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf, delimiter=';')
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode('utf-8')


@login_required
def export_snapshot_zip(request):
    _admin_required(request.user)

    import json
    import zipfile
    from django.conf import settings as dj_settings

    from tasks.models import Task
    from .models import LoginEvent, AdminAlert

    now = datetime.now()
    stamp = now.strftime('%Y%m%d_%H%M')

    zbuf = io.BytesIO()

    with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as z:
        # ── Справочники ──
        users_rows = [
            [u.pk, u.full_name, u.email,
             u.department.name if u.department else '',
             u.role.name if u.role else '',
             u.role.code if u.role else '',
             'да' if u.is_active else 'нет',
             'да' if u.is_superuser else 'нет',
             u.date_joined.strftime('%d.%m.%Y %H:%M') if u.date_joined else '',
             u.last_activity_at.strftime('%d.%m.%Y %H:%M') if u.last_activity_at else '']
            for u in User.objects.select_related('department', 'role').order_by('full_name')
        ]
        z.writestr('users.csv', _csv_bytes(
            ['ID', 'ФИО', 'Email', 'Подразделение', 'Роль', 'Код роли',
             'Активен', 'Суперадмин', 'Регистрация', 'Последняя активность'],
            users_rows,
        ))

        roles_rows = [
            [r.pk, r.name, r.code,
             'да' if r.can_manage else 'нет',
             'да' if r.can_admin else 'нет',
             'да' if r.can_plant else 'нет',
             r.description or '']
            for r in Role.objects.order_by('name')
        ]
        z.writestr('roles.csv', _csv_bytes(
            ['ID', 'Название', 'Код', 'Управление', 'Админ', 'Свод', 'Описание'],
            roles_rows,
        ))

        dept_rows = [
            [d.pk, d.name,
             d.created_at.strftime('%d.%m.%Y') if d.created_at else '',
             d.users.count()]
            for d in Department.objects.order_by('name')
        ]
        z.writestr('departments.csv', _csv_bytes(
            ['ID', 'Название', 'Создано', 'Людей'],
            dept_rows,
        ))

        tt_rows = [
            [t.pk, t.name, f'{t.plan_hours:.2f}'.replace('.', ','),
             t.due_days, 'да' if t.is_active else 'нет']
            for t in TaskType.objects.order_by('name')
        ]
        z.writestr('task_types.csv', _csv_bytes(
            ['ID', 'Название', 'План, ч', 'Срок, дней', 'Активна'],
            tt_rows,
        ))

        norm_rows = [
            [n.pk, f'{n.hours_per_day:.2f}'.replace('.', ','), n.note or '']
            for n in Norm.objects.order_by('-id')
        ]
        z.writestr('norms.csv', _csv_bytes(
            ['ID', 'Часов/день', 'Примечание'],
            norm_rows,
        ))

        # ── Аудит ──
        audit_rows = [
            [a.created_at.strftime('%d.%m.%Y %H:%M:%S'),
             a.actor.full_name if a.actor else '',
             a.actor.email if a.actor else '',
             a.get_action_display(),
             a.target_model,
             a.target_id,
             a.target_repr,
             a.ip or '']
            for a in AuditLog.objects.select_related('actor').order_by('-created_at')[:5000]
        ]
        z.writestr('audit.csv', _csv_bytes(
            ['Когда', 'Кто', 'Email', 'Действие', 'Модель', 'ID объекта',
             'Объект', 'IP'],
            audit_rows,
        ))

        # ── Логи входов (последние 5000) ──
        logins_rows = [
            [e.created_at.strftime('%d.%m.%Y %H:%M:%S'),
             e.user.full_name if e.user else '',
             e.email_attempted or '',
             'да' if e.success else 'нет',
             e.ip or '',
             (e.user_agent or '')[:120]]
            for e in LoginEvent.objects.select_related('user').order_by('-created_at')[:5000]
        ]
        z.writestr('logins.csv', _csv_bytes(
            ['Когда', 'Пользователь', 'Email/попытка', 'Успех', 'IP', 'User-Agent'],
            logins_rows,
        ))

        # ── Задачи (шапка, до 10000) ──
        tasks_rows = [
            [t.pk, t.title,
             t.get_status_display(), t.get_priority_display(),
             t.executor.full_name if t.executor else '',
             t.requester.full_name if t.requester else '',
             t.due.strftime('%d.%m.%Y') if t.due else '',
             f'{t.plan_hours:.2f}'.replace('.', ','),
             f'{t.accumulated_hours:.2f}'.replace('.', ','),
             t.created_at.strftime('%d.%m.%Y %H:%M') if t.created_at else '',
             t.finished_at.strftime('%d.%m.%Y %H:%M') if t.finished_at else '']
            for t in Task.objects.select_related('executor', 'requester').order_by('-created_at')[:10000]
        ]
        z.writestr('tasks.csv', _csv_bytes(
            ['ID', 'Задача', 'Статус', 'Приоритет', 'Исполнитель', 'Постановщик',
             'Срок', 'План, ч', 'Накоплено, ч', 'Создана', 'Завершена'],
            tasks_rows,
        ))

        # ── Алерты ──
        alert_rows = [
            [a.created_at.strftime('%d.%m.%Y %H:%M:%S'),
             a.get_kind_display(), a.get_severity_display(),
             a.title, a.message or '',
             a.count, 'да' if a.seen else 'нет',
             a.last_seen_at.strftime('%d.%m.%Y %H:%M') if a.last_seen_at else '']
            for a in AdminAlert.objects.order_by('-last_seen_at')
        ]
        z.writestr('alerts.csv', _csv_bytes(
            ['Создано', 'Тип', 'Серьёзность', 'Заголовок', 'Сообщение',
             'Срабатываний', 'Прочитано', 'Последнее'],
            alert_rows,
        ))

        # ── Метаданные ──
        meta = {
            'exported_at': now.strftime('%d.%m.%Y %H:%M:%S'),
            'exported_by': request.user.email,
            'django_version': __import__('django').get_version(),
            'python_version': __import__('platform').python_version(),
            'debug': bool(dj_settings.DEBUG),
            'timezone': dj_settings.TIME_ZONE,
            'counts': {
                'users': User.objects.count(),
                'roles': Role.objects.count(),
                'departments': Department.objects.count(),
                'task_types': TaskType.objects.count(),
                'norms': Norm.objects.count(),
                'tasks': Task.objects.count(),
                'audit_total': AuditLog.objects.count(),
                'logins_total': LoginEvent.objects.count(),
                'alerts_total': AdminAlert.objects.count(),
            },
        }
        z.writestr('meta.json', json.dumps(meta, ensure_ascii=False, indent=2))

        # ── README ──
        z.writestr('README.txt',
                   f'Экспорт админки СВОД\n'
                   f'Дата: {meta["exported_at"]}\n'
                   f'Кто: {meta["exported_by"]}\n\n'
                   f'Файлы:\n'
                   f'  users.csv        — пользователи\n'
                   f'  roles.csv        — роли\n'
                   f'  departments.csv  — подразделения\n'
                   f'  task_types.csv   — типовые задачи\n'
                   f'  norms.csv        — нормы часов\n'
                   f'  tasks.csv        — задачи (до 10000 последних)\n'
                   f'  audit.csv        — журнал аудита (до 5000 последних)\n'
                   f'  logins.csv       — события входов (до 5000 последних)\n'
                   f'  alerts.csv       — уведомления админа\n'
                   f'  meta.json        — метаданные снимка\n\n'
                   f'Кодировка CSV: UTF-8 с BOM, разделитель ";" — для Excel.\n')

    zbuf.seek(0)

    log_action(request, AuditLog.Action.UPDATE, request.user,
               changes={'export': 'snapshot_zip', 'counts': meta['counts']})

    response = HttpResponse(zbuf.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="svod_snapshot_{stamp}.zip"'
    return response


# ═════════════════════════════════════════════════════════════
#  РЕЗЕРВНЫЕ КОПИИ
# ═════════════════════════════════════════════════════════════

@login_required
def backups_list(request):
    _admin_required(request.user)

    from . import backups as bk

    items = []
    for row in bk.list_backups():
        items.append({
            'name': row['name'],
            'size_h': bk.human_size(row['size']),
            'size': row['size'],
            'mtime': datetime.fromtimestamp(row['mtime']),
        })

    total_size = sum(i['size'] for i in items)

    return render(request, 'admin_panel/backups.html', {
        'items': items,
        'total_size': bk.human_size(total_size) if items else '0 Б',
        'backup_dir': str(bk.BACKUP_DIR),
    })


@login_required
def backup_create(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_backups')

    from . import backups as bk

    try:
        name, size = bk.make_backup()
        log_action(request, AuditLog.Action.UPDATE, request.user,
                   changes={'backup': name, 'size': size})
        messages.success(request, f'Бэкап создан: {name} ({bk.human_size(size)}).')
    except Exception as e:
        messages.error(request, f'Ошибка создания бэкапа: {e}')

    return redirect('settings_backups')


@login_required
def backup_download(request, name):
    _admin_required(request.user)

    from django.http import FileResponse
    from . import backups as bk

    p = bk.backup_path(name)
    if not p:
        messages.error(request, 'Файл не найден.')
        return redirect('settings_backups')

    response = FileResponse(open(p, 'rb'), content_type='application/x-sqlite3')
    response['Content-Disposition'] = f'attachment; filename="{p.name}"'
    return response


@login_required
def backup_delete(request, name):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_backups')

    from . import backups as bk

    if bk.delete_backup(name):
        log_action(request, AuditLog.Action.DELETE, request.user,
                   changes={'backup_deleted': name})
        messages.success(request, f'Бэкап удалён: {name}.')
    else:
        messages.error(request, 'Файл не найден или некорректное имя.')

    return redirect('settings_backups')


@login_required
def health_view(request):
    _admin_required(request.user)

    from .health import collect_health

    data = collect_health()

    # Машиночитаемый формат для Zabbix/Healthchecks/скриптов
    if request.GET.get('format') == 'json':
        from django.http import JsonResponse
        return JsonResponse(data, json_dumps_params={'ensure_ascii': False})

    # Простой текстовый ping
    if request.GET.get('format') == 'text':
        status = 'ok' if data['ok'] else 'warn'
        return HttpResponse(f'svod_health={status}\n', content_type='text/plain')

    return render(request, 'admin_panel/health.html', data)


# ═════════════════════════════════════════════════════════════
#  ОБРАЩЕНИЯ В ПОДДЕРЖКУ
# ═════════════════════════════════════════════════════════════

@login_required
def tickets_list(request):
    _admin_required(request.user)

    from accounts.models import SupportTicket

    q = request.GET.get('q', '').strip()
    kind = request.GET.get('kind', '').strip()
    state = request.GET.get('state', '').strip()

    qs = SupportTicket.objects.select_related('author').order_by('-created_at')

    if q:
        qs = qs.filter(_ci_filter(q, 'text', 'page', 'author__full_name', 'author__email'))
    if kind:
        qs = qs.filter(kind=kind)
    if state == 'open':
        qs = qs.filter(done=False)
    elif state == 'done':
        qs = qs.filter(done=True)

    total = qs.count()
    open_count = SupportTicket.objects.filter(done=False).count()
    done_count = SupportTicket.objects.filter(done=True).count()

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'admin_panel/tickets.html', {
        'page': page,
        'total': total,
        'open_count': open_count,
        'done_count': done_count,
        'q': q,
        'kind': kind,
        'state': state,
        'page_qs': page_qs,
        'kind_choices': SupportTicket.Kind.choices,
    })


@login_required
def ticket_toggle(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_tickets')

    from accounts.models import SupportTicket
    t = get_object_or_404(SupportTicket, pk=pk)
    t.done = not t.done
    t.save(update_fields=['done'])

    log_action(request, AuditLog.Action.TOGGLE, t,
               changes={'done': t.done})

    messages.success(
        request,
        f'Обращение #{t.pk} помечено как {"обработанное" if t.done else "открытое"}.',
    )
    return redirect('settings_tickets')


# ═════════════════════════════════════════════════════════════
#  СЕССИИ РАБОТЫ / ЦЕХА
# ═════════════════════════════════════════════════════════════

@login_required
def sessions_list(request):
    """Старый /settings/sessions/ → /manager/sessions/.

    После патча 6.1 логика живёт в manager.views.sessions_list.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    return redirect(f"{reverse('manager_sessions')}?{qs.urlencode()}")


# ═════════════════════════════════════════════════════════════
#  ЭКСПОРТ СЕССИЙ
# ═════════════════════════════════════════════════════════════

@login_required
def sessions_export(request):
    _boss_required(request.user)

    from datetime import datetime as _dt, time as _time
    from tasks.models import TimeSession, ShopSession

    tab = request.GET.get('tab', 'work').strip()
    if tab not in ('work', 'shop'):
        tab = 'work'

    user_pk = request.GET.get('user', '').strip()
    task_pk = request.GET.get('task', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    min_hours = request.GET.get('min_hours', '').strip()

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    df = _parse_date(date_from)
    dt_ = _parse_date(date_to)

    def _aware_start(d):
        return timezone.make_aware(_dt.combine(d, _time.min), timezone.get_current_timezone())

    def _aware_end(d):
        return timezone.make_aware(_dt.combine(d, _time.max), timezone.get_current_timezone())

    try:
        min_h = float(min_hours.replace(',', '.')) if min_hours else None
    except ValueError:
        min_h = None

    if tab == 'work':
        qs = TimeSession.objects.select_related(
            'task', 'executor'
        ).order_by('-finished_at')
        qs = _apply_boss_scope(qs, request.user, 'executor__department')
        date_field = 'finished_at'
    else:
        qs = ShopSession.objects.select_related(
            'task', 'executor'
        ).order_by('-started_at')
        qs = _apply_boss_scope(qs, request.user, 'executor__department')
        date_field = 'started_at'

    if df:
        qs = qs.filter(**{f'{date_field}__gte': _aware_start(df)})
    if dt_:
        qs = qs.filter(**{f'{date_field}__lte': _aware_end(dt_)})
    if user_pk.isdigit():
        qs = qs.filter(executor_id=int(user_pk))
    if task_pk.isdigit():
        qs = qs.filter(task_id=int(task_pk))
    if min_h is not None:
        qs = qs.filter(duration_hours__gte=min_h)

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    fname = f'sessions_{tab}_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')

    if tab == 'work':
        writer.writerow([
            'Завершено', 'Сотрудник', 'Email',
            'Задача ID', 'Задача', 'Часов',
            'Старт', 'Статус на закрытии', 'Комментарий',
        ])
        for s in qs:
            writer.writerow([
                s.finished_at.strftime('%d.%m.%Y %H:%M') if s.finished_at else '',
                s.executor.full_name if s.executor else '',
                s.executor.email if s.executor else '',
                s.task.pk if s.task else '',
                s.task.title if s.task else '',
                f'{s.duration_hours:.2f}'.replace('.', ',') if s.duration_hours else '0,00',
                s.started_at.strftime('%d.%m.%Y %H:%M') if s.started_at else '',
                s.get_status_at_close_display() if s.status_at_close else '',
                (s.comment or '').replace('\n', ' '),
            ])
    else:
        writer.writerow([
            'Выход в цех', 'Возврат', 'Сотрудник', 'Email',
            'Задача ID', 'Задача', 'Часов', 'Состояние',
        ])
        for s in qs:
            writer.writerow([
                s.started_at.strftime('%d.%m.%Y %H:%M') if s.started_at else '',
                s.finished_at.strftime('%d.%m.%Y %H:%M') if s.finished_at else '',
                s.executor.full_name if s.executor else '',
                s.executor.email if s.executor else '',
                s.task.pk if s.task else '',
                s.task.title if s.task else '',
                (f'{s.duration_hours:.2f}'.replace('.', ',')
                 if s.finished_at and s.duration_hours else ''),
                'завершён' if s.finished_at else 'в процессе',
            ])

    log_action(request, AuditLog.Action.UPDATE, request.user, changes={
        'export': f'sessions_{tab}_csv',
        'rows': qs.count(),
    })
    return response


@login_required
def sessions_summary(request):
    """Старый /settings/sessions/summary/ → /manager/sessions/summary/.

    После патча 6.1 логика живёт в manager.views.sessions_summary.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    return redirect(f"{reverse('manager_sessions_summary')}?{qs.urlencode()}")


# ═════════════════════════════════════════════════════════════
#  KPI-ОТЧЁТ



# ═════════════════════════════════════════════════════════════
#  KPI-ОТЧЁТ
# ═════════════════════════════════════════════════════════════

@login_required
def kpi_report(request):
    """Старый /settings/kpi/ → /manager/reports/?kind=kpi.

    После патча 5.4a логика живёт в manager.views.kpi_report_page.
    Здесь — только редирект, чтобы не ломать внешние закладки.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    qs['kind'] = 'kpi'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")


# ═════════════════════════════════════════════════════════════
#  ЗАКАЗЫ
# ═════════════════════════════════════════════════════════════

@login_required
def orders_list(request):
    _boss_required(request.user)

    from tasks.models import Order, Task, TaskBranch

    q = request.GET.get('q', '').strip()
    ship_from = request.GET.get('ship_from', '').strip()
    ship_to = request.GET.get('ship_to', '').strip()
    state = request.GET.get('state', '').strip()  # '', 'active', 'done', 'empty'

    qs = Order.objects.order_by('-id')

    if q:
        qs = qs.filter(_ci_filter(q, 'number', 'product', 'comment'))
    if ship_from:
        qs = qs.filter(ship_due__gte=ship_from)
    if ship_to:
        qs = qs.filter(ship_due__lte=ship_to)

    # Обогащаем счётчиками и прогрессом
    orders = []
    for o in qs:
        tasks = Task.objects.filter(order=o)
        total = tasks.count()
        done = tasks.filter(status=Task.Status.DONE).count()
        cancelled = tasks.filter(status=Task.Status.CANCELLED).count()
        active = tasks.exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED]).count()
        branches = TaskBranch.objects.filter(order=o).count()

        row = {
            'order': o,
            'tasks_total': total,
            'tasks_done': done,
            'tasks_active': active,
            'tasks_cancelled': cancelled,
            'branches': branches,
            'progress': int(round(100 * done / total)) if total else 0,
        }

        if state == 'active' and active == 0:
            continue
        if state == 'done' and (total == 0 or done < total):
            continue
        if state == 'empty' and total > 0:
            continue

        orders.append(row)

    return render(request, 'admin_panel/orders.html', {
        'orders': orders,
        'total': len(orders),
        'q': q,
        'ship_from': ship_from,
        'ship_to': ship_to,
        'state': state,
    })


@login_required
def order_detail(request, pk):
    _boss_required(request.user)

    from tasks.models import Order, Task, TaskBranch

    order = get_object_or_404(Order, pk=pk)

    branches = list(
        TaskBranch.objects.filter(order=order).order_by('name')
    )
    tasks = list(
        Task.objects
        .select_related('executor', 'requester', 'branch')
        .filter(order=order)
        .order_by('branch_id', 'stage_order', 'created_at')
    )

    # Задачи без ветки
    no_branch = [t for t in tasks if t.branch_id is None]

    # Группировка по ветке
    branch_groups = []
    for b in branches:
        b_tasks = [t for t in tasks if t.branch_id == b.pk]
        done = sum(1 for t in b_tasks if t.status == Task.Status.DONE)
        branch_groups.append({
            'branch': b,
            'tasks': b_tasks,
            'total': len(b_tasks),
            'done': done,
            'progress': int(round(100 * done / len(b_tasks))) if b_tasks else 0,
        })

    # Общая статистика
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == Task.Status.DONE)
    cancelled = sum(1 for t in tasks if t.status == Task.Status.CANCELLED)
    active = total - done - cancelled

    return render(request, 'admin_panel/order_detail.html', {
        'order': order,
        'branch_groups': branch_groups,
        'no_branch_tasks': no_branch,
        'total': total,
        'done': done,
        'active': active,
        'cancelled': cancelled,
        'progress': int(round(100 * done / total)) if total else 0,
    })


@login_required
def order_edit(request, pk=None):
    _admin_required(request.user)

    from tasks.models import Order

    order = get_object_or_404(Order, pk=pk) if pk else None

    if request.method == 'POST':
        number = (request.POST.get('number') or '').strip()[:60]
        product = (request.POST.get('product') or '').strip()[:200]
        ship_due = (request.POST.get('ship_due') or '').strip()
        comment = (request.POST.get('comment') or '').strip()

        errors = []
        if not number:
            errors.append('Номер заказа обязателен.')
        if not product:
            errors.append('Изделие обязательно.')

        if number:
            exists = Order.objects.filter(number__iexact=number)
            if order:
                exists = exists.exclude(pk=order.pk)
            if exists.exists():
                errors.append('Заказ с таким номером уже есть.')

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            is_new = order is None
            if is_new:
                order = Order()
            order.number = number
            order.product = product
            order.ship_due = ship_due or None
            order.comment = comment
            order.save()
            log_action(
                request,
                AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                order,
                changes={'number': order.number, 'product': order.product,
                         'ship_due': str(order.ship_due) if order.ship_due else None},
            )
            messages.success(request, f'Заказ «{number}» сохранён.')
            return redirect('settings_order_detail', pk=order.pk)

    return render(request, 'admin_panel/order_edit.html', {
        'order': order,
        'owners': User.objects.filter(
            is_active=True, role__can_manage=True,
        ).select_related('department').order_by('full_name'),
    })


@login_required
def order_delete(request, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_orders')

    from tasks.models import Order, Task

    order = get_object_or_404(Order, pk=pk)
    linked = Task.objects.filter(order=order).count()
    if linked:
        messages.error(
            request,
            f'Нельзя удалить: к заказу привязано задач ({linked}). '
            f'Отвяжите или удалите их сначала.',
        )
        return redirect('settings_order_detail', pk=order.pk)

    number = order.number
    log_action(request, AuditLog.Action.DELETE, order)
    order.delete()
    messages.success(request, f'Заказ «{number}» удалён.')
    return redirect('settings_orders')


@login_required
def branch_edit(request, order_pk, pk=None):
    _admin_required(request.user)

    from tasks.models import Order, TaskBranch

    order = get_object_or_404(Order, pk=order_pk)
    branch = get_object_or_404(TaskBranch, pk=pk) if pk else None

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:150]
        errors = []
        if not name:
            errors.append('Название ветки обязательно.')

        if name:
            exists = TaskBranch.objects.filter(order=order, name__iexact=name)
            if branch:
                exists = exists.exclude(pk=branch.pk)
            if exists.exists():
                errors.append('Ветка с таким названием уже есть в этом заказе.')

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            is_new = branch is None
            if is_new:
                branch = TaskBranch(order=order)
            branch.name = name
            branch.save()
            log_action(
                request,
                AuditLog.Action.CREATE if is_new else AuditLog.Action.UPDATE,
                branch,
                changes={'name': branch.name, 'order_id': order.pk},
            )
            messages.success(request, f'Ветка «{name}» сохранена.')
            return redirect('settings_order_detail', pk=order.pk)

    return render(request, 'admin_panel/branch_edit.html', {
        'order': order,
        'branch': branch,
    })


@login_required
def branch_delete(request, order_pk, pk):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_order_detail', pk=order_pk)

    from tasks.models import Order, Task, TaskBranch

    order = get_object_or_404(Order, pk=order_pk)
    branch = get_object_or_404(TaskBranch, pk=pk, order=order)

    linked = Task.objects.filter(branch=branch).count()
    if linked:
        messages.error(
            request,
            f'Нельзя удалить ветку: привязано задач ({linked}).',
        )
    else:
        name = branch.name
        log_action(request, AuditLog.Action.DELETE, branch)
        branch.delete()
        messages.success(request, f'Ветка «{name}» удалена.')

    return redirect('settings_order_detail', pk=order.pk)


# ═════════════════════════════════════════════════════════════
#  ПРОБЛЕМНЫЕ ЗАДАЧИ
# ═════════════════════════════════════════════════════════════

@login_required
def problematic_tasks(request):
    _boss_required(request.user)

    from datetime import timedelta
    from django.utils import timezone
    from tasks.models import Task

    today = timezone.localdate()
    now = timezone.now()

    # Пороги (можно переопределить GET-параметрами)
    try:
        stale_hours = int(request.GET.get('stale_hours') or 12)
    except ValueError:
        stale_hours = 12
    try:
        stale_days = int(request.GET.get('stale_days') or 7)
    except ValueError:
        stale_days = 7

    stale_timer_from = now - timedelta(hours=stale_hours)
    stale_review_from = now - timedelta(days=stale_days)
    deep_overdue_from = today - timedelta(days=30)

    base_qs = (
        Task.objects
        .select_related('executor', 'requester', 'order', 'branch', 'task_type')
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
    )

    base_qs = _apply_boss_scope(base_qs, request.user, 'executor__department')
    categories = []

    # 1. Без дедлайна
    qs1 = base_qs.filter(due__isnull=True).order_by('-created_at')
    categories.append({
        'key': 'no_due',
        'title': 'Без дедлайна',
        'hint': 'Задача не попадёт в хронологию, рейтинг и просрочки.',
        'severity': 'warn',
        'tasks': list(qs1[:50]),
        'count': qs1.count(),
    })

    # 2. Без плана
    qs2 = base_qs.filter(plan_hours__lte=0).order_by('-created_at')
    categories.append({
        'key': 'no_plan',
        'title': 'Без плана часов',
        'hint': 'План = 0 — KPI и эффективность посчитать нельзя.',
        'severity': 'warn',
        'tasks': list(qs2[:50]),
        'count': qs2.count(),
    })

    # 3. IN_PROGRESS без таймера
    qs3 = base_qs.filter(
        status=Task.Status.IN_PROGRESS, session_started_at__isnull=True,
    ).order_by('-created_at')
    categories.append({
        'key': 'in_progress_no_timer',
        'title': '«В работе», но таймер не идёт',
        'hint': 'Сотрудник забыл нажать «Старт» или статус проставлен вручную.',
        'severity': 'danger',
        'tasks': list(qs3[:50]),
        'count': qs3.count(),
    })

    # 4. Таймер идёт слишком долго
    qs4 = base_qs.filter(
        session_started_at__isnull=False,
        session_started_at__lt=stale_timer_from,
    ).order_by('session_started_at')
    categories.append({
        'key': 'stale_timer',
        'title': f'Таймер идёт дольше {stale_hours} ч',
        'hint': 'Возможно, забыли поставить паузу или закрыть сессию.',
        'severity': 'danger',
        'tasks': list(qs4[:50]),
        'count': qs4.count(),
    })

    # 5. Зависла на проверке
    qs5 = base_qs.filter(
        status=Task.Status.REVIEW,
        finished_at__lt=stale_review_from,
    ).order_by('finished_at')
    categories.append({
        'key': 'stale_review',
        'title': f'На проверке > {stale_days} дн',
        'hint': 'Руководитель долго не принимает результат.',
        'severity': 'warn',
        'tasks': list(qs5[:50]),
        'count': qs5.count(),
    })

    # 6. Зависла на доработке
    qs6 = base_qs.filter(
        status=Task.Status.REWORK,
        finished_at__lt=stale_review_from,
    ).order_by('finished_at')
    categories.append({
        'key': 'stale_rework',
        'title': f'На доработке > {stale_days} дн',
        'hint': 'Задача вернулась и застряла.',
        'severity': 'warn',
        'tasks': list(qs6[:50]),
        'count': qs6.count(),
    })

    # 7. Глубокая просрочка
    qs7 = base_qs.filter(due__lt=deep_overdue_from).order_by('due')
    categories.append({
        'key': 'deep_overdue',
        'title': 'Просрочка > 30 дней',
        'hint': 'Скорее всего задачу уже надо отменить или разбить.',
        'severity': 'danger',
        'tasks': list(qs7[:50]),
        'count': qs7.count(),
    })

    # 8. С заказом, но без ветки
    qs8 = base_qs.filter(
        order__isnull=False, branch__isnull=True,
    ).order_by('-created_at')
    categories.append({
        'key': 'order_no_branch',
        'title': 'В заказе, но без ветки',
        'hint': 'Не попадёт в цепочки этапов и в карточку заказа по ветке.',
        'severity': 'warn',
        'tasks': list(qs8[:50]),
        'count': qs8.count(),
    })

    # 9. Аномально большой план
    qs9 = base_qs.filter(plan_hours__gt=100).order_by('-plan_hours')
    categories.append({
        'key': 'huge_plan',
        'title': 'План > 100 часов',
        'hint': 'Возможно, задачу нужно разбить на подзадачи.',
        'severity': 'warn',
        'tasks': list(qs9[:50]),
        'count': qs9.count(),
    })

    total_problems = sum(c['count'] for c in categories)

    # XLSX-экспорт
    if request.GET.get('format') == 'xlsx':
        from .excel import build_xlsx, xlsx_response

        header = [
            'Категория', 'ID', 'Задача', 'Статус', 'Приоритет',
            'Исполнитель', 'Постановщик', 'Срок',
            'План, ч', 'Накоплено, ч', 'Таймер с', 'Создана',
        ]
        body = []
        for c in categories:
            for t in c['tasks']:
                body.append([
                    c['title'],
                    t.pk,
                    t.title,
                    t.get_status_display(),
                    t.get_priority_display(),
                    t.executor.full_name if t.executor else '',
                    t.requester.full_name if t.requester else '',
                    t.due.strftime('%d.%m.%Y') if t.due else '',
                    round(t.plan_hours or 0, 2),
                    round(t.accumulated_hours or 0, 2),
                    t.session_started_at.strftime('%d.%m.%Y %H:%M') if t.session_started_at else '',
                    t.created_at.strftime('%d.%m.%Y %H:%M') if t.created_at else '',
                ])

        total = [
            'ИТОГО', '', '', '', '', '', '', '', '', '', '', len(body),
        ]

        data = build_xlsx(
            header=header, rows=body, total_row=total,
            sheet_name='Проблемные',
            title=f'Проблемные задачи на {datetime.now():%d.%m.%Y}',
            column_widths=[28, 8, 50, 14, 12, 24, 24, 12, 10, 12, 18, 18],
        )
        fname = f'problematic_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'problematic_xlsx',
            'rows': len(body),
        })
        return xlsx_response(data, fname)

    # CSV-экспорт
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'problematic_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = csv.writer(response, delimiter=';')
        writer.writerow([
            'Категория', 'ID', 'Задача', 'Статус', 'Приоритет',
            'Исполнитель', 'Постановщик', 'Срок',
            'План, ч', 'Накоплено, ч', 'Таймер с', 'Создана',
        ])
        for c in categories:
            for t in c['tasks']:
                writer.writerow([
                    c['title'],
                    t.pk,
                    t.title,
                    t.get_status_display(),
                    t.get_priority_display(),
                    t.executor.full_name if t.executor else '',
                    t.requester.full_name if t.requester else '',
                    t.due.strftime('%d.%m.%Y') if t.due else '',
                    f'{t.plan_hours:.2f}'.replace('.', ',') if t.plan_hours else '0,00',
                    f'{t.accumulated_hours:.2f}'.replace('.', ',') if t.accumulated_hours else '0,00',
                    t.session_started_at.strftime('%d.%m.%Y %H:%M') if t.session_started_at else '',
                    t.created_at.strftime('%d.%m.%Y %H:%M') if t.created_at else '',
                ])
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'problematic_csv',
            'rows': sum(len(c['tasks']) for c in categories),
        })
        return response

    return render(request, 'admin_panel/problematic.html', {
        'categories': categories,
        'total_problems': total_problems,
        'stale_hours': stale_hours,
        'stale_days': stale_days,
    })


# ═════════════════════════════════════════════════════════════
#  НАСТРОЙКИ АЛЕРТОВ
# ═════════════════════════════════════════════════════════════

@login_required
def alert_settings_edit(request):
    _admin_required(request.user)

    from .models import AdminAlertSettings

    cfg = AdminAlertSettings.get_solo()

    if request.method == 'POST':
        try:
            threshold = int(request.POST.get('failed_logins_threshold') or 5)
            window_min = int(request.POST.get('failed_logins_window_min') or 15)
        except ValueError:
            messages.error(request, 'Пороги должны быть целыми числами.')
            return redirect('settings_alert_settings')

        if threshold < 1 or window_min < 1:
            messages.error(request, 'Пороги должны быть больше нуля.')
            return redirect('settings_alert_settings')

        cfg.failed_logins_threshold = threshold
        cfg.failed_logins_window_min = window_min
        cfg.single_admin_alert = request.POST.get('single_admin_alert') == '1'
        cfg.no_norm_alert = request.POST.get('no_norm_alert') == '1'
        cfg.notify_email = (request.POST.get('notify_email') or '').strip()
        cfg.notify_on_danger_only = request.POST.get('notify_on_danger_only') == '1'
        cfg.save()

        log_action(request, AuditLog.Action.UPDATE, cfg, changes={
            'failed_logins_threshold': threshold,
            'failed_logins_window_min': window_min,
            'single_admin_alert': cfg.single_admin_alert,
            'no_norm_alert': cfg.no_norm_alert,
            'notify_email': cfg.notify_email,
            'notify_on_danger_only': cfg.notify_on_danger_only,
        })
        messages.success(request, 'Настройки уведомлений сохранены.')
        return redirect('settings_alerts')

    return render(request, 'admin_panel/alert_settings.html', {'cfg': cfg})


@login_required
def alert_test_email(request):
    _admin_required(request.user)

    if request.method != 'POST':
        return redirect('settings_alert_settings')

    from .alerts import get_alert_settings
    from django.core.mail import send_mail
    from django.conf import settings as dj_settings

    cfg = get_alert_settings()
    if not cfg or not cfg.notify_email:
        messages.error(request, 'Сначала укажите email для уведомлений.')
        return redirect('settings_alert_settings')

    try:
        send_mail(
            subject='[СВОД] Тестовое уведомление',
            message='Это тестовое письмо из админки СВОД. Если вы его видите — email настроен корректно.',
            from_email=dj_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[cfg.notify_email],
            fail_silently=False,
        )
        messages.success(request, f'Тестовое письмо отправлено на {cfg.notify_email}.')
    except Exception as e:
        messages.error(request, f'Не удалось отправить: {e}')

    return redirect('settings_alert_settings')


# ═════════════════════════════════════════════════════════════
#  ЛОГИ ЗАДАЧ
# ═════════════════════════════════════════════════════════════

@login_required
def task_logs_list(request):
    """Старый /settings/tasklogs/ → /manager/logs/.

    После патча 6.2 логика живёт в manager.views.task_logs_page.
    Старый URL сохранён как редирект.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    return redirect(f"{reverse('manager_task_logs')}?{qs.urlencode()}")


# ═════════════════════════════════════════════════════════════
#  ЦЕНТР ОБСЛУЖИВАНИЯ
# ═════════════════════════════════════════════════════════════

@login_required
def maintenance_page(request):
    _boss_required(request.user)

    from datetime import timedelta
    from django.utils import timezone
    from django.conf import settings as dj_settings
    from . import backups as bk
    from .health import collect_health
    from .models import AdminAlert, LoginEvent
    from tasks.models import Task

    now = timezone.now()
    day_ago = now - timedelta(hours=24)

    # Состояние
    health = collect_health()
    backups = bk.list_backups()
    latest_backup = backups[0] if backups else None
    latest_backup_age_h = (
        round((now.timestamp() - latest_backup['mtime']) / 3600, 1)
        if latest_backup else None
    )

    # Последние ручные операции (из аудита)
    recent_ops = list(
        AuditLog.objects
        .select_related('actor')
        .filter(
            Q(changes__isnull=False)
        )
        .order_by('-created_at')[:30]
    )

    # Счётчики
    counters = {
        'audit_total': AuditLog.objects.count(),
        'logins_total': LoginEvent.objects.count(),
        'alerts_unseen': AdminAlert.objects.filter(seen=False).count(),
        'tasks_active': Task.objects.exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED]
        ).count(),
        'backups_count': len(backups),
        'last_24h_logins': LoginEvent.objects.filter(
            success=True, created_at__gte=day_ago
        ).count(),
        'last_24h_logins_fail': LoginEvent.objects.filter(
            success=False, created_at__gte=day_ago
        ).count(),
    }

    # Настройки окружения — только read-only
    env_brief = {
        'debug': bool(dj_settings.DEBUG),
        'timezone': dj_settings.TIME_ZONE,
        'db_engine': dj_settings.DATABASES['default']['ENGINE'].split('.')[-1],
        'ad_enabled': bool(getattr(dj_settings, 'AD_ENABLED', False)),
        'email_backend': dj_settings.EMAIL_BACKEND.split('.')[-1],
    }

    return render(request, 'admin_panel/maintenance.html', {
        'health': health,
        'backups': backups[:5],
        'backups_total': len(backups),
        'latest_backup': latest_backup,
        'latest_backup_age_h': latest_backup_age_h,
        'counters': counters,
        'env_brief': env_brief,
        'recent_ops': recent_ops,
        'now': now,
    })


# ═════════════════════════════════════════════════════════════
#  РАССЫЛКИ
# ═════════════════════════════════════════════════════════════

@login_required
def broadcast_page(request):
    _admin_required(request.user)

    from accounts.models import Department, Role
    from .models import BroadcastLog
    from .broadcast import resolve_recipients

    if request.method == 'POST':
        action = request.POST.get('action') or 'preview'

        target = (request.POST.get('target') or 'all').strip()
        dept_pk = (request.POST.get('department') or '').strip()
        role_pk = (request.POST.get('role') or '').strip()
        subject = (request.POST.get('subject') or '').strip()[:200]
        body = (request.POST.get('body') or '').strip()

        dept_id = int(dept_pk) if dept_pk.isdigit() else None
        role_id = int(role_pk) if role_pk.isdigit() else None

        recipients_qs = resolve_recipients(target, dept_id, role_id)
        recipients_total = recipients_qs.count()

        target_label = ''
        if target == 'department' and dept_id:
            d = Department.objects.filter(pk=dept_id).first()
            target_label = f'Подразделение: {d.name}' if d else ''
        elif target == 'role' and role_id:
            r = Role.objects.filter(pk=role_id).first()
            target_label = f'Роль: {r.name}' if r else ''
        elif target == 'admins':
            target_label = 'Администраторы'
        else:
            target_label = 'Все пользователи'

        if action == 'send':
            if not subject or not body:
                messages.error(request, 'Заполните тему и текст.')
                return redirect('settings_broadcast')

            if recipients_total == 0:
                messages.error(request, 'Нет получателей в выбранном сегменте.')
                return redirect('settings_broadcast')

            log = BroadcastLog.objects.create(
                sender=request.user,
                target=target,
                target_label=target_label,
                subject=subject,
                body=body,
                recipients_total=recipients_total,
            )

            from .broadcast import send_broadcast
            sent, failed = send_broadcast(subject, body, list(recipients_qs), sender=request.user)

            log.recipients_sent = sent
            log.recipients_failed = failed
            log.save(update_fields=['recipients_sent', 'recipients_failed'])

            log_action(request, AuditLog.Action.CREATE, log, changes={
                'subject': subject, 'target': target, 'total': recipients_total,
                'sent': sent, 'failed': failed,
            })

            if failed:
                messages.warning(
                    request,
                    f'Отправлено: {sent} из {recipients_total}. Ошибок: {failed}.',
                )
            else:
                messages.success(request, f'Отправлено: {sent} писем.')
            return redirect('settings_broadcast')

        # Превью — не отправляем
        preview = list(recipients_qs.order_by('full_name')[:50])
        return render(request, 'admin_panel/broadcast.html', {
            'target': target,
            'dept_pk': dept_pk,
            'role_pk': role_pk,
            'subject': subject,
            'body': body,
            'recipients_total': recipients_total,
            'recipients_preview': preview,
            'target_label': target_label,
            'stage': 'preview',
            'departments': Department.objects.all().order_by('name'),
            'roles': Role.objects.all().order_by('name'),
            'history': list(BroadcastLog.objects.all()[:20]),
        })

    # GET — форма
    return render(request, 'admin_panel/broadcast.html', {
        'stage': 'form',
        'target': 'all',
        'departments': Department.objects.all().order_by('name'),
        'roles': Role.objects.all().order_by('name'),
        'history': list(BroadcastLog.objects.all()[:20]),
    })


# ═════════════════════════════════════════════════════════════
#  АНАЛИТИКА ПО ПОДРАЗДЕЛЕНИЯМ
# ═════════════════════════════════════════════════════════════

@login_required
def analytics_departments(request):
    """Старый /settings/analytics/departments/ → /manager/reports/?kind=departments.

    После патча 5.4b логика живёт в manager.views.analytics_departments_page.
    Здесь — только редирект.
    """
    _boss_required(request.user)
    qs = request.GET.copy()
    qs['kind'] = 'departments'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")

    from datetime import datetime as _dt, time as _time
    from django.db.models import Count
    from django.utils import timezone
    from tasks.models import Task

    today = timezone.localdate()
    default_from = today.replace(day=1)

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    date_from = _parse_date(request.GET.get('date_from', '')) or default_from
    date_to = _parse_date(request.GET.get('date_to', '')) or today

    start_dt = timezone.make_aware(
        _dt.combine(date_from, _time.min), timezone.get_current_timezone(),
    )
    end_dt = timezone.make_aware(
        _dt.combine(date_to, _time.max), timezone.get_current_timezone(),
    )

    # Все подразделения с активными сотрудниками
    depts = Department.objects.annotate(
        people_total=Count('users', distinct=True),
    ).filter(people_total__gt=0).order_by('name')

    # Boss — видит только своё подразделение
    if _is_narrow_scope(request.user):
        depts = depts.filter(pk=request.user.department_id)

    # Завершённые задачи в периоде
    done_qs = Task.objects.filter(
        status=Task.Status.DONE,
        finished_at__gte=start_dt, finished_at__lte=end_dt,
    )
    if _is_narrow_scope(request.user):
        done_qs = done_qs.filter(executor__department_id=request.user.department_id)

    # Задачи в работе (для просрочек)
    active_qs = Task.objects.exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    )
    if _is_narrow_scope(request.user):
        active_qs = active_qs.filter(executor__department_id=request.user.department_id)

    rows = []
    for d in depts:
        people_active = User.objects.filter(is_active=True, department=d).count()
        dept_done = list(done_qs.filter(executor__department=d))

        tasks_done = len(dept_done)
        plan_hours = sum(t.plan_hours or 0 for t in dept_done)
        fact_hours = sum(t.accumulated_hours or 0 for t in dept_done)
        on_time = sum(
            1 for t in dept_done
            if t.due and t.finished_at and t.finished_at.date() <= t.due
        )
        overdue_done = sum(
            1 for t in dept_done
            if t.due and t.finished_at and t.finished_at.date() > t.due
        )

        on_time_pct = int(round(100 * on_time / tasks_done)) if tasks_done else 0
        efficiency = int(round(100 * plan_hours / fact_hours)) if fact_hours > 0 else 0

        rows.append({
            'department': d,
            'people_total': d.people_total,
            'people_active': people_active,
            'tasks_done': tasks_done,
            'tasks_overdue_all': active_qs.filter(
                executor__department=d, due__lt=today,
            ).count(),
            'plan_hours': round(plan_hours, 2),
            'fact_hours': round(fact_hours, 2),
            'delta': round(fact_hours - plan_hours, 2),
            'on_time_pct': on_time_pct,
            'on_time': on_time,
            'overdue_done': overdue_done,
            'efficiency': efficiency,
            # для bar-chart
            'plan_pct': 0,
            'fact_pct': 0,
        })

    # Нормируем bar-chart по максимуму
    max_hours = max((r['plan_hours'] + r['fact_hours'] for r in rows), default=0) or 1
    for r in rows:
        r['plan_pct'] = min(100, int(round(100 * r['plan_hours'] / max_hours)))
        r['fact_pct'] = min(100, int(round(100 * r['fact_hours'] / max_hours)))

    # ── Итоги ──
    grand = {
        'people_total': sum(r['people_total'] for r in rows),
        'people_active': sum(r['people_active'] for r in rows),
        'tasks_done': sum(r['tasks_done'] for r in rows),
        'plan_hours': round(sum(r['plan_hours'] for r in rows), 2),
        'fact_hours': round(sum(r['fact_hours'] for r in rows), 2),
        'on_time': sum(r['on_time'] for r in rows),
    }
    grand['delta'] = round(grand['fact_hours'] - grand['plan_hours'], 2)
    grand['on_time_pct'] = (
        int(round(100 * grand['on_time'] / grand['tasks_done']))
        if grand['tasks_done'] else 0
    )
    grand['efficiency'] = (
        int(round(100 * grand['plan_hours'] / grand['fact_hours']))
        if grand['fact_hours'] > 0 else 0
    )

    # CSV-экспорт
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'departments_{date_from}_{date_to}_{datetime.now().strftime("%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = csv.writer(response, delimiter=';')
        writer.writerow([
            'Подразделение', 'Сотрудников (акт./всего)',
            'Задач готово', 'В срок', 'Просрочено', 'В срок, %',
            'План, ч', 'Факт, ч', 'Δ, ч', 'КПД, %',
        ])
        for r in rows:
            writer.writerow([
                r['department'].name,
                f'{r["people_active"]}/{r["people_total"]}',
                r['tasks_done'],
                r['on_time'],
                r['overdue_done'],
                r['on_time_pct'],
                f'{r["plan_hours"]:.2f}'.replace('.', ','),
                f'{r["fact_hours"]:.2f}'.replace('.', ','),
                f'{r["delta"]:.2f}'.replace('.', ','),
                r['efficiency'],
            ])
        writer.writerow([
            'ИТОГО', f'{grand["people_active"]}/{grand["people_total"]}',
            grand['tasks_done'], grand['on_time'], '', grand['on_time_pct'],
            f'{grand["plan_hours"]:.2f}'.replace('.', ','),
            f'{grand["fact_hours"]:.2f}'.replace('.', ','),
            f'{grand["delta"]:.2f}'.replace('.', ','),
            grand['efficiency'],
        ])
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'departments_csv',
            'period': f'{date_from}..{date_to}',
            'rows': len(rows),
        })
        return response

    return render(request, 'admin_panel/analytics_departments.html', {
        'rows': rows,
        'grand': grand,
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'is_narrow_scope': _is_narrow_scope(request.user),
        'scope_department': request.user.department if _is_narrow_scope(request.user) else None,
    })


# ═════════════════════════════════════════════════════════════
#  ЦЕЛОСТНОСТЬ ДАННЫХ
# ═════════════════════════════════════════════════════════════

@login_required
def integrity_check(request):
    _admin_required(request.user)

    from .integrity import collect_integrity_checks

    sections, summary = collect_integrity_checks()

    # CSV-экспорт
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'integrity_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = csv.writer(response, delimiter=';')
        writer.writerow(['Категория', 'Серьёзность', 'Кол-во', 'Описание'])
        for s in sections:
            if s['count']:
                writer.writerow([
                    s['title'],
                    s['severity'],
                    s['count'],
                    s['description'],
                ])
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'integrity_csv',
            'problems': summary['total_problems'],
        })
        return response

    return render(request, 'admin_panel/integrity.html', {
        'sections': sections,
        'summary': summary,
    })

# ═════════════════════════════════════════════════════════════
#  УВОЛЬНЕНИЕ / ВОССТАНОВЛЕНИЕ
# ═════════════════════════════════════════════════════════════

@login_required
@require_POST
def user_dismiss(request, pk):
    """Уволить сотрудника: is_active=False, статус=dismissed, дата, причина."""
    _admin_required(request.user)
    from django.utils import timezone
    from datetime import datetime as _dt

    user = get_object_or_404(User, pk=pk)

    if user.pk == request.user.pk:
        messages.error(request, 'Нельзя уволить самого себя.')
        return redirect('settings_user_detail', pk=pk)

    if _is_admin_user(user) and _active_admin_count(exclude_user_ids=[user.pk]) == 0:
        messages.error(
            request,
            'Это последний активный администратор. Сначала назначьте другого.'
        )
        return redirect('settings_user_detail', pk=pk)

    reason = (request.POST.get('reason') or '').strip()[:200]
    date_str = (request.POST.get('dismissed_at') or '').strip()

    dismissed_at = None
    if date_str:
        try:
            dismissed_at = _dt.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            dismissed_at = None

    before_status = user.employment_status
    user.dismiss(reason=reason, dismissed_at=dismissed_at or timezone.localdate())

    log_action(request, AuditLog.Action.UPDATE, user, changes={
        'employment_status': [before_status, user.employment_status],
        'dismissed_at': user.dismissed_at.isoformat() if user.dismissed_at else None,
        'dismissed_reason': user.dismissed_reason,
    })

    open_count = user.open_tasks().count()
    if open_count:
        messages.warning(
            request,
            f'«{user.full_name}» уволен. У него {open_count} открытых задач — '
            f'их нужно переназначить.'
        )
    else:
        messages.success(request, f'«{user.full_name}» уволен.')

    return redirect('settings_user_detail', pk=pk)


@login_required
@require_POST
def user_restore(request, pk):
    """Вернуть из увольнения."""
    _admin_required(request.user)

    user = get_object_or_404(User, pk=pk)
    if not user.is_dismissed and not user.is_archived:
        messages.info(request, 'Пользователь и так активен.')
        return redirect('settings_user_detail', pk=pk)

    before_status = user.employment_status
    user.restore_from_dismissal()

    log_action(request, AuditLog.Action.UPDATE, user, changes={
        'employment_status': [before_status, user.employment_status],
    })

    messages.success(request, f'«{user.full_name}» возвращён в работу.')
    return redirect('settings_user_detail', pk=pk)


@login_required
@require_POST
def user_reassign_tasks(request, pk):
    """Массово переназначить все открытые задачи с одного исполнителя на другого."""
    _admin_required(request.user)

    from tasks.models import TaskLog
    from django.db import transaction

    from_user = get_object_or_404(User, pk=pk)

    to_pk = (request.POST.get('to_user') or '').strip()
    if not to_pk.isdigit():
        messages.error(request, 'Выберите нового исполнителя.')
        return redirect('settings_user_detail', pk=pk)

    to_user = User.objects.filter(pk=int(to_pk), is_active=True).first()
    if not to_user:
        messages.error(request, 'Новый исполнитель не найден или отключён.')
        return redirect('settings_user_detail', pk=pk)

    # ── Проверка scope: руководитель может назначать только из своего отдела ──
    is_full_scope = (
            request.user.is_superuser
            or request.user.can_plant
            or request.user.is_admin_role
    )
    if not is_full_scope:
        if not request.user.is_boss:
            messages.error(request, 'Раздел доступен только руководителям.')
            return redirect('settings_user_detail', pk=pk)
        if not request.user.department_id:
            messages.error(request, 'У вас не назначено подразделение.')
            return redirect('settings_user_detail', pk=pk)
        if to_user.department_id != request.user.department_id:
            messages.error(
                request,
                'Можно переназначить только сотруднику своего подразделения.'
            )
            return redirect('settings_user_detail', pk=pk)

    if to_user.pk == from_user.pk:
        messages.error(request, 'Нельзя переназначить на того же сотрудника.')
        return redirect('settings_user_detail', pk=pk)

    tasks = list(from_user.open_tasks())
    if not tasks:
        messages.info(request, 'У сотрудника нет открытых задач.')
        return redirect('settings_user_detail', pk=pk)

    with transaction.atomic():
        for t in tasks:
            old_name = from_user.full_name
            t.executor = to_user
            t.save(update_fields=['executor'])
            TaskLog.objects.create(
                task=t,
                kind=TaskLog.Kind.PLAN,
                author=request.user,
                comment=f'Переназначение с «{old_name}» (уволен) на «{to_user.full_name}»',
            )

    log_action(request, AuditLog.Action.UPDATE, from_user, changes={
        'reassign_tasks': {'to': to_user.pk, 'count': len(tasks)},
    })

    messages.success(
        request,
        f'Переназначено {len(tasks)} задач(и) на «{to_user.full_name}».',
    )
    return redirect('settings_user_detail', pk=pk)

@login_required
def onboarding_stats(request):
    """Кто прошёл обучение, где остановился."""
    _admin_required(request.user)

    from accounts.onboarding import ONBOARDING_ROUTE

    users_qs = (
        User.objects
        .filter(is_active=True)
        .select_related('department', 'role')
        .order_by('full_name')
    )

    total_steps = len(ONBOARDING_ROUTE)
    step_labels = {step[0]: step[3] for step in ONBOARDING_ROUTE}

    # Сводные счётчики
    stats = {
        'total': 0,
        'not_started': 0,
        'in_progress': 0,
        'done': 0,
    }
    by_step = {step[0]: 0 for step in ONBOARDING_ROUTE}

    rows = []
    for u in users_qs:
        seen = u.tour_seen or {}
        done_count = sum(1 for step in ONBOARDING_ROUTE if step[0] in seen)

        if u.onboarding_done:
            state = 'done'
            stats['done'] += 1
        elif done_count == 0:
            state = 'not_started'
            stats['not_started'] += 1
        else:
            state = 'in_progress'
            stats['in_progress'] += 1

        stats['total'] += 1

        # Сколько людей дошли до каждого шага
        for i, step in enumerate(ONBOARDING_ROUTE):
            if done_count > i:
                by_step[step[0]] += 1

        rows.append({
            'user': u,
            'done_count': done_count,
            'total': total_steps,
            'percent': int(done_count * 100 / total_steps) if total_steps else 0,
            'state': state,
            'onboarding_done': u.onboarding_done,
            'tour_seen': seen,
            'last_seen': max(seen.values()) if seen else None,
        })

    # Разбивка по шагам — для воронки
    funnel = []
    for step in ONBOARDING_ROUTE:
        funnel.append({
            'tour_id': step[0],
            'title': step[3],
            'reached': by_step[step[0]],
            'percent': int(by_step[step[0]] * 100 / stats['total']) if stats['total'] else 0,
        })

    return render(request, 'admin_panel/onboarding_stats.html', {
        'rows': rows,
        'stats': stats,
        'funnel': funnel,
        'total_steps': total_steps,
        'step_labels': step_labels,
    })

@login_required
@require_POST
def onboarding_reset_user(request, pk):
    """Сбросить прогресс обучения для конкретного пользователя."""
    _admin_required(request.user)

    user = get_object_or_404(User, pk=pk)
    before = dict(user.tour_seen or {})
    user.tour_seen = {}
    user.onboarding_done = False
    user.save(update_fields=['tour_seen', 'onboarding_done'])

    log_action(request, AuditLog.Action.UPDATE, user, changes={
        'reset_onboarding': True,
        'tours_before': len(before),
    })

    messages.success(
        request,
        f'Прогресс обучения «{user.full_name}» сброшен.'
    )
    return redirect('settings_onboarding_stats')

# ═════════════════════════════════════════════════════════════
#  РАЗРАБОТКА (только для is_developer)
# ═════════════════════════════════════════════════════════════

def _developer_required(user):
    """Пускает разработчика, админа или суперюзера.

    Отдельная проверка (не _admin_required): обычный разработчик
    не должен получать доступ ко всей админке — только к своему
    техническому разделу.
    """
    if not (user.is_superuser or user.is_admin_role or user.is_developer):
        raise PermissionDenied(
            'Раздел доступен только разработчику или администратору.'
        )


@login_required
def dev_home(request):
    """Технический пульт: планировщик, health, логи, модели.

    Это скелет. Наполним в следующих патчах. Сейчас —
    рабочие плитки без сложных действий.
    """
    import json
    import platform
    from pathlib import Path

    from django.apps import apps
    from django.conf import settings as dj_settings

    _developer_required(request.user)

    # ── Планировщик ──
    scheduler_state = {}
    state_file = Path(dj_settings.BASE_DIR) / 'logs' / 'scheduler_state.json'
    if state_file.is_file():
        try:
            scheduler_state = json.loads(state_file.read_text(encoding='utf-8'))
        except Exception as e:
            scheduler_state = {'__error__': str(e)}

    # ── Health ──
    from .health import collect_health
    health = collect_health()

    # ── Логи ──
    log_dir = Path(dj_settings.BASE_DIR) / 'logs'
    log_files = []
    if log_dir.is_dir():
        for p in sorted(log_dir.glob('*.log'), key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            log_files.append({
                'name': p.name,
                'size': st.st_size,
                'mtime': timezone.datetime.fromtimestamp(st.st_mtime),
            })

    # Последний лог — хвост 30 строк
    last_log_lines = []
    if log_files:
        last_path = log_dir / log_files[0]['name']
        try:
            with last_path.open('r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                last_log_lines = [ln.rstrip('\n') for ln in all_lines[-30:]]
        except Exception as e:
            last_log_lines = [f'Ошибка чтения: {e}']

    # ── Модели ──
    models_info = []
    for model in apps.get_models():
        try:
            cnt = model.objects.count()
            err = None
        except Exception as e:
            cnt = None
            err = str(e)
        models_info.append({
            'app': model._meta.app_label,
            'name': model.__name__,
            'verbose': str(model._meta.verbose_name_plural),
            'count': cnt,
            'error': err,
        })
    models_info.sort(key=lambda m: (m['app'], m['name']))
    total_rows = sum(m['count'] or 0 for m in models_info)

    # ── Окружение ──
    env = {
        'python': platform.python_version(),
        'platform': f'{platform.system()} {platform.release()}',
        'debug': bool(dj_settings.DEBUG),
        'db_engine': dj_settings.DATABASES['default']['ENGINE'].split('.')[-1],
        'tz': dj_settings.TIME_ZONE,
        'scheduler_state_file': str(state_file),
        'scheduler_state_exists': state_file.is_file(),
    }

    return render(request, 'admin_panel/dev.html', {
        'env': env,
        'scheduler_state': scheduler_state,
        'health': health,
        'log_files': log_files,
        'last_log_lines': last_log_lines,
        'models_info': models_info,
        'total_rows': total_rows,
        'now': timezone.now(),
    })
