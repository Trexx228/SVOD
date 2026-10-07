import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.contrib.auth import get_user_model


logger = logging.getLogger(__name__)
User = get_user_model()


def resolve_recipients(target, department_id=None, role_id=None):
    """Возвращает queryset активных пользователей с непустым email."""
    qs = User.objects.filter(is_active=True).exclude(email='')

    if target == 'all':
        return qs
    if target == 'department' and department_id:
        return qs.filter(department_id=department_id)
    if target == 'role' and role_id:
        return qs.filter(role_id=role_id)
    if target == 'admins':
        return qs.filter(Q(is_superuser=True) | Q(role__can_admin=True))
    return qs.none()


def send_broadcast(subject, body, recipients, sender=None):
    """Рассылка. Возвращает (sent, failed)."""
    sent = 0
    failed = 0
    from_email = settings.DEFAULT_FROM_EMAIL

    for u in recipients:
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=from_email,
                recipient_list=[u.email],
                fail_silently=False,
            )
            sent += 1
        except Exception as e:
            logger.warning('Broadcast send failed for %s: %s', u.email, e)
            failed += 1

    return sent, failed
