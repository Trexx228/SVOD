"""
Тест подключения к AD. Запуск: python scripts/check_ad.py

⚠️  Никаких кредов в коде. Всё — из переменных окружения:
        AD_TEST_USER     — логин (без домена), например "svc-ad-test"
        AD_TEST_PASSWORD — пароль

Переменные задавайте локально в шелле / .env-файле вне репозитория,
либо в защищённых переменных CI.
"""
import os
import sys
from pathlib import Path

# Скрипт лежит в scripts/, а manage.py — в корне.
# Добавляем корень в sys.path, чтобы Django увидел config.settings.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402
from ldap3 import Server, Connection, NTLM, ALL, SUBTREE  # noqa: E402


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f'✗ Переменная окружения {name} не задана — выходим.')
        sys.exit(2)
    return value


def main() -> int:
    test_user = _require_env('AD_TEST_USER')
    test_password = _require_env('AD_TEST_PASSWORD')

    server = Server(settings.AD_SERVER, get_info=ALL)
    print(f'Connecting to {settings.AD_SERVER}...')

    try:
        conn = Connection(
            server,
            user=f'{settings.AD_DOMAIN}\\{test_user}',
            password=test_password,
            authentication=NTLM,
            auto_bind=True,
        )
        print('✓ Bind successful')

        conn.search(
            search_base=settings.AD_USER_BASE,
            search_filter=f'(sAMAccountName={test_user})',
            search_scope=SUBTREE,
            attributes=['displayName', 'mail', 'memberOf'],
        )
        if conn.entries:
            print(f'✓ User found: {conn.entries[0].displayName}')
            print(f'  Email: {conn.entries[0].mail}')
            print(f'  Groups: {list(conn.entries[0].memberOf)}')
        else:
            print('✗ User not found')

        conn.unbind()
        return 0
    except Exception as e:
        print(f'✗ Error: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
