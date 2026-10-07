from .models import AuditLog


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_action(request, action, obj, changes=None):
    """Единая точка записи аудита. Безопасна: ошибки глотаются."""
    try:
        AuditLog.objects.create(
            actor=getattr(request, 'user', None) if getattr(request.user, 'is_authenticated', False) else None,
            action=action,
            target_model=obj.__class__.__name__,
            target_id=str(getattr(obj, 'pk', '') or ''),
            target_repr=str(obj)[:300],
            changes=changes or None,
            ip=_client_ip(request),
        )
    except Exception:
        pass  # аудит не должен ломать бизнес-операцию
