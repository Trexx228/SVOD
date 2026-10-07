"""Автотесты tasks: создание, действия, urgent-сдвиг, дерево, timeline."""
import unittest
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from tasks.models import Task, TaskLog
from tasks.views import _urgent_shift_days

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class BaseTasksTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='ОГК')
        cls.staff_role = _role('staff')
        cls.admin_role = _role('admin', can_manage=True, can_admin=True)

        cls.executor = User.objects.create_user(
            email='exec@eag.su', password='x', full_name='Исполнитель',
            is_active=True, department=cls.dept, role=cls.staff_role,
        )
        cls.requester = User.objects.create_user(
            email='req@eag.su', password='x', full_name='Постановщик',
            is_active=True, department=cls.dept, role=cls.staff_role,
        )
        cls.admin = User.objects.create_user(
            email='adm@eag.su', password='x', full_name='Админ',
            is_active=True, department=cls.dept, role=cls.admin_role,
            is_staff=True,
        )

    def setUp(self):
        self.client.force_login(self.requester)

    def _mk_task(self, **kw):
        defaults = {
            'title': 'Задача',
            'plan_hours': 2.0,
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'executor': self.executor,
            'requester': self.requester,
            'status': 'new',
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)


# ─── Создание задачи ──────────────────────────────────────────

class TaskCreateTests(BaseTasksTestCase):

    def test_create_minimum(self):
        r = self.client.post(reverse('task_create'), {
            'title': 'Новая',
            'plan': '2',
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Task.objects.filter(title='Новая').exists())

    def test_create_without_title_fails(self):
        before = Task.objects.count()
        r = self.client.post(reverse('task_create'), {
            'title': '',
            'plan': '2',
            'exec_ids': str(self.executor.pk),
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Task.objects.count(), before)

    def test_create_without_executor_fails(self):
        before = Task.objects.count()
        self.client.post(reverse('task_create'), {
            'title': 'X', 'plan': '2',
        })
        self.assertEqual(Task.objects.count(), before)

    def test_create_start_after_due_fails(self):
        start = date.today() + timedelta(days=10)
        due = date.today() + timedelta(days=5)
        before = Task.objects.count()
        self.client.post(reverse('task_create'), {
            'title': 'X', 'plan': '2',
            'start_due': start.isoformat(),
            'due': due.isoformat(),
            'exec_ids': str(self.executor.pk),
        })
        self.assertEqual(Task.objects.count(), before)

    def test_create_group_creates_children(self):
        ex2 = User.objects.create_user(
            email='ex2@eag.su', password='x', full_name='Ex2',
            is_active=True, department=self.dept, role=self.staff_role,
        )
        r = self.client.post(reverse('task_create'), {
            'title': 'Групповая', 'plan': '3',
            'exec_ids': f'{self.executor.pk},{ex2.pk}',
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)
        group = Task.objects.get(title='Групповая', parent__isnull=True)
        self.assertEqual(group.children.count(), 2)


# ─── Действия ─────────────────────────────────────────────────

class TaskActionTests(BaseTasksTestCase):

    def test_start_sets_in_progress(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.executor)
        self.client.post(reverse('task_action', args=[t.pk, 'start']))
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)

    def test_pause_after_start(self):
        t = self._mk_task(status='in_progress')
        self.client.force_login(self.executor)
        self.client.post(reverse('task_action', args=[t.pk, 'pause']))
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.PAUSED)

    def test_submit_to_review(self):
        t = self._mk_task(status='in_progress')
        self.client.force_login(self.executor)
        self.client.post(reverse('task_action', args=[t.pk, 'review']))
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.REVIEW)

    def test_accept_by_requester(self):
        t = self._mk_task(status='review')
        self.client.force_login(self.requester)
        self.client.post(reverse('task_action', args=[t.pk, 'accept']))
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)

    def test_rework_logs_reason(self):
        t = self._mk_task(status='review')
        self.client.force_login(self.requester)
        self.client.post(
            reverse('task_action', args=[t.pk, 'rework']),
            {'comment': 'Переделать'},
        )
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.REWORK)
        self.assertTrue(
            TaskLog.objects.filter(task=t, kind='rework', comment='Переделать').exists()
        )

    def test_get_method_rejected(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.executor)
        r = self.client.get(reverse('task_action', args=[t.pk, 'start']))
        t.refresh_from_db()
        self.assertNotEqual(t.status, Task.Status.IN_PROGRESS)
        self.assertEqual(r.status_code, 302)

    def test_non_owner_cannot_start(self):
        t = self._mk_task(status='new')
        other = User.objects.create_user(
            email='other@eag.su', password='x', full_name='O',
            is_active=True, department=self.dept, role=self.staff_role,
        )
        self.client.force_login(other)
        self.client.post(reverse('task_action', args=[t.pk, 'start']))
        t.refresh_from_db()
        self.assertNotEqual(t.status, Task.Status.IN_PROGRESS)


