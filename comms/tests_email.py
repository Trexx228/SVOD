"""Автотесты comms.email: письма по задачам и утренний дайджест.

═══ Про подводные камни ═══

  1. `_can_email` возвращает False по ЧЕТЫРЁМ причинам:
     неактивный, без email, email_notifications=False, или сам None.
     Каждая ветка должна быть покрыта.
  2. `_send` ГЛОТАЕТ любые ошибки `send_mail` — email некритичный
     side-effect, падение SMTP не должно ломать бизнес-флоу.
  3. `notify_task_review` идёт REQUESTER'у (постановщику), а
     `notify_task_assigned` — EXECUTOR'у. Не перепутать!
  4. `send_daily_digest` возвращает False если total == 0 —
     не спамит пустыми письмами.
  5. Дайджест усекает списки: просрочено > 10 → «…и ещё N»,
     на сегодня > 15 → «…и ещё N», приёмка > 10 → «…и ещё N».
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Department, Role
from comms.email import (
    _can_email, _task_url, notify_task_accepted, notify_task_assigned,
    notify_task_returned, notify_task_review, send_daily_digest,
)
from tasks.models import Task

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class EmailBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep = Department.objects.get_or_create(name='ОГК')[0]
        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)

        cls.executor = User.objects.create_user(
            email='exec@test.ru', password='p', full_name='Исполнитель',
            department=cls.dep, role=cls.role_staff, is_active=True,
        )
        cls.requester = User.objects.create_user(
            email='req@test.ru', password='p', full_name='Постановщик',
            department=cls.dep, role=cls.role_mgr, is_active=True,
        )

    def _task(self, **kw):
        defaults = {
            'title': 'Задача', 'plan_hours': 4.0,
            'scale': 's', 'kind': 'work',
            'priority': Task.Priority.MEDIUM,
            'executor': self.executor, 'requester': self.requester,
            'status': Task.Status.NEW,
            'due': timezone.localdate() + timedelta(days=3),
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)


# ═════════════════════════════════════════════════════════════
#  _can_email
# ═════════════════════════════════════════════════════════════

class CanEmailTests(EmailBase):
    def test_ok(self):
        self.assertTrue(_can_email(self.executor))

    def test_none(self):
        self.assertFalse(_can_email(None))

    def test_inactive(self):
        self.executor.is_active = False
        self.assertFalse(_can_email(self.executor))

    def test_no_email(self):
        u = User(
            email='', full_name='Без почты', is_active=True,
        )
        u.set_password('p')
        u.save()
        self.assertFalse(_can_email(u))

    def test_notifications_disabled(self):
        self.executor.email_notifications = False
        self.assertFalse(_can_email(self.executor))

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_zero_email_blank(self):
        """⚠ Дополнительно: user без email — модель User требует email
        на уровне Manager, но БД может содержать. Проверяем что
        пустая строка не проходит.
        """
        u = User(email='', full_name='X', is_active=True)
        u.set_password('p')
        u.save()
        self.assertFalse(_can_email(u))


# ═════════════════════════════════════════════════════════════
#  _task_url
# ═════════════════════════════════════════════════════════════

class TaskUrlTests(EmailBase):
    def test_without_site_url(self):
        t = self._task()
        url = _task_url(t)
        self.assertTrue(url.startswith('/tasks/task/'))
        self.assertIn(str(t.pk), url)

    @override_settings(SITE_URL='')
    def test_empty_site_url(self):
        t = self._task()
        url = _task_url(t)
        self.assertTrue(url.startswith('/tasks/task/'))

    @override_settings(SITE_URL='https://svod.example.com')
    def test_with_site_url(self):
        t = self._task()
        url = _task_url(t)
        self.assertTrue(url.startswith('https://svod.example.com/tasks/task/'))

    @override_settings(SITE_URL='https://svod.example.com/')
    def test_site_url_trailing_slash_stripped(self):
        """⚠ `base.rstrip('/')` — не должно быть двойного слэша."""
        t = self._task()
        url = _task_url(t)
        self.assertNotIn('//tasks', url)
        self.assertIn('https://svod.example.com/tasks/task/', url)


# ═════════════════════════════════════════════════════════════
#  _send (через публичные notify_*)
# ═════════════════════════════════════════════════════════════

class SendBehaviourTests(EmailBase):
    @patch('comms.email.send_mail')
    def test_send_success(self, mock_send):
        t = self._task(title='Тестовая')
        ok = notify_task_assigned(t)
        self.assertTrue(ok)
        mock_send.assert_called_once()

    @patch('comms.email.send_mail')
    def test_send_swallows_exception(self, mock_send):
        """⚠ `_send` глотает любое исключение SMTP → возвращает False.

        Это осознанно: письмо некритично, падение SMTP не должно
        ломать создание/возврат/приёмку задачи.

        Лог `logger.exception` глушим, чтобы зелёный прогон не выглядел
        как ошибка. Логика теста проверяет именно возврат False.
        """
        mock_send.side_effect = RuntimeError('SMTP down')
        t = self._task()

        with self.assertLogs('comms.email', level='ERROR') as log_ctx:
            ok = notify_task_assigned(t)

        self.assertFalse(ok)
        self.assertTrue(any('Failed to send' in m for m in log_ctx.output))

    @patch('comms.email.send_mail')
    def test_send_not_called_when_cannot_email(self, mock_send):
        self.executor.email_notifications = False
        self.executor.save(update_fields=['email_notifications'])
        t = self._task()
        ok = notify_task_assigned(t)
        self.assertFalse(ok)
        mock_send.assert_not_called()


# ═════════════════════════════════════════════════════════════
#  notify_task_assigned
# ═════════════════════════════════════════════════════════════

class NotifyAssignedTests(EmailBase):
    @patch('comms.email.send_mail')
    def test_sends_to_executor(self, mock_send):
        t = self._task(title='Важно')
        notify_task_assigned(t)
        args, kwargs = mock_send.call_args
        recipients = kwargs.get('recipient_list') or args[3]
        self.assertEqual(recipients, [self.executor.email])

    @patch('comms.email.send_mail')
    def test_subject_contains_title(self, mock_send):
        t = self._task(title='Чертёж корпуса')
        notify_task_assigned(t)
        subject = mock_send.call_args.kwargs.get('subject') or \
                  mock_send.call_args.args[0]
        self.assertIn('Чертёж корпуса', subject)

    @patch('comms.email.send_mail')
    def test_body_contains_key_fields(self, mock_send):
        t = self._task(
            title='Чертёж',
            priority=Task.Priority.HIGH,
            plan_hours=5.0,                                    # ← ключевое
            start_due=timezone.localdate() + timedelta(days=1),
            due=timezone.localdate() + timedelta(days=5),
        )
        notify_task_assigned(t)
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('Чертёж', body)
        self.assertIn('Высокий', body)
        self.assertIn('5.0 ч', body)         # план часов
        self.assertIn('Начать', body)        # блок start_due
        self.assertIn('Срок', body)          # блок due
        self.assertIn('Постановщик', body)

    @patch('comms.email.send_mail')
    def test_body_without_start_due_omits_start_block(self, mock_send):
        """⚠ Если start_due=None, строка «Начать:» не должна появляться."""
        t = self._task(
            title='Без старта',
            start_due=None,
            due=timezone.localdate() + timedelta(days=5),
        )
        notify_task_assigned(t)
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertNotIn('Начать:', body)

    @patch('comms.email.send_mail')
    def test_body_without_due_omits_due_block(self, mock_send):
        """⚠ Если due=None — строки «Срок:» нет."""
        t = self._task(title='Без срока', due=None)
        notify_task_assigned(t)
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertNotIn('Срок:', body)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    @patch('comms.email.send_mail')
    def test_no_recipient_when_inactive(self, mock_send):
        self.executor.is_active = False
        self.executor.save(update_fields=['is_active'])
        t = self._task()
        ok = notify_task_assigned(t)
        self.assertFalse(ok)
        mock_send.assert_not_called()


# ═════════════════════════════════════════════════════════════
#  notify_task_returned
# ═════════════════════════════════════════════════════════════

class NotifyReturnedTests(EmailBase):
    @patch('comms.email.send_mail')
    def test_sends_to_executor(self, mock_send):
        t = self._task(status=Task.Status.REWORK)
        notify_task_returned(t, comment='Переделать')
        recipients = mock_send.call_args.kwargs.get('recipient_list') or \
                     mock_send.call_args.args[3]
        self.assertEqual(recipients, [self.executor.email])

    @patch('comms.email.send_mail')
    def test_body_contains_comment(self, mock_send):
        t = self._task()
        notify_task_returned(t, comment='Плохо оформлено')
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('Плохо оформлено', body)

    @patch('comms.email.send_mail')
    def test_works_without_comment(self, mock_send):
        t = self._task()
        ok = notify_task_returned(t, comment='')
        self.assertTrue(ok)


# ═════════════════════════════════════════════════════════════
#  notify_task_accepted
# ═════════════════════════════════════════════════════════════

class NotifyAcceptedTests(EmailBase):
    @patch('comms.email.send_mail')
    def test_sends_to_executor(self, mock_send):
        t = self._task(status=Task.Status.DONE,
                       finished_at=timezone.now())
        notify_task_accepted(t)
        recipients = mock_send.call_args.kwargs.get('recipient_list') or \
                     mock_send.call_args.args[3]
        self.assertEqual(recipients, [self.executor.email])

    @patch('comms.email.send_mail')
    def test_subject_mentions_accepted(self, mock_send):
        t = self._task()
        notify_task_accepted(t)
        subject = mock_send.call_args.kwargs.get('subject') or \
                  mock_send.call_args.args[0]
        self.assertIn('принята', subject)


# ═════════════════════════════════════════════════════════════
#  notify_task_review — ⚠ идёт REQUESTER'у!
# ═════════════════════════════════════════════════════════════

class NotifyReviewTests(EmailBase):
    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    @patch('comms.email.send_mail')
    def test_sends_to_requester_not_executor(self, mock_send):
        """⚠ Направление обратное `notify_task_assigned`:
        сдача на проверку → письмо ИДЁТ ПОСТАНОВЩИКУ (не исполнителю).
        """
        t = self._task(
            status=Task.Status.REVIEW, finished_at=timezone.now(),
        )
        notify_task_review(t)
        recipients = mock_send.call_args.kwargs.get('recipient_list') or \
                     mock_send.call_args.args[3]
        self.assertEqual(recipients, [self.requester.email])
        self.assertNotIn(self.executor.email, recipients)

    @patch('comms.email.send_mail')
    def test_body_contains_executor_name(self, mock_send):
        t = self._task(
            status=Task.Status.REVIEW, finished_at=timezone.now(),
        )
        notify_task_review(t)
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('Исполнитель', body)


# ═════════════════════════════════════════════════════════════
#  send_daily_digest
# ═════════════════════════════════════════════════════════════

class DailyDigestTests(EmailBase):
    @patch('comms.email.send_mail')
    def test_no_email_when_no_tasks(self, mock_send):
        """⚠ total == 0 → False, письмо не отправлено."""
        ok = send_daily_digest(self.executor, [], [], [])
        self.assertFalse(ok)
        mock_send.assert_not_called()

    @patch('comms.email.send_mail')
    def test_sends_with_today_tasks(self, mock_send):
        today = timezone.localdate()
        t = self._task(title='Сегодня', due=today)
        ok = send_daily_digest(self.executor, [t], [], [])
        self.assertTrue(ok)
        subject = mock_send.call_args.kwargs.get('subject') or \
                  mock_send.call_args.args[0]
        self.assertIn('Задачи на', subject)

    @patch('comms.email.send_mail')
    def test_body_sections(self, mock_send):
        today = timezone.localdate()
        t_today = self._task(title='Сегодня', due=today)
        t_over = self._task(title='Просрочена',
                            due=today - timedelta(days=2))
        t_review = self._task(
            title='На приёмке', status=Task.Status.REVIEW,
            finished_at=timezone.now(),
        )
        send_daily_digest(self.executor, [t_today], [t_over], [t_review])
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('ПРОСРОЧЕНО', body)
        self.assertIn('На сегодня', body)
        self.assertIn('Ждут приёмки', body)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    @patch('comms.email.send_mail')
    def test_overdue_truncated_at_10(self, mock_send):
        """⚠ > 10 просроченных → «…и ещё N»."""
        today = timezone.localdate()
        tasks = [
            self._task(title=f'P{i}', due=today - timedelta(days=i+1))
            for i in range(15)
        ]
        send_daily_digest(self.executor, [], tasks, [])
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('…и ещё 5', body)

    @patch('comms.email.send_mail')
    def test_today_truncated_at_15(self, mock_send):
        today = timezone.localdate()
        tasks = [
            self._task(title=f'T{i}', due=today)
            for i in range(20)
        ]
        send_daily_digest(self.executor, tasks, [], [])
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('…и ещё 5', body)

    @patch('comms.email.send_mail')
    def test_review_truncated_at_10(self, mock_send):
        tasks = [
            self._task(
                title=f'R{i}', status=Task.Status.REVIEW,
                finished_at=timezone.now(),
            )
            for i in range(12)
        ]
        send_daily_digest(self.executor, [], [], tasks)
        body = mock_send.call_args.kwargs.get('message') or \
               mock_send.call_args.args[1]
        self.assertIn('…и ещё 2', body)

    @patch('comms.email.send_mail')
    def test_total_counts_all_sections(self, mock_send):
        """total = len(today) + len(overdue) + len(review)."""
        today = timezone.localdate()
        t1 = self._task(due=today)
        t2 = self._task(due=today - timedelta(days=1))
        t3 = self._task(status=Task.Status.REVIEW,
                        finished_at=timezone.now())
        send_daily_digest(self.executor, [t1], [t2], [t3])
        subject = mock_send.call_args.kwargs.get('subject') or \
                  mock_send.call_args.args[0]
        self.assertIn('(3)', subject)
