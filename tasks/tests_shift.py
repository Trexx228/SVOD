"""Тесты сервиса пересчёта сдвига сроков."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, Role
from tasks.models import Task
from tasks.services_shift import (
    apply_shift_to_task,
    compute_task_shift,
    recompute_shift_for_executor,
)

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class ShiftBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep = Department.objects.get_or_create(name='ОГК')[0]
        cls.role = _role('staff')

        cls.executor = User.objects.create_user(
            email='exec@test.ru', password='p', full_name='Исполнитель',
            department=cls.dep, role=cls.role, is_active=True,
        )
        cls.requester = User.objects.create_user(
            email='req@test.ru', password='p', full_name='Постановщик',
            department=cls.dep, role=cls.role, is_active=True,
        )

    def _task(self, **kw):
        defaults = {
            'title': 'Задача',
            'plan_hours': 8.0,
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'executor': self.executor,
            'requester': self.requester,
            'status': Task.Status.NEW,
        }
        defaults.update(kw)

        # Автодублирование: если тест задал только original_*, но не текущее
        # значение — считаем, что задача только что создана, и сроки совпадают.
        # Это ближе к реальности: original_* заполняются в момент создания
        # из тех же значений, что и start_due/due.
        if 'original_due' in defaults and 'due' not in defaults:
            defaults['due'] = defaults['original_due']
        if 'original_start_due' in defaults and 'start_due' not in defaults:
            defaults['start_due'] = defaults['original_start_due']

        return Task.objects.create(**defaults)


class ComputeTaskShiftTests(ShiftBase):
    """Формула: shift = max(0, credit - reserve)."""

    def test_zero_credit_returns_zero(self):
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        self.assertEqual(compute_task_shift(t, 0), 0)

    def test_no_due_returns_zero(self):
        t = self._task(plan_hours=8)
        self.assertEqual(compute_task_shift(t, 5), 0)

    def test_full_reserve_no_shift(self):
        # окно 5, нужно 1, reserve=4, credit=1 → 0
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        self.assertEqual(compute_task_shift(t, 1), 0)

    def test_credit_exceeds_reserve(self):
        # окно 5, нужно 2, reserve=3, credit=5 → 2
        t = self._task(
            plan_hours=16,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        self.assertEqual(compute_task_shift(t, 5), 2)

    def test_negative_reserve_compensates_overload(self):
        # окно 2, нужно 3, reserve=-1, credit=1 → 2
        t = self._task(
            plan_hours=24,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 17),
        )
        self.assertEqual(compute_task_shift(t, 1), 2)

    def test_no_start_falls_back_to_created_at(self):
        # original_start_due нет → start = created_at (сегодня).
        # due в будущем на 30 дней → окно большое → shift=0
        future = timezone.localdate() + timedelta(days=30)
        t = self._task(plan_hours=8, original_due=future)
        self.assertEqual(compute_task_shift(t, 1), 0)

    def test_no_start_and_past_due_returns_zero(self):
        """Без start: дедлайн раньше даты создания → окно битое → shift=0.

        Проверка детерминирована вне зависимости от дня недели:
        чек `original_due < start` сработает даже если work_days_between
        на диапазоне из выходных вернёт 0 вместо отрицательного.
        """
        yesterday = timezone.localdate() - timedelta(days=1)
        t = self._task(plan_hours=8, original_due=yesterday)
        self.assertEqual(compute_task_shift(t, 5), 0)


class ApplyShiftToTaskTests(ShiftBase):

    def test_shift_forward(self):
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        apply_shift_to_task(t, 2)
        t.refresh_from_db()
        # 16.03 (пн) + 2 раб = 18.03 (ср)
        self.assertEqual(t.start_due, date(2026, 3, 18))
        # 20.03 (пт) + 2 раб = 24.03 (вт)
        self.assertEqual(t.due, date(2026, 3, 24))

    def test_shift_zero_returns_to_original(self):
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        apply_shift_to_task(t, 3)
        apply_shift_to_task(t, 0)
        t.refresh_from_db()
        self.assertEqual(t.start_due, date(2026, 3, 16))
        self.assertEqual(t.due, date(2026, 3, 20))

    def test_idempotent(self):
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        apply_shift_to_task(t, 2)
        apply_shift_to_task(t, 2)
        apply_shift_to_task(t, 2)
        t.refresh_from_db()
        self.assertEqual(t.due, date(2026, 3, 24))

    def test_returns_true_when_changed(self):
        t = self._task(
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        self.assertTrue(apply_shift_to_task(t, 1))
        self.assertFalse(apply_shift_to_task(t, 1))


class RecomputeShiftTests(ShiftBase):

    def test_full_flow_with_reserve(self):
        x = self._task(
            title='X с запасом',
            plan_hours=8,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 20),
        )
        self.executor.shift_credit_days = 2
        self.executor.save(update_fields=['shift_credit_days'])

        recompute_shift_for_executor(self.executor)
        x.refresh_from_db()
        self.assertEqual(x.due, date(2026, 3, 20))

    def test_full_flow_with_overload(self):
        x = self._task(
            title='X перегружен',
            plan_hours=24,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 17),
        )
        self.executor.shift_credit_days = 1
        self.executor.save(update_fields=['shift_credit_days'])

        recompute_shift_for_executor(self.executor)
        x.refresh_from_db()
        # 17.03 (вт) + 2 раб = 19.03 (чт)
        self.assertEqual(x.due, date(2026, 3, 19))

    def test_in_progress_not_shifted(self):
        t = self._task(
            title='В работе',
            plan_hours=24,
            status=Task.Status.IN_PROGRESS,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 17),
        )
        self.executor.shift_credit_days = 5
        self.executor.save(update_fields=['shift_credit_days'])

        recompute_shift_for_executor(self.executor)
        t.refresh_from_db()
        self.assertEqual(t.due, date(2026, 3, 17))

    def test_exclude_task_not_shifted(self):
        x = self._task(
            title='X перегружен',
            plan_hours=24,
            original_start_due=date(2026, 3, 16),
            original_due=date(2026, 3, 17),
        )
        self.executor.shift_credit_days = 5
        self.executor.save(update_fields=['shift_credit_days'])

        recompute_shift_for_executor(
            self.executor, exclude_task_pk=x.pk,
        )
        x.refresh_from_db()
        self.assertEqual(x.due, date(2026, 3, 17))
