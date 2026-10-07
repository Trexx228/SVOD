"""Тесты бизнес-задач №1–7: автостарт, сроки, хронология, попап,
оповещение о старте, цеховой таймер, статистика завода."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from comms.models import Notification
from tasks.models import ShopSession, Task, TaskLog

User = get_user_model()


class BusinessBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep, _ = Department.objects.get_or_create(name='ОГК')

        cls.role_emp, _ = Role.objects.get_or_create(
            code='staff-test',
            defaults={
                'name': 'Сотрудник',
                'can_manage': False, 'can_admin': False,
            })
        cls.role_mgr, _ = Role.objects.get_or_create(
            code='manager-test',
            defaults={
                'name': 'Руководитель',
                'can_manage': True, 'can_admin': False,
            })
        cls.role_adm, _ = Role.objects.get_or_create(
            code='admin-test',
            defaults={
                'name': 'Директор',
                'can_manage': True, 'can_admin': True,
            })

        cls.boss = User.objects.create_user(
            email='boss@zavod.ru', password='p', full_name='Босс Боссов',
            department=cls.dep, role=cls.role_mgr, is_active=True)
        cls.admin = User.objects.create_user(
            email='dir@zavod.ru', password='p', full_name='Директор Завода',
            department=cls.dep, role=cls.role_adm, is_active=True)
        cls.emp = User.objects.create_user(
            email='emp@zavod.ru', password='p', full_name='Иванов Иван',
            department=cls.dep, role=cls.role_emp, is_active=True)
        cls.emp2 = User.objects.create_user(
            email='emp2@zavod.ru', password='p', full_name='Петров Пётр',
            department=cls.dep, role=cls.role_emp, is_active=True)

    def create_task(self, **kw):
        """Хелпер: поставить задачу и убедиться, что она создана.

        Возвращает HTTP-ответ. Если форма не прошла валидацию —
        тест падает с понятным сообщением, а не с Task.DoesNotExist.
        """
        self.client.force_login(self.boss)
        data = {
            'title': kw.get('title', 'Тестовая задача'),
            'plan': kw.get('plan', '2'),
            'priority': kw.get('priority', 'medium'),
            'scale': 's', 'kind': 'work',
            'exec_ids': kw.get('exec_ids', str(self.emp.pk)),
            'start_due': kw.get('start_due', ''),
            'due': kw.get('due', ''),
            'order': kw.get('order', ''),
            'outside_order': kw.get('outside_order', '1'),
            'type': '', 'body': '',
        }
        resp = self.client.post(reverse('task_create'), data)

        if kw.get('expect_error'):
            return resp

        # Если ждали 302, а получили 200 — значит форма не прошла валидацию.
        # Достаём сообщения из контекста, только если он реально есть
        # (у redirect-ответа resp.context = None).
        if resp.status_code != 302:
            errors = []
            ctx = getattr(resp, 'context', None)
            if ctx is not None:
                try:
                    errors = [str(m) for m in ctx.get('messages', [])]
                except Exception:
                    pass
            self.fail(
                f'create_task: форма вернула {resp.status_code}, '
                f'ожидался редирект. Ошибки: {errors or "—"}'
            )

        return resp


# ── №1: старт задачи — только вручную ───────────────────────────

class TestAutoStart(BusinessBase):
    """Автостарта нет: таймер запускает исполнитель вручную."""

    def test_with_due_stays_new_until_manual_start(self):
        due = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.create_task(title='С дедлайном', due=due)

        t = Task.objects.get(title='С дедлайном')
        self.assertEqual(t.status, Task.Status.NEW)
        self.assertIsNone(t.session_started_at)

        self.client.force_login(self.emp)
        self.client.post(reverse('task_action', args=[t.pk, 'start']))

        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)
        self.assertIsNotNone(t.session_started_at)

    def test_group_children_stay_new(self):
        due = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.create_task(
            title='Групповая', due=due,
            exec_ids=f'{self.emp.pk},{self.emp2.pk}',
        )

        children = Task.objects.filter(parent__isnull=False)
        self.assertEqual(children.count(), 2)
        for c in children:
            self.assertEqual(c.status, Task.Status.NEW)
            self.assertIsNone(c.session_started_at)


# ── №2: срок начала и окончания ─────────────────────────────────

class TestStartDue(BusinessBase):
    """При постановке указываются начало и конец."""

    def test_dates_saved(self):
        s = (timezone.localdate() + timedelta(days=1)).isoformat()
        d = (timezone.localdate() + timedelta(days=4)).isoformat()
        self.create_task(title='Со сроками', start_due=s, due=d)

        t = Task.objects.get(title='Со сроками')
        self.assertEqual(t.start_due.isoformat(), s)
        self.assertEqual(t.due.isoformat(), d)

    def test_start_after_due_rejected(self):
        s = (timezone.localdate() + timedelta(days=5)).isoformat()
        d = (timezone.localdate() + timedelta(days=1)).isoformat()

        before = Task.objects.count()
        self.create_task(
            title='Кривая', start_due=s, due=d,
            expect_error=True,
        )
        self.assertEqual(Task.objects.count(), before)

    def test_detail_shows_start(self):
        s = (timezone.localdate() + timedelta(days=1)).isoformat()
        d = (timezone.localdate() + timedelta(days=4)).isoformat()
        self.create_task(title='Показать срок', start_due=s, due=d)

        t = Task.objects.get(title='Показать срок')
        self.client.force_login(self.emp)
        resp = self.client.get(reverse('task_detail', args=[t.pk]))
        self.assertContains(resp, t.start_due.strftime('%d.%m.%Y'))


# ── №3: хронология ──────────────────────────────────────────────

class TestTimeline(BusinessBase):
    """Хронология месяца, красные — приоритет/просрочка, сдвиг."""

    def _task_in_month(self, priority='medium'):
        s = timezone.localdate().replace(day=1) + timedelta(days=2)
        d = s + timedelta(days=3)
        self.create_task(
            title='В хронологии',
            start_due=s.isoformat(), due=d.isoformat(),
            priority=priority,
        )
        return Task.objects.get(title='В хронологии')

    def test_timeline_forbidden_for_staff(self):
        """Хронология — только руководитель/админ. Обычный сотрудник — 403."""
        self._task_in_month()
        self.client.force_login(self.emp)
        resp = self.client.get(
            reverse('tasks_registry'), {'view': 'timeline'},
        )
        self.assertEqual(resp.status_code, 403)

    def test_overdue_marked_late(self):
        today = timezone.localdate()
        if today.day < 3:
            self.skipTest('В начале месяца нет места для просрочки внутри месяца')
        s = today - timedelta(days=2)
        d = today - timedelta(days=1)
        self.create_task(
            title='Просрочка', start_due=s.isoformat(), due=d.isoformat(),
        )
        self.client.force_login(self.boss)
        resp = self.client.get(
            reverse('tasks_registry'), {'view': 'timeline'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'tl-bar late')

    def test_shift_moves_dates_and_logs(self):
        t = self._task_in_month()
        old_s, old_d = t.start_due, t.due
        self.client.force_login(self.emp)
        self.client.post(reverse('task_shift', args=[t.pk]), {'days': '1'})

        t.refresh_from_db()
        self.assertEqual(t.start_due, old_s + timedelta(days=1))
        self.assertEqual(t.due, old_d + timedelta(days=1))
        self.assertTrue(
            TaskLog.objects.filter(task=t, kind=TaskLog.Kind.DUE).exists(),
        )

    def test_shift_forbidden_for_other(self):
        t = self._task_in_month()
        old_s = t.start_due
        self.client.force_login(self.emp2)
        self.client.post(reverse('task_shift', args=[t.pk]), {'days': '1'})

        t.refresh_from_db()
        self.assertEqual(t.start_due, old_s)


# ── №4: попап о новой задаче ────────────────────────────────────

class TestPopup(BusinessBase):
    """Поллинг отдаёт верхнее уведомление — основа всплывающего окна."""

    def test_poll_top_after_task_created(self):
        self.client.force_login(self.emp)
        r = self.client.get(reverse('comms_poll'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'].split(';')[0], 'application/json')
        self.assertIsNone(r.json()['top'])

        self.create_task(title='Задача для попапа')

        self.client.force_login(self.emp)
        top = self.client.get(reverse('comms_poll')).json()['top']
        self.assertIsNotNone(top)
        self.assertIn('Задача для попапа', top['text'])
        self.assertTrue(top['url'])


# ── №5: оповещение о старте ─────────────────────────────────────

class TestStartNotify(BusinessBase):
    """Руководитель получает оповещение о взятии задачи в работу."""

    def test_manager_notified_on_start(self):
        self.create_task(title='Старт без дедлайна')
        t = Task.objects.get(title='Старт без дедлайна')
        self.assertEqual(t.status, Task.Status.NEW)

        self.client.force_login(self.emp)
        self.client.post(reverse('task_action', args=[t.pk, 'start']))

        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)
        self.assertTrue(Notification.objects.filter(
            recipient=self.boss,
            text__contains='взял(а) в работу',
        ).exists())


# ── №6: цеховой таймер ──────────────────────────────────────────

class TestShop(BusinessBase):
    """Цеховой таймер поверх основного + отчёт + оповещение."""

    def test_shop_session_notify_and_report(self):
        due = (timezone.localdate() + timedelta(days=2)).isoformat()
        self.create_task(title='Цеховая задача', due=due)

        t = Task.objects.get(title='Цеховая задача')
        main_started = t.session_started_at

        self.client.force_login(self.emp)
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))

        s = ShopSession.objects.get(task=t, finished_at__isnull=True)
        t.refresh_from_db()
        # основной таймер не остановлен
        self.assertEqual(t.session_started_at, main_started)

        self.assertTrue(Notification.objects.filter(
            recipient=self.boss,
            text__contains='вышел(ла) в цех',
        ).exists())

        self.client.post(reverse('task_action', args=[t.pk, 'shop_stop']))
        s.refresh_from_db()
        self.assertIsNotNone(s.finished_at)
        self.assertGreater(s.duration_hours, 0)

        resp = self.client.get(reverse('task_detail', args=[t.pk]))
        self.assertGreater(resp.context['shop_total'], 0)

    def test_shop_double_start_rejected(self):
        due = (timezone.localdate() + timedelta(days=2)).isoformat()
        self.create_task(title='Цех-дубль', due=due)
        t = Task.objects.get(title='Цех-дубль')

        self.client.force_login(self.emp)
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))

        self.assertEqual(
            ShopSession.objects.filter(finished_at__isnull=True).count(),
            1,
        )

    def test_shop_start_forbidden_for_other(self):
        """Сотрудник не может уйти в цех по чужой задаче."""
        due = (timezone.localdate() + timedelta(days=2)).isoformat()
        self.create_task(title='Чужая', due=due)
        t = Task.objects.get(title='Чужая')

        self.client.force_login(self.emp2)
        self.client.post(reverse('task_action', args=[t.pk, 'shop_start']))

        self.assertFalse(
            ShopSession.objects.filter(task=t, finished_at__isnull=True).exists(),
        )

    def test_reports_csv_has_shop_column(self):
        self.client.force_login(self.boss)
        resp = self.client.get(reverse('manager_reports_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Цех, ч', resp.content.decode('utf-8-sig'))


# ── №7: свод по заводу ──────────────────────────────────────────

class TestPlant(BusinessBase):
    """Статистика завода доступна руководителю с админ-правами."""

    def test_plant_page_for_admin(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('manager_plant'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.dep.name)
        self.assertContains(resp, 'Цех, ч')

    def test_plant_forbidden_for_dept_boss(self):
        self.client.force_login(self.boss)
        self.assertEqual(
            self.client.get(reverse('manager_plant')).status_code, 403,
        )

    def test_plant_csv(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('manager_plant_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Подразделение', resp.content.decode('utf-8-sig'))
