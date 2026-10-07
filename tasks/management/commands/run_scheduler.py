"""Планировщик фоновых задач СВОД.

Запускается как отдельный процесс:

    python manage.py run_scheduler

Интервальные задачи (каждые N секунд):
    - escalate_shift_requests — эскалация заявок на сдвиг плана (10 мин)
    - notify_overdue          — уведомления о просрочках (30 мин)
    - compute_idle_since      — пересчёт простоя (5 мин)

Задачи по расписанию (раз в сутки):
    - cleanup_old_records     — очистка старых записей в 3:00
    - load_ru_holidays        — праздники РФ 1 и 15 числа в 3:30
    - send_daily_digest       — утренний дайджест в 9:00
    - capture_snapshot        — снимок задач в 23:55

Состояние расписанных задач (дата последнего запуска) хранится в
logs/scheduler_state.json — чтобы при перезапуске процесса задачи
не выполнялись повторно.

Останавливается по Ctrl+C или SIGTERM.
"""
import json
import logging
import signal
import time
from datetime import date as _date, time as dtime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


logger = logging.getLogger(__name__)


# ─── Интервальные задачи (каждые N секунд) ────────────────────────────────
TASKS = [
    {
        'name': 'escalate_shift_requests',
        'interval': 10 * 60,      # каждые 10 минут
        'last_run': 0,
    },
    {
        'name': 'notify_overdue',
        'interval': 30 * 60,      # каждые 30 минут
        'last_run': 0,
    },
    {
        'name': 'compute_idle_since',
        'interval': 5 * 60,       # каждые 5 минут
        'last_run': 0,
    },
]

# ─── Задачи по расписанию (раз в сутки) ───────────────────────────────────
SCHEDULED = [
    {
        'name': 'cleanup_old_records',
        'at': dtime(3, 0),
        'last_date': None,
    },
    {
        # Праздники РФ: 1 и 15 числа в 3:30. Грузим текущий + следующий год.
        # Следующий год — заранее, чтобы к январю данные уже были в БД.
        'name': 'load_ru_holidays',
        'at': dtime(3, 30),
        'last_date': None,
        'run_only_days': (1, 15),
        'load_two_years': True,
    },
    {
        'name': 'notify_order_deadlines',
        'at': dtime(9, 5),
        'last_date': None,
    },
    {
        'name': 'send_daily_digest',
        'at': dtime(9, 0),
        'last_date': None,
    },
    {
        'name': 'capture_daily_metrics',
        'at': dtime(23, 50),
        'last_date': None,
    },
    {
        'name': 'capture_snapshot',
        'at': dtime(23, 55),
        'last_date': None,
    },
]


STATE_FILE = Path(settings.BASE_DIR) / 'logs' / 'scheduler_state.json'


