"""Тесты генерации плана заказа по шаблону."""
from datetime import date
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from accounts.models import Department, Role
from core.models import TaskType
from tasks.models import Order, OrderTemplate, OrderTemplateStage, Task, TaskBranch
from tasks.order_template import generate_plan_from_template

User = get_user_model()



class OrderTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='ОГК')
        cls.role = Role.objects.create(
            name='Инженер', code='staff', can_manage=False, can_admin=False,
        )
        cls.boss_role = Role.objects.create(
            name='Руководитель', code='manager', can_manage=True,
        )
        cls.boss = User.objects.create_user(
            email='boss@eag.su', password='x', full_name='Босс',
            is_active=True, department=cls.dept, role=cls.boss_role,
        )
        cls.staff = User.objects.create_user(
            email='staff@eag.su', password='x', full_name='Инженер',
            is_active=True, department=cls.dept, role=cls.role,
        )
        cls.tt = TaskType.objects.create(
            name='Чертёж', plan_hours=16, due_days=5,
        )

    def _order(self, **kw):
        base = {
            'number': 'T-001',
            'product': 'Генератор',
            'contract_start': date(2026, 1, 5),
            'design_start':   date(2026, 1, 6),
            'design_end':     date(2026, 2, 6),
            'ship_due':       date(2026, 4, 1),
        }
        base.update(kw)
        return Order.objects.create(**base)

    def _template(self):
        tpl = OrderTemplate.objects.create(name='Стандарт', is_default=True)
        OrderTemplateStage.objects.create(
            template=tpl, order=1, title='Проектирование',
            branch_name='Проектирование',
            task_type=self.tt, executor_role_code='staff',
            offset_anchor='contract_start', offset_days=1,
            duration_days=20, use_task_type_hours=True,
        )
        OrderTemplateStage.objects.create(
            template=tpl, order=2, title='Изготовление',
            branch_name='Производство',
            task_type=self.tt, executor_role_code='staff',
            offset_anchor='design_end', offset_days=0,
            duration_days=40, use_task_type_hours=True,
        )
        return tpl

    def test_generate_creates_tasks_and_branches(self):
        order = self._order()
        tpl = self._template()

        result = generate_plan_from_template(order, tpl, requester=self.boss)

        self.assertEqual(result['tasks_created'], 2)
        self.assertEqual(result['branches_created'], 2)
        self.assertEqual(result['tasks_skipped'], 0)
        self.assertEqual(Task.objects.filter(order=order).count(), 2)
        self.assertEqual(TaskBranch.objects.filter(order=order).count(), 2)

    def test_second_stage_in_branch_is_blocked(self):
        """Если в одной ветке два этапа, второй должен ждать первого."""
        order = self._order()
        tpl = OrderTemplate.objects.create(name='Two-stage')
        OrderTemplateStage.objects.create(
            template=tpl, order=1, title='A',
            branch_name='Ветка', task_type=self.tt,
            executor_role_code='staff',
            offset_anchor='contract_start', offset_days=1,
            duration_days=5, use_task_type_hours=True,
        )
        OrderTemplateStage.objects.create(
            template=tpl, order=2, title='B',
            branch_name='Ветка', task_type=self.tt,
            executor_role_code='staff',
            offset_anchor='contract_start', offset_days=10,
            duration_days=5, use_task_type_hours=True,
        )
        generate_plan_from_template(order, tpl, requester=self.boss)

        a = Task.objects.get(order=order, title='A')
        b = Task.objects.get(order=order, title='B')
        self.assertFalse(a.blocked_by_stage)
        self.assertTrue(b.blocked_by_stage)
        self.assertEqual(a.stage_order, 1)
        self.assertEqual(b.stage_order, 2)

    def test_skips_when_anchor_missing(self):
        order = self._order(ship_due=None)
        tpl = OrderTemplate.objects.create(name='Bad')
        OrderTemplateStage.objects.create(
            template=tpl, order=1, title='X',
            task_type=self.tt, executor_role_code='staff',
            offset_anchor='ship_due', offset_days=0,
            duration_days=5, use_task_type_hours=True,
        )
        result = generate_plan_from_template(order, tpl, requester=self.boss)
        self.assertEqual(result['tasks_created'], 0)
        self.assertEqual(result['tasks_skipped'], 1)
        self.assertTrue(result['errors'])

    def test_view_requires_boss(self):
        order = self._order()
        self.client.force_login(self.staff)
        r = self.client.get(reverse('order_generate_from_template', args=[order.pk]))
        self.assertEqual(r.status_code, 403)

    def test_view_applies_template(self):
        order = self._order()
        tpl = self._template()
        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': str(tpl.pk)},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Task.objects.filter(order=order).count(), 2)

# ═════════════════════════════════════════════════════════════
#  View: order_generate_from_template
# ═════════════════════════════════════════════════════════════

