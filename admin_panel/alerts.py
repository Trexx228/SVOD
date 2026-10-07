import logging

from django.core.mail import send_mail
from django.conf import settings as dj_settings
from django.utils import timezone

from .models import AdminAlert

logger = logging.getLogger(__name__)


def get_alert_settings():
    """Ленивая обёртка для singleton-настроек."""
    from .models import AdminAlertSettings
    try:
        return AdminAlertSettings.get_solo()
    except Exception:
        return None


def raise_alert(kind, key, title, message='', url='',
                severity=AdminAlert.Severity.WARN, send_email=True):
    """Создать или обновить алерт. Опционально — отправить email."""
    try:
        obj, created = AdminAlert.objects.get_or_create(
            key=key,
            defaults={
                'kind': kind,
                'severity': severity,
                'title': title,
                'message': message,
                'url': url,
                'count': 1,
                'seen': False,
            },
        )
        if not created:
            obj.kind = kind
            obj.severity = severity
            obj.title = title
            obj.message = message
            obj.url = url
            obj.count += 1
            obj.seen = False
            obj.last_seen_at = timezone.now()
            obj.save()

        if send_email and (created or obj.count in (5, 10, 25, 50, 100)):
            # письмо шлём не каждый раз, чтобы не спамить
            _send_email_if_configured(obj)

        return obj
    except Exception:
        logger.exception('raise_alert failed for key=%s', key)
        return None


def _send_email_if_configured(alert):
    cfg = get_alert_settings()
    if not cfg or not cfg.notify_email:
        return

    if cfg.notify_on_danger_only and alert.severity != AdminAlert.Severity.DANGER:
        return

    subject = f'[СВОД] {alert.title}'
    body = (
        f'{alert.message}\n\n'
        f'Серьёзность: {alert.get_severity_display()}\n'
        f'Срабатываний: {alert.count}\n'
        f'Последнее: {alert.last_seen_at:%d.%m.%Y %H:%M}\n'
    )
    if alert.url:
        base = getattr(dj_settings, 'SITE_URL', '') or ''
        body += f'\nОткрыть: {base}{alert.url}\n'

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=dj_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[cfg.notify_email],
            fail_silently=True,
        )
    except Exception:
        logger.exception('Failed to send admin alert email')


def resolve_alert(key):
    AdminAlert.objects.filter(key=key, seen=False).update(seen=True)
