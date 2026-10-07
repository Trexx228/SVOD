import shutil
import time
from pathlib import Path

from django.conf import settings
from django.db import connection


def _db_check():
    info = {'ok': False, 'engine': connection.vendor, 'error': None,
            'size_bytes': 0, 'tables': 0, 'last_write_ago_sec': None}
    try:
        info['engine'] = connection.vendor
        path = Path(connection.settings_dict.get('NAME') or '')
        if path.exists():
            info['size_bytes'] = path.stat().st_size
            info['last_write_ago_sec'] = int(time.time() - path.stat().st_mtime)

        with connection.cursor() as cur:
            if connection.vendor == 'sqlite':
                cur.execute(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                info['tables'] = cur.fetchone()[0]
            else:
                cur.execute("SELECT 1")
        info['ok'] = True
    except Exception as e:
        info['error'] = str(e)
    return info


def _disk_check():
    try:
        total, used, free = shutil.disk_usage(str(settings.BASE_DIR))
        return {'ok': free > 500 * 1024 * 1024,
                'total': total, 'used': used, 'free': free}
    except Exception as e:
        return {'ok': False, 'total': 0, 'used': 0, 'free': 0, 'error': str(e)}


def _backups_check():
    from . import backups as bk
    items = bk.list_backups()
    if not items:
        return {'ok': False, 'count': 0, 'latest': None,
                'latest_age_hours': None, 'total_bytes': 0}
    latest = items[0]
    age_hours = (time.time() - latest['mtime']) / 3600
    return {
        'ok': age_hours < 48,
        'count': len(items),
        'latest': latest['name'],
        'latest_age_hours': round(age_hours, 1),
        'total_bytes': sum(i['size'] for i in items),
    }


def _admins_check():
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    User = get_user_model()
    cnt = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__can_admin=True)
    ).count()
    return {'ok': cnt >= 2, 'count': cnt}


def _alerts_check():
    from .models import AdminAlert
    unseen = AdminAlert.objects.filter(seen=False).count()
    danger = AdminAlert.objects.filter(
        seen=False, severity=AdminAlert.Severity.DANGER
    ).count()
    return {'ok': unseen == 0, 'unseen': unseen, 'danger': danger}


def _norm_check():
    from core.models import Norm
    n = Norm.objects.order_by('-id').first()
    return {'ok': n is not None,
            'hours_per_day': n.hours_per_day if n else None}


def _ad_check():
    enabled = bool(getattr(settings, 'AD_ENABLED', False))
    return {
        'ok': True,
        'enabled': enabled,
        'server': getattr(settings, 'AD_SERVER', '') or '',
        'domain': getattr(settings, 'AD_DOMAIN', '') or '',
    }


def collect_health():
    """Возвращает словарь со всеми проверками + общий статус."""
    checks = {
        'db': _db_check(),
        'disk': _disk_check(),
        'backups': _backups_check(),
        'admins': _admins_check(),
        'alerts': _alerts_check(),
        'norm': _norm_check(),
        'ad': _ad_check(),
    }

    # Общий статус
    critical = ['db', 'disk']
    ok = all(checks[k].get('ok') for k in critical)
    warnings = [name for name, c in checks.items() if not c.get('ok')]
    # warnings содержат также critical
    warnings = [w for w in warnings if w not in critical]

    return {
        'ok': ok and not warnings,
        'critical_ok': ok,
        'warnings': warnings,
        'checks': checks,
        'timestamp': int(time.time()),
    }
