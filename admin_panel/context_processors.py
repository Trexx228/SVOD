def admin_alerts(request):
    """Счётчики для бейджей в шапке админки."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    is_admin = user.is_superuser or getattr(user, 'is_admin_role', False)
    if not is_admin:
        return {}

    ctx = {'unseen_alerts_count': 0, 'open_tickets_count': 0}

    try:
        from .models import AdminAlert
        ctx['unseen_alerts_count'] = AdminAlert.objects.filter(seen=False).count()
    except Exception:
        pass

    try:
        from accounts.models import SupportTicket
        ctx['open_tickets_count'] = SupportTicket.objects.filter(done=False).count()
    except Exception:
        pass

    return ctx


def shift_requests_badge(request):
    """Счётчик активных заявок, где пользователь — согласующий."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}

    try:
        from tasks.models import PlanShiftRequest
        cnt = PlanShiftRequest.objects.filter(
            recipients=user,
            status=PlanShiftRequest.Status.PENDING,
        ).count()
    except Exception:
        cnt = 0

    return {'my_shift_requests_count': cnt}
