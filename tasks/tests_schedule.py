"""Тесты опционального WorkSchedule в утилитах дат (tasks/utils.py).

Проверяют C.2: функции is_workday, next_workday, add_work_days,
work_days_between, work_days_options принимают schedule=None.

При schedule=None — старая логика (пн–пт + праздники).
При schedule=WorkSchedule — паттерн графика, праздники игнорируются.

Все тесты используют фиксированный anchor_date в будущем и оперируют
датами относительно него — не зависят от сегодня и дня недели.

Кэш norm_hours() и svod_holiday_map не подчищаются транзакциями,
чистим вручную.
"""
from datetime import date, timedelta

from django.core.cache import cache
from django.test import TestCase

from accounts.models import WorkSchedule
from core.models import Holiday
from tasks.utils import (
    add_work_days,
    day_info,
    is_workday,
    next_workday,
    work_days_between,
    work_days_options,
)


# 2026-11-02 — понедельник (проверено отдельно), удобно как «якорь».
ANCHOR = date(2026, 11, 2)


class ScheduleBase(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _mk_schedule(self, pattern, anchor=ANCHOR, name='Тестовый',
                     use_calendar=False):
        """Хелпер: создать график.

        По умолчанию use_calendar=False (только паттерн) — это чистая
        семантика графика. Тесты про праздники включают его явно.
        """
        return WorkSchedule.objects.create(
            name=name, pattern=pattern, anchor_date=anchor,
            hours_per_shift=8.0, is_active=True,
            use_calendar=use_calendar,
        )


# ═════════════════════════════════════════════════════════════
#  5/2 — стандартная пятидневка
# ═════════════════════════════════════════════════════════════

class FiveTwoTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.sch = self._mk_schedule([1, 1, 1, 1, 1, 0, 0], name='5/2')

    def test_mon_to_fri_working(self):
        for i in range(5):
            d = ANCHOR + timedelta(days=i)
            self.assertTrue(is_workday(d, schedule=self.sch), msg=str(d))

    def test_sat_sun_weekend(self):
        self.assertFalse(is_workday(ANCHOR + timedelta(days=5), schedule=self.sch))
        self.assertFalse(is_workday(ANCHOR + timedelta(days=6), schedule=self.sch))

    def test_second_week_repeats(self):
        d = ANCHOR + timedelta(days=7)  # след. понедельник
        self.assertTrue(is_workday(d, schedule=self.sch))

    def test_source_is_schedule(self):
        info = day_info(ANCHOR, schedule=self.sch)
        self.assertEqual(info['source'], 'schedule')
        self.assertTrue(info['is_working'])

    def test_next_workday_from_weekend(self):
        sat = ANCHOR + timedelta(days=5)
        self.assertEqual(next_workday(sat, schedule=self.sch), ANCHOR + timedelta(days=7))

    def test_add_work_days(self):
        # Пн + 5 раб. дней = следующий Пн
        result = add_work_days(ANCHOR, 5, schedule=self.sch)
        self.assertEqual(result, ANCHOR + timedelta(days=7))

    def test_work_days_between(self):
        # Пн → Пт включительно = 5
        self.assertEqual(
            work_days_between(ANCHOR, ANCHOR + timedelta(days=4), schedule=self.sch),
            5,
        )
        # Пн → Сб включительно = 5 (Сб не работает)
        self.assertEqual(
            work_days_between(ANCHOR, ANCHOR + timedelta(days=5), schedule=self.sch),
            5,
        )


# ═════════════════════════════════════════════════════════════
#  2/2 — два работают, два отдыхают
# ═════════════════════════════════════════════════════════════

class TwoTwoTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.sch = self._mk_schedule([1, 1, 0, 0], name='2/2')

    def test_pattern_cycle(self):
        # offset 0 — раб, 1 — раб, 2 — вых, 3 — вых, 4 — раб (цикл)
        expected = [True, True, False, False, True, True, False, False]
        for i, exp in enumerate(expected):
            d = ANCHOR + timedelta(days=i)
            self.assertEqual(is_workday(d, schedule=self.sch), exp, msg=str(d))

    def test_add_work_days_skips_weekend(self):
        # Пн (offset 0) + 2 раб. дней:
        #   offset 1 — раб (added=1)
        #   offset 2 — вых
        #   offset 3 — вых
        #   offset 4 — раб (added=2)
        result = add_work_days(ANCHOR, 2, schedule=self.sch)
        self.assertEqual(result, ANCHOR + timedelta(days=4))


# ═════════════════════════════════════════════════════════════
#  7/7 — неделя через неделю
# ═════════════════════════════════════════════════════════════

class SevenSevenTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.sch = self._mk_schedule(
            [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
            name='7/7',
        )

    def test_first_seven_working(self):
        for i in range(7):
            d = ANCHOR + timedelta(days=i)
            self.assertTrue(is_workday(d, schedule=self.sch), msg=str(d))

    def test_next_seven_weekend(self):
        for i in range(7, 14):
            d = ANCHOR + timedelta(days=i)
            self.assertFalse(is_workday(d, schedule=self.sch), msg=str(d))

    def test_cycle_repeats_at_14(self):
        d = ANCHOR + timedelta(days=14)
        self.assertTrue(is_workday(d, schedule=self.sch))

    def test_next_workday_from_long_weekend(self):
        # Последний день «отдыха» — 13-й
        d = ANCHOR + timedelta(days=13)
        self.assertEqual(
            next_workday(d, schedule=self.sch),
            ANCHOR + timedelta(days=14),
            )


# ═════════════════════════════════════════════════════════════
#  Праздники игнорируются при заданном графике
# ═════════════════════════════════════════════════════════════

class HolidaysRespectTests(ScheduleBase):
    def test_holiday_ignored_when_use_calendar_false(self):
        """use_calendar=False → паттерн побеждает над праздником.

        Семантика сменных графиков: 2/2, 7/7 не знают про 1 января —
        если смена по паттерну, человек работает.
        """
        d = ANCHOR + timedelta(days=2)  # среда, рабочий по 5/2
        Holiday.objects.create(date=d, name='Тестовый праздник', is_working=False)

        # Без графика — праздник.
        self.assertFalse(is_workday(d))

        # С графиком без календаря — рабочий.
        sch = self._mk_schedule(
            [1, 1, 1, 1, 1, 0, 0], name='5/2 без календаря',
            use_calendar=False,
        )
        self.assertTrue(is_workday(d, schedule=sch))

    def test_holiday_respected_when_use_calendar_true(self):
        """use_calendar=True → праздник побеждает над паттерном.

        Семантика офисных сотрудников: 1 января — выходной, даже если
        паттерн 5/2 формально говорит «работать в среду».
        """
        d = ANCHOR + timedelta(days=2)
        Holiday.objects.create(date=d, name='Тестовый праздник', is_working=False)

        sch = self._mk_schedule(
            [1, 1, 1, 1, 1, 0, 0], name='5/2 с календарём',
            use_calendar=True,
        )
        self.assertFalse(is_workday(d, schedule=sch))

    def test_transfer_working_weekend_respected(self):
        """Перенос: суббота помечена как рабочий — при use_calendar=True
        она работает, даже если паттерн говорит «выходной».
        """
        sat = ANCHOR + timedelta(days=5)  # суббота
        Holiday.objects.create(date=sat, name='Рабочая суббота', is_working=True)

        # 5/2 без календаря: суббота выходная.
        sch_no = self._mk_schedule([1, 1, 1, 1, 1, 0, 0], use_calendar=False)
        self.assertFalse(is_workday(sat, schedule=sch_no))

        # 5/2 с календарём: суббота рабочая (перенос).
        sch_cal = self._mk_schedule(
            [1, 1, 1, 1, 1, 0, 0], name='5/2+cal',
            use_calendar=True,
        )
        self.assertTrue(is_workday(sat, schedule=sch_cal))


# ═════════════════════════════════════════════════════════════
#  Fallback-и
# ═════════════════════════════════════════════════════════════

class FallbackTests(ScheduleBase):
    def test_schedule_none_uses_old_logic(self):
        """schedule=None → пн–пт, как было."""
        # 2026-11-02 — понедельник
        self.assertTrue(is_workday(ANCHOR))
        # 2026-11-07 — суббота
        self.assertFalse(is_workday(ANCHOR + timedelta(days=5)))

    def test_empty_pattern_falls_back(self):
        """Паттерн [] → fallback на старую логику, не зацикливаемся."""
        sch = self._mk_schedule([], name='Пустой')
        # В понедельник — рабочий (как без графика).
        self.assertTrue(is_workday(ANCHOR, schedule=sch))

    def test_all_zero_pattern_falls_back(self):
        """Паттерн [0,0,0] → fallback, иначе next_workday зациклится."""
        sch = self._mk_schedule([0, 0, 0], name='Вечно выходной')
        self.assertTrue(is_workday(ANCHOR, schedule=sch))
        # next_workday не должен зависнуть
        self.assertEqual(next_workday(ANCHOR, schedule=sch), ANCHOR)

    def test_region_code_ignored_when_schedule(self):
        """При заданном schedule region_code не влияет."""
        sch = self._mk_schedule([1, 1, 1, 1, 1, 0, 0])
        info1 = day_info(ANCHOR, region_code='RU', schedule=sch)
        info2 = day_info(ANCHOR, region_code='XX', schedule=sch)
        self.assertEqual(info1, info2)


# ═════════════════════════════════════════════════════════════
#  work_days_options
# ═════════════════════════════════════════════════════════════

class WorkDaysOptionsTests(ScheduleBase):
    def test_options_follow_schedule(self):
        """Последовательность не пропускает нерабочие дни."""
        sch = self._mk_schedule([1, 1, 0, 0], name='2/2')
        opts = work_days_options(ANCHOR, max_days=4, schedule=sch)
        # 4 варианта: offset 0, offset 1, offset 4, offset 5
        # (offset 2 и 3 — выходные, add_work_days пропустит)
        dates = [d for _, d in opts]
        self.assertEqual(dates[0], ANCHOR)
        self.assertEqual(dates[1], ANCHOR + timedelta(days=1))
        self.assertEqual(dates[2], ANCHOR + timedelta(days=4))
        self.assertEqual(dates[3], ANCHOR + timedelta(days=5))



# ═════════════════════════════════════════════════════════════
#  C.3a: schedule применяется в сдвиге, загруженности и шаблонах
# ═════════════════════════════════════════════════════════════

class ScheduleForUserTests(TestCase):
    """Приоритет источников графика: личный → отдела → старая логика.

    Seed-график «Стандартный 5/2» осознанно игнорируется — иначе на
    проде пропадут праздники РФ.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        from accounts.models import WorkSchedule as WS
        from django.contrib.auth import get_user_model

        cls.role = Role.objects.get_or_create(
            code='staff-sfu', defaults={'name': 'Сотрудник SFU'},
        )[0]

        cls.dept = Department.objects.create(name='SFU-пусто')
        cls.dept52 = Department.objects.create(name='SFU-5-2')
        cls.dept22 = Department.objects.create(name='SFU-2-2')

        cls.ws_52 = WS.objects.create(
            name='Стандартный 5/2', pattern=[1, 1, 1, 1, 1, 0, 0],
            anchor_date=ANCHOR, hours_per_shift=8.0,
        )
        cls.ws_22 = WS.objects.create(
            name='2/2', pattern=[1, 1, 0, 0],
            anchor_date=ANCHOR, hours_per_shift=8.0,
        )
        cls.dept52.default_schedule = cls.ws_52
        cls.dept52.save(update_fields=['default_schedule'])
        cls.dept22.default_schedule = cls.ws_22
        cls.dept22.save(update_fields=['default_schedule'])

        U = get_user_model()
        cls.u_empty = U.objects.create_user(
            email='u-empty@test.ru', password='p', full_name='Пусто',
            department=cls.dept, role=cls.role, is_active=True,
        )
        cls.u_52 = U.objects.create_user(
            email='u-52@test.ru', password='p', full_name='5/2',
            department=cls.dept52, role=cls.role, is_active=True,
        )
        cls.u_22 = U.objects.create_user(
            email='u-22@test.ru', password='p', full_name='2/2',
            department=cls.dept22, role=cls.role, is_active=True,
        )

    def setUp(self):
        cache.clear()

    def test_no_schedule_returns_none(self):
        from tasks.utils import schedule_for_user
        self.assertIsNone(schedule_for_user(self.u_empty))

    def test_seed_5_2_applied_with_calendar(self):
        """Seed-график 5/2 теперь применяется, но с use_calendar=True.

        Раньше seed отсекался в schedule_for_user, чтобы не терять
        праздники. Теперь эту роль играет флаг use_calendar.
        """
        from tasks.utils import schedule_for_user

        s = schedule_for_user(self.u_52)
        self.assertIsNotNone(s)
        self.assertEqual(s.pk, self.ws_52.pk)
        self.assertTrue(s.use_calendar)

    def test_department_2_2_applied(self):
        from tasks.utils import schedule_for_user
        self.assertEqual(schedule_for_user(self.u_22).pk, self.ws_22.pk)

    def test_personal_overrides_department(self):
        from tasks.utils import schedule_for_user
        from accounts.models import WorkSchedule as WS
        ws_night = WS.objects.create(
            name='Ночная смена 2/2', pattern=[1, 1, 0, 0],
            anchor_date=ANCHOR, hours_per_shift=8.0,
        )
        self.u_22.schedule = ws_night
        self.u_22.save(update_fields=['schedule'])
        self.assertEqual(schedule_for_user(self.u_22).pk, ws_night.pk)

    def test_inactive_ignored(self):
        from tasks.utils import schedule_for_user
        self.ws_22.is_active = False
        self.ws_22.save(update_fields=['is_active'])
        self.assertIsNone(schedule_for_user(self.u_22))


class RecomputeShiftWithScheduleTests(TestCase):
    """recompute_shift_for_executor учитывает график исполнителя."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        from accounts.models import WorkSchedule as WS
        from django.contrib.auth import get_user_model

        cls.role = Role.objects.get_or_create(
            code='staff-rsw', defaults={'name': 'Сотрудник RSW'},
        )[0]
        cls.dept = Department.objects.create(name='RSW')
        cls.ws_77 = WS.objects.create(
            name='7/7', pattern=[1]*7 + [0]*7,
            anchor_date=ANCHOR, hours_per_shift=8.0,
            use_calendar=False,   # сменный график, праздники не важны
        )
        cls.dept.default_schedule = cls.ws_77
        cls.dept.save(update_fields=['default_schedule'])

        U = get_user_model()
        cls.executor = U.objects.create_user(
            email='ex-rsw@test.ru', password='p', full_name='Исполнитель 7/7',
            department=cls.dept, role=cls.role, is_active=True,
        )
        cls.requester = U.objects.create_user(
            email='req-rsw@test.ru', password='p', full_name='Постановщик',
            department=cls.dept, role=cls.role, is_active=True,
        )

    def setUp(self):
        cache.clear()

    def test_shift_uses_77_schedule(self):
        """При 7/7 сдвиг на 2 раб. дня пропускает 7-дневный отдых."""
        from tasks.models import Task
        from tasks.services_shift import recompute_shift_for_executor

        # Задача с окном 1 день (крошечный reserve).
        # Плана хватает на 1 день, всё остальное credit уйдёт в сдвиг.
        start = ANCHOR
        due = ANCHOR  # окно = 1 раб. день по 7/7

        t = Task.objects.create(
            title='Окно 1', plan_hours=8, scale='s', kind='work',
            priority='medium', executor=self.executor, requester=self.requester,
            status=Task.Status.NEW,
            original_start_due=start, original_due=due,
            start_due=start, due=due,
        )
        self.executor.shift_credit_days = 3
        self.executor.save(update_fields=['shift_credit_days'])

        recompute_shift_for_executor(self.executor)
        t.refresh_from_db()

        # reserve = 1 - 1 = 0; shift = 3 - 0 = 3.
        # По графику 7/7 три рабочих дня от ANCHOR — это ANCHOR+1, +2, +3.
        self.assertEqual(t.due, ANCHOR + timedelta(days=3))
