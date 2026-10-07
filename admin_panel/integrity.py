from datetime import timedelta

from django.utils import timezone
from django.db.models import Q, Count

from accounts.models import Department, Role
from tasks.models import Task, TimeSession, ShopSession


def _section(key, title, description, severity, qs, limit=50, url_prefix='/tasks/task/'):
    """Собирает секцию: (rows, count)."""
    try:
        count = qs.count()
    except Exception:
        count = 0
    rows = []
    if count:
        try:
            for obj in qs[:limit]:
                rows.append(obj)
        except Exception:
            pass
    return {
        'key': key,
        'title': title,
        'description': description,
        'severity': severity,
        'count': count,
        'rows': rows,
        'url_prefix': url_prefix,
    }


def collect_integrity_checks():
    now = timezone.now()
    today = timezone.localdate()

    sections = []

    # 1. Задачи с finished_at, но статус не завершающий
    qs = Task.objects.filter(
        finished_at__isnull=False,
    ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
    sections.append(_section(
        'finished_at_wrong_status',
        'Завершены, но статус не завершающий',
        'У задачи заполнено «Финиш», но статус — не «Готово» и не «Отменена».',
        'danger',
        qs.select_related('executor', 'requester').order_by('-finished_at'),
    ))

    # 2. Задачи DONE/CANCELLED без finished_at
    qs = Task.objects.filter(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED],
        finished_at__isnull=True,
    )
    sections.append(_section(
        'done_no_finished_at',
        'Завершены без даты финиша',
        'Статус «Готово» или «Отменена», но поле «Финиш» пустое. Сломает KPI.',
        'warn',
        qs.select_related('executor').order_by('-created_at'),
    ))

    # 3. Таймер идёт, но статус не IN_PROGRESS
    qs = Task.objects.filter(
        session_started_at__isnull=False,
    ).exclude(status=Task.Status.IN_PROGRESS)
    sections.append(_section(
        'timer_wrong_status',
        'Таймер идёт, но статус не «В работе»',
        'На задаче запущена сессия, но статус другой. Зависший таймер.',
        'danger',
        qs.select_related('executor').order_by('session_started_at'),
    ))

    # 4. Сессии работы с нулевой/отрицательной длительностью.
    qs = TimeSession.objects.filter(duration_hours__lte=0)
    sections.append(_section(
        'session_zero_duration',
        'Сессии работы с нулевой/отрицательной длительностью',
        'Похоже на сбой подсчёта: duration_hours <= 0.',
        'warn',
        qs.select_related('task', 'executor').order_by('-finished_at'),
        url_prefix='/settings/sessions/?task=',
    ))

    # 4a. Сессии работы длиннее 48 часов
    qs = TimeSession.objects.filter(duration_hours__gt=48)
    sections.append(_section(
        'session_too_long',
        'Сессии работы длиннее 48 часов',
        'Скорее всего, забыли остановить таймер. Проверьте на реальность.',
        'warn',
        qs.select_related('task', 'executor').order_by('-duration_hours'),
        url_prefix='/settings/sessions/?task=',
    ))

    # 4b. Цеховые сессии с нулевой/отрицательной длительностью
    qs = ShopSession.objects.filter(
        finished_at__isnull=False,
        duration_hours__lte=0,
    )
    sections.append(_section(
        'shop_zero_duration',
        'Цеховые сессии с нулевой/отрицательной длительностью',
        'Нарушение учёта времени в цеху: duration_hours <= 0.',
        'warn',
        qs.select_related('task', 'executor').order_by('-finished_at'),
        url_prefix='/settings/sessions/?tab=shop&task=',
    ))

    # 4a. Слишком длинные сессии работы (> 48 ч)
    qs = TimeSession.objects.filter(duration_hours__gt=48)
    sections.append(_section(
        'session_too_long',
        'Сессии работы длиннее 48 часов',
        'Скорее всего, забыли остановить таймер. Проверьте на реальность.',
        'warn',
        qs.select_related('task', 'executor').order_by('-duration_hours'),
        url_prefix='/settings/sessions/?task=',
    ))

    # 4b. Цеховые сессии с нулевой/отрицательной длительностью
    qs = ShopSession.objects.filter(
        finished_at__isnull=False,
        duration_hours__lte=0,
    )
    sections.append(_section(
        'shop_zero_duration',
        'Цеховые сессии с нулевой/отрицательной длительностью',
        'Нарушение учёта времени в цеху: duration_hours <= 0.',
        'warn',
        qs.select_related('task', 'executor').order_by('-finished_at'),
        url_prefix='/settings/sessions/?tab=shop&task=',
    ))

    # 5. Сессии, где финиш < старт
    qs = TimeSession.objects.filter(finished_at__lt=models_f_expr('started_at'))
    sections.append(_section(
        'session_negative',
        'Сессии, где финиш раньше старта',
        'Битые метки времени. Возможно, сбой при редактировании.',
        'danger',
        qs.select_related('task', 'executor').order_by('-finished_at'),
        url_prefix='/settings/sessions/?task=',
    ))

    # 6. Активные цеховые сессии старше 24 часов
    old_shop = now - timedelta(hours=24)
    qs = ShopSession.objects.filter(
        finished_at__isnull=True,
        started_at__lt=old_shop,
    )
    sections.append(_section(
        'shop_stale',
        'Активные цеховые сессии старше 24 часов',
        'Сотрудник вышел в цех и не вернулся. Возможно, забыл закрыть.',
        'warn',
        qs.select_related('task', 'executor').order_by('started_at'),
        url_prefix='/settings/sessions/?tab=shop&task=',
    ))

    # 7. Роли без пользователей
    qs = Role.objects.annotate(uc=Count('users')).filter(uc=0)
    sections.append(_section(
        'role_empty',
        'Роли без пользователей',
        'Мёртвый груз. Можно удалить, если не нужны для истории.',
        'info',
        qs.order_by('name'),
        url_prefix='/settings/roles/',
    ))

    # 8. Подразделения без пользователей
    qs = Department.objects.annotate(uc=Count('users')).filter(uc=0)
    sections.append(_section(
        'dept_empty',
        'Подразделения без пользователей',
        'Пустые отделы. Проверьте, нужны ли они.',
        'info',
        qs.order_by('name'),
        url_prefix='/settings/departments/',
    ))

    # 9. Активные пользователи без роли
    from django.contrib.auth import get_user_model
    User = get_user_model()
    qs = User.objects.filter(is_active=True, role__isnull=True)
    sections.append(_section(
        'user_no_role',
        'Активные пользователи без роли',
        'Не получают доступ к функциям портала, зависимым от роли.',
        'warn',
        qs.order_by('full_name'),
        url_prefix='/settings/users/',
    ))

    # 10. Активные пользователи без подразделения
    qs = User.objects.filter(is_active=True, department__isnull=True)
    sections.append(_section(
        'user_no_dept',
        'Активные пользователи без подразделения',
        'Не попадут в отчёты по отделам.',
        'warn',
        qs.order_by('full_name'),
        url_prefix='/settings/users/',
    ))

    # 11. Задачи с branch, но без order
    qs = Task.objects.filter(
        branch__isnull=False, order__isnull=True,
    )
    sections.append(_section(
        'branch_no_order',
        'Задачи с веткой, но без заказа',
        'Нарушение структуры: ветка не может существовать без заказа.',
        'danger',
        qs.select_related('executor', 'branch').order_by('-created_at'),
    ))

    # 12. Задачи с веткой из другого заказа
    # (проверка через Python — SQL join неудобен)
    mismatched = []
    for t in Task.objects.filter(
            branch__isnull=False, order__isnull=False,
    ).select_related('branch', 'order')[:5000]:
        if t.branch.order_id != t.order_id:
            mismatched.append(t.pk)

    qs = Task.objects.filter(pk__in=mismatched)
    sections.append(_section(
        'branch_wrong_order',
        'Ветка не из того заказа',
        'Задача привязана к ветке чужого заказа. Битые связи.',
        'danger',
        qs.select_related('executor', 'branch', 'order').order_by('-created_at'),
    ))

    # 13. Задачи с parent=self
    qs = Task.objects.filter(parent_id=models_f_expr('id'))
    sections.append(_section(
        'self_parent',
        'Задача — родитель самой себя',
        'Циклическая ссылка. Сломает дерево задач.',
        'danger',
        qs.order_by('pk'),
    ))

    # 14. Пользователи с подозрительным email (без @)
    qs = User.objects.exclude(email__contains='@')
    sections.append(_section(
        'bad_email',
        'Пользователи с некорректным email',
        'Email без символа @. Проверьте карточки.',
        'warn',
        qs.order_by('full_name'),
        url_prefix='/settings/users/',
    ))

    # 15. Роли can_admin без активных админов
    qs = Role.objects.filter(can_admin=True).annotate(
        active_admins=Count(
            'users',
            filter=Q(users__is_active=True),
            distinct=True,
        )
    ).filter(active_admins=0)
    sections.append(_section(
        'role_admin_no_users',
        'Роль-админ без активных пользователей',
        'Роль даёт права администратора, но никто ей не пользуется.',
        'info',
        qs.order_by('name'),
        url_prefix='/settings/roles/',
    ))

    # 16. Задачи без исполнителя (теоретически невозможно, но проверим)
    qs = Task.objects.filter(executor__isnull=True)
    sections.append(_section(
        'task_no_executor',
        'Задачи без исполнителя',
        'Нарушение целостности — поле обязательное.',
        'danger',
        qs.order_by('-created_at'),
    ))

    # Считаем итоги
    total_problems = sum(s['count'] for s in sections if s['severity'] in ('danger', 'warn'))
    total_info = sum(s['count'] for s in sections if s['severity'] == 'info')
    critical_count = sum(s['count'] for s in sections if s['severity'] == 'danger')

    return sections, {
        'total_problems': total_problems,
        'total_info': total_info,
        'critical_count': critical_count,
        'sections_with_problems': sum(1 for s in sections if s['count']),
    }


def models_f_expr(field):
    """Обёртка для F() — импорт по месту, чтобы не тянуть в начало."""
    from django.db.models import F
    return F(field)