class Command(BaseCommand):
    help = 'Планировщик фоновых задач (эскалация, дайджест, очистка).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tick', type=int, default=30,
            help='Как часто проверять задачи (в секундах). По умолчанию 30.',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='Выполнить все задачи один раз и выйти (для теста).',
        )

    # ── Сохранение/восстановление состояния ───────────────────────────────
    def _load_state(self):
        """Читает даты последнего запуска SCHEDULED-задач из файла."""
        try:
            if not STATE_FILE.is_file():
                return {}
            with STATE_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception('Не удалось прочитать состояние scheduler')
            return {}

    def _save_state(self):
        """Сохраняет даты последнего запуска SCHEDULED-задач."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                sch['name']: (
                    sch['last_date'].isoformat() if sch['last_date'] else None
                )
                for sch in SCHEDULED
            }
            with STATE_FILE.open('w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception('Не удалось сохранить состояние scheduler')

    def _restore_state(self):
        """Загружает last_date в объекты SCHEDULED."""
        data = self._load_state()
        for sch in SCHEDULED:
            iso = data.get(sch['name'])
            if not iso:
                continue
            try:
                sch['last_date'] = _date.fromisoformat(iso)
            except Exception:
                sch['last_date'] = None

    # ── Основной цикл ─────────────────────────────────────────────────────
    def handle(self, *args, **options):
        tick = options['tick']
        once = options['once']

        running = {'value': True}

        def stop(signum, frame):
            running['value'] = False
            self.stdout.write(self.style.WARNING('\nОстанавливаюсь…'))

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        self._restore_state()

        self.stdout.write(self.style.SUCCESS(
            f'Планировщик запущен. Тик: {tick} с. '
            f'Задач: {len(TASKS)} интервальных + {len(SCHEDULED)} по расписанию.'
        ))

        if once:
            self._run_all()
            self.stdout.write(self.style.SUCCESS('Все задачи выполнены (--once).'))
            return

        while running['value']:
            try:
                self._tick_once()
            except Exception as e:
                logger.exception('scheduler tick failed: %s', e)
                self.stderr.write(self.style.ERROR(f'Ошибка тика: {e}'))

            # Спим маленькими порциями, чтобы быстро выйти по сигналу
            slept = 0
            while slept < tick and running['value']:
                time.sleep(1)
                slept += 1

        self.stdout.write(self.style.SUCCESS('Планировщик остановлен.'))

    def _tick_once(self):
        now_ts = time.time()
        now_dt = timezone.now()

        # 1. Интервальные задачи
        for task in TASKS:
            if now_ts - task['last_run'] >= task['interval']:
                self._run_command(task['name'])
                task['last_run'] = now_ts

        # 2. Задачи по расписанию
        today = timezone.localdate()
        current_time = timezone.localtime(now_dt).time()

        state_changed = False
        for sch in SCHEDULED:
            if sch['last_date'] == today:
                continue

            # Ограничение по дню месяца (например, праздники — 1 и 15)
            only_days = sch.get('run_only_days')
            if only_days and today.day not in only_days:
                continue

            # Время пришло — выполняем
            if current_time >= sch['at']:
                self._run_command(sch['name'], sch=sch)
                sch['last_date'] = today
                state_changed = True

        if state_changed:
            self._save_state()

    def _run_all(self):
        """Прогон всех задач один раз (для теста через --once).

        Здесь не проверяются run_only_days — для отладки всё должно
        выполниться, даже если сегодня не 1 и не 15 число.
        """
        for task in TASKS:
            self._run_command(task['name'])
        for sch in SCHEDULED:
            self._run_command(sch['name'], sch=sch)

    # ── Запуск одной команды ──────────────────────────────────────────────
    def _run_command(self, name, sch=None):
        """Запустить management-команду.

        sch: описание задачи из SCHEDULED (dict) или None для TASKS.

        Особый случай: load_ru_holidays с load_two_years=True —
        вызывается дважды (текущий + следующий год).
        """
        ts = timezone.now().strftime('%d.%m %H:%M:%S')
        sch = sch or {}

        if name == 'load_ru_holidays' and sch.get('load_two_years'):
            self._run_holidays_two_years(ts)
            return

        try:
            call_command(name, verbosity=0)
            self.stdout.write(f'[{ts}] ✓ {name}')
            logger.info('scheduler: %s OK', name)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'[{ts}] ✗ {name}: {e}'))
            logger.exception('scheduler: %s failed', name)

    def _run_holidays_two_years(self, ts):
        """Загрузить праздники РФ на текущий и следующий год.

        Каждый год — отдельный вызов. Приоритет:
          1. Встроенный список (builtin) — есть данные на 2025 и 2026.
          2. xmlcalendar.ru — нужен интернет, но есть более свежие годы.
        Если для года нет данных ни там, ни там — пишем warning и идём
        дальше. Планировщик не должен падать из-за одного года.
        """
        this_year = _date.today().year

        for year in (this_year, this_year + 1):
            # 1) Встроенный список
            try:
                call_command(
                    'load_ru_holidays',
                    year=year,
                    source='builtin',
                    verbosity=0,
                )
                self.stdout.write(
                    f'[{ts}] ✓ load_ru_holidays ({year}, builtin)'
                )
                logger.info(
                    'scheduler: load_ru_holidays %s (builtin) OK', year,
                )
                continue
            except Exception as e:
                logger.warning(
                    'scheduler: load_ru_holidays %s (builtin) failed: %s',
                    year, e,
                )

            # 2) Fallback: xmlcalendar.ru
            try:
                call_command(
                    'load_ru_holidays',
                    year=year,
                    source='xmlcalendar',
                    verbosity=0,
                )
                self.stdout.write(
                    f'[{ts}] ✓ load_ru_holidays ({year}, xmlcalendar)'
                )
                logger.info(
                    'scheduler: load_ru_holidays %s (xmlcalendar) OK', year,
                )
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f'[{ts}] ⚠ load_ru_holidays ({year}) — нет данных: {e}'
                ))
                logger.warning(
                    'scheduler: load_ru_holidays %s — no data: %s', year, e,
                )
