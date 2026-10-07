"""Автотесты core: Norm, TaskType, Holiday, load_ru_holidays, work_days_*.

Документирует реальное поведение, включая известные ограничения кэша.
"""
from datetime import date

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import Holiday, Norm, TaskType
from tasks.utils import (
    add_work_days, day_info, is_workday, next_workday, norm_hours,
    work_days_between, work_days_options,
)


class CoreBase(TestCase):
    """Кэш Django не откатывается вместе с транзакцией, поэтому
    чистим его между тестами вручную — иначе тесты протекают."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()


# ═════════════════════════════════════════════════════════════
#  Модели
# ═════════════════════════════════════════════════════════════

class TaskTypeModelTests(CoreBase):
    def test_create_defaults(self):
        tt = TaskType.objects.create(name='Чертёж', plan_hours=16)
        self.assertEqual(tt.plan_hours, 16)
        self.assertEqual(tt.due_days, 1)
        self.assertTrue(tt.is_active)

    def test_str(self):
        tt = TaskType.objects.create(name='Чертёж', plan_hours=4)
        self.assertEqual(str(tt), 'Чертёж')

    def test_name_unique(self):
        TaskType.objects.create(name='X', plan_hours=1)
        with self.assertRaises(Exception):
            TaskType.objects.create(name='X', plan_hours=2)

    def test_negative_plan_rejected(self):
        tt = TaskType(name='X', plan_hours=-1)
        with self.assertRaises(ValidationError):
            tt.full_clean()

    def test_zero_plan_ok(self):
        tt = TaskType(name='X', plan_hours=0)
        tt.full_clean()

    def test_due_days_min_one(self):
        tt = TaskType(name='X', plan_hours=1, due_days=0)
        with self.assertRaises(ValidationError):
            tt.full_clean()


class NormModelTests(CoreBase):
    def test_default_hours(self):
        n = Norm.objects.create()
        self.assertEqual(n.hours_per_day, 8.0)

    def test_str(self):
        n = Norm.objects.create(hours_per_day=7.5)
        self.assertIn('7.5', str(n))

    def test_negative_rejected(self):
        n = Norm(hours_per_day=-1)
        with self.assertRaises(ValidationError):
            n.full_clean()

    def test_zero_ok(self):
        n = Norm(hours_per_day=0)
        n.full_clean()


class HolidayModelTests(CoreBase):
    def test_create_default_not_working(self):
        h = Holiday.objects.create(
            date=date(2026, 1, 1), name='Новый год',
        )
        self.assertFalse(h.is_working)

    def test_str(self):
        h = Holiday.objects.create(date=date(2026, 1, 1), name='Новый год')
        self.assertIn('01.01.2026', str(h))
        self.assertIn('Новый год', str(h))

    def test_date_unique(self):
        Holiday.objects.create(date=date(2026, 1, 1))
        with self.assertRaises(Exception):
            Holiday.objects.create(date=date(2026, 1, 1))

    def test_working_holiday(self):
        h = Holiday.objects.create(
            date=date(2026, 1, 10), name='Рабочая суббота',
            is_working=True,
        )
        self.assertTrue(h.is_working)


# ═════════════════════════════════════════════════════════════
#  Сигнал инвалидации кэша
# ═════════════════════════════════════════════════════════════

class HolidayCacheSignalTests(CoreBase):
    def test_cache_cleared_on_create(self):
        cache.set('svod_holiday_map', {'old': 'value'})
        Holiday.objects.create(date=date(2026, 1, 1), name='NY')
        self.assertIsNone(cache.get('svod_holiday_map'))

    def test_cache_cleared_on_update(self):
        h = Holiday.objects.create(date=date(2026, 1, 1), name='NY')
        cache.set('svod_holiday_map', {'old': 'value'})
        h.name = 'Новый год'
        h.save()
        self.assertIsNone(cache.get('svod_holiday_map'))

    def test_cache_cleared_on_delete(self):
        h = Holiday.objects.create(date=date(2026, 1, 1))
        cache.set('svod_holiday_map', {'old': 'value'})
        h.delete()
        self.assertIsNone(cache.get('svod_holiday_map'))


# ═════════════════════════════════════════════════════════════
#  norm_hours — с известными багами
# ═════════════════════════════════════════════════════════════

class NormHoursTests(CoreBase):
    def test_default_8_when_no_norm(self):
        self.assertEqual(norm_hours(), 8.0)

    def test_from_db(self):
        Norm.objects.create(hours_per_day=7.5)
        self.assertEqual(norm_hours(), 7.5)

    def test_cached(self):
        """Второй вызов берёт значение из кэша."""
        Norm.objects.create(hours_per_day=6.0)
        self.assertEqual(norm_hours(), 6.0)
        Norm.objects.all().delete()
        # Кэш всё ещё держит 6.0 → доказывает, что кэш работает
        self.assertEqual(norm_hours(), 6.0)

    def test_zero_norm_treated_as_default(self):
        """⚠ hours_per_day=0 — falsy → трактуется как «не задано» → 8.0.

        Это поведение через `n.hours_per_day if n and n.hours_per_day else 8.0`.
        Тест документирует его, чтобы при рефакторинге случайно не сломать.
        """
        Norm.objects.create(hours_per_day=0)
        self.assertEqual(norm_hours(), 8.0)

    def test_cache_stale_after_save(self):
        """⚠ Известное ограничение: кэш не инвалидируется при save() Norm.

        Изменение через админку вступит в силу только через час
        (или после cache.delete('norm_hours_per_day')).
        """
        n = Norm.objects.create(hours_per_day=6.0)
        self.assertEqual(norm_hours(), 6.0)
        n.hours_per_day = 7.0
        n.save()
        # Всё ещё 6.0 — кэш не инвалидировался
        self.assertEqual(norm_hours(), 6.0)

    def test_manual_cache_delete_refreshes(self):
        """Показать: ручной cache.delete решает проблему stale."""
        Norm.objects.create(hours_per_day=6.0)
        self.assertEqual(norm_hours(), 6.0)

        Norm.objects.all().update(hours_per_day=7.0)
        cache.delete('norm_hours_per_day')

        self.assertEqual(norm_hours(), 7.0)

    def test_negative_via_save_returns_negative(self):
        """🔴 БАГ: negative hours через .save() (без full_clean) утекает в KPI.

        `norm_hours()` берёт `n.hours_per_day` как есть, без проверки
        на отрицательное. Значит `_correct_urgent_shift` поделит на -8
        и получит отрицательные дни.

        Тест документирует этот баг.
        """
        Norm.objects.create(hours_per_day=-1)
        self.assertEqual(norm_hours(), -1.0)

    def test_negative_rejected_by_full_clean(self):
        """Правильный путь: full_clean отклоняет отрицательное."""
        n = Norm(hours_per_day=-1)
        with self.assertRaises(ValidationError):
            n.full_clean()
        # И через form админки нельзя сохранить


# ═════════════════════════════════════════════════════════════
#  day_info — приоритет источников
# ═════════════════════════════════════════════════════════════

class DayInfoTests(CoreBase):
    def test_plain_workday(self):
        # 2026-03-16 — понедельник, обычный рабочий день
        info = day_info(date(2026, 3, 16))
        self.assertTrue(info['is_working'])
        self.assertEqual(info['source'], 'workday')

    def test_saturday(self):
        info = day_info(date(2026, 3, 14))
        self.assertFalse(info['is_working'])
        self.assertEqual(info['source'], 'weekend')

    def test_sunday(self):
        info = day_info(date(2026, 3, 15))
        self.assertFalse(info['is_working'])
        self.assertEqual(info['source'], 'weekend')

    def test_new_year_is_holiday(self):
        """1 января — из библиотеки `holidays`."""
        info = day_info(date(2026, 1, 1))
        self.assertFalse(info['is_working'])
        # Библиотека holidays знает 1 января как праздник
        self.assertEqual(info['source'], 'holiday')

    def test_women_day_prefers_holiday_source(self):
        """8 марта 2026 = воскресенье. Библиотека holidays знает эту дату
        как праздник → source должен быть 'holiday', НЕ 'weekend'.

        Это документирует приоритет: праздник важнее календарного выходного.
        """
        info = day_info(date(2026, 3, 8))
        self.assertFalse(info['is_working'])
        self.assertEqual(info['source'], 'holiday')

    def test_priority_1_db_over_library(self):
        """⚠ Верхний приоритет — БД.

        Если библиотека знает 1 января как праздник, но в БД мы
        помечаем 1 января как рабочий день (is_working=True),
        `day_info` вернёт информацию из БД.
        """
        Holiday.objects.create(
            date=date(2026, 1, 1), name='Рабочий день',
            is_working=True,
        )
        info = day_info(date(2026, 1, 1))
        self.assertTrue(info['is_working'])
        self.assertEqual(info['source'], 'transfer')

    def test_db_marks_weekend_as_working(self):
        Holiday.objects.create(
            date=date(2026, 1, 10), name='Рабочая суббота',
            is_working=True,
        )
        info = day_info(date(2026, 1, 10))
        self.assertTrue(info['is_working'])
        self.assertEqual(info['source'], 'transfer')

    def test_db_marks_weekday_as_holiday(self):
        Holiday.objects.create(
            date=date(2026, 1, 14), name='Корпоратив',
            is_working=False,
        )
        info = day_info(date(2026, 1, 14))
        self.assertFalse(info['is_working'])
        self.assertEqual(info['source'], 'holiday')

    def test_return_keys(self):
        info = day_info(date(2026, 3, 16))
        self.assertIn('is_working', info)
        self.assertIn('name', info)
        self.assertIn('source', info)


class IsWorkdayTests(CoreBase):
    def test_monday(self):
        self.assertTrue(is_workday(date(2026, 3, 16)))

    def test_saturday(self):
        self.assertFalse(is_workday(date(2026, 3, 14)))

    def test_sunday(self):
        self.assertFalse(is_workday(date(2026, 3, 15)))

    def test_new_year(self):
        self.assertFalse(is_workday(date(2026, 1, 1)))

    def test_db_override_working_weekend(self):
        Holiday.objects.create(
            date=date(2026, 3, 14), name='Рабочая суббота',
            is_working=True,
        )
        self.assertTrue(is_workday(date(2026, 3, 14)))


# ═════════════════════════════════════════════════════════════
#  next_workday — границы
# ═════════════════════════════════════════════════════════════

class NextWorkdayTests(CoreBase):
    def test_already_workday_returns_same(self):
        d = date(2026, 3, 16)
        self.assertEqual(next_workday(d), d)

    def test_saturday_to_monday(self):
        self.assertEqual(
            next_workday(date(2026, 3, 14)),
            date(2026, 3, 16),
        )

    def test_sunday_to_monday(self):
        self.assertEqual(
            next_workday(date(2026, 3, 15)),
            date(2026, 3, 16),
        )

    def test_skips_holiday_from_db(self):
        Holiday.objects.create(
            date=date(2026, 3, 16), name='X', is_working=False,
        )
        self.assertEqual(
            next_workday(date(2026, 3, 16)),
            date(2026, 3, 17),
        )

    def test_year_boundary(self):
        """31.12.2026 — пятница. Без записи в БД — рабочий день.

        Если БД помечает 31.12 как нерабочий, next_workday идёт в 2027.
        Здесь мы явно создаём запись, чтобы тест был детерминированным
        и не зависел от того, вызвали ли мы load_ru_holidays в setUp.
        """
        # Явно помечаем 31.12.2026 нерабочим (как это делает BUILTIN)
        Holiday.objects.create(
            date=date(2026, 12, 31), name='Перенос с 4 января',
            is_working=False,
        )
        result = next_workday(date(2026, 12, 31))
        self.assertGreater(result, date(2026, 12, 31))
        self.assertTrue(is_workday(result))
        # Должен быть в 2027
        self.assertEqual(result.year, 2027)

    def test_year_boundary_without_db_record(self):
        """Без записи в БД 31.12.2026 (пятница) — обычный рабочий день."""
        self.assertTrue(is_workday(date(2026, 12, 31)))
        self.assertEqual(
            next_workday(date(2026, 12, 31)),
            date(2026, 12, 31),
        )

    def test_result_is_always_workday(self):
        d = next_workday(date(2026, 1, 5))
        self.assertTrue(is_workday(d))
        self.assertGreaterEqual(d, date(2026, 1, 5))


# ═════════════════════════════════════════════════════════════
#  add_work_days — границы и переходы
# ═════════════════════════════════════════════════════════════

class AddWorkDaysTests(CoreBase):
    def test_zero_from_saturday_returns_monday(self):
        self.assertEqual(
            add_work_days(date(2026, 3, 14), 0),
            date(2026, 3, 16),
        )

    def test_zero_from_workday_returns_same(self):
        self.assertEqual(
            add_work_days(date(2026, 3, 16), 0),
            date(2026, 3, 16),
        )

    def test_one_after_friday(self):
        self.assertEqual(
            add_work_days(date(2026, 3, 13), 1),
            date(2026, 3, 16),
        )

    def test_five_after_monday(self):
        # Пн 16 + 5 раб = Пн 23
        self.assertEqual(
            add_work_days(date(2026, 3, 16), 5),
            date(2026, 3, 23),
        )

    def test_none_returns_none(self):
        self.assertIsNone(add_work_days(None, 5))

    def test_negative_treated_as_zero(self):
        self.assertEqual(
            add_work_days(date(2026, 3, 16), -3),
            date(2026, 3, 16),
        )

    def test_skips_db_holiday(self):
        Holiday.objects.create(
            date=date(2026, 3, 17), name='X', is_working=False,
        )
        # Пн 16 + 1 раб = Ср 18 (Вт 17 пропущен как праздник)
        self.assertEqual(
            add_work_days(date(2026, 3, 16), 1),
            date(2026, 3, 18),
        )

    def test_working_weekend_counted_as_workday(self):
        Holiday.objects.create(
            date=date(2026, 3, 14), name='Рабочая суббота',
            is_working=True,
        )
        # Пт 13 + 1 раб = Сб 14 (считаем рабочей)
        self.assertEqual(
            add_work_days(date(2026, 3, 13), 1),
            date(2026, 3, 14),
        )

    def test_crosses_month_boundary(self):
        """Пн 30.03.2026 + 5 раб. дней = Пн 06.04.2026."""
        self.assertEqual(
            add_work_days(date(2026, 3, 30), 5),
            date(2026, 4, 6),
        )

    def test_crosses_multi_month_with_holidays(self):
        """Длинный перелёт через месяц. Только проверяем, что все
        промежуточные дни рабочие."""
        result = add_work_days(date(2026, 4, 27), 30)
        self.assertGreater(result, date(2026, 4, 27))
        self.assertTrue(is_workday(result))
        # 30 рабочих дней — точно больше месяца
        self.assertGreaterEqual(
            (result - date(2026, 4, 27)).days,
            30,
        )


# ═════════════════════════════════════════════════════════════
#  work_days_between — границы
# ═════════════════════════════════════════════════════════════

class WorkDaysBetweenTests(CoreBase):
    def test_same_workday_is_one(self):
        d = date(2026, 3, 16)
        self.assertEqual(work_days_between(d, d), 1)

    def test_same_weekend_is_zero(self):
        d = date(2026, 3, 14)
        self.assertEqual(work_days_between(d, d), 0)

    def test_mon_to_fri_is_five(self):
        self.assertEqual(
            work_days_between(date(2026, 3, 16), date(2026, 3, 20)),
            5,
        )

    def test_friday_to_monday_is_two(self):
        self.assertEqual(
            work_days_between(date(2026, 3, 13), date(2026, 3, 16)),
            2,
        )

    def test_full_two_weeks_is_ten(self):
        # Пн 16 → Пт 27 (10 рабочих дней)
        self.assertEqual(
            work_days_between(date(2026, 3, 16), date(2026, 3, 27)),
            10,
        )

    def test_reversed_returns_negative(self):
        self.assertEqual(
            work_days_between(date(2026, 3, 20), date(2026, 3, 16)),
            -5,
        )

    def test_none_start(self):
        self.assertIsNone(work_days_between(None, date(2026, 3, 16)))

    def test_none_end(self):
        self.assertIsNone(work_days_between(date(2026, 3, 16), None))

    def test_holiday_excluded(self):
        Holiday.objects.create(
            date=date(2026, 3, 17), name='X', is_working=False,
        )
        # Пн 16 → Ср 18: только Пн и Ср = 2
        self.assertEqual(
            work_days_between(date(2026, 3, 16), date(2026, 3, 18)),
            2,
        )

    def test_working_weekend_included(self):
        Holiday.objects.create(
            date=date(2026, 3, 14), name='Рабочая суббота',
            is_working=True,
        )
        # Пт 13 → Пн 16: Пт + Сб(раб) + Пн = 3
        self.assertEqual(
            work_days_between(date(2026, 3, 13), date(2026, 3, 16)),
            3,
        )

    def test_new_year_holidays_are_zero(self):
        """⚠ 1–8 января — новогодние каникулы.

        Диапазон 01.01.2026 — 08.01.2026 — все дни нерабочие.
        work_days_between должен вернуть 0.
        """
        result = work_days_between(date(2026, 1, 1), date(2026, 1, 8))
        self.assertEqual(result, 0)

    def test_holiday_on_both_bounds_is_zero(self):
        """Если и старт, и финиш — праздники (и больше ничего нет),
        результат 0."""
        Holiday.objects.create(date=date(2026, 3, 16), name='X',
                               is_working=False)
        Holiday.objects.create(date=date(2026, 3, 17), name='Y',
                               is_working=False)
        self.assertEqual(
            work_days_between(date(2026, 3, 16), date(2026, 3, 17)),
            0,
        )


# ═════════════════════════════════════════════════════════════
#  work_days_options
# ═════════════════════════════════════════════════════════════

class WorkDaysOptionsTests(CoreBase):
    def test_start_none_returns_empty(self):
        self.assertEqual(work_days_options(None), [])

    def test_generates_sequence(self):
        opts = work_days_options(date(2026, 3, 16), max_days=5)
        self.assertEqual(len(opts), 5)
        self.assertEqual(opts[0], (1, date(2026, 3, 16)))
        self.assertEqual(opts[4], (5, date(2026, 3, 20)))

    def test_skips_weekend_in_sequence(self):
        opts = work_days_options(date(2026, 3, 13), max_days=3)
        # (1, Пт 13), (2, Пн 16), (3, Вт 17)
        self.assertEqual(opts[0], (1, date(2026, 3, 13)))
        self.assertEqual(opts[1], (2, date(2026, 3, 16)))
        self.assertEqual(opts[2], (3, date(2026, 3, 17)))

    def test_skips_db_holiday(self):
        Holiday.objects.create(
            date=date(2026, 3, 17), name='X', is_working=False,
        )
        opts = work_days_options(date(2026, 3, 16), max_days=3)
        # (1, Пн 16), (2, Ср 18), (3, Чт 19)
        self.assertEqual(opts[1], (2, date(2026, 3, 18)))
        self.assertEqual(opts[2], (3, date(2026, 3, 19)))

    def test_default_max_days(self):
        opts = work_days_options(date(2026, 3, 16))
        self.assertEqual(len(opts), 200)


# ═════════════════════════════════════════════════════════════
#  load_ru_holidays — точные списки
# ═════════════════════════════════════════════════════════════

class LoadRuHolidaysCommandTests(CoreBase):
    # Точные списки из BUILTIN в core/management/commands/load_ru_holidays.py.
    # Если BUILTIN меняется — обновляем оба списка здесь.
    BUILTIN_2025 = [
        date(2025, 5, 2),
        date(2025, 11, 3),
    ]
    BUILTIN_2026 = [
        date(2026, 1, 9),
        date(2026, 3, 9),
        date(2026, 5, 11),
        date(2026, 6, 14),
        date(2026, 12, 31),
    ]

    def test_builtin_2026_loads_exact_dates(self):
        Holiday.objects.all().delete()
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        loaded = set(Holiday.objects.values_list('date', flat=True))
        self.assertEqual(loaded, set(self.BUILTIN_2026))

    def test_builtin_2025_loads_exact_dates(self):
        Holiday.objects.all().delete()
        call_command('load_ru_holidays', year=2025, source='builtin',
                     verbosity=0)
        loaded = set(Holiday.objects.values_list('date', flat=True))
        self.assertEqual(loaded, set(self.BUILTIN_2025))

    def test_builtin_records_all_not_working(self):
        """⚠ Все BUILTIN-записи помечаются is_working=False.

        Это подразумевает, что в 2025–2026 не было «рабочих суббот»,
        которые нужно отрабатывать. Если правительство объявит такие,
        BUILTIN надо править вручную.
        """
        Holiday.objects.all().delete()
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        self.assertFalse(
            Holiday.objects.filter(is_working=True).exists(),
        )

    def test_clear_flag_removes_old(self):
        Holiday.objects.create(date=date(2020, 1, 1), name='Old')
        call_command('load_ru_holidays', year=2026, source='builtin',
                     clear=True, verbosity=0)
        self.assertFalse(
            Holiday.objects.filter(date=date(2020, 1, 1)).exists(),
        )

    def test_idempotent(self):
        Holiday.objects.all().delete()
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        n1 = Holiday.objects.count()
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        n2 = Holiday.objects.count()
        self.assertEqual(n1, n2)

    def test_update_existing_record(self):
        """Дата уже есть → обновляется, не дублируется."""
        Holiday.objects.create(
            date=date(2026, 1, 9), name='Old name',
            is_working=True,
        )
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        h = Holiday.objects.get(date=date(2026, 1, 9))
        self.assertFalse(h.is_working)
        self.assertNotEqual(h.name, 'Old name')

    def test_unknown_year_raises_command_error(self):
        """⚠ 2027 и далее — нет ни в BUILTIN, ни в xmlcalendar без сети.

        Команда должна явно упасть с CommandError, а не молча вернуть 0.
        """
        Holiday.objects.all().delete()
        with self.assertRaises(CommandError):
            call_command('load_ru_holidays', year=2027, source='builtin',
                         verbosity=0)

    def test_cache_cleared_after_command(self):
        cache.set('svod_holiday_map', {'old': 'value'})
        call_command('load_ru_holidays', year=2026, source='builtin',
                     verbosity=0)
        self.assertIsNone(cache.get('svod_holiday_map'))
