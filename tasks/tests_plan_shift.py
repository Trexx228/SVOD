"""Автотесты tasks.plan_shift: заявки на сдвиг, эскалация, решения, ужим."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from comms.models import Notification
from tasks import plan_shift
from tasks.models import (
    OrderDeadlineNotification, PlanShiftRequest, PlanShiftStep,
    Task, TaskBranch, TaskLog, Order,
)
from tasks.utils import add_work_days

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


# ═════════════════════════════════════════════════════════════
#  База
# ═════════════════════════════════════════════════════════════

class PlanShiftBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep = Department.objects.get_or_create(name='ОГК')[0]
        cls.other_dep = Department.objects.get_or_create(name='ОГТ')[0]

        cls.role_staff = _role('staff')
        cls.role_deputy = _role('deputy')
        cls.role_mgr = _role('manager', can_manage=True)
        cls.role_plant = _role('plant_head', can_manage=True, can_plant=True)

        # Исполнитель и постановщик
        cls.executor = User.objects.create_user(
            email='exec@test.ru', password='p', full_name='Исполнитель',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )
        # requester — обычный постановщик, НЕ менеджер, чтобы не попадал
        # в reviewers для MANAGER-уровня. Менеджеры у нас отдельные.
        cls.requester = User.objects.create_user(
            email='req@test.ru', password='p', full_name='Постановщик',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )
        # Согласующие трёх уровней
        cls.deputy = User.objects.create_user(
            email='deputy@test.ru', password='p', full_name='Заместитель',
            department=cls.dep, role=cls.role_deputy, is_active=True,
        )
        cls.manager = User.objects.create_user(
            email='mgr@test.ru', password='p', full_name='Начальник ОГК',
            department=cls.dep, role=cls.role_mgr, is_active=True,
        )
        cls.plant = User.objects.create_user(
            email='plant@test.ru', password='p', full_name='Начальник завода',
            department=cls.dep, role=cls.role_plant, is_active=True,
        )

    def _source_task(self, **kw):
        """Срочная задача-виновник с plan_hours=24 → 3 раб. дня."""
        today = timezone.localdate()
        defaults = {
            'title': 'Срочная задача',
            'plan_hours': 24.0,   # 24 / 8 = 3 раб. дня
            'scale': 's', 'kind': 'work',
            'priority': Task.Priority.URGENT,
            'executor': self.executor,
            'requester': self.requester,
            'status': Task.Status.NEW,
            'start_due': today,
            'due': today + timedelta(days=5),
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)

    def _other_open_task(self, **kw):
        """Обычная открытая задача того же исполнителя."""
        today = timezone.localdate()
        start = today + timedelta(days=5)
        due = today + timedelta(days=10)
        defaults = {
            'title': 'Обычная',
            'plan_hours': 2.0,
            'scale': 's', 'kind': 'work',
            'priority': Task.Priority.MEDIUM,
            'executor': self.executor,
            'requester': self.requester,
            'status': Task.Status.NEW,
            'start_due': start,
            'due': due,
            'original_start_due': start,
            'original_due': due,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)


# ═════════════════════════════════════════════════════════════
#  find_reviewers
# ═════════════════════════════════════════════════════════════

class FindReviewersTests(PlanShiftBase):
    def test_deputy_found_in_department(self):
        rs = plan_shift.find_reviewers(self.dep, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual([u.pk for u in rs], [self.deputy.pk])

    def test_manager_found_in_department(self):
        rs = plan_shift.find_reviewers(self.dep, PlanShiftRequest.Level.MANAGER)
        pks = {u.pk for u in rs}
        self.assertIn(self.manager.pk, pks)

    def test_plant_ignores_department(self):
        """PLANT — единственный уровень, где отдел не учитывается."""
        rs = plan_shift.find_reviewers(
            self.other_dep, PlanShiftRequest.Level.PLANT,
        )
        self.assertEqual([u.pk for u in rs], [self.plant.pk])

    def test_deputy_not_found_in_other_department(self):
        rs = plan_shift.find_reviewers(
            self.other_dep, PlanShiftRequest.Level.DEPUTY,
        )
        self.assertEqual(rs, [])

    def test_inactive_excluded(self):
        self.deputy.is_active = False
        self.deputy.save(update_fields=['is_active'])
        rs = plan_shift.find_reviewers(self.dep, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(rs, [])

    def test_unknown_level_returns_empty(self):
        rs = plan_shift.find_reviewers(self.dep, 'nonsense')
        self.assertEqual(rs, [])

    def test_department_none_for_deputy_returns_empty(self):
        rs = plan_shift.find_reviewers(None, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(rs, [])


# ═════════════════════════════════════════════════════════════
#  pick_start_level
# ═════════════════════════════════════════════════════════════

class PickStartLevelTests(PlanShiftBase):
    def test_deputy_when_deputy_exists(self):
        lvl = plan_shift.pick_start_level(self.dep)
        self.assertEqual(lvl, PlanShiftRequest.Level.DEPUTY)

    def test_manager_when_no_deputy(self):
        self.deputy.delete()
        lvl = plan_shift.pick_start_level(self.dep)
        self.assertEqual(lvl, PlanShiftRequest.Level.MANAGER)

    def test_plant_when_only_plant(self):
        self.deputy.delete()
        # Удалим всех manager'ов
        User.objects.filter(role=self.role_mgr).delete()
        lvl = plan_shift.pick_start_level(self.dep)
        self.assertEqual(lvl, PlanShiftRequest.Level.PLANT)

    def test_none_when_nobody(self):
        User.objects.all().delete()
        lvl = plan_shift.pick_start_level(self.dep)
        self.assertIsNone(lvl)


# ═════════════════════════════════════════════════════════════
#  build_affected_list
# ═════════════════════════════════════════════════════════════

class BuildAffectedListTests(PlanShiftBase):
    def test_includes_other_open_tasks_of_executor(self):
        src = self._source_task()
        other = self._other_open_task(title='Другая')
        affected = plan_shift.build_affected_list(src)
        self.assertEqual([t.pk for t in affected], [other.pk])

    def test_excludes_source(self):
        src = self._source_task()
        affected = plan_shift.build_affected_list(src)
        self.assertNotIn(src.pk, [t.pk for t in affected])

    def test_excludes_done_and_cancelled(self):
        src = self._source_task()
        self._other_open_task(
            title='Готово', status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        self._other_open_task(
            title='Отменено', status=Task.Status.CANCELLED,
            finished_at=timezone.now(),
        )
        affected = plan_shift.build_affected_list(src)
        self.assertEqual(affected, [])

    def test_excludes_other_executors(self):
        src = self._source_task()
        self._other_open_task(
            title='Чужая', executor=self.deputy,
        )
        affected = plan_shift.build_affected_list(src)
        self.assertEqual(affected, [])


# ═════════════════════════════════════════════════════════════
#  create_request
# ═════════════════════════════════════════════════════════════

class CreateRequestTests(PlanShiftBase):
    def test_creates_pending_request_on_deputy_level(self):
        src = self._source_task()
        req = plan_shift.create_request(src, 'Очень срочно')

        self.assertIsNotNone(req)
        self.assertEqual(req.status, PlanShiftRequest.Status.PENDING)
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(req.shift_days, 3)
        self.assertEqual(req.department, self.dep)
        self.assertEqual(req.initiated_by, self.requester)
        self.assertEqual(req.source_task, src)

    def test_recipients_are_deputies(self):
        src = self._source_task()
        req = plan_shift.create_request(src, 'X')
        self.assertEqual(
            list(req.recipients.values_list('pk', flat=True)),
            [self.deputy.pk],
        )

    def test_steps_created(self):
        src = self._source_task()
        req = plan_shift.create_request(src, 'X')
        steps = list(req.steps.all())
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].level, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(steps[0].reviewer, self.deputy)
        self.assertEqual(steps[0].decision, PlanShiftStep.Decision.PENDING)

    def test_source_task_goes_to_pending_approval(self):
        src = self._source_task()
        plan_shift.create_request(src, 'X')
        src.refresh_from_db()
        self.assertEqual(src.status, Task.Status.PENDING_APPROVAL)

    def test_shift_log_created(self):
        src = self._source_task()
        plan_shift.create_request(src, 'Очень срочно')
        log = TaskLog.objects.filter(
            task=src, kind=TaskLog.Kind.SHIFT_REQUESTED,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.author, self.requester)
        self.assertEqual(log.shift_days, 3)

    def test_affected_tasks_set(self):
        src = self._source_task()
        other = self._other_open_task()
        req = plan_shift.create_request(src, 'X')
        self.assertEqual(
            list(req.affected_tasks.values_list('pk', flat=True)),
            [other.pk],
        )

    def test_escalate_at_set_for_deputy(self):
        """DEPUTY имеет таймаут 2 часа."""
        src = self._source_task()
        before = timezone.now()
        req = plan_shift.create_request(src, 'X')
        delta = req.escalate_at - before
        # ~2 часа (плюс-минус пара секунд на выполнение)
        self.assertGreater(delta.total_seconds(), 7000)
        self.assertLess(delta.total_seconds(), 7300)

    def test_no_reviewers_applies_immediately(self):
        """Если согласующих нет — credit применяется сразу, заявка не создаётся."""
        # Убираем согласующих.
        User.objects.filter(
            role__code__in=['deputy', 'manager', 'plant_head'],
        ).exclude(pk__in=[self.requester.pk, self.executor.pk]).update(
            role=self.role_staff,
        )
        src = self._source_task(plan_hours=24.0)  # 3 раб. дня
        other = self._other_open_task()

        result = plan_shift.create_request(src, 'X')

        self.assertIsNone(result)
        self.assertEqual(PlanShiftRequest.objects.count(), 0)

        # Credit вырос на 3 раб. дня.
        self.executor.refresh_from_db()
        self.assertEqual(self.executor.shift_credit_days, 3)

        # planned_shift_days записан у source
        src.refresh_from_db()
        self.assertEqual(src.planned_shift_days, 3)
        self.assertEqual(src.status, Task.Status.NEW)

        # other имеет большой резерв → не сдвинулся.
        # _other_open_task: окно 5 раб. дней, plan_hours=2 → needed=1, reserve=4.
        # credit=3 < reserve=4 → shift=0.
        other.refresh_from_db()
        self.assertEqual(other.due, other.original_due or other.due)

    def test_no_department_no_reviewers(self):
        """Executor без отдела — согласовать некому, сдвиг применяется сразу."""
        self.executor.department = None
        self.executor.save(update_fields=['department'])
        src = self._source_task()
        result = plan_shift.create_request(src, 'X')
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════
#  escalate
# ═════════════════════════════════════════════════════════════

class EscalateTests(PlanShiftBase):
    def _pending_req(self):
        src = self._source_task()
        return plan_shift.create_request(src, 'X')

    def test_deputy_to_manager(self):
        req = self._pending_req()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)

        ok = plan_shift.escalate(req)
        self.assertTrue(ok)

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.MANAGER)
        # Recipients теперь менеджеры
        pks = set(req.recipients.values_list('pk', flat=True))
        self.assertIn(self.manager.pk, pks)

    def test_manager_to_plant(self):
        req = self._pending_req()
        plan_shift.escalate(req)  # → manager
        ok = plan_shift.escalate(req)  # → plant
        self.assertTrue(ok)

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.PLANT)
        self.assertEqual(
            list(req.recipients.values_list('pk', flat=True)),
            [self.plant.pk],
        )

    def test_plant_level_no_further_escalation(self):
        req = self._pending_req()
        plan_shift.escalate(req)  # → manager
        plan_shift.escalate(req)  # → plant
        ok = plan_shift.escalate(req)  # → no-op
        self.assertFalse(ok)
        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.PLANT)

    def test_previous_level_marked_timeout(self):
        req = self._pending_req()
        plan_shift.escalate(req)
        step = PlanShiftStep.objects.get(
            request=req, level=PlanShiftRequest.Level.DEPUTY,
        )
        self.assertEqual(step.decision, PlanShiftStep.Decision.TIMEOUT)
        self.assertIsNotNone(step.decided_at)

    def test_new_step_created(self):
        req = self._pending_req()
        plan_shift.escalate(req)
        steps = PlanShiftStep.objects.filter(
            request=req, level=PlanShiftRequest.Level.MANAGER,
        )
        self.assertEqual(steps.count(), 1)

    def test_non_pending_request_does_not_escalate(self):
        req = self._pending_req()
        req.status = PlanShiftRequest.Status.APPROVED
        req.save(update_fields=['status'])

        ok = plan_shift.escalate(req)
        self.assertFalse(ok)

    def test_deputy_missing_skips_to_plant(self):
        """DEPUTY → MANAGER, но менеджеров нет → сразу PLANT."""
        req = self._pending_req()
        # Удалим всех manager'ов
        User.objects.filter(role=self.role_mgr).delete()

        ok = plan_shift.escalate(req)
        self.assertTrue(ok)
        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.PLANT)


# ═════════════════════════════════════════════════════════════
#  approve
# ═════════════════════════════════════════════════════════════

class ApproveTests(PlanShiftBase):
    def _pending_req(self):
        src = self._source_task()
        other = self._other_open_task()
        req = plan_shift.create_request(src, 'Срочно')
        return req, src, other

    def test_approve_by_deputy_sets_approved(self):
        req, src, _ = self._pending_req()
        ok = plan_shift.approve(req, self.deputy, note='Ок')
        self.assertTrue(ok)

        req.refresh_from_db()
        self.assertEqual(req.status, PlanShiftRequest.Status.APPROVED)
        self.assertEqual(req.resolved_by, self.deputy)
        self.assertEqual(req.decision_note, 'Ок')
        self.assertIsNotNone(req.resolved_at)

    def test_approve_moves_source_back_to_new(self):
        req, src, _ = self._pending_req()
        plan_shift.approve(req, self.deputy)
        src.refresh_from_db()
        self.assertEqual(src.status, Task.Status.NEW)

    def test_approve_shifts_other_tasks(self):
        """Approve увеличивает credit; other с большим резервом не сдвигается."""
        req, src, other = self._pending_req()
        old_due = other.due

        plan_shift.approve(req, self.deputy)

        self.executor.refresh_from_db()
        self.assertEqual(self.executor.shift_credit_days, 3)

        src.refresh_from_db()
        self.assertEqual(src.planned_shift_days, 3)

        other.refresh_from_db()
        # reserve=4, credit=3 → shift=0. Срок не тронут.
        self.assertEqual(other.due, old_due)

    def test_approve_creates_shift_approved_log(self):
        req, src, _ = self._pending_req()
        plan_shift.approve(req, self.deputy)
        log = TaskLog.objects.filter(
            task=src, kind=TaskLog.Kind.SHIFT_APPROVED,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.shift_days, 3)

    def test_approve_marks_own_step_as_approved(self):
        req, _, _ = self._pending_req()
        plan_shift.approve(req, self.deputy)
        step = PlanShiftStep.objects.get(
            request=req, reviewer=self.deputy,
        )
        self.assertEqual(step.decision, PlanShiftStep.Decision.APPROVED)

    def test_approve_marks_other_steps_as_timeout(self):
        """Если согласующих несколько, остальные → TIMEOUT."""
        deputy2 = User.objects.create_user(
            email='dep2@test.ru', password='p', full_name='Заместитель 2',
            department=self.dep, role=self.role_deputy, is_active=True,
        )
        req, _, _ = self._pending_req()
        plan_shift.approve(req, self.deputy)
        step2 = PlanShiftStep.objects.get(
            request=req, reviewer=deputy2,
        )
        self.assertEqual(step2.decision, PlanShiftStep.Decision.TIMEOUT)

    def test_approve_second_time_returns_false(self):
        req, _, _ = self._pending_req()
        plan_shift.approve(req, self.deputy)
        ok = plan_shift.approve(req, self.deputy)
        self.assertFalse(ok)


# ═════════════════════════════════════════════════════════════
#  reject
# ═════════════════════════════════════════════════════════════

class RejectTests(PlanShiftBase):
    def _pending_req(self):
        src = self._source_task()
        other = self._other_open_task()
        req = plan_shift.create_request(src, 'Срочно')
        return req, src, other

    def test_reject_sets_status(self):
        req, _, _ = self._pending_req()
        ok = plan_shift.reject(req, self.deputy, note='Нет')
        self.assertTrue(ok)
        req.refresh_from_db()
        self.assertEqual(req.status, PlanShiftRequest.Status.REJECTED)
        self.assertEqual(req.resolved_by, self.deputy)

    def test_reject_cancels_source_task(self):
        req, src, _ = self._pending_req()
        plan_shift.reject(req, self.deputy, note='Нет')
        src.refresh_from_db()
        self.assertEqual(src.status, Task.Status.CANCELLED)
        self.assertIsNotNone(src.finished_at)

    def test_reject_does_not_shift_others(self):
        req, _, other = self._pending_req()
        old_due = other.due
        plan_shift.reject(req, self.deputy)
        other.refresh_from_db()
        self.assertEqual(other.due, old_due)

    def test_reject_creates_shift_rejected_log(self):
        req, src, _ = self._pending_req()
        plan_shift.reject(req, self.deputy, note='Плохое обоснование')
        log = TaskLog.objects.filter(
            task=src, kind=TaskLog.Kind.SHIFT_REJECTED,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.comment, 'Плохое обоснование')

    def test_reject_marks_step(self):
        req, _, _ = self._pending_req()
        plan_shift.reject(req, self.deputy)
        step = PlanShiftStep.objects.get(request=req, reviewer=self.deputy)
        self.assertEqual(step.decision, PlanShiftStep.Decision.REJECTED)


# ═════════════════════════════════════════════════════════════
#  find_reassign_candidates
# ═════════════════════════════════════════════════════════════

class FindReassignCandidatesTests(PlanShiftBase):
    def test_returns_same_dept_same_role(self):
        same = User.objects.create_user(
            email='same@test.ru', password='p', full_name='Коллега',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        # Не тот отдел
        other_dep_u = User.objects.create_user(
            email='otherdep@test.ru', password='p', full_name='Другой отдел',
            department=self.other_dep, role=self.role_staff, is_active=True,
        )
        # Другая роль
        other_role_u = User.objects.create_user(
            email='otherrole@test.ru', password='p', full_name='Другая роль',
            department=self.dep, role=self.role_mgr, is_active=True,
        )

        src = self._source_task()
        candidates = plan_shift.find_reassign_candidates(src)
        pks = {c['user'].pk for c in candidates}

        # Наш одноотдельческий коллега — кандидат
        self.assertIn(same.pk, pks)
        # Постановщик тоже staff в том же отделе → тоже кандидат
        self.assertIn(self.requester.pk, pks)
        # Чужой отдел / чужая роль — не кандидаты
        self.assertNotIn(other_dep_u.pk, pks)
        self.assertNotIn(other_role_u.pk, pks)
        # Сам себя не предлагаем
        self.assertNotIn(self.executor.pk, pks)

    def test_excludes_self(self):
        src = self._source_task()
        candidates = plan_shift.find_reassign_candidates(src)
        self.assertNotIn(self.executor.pk, [c['user'].pk for c in candidates])

    def test_no_department_returns_empty(self):
        self.executor.department = None
        self.executor.save(update_fields=['department'])
        src = self._source_task()
        candidates = plan_shift.find_reassign_candidates(src)
        self.assertEqual(candidates, [])

    def test_sorted_by_free_hours_desc(self):
        same1 = User.objects.create_user(
            email='s1@test.ru', password='p', full_name='Свободный',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        same2 = User.objects.create_user(
            email='s2@test.ru', password='p', full_name='Занятый',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        # same1 — без задач, same2 — с большой нагрузкой
        Task.objects.create(
            title='Груз', plan_hours=500.0, scale='s', kind='work',
            executor=same2, requester=self.requester,
            status=Task.Status.NEW,
        )
        src = self._source_task()
        candidates = plan_shift.find_reassign_candidates(src)
        pks = [c['user'].pk for c in candidates]

        # same1 (свободный) должен быть выше same2 (загруженного)
        self.assertIn(same1.pk, pks)
        self.assertIn(same2.pk, pks)
        self.assertLess(pks.index(same1.pk), pks.index(same2.pk))
        # same2 загружен полностью → его free_hours == 0
        same2_row = next(c for c in candidates if c['user'].pk == same2.pk)
        self.assertEqual(same2_row['free_hours'], 0)


# ═════════════════════════════════════════════════════════════
#  find_chain_tasks
# ═════════════════════════════════════════════════════════════

class FindChainTasksTests(PlanShiftBase):
    def _branch_setup(self):
        order = Order.objects.create(
            number='O-1', product='Генератор',
            ship_due=timezone.localdate() + timedelta(days=60),
        )
        br = TaskBranch.objects.create(order=order, name='Основная')
        return order, br

    def test_returns_blocked_stages_after_source(self):
        order, br = self._branch_setup()
        s1 = self._source_task(branch=br, stage_order=1)
        s2 = Task.objects.create(
            title='Этап 2', plan_hours=2.0, scale='s', kind='work',
            executor=self.executor, requester=self.requester,
            status=Task.Status.NEW,
            branch=br, stage_order=2, blocked_by_stage=True,
            order=order,
        )
        s3 = Task.objects.create(
            title='Этап 3', plan_hours=2.0, scale='s', kind='work',
            executor=self.executor, requester=self.requester,
            status=Task.Status.NEW,
            branch=br, stage_order=3, blocked_by_stage=True,
            order=order,
        )
        chain = plan_shift.find_chain_tasks(s1)
        self.assertEqual([t.pk for t in chain], [s2.pk, s3.pk])

    def test_excludes_unblocked(self):
        order, br = self._branch_setup()
        s1 = self._source_task(branch=br, stage_order=1)
        Task.objects.create(
            title='Уже разблокирован', plan_hours=2.0, scale='s', kind='work',
            executor=self.executor, requester=self.requester,
            status=Task.Status.NEW,
            branch=br, stage_order=2, blocked_by_stage=False,
            order=order,
        )
        chain = plan_shift.find_chain_tasks(s1)
        self.assertEqual(chain, [])

    def test_no_branch_returns_empty(self):
        s1 = self._source_task()  # без ветки
        chain = plan_shift.find_chain_tasks(s1)
        self.assertEqual(chain, [])

    def test_no_stage_order_returns_empty(self):
        order, br = self._branch_setup()
        s1 = self._source_task(branch=br, stage_order=None)
        chain = plan_shift.find_chain_tasks(s1)
        self.assertEqual(chain, [])


# ═════════════════════════════════════════════════════════════
#  can_split
# ═════════════════════════════════════════════════════════════

class CanSplitTests(PlanShiftBase):
    def test_ok_when_plan_large_enough(self):
        src = self._source_task(plan_hours=10.0)
        self.assertTrue(plan_shift.can_split(src))

    def test_false_when_plan_too_small(self):
        src = self._source_task(plan_hours=3.0)
        self.assertFalse(plan_shift.can_split(src))

    def test_false_when_done(self):
        src = self._source_task(
            plan_hours=10.0, status=Task.Status.DONE,
            finished_at=timezone.now(),
        )
        self.assertFalse(plan_shift.can_split(src))

    def test_true_when_pending_approval(self):
        """PENDING_APPROVAL — рабочее состояние во время ужима, разбивать можно."""
        src = self._source_task(
            plan_hours=10.0, status=Task.Status.PENDING_APPROVAL,
        )
        self.assertTrue(plan_shift.can_split(src))

    def test_false_when_cancelled(self):
        src = self._source_task(
            plan_hours=10.0, status=Task.Status.CANCELLED,
            finished_at=timezone.now(),
        )
        self.assertFalse(plan_shift.can_split(src))


# ═════════════════════════════════════════════════════════════
#  apply_squeeze
# ═════════════════════════════════════════════════════════════

class ApplySqueezeTests(PlanShiftBase):
    def _pending_req(self):
        src = self._source_task(plan_hours=10.0)
        req = plan_shift.create_request(src, 'X')
        return req, src

    def test_reassign_moves_task(self):
        req, src = self._pending_req()
        victim = self._other_open_task(title='Перекинуть')
        new_exec = User.objects.create_user(
            email='new@test.ru', password='p', full_name='Новый',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        result = plan_shift.apply_squeeze(
            req, self.deputy,
            {'reassign': {victim.pk: new_exec.pk}},
        )
        victim.refresh_from_db()
        self.assertEqual(victim.executor, new_exec)
        self.assertEqual(result['moved'], 1)
        req.refresh_from_db()
        self.assertEqual(req.status, PlanShiftRequest.Status.SQUEEZED)

    def test_unblock_clears_flag(self):
        order = Order.objects.create(
            number='O-1', product='X',
            ship_due=timezone.localdate() + timedelta(days=60),
        )
        br = TaskBranch.objects.create(order=order, name='B')
        src = self._source_task(
            plan_hours=10.0, branch=br, stage_order=1, order=order,
        )
        req = plan_shift.create_request(src, 'X')

        blocked = Task.objects.create(
            title='Этап 2', plan_hours=2.0, scale='s', kind='work',
            executor=self.executor, requester=self.requester,
            status=Task.Status.NEW,
            branch=br, stage_order=2, blocked_by_stage=True,
            order=order,
        )
        result = plan_shift.apply_squeeze(
            req, self.deputy, {'unblock': [blocked.pk]},
        )
        blocked.refresh_from_db()
        self.assertFalse(blocked.blocked_by_stage)
        self.assertEqual(result['unblocked'], 1)

    def test_split_creates_tail_task(self):
        req, src = self._pending_req()
        old_plan = src.plan_hours  # 10.0

        result = plan_shift.apply_squeeze(
            req, self.deputy,
            {'split': {'first_hours': 4.0, 'second_days': 3}},
        )
        self.assertTrue(result['split'])

        src.refresh_from_db()
        self.assertEqual(src.plan_hours, 4.0)

        tail = Task.objects.filter(
            title__startswith='[остаток]', executor=self.executor,
        ).first()
        self.assertIsNotNone(tail)
        self.assertEqual(tail.plan_hours, old_plan - 4.0)

    def test_split_skipped_if_not_splittable(self):
        req, src = self._pending_req()
        src.plan_hours = 3.0
        src.save(update_fields=['plan_hours'])

        result = plan_shift.apply_squeeze(
            req, self.deputy,
            {'split': {'first_hours': 1.0, 'second_days': 2}},
        )
        self.assertFalse(result['split'])

    def test_status_becomes_squeezed(self):
        req, _ = self._pending_req()
        plan_shift.apply_squeeze(req, self.deputy, {})
        req.refresh_from_db()
        self.assertEqual(req.status, PlanShiftRequest.Status.SQUEEZED)

    def test_source_back_to_new(self):
        req, src = self._pending_req()
        plan_shift.apply_squeeze(req, self.deputy, {})
        src.refresh_from_db()
        self.assertEqual(src.status, Task.Status.NEW)

    def test_empty_actions_still_squeezes(self):
        """apply_squeeze без действий — просто закрывает заявку."""
        req, _ = self._pending_req()
        result = plan_shift.apply_squeeze(req, self.deputy, {})
        self.assertEqual(result['moved'], 0)
        self.assertEqual(result['unblocked'], 0)
        self.assertFalse(result['split'])


# ═════════════════════════════════════════════════════════════
#  Views — базовые проверки доступа
# ═════════════════════════════════════════════════════════════

class ShiftRequestViewsTests(PlanShiftBase):
    def setUp(self):
        self.src = self._source_task(plan_hours=10.0)
        self.req = plan_shift.create_request(self.src, 'Очень срочно')

    def test_list_requires_login(self):
        r = self.client.get(reverse('shift_requests_list'))
        self.assertEqual(r.status_code, 302)

    def test_list_shows_own_pending(self):
        self.client.force_login(self.deputy)
        r = self.client.get(reverse('shift_requests_list'))
        self.assertEqual(r.status_code, 200)
        my = r.context['my_requests']
        self.assertEqual([x.pk for x in my], [self.req.pk])

    def test_list_excludes_foreign_for_regular_user(self):
        self.client.force_login(self.executor)
        r = self.client.get(reverse('shift_requests_list'))
        # Исполнитель не согласующий и не босс → 403
        self.assertEqual(r.status_code, 403)

    def test_detail_accessible_to_reviewer(self):
        self.client.force_login(self.deputy)
        r = self.client.get(
            reverse('shift_request_detail', args=[self.req.pk]),
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['can_review'])

    def test_detail_accessible_to_boss(self):
        self.client.force_login(self.manager)
        r = self.client.get(
            reverse('shift_request_detail', args=[self.req.pk]),
        )
        self.assertEqual(r.status_code, 200)

    def test_detail_forbidden_to_unrelated(self):
        stranger = User.objects.create_user(
            email='str@test.ru', password='p', full_name='Чужой',
            department=self.dep, role=self.role_staff, is_active=True,
        )
        self.client.force_login(stranger)
        r = self.client.get(
            reverse('shift_request_detail', args=[self.req.pk]),
        )
        self.assertEqual(r.status_code, 403)

    def test_approve_via_view(self):
        self.client.force_login(self.deputy)
        r = self.client.post(
            reverse('shift_request_approve', args=[self.req.pk]),
            {'note': 'Ок'},
        )
        self.assertEqual(r.status_code, 302)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, PlanShiftRequest.Status.APPROVED)

    def test_reject_via_view(self):
        self.client.force_login(self.deputy)
        r = self.client.post(
            reverse('shift_request_reject', args=[self.req.pk]),
            {'note': 'Нет'},
        )
        self.assertEqual(r.status_code, 302)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, PlanShiftRequest.Status.REJECTED)

    def test_approve_forbidden_for_non_reviewer(self):
        self.client.force_login(self.executor)
        r = self.client.post(
            reverse('shift_request_approve', args=[self.req.pk]),
        )
        self.assertEqual(r.status_code, 403)

    def test_get_approve_not_allowed(self):
        """approve защищён @require_POST."""
        self.client.force_login(self.deputy)
        r = self.client.get(
            reverse('shift_request_approve', args=[self.req.pk]),
        )
        # 405 от декоратора, или редирект от логики
        self.assertIn(r.status_code, (405,))

# ═════════════════════════════════════════════════════════════
#  Management-команда: escalate_shift_requests
# ═════════════════════════════════════════════════════════════

class EscalateShiftRequestsCommandTests(PlanShiftBase):
    """Тесты management-команды escalate_shift_requests.

    Команда:
      1. Берёт только PENDING-заявки со escalate_at <= now.
      2. Для каждой дёргает plan_shift.escalate(req).
      3. Пропускает уже решённые и «не пора ещё» — не трогает.
      4. Глотает исключения: одна упавшая заявка не рушит проход.
      5. --dry-run: печатает, но не мутирует.

    Создаём заявку через plan_shift.create_request (у нас в базе
    есть deputy → DEPUTY-уровень, escalate_at = now + 2h), потом
    перебиваем escalate_at через .update() — в прошлое или будущее.
    """

    def _call(self, **kwargs):
        out, err = StringIO(), StringIO()
        call_command(
            'escalate_shift_requests',
            stdout=out, stderr=err, verbosity=0, **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def _pending_request(self, escalate_minutes_from_now=0, **kw):
        """Создать PENDING-заявку с заданным escalate_at.

        Отрицательное значение — в прошлом (пора эскалировать).
        Положительное — в будущем (ещё не пора).
        """
        src = self._source_task(**kw)
        req = plan_shift.create_request(src, 'X')
        self.assertIsNotNone(req, 'ожидали, что заявка создастся')

        PlanShiftRequest.objects.filter(pk=req.pk).update(
            escalate_at=timezone.now() + timedelta(minutes=escalate_minutes_from_now),
        )
        req.refresh_from_db()
        return req

    # ── Эскалация ───────────────────────────────────────────────

    def test_escalates_overdue_pending(self):
        """Просрочен escalate_at → DEPUTY → MANAGER."""
        req = self._pending_request(escalate_minutes_from_now=-60)
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)

        self._call()

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.MANAGER)

    def test_does_not_touch_future_requests(self):
        """escalate_at в будущем → не трогаем."""
        req = self._pending_request(escalate_minutes_from_now=60)
        self._call()

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)

    def test_does_not_touch_resolved_requests(self):
        """APPROVED-заявки игнорируются, даже если escalate_at в прошлом."""
        req = self._pending_request(escalate_minutes_from_now=-60)
        PlanShiftRequest.objects.filter(pk=req.pk).update(
            status=PlanShiftRequest.Status.APPROVED,
            resolved_at=timezone.now(),
            resolved_by=self.deputy,
        )

        self._call()

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(req.status, PlanShiftRequest.Status.APPROVED)

    def test_escalates_multiple_requests(self):
        """Три просроченные заявки — все эскалируются за один прогон."""
        reqs = [self._pending_request(escalate_minutes_from_now=-60) for _ in range(3)]
        self._call()

        for r in reqs:
            r.refresh_from_db()
            self.assertEqual(r.current_level, PlanShiftRequest.Level.MANAGER)

    def test_mixed_queue_only_overdue_escalated(self):
        """Из двух — только та, у которой escalate_at в прошлом."""
        overdue = self._pending_request(escalate_minutes_from_now=-60)
        future = self._pending_request(escalate_minutes_from_now=60)

        self._call()

        overdue.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(overdue.current_level, PlanShiftRequest.Level.MANAGER)
        self.assertEqual(future.current_level, PlanShiftRequest.Level.DEPUTY)

    # ── --dry-run ───────────────────────────────────────────────

    def test_dry_run_does_not_escalate(self):
        req = self._pending_request(escalate_minutes_from_now=-60)
        out, _ = self._call(dry_run=True)

        req.refresh_from_db()
        self.assertEqual(req.current_level, PlanShiftRequest.Level.DEPUTY)
        self.assertIn('[dry]', out)
        self.assertIn('Эскалировано: 1', out)

    def test_dry_run_does_not_touch_future(self):
        self._pending_request(escalate_minutes_from_now=60)
        out, _ = self._call(dry_run=True)
        self.assertIn('Эскалировано: 0', out)

    # ── continue-on-error ───────────────────────────────────────

    def test_continues_after_exception(self):
        """Одна упавшая заявка не мешает обработать остальные."""
        r1 = self._pending_request(escalate_minutes_from_now=-60)
        r2 = self._pending_request(escalate_minutes_from_now=-60)

        original = plan_shift.escalate

        def fake(req):
            if req.pk == r1.pk:
                raise RuntimeError('boom')
            return original(req)

        with patch.object(plan_shift, 'escalate') as m:
            m.side_effect = fake
            out, err = self._call()

        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual(r1.current_level, PlanShiftRequest.Level.DEPUTY)
        self.assertEqual(r2.current_level, PlanShiftRequest.Level.MANAGER)

        self.assertIn('boom', err)
        # Успешная обработана, падение в счётчик не попало
        self.assertIn('Эскалировано: 1', out)

    # ── Счётчик ─────────────────────────────────────────────────

    def test_counter_reflects_only_escalated(self):
        """Эскалированные, но не «уже на PLANT» — считаются."""
        # Одна нормальная
        self._pending_request(escalate_minutes_from_now=-60)
        # Одна — уже на PLANT: escalate вернёт False (нечего дальше)
        plant_req = self._pending_request(escalate_minutes_from_now=-60)
        PlanShiftRequest.objects.filter(pk=plant_req.pk).update(
            current_level=PlanShiftRequest.Level.PLANT,
        )

        out, _ = self._call()
        # plant-уровень: escalate() вернёт False → в счётчик не попадёт
        self.assertIn('Эскалировано: 1', out)

# ═════════════════════════════════════════════════════════════
#  Management-команда: notify_order_deadlines
# ═════════════════════════════════════════════════════════════

class NotifyOrderDeadlinesCommandTests(PlanShiftBase):
    """Тесты management-команды notify_order_deadlines.

    Команда:
      - перебирает 4 анкора × пороги 7 и 3 рабочих дня;
      - ищет заказы, у которых anchor == add_work_days(today, N);
      - шлёт Notification владельцу и пишет OrderDeadlineNotification;
      - дедуп по (order, anchor, days_before, anchor_date);
      - если anchor_date изменилась — отправит заново;
      - без owner → ничего не шлёт.

    Чтобы не зависеть от праздников РФ, дату КТ считаем той же
    функцией add_work_days, что использует команда.
    """

    def _call(self, **kwargs):
        out, err = StringIO(), StringIO()
        call_command(
            'notify_order_deadlines',
            stdout=out, stderr=err, verbosity=0, **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def _order(self, **kw):
        base = {
            'number': 'ND-001',
            'product': 'Изделие-notify',
            'owner': self.deputy,
        }
        base.update(kw)
        return Order.objects.create(**base)

    def _notifications_for(self, order):
        # Все уведомления по этому заказу, отправленные командой
        return Notification.objects.filter(
            recipient=order.owner,
            text__startswith=f'📅 Заказ {order.number}',
        )

    def _set_anchor_7d(self, order, attr='design_end'):
        """Проставить указанному полю дату через 7 раб. дней от today."""
        today = timezone.localdate()
        target = add_work_days(today, 7)
        setattr(order, attr, target)
        order.save(update_fields=[attr])
        return target

    def _set_anchor_3d(self, order, attr='ship_due'):
        today = timezone.localdate()
        target = add_work_days(today, 3)
        setattr(order, attr, target)
        order.save(update_fields=[attr])
        return target

    # ── Базовые сценарии ────────────────────────────────────────

    def test_sends_notification_for_7_days_before(self):
        order = self._order()
        self._set_anchor_7d(order, attr='design_end')

        self._call()

        self.assertEqual(self._notifications_for(order).count(), 1)
        text = self._notifications_for(order).first().text
        self.assertIn('7 раб. д.', text)
        self.assertIn('Конец проектирования', text)

    def test_sends_notification_for_3_days_before(self):
        order = self._order()
        self._set_anchor_3d(order, attr='ship_due')

        self._call()

        self.assertEqual(self._notifications_for(order).count(), 1)
        text = self._notifications_for(order).first().text
        self.assertIn('3 раб. д.', text)
        self.assertIn('Отгрузка', text)

    def test_both_thresholds_fire_for_same_order(self):
        """Одна и та же точка за 7 и за 3 дня — два разных уведомления."""
        order = self._order()

        # КТ на +7 дней
        target_7 = add_work_days(timezone.localdate(), 7)
        order.design_end = target_7
        order.save(update_fields=['design_end'])
        self._call()

        # Сдвигаем КТ на +3 дня
        target_3 = add_work_days(timezone.localdate(), 3)
        order.design_end = target_3
        order.save(update_fields=['design_end'])
        self._call()

        # Два уведомления: одно за 7 (для target_7), одно за 3 (для target_3)
        self.assertEqual(self._notifications_for(order).count(), 2)

    def test_all_four_anchors_send_at_7(self):
        """Все 4 КТ на +7 дней — четыре уведомления."""
        order = self._order()
        today = timezone.localdate()
        target = add_work_days(today, 7)
        for attr in ('contract_start', 'design_start', 'design_end', 'ship_due'):
            setattr(order, attr, target)
        order.save(update_fields=[
            'contract_start', 'design_start', 'design_end', 'ship_due',
        ])

        self._call()

        self.assertEqual(self._notifications_for(order).count(), 4)

    # ── Дедупликация ────────────────────────────────────────────

    def test_deduplication_same_run_twice(self):
        """Второй прогон без изменений — не шлёт повторно."""
        order = self._order()
        self._set_anchor_7d(order, attr='design_end')

        self._call()
        self._call()

        self.assertEqual(self._notifications_for(order).count(), 1)
        self.assertEqual(
            OrderDeadlineNotification.objects.filter(order=order).count(), 1,
        )

    def test_anchor_date_change_resends(self):
        """Контракт модели: разные anchor_date — разные записи.

        Механизм дедупа такой: команда ищет существующую запись
        с точным (order, anchor, days_before, anchor_date). Если
        КТ перенесли — anchor_date другая, команда создаст новую
        запись и отправит уведомление заново. Это и есть «resend
        при переносе», а не какой-то отдельный код.
        """
        order = self._order()
        today = timezone.localdate()
        day_7 = add_work_days(today, 7)
        day_8 = add_work_days(today, 8)

        OrderDeadlineNotification.objects.create(
            order=order, anchor='design_end',
            days_before=7, anchor_date=day_7,
        )
        OrderDeadlineNotification.objects.create(
            order=order, anchor='design_end',
            days_before=7, anchor_date=day_8,
        )

        self.assertEqual(
            OrderDeadlineNotification.objects.filter(order=order).count(), 2,
        )

    # ── Без owner / неактивный owner ────────────────────────────

    def test_no_owner_means_no_notification(self):
        """Заказ без owner не попадает в выборку вообще.

        ⚠️ Команда фильтрует owner__isnull=False прямо в SQL
        (см. _orders_to_notify), поэтому такой заказ не доходит
        до проверки получателей и НЕ считается в «пропущено».
        Проверяем это осознанно — не как баг, а как контракт.
        """
        order = self._order(owner=None)
        self._set_anchor_7d(order, attr='design_end')

        out, _ = self._call()

        self.assertEqual(Notification.objects.filter(
            text__startswith=f'📅 Заказ {order.number}',
        ).count(), 0)
        self.assertEqual(
            OrderDeadlineNotification.objects.filter(order=order).count(), 0,
        )
        self.assertIn('пропущено: 0', out)

    def test_inactive_owner_skipped(self):
        self.deputy.is_active = False
        self.deputy.save(update_fields=['is_active'])

        order = self._order()
        self._set_anchor_7d(order, attr='design_end')

        self._call()

        # Уведомления в БД нет: _recipients вернул []
        self.assertEqual(Notification.objects.filter(
            text__startswith=f'📅 Заказ {order.number}',
        ).count(), 0)

    # ── Нет подходящих заказов ──────────────────────────────────

    def test_no_orders_no_notifications(self):
        out, _ = self._call()
        self.assertIn('Отправлено: 0', out)

    def test_far_future_order_ignored(self):
        """КТ далеко (не за 7 и не за 3) — не трогаем."""
        order = self._order()
        today = timezone.localdate()
        order.design_end = add_work_days(today, 30)
        order.save(update_fields=['design_end'])

        self._call()

        self.assertEqual(self._notifications_for(order).count(), 0)

    # ── --dry-run ───────────────────────────────────────────────

    def test_dry_run_does_not_send(self):
        order = self._order()
        self._set_anchor_7d(order, attr='design_end')

        out, _ = self._call(dry_run=True)

        self.assertEqual(self._notifications_for(order).count(), 0)
        self.assertEqual(
            OrderDeadlineNotification.objects.filter(order=order).count(), 0,
        )
        self.assertIn('[dry]', out)
        self.assertIn('Отправлено: 1', out)

    # ── Контракт дедупа на уровне модели ────────────────────────

    def test_dedup_constraint_unique(self):
        """UniqueConstraint по (order, anchor, days_before, anchor_date)."""
        order = self._order()
        today = timezone.localdate()
        target = add_work_days(today, 7)

        OrderDeadlineNotification.objects.create(
            order=order, anchor='design_end', days_before=7, anchor_date=target,
        )
        with self.assertRaises(Exception):
            OrderDeadlineNotification.objects.create(
                order=order, anchor='design_end', days_before=7, anchor_date=target,
            )
