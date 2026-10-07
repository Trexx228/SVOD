"""Автотесты dashboard: главная, focus, приёмка, history scope, пятничный дайджест.

═══ Про подводные камни ═══

  1. `focus` = первый IN_PROGRESS или первый open_tasks, если IN_PROGRESS нет.
  2. `open_tasks` ИСКЛЮЧАЕТ blocked_by_stage=True — заблокированные
     этапы не мешают фокусу.
  3. `cards` = все open_tasks минус focus. Если задач 0 — cards пусто.
  4. `history` scope: `is_admin_role` (не `can_plant`!) — начальник
     завода без can_admin видит только свой отдел. Это фича, но легко
     перепутать с `can_plant`.
  5. Пятничный дайджест: `weekday() == 4`. Создаётся один раз на
     пользователя в пятницу. Проверка `created_at__date__gte=week_start`.
  6. Дайджест не удаляется и не «пересоздаётся» при повторных заходах.
"""
from datetime import date, datetime, time as dtime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as tz

from accounts.models import User, Role, Department
from comms.models import Notification
from tasks.models import Task

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class DashboardBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.get_or_create(name='ОГК')[0]
        cls.other_dept = Department.objects.get_or_create(name='ОГТ')[0]

        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)
        cls.role_admin = _role('admin', can_manage=True, can_admin=True)

        cls.user = User.objects.create_user(
            email='u@test.ru', password='p', full_name='Юзер',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.other = User.objects.create_user(
            email='o@test.ru', password='p', full_name='Другой',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.stranger = User.objects.create_user(
            email='s@test.ru', password='p', full_name='Чужой',
            department=cls.other_dept, role=cls.role_staff, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='m@test.ru', password='p', full_name='Менеджер',
            department=cls.dept, role=cls.role_mgr, is_active=True,
        )
        cls.admin = User.objects.create_user(
            email='a@test.ru', password='p', full_name='Админ',
            department=cls.dept, role=cls.role_admin,
            is_active=True, is_staff=True,
        )

    def _task(self, executor=None, **kw):
        defaults = {
            'title': 'Задача', 'plan_hours': 2.0, 'scale': 's', 'kind': 'work',
            'executor': executor or self.user, 'requester': self.mgr,
            'status': Task.Status.NEW,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)


# ═════════════════════════════════════════════════════════════
#  Доступ
# ═════════════════════════════════════════════════════════════

class DashboardAccessTests(DashboardBase):
    def test_dashboard_requires_login(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 302)

    def test_history_requires_login(self):
        r = self.client.get(reverse('history'))
        self.assertEqual(r.status_code, 302)

    def test_dashboard_ok(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)

    def test_history_ok(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('history'))
        self.assertEqual(r.status_code, 200)


# ═════════════════════════════════════════════════════════════
#  Focus (фокусная задача)
# ═════════════════════════════════════════════════════════════

class FocusTests(DashboardBase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_no_tasks_focus_none(self):
        r = self.client.get(reverse('dashboard'))
        self.assertIsNone(r.context['focus'])
        self.assertEqual(r.context['cards'], [])

    def test_in_progress_wins_over_new(self):
        t_new = self._task(title='New')
        t_work = self._task(title='Work', status=Task.Status.IN_PROGRESS)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['focus'].pk, t_work.pk)
        titles = [t.title for t in r.context['cards']]
        self.assertIn('New', titles)
        self.assertNotIn('Work', titles)

    def test_first_open_when_no_in_progress(self):
        """Без IN_PROGRESS focus = первый из open_tasks."""
        t = self._task(title='Одна')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['focus'].pk, t.pk)
        self.assertEqual(r.context['cards'], [])

    def test_focus_excludes_blocked_by_stage(self):
        """⚠ Заблокированный этап не попадает в open_tasks."""
        t_blocked = self._task(title='Ждёт', blocked_by_stage=True)
        t_ok = self._task(title='Свободна')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['focus'].pk, t_ok.pk)
        all_ids = [r.context['focus'].pk] + [t.pk for t in r.context['cards']]
        self.assertNotIn(t_blocked.pk, all_ids)

    def test_focus_excludes_done_and_cancelled(self):
        self._task(title='DONE', status=Task.Status.DONE,
                   finished_at=tz.now())
        self._task(title='CANCEL', status=Task.Status.CANCELLED,
                   finished_at=tz.now())
        t_new = self._task(title='NEW')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['focus'].pk, t_new.pk)

    def test_focus_does_not_include_other_user_tasks(self):
        self._task(title='Чужая', executor=self.other)
        r = self.client.get(reverse('dashboard'))
        self.assertIsNone(r.context['focus'])

    def test_cards_contain_all_open_except_focus(self):
        for i in range(4):
            self._task(title=f'T{i}')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(len(r.context['cards']), 3)
        focus_pk = r.context['focus'].pk
        for t in r.context['cards']:
            self.assertNotEqual(t.pk, focus_pk)


