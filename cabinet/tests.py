"""Автотесты cabinet: главная, задачи, сессии, KPI, логи, настройки, профиль.

После патча 2.2 весь кабинет живёт на одном URL /me/ с ?tab=...
Старые URL'ы (/me/kpi/, /me/settings/ и т.д.) — редиректы, и Django
test client за ними НЕ следует. Поэтому все тесты бьют напрямую на
/me/?tab=..., а POST — на /me/?tab=settings.

CSV из KPI вынесен в отдельный URL me_kpi_export (/me/kpi/export.csv).
"""
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from tasks.models import ShopSession, Task, TaskLog, TimeSession

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


# ═════════════════════════════════════════════════════════════
#  База
# ═════════════════════════════════════════════════════════════

class CabinetBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.get_or_create(name='ОГК')[0]
        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)

        cls.user = User.objects.create_user(
            email='emp@test.ru', password='p', full_name='Иванов Иван',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.other = User.objects.create_user(
            email='other@test.ru', password='p', full_name='Петров Пётр',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.boss = User.objects.create_user(
            email='boss@test.ru', password='p', full_name='Босс',
            department=cls.dept, role=cls.role_mgr, is_active=True,
        )

    def _task(self, **kw):
        defaults = {
            'title': 'Задача', 'plan_hours': 2.0, 'scale': 's', 'kind': 'work',
            'executor': self.user, 'requester': self.boss,
            'status': Task.Status.NEW,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)

    def _work_session(self, task, hours=1.0, **kw):
        end = kw.pop('finished_at', timezone.now())
        start = end - timedelta(hours=hours)
        return TimeSession.objects.create(
            task=task, executor=task.executor,
            started_at=start, finished_at=end,
            duration_hours=hours,
            status_at_close=task.status,
            **kw,
        )


# ═════════════════════════════════════════════════════════════
#  Доступ
# ═════════════════════════════════════════════════════════════

class AccessTests(CabinetBase):
    """Анонимный редиректится, аутентифицированный — 200.

    Все табы — на одном URL /me/, различаются ?tab=….
    """

    URLS = [
        ('me_home', ''),               # overview по умолчанию
        ('me_home', '?tab=overview'),
        ('me_home', '?tab=tasks'),
        ('me_home', '?tab=sessions'),
        ('me_home', '?tab=kpi'),
        ('me_home', '?tab=motivation'),
        ('me_home', '?tab=logs'),
        ('me_home', '?tab=profile'),
        ('me_home', '?tab=settings'),
    ]

    def test_anonymous_redirected_to_login(self):
        for name, qs in self.URLS:
            r = self.client.get(reverse(name) + qs)
            self.assertEqual(r.status_code, 302, msg=name + qs)
            self.assertIn('/accounts/login/', r['Location'], msg=name + qs)

    def test_authenticated_ok(self):
        self.client.force_login(self.user)
        for name, qs in self.URLS:
            r = self.client.get(reverse(name) + qs)
            self.assertEqual(r.status_code, 200, msg=name + qs)


# ═════════════════════════════════════════════════════════════
#  me_home → overview
# ═════════════════════════════════════════════════════════════

class MeHomeTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_stats_counts(self):
        self._task(title='Новая', status=Task.Status.NEW)
        self._task(title='В работе', status=Task.Status.IN_PROGRESS)
        self._task(title='На проверке', status=Task.Status.REVIEW)
        self._task(title='Доработка', status=Task.Status.REWORK)
        self._task(title='Готово', status=Task.Status.DONE,
                   finished_at=timezone.now())
        yesterday = timezone.localdate() - timedelta(days=1)
        self._task(title='Просрочено', due=yesterday)

        r = self.client.get(reverse('me_home'))
        self.assertEqual(r.status_code, 200)
        s = r.context['task_stats']
        self.assertEqual(s['in_progress'], 1)
        self.assertEqual(s['review'], 1)
        self.assertEqual(s['rework'], 1)
        self.assertEqual(s['done'], 1)
        self.assertEqual(s['overdue'], 1)

    def test_hours_month(self):
        t1 = self._task(title='A')
        t2 = self._task(title='B')
        self._work_session(t1, hours=2.5)
        self._work_session(t2, hours=1.5)

        r = self.client.get(reverse('me_home'))
        self.assertEqual(r.context['work_hours_month'], 4.0)

    def test_shop_hours_month(self):
        t = self._task(title='C')
        s = ShopSession.objects.create(task=t, executor=self.user)
        ShopSession.objects.filter(pk=s.pk).update(
            finished_at=timezone.now(),
            duration_hours=3.0,
        )
        r = self.client.get(reverse('me_home'))
        self.assertEqual(r.context['shop_hours_month'], 3.0)

    def test_active_task_session(self):
        t = self._task(
            title='Таймер идёт',
            status=Task.Status.IN_PROGRESS,
            session_started_at=timezone.now(),
        )
        r = self.client.get(reverse('me_home'))
        self.assertEqual(r.context['active_task'].pk, t.pk)

    def test_active_shop_session(self):
        t = self._task(title='Цех')
        ShopSession.objects.create(task=t, executor=self.user)
        r = self.client.get(reverse('me_home'))
        self.assertIsNotNone(r.context['active_shop'])

    def test_last_work_sessions_limited(self):
        t = self._task()
        for _ in range(7):
            self._work_session(t, hours=1.0)
        r = self.client.get(reverse('me_home'))
        self.assertEqual(len(r.context['last_work']), 5)

    def test_last_tasks_limited(self):
        for i in range(10):
            self._task(title=f'Задача {i}')
        r = self.client.get(reverse('me_home'))
        self.assertEqual(len(r.context['last_tasks']), 8)

    def test_today_tasks_only_open_today(self):
        today = timezone.localdate()
        t1 = self._task(title='Сегодня открытая', due=today)
        self._task(title='Сегодня готовая', due=today,
                   status=Task.Status.DONE, finished_at=timezone.now())
        self._task(title='Завтра', due=today + timedelta(days=1))

        r = self.client.get(reverse('me_home'))
        titles = [t.title for t in r.context['today_tasks']]
        self.assertIn(t1.title, titles)
        self.assertNotIn('Сегодня готовая', titles)
        self.assertNotIn('Завтра', titles)

    def test_other_users_tasks_not_counted(self):
        self._task(title='Моя')
        Task.objects.create(
            title='Чужая', plan_hours=2.0, scale='s', kind='work',
            executor=self.other, requester=self.boss,
            status=Task.Status.IN_PROGRESS,
        )
        r = self.client.get(reverse('me_home'))
        self.assertEqual(r.context['task_stats']['in_progress'], 0)


# ═════════════════════════════════════════════════════════════
#  Таб «Мои задачи» /me/?tab=tasks
# ═════════════════════════════════════════════════════════════

class MeTasksTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)
        self.t1 = self._task(
            title='Чертёж', status=Task.Status.IN_PROGRESS,
            priority=Task.Priority.HIGH,
            due=timezone.localdate() + timedelta(days=1),
            body='Описание чертежа',
        )
        self.t2 = self._task(
            title='Спецификация', status=Task.Status.NEW,
            priority=Task.Priority.LOW,
            due=timezone.localdate() + timedelta(days=10),
        )

    def _get(self, **params):
        params.setdefault('tab', 'tasks')
        return self.client.get(reverse('me_home'), params)

    def _titles(self, r):
        return [t.title for t in r.context['page'].object_list]

    def test_only_own_tasks(self):
        Task.objects.create(
            title='Чужая', plan_hours=2.0, scale='s', kind='work',
            executor=self.other, requester=self.boss,
            status=Task.Status.NEW,
        )
        r = self._get()
        titles = self._titles(r)
        self.assertIn('Чертёж', titles)
        self.assertNotIn('Чужая', titles)

    def test_search_by_title(self):
        r = self._get(q='Чертёж')
        self.assertEqual(self._titles(r), ['Чертёж'])

    def test_search_by_body(self):
        r = self._get(q='Описание')
        self.assertEqual(self._titles(r), ['Чертёж'])

    def test_filter_status(self):
        r = self._get(status=Task.Status.IN_PROGRESS)
        self.assertEqual(self._titles(r), ['Чертёж'])

    def test_filter_priority(self):
        r = self._get(priority=Task.Priority.LOW)
        self.assertEqual(self._titles(r), ['Спецификация'])

    def test_filter_due_from(self):
        today = timezone.localdate()
        r = self._get(due_from=(today + timedelta(days=5)).isoformat())
        self.assertEqual(self._titles(r), ['Спецификация'])

    def test_filter_due_to(self):
        today = timezone.localdate()
        r = self._get(due_to=(today + timedelta(days=5)).isoformat())
        self.assertEqual(self._titles(r), ['Чертёж'])


# ═════════════════════════════════════════════════════════════
#  Таб «Мои часы» /me/?tab=sessions
# ═════════════════════════════════════════════════════════════

class MeSessionsTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'sessions')
        return self.client.get(reverse('me_home'), params)

    def test_tab_work_default(self):
        r = self._get()
        self.assertEqual(r.context['subtab'], 'work')

    def test_tab_shop(self):
        r = self._get(subtab='shop')
        self.assertEqual(r.context['subtab'], 'shop')

    def test_invalid_tab_defaults_to_work(self):
        r = self._get(subtab='xxx')
        self.assertEqual(r.context['subtab'], 'work')

    def test_work_sessions_listed_and_sum(self):
        t = self._task()
        self._work_session(t, hours=2.0)
        self._work_session(t, hours=3.0)
        r = self._get()
        self.assertEqual(r.context['total'], 2)
        self.assertEqual(r.context['total_hours'], 5.0)

    def test_shop_sessions_listed(self):
        t = self._task()
        s = ShopSession.objects.create(task=t, executor=self.user)
        ShopSession.objects.filter(pk=s.pk).update(
            finished_at=timezone.now(),
            duration_hours=1.5,
        )
        r = self._get(subtab='shop')
        self.assertEqual(r.context['total'], 1)
        self.assertEqual(r.context['total_hours'], 1.5)

    def test_other_users_sessions_not_listed(self):
        t_other = Task.objects.create(
            title='Чужая', plan_hours=2.0, scale='s', kind='work',
            executor=self.other, requester=self.boss,
            status=Task.Status.NEW,
        )
        end = timezone.now()
        TimeSession.objects.create(
            task=t_other, executor=self.other,
            started_at=end - timedelta(hours=1),
            finished_at=end, duration_hours=1.0,
            status_at_close=Task.Status.DONE,
        )
        r = self._get()
        self.assertEqual(r.context['total'], 0)

    def test_date_range_filter_excludes_old(self):
        t = self._task()
        old_date = timezone.now() - timedelta(days=60)
        self._work_session(t, hours=1.0, finished_at=old_date)
        # По умолчанию период — текущий месяц
        r = self._get()
        self.assertEqual(r.context['total'], 0)


