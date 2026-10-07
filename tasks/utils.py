import math
import re
from datetime import datetime, time as dtime, timedelta

from django.core.cache import cache
from django.utils import timezone

UNIT_LABELS = {'auto': '', 'min': 'мин', 'h': 'ч', 'd': 'чел-дн'}


def valid_unit(value):
    return value if value in UNIT_LABELS else 'auto'


def norm_hours():
    val = cache.get('norm_hours_per_day')
    if val is None:
        from core.models import Norm
        n = Norm.objects.first()
        val = n.hours_per_day if n and n.hours_per_day else 8.0
        cache.set('norm_hours_per_day', val, timeout=3600)  # Кэш на 1 час
    return val


def norm_hours_for(user):
    """Норма часов в день для конкретного пользователя.

    Приоритет:
      1. Department.hours_per_day — если задано для отдела пользователя.
      2. Глобальная Norm.hours_per_day.
      3. 8.0 — если ничего не задано.

    Глобальная норма кэшируется (см. norm_hours), а override отдела
    читается из БД — это быстро и не создаёт N+1, если department
    подгружен через select_related.
    """
    if user is not None and user.department_id:
        dept_val = user.department.hours_per_day
        if dept_val and dept_val > 0:
            return float(dept_val)
    return norm_hours()


def kpi_percent(plan, fact, min_fact=1.0):
    """KPI = план/факт в %. Ниже min_fact часов факта — недостоверно."""
    if not plan or not fact or fact < min_fact:
        return None
    return round(plan / fact * 100)


def parse_plan(text, scale='s', norm=8.0):
    """Время на задачу с учётом масштаба."""
    text = (text or '').strip().lower().replace(',', '.')
    if not text:
        return None

    # 1:15 -> 1 ч 15 мин
    m = re.match(r'^(\d+):(\d{1,2})$', text)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60

    # 30 мин
    m = re.match(r'^([\d.]+)\sмин$', text)
    if m:
        return float(m.group(1)) / 60

    # 2 ч / 2 ч 30 мин
    m = re.match(r'^([\d.]+)\sч\s*(?:([\d.]+)\sмин)?$', text)
    if m:
        return float(m.group(1)) + (float(m.group(2)) / 60 if m.group(2) else 0)

    # 3 д / 3 д 4 ч
    m = re.match(r'^([\d.]+)\sд\s*(?:([\d.]+)\s*ч)?$', text)
    if m:
        return float(m.group(1)) * norm + (float(m.group(2)) if m.group(2) else 0)

    try:
        value = float(text)
    except ValueError:
        return None

    factor = {
        'xs': 1 / 60,
        's': 1.0,
        'm': norm,
        'l': norm * 5,
        'xl': norm * 21,
    }.get(scale, 1.0)

    return value * factor


def format_spent(hours):
    if not hours or hours <= 0:
        return '0 мин'

    total_min = math.ceil(hours * 60 - 1e-9)
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)

    if d:
        return f'{d} д {h} ч {m} мин'
    if h:
        return f'{h} ч {m} мин'
    return f'{m} мин'


def format_hours(hours, unit='auto', norm=None):
    if hours is None:
        return ''

    label = UNIT_LABELS.get(unit, '')

    if not hours or hours <= 0:
        return f'0 {label}'.strip() if label else '0 мин'

    if unit == 'min':
        return f'{math.ceil(hours * 60 - 1e-9)} мин'

    if unit == 'h':
        return f'{hours:.2f}'.replace('.', ',') + ' ч'

    if unit == 'd':
        if norm is None:
            norm = norm_hours()
        return f'{(hours / norm):.2f}'.replace('.', ',') + ' чел-дн'

    return format_spent(hours)


def format_overdue(task):
    from .models import Task

    if not task.due or task.status in (Task.Status.DONE, Task.Status.CANCELLED):
        return ''

    deadline = timezone.make_aware(datetime.combine(task.due, dtime(23, 59)))
    now = timezone.now()

    if now <= deadline:
        return ''

    return format_spent((now - deadline).total_seconds() / 3600)


# ─────────────────────────────────────────────────────────────
#  Рабочие дни (производственный календарь)
#
#  Приоритет источников:
#    1. Модель core.Holiday — точные переносы и праздники из БД
#    2. Библиотека `holidays` — фиксированные гос. праздники по региону
#    3. Будний день (пн–пт)
# ─────────────────────────────────────────────────────────────

def _holiday_map():
    """Словарь {date: {'is_working': bool, 'name': str, 'source': str}}.

    Кешируется на 1 час. Инвалидируется сигналом при изменении Holiday.
    """
    cached = cache.get('svod_holiday_map')
    if cached is not None:
        return cached

    from core.models import Holiday
    result = {
        h.date: {
            'is_working': h.is_working,
            'name': h.name or '',
            'source': 'transfer' if h.is_working else 'holiday',
        }
        for h in Holiday.objects.all()
    }
    cache.set('svod_holiday_map', result, timeout=3600)
    return result