# ─── Правка ───────────────────────────────────────────────────

class TaskEditTests(BaseTasksTestCase):

    def test_requester_can_edit_plan(self):
        t = self._mk_task(plan_hours=4.0)
        self.client.force_login(self.requester)
        self.client.post(reverse('task_edit', args=[t.pk]), {'plan': '6'})
        t.refresh_from_db()
        self.assertEqual(t.plan_hours, 6.0)
        self.assertTrue(TaskLog.objects.filter(task=t, kind='plan').exists())

    def test_executor_cannot_edit(self):
        t = self._mk_task(plan_hours=4.0)
        self.client.force_login(self.executor)
        r = self.client.post(reverse('task_edit', args=[t.pk]), {'plan': '6'})
        self.assertEqual(r.status_code, 403)
        t.refresh_from_db()
        self.assertEqual(t.plan_hours, 4.0)


# ─── Urgent ───────────────────────────────────────────────────

@unittest.skipUnless(
    hasattr(Task.Priority, 'URGENT'),
    'Приоритет URGENT не добавлен в Task.Priority',
)
class UrgentPriorityTests(BaseTasksTestCase):
    """Срочная задача сдвигает чужие сроки РАБОЧИМИ днями по объёму работы."""

    def test_urgent_shift_days_by_hours(self):
        """8 часов при норме 8 = 1 рабочий день."""
        t = self._mk_task(plan_hours=8)
        self.assertEqual(_urgent_shift_days(t), 1)

    def test_urgent_shift_days_multiple_days(self):
        """16 часов при норме 8 = 2 рабочих дня."""
        t = self._mk_task(plan_hours=16)
        self.assertEqual(_urgent_shift_days(t), 2)

    def test_urgent_shift_days_rounds_up(self):
        """9 часов = ceil(9/8) = 2 дня."""
        t = self._mk_task(plan_hours=9)
        self.assertEqual(_urgent_shift_days(t), 2)

    def test_urgent_shift_days_zero_hours_returns_one(self):
        """Срочная без оценки = минимум 1 день."""
        t = self._mk_task(plan_hours=0)
        self.assertEqual(_urgent_shift_days(t), 1)

    def test_urgent_shift_days_uses_department_norm(self):
        """Цех с нормой 12 ч: 24 часа = 2 дня, а не 3."""
        dept12 = Department.objects.create(name='Цех-12', hours_per_day=12.0)
        executor12 = User.objects.create_user(
            email='ceh@eag.su', password='x', full_name='Цеховик',
            is_active=True, department=dept12, role=self.staff_role,
        )
        t = self._mk_task(plan_hours=24, executor=executor12)
        self.assertEqual(_urgent_shift_days(t), 2)


# ─── Дерево ───────────────────────────────────────────────────

