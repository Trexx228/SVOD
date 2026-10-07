"""Автотесты accounts: аутентификация, регистрация, scope, роли."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, Role

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class AuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = _role('staff')
        cls.dept = Department.objects.create(name='ОГК')
        cls.user = User.objects.create_user(
            email='u@eag.su', password='secret123', full_name='U',
            is_active=True, department=cls.dept, role=cls.role,
        )

    def test_login_success(self):
        ok = self.client.login(username='u@eag.su', password='secret123')
        self.assertTrue(ok)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)

    def test_login_wrong_password(self):
        ok = self.client.login(username='u@eag.su', password='wrong')
        self.assertFalse(ok)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        ok = self.client.login(username='u@eag.su', password='secret123')
        self.assertFalse(ok)

    def test_logout_redirects(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse('logout'))
        self.assertIn(r.status_code, (200, 302))

    def test_anonymous_redirected_from_profile(self):
        r = self.client.get(reverse('profile'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/accounts/login/', r['Location'])


class RegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = _role('staff')

    def test_registration_closed_by_default(self):
        from django.conf import settings
        if getattr(settings, 'REGISTRATION_OPEN', False):
            self.skipTest('регистрация открыта')
        r = self.client.get(reverse('register'))
        self.assertEqual(r.status_code, 302)

    def test_email_domain_validation(self):
        from accounts.forms import RegistrationForm
        form = RegistrationForm(data={
            'email': 'x@gmail.com',
            'full_name': 'X',
            'department_hint': 'ОГК',
            'password1': 'superSecret999',
            'password2': 'superSecret999',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)


class ScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_role = _role('admin', can_manage=True, can_admin=True, can_plant=True)
        cls.manager_role = _role('manager', can_manage=True)
        cls.plant_role = _role('plant', can_manage=True, can_plant=True)
        cls.staff_role = _role('staff')
        cls.dept = Department.objects.create(name='ОГК')

    def _mk(self, email, role):
        return User.objects.create_user(
            email=email, password='x', full_name=email,
            is_active=True, department=self.dept, role=role,
        )

    def test_scope_engineer(self):
        self.assertEqual(self._mk('a@eag.su', self.staff_role).scope, 'self')

    def test_scope_manager(self):
        self.assertEqual(self._mk('b@eag.su', self.manager_role).scope, 'department')

    def test_scope_plant(self):
        self.assertEqual(self._mk('c@eag.su', self.plant_role).scope, 'plant')

    def test_scope_admin(self):
        self.assertEqual(self._mk('d@eag.su', self.admin_role).scope, 'admin')

    def test_is_boss(self):
        m = self._mk('e@eag.su', self.manager_role)
        self.assertTrue(m.is_boss)

    def test_staff_not_boss(self):
        s = self._mk('f@eag.su', self.staff_role)
        self.assertFalse(s.is_boss)


class TourAndOnboardingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = _role('staff')
        cls.user = User.objects.create_user(
            email='t@eag.su', password='x', full_name='Т', is_active=True,
            role=cls.role,
        )

    def test_tour_mark_seen_requires_post(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('tour_mark_seen', args=['dashboard']))
        self.assertEqual(r.status_code, 405)

    def test_tour_mark_seen_writes_json(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse('tour_mark_seen', args=['dashboard']))
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertIn('dashboard', self.user.tour_seen)

    def test_tour_reset_clears(self):
        self.user.tour_seen = {'dashboard': '2026-01-01T00:00:00'}
        self.user.onboarding_done = True
        self.user.save()

        self.client.force_login(self.user)
        r = self.client.post(reverse('tour_reset'))
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tour_seen, {})
        self.assertFalse(self.user.onboarding_done)

    def test_onboarding_next_starts_at_zero(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('onboarding_next'))
        data = r.json()
        self.assertEqual(data['done'], 0)
        self.assertEqual(data['total'], 6)
        self.assertFalse(data['is_done'])
        self.assertIsNotNone(data['next'])

    def test_onboarding_next_after_all_seen(self):
        from accounts.onboarding import ONBOARDING_ROUTE
        self.user.tour_seen = {step[0]: '2026-01-01T00:00:00' for step in ONBOARDING_ROUTE}
        self.user.save()

        self.client.force_login(self.user)
        data = self.client.get(reverse('onboarding_next')).json()
        self.assertTrue(data['is_done'])
        self.assertIsNone(data['next'])

    def test_onboarding_finish_sets_flag(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse('onboarding_finish'))
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.onboarding_done)

    def test_onboarding_start_redirects(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('onboarding_start'))
        self.assertEqual(r.status_code, 302)


class EmploymentStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = _role('staff')
        cls.user = User.objects.create_user(
            email='e@eag.su', password='x', full_name='Уволенный',
            is_active=True, role=cls.role,
        )

    def test_default_status_is_active(self):
        self.assertEqual(
            self.user.employment_status,
            User.EmploymentStatus.ACTIVE,
        )
        self.assertTrue(self.user.is_working)
        self.assertFalse(self.user.is_dismissed)

    def test_dismiss_sets_fields(self):
        self.user.dismiss(reason='Тест', dismissed_at=None)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(
            self.user.employment_status,
            User.EmploymentStatus.DISMISSED,
        )
        self.assertEqual(self.user.dismissed_reason, 'Тест')
        self.assertIsNotNone(self.user.dismissed_at)
        self.assertTrue(self.user.is_dismissed)
        self.assertFalse(self.user.is_working)

    def test_restore_from_dismissal(self):
        self.user.dismiss(reason='Тест')
        self.user.restore_from_dismissal()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(
            self.user.employment_status,
            User.EmploymentStatus.ACTIVE,
        )
        self.assertIsNone(self.user.dismissed_at)
        self.assertEqual(self.user.dismissed_reason, '')

    def test_open_tasks_returns_only_open(self):
        from tasks.models import Task
        from django.utils import timezone

        t1 = Task.objects.create(
            title='Открытая', plan_hours=1, scale='s', kind='work',
            executor=self.user, requester=self.user, status=Task.Status.NEW,
        )
        Task.objects.create(
            title='Закрытая', plan_hours=1, scale='s', kind='work',
            executor=self.user, requester=self.user,
            status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        self.assertEqual(list(self.user.open_tasks()), [t1])

# ═════════════════════════════════════════════════════════════
#  Шапка: набор пунктов зависит от прав
# ═════════════════════════════════════════════════════════════

class NavigationRoleTests(TestCase):
    """base.html показывает разные пункты шапки в зависимости от прав.

    Проверяем по href'ам: тесты должны быть устойчивы к переименованию
    текстов, но URL — это контракт навигации.

    Открываем страницу /me/ (личный кабинет): там точно нет ссылок
    на /manager/ и /settings/ в контенте, значит любой href="/manager/..."
    в HTML пришёл только из шапки.
    """

    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='NAV-ОГК')

        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)
        cls.role_plant = _role('plant_head', can_manage=True, can_plant=True)
        cls.role_admin = _role('admin_nav', can_manage=True, can_admin=True)
        cls.role_dev = _role('dev_only', is_developer=True)

        cls.staff = User.objects.create_user(
            email='nav-staff@test.ru', password='p', full_name='Сотрудник',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='nav-mgr@test.ru', password='p', full_name='Нач. отдела',
            department=cls.dept, role=cls.role_mgr, is_active=True,
        )
        cls.plant = User.objects.create_user(
            email='nav-plant@test.ru', password='p', full_name='Директор',
            department=cls.dept, role=cls.role_plant, is_active=True,
        )
        cls.admin = User.objects.create_user(
            email='nav-admin@test.ru', password='p', full_name='Админ',
            department=cls.dept, role=cls.role_admin, is_active=True,
        )
        cls.developer = User.objects.create_user(
            email='nav-dev@test.ru', password='p', full_name='Разработчик',
            department=cls.dept, role=cls.role_dev, is_active=True,
        )

    def _html(self, user):
        self.client.force_login(user)
        r = self.client.get('/me/')
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # ── Базовый набор у всех ──

    def test_base_items_visible_for_staff(self):
        html = self._html(self.staff)
        self.assertIn('href="/"', html)
        self.assertIn('href="/me/"', html)
        self.assertIn('href="/tasks/create/"', html)
        # После патча 4.3 реестр задач — единый пункт шапки.
        # Внутри реестра — переключатель дерево / хронология / календарь.
        self.assertIn('href="/tasks/?view=tree"', html)
        self.assertNotIn('href="/tasks/?view=calendar"', html)

    def test_base_items_visible_for_admin_too(self):
        """Даже у админа базовые пункты на месте."""
        html = self._html(self.admin)
        self.assertIn('href="/tasks/create/"', html)
        self.assertIn('href="/tasks/?view=tree"', html)

    # ── Кабинет руководителя ──

    def test_staff_does_not_see_manager_cabinet(self):
        html = self._html(self.staff)
        self.assertNotIn('href="/manager/cabinet/"', html)

    def test_manager_sees_cabinet(self):
        html = self._html(self.mgr)
        self.assertIn('href="/manager/cabinet/"', html)

    def test_admin_sees_cabinet(self):
        """Админ — это тоже boss, кабинет ему нужен."""
        html = self._html(self.admin)
        self.assertIn('href="/manager/cabinet/"', html)

    # ── Свод по заводу и оперативный монитор ──

    def test_manager_does_not_see_plant(self):
        html = self._html(self.mgr)
        self.assertNotIn('href="/manager/plant/"', html)

    def test_plant_sees_plant_and_live(self):
        html = self._html(self.plant)
        self.assertIn('href="/manager/plant/"', html)
        self.assertIn('href="/manager/plant/live/"', html)

    def test_admin_sees_plant(self):
        """can_admin тоже даёт can_plant."""
        html = self._html(self.admin)
        self.assertIn('href="/manager/plant/"', html)

    # ── Администрирование ──

    def test_staff_does_not_see_settings(self):
        html = self._html(self.staff)
        self.assertNotIn('href="/settings/"', html)

    def test_manager_does_not_see_settings(self):
        html = self._html(self.mgr)
        self.assertNotIn('href="/settings/"', html)

    def test_admin_sees_settings(self):
        html = self._html(self.admin)
        self.assertIn('href="/settings/"', html)

    # ── Разработчик ──

    def test_developer_does_not_see_settings(self):
        """Разработчик без admin не должен видеть админку."""
        html = self._html(self.developer)
        self.assertNotIn('href="/settings/"', html)

    def test_developer_does_not_see_manager_cabinet(self):
        """Разработчик без can_manage не видит кабинет руководителя."""
        html = self._html(self.developer)
        self.assertNotIn('href="/manager/cabinet/"', html)


class UserIsDeveloperPropertyTests(TestCase):
    """Property User.is_developer — когда True, когда False."""

    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='DEV-ОГК')
        cls.role_staff = _role('staff')
        cls.role_dev = _role('dev_only', is_developer=True)
        cls.role_admin = _role('admin_dev', can_admin=True)
        cls.role_mgr = _role('manager_dev', can_manage=True)

    def _mk(self, email, role=None, **kw):
        return User.objects.create_user(
            email=email, password='p', full_name=email,
            department=self.dept, role=role, is_active=True, **kw,
        )

    def test_staff_is_not_developer(self):
        u = self._mk('s@test.ru', role=self.role_staff)
        self.assertFalse(u.is_developer)

    def test_dev_role_is_developer(self):
        u = self._mk('d@test.ru', role=self.role_dev)
        self.assertTrue(u.is_developer)

    def test_admin_is_developer(self):
        u = self._mk('a@test.ru', role=self.role_admin)
        self.assertTrue(u.is_developer)

    def test_manager_without_flag_is_not_developer(self):
        u = self._mk('m@test.ru', role=self.role_mgr)
        self.assertFalse(u.is_developer)

    def test_superuser_is_developer(self):
        root = User.objects.create_superuser(
            email='root-dev@test.ru', password='x',
        )
        self.assertTrue(root.is_developer)

    def test_no_role_is_not_developer(self):
        u = self._mk('none@test.ru', role=None)
        self.assertFalse(u.is_developer)