# ═════════════════════════════════════════════════════════════
#  Таб «Мой KPI» /me/?tab=kpi
# ═════════════════════════════════════════════════════════════

class MeKpiTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'kpi')
        return self.client.get(reverse('me_home'), params)

    def test_summary_zero_when_no_data(self):
        r = self._get()
        self.assertEqual(r.context['summary']['tasks_done'], 0)
        self.assertEqual(r.context['summary']['plan_hours'], 0)

    def test_summary_counts_done(self):
        today = timezone.localdate()
        self._task(
            title='Готово 1', status=Task.Status.DONE,
            finished_at=timezone.now(),
            plan_hours=4.0, accumulated_hours=3.5,
            due=today + timedelta(days=1),
        )
        self._task(
            title='Готово 2', status=Task.Status.DONE,
            finished_at=timezone.now(),
            plan_hours=2.0, accumulated_hours=3.0,
            due=today - timedelta(days=1),
        )
        r = self._get()
        s = r.context['summary']
        self.assertEqual(s['tasks_done'], 2)
        self.assertEqual(s['plan_hours'], 6.0)
        self.assertEqual(s['fact_hours'], 6.5)
        self.assertEqual(s['on_time'], 1)
        self.assertEqual(s['overdue'], 1)

    def test_only_done_in_period(self):
        old = timezone.now() - timedelta(days=180)
        self._task(
            title='Старая', status=Task.Status.DONE,
            finished_at=old, plan_hours=10.0, accumulated_hours=10.0,
        )
        r = self._get()
        self.assertEqual(r.context['summary']['tasks_done'], 0)

    def test_efficiency_calculated(self):
        self._task(
            title='Готово', status=Task.Status.DONE,
            finished_at=timezone.now(),
            plan_hours=10.0, accumulated_hours=5.0,
        )
        r = self._get()
        # efficiency = 100 * plan / fact = 200
        self.assertEqual(r.context['summary']['efficiency'], 200)

    def test_csv_export(self):
        self._task(
            title='Готово', status=Task.Status.DONE,
            finished_at=timezone.now(),
            plan_hours=4.0, accumulated_hours=4.0,
        )
        r = self.client.get(reverse('me_kpi_export'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))
        body = r.content.decode('utf-8-sig')
        self.assertIn('Показатель', body)
        self.assertIn('Задач готово', body)
        self.assertIn('Сотрудник', body)