class OrderGenerateViewTests(TestCase):
    """Тесты view order_generate_from_template.

    Покрываем то, чего нет в сервисных тестах:
      - GET: рендер формы с контекстом (templates, has_tasks);
      - POST без выбранного шаблона → ошибка, ничего не создаётся;
      - POST с несуществующим / неактивным шаблоном → ошибка;
      - POST с валидным → задачи созданы, редирект на order_plan;
      - 404 для несуществующего заказа.
    """

    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='ОГК-VIEW')
        cls.role_staff = Role.objects.create(
            name='Инженер', code='staff', can_manage=False, can_admin=False,
        )
        cls.role_boss = Role.objects.create(
            name='Руководитель', code='manager', can_manage=True,
        )
        cls.boss = User.objects.create_user(
            email='boss-view@eag.su', password='x', full_name='Босс',
            is_active=True, department=cls.dept, role=cls.role_boss,
        )
        cls.staff = User.objects.create_user(
            email='staff-view@eag.su', password='x', full_name='Инженер',
            is_active=True, department=cls.dept, role=cls.role_staff,
        )
        cls.tt = TaskType.objects.create(
            name='Чертёж-view', plan_hours=8, due_days=3,
        )

    def _order(self, **kw):
        base = {
            'number': 'V-001',
            'product': 'Генератор-view',
            'contract_start': date(2026, 1, 5),
            'design_end':     date(2026, 2, 6),
            'ship_due':       date(2026, 4, 1),
        }
        base.update(kw)
        return Order.objects.create(**base)

    def _active_template(self):
        tpl = OrderTemplate.objects.create(name='Активный-view', is_active=True)
        OrderTemplateStage.objects.create(
            template=tpl, order=1, title='Работа',
            branch_name='Ветка-view',
            task_type=self.tt, executor_role_code='staff',
            offset_anchor='contract_start', offset_days=0,
            duration_days=5, use_task_type_hours=True,
        )
        return tpl

    # ── GET ────────────────────────────────────────────────────

    def test_get_ok_for_boss(self):
        order = self._order()
        self.client.force_login(self.boss)
        r = self.client.get(
            reverse('order_generate_from_template', args=[order.pk]),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('templates', r.context)

    def test_get_requires_boss(self):
        order = self._order()
        self.client.force_login(self.staff)
        r = self.client.get(
            reverse('order_generate_from_template', args=[order.pk]),
        )
        self.assertEqual(r.status_code, 403)

    def test_get_requires_login(self):
        order = self._order()
        r = self.client.get(
            reverse('order_generate_from_template', args=[order.pk]),
        )
        self.assertEqual(r.status_code, 302)

    def test_get_shows_only_active_templates(self):
        order = self._order()
        active = self._active_template()
        inactive = OrderTemplate.objects.create(
            name='Неактивный-view', is_active=False,
        )

        self.client.force_login(self.boss)
        r = self.client.get(
            reverse('order_generate_from_template', args=[order.pk]),
        )
        tpl_list = list(r.context['templates'])
        self.assertIn(active, tpl_list)
        self.assertNotIn(inactive, tpl_list)

    def test_get_has_tasks_flag_false_initially(self):
        order = self._order()
        self.client.force_login(self.boss)
        r = self.client.get(
            reverse('order_generate_from_template', args=[order.pk]),
        )
        self.assertFalse(r.context['has_tasks'])

    def test_get_404_for_unknown_order(self):
        self.client.force_login(self.boss)
        r = self.client.get(
            reverse('order_generate_from_template', args=[999999]),
        )
        self.assertEqual(r.status_code, 404)

    # ── POST: ошибки ────────────────────────────────────────────

    def test_post_without_template_shows_error(self):
        order = self._order()
        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': ''},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Task.objects.filter(order=order).count(), 0)

    def test_post_with_unknown_template_shows_error(self):
        order = self._order()
        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': '999999'},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Task.objects.filter(order=order).count(), 0)

    def test_post_with_inactive_template_shows_error(self):
        """View фильтрует is_active=True — неактивный шаблон не сработает."""
        order = self._order()
        inactive = OrderTemplate.objects.create(
            name='Неактивный-post', is_active=False,
        )
        OrderTemplateStage.objects.create(
            template=inactive, order=1, title='X',
            task_type=self.tt, executor_role_code='staff',
            offset_anchor='contract_start', offset_days=0,
            duration_days=5, use_task_type_hours=True,
        )

        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': str(inactive.pk)},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Task.objects.filter(order=order).count(), 0)

    # ── POST: успех ─────────────────────────────────────────────

    def test_post_with_valid_template_creates_tasks(self):
        order = self._order()
        tpl = self._active_template()

        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': str(tpl.pk)},
        )
        # Редирект на order_plan
        self.assertEqual(r.status_code, 302)
        self.assertIn(
            reverse('order_plan', args=[order.pk]), r.url,
        )
        self.assertEqual(Task.objects.filter(order=order).count(), 1)

    def test_post_creates_branches(self):
        order = self._order()
        tpl = self._active_template()

        self.client.force_login(self.boss)
        self.client.post(
            reverse('order_generate_from_template', args=[order.pk]),
            {'template': str(tpl.pk)},
        )
        self.assertEqual(TaskBranch.objects.filter(order=order).count(), 1)

    def test_post_404_for_unknown_order(self):
        tpl = self._active_template()
        self.client.force_login(self.boss)
        r = self.client.post(
            reverse('order_generate_from_template', args=[999999]),
            {'template': str(tpl.pk)},
        )
        self.assertEqual(r.status_code, 404)