class TaskTreeTests(BaseTasksTestCase):

    def test_tree_renders(self):
        self._mk_task()
        r = self.client.get(reverse('tasks_registry'), {'view': 'tree'})
        self.assertEqual(r.status_code, 200)

    def test_tree_default_view(self):
        """Без ?view= — показывается дерево."""
        self._mk_task()
        r = self.client.get(reverse('tasks_registry'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['registry_view'], 'tree')

    def test_unknown_view_falls_back_to_tree(self):
        r = self.client.get(reverse('tasks_registry'), {'view': 'nonsense'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['registry_view'], 'tree')

    def test_tree_open_filter_hides_closed(self):
        self._mk_task(title='Открытая', status='new')
        self._mk_task(
            title='Закрытая', status='done',
            finished_at='2026-01-01T00:00:00Z',
        )
        r = self.client.get(
            reverse('tasks_registry'), {'view': 'tree', 'f': 'open'},
        )
        content = r.content.decode()
        self.assertIn('Открытая', content)
        self.assertNotIn('Закрытая', content)

    def test_old_url_redirects_to_registry(self):
        r = self.client.get(reverse('task_tree'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/tasks/', r['Location'])
        self.assertIn('view=tree', r['Location'])

    def test_registry_tabs_present(self):
        """Переключатель вида: дерево/хронология/календарь — для boss.

        Хронология доступна только boss/admin, поэтому заходим админом.
        Для staff партиал показывает только «Дерево» и «Календарь» —
        см. test_timeline_tab_hidden_for_engineer.
        """
        self.client.force_login(self.admin)
        r = self.client.get(reverse('tasks_registry'))
        self.assertContains(r, 'Дерево')
        self.assertContains(r, 'Хронология')
        self.assertContains(r, 'Календарь')
        self.assertContains(r, '?view=tree')
        self.assertContains(r, '?view=timeline')
        self.assertContains(r, '?view=calendar')

    def test_timeline_tab_hidden_for_engineer(self):
        """Обычный инженер не видит таб «Хронология» (нет прав)."""
        self.client.force_login(self.executor)
        r = self.client.get(reverse('tasks_registry'))
        self.assertContains(r, 'Дерево')
        self.assertContains(r, 'Календарь')
        self.assertNotContains(r, '?view=timeline')

# ─── Timeline ─────────────────────────────────────────────────

class TimelineTests(BaseTasksTestCase):

    def test_boss_can_open(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('tasks_registry'), {'view': 'timeline'})
        self.assertEqual(r.status_code, 200)

    def test_engineer_forbidden(self):
        self.client.force_login(self.executor)
        r = self.client.get(reverse('tasks_registry'), {'view': 'timeline'})
        self.assertEqual(r.status_code, 403)

    def test_old_url_redirects(self):
        r = self.client.get(reverse('task_timeline'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('view=timeline', r['Location'])

    def test_csv_export_boss(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('timeline_export'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))  # BOM


# ─── Week toggle ──────────────────────────────────────────────

class WeekToggleTests(BaseTasksTestCase):

    def test_executor_can_toggle(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.executor)
        self.client.post(reverse('week_toggle', args=[t.pk]))
        from tasks.models import WeekCommit
        self.assertTrue(
            WeekCommit.objects.filter(user=self.executor, task=t).exists()
        )

    def test_other_user_cannot_toggle(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.requester)
        self.client.post(reverse('week_toggle', args=[t.pk]))
        from tasks.models import WeekCommit
        self.assertFalse(WeekCommit.objects.filter(task=t).exists())


class TaskActionGuardTests(BaseTasksTestCase):
    """Guards: задача принимает только действия, допустимые из её статуса."""

    def _act(self, task, action, **post):
        url = reverse('task_action', args=[task.pk, action])
        return self.client.post(url, post)

    def test_cannot_start_done_task(self):
        t = self._mk_task(
            title='Уже закрыта',
            status='done',
            finished_at='2026-01-01T00:00:00Z',
        )
        self.client.force_login(self.executor)
        self._act(t, 'start')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)

    def test_cannot_pause_new_task(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.executor)
        self._act(t, 'pause')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.NEW)

    def test_cannot_accept_new_task(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.requester)
        self._act(t, 'accept')
        t.refresh_from_db()
        self.assertNotEqual(t.status, Task.Status.DONE)

    def test_cannot_rework_new_task(self):
        t = self._mk_task(status='new')
        self.client.force_login(self.requester)
        self._act(t, 'rework', comment='тест')
        t.refresh_from_db()
        self.assertNotEqual(t.status, Task.Status.REWORK)

    def test_cannot_close_closed_task(self):
        t = self._mk_task(
            title='DONE',
            status='done',
            finished_at='2026-01-01T00:00:00Z',
        )
        t.refresh_from_db()          # нормализовать finished_at в datetime
        original_finished = t.finished_at

        self.client.force_login(self.admin)
        self._act(t, 'close')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)
        self.assertEqual(t.finished_at, original_finished)

    def test_cannot_double_accept(self):
        """Второй POST accept на уже принятой задаче — no-op."""
        t = self._mk_task(
            title='На приёмке',
            status='review',
            finished_at='2026-01-01T00:00:00Z',
        )
        self.client.force_login(self.requester)
        self._act(t, 'accept')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)

        # Второй раз — не должен сломать
        self._act(t, 'accept')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)

    def test_can_start_from_paused(self):
        """Из PAUSED start разрешён."""
        t = self._mk_task(status='paused')
        self.client.force_login(self.executor)
        self._act(t, 'resume')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)

    def test_can_start_from_rework(self):
        """Из REWORK resume разрешён."""
        t = self._mk_task(status='rework')
        self.client.force_login(self.executor)
        self._act(t, 'resume')
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)