# ═════════════════════════════════════════════════════════════
#  Таб «Мои действия» /me/?tab=logs
# ═════════════════════════════════════════════════════════════

class MeLogsTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'logs')
        return self.client.get(reverse('me_home'), params)

    def test_empty(self):
        r = self._get()
        self.assertEqual(r.context['total'], 0)

    def test_listed(self):
        t = self._task()
        TaskLog.objects.create(
            task=t, kind=TaskLog.Kind.PLAN, author=self.user,
            comment='план изменён',
        )
        r = self._get()
        self.assertEqual(r.context['total'], 1)

    def test_filter_by_kind(self):
        t = self._task()
        TaskLog.objects.create(task=t, kind=TaskLog.Kind.PLAN, author=self.user)
        TaskLog.objects.create(task=t, kind=TaskLog.Kind.DUE, author=self.user)
        r = self._get(kind='plan')
        self.assertEqual(r.context['total'], 1)

    def test_only_own_logs(self):
        t = self._task()
        TaskLog.objects.create(
            task=t, kind=TaskLog.Kind.PLAN, author=self.boss,
        )
        r = self._get()
        self.assertEqual(r.context['total'], 0)

    def test_kind_stats_present(self):
        t = self._task()
        TaskLog.objects.create(task=t, kind=TaskLog.Kind.PLAN, author=self.user)
        TaskLog.objects.create(task=t, kind=TaskLog.Kind.PLAN, author=self.user)
        TaskLog.objects.create(task=t, kind=TaskLog.Kind.DUE, author=self.user)
        r = self._get()
        stats = r.context['kind_stats']
        self.assertEqual(len(stats), 2)
        plan_row = [s for s in stats if s['code'] == 'plan'][0]
        self.assertEqual(plan_row['count'], 2)