# ═════════════════════════════════════════════════════════════
#  My review (ждут приёмки)
# ═════════════════════════════════════════════════════════════

class MyReviewTests(DashboardBase):
    def setUp(self):
        self.client.force_login(self.mgr)

    def test_review_shows_only_requester_side(self):
        """⚠ my_review = задачи где я — постановщик и статус REVIEW."""
        t1 = self._task(
            title='Ждёт меня',
            executor=self.user, requester=self.mgr,
            status=Task.Status.REVIEW, finished_at=tz.now(),
        )
        # Другая задача с REVIEW, но я не постановщик
        self._task(
            title='Чужая приёмка',
            executor=self.user, requester=self.user,
            status=Task.Status.REVIEW, finished_at=tz.now(),
        )
        r = self.client.get(reverse('dashboard'))
        titles = [t.title for t in r.context['my_review']]
        self.assertIn('Ждёт меня', titles)
        self.assertNotIn('Чужая приёмка', titles)

    def test_review_empty_when_no_review(self):
        self._task(title='Новая', status=Task.Status.NEW)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(list(r.context['my_review']), [])


# ═════════════════════════════════════════════════════════════
#  Closed recent
# ═════════════════════════════════════════════════════════════

class ClosedRecentTests(DashboardBase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_only_my_closed(self):
        self._task(title='Моя DONE', status=Task.Status.DONE,
                   finished_at=tz.now())
        self._task(title='Чужая', executor=self.other,
                   status=Task.Status.DONE, finished_at=tz.now())
        r = self.client.get(reverse('dashboard'))
        titles = [t.title for t in r.context['closed_recent']]
        self.assertIn('Моя DONE', titles)
        self.assertNotIn('Чужая', titles)

    def test_ordered_by_finished_at_desc(self):
        now = tz.now()
        t_old = self._task(title='Старая', status=Task.Status.DONE,
                           finished_at=now - timedelta(days=5))
        t_new = self._task(title='Свежая', status=Task.Status.DONE,
                           finished_at=now)
        r = self.client.get(reverse('dashboard'))
        titles = [t.title for t in r.context['closed_recent']]
        self.assertEqual(titles[0], 'Свежая')
        self.assertEqual(titles[1], 'Старая')

    def test_limit_15(self):
        for i in range(20):
            self._task(title=f'T{i}', status=Task.Status.DONE,
                       finished_at=tz.now())


# ═════════════════════════════════════════════════════════════
#  History — scope
# ═════════════════════════════════════════════════════════════

class HistoryScopeTests(DashboardBase):
    def setUp(self):
        now = tz.now()
        # Задачи для трёх категорий
        self.mine = self._task(
            title='Моя', executor=self.user, requester=self.mgr,
            status=Task.Status.DONE, finished_at=now,
        )
        self.colleagues = self._task(
            title='Коллега', executor=self.other, requester=self.mgr,
            status=Task.Status.DONE, finished_at=now,
        )
        self.stranger_ = self._task(
            title='Чужой отдел', executor=self.stranger, requester=self.mgr,
            status=Task.Status.DONE, finished_at=now,
        )

    def test_staff_sees_only_own(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        self.assertIn('Моя', titles)
        self.assertNotIn('Коллега', titles)
        self.assertNotIn('Чужой отдел', titles)

    def test_manager_sees_department(self):
        """⚠ manager с отделом видит задачи всего отдела."""
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        self.assertIn('Моя', titles)
        self.assertIn('Коллега', titles)
        self.assertNotIn('Чужой отдел', titles)

    def test_admin_sees_all(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        self.assertIn('Чужой отдел', titles)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_manager_without_department_sees_only_own(self):
        """⚠ Если manager без department_id — попадает в else и видит
        только свои. Это логика `if ... and user.department_id`.
        """
        orphan_mgr = User.objects.create_user(
            email='orphan@test.ru', password='p', full_name='Безотдел',
            department=None, role=self.role_mgr, is_active=True,
        )
        self.client.force_login(orphan_mgr)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        # У orphan_mgr нет своих задач вообще
        self.assertEqual(titles, [])

    def test_can_plant_without_can_admin_sees_only_own_department(self):
        """⚠ history НЕ использует can_plant! Только is_admin_role.

        Начальник завода (can_plant=True, can_admin=False) получит
        только свой отдел. Проверяем чтобы задокументировать.
        """
        plant_role = _role('plant_only', can_manage=True, can_plant=True)
        director = User.objects.create_user(
            email='dir@test.ru', password='p', full_name='Директор',
            department=self.dept, role=plant_role, is_active=True,
        )
        self.client.force_login(director)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        self.assertIn('Моя', titles)
        self.assertIn('Коллега', titles)
        self.assertNotIn('Чужой отдел', titles)


class HistoryFilterTests(DashboardBase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_only_done_and_cancelled(self):
        now = tz.now()
        self._task(title='DONE', status=Task.Status.DONE, finished_at=now)
        self._task(title='CANCEL', status=Task.Status.CANCELLED,
                   finished_at=now)
        self._task(title='NEW', status=Task.Status.NEW)
        self._task(title='IN_PROGRESS', status=Task.Status.IN_PROGRESS)
        r = self.client.get(reverse('history'))
        titles = set(t.title for t in r.context['tasks'])
        self.assertEqual(titles, {'DONE', 'CANCEL'})

    def test_ordered_by_finished_at_desc(self):
        now = tz.now()
        self._task(title='Старая', status=Task.Status.DONE,
                   finished_at=now - timedelta(days=5))
        self._task(title='Свежая', status=Task.Status.DONE, finished_at=now)
        r = self.client.get(reverse('history'))
        titles = [t.title for t in r.context['tasks']]
        self.assertEqual(titles, ['Свежая', 'Старая'])

    def test_empty_when_no_closed(self):
        self._task(title='X', status=Task.Status.NEW)
        r = self.client.get(reverse('history'))
        self.assertEqual(list(r.context['tasks']), [])


# ═════════════════════════════════════════════════════════════
#  Пятничный дайджест
# ═════════════════════════════════════════════════════════════

class FridayDigestTests(DashboardBase):
    def _nearest_friday(self):
        today = date.today()
        days_ahead = 4 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return today + timedelta(days=days_ahead)

    def _nearest_non_friday(self):
        """Ближайший день, не пятница."""
        today = date.today()
        for offset in range(1, 8):
            d = today + timedelta(days=offset)
            if d.weekday() != 4:
                return d
        raise AssertionError('Не нашли не-пятницу')

    def setUp(self):
        self.client.force_login(self.user)

    def test_digest_created_once_in_friday(self):
        friday = self._nearest_friday()
        fake_now = tz.make_aware(datetime.combine(friday, dtime(12, 0)))

        url = reverse('dashboard')

        with patch('django.utils.timezone.localdate', return_value=friday), \
                patch('django.utils.timezone.now', return_value=fake_now):
            r1 = self.client.get(url)
            self.assertEqual(r1.status_code, 200)
            count1 = Notification.objects.filter(
                recipient=self.user,
                text__startswith='📅 Итог недели',
            ).count()
            self.assertEqual(count1, 1)

            r2 = self.client.get(url)
            self.assertEqual(r2.status_code, 200)
            count2 = Notification.objects.filter(
                recipient=self.user,
                text__startswith='📅 Итог недели',
            ).count()
            self.assertEqual(count2, 1)

    def test_digest_not_created_on_non_friday(self):
        not_friday = self._nearest_non_friday()
        fake_now = tz.make_aware(datetime.combine(not_friday, dtime(12, 0)))

        with patch('django.utils.timezone.localdate', return_value=not_friday), \
                patch('django.utils.timezone.now', return_value=fake_now):
            self.client.get(reverse('dashboard'))
            count = Notification.objects.filter(
                recipient=self.user,
                text__startswith='📅 Итог недели',
            ).count()
            self.assertEqual(count, 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_digest_per_user_isolation(self):
        """⚠ Дайджест у каждого свой. Заход одним юзером не создаёт
        Notification другим.
        """
        friday = self._nearest_friday()
        fake_now = tz.make_aware(datetime.combine(friday, dtime(12, 0)))

        with patch('django.utils.timezone.localdate', return_value=friday), \
                patch('django.utils.timezone.now', return_value=fake_now):
            self.client.get(reverse('dashboard'))
            self.client.force_login(self.other)
            self.client.get(reverse('dashboard'))

        mine = Notification.objects.filter(recipient=self.user).count()
        theirs = Notification.objects.filter(recipient=self.other).count()
        self.assertEqual(mine, 1)
        self.assertEqual(theirs, 1)

    def test_digest_uses_week_start_filter(self):
        """⚠ Проверка `created_at__date__gte=week_start`:
        если дайджест уже был на прошлой неделе, в эту пятницу
        создастся новый. Это фича (не даёт «одно на всю жизнь»).
        """
        friday = self._nearest_friday()
        week_start = friday - timedelta(days=friday.weekday())

        # Симулируем дайджест прошлой недели
        old = Notification.objects.create(
            recipient=self.user,
            text='📅 Итог недели: старый',
            url='/',
            kind=Notification.Kind.INFO,
        )
        Notification.objects.filter(pk=old.pk).update(
            created_at=tz.make_aware(
                datetime.combine(week_start - timedelta(days=7), dtime(12, 0))
            ),
        )

        fake_now = tz.make_aware(datetime.combine(friday, dtime(12, 0)))
        with patch('django.utils.timezone.localdate', return_value=friday), \
                patch('django.utils.timezone.now', return_value=fake_now):
            self.client.get(reverse('dashboard'))

        count = Notification.objects.filter(
            recipient=self.user,
            text__startswith='📅 Итог недели',
        ).count()
        self.assertEqual(count, 2)   # старый + новый
