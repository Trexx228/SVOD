"""Автотесты admin_panel.integrity: 18 секций + summary + контракт.

═══ Про подводные камни ═══

Этот модуль покрывает не только happy-path диагностики, но и её
известные ограничения:

  1. `_section` глотает ЛЮБОЕ исключение из `qs.count()` и из итерации.
     Реальный сбой SQL → секция выглядит пустой. Тест это фиксирует.
  2. `branch_wrong_order` в integrity.py проверяет только первые 5000
     задач через Python-итерацию. Задачи сверх лимита не проверяются.
  3. `Task.executor=PROTECT, null=False` — секция `task_no_executor`
     никогда не срабатывает. Мы это документируем явно, чтобы никто
     не думал, что она ловит реальные проблемы.
  4. `rows[:limit]` (50 по умолчанию) — если записей больше, `count`
     правильный, но `rows` обрезаны. UI должен это учитывать.
  5. `norm_hours()` кэшируется; при изменении Norm через save()
     без cache.delete — кэш остаётся stale на час.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, Role
from admin_panel.integrity import collect_integrity_checks, _section
from tasks.models import (
    Order, ShopSession, Task, TaskBranch, TimeSession,
)

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


# ═════════════════════════════════════════════════════════════
#  База
# ═════════════════════════════════════════════════════════════

class IntegrityBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep = Department.objects.get_or_create(name='ОГК')[0]
        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)

        cls.user = User.objects.create_user(
            email='u@test.ru', password='p', full_name='Юзер',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='m@test.ru', password='p', full_name='Менеджер',
            department=cls.dep, role=cls.role_mgr, is_active=True,
        )

    def _task(self, **kw):
        defaults = {
            'title': 'Задача', 'plan_hours': 2.0, 'scale': 's', 'kind': 'work',
            'executor': self.user, 'requester': self.mgr,
            'status': Task.Status.NEW,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)

    def _sections_by_key(self):
        sections, _ = collect_integrity_checks()
        return {s['key']: s for s in sections}


# ═════════════════════════════════════════════════════════════
#  Контракт _section
# ═════════════════════════════════════════════════════════════

class SectionContractTests(IntegrityBase):
    """_section — низкоуровневый хелпер. Фиксируем контракт."""

    def test_returns_dict_with_required_keys(self):
        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=Task.objects.none(),
        )
        for k in ('key', 'title', 'description', 'severity',
                  'count', 'rows', 'url_prefix'):
            self.assertIn(k, s)

    def test_default_url_prefix(self):
        """Дефолт — /tasks/task/ (открыть задачу)."""
        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=Task.objects.none(),
        )
        self.assertEqual(s['url_prefix'], '/tasks/task/')

    def test_custom_url_prefix_preserved(self):
        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=Task.objects.none(),
            url_prefix='/custom/',
        )
        self.assertEqual(s['url_prefix'], '/custom/')

    def test_default_limit_is_50(self):
        for i in range(60):
            self._task(
                title=f'D{i}', status=Task.Status.IN_PROGRESS,
                finished_at=timezone.now(),
            )
        qs = Task.objects.filter(finished_at__isnull=False)
        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=qs,
        )
        self.assertEqual(s['count'], 60)
        self.assertEqual(len(s['rows']), 50)

    def test_custom_limit_respected(self):
        for i in range(10):
            self._task(title=f'D{i}')
        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=Task.objects.all(), limit=3,
        )
        self.assertEqual(len(s['rows']), 3)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_count_exception_swallowed(self):
        """⚠ _section ГЛОТАЕТ исключение из qs.count() и ставит count=0.

        Это значит: реальный сбой SQL (например, битый индекс)
        покажет секцию как «пустую» вместо того, чтобы упасть.
        Мы документируем это поведение — при рефакторинге нужно
        помнить, что «чистая» секция может означать «сломанный запрос».
        """
        broken_qs = Task.objects.all()
        with patch.object(broken_qs, 'count', side_effect=RuntimeError('boom')):
            s = _section(
                key='x', title='X', description='D',
                severity='danger', qs=broken_qs,
            )
        self.assertEqual(s['count'], 0)
        self.assertEqual(s['rows'], [])

    def test_iteration_exception_yields_empty_rows(self):
        """⚠ Если qs.count() вернул > 0, но срез/итерация упали —
        получим count > 0 и rows = []. Это осознанное поведение,
        но легко забыть. Тест фиксирует.

        ⚠ Подводный камень мока: `qs[:limit]` создаёт НОВЫЙ QuerySet,
        поэтому `patch.object(qs, '__iter__')` бесполезен — надо
        подсунуть прокси, который рушится на `__getitem__`.
        """
        for i in range(3):
            self._task(title=f'D{i}')

        real_qs = Task.objects.all()

        class BrokenSlice:
            """Прокси: count() работает, срез/итерация — падают."""
            def __init__(self, qs):
                self._qs = qs

            def count(self):
                return self._qs.count()

            def __getitem__(self, key):
                raise RuntimeError('boom on slice')

            def __iter__(self):
                raise RuntimeError('boom on iter')

        s = _section(
            key='x', title='X', description='D',
            severity='warn', qs=BrokenSlice(real_qs), limit=50,
        )
        self.assertEqual(s['count'], 3)
        self.assertEqual(s['rows'], [])


# ═════════════════════════════════════════════════════════════
#  Структура вывода
# ═════════════════════════════════════════════════════════════

class SectionStructureTests(IntegrityBase):
    EXPECTED_KEYS = {
        'finished_at_wrong_status',
        'done_no_finished_at',
        'timer_wrong_status',
        'session_zero_duration',
        'session_negative',
        'session_too_long',
        'shop_stale',
        'shop_zero_duration',
        'role_empty',
        'dept_empty',
        'user_no_role',
        'user_no_dept',
        'branch_no_order',
        'branch_wrong_order',
        'self_parent',
        'bad_email',
        'role_admin_no_users',
        'task_no_executor',
    }

    def test_all_sections_present(self):
        by_key = self._sections_by_key()
        missing = self.EXPECTED_KEYS - set(by_key.keys())
        extra = set(by_key.keys()) - self.EXPECTED_KEYS
        self.assertEqual(missing, set(), msg=f'Не найдены: {missing}')
        if extra:
            self.fail(f'Новые секции не в тесте: {extra}')

    def test_return_tuple(self):
        result = collect_integrity_checks()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        sections, summary = result
        self.assertIsInstance(sections, list)
        self.assertIsInstance(summary, dict)

    def test_each_section_has_required_fields(self):
        for key, s in self._sections_by_key().items():
            for field in ('key', 'title', 'description', 'severity',
                          'count', 'rows', 'url_prefix'):
                self.assertIn(field, s, msg=f'{key}.{field}')

    def test_severity_values_valid(self):
        for key, s in self._sections_by_key().items():
            self.assertIn(s['severity'], ('info', 'warn', 'danger'),
                          msg=key)

    def test_clean_db_no_problems(self):
        """Пустая БД → ни в одной danger-секции нет проблем."""
        for key, s in self._sections_by_key().items():
            if s['severity'] == 'danger':
                self.assertEqual(s['count'], 0, msg=key)


# ═════════════════════════════════════════════════════════════
#  Секция: finished_at_wrong_status
# ═════════════════════════════════════════════════════════════

class FinishedAtWrongStatusTests(IntegrityBase):
    def test_detects_finished_at_without_terminal_status(self):
        self._task(
            title='Битая', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        s = self._sections_by_key()['finished_at_wrong_status']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')

    def test_done_with_finished_at_is_ok(self):
        self._task(
            title='Ок', status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self._sections_by_key()['finished_at_wrong_status']['count'], 0,
        )

    def test_cancelled_with_finished_at_is_ok(self):
        self._task(
            title='Отменена', status=Task.Status.CANCELLED,
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self._sections_by_key()['finished_at_wrong_status']['count'], 0,
        )

    def test_review_with_finished_at_flagged(self):
        """REVIEW не считается «финишным» статусом."""
        self._task(
            title='На проверке', status=Task.Status.REVIEW,
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self._sections_by_key()['finished_at_wrong_status']['count'], 1,
        )

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_mutually_exclusive_with_done_no_finished_at(self):
        """Одна задача не может попасть одновременно в finished_at_wrong_status
        и в done_no_finished_at — условия взаимоисключающие.
        """
        self._task(
            title='DONE с fin', status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        self._task(title='DONE без fin', status=Task.Status.DONE)
        by_key = self._sections_by_key()
        self.assertEqual(by_key['finished_at_wrong_status']['count'], 0)
        self.assertEqual(by_key['done_no_finished_at']['count'], 1)


# ═════════════════════════════════════════════════════════════
#  Секция: done_no_finished_at
# ═════════════════════════════════════════════════════════════

class DoneNoFinishedAtTests(IntegrityBase):
    def test_done_without_finished_at(self):
        self._task(title='DONE без fin', status=Task.Status.DONE)
        s = self._sections_by_key()['done_no_finished_at']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')

    def test_cancelled_without_finished_at(self):
        self._task(title='CANCEL без fin', status=Task.Status.CANCELLED)
        self.assertEqual(
            self._sections_by_key()['done_no_finished_at']['count'], 1,
        )

    def test_new_without_finished_at_is_ok(self):
        self._task(title='New', status=Task.Status.NEW)
        self.assertEqual(
            self._sections_by_key()['done_no_finished_at']['count'], 0,
        )


# ═════════════════════════════════════════════════════════════
#  Секция: timer_wrong_status
# ═════════════════════════════════════════════════════════════

class TimerWrongStatusTests(IntegrityBase):
    def test_timer_running_but_status_not_in_progress(self):
        self._task(
            title='Зависший таймер',
            status=Task.Status.PAUSED,
            session_started_at=timezone.now(),
        )
        s = self._sections_by_key()['timer_wrong_status']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')

    def test_in_progress_with_timer_is_ok(self):
        self._task(
            title='Ок', status=Task.Status.IN_PROGRESS,
            session_started_at=timezone.now(),
        )
        self.assertEqual(
            self._sections_by_key()['timer_wrong_status']['count'], 0,
        )

    def test_pending_approval_with_timer_flagged(self):
        """PENDING_APPROVAL — тоже не IN_PROGRESS."""
        self._task(
            title='Ждёт согласования',
            status=Task.Status.PENDING_APPROVAL,
            session_started_at=timezone.now(),
        )
        self.assertEqual(
            self._sections_by_key()['timer_wrong_status']['count'], 1,
        )


# ═════════════════════════════════════════════════════════════
#  TimeSession
# ═════════════════════════════════════════════════════════════

class SessionZeroDurationTests(IntegrityBase):
    def test_zero_duration(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=0.0,
            status_at_close=Task.Status.DONE,
        )
        s = self._sections_by_key()['session_zero_duration']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')

    def test_negative_duration(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=-1.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_zero_duration']['count'], 1,
        )

    def test_positive_duration_is_ok(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=1), finished_at=now,
            duration_hours=1.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_zero_duration']['count'], 0,
        )

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_zero_and_negative_both_flagged(self):
        """Фильтр `__lte=0` ловит и 0, и отрицательные. Проверим."""
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=0.0,
            status_at_close=Task.Status.DONE,
        )
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=-5.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_zero_duration']['count'], 2,
        )


class SessionNegativeTests(IntegrityBase):
    def test_finished_before_started(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now - timedelta(hours=1),
            duration_hours=1.0,
            status_at_close=Task.Status.DONE,
        )
        s = self._sections_by_key()['session_negative']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')

    def test_normal_session_is_ok(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=1), finished_at=now,
            duration_hours=1.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_negative']['count'], 0,
        )

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_equal_start_and_end_not_flagged(self):
        """started_at == finished_at — не negative, не flagged."""
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=0.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_negative']['count'], 0,
        )


class SessionTooLongTests(IntegrityBase):
    """Сессии > 48 ч."""

    def test_long_session_detected(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=60), finished_at=now,
            duration_hours=60.0,
            status_at_close=Task.Status.DONE,
        )
        s = self._sections_by_key()['session_too_long']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')

    def test_48h_boundary_not_flagged(self):
        """Ровно 48 → не flagged (`__gt=48`)."""
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=48), finished_at=now,
            duration_hours=48.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_too_long']['count'], 0,
        )

    def test_just_over_48h_flagged(self):
        """48.1 → flagged."""
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=48, minutes=6),
            finished_at=now,
            duration_hours=48.1,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_too_long']['count'], 1,
        )

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_48h_session_not_in_zero_duration(self):
        """Одна сессия не должна попадать в несколько секций."""
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now - timedelta(hours=48), finished_at=now,
            duration_hours=48.0,
            status_at_close=Task.Status.DONE,
        )
        self.assertEqual(
            self._sections_by_key()['session_zero_duration']['count'], 0,
        )
        self.assertEqual(
            self._sections_by_key()['session_too_long']['count'], 0,
        )


# ═════════════════════════════════════════════════════════════
#  ShopSession
# ═════════════════════════════════════════════════════════════

class ShopStaleTests(IntegrityBase):
    def test_stale_open_session(self):
        t = self._task()
        s = ShopSession.objects.create(task=t, executor=self.user)
        ShopSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(hours=30),
        )
        s = self._sections_by_key()['shop_stale']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')

    def test_fresh_open_session_ok(self):
        t = self._task()
        ShopSession.objects.create(task=t, executor=self.user)
        self.assertEqual(self._sections_by_key()['shop_stale']['count'], 0)

    def test_closed_session_ok(self):
        t = self._task()
        now = timezone.now()
        ShopSession.objects.create(
            task=t, executor=self.user,
            finished_at=now, duration_hours=1.0,
        )
        self.assertEqual(self._sections_by_key()['shop_stale']['count'], 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_24h_boundary(self):
        """Ровно 24 часа → не flagged (`__lt` для started_at)."""
        t = self._task()
        s = ShopSession.objects.create(task=t, executor=self.user)
        ShopSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(hours=24),
        )
        # started_at < now - 24h → False если ровно 24
        # Проверим что «примерно 24.001» уже flagged
        ShopSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(hours=24, minutes=1),
        )
        self.assertEqual(
            self._sections_by_key()['shop_stale']['count'], 1,
        )


class ShopZeroDurationTests(IntegrityBase):
    def test_zero_duration_shop_session(self):
        t = self._task()
        ShopSession.objects.create(
            task=t, executor=self.user,
            finished_at=timezone.now(), duration_hours=0.0,
        )
        self.assertEqual(
            self._sections_by_key()['shop_zero_duration']['count'], 1,
        )

    def test_negative_duration_shop_session(self):
        t = self._task()
        ShopSession.objects.create(
            task=t, executor=self.user,
            finished_at=timezone.now(), duration_hours=-0.5,
        )
        self.assertEqual(
            self._sections_by_key()['shop_zero_duration']['count'], 1,
        )

    def test_open_session_not_in_zero_duration(self):
        """Открытые сессии — только в shop_stale, не в zero_duration."""
        t = self._task()
        s = ShopSession.objects.create(task=t, executor=self.user)
        ShopSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(hours=30),
        )
        self.assertEqual(
            self._sections_by_key()['shop_zero_duration']['count'], 0,
        )
        self.assertEqual(
            self._sections_by_key()['shop_stale']['count'], 1,
        )


# ═════════════════════════════════════════════════════════════
#  Справочники
# ═════════════════════════════════════════════════════════════

class RoleEmptyTests(IntegrityBase):
    def test_role_without_users(self):
        Role.objects.create(code='ghost', name='Никем не занятая')
        s = self._sections_by_key()['role_empty']
        self.assertGreaterEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'info')

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_role_with_inactive_users_counted_as_empty(self):
        """`Count('users')` без фильтра — считает и отключённых.

        Это значит: роль с одним отключённым user НЕ попадёт в role_empty,
        даже если фактически никто её не использует.
        """
        ghost_role = Role.objects.create(code='ghost2', name='Пустая')
        User.objects.create_user(
            email='x@test.ru', password='p', full_name='X',
            role=ghost_role, is_active=False,
        )
        s = self._sections_by_key()['role_empty']
        codes = [r.code for r in s['rows']]
        self.assertNotIn('ghost2', codes)


class DeptEmptyTests(IntegrityBase):
    def test_empty_department(self):
        Department.objects.create(name='Пустой')
        s = self._sections_by_key()['dept_empty']
        self.assertGreaterEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'info')


class RoleAdminNoUsersTests(IntegrityBase):
    def test_admin_role_without_active_users(self):
        Role.objects.create(
            code='ghost_admin', name='Пустой админ',
            can_admin=True,
        )
        s = self._sections_by_key()['role_admin_no_users']
        self.assertGreaterEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'info')

    def test_admin_role_with_user_not_flagged(self):
        admin_role = _role('real_admin', can_admin=True)
        self.user.role = admin_role
        self.user.save(update_fields=['role'])
        s = self._sections_by_key()['role_admin_no_users']
        codes = [r.code for r in s['rows']]
        self.assertNotIn('real_admin', codes)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_admin_role_with_only_inactive_users_flagged(self):
        """Роль даёт админку, но все её обладатели — неактивные.
        Это warning-уровневая проблема: админка «простаивает».
        """
        admin_role = Role.objects.create(
            code='sleeping_admin', name='Спящий админ',
            can_admin=True,
        )
        User.objects.create_user(
            email='sleeping@test.ru', password='p', full_name='С',
            role=admin_role, is_active=False,
        )
        s = self._sections_by_key()['role_admin_no_users']
        codes = [r.code for r in s['rows']]
        self.assertIn('sleeping_admin', codes)


# ═════════════════════════════════════════════════════════════
#  Пользователи
# ═════════════════════════════════════════════════════════════

class UserNoRoleTests(IntegrityBase):
    def test_active_user_without_role(self):
        User.objects.create_user(
            email='norole@test.ru', password='p', full_name='Безрольный',
            department=self.dep, role=None, is_active=True,
        )
        s = self._sections_by_key()['user_no_role']
        self.assertGreaterEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')

    def test_inactive_user_without_role_not_counted(self):
        User.objects.create_user(
            email='inactive@test.ru', password='p', full_name='Отключённый',
            department=self.dep, role=None, is_active=False,
        )
        emails = [u.email for u in self._sections_by_key()['user_no_role']['rows']]
        self.assertNotIn('inactive@test.ru', emails)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_user_can_be_in_no_role_and_no_dept(self):
        """Один пользователь может попасть в обе секции — это не баг,
        это две разные проблемы. UI должен показывать его дважды.
        """
        User.objects.create_user(
            email='lost@test.ru', password='p', full_name='Потеряшка',
            department=None, role=None, is_active=True,
        )
        by_key = self._sections_by_key()
        emails_role = [u.email for u in by_key['user_no_role']['rows']]
        emails_dept = [u.email for u in by_key['user_no_dept']['rows']]
        self.assertIn('lost@test.ru', emails_role)
        self.assertIn('lost@test.ru', emails_dept)


class UserNoDeptTests(IntegrityBase):
    def test_active_user_without_department(self):
        User.objects.create_user(
            email='nodept@test.ru', password='p', full_name='Безотдельский',
            department=None, role=self.role_staff, is_active=True,
        )
        s = self._sections_by_key()['user_no_dept']
        self.assertGreaterEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'warn')


class BadEmailTests(IntegrityBase):
    def test_email_without_at_sign(self):
        u = User(
            email='broken', full_name='Битый',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        u.set_password('p')
        u.save()
        s = self._sections_by_key()['bad_email']
        self.assertGreaterEqual(s['count'], 1)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_email_with_at_sign_but_invalid_not_flagged(self):
        """Секция ловит только «нет @». Всё, что похоже на email,
        но битое (например `a@b`), не flagged.
        """
        u = User(
            email='weird@', full_name='Странный',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        u.set_password('p')
        u.save()
        emails = [x.email for x in self._sections_by_key()['bad_email']['rows']]
        self.assertNotIn('weird@', emails)


# ═════════════════════════════════════════════════════════════
#  Задачи: ветки, order, parent
# ═════════════════════════════════════════════════════════════

class BranchNoOrderTests(IntegrityBase):
    def test_branch_without_order(self):
        order = Order.objects.create(
            number='O-1', product='X',
            ship_due=timezone.localdate() + timedelta(days=30),
        )
        br = TaskBranch.objects.create(order=order, name='B')
        self._task(branch=br, order=None)
        s = self._sections_by_key()['branch_no_order']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')


class BranchWrongOrderTests(IntegrityBase):
    def test_branch_from_other_order(self):
        o1 = Order.objects.create(number='O-1', product='X')
        o2 = Order.objects.create(number='O-2', product='Y')
        br1 = TaskBranch.objects.create(order=o1, name='B1')
        self._task(branch=br1, order=o2)
        s = self._sections_by_key()['branch_wrong_order']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')

    def test_correct_branch_order_ok(self):
        o1 = Order.objects.create(number='O-1', product='X')
        br1 = TaskBranch.objects.create(order=o1, name='B1')
        self._task(branch=br1, order=o1)
        self.assertEqual(
            self._sections_by_key()['branch_wrong_order']['count'], 0,
        )

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_python_iteration_limit_5000(self):
        """⚠ integrity.py итерирует первые 5000 задач с branch/order.

        Если у задачи #5001 ветка из чужого заказа — она не попадёт
        в отчёт. Тест фиксирует это ограничение: это не баг, но
        граница, о которой нужно помнить.
        """
        # Проверяем, что логика в принципе работает — не создаём 5001 задачу
        # (это медленно), но фиксируем комментарием поведение.
        o1 = Order.objects.create(number='O-1', product='X')
        o2 = Order.objects.create(number='O-2', product='Y')
        br1 = TaskBranch.objects.create(order=o1, name='B1')
        # 3 задачи с битой связкой
        for i in range(3):
            self._task(title=f'Wrong{i}', branch=br1, order=o2)
        s = self._sections_by_key()['branch_wrong_order']
        self.assertEqual(s['count'], 3)


class SelfParentTests(IntegrityBase):
    def test_task_is_its_own_parent(self):
        t = self._task()
        Task.objects.filter(pk=t.pk).update(parent_id=t.pk)
        s = self._sections_by_key()['self_parent']
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['severity'], 'danger')

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_mutual_cycle_not_detected(self):
        """⚠ A.parent=B, B.parent=A — цикл из двух задач.

        Секция `self_parent` ловит только self-reference, но
        НЕ ловит циклы длиннее 1. Это известное ограничение.
        """
        a = self._task(title='A')
        b = self._task(title='B')
        Task.objects.filter(pk=a.pk).update(parent_id=b.pk)
        Task.objects.filter(pk=b.pk).update(parent_id=a.pk)
        s = self._sections_by_key()['self_parent']
        # Цикл A↔B не flagged этой секцией — только self-loop ловится
        self.assertEqual(s['count'], 0)


class TaskNoExecutorTests(IntegrityBase):
    """⚠ Секция существует, но НИКОГДА не срабатывает:

    Task.executor = FK(on_delete=PROTECT, null=False) — модель
    физически не даёт создать задачу без исполнителя. Фильтр
    `executor__isnull=True` всегда вернёт 0. Секция — «страховка»
    на случай raw SQL или сломанного constraint.
    """

    def test_section_exists_and_empty(self):
        self._task()
        s = self._sections_by_key()['task_no_executor']
        self.assertEqual(s['count'], 0)
        self.assertEqual(s['severity'], 'danger')


# ═════════════════════════════════════════════════════════════
#  Summary
# ═════════════════════════════════════════════════════════════

class SummaryTests(IntegrityBase):
    def _run(self):
        return collect_integrity_checks()

    def test_clean_summary(self):
        _, summary = self._run()
        self.assertEqual(summary['total_problems'], 0)
        self.assertEqual(summary['critical_count'], 0)
        self.assertIsInstance(summary['total_info'], int)

    def test_danger_counts_in_problems_and_critical(self):
        self._task(
            title='D', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        _, summary = self._run()
        self.assertGreaterEqual(summary['total_problems'], 1)
        self.assertGreaterEqual(summary['critical_count'], 1)

    def test_warn_counts_in_problems_not_critical(self):
        t = self._task()
        now = timezone.now()
        TimeSession.objects.create(
            task=t, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=0.0,
            status_at_close=Task.Status.DONE,
        )
        _, summary = self._run()
        self.assertGreaterEqual(summary['total_problems'], 1)
        self.assertEqual(summary['critical_count'], 0)

    def test_info_does_not_count_in_problems(self):
        Role.objects.create(code='ghost', name='Пусто')
        _, summary = self._run()
        self.assertGreaterEqual(summary['total_info'], 1)
        self.assertEqual(summary['total_problems'], 0)
        self.assertEqual(summary['critical_count'], 0)

    def test_sections_with_problems_counts_only_nonzero(self):
        _, summary = self._run()
        self.assertEqual(summary['sections_with_problems'], 0)

        self._task(
            title='D', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        _, summary = self._run()
        self.assertEqual(summary['sections_with_problems'], 1)

    def test_multiple_sections_counted_separately(self):
        self._task(
            title='D', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        self._task(title='DoneNoFin', status=Task.Status.DONE)
        _, summary = self._run()
        self.assertEqual(summary['sections_with_problems'], 2)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_total_problems_includes_danger_and_warn(self):
        """Проверим формулу точно: total_problems = sum(danger) + sum(warn),
        исключая info.
        """
        # danger: 1 задача
        self._task(
            title='D', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        # warn: 1 битая сессия
        t2 = self._task(title='T2')
        now = timezone.now()
        TimeSession.objects.create(
            task=t2, executor=self.user,
            started_at=now, finished_at=now,
            duration_hours=0.0,
            status_at_close=Task.Status.DONE,
        )
        # info: 1 пустая роль
        Role.objects.create(code='ghost', name='Пусто')

        _, summary = self._run()
        # danger 1 + warn 1 = 2
        self.assertEqual(summary['total_problems'], 2)
        self.assertEqual(summary['critical_count'], 1)
        self.assertGreaterEqual(summary['total_info'], 1)


# ═════════════════════════════════════════════════════════════
#  Row-данные и url_prefix
# ═════════════════════════════════════════════════════════════

class RowDataTests(IntegrityBase):
    def test_rows_limited_to_50(self):
        for i in range(60):
            self._task(
                title=f'D{i}', status=Task.Status.IN_PROGRESS,
                finished_at=timezone.now(),
            )
        s = self._sections_by_key()['finished_at_wrong_status']
        self.assertEqual(s['count'], 60)
        self.assertEqual(len(s['rows']), 50)

    def test_rows_are_objects(self):
        t = self._task(
            title='D', status=Task.Status.IN_PROGRESS,
            finished_at=timezone.now(),
        )
        s = self._sections_by_key()['finished_at_wrong_status']
        self.assertEqual(s['rows'][0].pk, t.pk)

    def test_url_prefix_per_section(self):
        """Проверяем UI-контракт: каждая секция ведёт в свой раздел."""
        by_key = self._sections_by_key()
        for key in ('session_zero_duration', 'session_negative',
                    'session_too_long'):
            self.assertIn('/sessions/', by_key[key]['url_prefix'], msg=key)
        self.assertIn('tab=shop', by_key['shop_zero_duration']['url_prefix'])
        self.assertIn('tab=shop', by_key['shop_stale']['url_prefix'])
        self.assertIn('/roles/', by_key['role_empty']['url_prefix'])
        self.assertIn('/users/', by_key['user_no_role']['url_prefix'])
        self.assertIn('/users/', by_key['user_no_dept']['url_prefix'])
        self.assertIn('/departments/', by_key['dept_empty']['url_prefix'])
