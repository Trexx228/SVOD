"""Автотесты gamify: каталог, ачивки, streak, level, KPI, rating, motivation."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from gamify.models import Achievement, Kudos, Streak, UserAchievement
from gamify.services import (
    ACHIEVEMENTS, check_achievements,
    closed_tasks, ensure_catalog, level_info, manager_kpi_percent,
    touch_streak, user_kpi_percent,
)
from tasks.models import Task, TaskLog

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


# ═════════════════════════════════════════════════════════════
#  База
# ═════════════════════════════════════════════════════════════

class GamifyBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep = Department.objects.get_or_create(name='ОГК')[0]
        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)

        cls.user = User.objects.create_user(
            email='emp@test.ru', password='p', full_name='Иванов Иван',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='boss@test.ru', password='p', full_name='Руководитель',
            department=cls.dep, role=cls.role_mgr, is_active=True,
        )
        cls.other = User.objects.create_user(
            email='other@test.ru', password='p', full_name='Петров Пётр',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )

    def _task(self, executor=None, **kw):
        defaults = {
            'title': 'Задача', 'plan_hours': 2.0, 'scale': 's', 'kind': 'work',
            'executor': executor or self.user, 'requester': self.mgr,
            'status': Task.Status.DONE,
            'finished_at': timezone.now(),
            'accumulated_hours': 2.0,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)


# ═════════════════════════════════════════════════════════════
#  Каталог
# ═════════════════════════════════════════════════════════════

class EnsureCatalogTests(GamifyBase):
    def test_creates_all_achievements(self):
        Achievement.objects.all().delete()
        ensure_catalog()
        codes = set(Achievement.objects.values_list('code', flat=True))
        expected = {code for code, _, _, _ in ACHIEVEMENTS}
        self.assertEqual(codes, expected)

    def test_idempotent(self):
        Achievement.objects.all().delete()
        ensure_catalog()
        n1 = Achievement.objects.count()
        ensure_catalog()
        n2 = Achievement.objects.count()
        self.assertEqual(n1, n2)
        self.assertEqual(n1, len(ACHIEVEMENTS))

    def test_does_not_override_existing(self):
        Achievement.objects.all().delete()
        Achievement.objects.create(
            code='first', title='Custom title', icon='✨', description='custom',
        )
        ensure_catalog()
        a = Achievement.objects.get(code='first')
        self.assertEqual(a.title, 'Custom title')


# ═════════════════════════════════════════════════════════════
#  closed_tasks
# ═════════════════════════════════════════════════════════════

class ClosedTasksTests(GamifyBase):
    def test_returns_only_done(self):
        self._task(title='DONE', status=Task.Status.DONE,
                   finished_at=timezone.now())
        self._task(title='IN_PROGRESS', status=Task.Status.IN_PROGRESS,
                   finished_at=None)
        self._task(title='CANCELLED', status=Task.Status.CANCELLED,
                   finished_at=timezone.now())
        titles = list(closed_tasks(self.user).values_list('title', flat=True))
        self.assertEqual(titles, ['DONE'])

    def test_only_given_user(self):
        self._task(title='Моя')
        self._task(title='Чужая', executor=self.other)
        self.assertEqual(closed_tasks(self.user).count(), 1)

    def test_since_filter(self):
        now = timezone.now()
        self._task(title='Свежая', finished_at=now)
        self._task(title='Старая', finished_at=now - timedelta(days=60))
        qs = closed_tasks(self.user, since=now - timedelta(days=30))
        titles = list(qs.values_list('title', flat=True))
        self.assertEqual(titles, ['Свежая'])


# ═════════════════════════════════════════════════════════════
#  level_info
# ═════════════════════════════════════════════════════════════

class LevelInfoTests(GamifyBase):
    def test_zero_hours_is_trainee(self):
        info = level_info(self.user)
        self.assertEqual(info['name'], 'Стажёр')
        self.assertEqual(info['hours'], 0)
        self.assertEqual(info['progress'], 0)

    def test_partial_progress_to_specialist(self):
        self._task(accumulated_hours=10.0)
        info = level_info(self.user)
        self.assertEqual(info['name'], 'Стажёр')
        self.assertEqual(info['progress'], 50)

    def test_specialist_at_20_hours(self):
        self._task(accumulated_hours=20.0)
        self.assertEqual(level_info(self.user)['name'], 'Специалист')

    def test_profi_at_60(self):
        self._task(accumulated_hours=60.0)
        self.assertEqual(level_info(self.user)['name'], 'Профи')

    def test_master_at_150(self):
        self._task(accumulated_hours=150.0)
        info = level_info(self.user)
        self.assertEqual(info['name'], 'Мастер')
        self.assertEqual(info['progress'], 100)

    def test_max_level_capped_progress(self):
        self._task(accumulated_hours=1000.0)
        info = level_info(self.user)
        self.assertEqual(info['name'], 'Мастер')
        self.assertEqual(info['progress'], 100)

    def test_hours_sum_from_multiple_tasks(self):
        self._task(accumulated_hours=15.0)
        self._task(accumulated_hours=10.0)
        info = level_info(self.user)
        self.assertEqual(info['hours'], 25.0)
        self.assertEqual(info['name'], 'Специалист')


# ═════════════════════════════════════════════════════════════
#  touch_streak
# ═════════════════════════════════════════════════════════════

class TouchStreakTests(GamifyBase):
    def test_first_touch_sets_one(self):
        s = touch_streak(self.user)
        self.assertEqual(s.current_days, 1)
        self.assertEqual(s.best_days, 1)
        self.assertEqual(s.last_ok_date, timezone.localdate())

    def test_same_day_no_change(self):
        touch_streak(self.user)
        s2 = touch_streak(self.user)
        self.assertEqual(s2.current_days, 1)

    def test_next_day_increments(self):
        Streak.objects.create(
            user=self.user, current_days=3, best_days=3,
            last_ok_date=timezone.localdate() - timedelta(days=1),
        )
        s = touch_streak(self.user)
        self.assertEqual(s.current_days, 4)
        self.assertEqual(s.best_days, 4)

    def test_gap_resets_to_one_but_keeps_best(self):
        Streak.objects.create(
            user=self.user, current_days=10, best_days=10,
            last_ok_date=timezone.localdate() - timedelta(days=5),
        )
        s = touch_streak(self.user)
        self.assertEqual(s.current_days, 1)
        self.assertEqual(s.best_days, 10)


# ═════════════════════════════════════════════════════════════
#  user_kpi_percent — регресс на баг с int
# ═════════════════════════════════════════════════════════════

class UserKpiPercentTests(GamifyBase):
    def test_none_without_data(self):
        self.assertIsNone(user_kpi_percent(self.user))

    def test_100_percent(self):
        self._task(plan_hours=10.0, accumulated_hours=10.0)
        self.assertEqual(user_kpi_percent(self.user), 100)

    def test_200_percent_when_faster(self):
        self._task(plan_hours=10.0, accumulated_hours=5.0)
        self.assertEqual(user_kpi_percent(self.user), 200)

    def test_50_percent_when_slower(self):
        self._task(plan_hours=10.0, accumulated_hours=20.0)
        self.assertEqual(user_kpi_percent(self.user), 50)

    def test_none_when_fact_below_1h(self):
        self._task(plan_hours=10.0, accumulated_hours=0.5)
        self.assertIsNone(user_kpi_percent(self.user))

    def test_since_accepts_datetime(self):
        """Регресс: user_kpi_percent(user, 30) падал с TypeError."""
        now = timezone.now()
        self._task(plan_hours=10.0, accumulated_hours=10.0, finished_at=now)
        result = user_kpi_percent(self.user, since=now - timedelta(days=30))
        self.assertEqual(result, 100)

    def test_since_and_until_exclude_old(self):
        now = timezone.now()
        self._task(plan_hours=10.0, accumulated_hours=10.0,
                   finished_at=now - timedelta(days=100))
        result = user_kpi_percent(
            self.user,
            since=now - timedelta(days=30),
            until=now,
        )
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════
#  manager_kpi_percent
# ═════════════════════════════════════════════════════════════

class ManagerKpiPercentTests(GamifyBase):
    def test_none_without_department(self):
        no_dept_mgr = User.objects.create_user(
            email='nodept@test.ru', password='p', full_name='Безотдельный',
            role=self.role_mgr, is_active=True,
        )
        self.assertIsNone(manager_kpi_percent(no_dept_mgr))

    def test_composite_is_exactly_100_with_ideal_data(self):
        """Все компоненты идеальны → composite == 100 (не «около 100»)."""
        # личная задача руководителя
        self._task(executor=self.mgr, plan_hours=10.0, accumulated_hours=10.0)
        # задача сотрудника в его отделе
        self._task(executor=self.user, plan_hours=10.0, accumulated_hours=10.0)

        result = manager_kpi_percent(self.mgr)
        self.assertEqual(result, 100)

    def test_none_when_no_data_at_all(self):
        result = manager_kpi_percent(self.mgr)
        # Никаких задач → команда пустая, личных нет.
        # Если что-то доступно — вернётся int, но не должно быть «полного успеха».
        self.assertTrue(result is None or result == 0 or result < 100)


# ═════════════════════════════════════════════════════════════
#  check_achievements
# ═════════════════════════════════════════════════════════════

class CheckAchievementsTests(GamifyBase):
    def _codes(self, user=None):
        return set(
            UserAchievement.objects
            .filter(user=user or self.user)
            .values_list('achievement__code', flat=True)
        )

    # ── Защита от «молчаливого падения» ────────────────────
    def test_no_streak_row_does_not_crash(self):
        """check_achievements не должен падать, когда Streak ещё нет."""
        self._task()
        check_achievements(self.user)  # ключевое: не должно бросить
        # sanity-check: метод дошёл до конца и выдал 'first'
        self.assertIn('first', self._codes())
        # streak-ачивки не выданы
        self.assertNotIn('streak7', self._codes())
        self.assertNotIn('streak30', self._codes())

    # ── first ──────────────────────────────────────────────
    def test_first_awarded_on_first_closed_task(self):
        check_achievements(self.user)
        self.assertNotIn('first', self._codes())

        self._task()
        check_achievements(self.user)
        self.assertIn('first', self._codes())

    def test_first_awarded_only_once(self):
        self._task()
        check_achievements(self.user)
        check_achievements(self.user)
        self.assertEqual(
            UserAchievement.objects.filter(
                user=self.user, achievement__code='first',
            ).count(),
            1,
        )

    # ── speedy ─────────────────────────────────────────────
    def test_speedy_requires_three_fast_tasks_in_week(self):
        self._task(plan_hours=10.0, accumulated_hours=8.0)
        self._task(plan_hours=10.0, accumulated_hours=9.0)
        check_achievements(self.user)
        # sanity: функция дошла до конца
        self.assertIn('first', self._codes())
        self.assertNotIn('speedy', self._codes())

        self._task(plan_hours=10.0, accumulated_hours=5.0)
        check_achievements(self.user)
        self.assertIn('speedy', self._codes())

    def test_speedy_ignores_slow_tasks(self):
        for _ in range(3):
            self._task(plan_hours=5.0, accumulated_hours=10.0)
        check_achievements(self.user)
        self.assertIn('first', self._codes())
        self.assertNotIn('speedy', self._codes())

    # ── sniper ─────────────────────────────────────────────
    def test_sniper_requires_kpi_95_105(self):
        for _ in range(3):
            self._task(plan_hours=10.0, accumulated_hours=10.0)
        check_achievements(self.user)
        # KPI = 100 → в диапазоне
        self.assertIn('sniper', self._codes())

    def test_sniper_skipped_when_kpi_off(self):
        # KPI = 200% — вне 95–105
        for _ in range(3):
            self._task(plan_hours=10.0, accumulated_hours=5.0)
        check_achievements(self.user)
        self.assertIn('first', self._codes())
        self.assertNotIn('sniper', self._codes())

    # ── reliable ───────────────────────────────────────────
    def test_reliable_requires_five_and_no_rework(self):
        for _ in range(4):
            self._task()
        check_achievements(self.user)
        self.assertNotIn('reliable', self._codes())

        self._task()
        check_achievements(self.user)
        self.assertIn('reliable', self._codes())

    def test_reliable_blocked_by_rework(self):
        tasks = [self._task() for _ in range(5)]
        # rework-лог именно на одну из его задач
        TaskLog.objects.create(
            task=tasks[0], kind=TaskLog.Kind.REWORK, author=self.mgr,
        )
        check_achievements(self.user)
        self.assertNotIn('reliable', self._codes())

    # ── streak7 / streak30 ─────────────────────────────────
    def test_streak7_awarded(self):
        Streak.objects.create(
            user=self.user, current_days=7, best_days=7,
            last_ok_date=timezone.localdate(),
        )
        check_achievements(self.user)
        self.assertIn('streak7', self._codes())
        self.assertNotIn('streak30', self._codes())

    def test_streak30_awarded(self):
        Streak.objects.create(
            user=self.user, current_days=30, best_days=30,
            last_ok_date=timezone.localdate(),
        )
        check_achievements(self.user)
        self.assertIn('streak30', self._codes())


# ═════════════════════════════════════════════════════════════
#  Kudos — constraint
# ═════════════════════════════════════════════════════════════

class KudosTests(GamifyBase):
    def test_cannot_thank_self_via_clean(self):
        k = Kudos(from_user=self.user, to_user=self.user, text='Молодец')
        with self.assertRaises(ValidationError):
            k.full_clean()

    def test_can_thank_other(self):
        k = Kudos.objects.create(
            from_user=self.user, to_user=self.other, text='Спасибо',
        )
        self.assertIsNotNone(k.pk)

    def test_str_contains_arrow(self):
        k = Kudos.objects.create(
            from_user=self.user, to_user=self.other, text='X',
        )
        self.assertIn('→', str(k))


# ═════════════════════════════════════════════════════════════
#  UserAchievement — уникальность
# ═════════════════════════════════════════════════════════════

class UserAchievementUniqueTests(GamifyBase):
    def test_unique_per_user_and_achievement(self):
        a = Achievement.objects.create(code='x', title='X')
        UserAchievement.objects.create(user=self.user, achievement=a)
        with self.assertRaises(IntegrityError):
            UserAchievement.objects.create(user=self.user, achievement=a)

    def test_different_users_ok(self):
        a = Achievement.objects.create(code='x', title='X')
        UserAchievement.objects.create(user=self.user, achievement=a)
        UserAchievement.objects.create(user=self.other, achievement=a)
        self.assertEqual(UserAchievement.objects.filter(achievement=a).count(), 2)


# ═════════════════════════════════════════════════════════════
#  Views
# ═════════════════════════════════════════════════════════════

class MotivationViewTests(GamifyBase):
    """После патча 3 /motivation/ — редирект на /me/?tab=motivation.

    Раньше здесь были тесты контекста (level, kpi, achievements и т.д.).
    Теперь они в cabinet/tests.py::MeMotivationTests — там, где живёт
    реальная страница. Здесь только проверка редиректа.
    """

    def test_requires_login(self):
        r = self.client.get(reverse('motivation'))
        self.assertEqual(r.status_code, 302)
        # Анонимный уходит на login, потому что @login_required
        # перехватывает раньше редиректа. Проверять сам редирект
        # на /me/ будем под залогиненным.

    def test_redirects_to_cabinet(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('motivation'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/me/?tab=motivation', r['Location'])


class RatingViewTests(GamifyBase):
    """После патча 3 /rating/ — редирект на /me/?tab=motivation.

    Тесты контекста переехали в cabinet/tests.py::MeMotivationTests.
    """

    def test_requires_login(self):
        r = self.client.get(reverse('rating'))
        self.assertEqual(r.status_code, 302)

    def test_redirects_to_cabinet(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('rating'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/me/?tab=motivation', r['Location'])