class TaskReworkCreateTests(BaseTasksTestCase):
    """Механизм доработок.

    Зафиксировать доработку к закрытой задаче может:
      - суперюзер;
      - админ портала (role.can_admin);
      - руководитель ОТДЕЛА ИСПОЛНИТЕЛЯ (role.can_manage +
        совпадение department_id с отделом исполнителя).

    Не может:
      - постановщик без роли руководителя;
      - сам исполнитель;
      - руководитель другого отдела.
    """

    def _done_task(self, **kw):
        defaults = {
            'title': 'Закрытая задача',
            'status': 'done',
            'finished_at': '2026-01-01T00:00:00Z',
            'plan_hours': 8.0,
        }
        defaults.update(kw)
        return self._mk_task(**defaults)

    def test_boss_creates_rework(self):
        parent = self._done_task()
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4', 'reason': 'Доделать шлифовку'},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            Task.objects.filter(
                parent=parent, title__startswith='[доработка]',
            ).exists()
        )

    def test_created_rework_inherits_executor_and_order(self):
        parent = self._done_task()
        self.client.force_login(self.admin)
        self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        child = Task.objects.get(parent=parent)
        self.assertEqual(child.executor, parent.executor)
        self.assertEqual(child.order, parent.order)
        self.assertEqual(child.branch, parent.branch)
        self.assertEqual(child.requester, self.admin)

    def test_rework_from_open_task_rejected(self):
        parent = self._mk_task(status='new')
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(
            Task.objects.filter(parent=parent).exists()
        )

    def test_non_boss_cannot_create_rework(self):
        parent = self._done_task()
        self.client.force_login(self.executor)
        self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertFalse(
            Task.objects.filter(parent=parent).exists()
        )

    def test_get_method_rejected(self):
        parent = self._done_task()
        self.client.force_login(self.admin)
        r = self.client.get(
            reverse('task_rework_create', args=[parent.pk]),
        )
        self.assertEqual(r.status_code, 405)

    # ── Права: кто может фиксировать доработку ──────────────────────────

    def _manager_of_department(self, dept, email='mgr@eag.su'):
        """Создать руководителя отдела (can_manage) в указанном отделе."""
        role = _role('manager', can_manage=True)
        return User.objects.create_user(
            email=email, password='x', full_name='Руководитель отдела',
            is_active=True, department=dept, role=role,
        )

    def test_manager_of_executor_department_can_create_rework(self):
        """Руководитель отдела ИСПОЛНИТЕЛЯ — может."""
        parent = self._done_task()
        mgr = self._manager_of_department(self.dept, email='mgr_dept@eag.su')

        self.client.force_login(mgr)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4', 'reason': 'Доделать шлифовку'},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        child = Task.objects.filter(parent=parent).first()
        self.assertIsNotNone(child)
        self.assertEqual(child.requester, mgr)

    def test_manager_of_other_department_cannot_create_rework(self):
        """Руководитель ЧУЖОГО отдела — не может."""
        parent = self._done_task()
        other_dept = Department.objects.create(name='ОГТ')
        other_mgr = self._manager_of_department(
            other_dept, email='mgr_other@eag.su',
        )

        self.client.force_login(other_mgr)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Task.objects.filter(parent=parent).exists())

    def test_requester_without_manager_role_cannot_create_rework(self):
        """Постановщик без роли руководителя — не может, даже если он автор."""
        parent = self._done_task()
        # self.requester имеет роль staff (без can_manage)
        self.client.force_login(self.requester)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Task.objects.filter(parent=parent).exists())

    def test_admin_role_can_create_rework(self):
        """Админ портала (can_admin) — может, вне зависимости от отдела."""
        parent = self._done_task()
        # self.admin имеет admin_role (can_manage + can_admin)
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        # 302 на detail дочерней задачи
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Task.objects.filter(parent=parent).exists())

    # ── Начальник завода — не должен фиксировать доработки ──────────────

    def _make_plant_director(self, dept):
        """Создать начальника завода (can_plant, без can_admin)."""
        plant_role = _role('plant_head', can_manage=True, can_plant=True)
        return User.objects.create_user(
            email='plant@eag.su', password='x', full_name='Начальник завода',
            is_active=True, department=dept, role=plant_role,
        )

    def test_plant_director_cannot_create_rework_in_his_own_department(self):
        """Начальник завода привязан к отделу исполнителя — всё равно НЕ может."""
        parent = self._done_task()
        director = self._make_plant_director(self.dept)

        self.client.force_login(director)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4', 'reason': 'Попытка'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Task.objects.filter(parent=parent).exists())

    def test_plant_director_cannot_create_rework_in_other_department(self):
        """Начальник завода вне отдела исполнителя — тоже НЕ может."""
        parent = self._done_task()
        other_dept = Department.objects.create(name='ОГТ')
        director = self._make_plant_director(other_dept)

        self.client.force_login(director)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Task.objects.filter(parent=parent).exists())

    def test_admin_role_with_can_plant_still_can(self):
        """Если у роли can_admin + can_plant — фиксирует, как админ."""
        parent = self._done_task()
        both_role = _role(
            'admin_plant', can_manage=True, can_admin=True, can_plant=True,
        )
        boss = User.objects.create_user(
            email='boss@eag.su', password='x', full_name='Админ+Директор',
            is_active=True, department=self.dept, role=both_role,
        )
        self.client.force_login(boss)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Task.objects.filter(parent=parent).exists())

    def test_manager_of_same_department_still_can(self):
        """Регрессия: обычный руководитель того же отдела — по-прежнему может."""
        parent = self._done_task()
        mgr = self._manager_of_department(self.dept, email='mgr_reg@eag.su')

        self.client.force_login(mgr)
        r = self.client.post(
            reverse('task_rework_create', args=[parent.pk]),
            {'plan': '4'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Task.objects.filter(parent=parent).exists())


class ShopStartOnClosedTests(BaseTasksTestCase):
    """Проверка shop_start на закрытой и отменённой задаче (тесты B.3)."""

    def test_shop_start_on_done_warns_not_creates_session(self):
        from tasks.models import ShopSession
        t = self._mk_task(
            title='DONE',
            status='done',
            finished_at='2026-01-01T00:00:00Z',
        )
        self.client.force_login(self.executor)
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))
        self.assertFalse(
            ShopSession.objects.filter(task=t, finished_at__isnull=True).exists()
        )

    def test_shop_start_on_cancelled_blocked(self):
        from tasks.models import ShopSession
        t = self._mk_task(
            title='CANCELLED',
            status='cancelled',
            finished_at='2026-01-01T00:00:00Z',
        )
        self.client.force_login(self.executor)
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))
        self.assertFalse(
            ShopSession.objects.filter(task=t, finished_at__isnull=True).exists()
        )