# ═════════════════════════════════════════════════════════════
#  Таб «Настройки» /me/?tab=settings
# ═════════════════════════════════════════════════════════════

class MeSettingsTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def _get(self):
        return self.client.get(reverse('me_home'), {'tab': 'settings'})

    def _post(self, data):
        return self.client.post(reverse('me_home') + '?tab=settings', data)

    def test_get_ok(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)

    def test_theme_change(self):
        self._post({
            'theme': 'dark',
            'font_size': self.user.font_size,
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, 'dark')

    def test_invalid_theme_ignored(self):
        self.user.theme = 'dark'
        self.user.save()
        self._post({'theme': 'nonsense'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, 'dark')

    def test_font_size_change(self):
        self._post({
            'theme': self.user.theme,
            'font_size': 'l',
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.font_size, 'l')

    def test_sound_on(self):
        self.user.sound_on = False
        self.user.save()
        self._post({
            'theme': self.user.theme,
            'sound_on': '1',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.sound_on)

    def test_sound_off(self):
        self.user.sound_on = True
        self.user.save()
        self._post({
            'theme': self.user.theme,
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.sound_on)

    def test_email_notifications_toggle(self):
        self.assertTrue(self.user.email_notifications)
        self._post({
            'theme': self.user.theme,
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_notifications)

    def test_email_digest_toggle(self):
        self.assertFalse(self.user.email_digest_daily)
        self._post({
            'theme': self.user.theme,
            'email_digest_daily': '1',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_digest_daily)

    def test_bg_style_custom_to_solid(self):
        """Legacy 'custom' должен превратиться в 'solid'."""
        self._post({
            'theme': self.user.theme,
            'bg_style': 'custom',
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.bg_style, 'solid')

    def test_bg_color_saved_when_used(self):
        self._post({
            'theme': self.user.theme,
            'use_bg_color': '1',
            'bg_color': '#aabbcc',
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.bg_color, '#aabbcc')

    def test_bg_color_cleared_when_unused(self):
        self.user.bg_color = '#aabbcc'
        self.user.save()
        self._post({
            'theme': self.user.theme,
            # use_bg_color не передан → фон сбросить
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.bg_color, '')


# ═════════════════════════════════════════════════════════════
#  Таб «Настройки» — загрузка файла фона
# ═════════════════════════════════════════════════════════════

class MeSettingsUploadTests(CabinetBase):
    """Тесты загрузки картинки фона (с временным MEDIA_ROOT)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp_media = tempfile.mkdtemp(prefix='svod-test-media-')
        cls._override = override_settings(MEDIA_ROOT=cls._tmp_media)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        shutil.rmtree(cls._tmp_media, ignore_errors=True)

    def setUp(self):
        self.client.force_login(self.user)

    def _post(self, data):
        return self.client.post(reverse('me_home') + '?tab=settings', data)

    def test_reject_oversize(self):
        big = SimpleUploadedFile(
            'big.png', b'a' * (5 * 1024 * 1024 + 1),
            content_type='image/png',
                       )
        self._post({
            'theme': self.user.theme,
            'bg_image': big,
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.bg_image)

    def test_reject_non_image(self):
        bad = SimpleUploadedFile(
            'script.sh', b'#!/bin/sh\necho hi',
            content_type='application/x-sh',
        )
        self._post({
            'theme': self.user.theme,
            'bg_image': bad,
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.bg_image)

    def test_accept_png(self):
        png = SimpleUploadedFile(
            'wall.png', b'\x89PNG\r\n\x1a\n' + b'a' * 100,
            content_type='image/png',
                        )
        self._post({
            'theme': self.user.theme,
            'bg_image': png,
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.bg_image)
        self.assertEqual(self.user.bg_style, 'image')
        self.assertEqual(self.user.bg_color, '')

    def test_bg_image_clear(self):
        png = SimpleUploadedFile(
            'wall.png', b'\x89PNG\r\n\x1a\n' + b'a' * 100,
            content_type='image/png',
                        )
        self._post({
            'theme': self.user.theme,
            'bg_image': png,
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.bg_image)

        self._post({
            'theme': self.user.theme,
            'bg_image_clear': '1',
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.bg_image)
        self.assertEqual(self.user.bg_style, 'grad')


# ═════════════════════════════════════════════════════════════
#  Таб «Профиль» /me/?tab=profile
# ═════════════════════════════════════════════════════════════

class MeProfileTests(CabinetBase):
    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'profile')
        return self.client.get(reverse('me_home'), params)

    def test_get_ok(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['profile_user'].pk, self.user.pk)

    def test_stats_counts(self):
        self._task(
            title='Готово в этом месяце',
            status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        r = self._get()
        s = r.context['stats']
        self.assertEqual(s['tasks_total'], 1)
        self.assertEqual(s['tasks_done_month'], 1)

    def test_login_events_listed(self):
        from admin_panel.models import LoginEvent

        initial_ok = LoginEvent.objects.filter(
            user=self.user, success=True,
        ).count()

        LoginEvent.objects.create(
            user=self.user, email_attempted=self.user.email,
            success=True, ip='127.0.0.1',
        )

        r = self._get()
        self.assertEqual(len(r.context['logins']), initial_ok + 1)
        self.assertEqual(r.context['logins_ok'], initial_ok + 1)
        self.assertEqual(r.context['logins_failed'], 0)

    def test_login_events_includes_failure(self):
        from admin_panel.models import LoginEvent

        LoginEvent.objects.create(
            user=self.user,
            email_attempted=self.user.email,
            success=False, ip='10.0.0.1',
        )
        LoginEvent.objects.create(
            user=None,
            email_attempted=self.user.email,
            success=False, ip='10.0.0.2',
        )

        r = self._get()
        self.assertGreaterEqual(r.context['logins_failed'], 2)


class MeProfileOvertimeTests(CabinetBase):
    """Сверхурочные в профиле: stats + история + пагинация."""

    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'profile')
        return self.client.get(reverse('me_home'), params)

    def _rec(self, **kw):
        from tasks.models import OvertimeRecord
        from accounts.models import Department

        today = timezone.localdate()
        defaults = {
            'user': self.user,
            'department': Department.objects.get(name='ОГК'),
            'date': today,
            'hours': 2.5,
            'reason': 'Ремонт',
            'created_by': self.boss,
        }
        defaults.update(kw)
        return OvertimeRecord.objects.create(**defaults)

    def test_no_overtime_means_empty_stats(self):
        r = self._get()
        self.assertEqual(r.context['stats']['overtime_count_total'], 0)
        self.assertEqual(r.context['stats']['overtime_hours_total'], 0)
        self.assertNotContains(r, 'Мои сверхурочные')

    def test_stats_show_current_month(self):
        today = timezone.localdate()
        self._rec(date=today, hours=3.0)
        self._rec(date=today, hours=1.5)

        r = self._get()
        self.assertEqual(r.context['stats']['overtime_count_month'], 2)
        self.assertEqual(r.context['stats']['overtime_hours_month'], 4.5)
        self.assertEqual(r.context['stats']['overtime_count_total'], 2)
        self.assertEqual(r.context['stats']['overtime_hours_total'], 4.5)
        self.assertContains(r, 'Мои сверхурочные')

    def test_stats_split_month_and_all_time(self):
        """Записи за прошлый месяц идут в total, не в month."""
        today = timezone.localdate()
        last_month_date = (today.replace(day=1) - timedelta(days=1))

        self._rec(date=today, hours=2.0)
        self._rec(date=last_month_date, hours=5.0)

        r = self._get()
        self.assertEqual(r.context['stats']['overtime_count_month'], 1)
        self.assertEqual(r.context['stats']['overtime_hours_month'], 2.0)
        self.assertEqual(r.context['stats']['overtime_count_total'], 2)
        self.assertEqual(r.context['stats']['overtime_hours_total'], 7.0)

    def test_other_user_overtime_not_shown(self):
        """Не видим чужих сверхурочных."""
        from tasks.models import OvertimeRecord
        from accounts.models import Department

        OvertimeRecord.objects.create(
            user=self.other,
            department=Department.objects.get(name='ОГК'),
            date=timezone.localdate(),
            hours=99.0,
            created_by=self.boss,
        )

        r = self._get()
        self.assertEqual(r.context['stats']['overtime_count_total'], 0)
        self.assertEqual(r.context['stats']['overtime_hours_total'], 0)

    def test_history_pagination(self):
        """31 запись → 2 страницы."""
        today = timezone.localdate()

        for i in range(31):
            self._rec(date=today - timedelta(days=i), hours=1.0)

        r = self._get()
        page = r.context['overtime_page']
        self.assertEqual(page.paginator.num_pages, 2)
        self.assertEqual(len(page.object_list), 30)

        r2 = self._get(overtime_page=2)
        page2 = r2.context['overtime_page']
        self.assertEqual(len(page2.object_list), 1)

# ═════════════════════════════════════════════════════════════
#  Таб «Мотивация» /me/?tab=motivation
# ═════════════════════════════════════════════════════════════

class MeMotivationTests(CabinetBase):
    """Тесты таба «Мотивация»: KPI, уровень, стрик, ачивки, рейтинг.

    После патча 3 сюда переехали старые MotivationViewTests и
    RatingViewTests. Логика не изменилась — источник данных тот же.
    """

    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, **params):
        params.setdefault('tab', 'motivation')
        return self.client.get(reverse('me_home'), params)

    def test_ok_empty_data(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['level']['name'], 'Стажёр')
        self.assertIsNone(r.context['kpi'])
        self.assertEqual(r.context['rating_rows'], [])

    def test_context_keys_present(self):
        r = self._get()
        for key in (
                'level', 'levels', 'kpi', 'closed30', 'streak',
                'achievements', 'kudos_in', 'kudos_out', 'days',
                'rating_rows',
        ):
            self.assertIn(key, r.context, msg=key)

    def test_achievements_catalog_created_on_visit(self):
        from gamify.models import Achievement
        from gamify.services import ACHIEVEMENTS

        Achievement.objects.all().delete()
        self._get()
        self.assertEqual(Achievement.objects.count(), len(ACHIEVEMENTS))

    def test_kudos_in_and_out(self):
        from gamify.models import Kudos

        Kudos.objects.create(from_user=self.other, to_user=self.user, text='Спасибо')
        Kudos.objects.create(from_user=self.user, to_user=self.other, text='Взаимно')

        r = self._get()
        self.assertEqual(len(r.context['kudos_in']), 1)
        self.assertEqual(len(r.context['kudos_out']), 1)

    def test_levels_list_marks_current(self):
        # level_info считает только DONE-задачи, поэтому закрываем
        self._task(
            accumulated_hours=25.0,
            status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        r = self._get()
        current = [lv for lv in r.context['levels'] if lv['current']]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['name'], 'Специалист')

    def test_rating_empty(self):
        r = self._get()
        self.assertEqual(len(r.context['rating_rows']), 0)

    def test_rating_medals_top_three(self):
        # 3 закрытых задачи у user, 2 у other, 1 у boss
        for _ in range(3):
            self._task(executor=self.user, plan_hours=1.0,
                       accumulated_hours=1.0,
                       status=Task.Status.DONE,
                       finished_at=timezone.now())
        for _ in range(2):
            self._task(executor=self.other, plan_hours=1.0,
                       accumulated_hours=1.0,
                       status=Task.Status.DONE,
                       finished_at=timezone.now())
        self._task(executor=self.boss, plan_hours=1.0,
                   accumulated_hours=1.0,
                   status=Task.Status.DONE,
                   finished_at=timezone.now())

        r = self._get()
        rows = r.context['rating_rows']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['place'], '🥇')
        self.assertEqual(rows[1]['place'], '🥈')
        self.assertEqual(rows[2]['place'], '🥉')

    def test_rating_only_current_month(self):
        old = timezone.now() - timedelta(days=90)
        self._task(
            status=Task.Status.DONE,
            finished_at=old,
            accumulated_hours=10.0,
        )
        r = self._get()
        self.assertEqual(len(r.context['rating_rows']), 0)

    def test_rating_kpi_in_row(self):
        self._task(
            status=Task.Status.DONE,
            finished_at=timezone.now(),
            plan_hours=10.0,
            accumulated_hours=10.0,
        )
        r = self._get()
        self.assertEqual(r.context['rating_rows'][0]['kpi'], 100)
