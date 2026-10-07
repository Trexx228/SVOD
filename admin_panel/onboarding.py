from django.conf import settings
from django.db.models import Q
from django.contrib.auth import get_user_model

from accounts.models import Department, Role
from core.models import TaskType, Norm

User = get_user_model()


def _check_admins():
    cnt = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__can_admin=True)
    ).count()
    return {
        'key': 'admins',
        'title': 'Назначить резервного администратора',
        'hint': 'Минимум 2 активных админа — страховка от блокировки.',
        'done': cnt >= 2,
        'url': '/settings/users/',
        'url_text': 'Пользователи',
        'current': f'{cnt} админ(а)',
    }


def _check_departments():
    cnt = Department.objects.count()
    return {
        'key': 'departments',
        'title': 'Создать подразделения',
        'hint': 'Хотя бы одно подразделение — база для распределения задач.',
        'done': cnt >= 1,
        'url': '/settings/departments/new/',
        'url_text': 'Добавить',
        'current': f'{cnt}',
    }


def _check_roles():
    required = {'staff', 'manager', 'admin'}
    have = set(Role.objects.filter(code__in=required).values_list('code', flat=True))
    missing = required - have
    return {
        'key': 'roles',
        'title': 'Завести базовые роли',
        'hint': 'staff, manager, admin — используются в правах доступа.',
        'done': not missing,
        'url': '/settings/roles/',
        'url_text': 'Роли',
        'current': (f'нет: {", ".join(sorted(missing))}' if missing else 'все на месте'),
    }


def _check_task_types():
    cnt = TaskType.objects.filter(is_active=True).count()
    return {
        'key': 'task_types',
        'title': 'Добавить типовые задачи',
        'hint': 'Шаблоны ускоряют постановку и нормирование.',
        'done': cnt >= 1,
        'url': '/settings/task-types/new/',
        'url_text': 'Добавить',
        'current': f'{cnt} активных',
    }


def _check_norm():
    n = Norm.objects.order_by('-id').first()
    return {
        'key': 'norm',
        'title': 'Задать норму часов в день',
        'hint': 'Используется в KPI, отчётах и расчёте загрузки.',
        'done': n is not None,
        'url': '/settings/norms/new/',
        'url_text': 'Задать',
        'current': (f'{n.hours_per_day} ч/день' if n else 'не задана'),
    }


def _check_backup():
    try:
        from . import backups as bk
        items = bk.list_backups()
    except Exception:
        items = []
    return {
        'key': 'backup',
        'title': 'Сделать первый бэкап',
        'hint': 'Резервная копия БД — обязательный минимум.',
        'done': len(items) >= 1,
        'url': '/settings/backups/',
        'url_text': 'Бэкапы',
        'current': f'{len(items)} файл(ов)',
    }


def _check_activity():
    """Заходил ли кто-то кроме админа."""
    from admin_panel.models import LoginEvent
    cnt = LoginEvent.objects.filter(success=True).count()
    return {
        'key': 'activity',
        'title': 'Пригласить первых пользователей',
        'hint': 'Портал работает, когда в нём есть люди.',
        'done': cnt >= 3,
        'url': '/settings/users/new/',
        'url_text': 'Добавить',
        'current': f'{cnt} успешных вход(ов)',
    }


def _check_ad():
    enabled = bool(getattr(settings, 'AD_ENABLED', False))
    return {
        'key': 'ad',
        'title': 'Настроить Active Directory (опционально)',
        'hint': 'Автоматическая аутентификация и синхронизация ролей из групп AD.',
        'done': enabled,   # опционально — не блокирует прогресс
        'optional': True,
        'url': '/settings/system/',
        'url_text': 'Настройки',
        'current': ('включено' if enabled else 'выключено'),
    }


def collect_onboarding():
    """Возвращает (checks, progress). progress = {'done': n, 'total': m, 'percent': p}."""
    checks = [
        _check_admins(),
        _check_departments(),
        _check_roles(),
        _check_task_types(),
        _check_norm(),
        _check_backup(),
        _check_activity(),
        _check_ad(),
    ]

    # Опциональные не считаем в total
    required = [c for c in checks if not c.get('optional')]
    done = sum(1 for c in required if c['done'])
    total = len(required)

    return checks, {
        'done': done,
        'total': total,
        'percent': int(done * 100 / total) if total else 100,
        'all_done': done == total,
    }
