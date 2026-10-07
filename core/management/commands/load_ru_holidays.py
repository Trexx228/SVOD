"""
Загрузка производственного календаря РФ.

Два режима:
  1. --source=builtin  — встроенный список (2025–2026, актуально на 01.10.2025)
  2. --source=xmlcalendar — попытка загрузить с http://xmlcalendar.ru/
                            (нужен пакет requests или urllib)

Пример:
    python manage.py load_ru_holidays --year=2026
    python manage.py load_ru_holidays --year=2026 --source=xmlcalendar
"""
import json
import urllib.request
import urllib.error
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Holiday


# ─────────────────────────────────────────────────────────────
# Встроенный список: официальные праздники + переносы
# Источник: ТК РФ ст. 112, Постановление Правительства РФ
# от 24.09.2025 № 1466 (для 2026) и от 04.10.2024 № 1335 (для 2025)
# ─────────────────────────────────────────────────────────────
# Только переносы и «нестандартные» дни.
# Фиксированные праздники (1–8 янв, 23.02, 08.03, 01.05, 09.05, 12.06, 04.11)
# знает библиотека `holidays`, их сюда писать НЕ нужно.
BUILTIN = {
    2025: [
        (date(2025, 5, 2),  'Перенос с 4 января (сб)'),
        (date(2025, 11, 3), 'Перенос с 5 января (вс)'),
    ],
    2026: [
        (date(2026, 1, 9),  'Перенос с 3 января (сб)'),
        (date(2026, 3, 9),  'Перенос с 8 марта (вс)'),
        (date(2026, 5, 11), 'Перенос с 9 мая (сб)'),
        (date(2026, 6, 14), 'Перенос с 12 июня (пт)'),
        (date(2026, 12, 31), 'Перенос с 4 января (вс)'),
    ],
}


class Command(BaseCommand):
    help = 'Загружает производственный календарь РФ (праздники и переносы)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--year', type=int, default=date.today().year,
            help='Год (по умолчанию — текущий).',
        )
        parser.add_argument(
            '--source', choices=['builtin', 'xmlcalendar'],
            default='builtin',
            help='Источник: встроенный список или xmlcalendar.ru',
        )
        parser.add_argument(
            '--clear', action='store_true',
            help='Удалить все существующие записи перед загрузкой.',
        )

    def handle(self, *args, **opts):
        year = opts['year']
        source = opts['source']

        if source == 'builtin':
            records = self._from_builtin(year)
        else:
            records = self._from_xmlcalendar(year)

        if not records:
            raise CommandError(
                f'Нет данных для {year} года. '
                f'Попробуйте --source=builtin или обновите встроенный список.'
            )

        with transaction.atomic():
            if opts['clear']:
                Holiday.objects.all().delete()
                self.stdout.write(self.style.WARNING('Старые записи удалены.'))

            created = updated = 0
            for d, name, is_working in records:
                obj, was_created = Holiday.objects.update_or_create(
                    date=d,
                    defaults={'name': name, 'is_working': is_working},
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Загружено: создано {created}, обновлено {updated}.'
        ))

    # ── Встроенный список ──
    def _from_builtin(self, year):
        raw = BUILTIN.get(year)
        if not raw:
            return []
        return [(d, name, False) for d, name in raw]

    # ── xmlcalendar.ru ──
    def _from_xmlcalendar(self, year):
        """Загрузка с http://xmlcalendar.ru/data/ru/{year}/calendar.json

        Формат JSON:
        {
          "months": [
            {"month": 1, "days": "1,2,3,4,5,6,7,8,9,10,11,..."},
            ...
          ]
        }
        days — строка, где «*» помечены праздничные дни.
        """
        url = f'http://xmlcalendar.ru/data/ru/{year}/calendar.json'
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            raise CommandError(f'Не удалось загрузить {url}: {e}')

        records = []
        for month_data in data.get('months', []):
            month = int(month_data['month'])
            for token in month_data.get('days', '').split(','):
                token = token.strip()
                if not token:
                    continue
                if token.endswith('*'):
                    day = int(token.rstrip('*'))
                    records.append((
                        date(year, month, day),
                        'Праздник (xmlcalendar)',
                        False,
                    ))
        return records
