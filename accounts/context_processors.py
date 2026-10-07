"""Контекст-процессор обучалки и онбординга."""
from .onboarding import get_progress, should_show_offer


# Страницы, у которых есть тур. id тура = url_name.
TOUR_PAGES = {
    # Сотрудник
    'dashboard', 'task_create', 'task_detail', 'task_tree',
    'task_timeline', 'task_calendar',

    # Кабинет
    'me_home', 'me_kpi', 'me_settings',

    # Руководитель
    'manager_cabinet', 'manager_team', 'manager_review',
    'manager_flow', 'manager_analytics', 'manager_orders',
    'order_plan', 'manager_plant', 'manager_plant_live',
    'manager_online', 'manager_task_logs',

    # Общение
    'dialogs', 'notifications', 'rating', 'motivation',

    # Админка
    'settings_home', 'settings_users', 'settings_roles',
    'settings_backups', 'settings_health', 'settings_maintenance',
    'settings_integrity', 'settings_audit', 'settings_alerts',
}

# Туры, которые стартуют сами при первом заходе.
AUTOSTART_TOURS = {'dashboard'}


def _is_new_user(user):
    """Зарегистрирован меньше 7 дней назад."""
    from django.utils import timezone
    from datetime import timedelta
    if not user.date_joined:
        return False
    return (timezone.now() - user.date_joined) < timedelta(days=7)


def tours(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}

    url_name = ''
    if getattr(request, 'resolver_match', None):
        url_name = request.resolver_match.url_name or ''

    tour_id = url_name if url_name in TOUR_PAGES else None
    seen = user.tour_seen or {}
    pending = bool(tour_id) and tour_id not in seen
    autostart = pending and tour_id in AUTOSTART_TOURS

    show_offer = should_show_offer(user)
    done, total, next_step, is_done = get_progress(user)

    return {
        'tour_id': tour_id,
        'tour_pending': pending,
        'tour_autostart': autostart,
        'tour_seen': seen,

        'onboarding_offer': show_offer,
        'onboarding_done_count': done,
        'onboarding_total': total,
        'onboarding_next': next_step,
        'onboarding_is_done': is_done,
        # ── Hints ──
        'hints_seen': 'hints_home' in seen,
        'hints_is_new': _is_new_user(user),
        'hints_onboarding_done': user.onboarding_done,
    }
