import re
from pathlib import Path

from django.conf import settings
from django.db import connection


BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'
BACKUP_NAME_RE = re.compile(r'^backup_[0-9]{8}_[0-9]{6}\.sqlite3$')


def ensure_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def is_valid_name(name):
    """Только свои имена файлов. Никаких ../, слешей, чужих расширений."""
    return bool(BACKUP_NAME_RE.match(name or ''))


def make_backup(prefix='backup'):
    """Создаёт консистентную копию текущей БД. Возвращает (name, size_bytes)."""
    from datetime import datetime

    ensure_dir()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    name = f'{prefix}_{timestamp}.sqlite3'
    target = BACKUP_DIR / name

    if connection.vendor != 'sqlite':
        raise RuntimeError(
            'Резервное копирование реализовано только для SQLite. '
            'Для PostgreSQL используйте pg_dump.'
        )

    # VACUUM INTO — консистентный снапшот без блокировки записи.
    # Кавычки экранируем — путь не должен ломать SQL.
    safe_path = str(target).replace("'", "''")
    with connection.cursor() as cur:
        cur.execute(f"VACUUM INTO '{safe_path}'")

    return name, target.stat().st_size


def list_backups():
    """Список файлов: (name, size, mtime). Сортировка по убыванию времени."""
    if not BACKUP_DIR.exists():
        return []
    items = []
    for p in BACKUP_DIR.iterdir():
        if p.is_file() and is_valid_name(p.name):
            st = p.stat()
            items.append({'name': p.name, 'size': st.st_size, 'mtime': st.st_mtime})
    items.sort(key=lambda x: x['mtime'], reverse=True)
    return items


def human_size(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} ТБ'


def backup_path(name):
    """Безопасный путь к бэкапу. None, если имя невалидно или файла нет."""
    if not is_valid_name(name):
        return None
    p = BACKUP_DIR / name
    if not p.is_file():
        return None
    return p


def delete_backup(name):
    p = backup_path(name)
    if p:
        p.unlink()
        return True
    return False