def day_info(d, region_code='RU', schedule=None):
    """Полная информация о дне: рабочий ли, почему, как называется.

    Приоритет источников:
      1. Если задан schedule (WorkSchedule) — используется его паттерн,
         праздники и переносы игнорируются. Это осознанное решение:
         сменный режим (2/2, 7/7) существует как раз потому, что
         «пн–пт и 1 января — выходной» к нему не применяется.
      2. Оверрайд из БД (core.Holiday) — переносы и уточнения.
      3. Библиотека `holidays` — фиксированные гос. праздники.
      4. Обычный день недели (пн–пт).

    Возвращает {'is_working': bool, 'name': str, 'source': str}.
    source: 'schedule' | 'workday' | 'weekend' | 'holiday' | 'transfer'
    """
    # 1. Задан график — работаем от его паттерна.
    if schedule is not None:
        # Паттерн пустой или весь из нулей — граф нерабочий,
        # fallback на старую логику, чтобы не зацикливаться в next_workday.
        pattern = schedule.pattern or []
        if pattern and any(pattern):
            # Если включён производственный календарь — праздники
            # и переносы переопределяют паттерн (важнее графика):
            #   - праздник (в БД или гос. календаре) → выходной;
            #   - перенос (в БД как is_working=True) → рабочий.
            if getattr(schedule, 'use_calendar', False):
                override = _holiday_map().get(d)
                if override is not None:
                    return override

                try:
                    import holidays
                    country_holidays = holidays.country_holidays(
                        region_code, years=d.year,
                    )
                    if d in country_holidays:
                        return {
                            'is_working': False,
                            'name': str(country_holidays[d]),
                            'source': 'holiday',
                        }
                except Exception:
                    pass

            working = schedule.is_working_on(d)
            return {
                'is_working': working,
                'name': schedule.name if working else f'{schedule.name} · выходной',
                'source': 'schedule',
            }
        # Паттерн пустой/нулевой → fallback на старую логику.

    # 2. Оверрайд из БД (переносы + уточнения)
    info = _holiday_map().get(d)
    if info is not None:
        return info

    # 3. Библиотека праздников по региону
    try:
        import holidays
        country_holidays = holidays.country_holidays(region_code, years=d.year)
        if d in country_holidays:
            return {
                'is_working': False,
                'name': str(country_holidays[d]),
                'source': 'holiday',
            }
    except Exception:
        pass

    # 4. Обычный день недели
    if d.weekday() >= 5:
        return {'is_working': False, 'name': 'Выходной', 'source': 'weekend'}
    return {'is_working': True, 'name': 'Рабочий день', 'source': 'workday'}


def is_workday(d, region_code='RU', schedule=None):
    """Рабочий ли день.

    Приоритет: schedule → переносы (core.Holiday) → гос. праздники → пн–пт.
    """
    return day_info(d, region_code, schedule)['is_working']


def next_workday(d, region_code='RU', schedule=None):
    """Ближайший рабочий день (включая сам d, если он рабочий)."""
    while not is_workday(d, region_code, schedule):
        d = d + timedelta(days=1)
    return d


def add_work_days(start, days, region_code='RU', schedule=None):
    """Через N рабочих дней от start.

    days=0 → ближайший рабочий.
    days=1 → первый рабочий после start.
    """
    if not start:
        return None

    if days <= 0:
        return next_workday(start, region_code, schedule)

    d = start
    added = 0
    while added < days:
        d = d + timedelta(days=1)
        if is_workday(d, region_code, schedule):
            added += 1
    return d


def work_days_between(start, end, region_code='RU', schedule=None):
    """Сколько рабочих дней между start и end (включительно).

    Если end < start → отрицательное число.
    """
    if not start or not end:
        return None

    if end < start:
        return -work_days_between(end, start, region_code, schedule)

    days = 0
    d = start
    while d <= end:
        if is_workday(d, region_code, schedule):
            days += 1
        d = d + timedelta(days=1)
    return days


def work_days_options(start, max_days=200, region_code='RU', schedule=None):
    """Варианты: [(work_days, end_date), ...]."""
    if not start:
        return []
    return [
        (n, add_work_days(start, n - 1, region_code, schedule))
        for n in range(1, max_days + 1)
    ]

# ─────────────────────────────────────────────────────────────
#  График работы сотрудника (WorkSchedule)
# ─────────────────────────────────────────────────────────────

def schedule_for_user(user):
    """Определить эффективный график работы сотрудника.

    Приоритет:
      1. user.schedule — личный, если задан и активен.
      2. user.department.default_schedule — график отдела, если активен.
      3. None — старая логика (пн–пт + праздники РФ).

    Что считать рабочим днём в выбранном графике — решает
    WorkSchedule.use_calendar: если True, праздники и переносы
    переопределяют паттерн; если False, работает только паттерн.
    """
    if user is None:
        return None

    s = getattr(user, 'schedule', None)
    if s is not None and s.is_active:
        return s

    dept = getattr(user, 'department', None)
    if dept is not None:
        s = getattr(dept, 'default_schedule', None)
        if s is not None and s.is_active:
            return s

    return None
