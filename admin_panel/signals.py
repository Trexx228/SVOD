from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (
    user_logged_in,
    user_login_failed,
    user_logged_out,
)
from django.dispatch import receiver
from django.utils import timezone

from .alerts import raise_alert
from .models import AdminAlert, LoginEvent

User = get_user_model()


def _client_ip(request):
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _ua(request):
    if not request:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:300]


def _check_failed_logins_alert(email, ip):
    from .alerts import get_alert_settings

    cfg = get_alert_settings()
    threshold = cfg.failed_logins_threshold if cfg else 5
    window_min = cfg.failed_logins_window_min if cfg else 15

    window_start = timezone.now() - timedelta(minutes=window_min)

    if ip:
        cnt = LoginEvent.objects.filter(
            success=False, ip=ip, created_at__gte=window_start,
        ).count()
        if cnt >= threshold:
            raise_alert(
                kind=AdminAlert.Kind.FAILED_LOGINS,
                key=f'failed_login_ip:{ip}',
                severity=AdminAlert.Severity.WARN,
                title=f'Серия отказов входа с IP {ip}',
                message=f'{cnt} неудачных попыток за {window_min} минут.',
                url=f'/settings/logins/?q={ip}&result=failed',
            )

    if email:
        cnt_e = LoginEvent.objects.filter(
            success=False, email_attempted__iexact=email,
            created_at__gte=window_start,
        ).count()
        if cnt_e >= threshold:
            raise_alert(
                kind=AdminAlert.Kind.FAILED_LOGINS,
                key=f'failed_login_email:{email.lower()}',
                severity=AdminAlert.Severity.WARN,
                title=f'Серия отказов входа для «{email}»',
                message=f'{cnt_e} неудачных попыток за {window_min} минут.',
                url=f'/settings/logins/?q={email}&result=failed',
            )


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    try:
        LoginEvent.objects.create(
            user=user,
            email_attempted=user.email,
            success=True,
            ip=_client_ip(request),
            user_agent=_ua(request),
        )
    except Exception:
        pass


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    try:
        email = ''
        if credentials:
            email = (credentials.get('username') or credentials.get('email') or '')[:254]
        ip = _client_ip(request)

        # Пытаемся связать с существующим пользователем — иначе
        # в профиле (и в фильтрах админки) отказы не видны.
        # На момент сигнала user ещё неизвестен, но email мы знаем.
        user = None
        if email:
            user = User.objects.filter(email__iexact=email).first()

        LoginEvent.objects.create(
            user=user,
            email_attempted=email,
            success=False,
            ip=ip,
            user_agent=_ua(request),
        )
        _check_failed_logins_alert(email, ip)
    except Exception:
        pass


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    pass