class DecomposeClosedTaskTests(BaseTasksTestCase):
    """Исполнитель не может создать подзадачу к своей ЗАКРЫТОЙ задаче.

    Раньше: task_create?parent=<done_pk> проходил, потому что
    _can_decompose пускал исполнителя без оглядки на статус.
    Теперь: _can_decompose возвращает False для DONE/CANCELLED,
    task_create делает редирект на task_detail с ошибкой.
    """

    def _done_task(self, **kw):
        defaults = {
            'title': 'Закрытая',
            'status': 'done',
            'finished_at': '2026-01-01T00:00:00Z',
        }
        defaults.update(kw)
        return self._mk_task(**defaults)

    def test_executor_cannot_decompose_own_done_task_via_form(self):
        """GET task_create?parent=<done> под исполнителем → редирект."""
        parent = self._done_task()
        self.client.force_login(self.executor)
        r = self.client.get(
            reverse('task_create') + f'?parent={parent.pk}',
            )
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'/tasks/task/{parent.pk}/', r.url)

    def test_executor_cannot_post_decompose_own_done_task(self):
        """POST task_create с parent=<done> под исполнителем — задача не создаётся."""
        parent = self._done_task()
        before = Task.objects.count()
        self.client.force_login(self.executor)
        r = self.client.post(reverse('task_create'), {
            'title': 'Попытка обойти',
            'plan': '2',
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'parent': str(parent.pk),
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Task.objects.count(), before)
        self.assertFalse(
            Task.objects.filter(parent=parent, title='Попытка обойти').exists()
        )

    def test_executor_can_decompose_own_open_task(self):
        """К своей ОТКРЫТОЙ задаче подзадачу создать можно."""
        parent = self._mk_task(status='new')
        before = Task.objects.count()
        self.client.force_login(self.executor)
        r = self.client.post(reverse('task_create'), {
            'title': 'Подзадача',
            'plan': '2',
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'parent': str(parent.pk),
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Task.objects.count(), before + 1)
        self.assertTrue(
            Task.objects.filter(parent=parent, title='Подзадача').exists()
        )

    def test_boss_cannot_bypass_rework_via_task_create(self):
        """Даже is_boss к закрытой задаче через task_create не пройдёт —
        только через task_rework_create."""
        parent = self._done_task()
        before = Task.objects.count()
        self.client.force_login(self.admin)
        r = self.client.get(
            reverse('task_create') + f'?parent={parent.pk}',
            )
        self.assertEqual(r.status_code, 302)
        # POST тоже не создаст
        r2 = self.client.post(reverse('task_create'), {
            'title': 'Обход',
            'plan': '2',
            'scale': 's',
            'kind': 'work',
            'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'parent': str(parent.pk),
            'outside_order': '1',
        })
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(Task.objects.count(), before)



class ExternalDueTests(BaseTasksTestCase):
    """Внешний (формальный) срок для заказчика.

    Семантика:
      - due           — внутренний срок, по нему работают и сдвигают;
      - external_due  — обещание заказчику, НЕ сдвигается;
      - исполнитель external_due не видит нигде.

    Право видеть/менять: постановщик, is_boss, is_admin_role.
    """

    def test_create_saves_external_due(self):
        self.client.post(reverse('task_create'), {
            'title': 'С внешним',
            'plan': '2',
            'scale': 's', 'kind': 'work', 'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'outside_order': '1',
            'due': (date.today() + timedelta(days=3)).isoformat(),
            'external_due': (date.today() + timedelta(days=7)).isoformat(),
        })
        t = Task.objects.get(title='С внешним')
        self.assertEqual(t.external_due, date.today() + timedelta(days=7))

    def test_create_without_external_due_ok(self):
        self.client.post(reverse('task_create'), {
            'title': 'Без внешнего',
            'plan': '2', 'scale': 's', 'kind': 'work',
            'exec_ids': str(self.executor.pk),
            'outside_order': '1',
        })
        t = Task.objects.get(title='Без внешнего')
        self.assertIsNone(t.external_due)

    # ── Видимость плитки в task-head-info ───────────────────────────────
    # «Формальный срок» встречается в двух местах карточки:
    #   1) плитка <div class="l">Формальный срок</div> в task-head-info —
    #      только когда external_due задан и виден пользователю;
    #   2) label в форме правки задачи (блок «Правка задачи») —
    #      всегда, если can_edit=True (постановщик, is_boss).
    # В тестах про видимость проверяем именно плитку — по её HTML-маркеру.

    def test_executor_does_not_see_external_due_tile(self):
        t = self._mk_task(
            external_due=date.today() + timedelta(days=10),
        )
        self.client.force_login(self.executor)
        r = self.client.get(reverse('task_detail', args=[t.pk]))
        # Плитки нет
        self.assertNotContains(r, '<div class="l">Формальный срок</div>')
        # Строки в «Все параметры» тоже нет
        self.assertNotContains(r, '<th>Формальный срок</th>')
        # Даты нет в html
        self.assertNotIn(
            t.external_due.strftime('%d.%m.%Y'), r.content.decode(),
        )

    def test_requester_sees_external_due_tile(self):
        t = self._mk_task(
            external_due=date.today() + timedelta(days=10),
        )
        self.client.force_login(self.requester)
        r = self.client.get(reverse('task_detail', args=[t.pk]))
        self.assertContains(r, '<div class="l">Формальный срок</div>')
        self.assertContains(r, '<th>Формальный срок</th>')
        self.assertContains(r, t.external_due.strftime('%d.%m.%Y'))

    def test_admin_sees_external_due_tile(self):
        t = self._mk_task(
            external_due=date.today() + timedelta(days=10),
        )
        self.client.force_login(self.admin)
        r = self.client.get(reverse('task_detail', args=[t.pk]))
        self.assertContains(r, '<div class="l">Формальный срок</div>')
        self.assertContains(r, '<th>Формальный срок</th>')
        self.assertContains(r, t.external_due.strftime('%d.%m.%Y'))

    def test_requester_can_edit_external_due(self):
        t = self._mk_task()
        new_date = date.today() + timedelta(days=15)
        self.client.force_login(self.requester)
        self.client.post(
            reverse('task_edit', args=[t.pk]),
            {'external_due': new_date.isoformat()},
        )
        t.refresh_from_db()
        self.assertEqual(t.external_due, new_date)

    def test_executor_cannot_edit_external_due(self):
        old = date.today() + timedelta(days=10)
        t = self._mk_task(external_due=old)
        self.client.force_login(self.executor)
        # POST не пройдёт (task_edit даёт 403 для исполнителя)
        new_date = date.today() + timedelta(days=20)
        r = self.client.post(
            reverse('task_edit', args=[t.pk]),
            {'external_due': new_date.isoformat()},
        )
        self.assertEqual(r.status_code, 403)
        t.refresh_from_db()
        self.assertEqual(t.external_due, old)

    def test_shift_does_not_touch_external_due(self):
        """Сдвиг срочной не меняет external_due других задач."""
        other = self._mk_task(
            title='Обычная',
            external_due=date.today() + timedelta(days=20),
        )
        # Запомним external_due до сдвига
        original_external = other.external_due

        # Применим сдвиг через сервис (минуя approve)
        self.executor.shift_credit_days = 5
        self.executor.save(update_fields=['shift_credit_days'])
        from tasks.services_shift import recompute_shift_for_executor
        recompute_shift_for_executor(self.executor, exclude_task_pk=None)

        other.refresh_from_db()
        self.assertEqual(other.external_due, original_external)


class ExternalDueMigrationTests(BaseTasksTestCase):
    """Существующие задачи без external_due работают как раньше."""

    def test_old_task_without_external_due_renders_ok(self):
        t = self._mk_task()
        self.assertIsNone(t.external_due)
        self.client.force_login(self.requester)
        r = self.client.get(reverse('task_detail', args=[t.pk]))
        self.assertEqual(r.status_code, 200)
        # Плитки нет — потому что нет даты
        self.assertNotContains(r, '<div class="l">Формальный срок</div>')
        self.assertNotContains(r, '<th>Формальный срок</th>')
        # В форме правки поле всё равно есть (постановщик может задать позже)
        self.assertContains(r, '<label>Формальный срок</label>')



# ═════════════════════════════════════════════════════════════
#  F.3b: предупреждение при постановке на отпускника
# ═════════════════════════════════════════════════════════════

class VacationWarningUnitTests(TestCase):
    """Юнит-тесты _executor_vacation_warning — без view.

    setUpTestData создаёт cls.user один раз. Между тестами БД
    откатывается к savepoint, но Python-объект остаётся тем же.
    Поэтому в setUp перезагружаем его из БД — иначе изменение
    vacation_from в одном тесте протечёт в следующий.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        cls.dept = Department.objects.get_or_create(name='VAC-WARN-ОГК')[0]
        cls.role = Role.objects.get_or_create(
            code='staff-vw', defaults={'name': 'Сотрудник VW'},
        )[0]

        User = get_user_model()
        cls.user = User.objects.create_user(
            email='vw@test.ru', password='p', full_name='Отпускной',
            department=cls.dept, role=cls.role, is_active=True,
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        # Сброс in-memory состояния: сбрасываем атрибуты и перечитываем
        # из БД (БД уже откачена к savepoint setUpTestData).
        self.user.refresh_from_db()

    def _vacation(self, days_from=0, days_to=5):
        from datetime import timedelta
        today = timezone.localdate()
        self.user.vacation_from = today + timedelta(days=days_from)
        self.user.vacation_to = today + timedelta(days=days_to)
        self.user.employment_status = self.user.EmploymentStatus.VACATION
        self.user.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

    def test_none_if_not_on_vacation(self):
        from tasks.views import _executor_vacation_warning
        self.assertIsNone(_executor_vacation_warning(self.user))

    def test_none_if_executor_is_none(self):
        from tasks.views import _executor_vacation_warning
        self.assertIsNone(_executor_vacation_warning(None))

    def test_warns_if_status_no_dates(self):
        from tasks.views import _executor_vacation_warning
        self.user.employment_status = self.user.EmploymentStatus.VACATION
        self.user.save(update_fields=['employment_status'])
        result = _executor_vacation_warning(self.user)
        self.assertIsNotNone(result)
        self.assertIn('отпуске', result)

    def test_warns_if_dates_overlap_today(self):
        from tasks.views import _executor_vacation_warning
        self._vacation(days_from=-2, days_to=5)
        result = _executor_vacation_warning(self.user)
        self.assertIsNotNone(result)
        self.assertIn('пересекается', result)

    def test_warns_if_task_due_inside_vacation(self):
        from datetime import timedelta
        from tasks.views import _executor_vacation_warning
        today = timezone.localdate()
        self._vacation(days_from=10, days_to=20)

        # Задача с дедлайном внутри отпуска
        result = _executor_vacation_warning(
            self.user,
            start_due=today,
            due=today + timedelta(days=15),
        )
        self.assertIsNotNone(result)
        self.assertIn('пересекается', result)

    def test_no_warn_if_task_ends_before_vacation(self):
        from datetime import timedelta
        from tasks.views import _executor_vacation_warning
        today = timezone.localdate()
        self._vacation(days_from=30, days_to=40)

        result = _executor_vacation_warning(
            self.user,
            start_due=today,
            due=today + timedelta(days=10),
        )
        self.assertIsNone(result)

    def test_no_warn_if_task_starts_after_vacation(self):
        from datetime import timedelta
        from tasks.views import _executor_vacation_warning
        today = timezone.localdate()
        self._vacation(days_from=-30, days_to=-20)

        result = _executor_vacation_warning(
            self.user,
            start_due=today,
            due=today + timedelta(days=5),
        )
        self.assertIsNone(result)


class VacationWarningInCreateViewTests(TestCase):
    """task_create показывает warning при постановке на отпускника.

    setUp с refresh_from_db — потому что первый тест (alphabetically)
    меняет in-memory объект self.executor (ставит VACATION). Без сброса
    второй тест увидит устаревший статус и получит лишний warning.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        cls.dept = Department.objects.get_or_create(name='VAC-CR-ОГК')[0]
        cls.role_staff = Role.objects.get_or_create(
            code='staff-vc', defaults={'name': 'Сотрудник VC'},
        )[0]

        User = get_user_model()
        cls.requester = User.objects.create_user(
            email='req-vc@test.ru', password='p', full_name='Постановщик',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.executor = User.objects.create_user(
            email='ex-vc@test.ru', password='p', full_name='Отпускник Исполнитель',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.requester.refresh_from_db()
        self.executor.refresh_from_db()

    def test_creates_task_with_vacation_warning(self):
        """Задача создаётся, но в messages появляется warning."""
        from datetime import timedelta
        from django.contrib.messages import get_messages

        today = timezone.localdate()
        self.executor.vacation_from = today
        self.executor.vacation_to = today + timedelta(days=10)
        self.executor.employment_status = self.executor.EmploymentStatus.VACATION
        self.executor.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

        self.client.force_login(self.requester)
        r = self.client.post(reverse('task_create'), {
            'title': 'Задача на отпускника',
            'plan': '2',
            'scale': 's', 'kind': 'work', 'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'start_due': today.isoformat(),
            'due': (today + timedelta(days=3)).isoformat(),
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)

        # Задача создалась, несмотря на отпуск
        self.assertTrue(
            Task.objects.filter(title='Задача на отпускника').exists()
        )

        # В messages есть warning про отпуск
        msgs = [str(m) for m in get_messages(r.wsgi_request)]
        has_vacation_warning = any('пересекается' in m for m in msgs)
        self.assertTrue(
            has_vacation_warning,
            msg=f'Ожидался warning про отпуск. Получено: {msgs}',
        )

    def test_no_warning_for_regular_executor(self):
        from django.contrib.messages import get_messages

        self.client.force_login(self.requester)
        r = self.client.post(reverse('task_create'), {
            'title': 'Обычная задача',
            'plan': '2', 'scale': 's', 'kind': 'work', 'priority': 'medium',
            'exec_ids': str(self.executor.pk),
            'outside_order': '1',
        })
        self.assertEqual(r.status_code, 302)

        msgs = [str(m) for m in get_messages(r.wsgi_request)]
        self.assertFalse(
            any('пересекается' in m for m in msgs),
            msg=f'Не должно быть warning про отпуск. Получено: {msgs}',
        )
