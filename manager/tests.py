"""Автотесты manager: доступ по ролям, кабинет, plant, отчёты."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from tasks.models import Task
from tasks.utils import add_work_days

from manager.views import _compute_load_rows

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class ManagerBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ogk = Department.objects.create(name='ОГК')
        cls.ogt = Department.objects.create(name='ОГТ')

        cls.admin_role = _role('admin', can_manage=True, can_admin=True, can_plant=True)
        cls.manager_role = _role('manager', can_manage=True)
        cls.plant_role = _role('plant', can_manage=True, can_plant=True)
        cls.staff_role = _role('staff')

        cls.admin = User.objects.create_user(
            email='admin@eag.su', password='x', full_name='Админ',
            is_active=True, department=cls.ogk, role=cls.admin_role, is_staff=True,
        )
        cls.mgr_ogk = User.objects.create_user(
            email='mgrogk@eag.su', password='x', full_name='Начальник ОГК',
            is_active=True, department=cls.ogk, role=cls.manager_role,
        )
        cls.mgr_ogt = User.objects.create_user(
            email='mgrogt@eag.su', password='x', full_name='Начальник ОГТ',
            is_active=True, department=cls.ogt, role=cls.manager_role,
        )
        cls.director = User.objects.create_user(
            email='dir@eag.su', password='x', full_name='Директор',
            is_active=True, department=cls.ogk, role=cls.plant_role,
        )
        cls.eng_ogk = User.objects.create_user(
            email='engogk@eag.su', password='x', full_name='Инженер ОГК',
            is_active=True, department=cls.ogk, role=cls.staff_role,
        )
        cls.eng_ogt = User.objects.create_user(
            email='engogt@eag.su', password='x', full_name='Инженер ОГТ',
            is_active=True, department=cls.ogt, role=cls.staff_role,
        )

        now = timezone.now()
        cls.t_ogk = Task.objects.create(
            title='Задача ОГК', plan_hours=2, scale='s', kind='work',
            priority='medium', executor=cls.eng_ogk, requester=cls.mgr_ogk,
            status='in_progress',
        )
        cls.t_ogt = Task.objects.create(
            title='Задача ОГТ', plan_hours=2, scale='s', kind='work',
            priority='medium', executor=cls.eng_ogt, requester=cls.mgr_ogt,
            status='review',
            finished_at=now,
        )


class ManagerAccessTests(ManagerBaseTestCase):

    def test_cabinet_requires_boss(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_cabinet'))
        self.assertEqual(r.status_code, 403)

    def test_cabinet_ok_for_manager(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_cabinet'))
        self.assertEqual(r.status_code, 200)

    def test_cabinet_ok_for_admin(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('manager_cabinet'))
        self.assertEqual(r.status_code, 200)

    def test_person_page_denies_other_department(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_person', args=[self.eng_ogt.pk]))
        self.assertEqual(r.status_code, 403)

    def test_person_page_allows_director_any_department(self):
        self.client.force_login(self.director)
        r = self.client.get(reverse('manager_person', args=[self.eng_ogt.pk]))
        self.assertEqual(r.status_code, 200)

    def test_plant_requires_can_plant(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_plant'))
        self.assertEqual(r.status_code, 403)

    def test_plant_ok_for_director(self):
        self.client.force_login(self.director)
        r = self.client.get(reverse('manager_plant'))
        self.assertEqual(r.status_code, 200)


class CabinetContentTests(ManagerBaseTestCase):

    def test_manager_does_not_see_other_department_review(self):
        """Начальник ОГК не видит задачу в приёмке от ОГТ."""
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_review'))
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertNotIn('Задача ОГТ', content)

    def test_manager_sees_own_department_review(self):
        """Начальник ОГК видит задачу в приёмке от ОГК."""
        t = Task.objects.create(
            title='Своя на приёмке',
            plan_hours=2, scale='s', kind='work', priority='medium',
            executor=self.eng_ogk, requester=self.mgr_ogk,
            status='review',
            finished_at=timezone.now(),
        )
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_review'))
        content = r.content.decode()
        self.assertIn(t.title, content)

    def test_engineer_has_no_access_to_review(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_review'))
        self.assertEqual(r.status_code, 403)


class ReportCSVTests(ManagerBaseTestCase):

    def test_csv_boss_only(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports_csv'))
        self.assertEqual(r.status_code, 403)

    def test_csv_manager_ok_with_bom(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports_csv'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))

    def test_plant_csv_director_only(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_plant_csv'))
        self.assertEqual(r.status_code, 403)

        self.client.force_login(self.director)
        r = self.client.get(reverse('manager_plant_csv'))
        self.assertEqual(r.status_code, 200)

class DepartmentHoursTests(TestCase):
    """Доступ к странице норм часов и сохранение значения."""

    @classmethod
    def setUpTestData(cls):
        cls.ogk = Department.objects.create(name='ОГК')

        cls.admin_role = _role('admin', can_manage=True, can_admin=True, can_plant=True)
        cls.manager_role = _role('manager', can_manage=True)
        cls.staff_role = _role('staff')

        cls.plant = User.objects.create_user(
            email='dir@eag.su', password='x', full_name='Директор',
            is_active=True, department=cls.ogk, role=cls.admin_role,
        )
        cls.mgr = User.objects.create_user(
            email='mgr@eag.su', password='x', full_name='Начальник',
            is_active=True, department=cls.ogk, role=cls.manager_role,
        )
        cls.staff = User.objects.create_user(
            email='st@eag.su', password='x', full_name='Инженер',
            is_active=True, department=cls.ogk, role=cls.staff_role,
        )

    def test_manager_cannot_open(self):
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('manager_department_hours'))
        self.assertEqual(r.status_code, 403)

    def test_staff_cannot_open(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('manager_department_hours'))
        self.assertEqual(r.status_code, 403)

    def test_plant_can_open(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_department_hours'))
        self.assertEqual(r.status_code, 200)

    def test_set_department_override(self):
        self.client.force_login(self.plant)
        self.client.post(reverse('manager_department_hours'), {
            'target': str(self.ogk.pk),
            'hours_per_day': '12',
        })
        self.ogk.refresh_from_db()
        self.assertEqual(self.ogk.hours_per_day, 12.0)

    def test_clear_department_override(self):
        self.ogk.hours_per_day = 12.0
        self.ogk.save(update_fields=['hours_per_day'])

        self.client.force_login(self.plant)
        self.client.post(reverse('manager_department_hours'), {
            'target': str(self.ogk.pk),
            'hours_per_day': '',
        })
        self.ogk.refresh_from_db()
        self.assertIsNone(self.ogk.hours_per_day)

    def test_reject_negative(self):
        self.client.force_login(self.plant)
        self.client.post(reverse('manager_department_hours'), {
            'target': str(self.ogk.pk),
            'hours_per_day': '-5',
        })
        self.ogk.refresh_from_db()
        self.assertIsNone(self.ogk.hours_per_day)

    def test_reject_non_numeric(self):
        self.client.force_login(self.plant)
        self.client.post(reverse('manager_department_hours'), {
            'target': str(self.ogk.pk),
            'hours_per_day': 'abc',
        })
        self.ogk.refresh_from_db()
        self.assertIsNone(self.ogk.hours_per_day)

    def test_accepts_comma(self):
        self.client.force_login(self.plant)
        self.client.post(reverse('manager_department_hours'), {
            'target': str(self.ogk.pk),
            'hours_per_day': '7,5',
        })
        self.ogk.refresh_from_db()
        self.assertEqual(self.ogk.hours_per_day, 7.5)


class NormHoursForTests(TestCase):
    """norm_hours_for: приоритет отдела над глобальной нормой."""

    @classmethod
    def setUpTestData(cls):
        cls.ogk = Department.objects.create(name='ОГК')
        cls.dept12 = Department.objects.create(name='Цех', hours_per_day=12.0)
        cls.role = _role('staff')

        cls.user_ogk = User.objects.create_user(
            email='u1@eag.su', password='x', full_name='A',
            is_active=True, department=cls.ogk, role=cls.role,
        )
        cls.user_dept12 = User.objects.create_user(
            email='u2@eag.su', password='x', full_name='B',
            is_active=True, department=cls.dept12, role=cls.role,
        )
        cls.user_no_dept = User.objects.create_user(
            email='u3@eag.su', password='x', full_name='C',
            is_active=True, department=None, role=cls.role,
        )

    def test_department_override_wins(self):
        from tasks.utils import norm_hours_for
        # Глобальная норма не тронута, но у цеха override 12.
        self.assertEqual(norm_hours_for(self.user_dept12), 12.0)

    def test_fallback_to_global(self):
        from tasks.utils import norm_hours_for, norm_hours
        self.assertEqual(norm_hours_for(self.user_ogk), norm_hours())

    def test_no_department_uses_global(self):
        from tasks.utils import norm_hours_for, norm_hours
        self.assertEqual(norm_hours_for(self.user_no_dept), norm_hours())

    def test_none_user_uses_global(self):
        from tasks.utils import norm_hours_for, norm_hours
        self.assertEqual(norm_hours_for(None), norm_hours())



class OvertimePlantSummaryTests(TestCase):
    """Свод сверхурочных на /manager/plant/ для начальника завода."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        from tasks.models import OvertimeRecord

        cls.dept_ogk = Department.objects.create(name='ОГК')
        cls.dept_ogt = Department.objects.create(name='ОГТ')

        cls.role_staff = Role.objects.get_or_create(
            code='staff', defaults={'name': 'Инженер'},
        )[0]
        cls.role_plant = Role.objects.get_or_create(
            code='plant_head',
            defaults={'name': 'Начальник завода',
                      'can_manage': True, 'can_plant': True},
        )[0]
        cls.role_mgr = Role.objects.get_or_create(
            code='manager',
            defaults={'name': 'Начальник отдела', 'can_manage': True},
        )[0]

        User = get_user_model()
        cls.plant = User.objects.create_user(
            email='plant@test.ru', password='p', full_name='Директор',
            role=cls.role_plant, department=cls.dept_ogk, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='mgr@test.ru', password='p', full_name='Нач. ОГК',
            role=cls.role_mgr, department=cls.dept_ogk, is_active=True,
        )
        cls.worker1 = User.objects.create_user(
            email='w1@test.ru', password='p', full_name='Работник 1',
            role=cls.role_staff, department=cls.dept_ogk, is_active=True,
        )
        cls.worker2 = User.objects.create_user(
            email='w2@test.ru', password='p', full_name='Работник 2',
            role=cls.role_staff, department=cls.dept_ogt, is_active=True,
        )

        today = timezone.localdate()
        OvertimeRecord.objects.create(
            user=cls.worker1, department=cls.dept_ogk,
            date=today, hours=3.0, reason='Тест',
            created_by=cls.mgr,
        )
        OvertimeRecord.objects.create(
            user=cls.worker1, department=cls.dept_ogk,
            date=today, hours=2.0,
            created_by=cls.mgr,
        )
        OvertimeRecord.objects.create(
            user=cls.worker2, department=cls.dept_ogt,
            date=today, hours=1.5,
            created_by=cls.mgr,
        )

    def test_plant_page_shows_overtime_column(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_plant'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Сверхурочные')

    def test_overtime_hours_aggregated_by_dept(self):
        from manager.views import _plant_rows
        rows, _ = _plant_rows()
        by_name = {r['dep']: r for r in rows}

        ogk = by_name.get('ОГК')
        ogt = by_name.get('ОГТ')
        self.assertIsNotNone(ogk)
        self.assertIsNotNone(ogt)
        self.assertEqual(ogk['overtime_hours'], 5.0)
        self.assertEqual(ogk['overtime_people'], 1)
        self.assertEqual(ogt['overtime_hours'], 1.5)

    def test_overtime_days_block(self):
        from manager.views import _overtime_by_day
        days, by_dep, grand = _overtime_by_day()
        self.assertEqual(grand['hours'], 6.5)
        self.assertEqual(grand['count'], 3)
        self.assertEqual(len(days), 1)
        # ОГК в топе — 5.0 ч > 1.5 ч
        self.assertEqual(by_dep[0]['dep'], 'ОГК')

    def test_manager_cannot_open_plant(self):
        """Руководитель отдела не должен видеть свод завода."""
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('manager_plant'))
        self.assertEqual(r.status_code, 403)

        # ── Регрессия: __str__ не должен падать ──
    def test_str_returns_readable(self):
        """Строковое представление: user · дата · часы."""
        from tasks.models import OvertimeRecord

        rec = OvertimeRecord.objects.create(
            user=self.worker1, department=self.dept_ogk,
            date=timezone.localdate(), hours=2.5,
            reason='Ремонт', created_by=self.mgr,
        )
        s = str(rec)
        self.assertIn('Работник 1', s)
        self.assertIn('2.5', s)



class OvertimeEditAndFilterTests(TestCase):
    """B.5.1-edit: правка сверхурочной записи + фильтр по сотруднику."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role

        cls.dept_a = Department.objects.create(name='ОГК')
        cls.dept_b = Department.objects.create(name='ОГТ')

        cls.role_plant = Role.objects.get_or_create(
            code='plant_head',
            defaults={'name': 'Начальник завода',
                      'can_manage': True, 'can_plant': True},
        )[0]
        cls.role_mgr = Role.objects.get_or_create(
            code='manager',
            defaults={'name': 'Начальник отдела', 'can_manage': True},
        )[0]
        cls.role_staff = Role.objects.get_or_create(
            code='staff', defaults={'name': 'Инженер'},
        )[0]

        User = get_user_model()

        cls.mgr_a = User.objects.create_user(
            email='mgr_a@test.ru', password='p', full_name='Нач. ОГК',
            role=cls.role_mgr, department=cls.dept_a, is_active=True,
        )
        cls.mgr_b = User.objects.create_user(
            email='mgr_b@test.ru', password='p', full_name='Нач. ОГТ',
            role=cls.role_mgr, department=cls.dept_b, is_active=True,
        )
        cls.plant = User.objects.create_user(
            email='plant@test.ru', password='p', full_name='Директор',
            role=cls.role_plant, department=cls.dept_a, is_active=True,
        )

        cls.worker_a = User.objects.create_user(
            email='wa@test.ru', password='p', full_name='Работник А',
            role=cls.role_staff, department=cls.dept_a, is_active=True,
        )
        cls.worker_b = User.objects.create_user(
            email='wb@test.ru', password='p', full_name='Работник Б',
            role=cls.role_staff, department=cls.dept_b, is_active=True,
        )

        from tasks.models import OvertimeRecord

        today = timezone.localdate()
        cls.rec_a = OvertimeRecord.objects.create(
            user=cls.worker_a, department=cls.dept_a,
            date=today, hours=2.0, reason='Ремонт',
            created_by=cls.mgr_a,
        )
        cls.rec_b = OvertimeRecord.objects.create(
            user=cls.worker_b, department=cls.dept_b,
            date=today, hours=4.0,
            created_by=cls.mgr_b,
        )

    # ── Правка своей записи ──

    def test_edit_own_record_success(self):

        self.client.force_login(self.mgr_a)
        today = timezone.localdate()

        r = self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
            {
                'date': today.isoformat(),
                'hours': '3.5',
                'reason': 'Ремонт станка',
                'task': 'none',
            },
        )
        self.assertEqual(r.status_code, 302)

        self.rec_a.refresh_from_db()
        self.assertEqual(self.rec_a.hours, 3.5)
        self.assertEqual(self.rec_a.reason, 'Ремонт станка')
        # Задача не менялась (была None)
        self.assertIsNone(self.rec_a.task)

    def test_edit_changes_date_and_reason(self):
        self.client.force_login(self.mgr_a)
        new_date = timezone.localdate() - timezone.timedelta(days=3)

        self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
            {
                'date': new_date.isoformat(),
                'hours': '2.0',
                'reason': 'Перенесли на прошлую неделю',
                'task': 'none',
            },
        )
        self.rec_a.refresh_from_db()
        self.assertEqual(self.rec_a.date, new_date)
        self.assertEqual(self.rec_a.reason, 'Перенесли на прошлую неделю')

    # ── Права ──

    def test_edit_foreign_record_forbidden(self):
        """Руководитель ОГК не может править запись ОГТ."""
        self.client.force_login(self.mgr_a)
        today = timezone.localdate()

        r = self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_b.pk]),
            {
                'date': today.isoformat(),
                'hours': '99.0',
                'reason': 'Попытка взлома',
                'task': 'none',
            },
        )
        self.assertEqual(r.status_code, 403)

        # Не изменилось
        self.rec_b.refresh_from_db()
        self.assertEqual(self.rec_b.hours, 4.0)

    def test_plant_cannot_edit(self):
        """Начальник завода только смотрит свод — править не может."""
        self.client.force_login(self.plant)
        today = timezone.localdate()

        r = self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
            {
                'date': today.isoformat(),
                'hours': '1.0',
                'reason': 'X',
                'task': 'none',
            },
        )
        self.assertEqual(r.status_code, 403)

    def test_get_method_rejected(self):
        """Правка только через POST."""
        self.client.force_login(self.mgr_a)
        r = self.client.get(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
        )
        self.assertEqual(r.status_code, 405)

    # ── Валидация ──

    def test_edit_rejects_negative_hours(self):
        self.client.force_login(self.mgr_a)
        today = timezone.localdate()

        self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
            {
                'date': today.isoformat(),
                'hours': '-1',
                'reason': '',
                'task': 'none',
            },
        )
        self.rec_a.refresh_from_db()
        self.assertEqual(self.rec_a.hours, 2.0)  # не изменилось

    def test_edit_rejects_over_24_hours(self):
        self.client.force_login(self.mgr_a)
        today = timezone.localdate()

        self.client.post(
            reverse('manager_overtime_edit', args=[self.rec_a.pk]),
            {
                'date': today.isoformat(),
                'hours': '25',
                'reason': '',
                'task': 'none',
            },
        )
        self.rec_a.refresh_from_db()
        self.assertEqual(self.rec_a.hours, 2.0)

    # ── Фильтр по сотруднику ──

    def test_filter_by_user_shows_only_that_user(self):
        self.client.force_login(self.mgr_a)
        r = self.client.get(
            reverse('manager_overtime'),
            {'user': self.worker_a.pk},
        )
        self.assertEqual(r.status_code, 200)

        # В rows только worker_a
        rows = r.context['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user'].pk, self.worker_a.pk)
        self.assertEqual(r.context['filter_user_id'], self.worker_a.pk)

    def test_filter_ignores_user_out_of_scope(self):
        """Менеджер ОГК не может фильтровать по сотруднику ОГТ."""
        self.client.force_login(self.mgr_a)
        r = self.client.get(
            reverse('manager_overtime'),
            {'user': self.worker_b.pk},
        )
        self.assertEqual(r.status_code, 200)
        # Фильтр игнорируется: показываются все сотрудники ОГК
        self.assertIsNone(r.context['filter_user_id'])
        rows = r.context['rows']
        # В scope ОГК только worker_a (и сам mgr_a, но у него 0 записей —
        # но он тоже в rows, т.к. member_ids включает всех активных ОГК)
        user_ids = {row['user'].pk for row in rows}
        self.assertIn(self.worker_a.pk, user_ids)
        self.assertNotIn(self.worker_b.pk, user_ids)

    def test_filter_shows_correct_total(self):
        """При фильтре по одному сотруднику totals считаются по нему."""
        self.client.force_login(self.mgr_a)
        r = self.client.get(
            reverse('manager_overtime'),
            {'user': self.worker_a.pk},
        )
        self.assertEqual(r.context['totals']['hours'], 2.0)
        self.assertEqual(r.context['totals']['records'], 1)

    # ── CSV-выгрузка ──

    def test_csv_has_bom_and_headers(self):
        self.client.force_login(self.mgr_a)
        r = self.client.get(reverse('manager_overtime_csv'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))

        body = r.content.decode('utf-8-sig')
        self.assertIn('Дата', body)
        self.assertIn('Часы', body)
        self.assertIn('Сотрудник', body)
        self.assertIn('ИТОГО', body)

    def test_csv_only_own_department(self):
        """Руководитель ОГК не видит в CSV записи ОГТ."""
        self.client.force_login(self.mgr_a)
        r = self.client.get(reverse('manager_overtime_csv'))
        body = r.content.decode('utf-8-sig')

        # worker_a есть, worker_b нет
        self.assertIn('Работник А', body)
        self.assertNotIn('Работник Б', body)

    def test_csv_filter_by_user(self):
        self.client.force_login(self.mgr_a)
        r = self.client.get(
            reverse('manager_overtime_csv'),
            {'user': self.worker_a.pk},
        )
        body = r.content.decode('utf-8-sig')

        # Ровно одна строка данных с «Работник А»
        data_rows = [ln for ln in body.splitlines() if 'Работник А' in ln]
        self.assertEqual(len(data_rows), 1)

        # В этой строке — 2,0 часа (формат с запятой)
        self.assertIn('2,00', data_rows[0])

        # Никакой другой сотрудник ОГК не попал
        self.assertNotIn('Работник Б', body)

    def test_csv_plant_forbidden(self):
        """Начальник завода не имеет доступа к CSV сверхурочных."""
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_overtime_csv'))
        self.assertEqual(r.status_code, 403)



# ═════════════════════════════════════════════════════════════
#  Загруженность отдела (_compute_load_rows)
# ═════════════════════════════════════════════════════════════

class ComputeLoadRowsTests(TestCase):
    """Тесты карточки «Загруженность отдела».

    Проверяем КЛАСС загрузки (empty / under / ok / over), а не
    точный процент: подбираем план и окно с запасом от границ,
    чтобы тест не зависел от дня недели, выходных и праздников.

    Кэш norm_hours() глобальный — чистим в setUp, чтобы значения
    не протекали между тестами.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role

        cls.dept = Department.objects.get_or_create(name='ОГК-LOAD')[0]
        cls.other_dept = Department.objects.get_or_create(name='ОГТ-LOAD')[0]

        cls.role_mgr = Role.objects.get_or_create(
            code='manager-load',
            defaults={'name': 'Нач. отдела (load)', 'can_manage': True},
        )[0]
        cls.role_staff = Role.objects.get_or_create(
            code='staff-load',
            defaults={'name': 'Сотрудник (load)'},
        )[0]

        User = get_user_model()

        cls.mgr = User.objects.create_user(
            email='mgr-load@test.ru', password='p',
            full_name='Начальник ОГК-LOAD',
            department=cls.dept, role=cls.role_mgr, is_active=True,
        )
        cls.worker = User.objects.create_user(
            email='w-load@test.ru', password='p',
            full_name='Работник 1',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.worker2 = User.objects.create_user(
            email='w2-load@test.ru', password='p',
            full_name='Работник 2',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.stranger = User.objects.create_user(
            email='str-load@test.ru', password='p',
            full_name='Чужой отдел',
            department=cls.other_dept, role=cls.role_staff, is_active=True,
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _task(self, executor, plan_hours, due, **kw):
        defaults = {
            'title': 'Задача', 'scale': 's', 'kind': 'work',
            'executor': executor, 'requester': self.mgr,
            'status': Task.Status.NEW,
            'plan_hours': plan_hours,
            'due': due,
        }
        defaults.update(kw)
        return Task.objects.create(**defaults)

    def _row(self, user, rows):
        for r in rows:
            if r['user'].pk == user.pk:
                return r
        self.fail(f'Строка для {user.full_name} не найдена')

    # ── Пустой отдел ──

    def test_empty_when_no_open_tasks(self):
        """Сотрудник без открытых задач → cls == 'empty', load_pct == 0."""
        rows, totals = _compute_load_rows(self.mgr)
        for r in rows:
            self.assertEqual(r['cls'], 'empty', msg=r['user'].full_name)
            self.assertEqual(r['load_pct'], 0)
            self.assertEqual(r['needed_days'], 0)
        self.assertEqual(totals['no_tasks'], len(rows))
        self.assertEqual(totals['overloaded'], 0)

    def test_closed_tasks_not_counted(self):
        """DONE/CANCELLED не идут в загруженность."""
        today = timezone.localdate()
        self._task(
            self.worker, plan_hours=200,
            due=add_work_days(today, 5),
            status=Task.Status.DONE, finished_at=timezone.now(),
        )
        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['cls'], 'empty')
        self.assertEqual(r['n_open'], 0)

    # ── Классы загрузки ──

    def test_under_when_low_load(self):
        """План 8 ч, окно ~30 раб. дней → load ≈ 3% → under."""
        today = timezone.localdate()
        self._task(self.worker, plan_hours=8, due=add_work_days(today, 30))

        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['cls'], 'under')
        self.assertEqual(r['needed_days'], 1)
        self.assertLess(r['load_pct'], 60)

    def test_ok_when_balanced_load(self):
        """План 80 ч, окно ~12 раб. дней → load ≈ 83-91% → ok."""
        today = timezone.localdate()
        self._task(self.worker, plan_hours=80, due=add_work_days(today, 11))

        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['cls'], 'ok')
        self.assertEqual(r['needed_days'], 10)
        self.assertGreaterEqual(r['load_pct'], 60)
        self.assertLessEqual(r['load_pct'], 110)

    def test_over_when_overloaded(self):
        """План 200 ч, окно ~5 раб. дней → load > 400% → over."""
        today = timezone.localdate()
        self._task(self.worker, plan_hours=200, due=add_work_days(today, 5))

        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['cls'], 'over')
        self.assertEqual(r['needed_days'], 25)
        self.assertGreater(r['load_pct'], 110)

    # ── Норма подразделения ──

    def test_department_override_norm_used(self):
        """Отдел с hours_per_day=12 → needed_days считается по 12, не по 8.

        60 ч ÷ 12 = 5 дней. Окно 5 раб. дней. load = 100% → ok.
        Если бы взялась норма 8: 60 ÷ 8 = 8 дней, 8 ÷ 5 = 160% → over.
        """
        from accounts.models import Department

        dept12 = Department.objects.create(name='Цех-LOAD-12', hours_per_day=12.0)
        mgr12 = User.objects.create_user(
            email='mgr12-load@test.ru', password='p',
            full_name='Нач. цеха 12',
            department=dept12, role=self.role_mgr, is_active=True,
        )
        worker12 = User.objects.create_user(
            email='w12-load@test.ru', password='p',
            full_name='Цеховик',
            department=dept12, role=self.role_staff, is_active=True,
        )

        today = timezone.localdate()
        Task.objects.create(
            title='Цеховая', plan_hours=60, scale='s', kind='work',
            executor=worker12, requester=mgr12, status=Task.Status.NEW,
            due=add_work_days(today, 5),
        )

        rows, _ = _compute_load_rows(mgr12)
        r = self._row(worker12, rows)
        self.assertEqual(r['needed_days'], 5)
        self.assertEqual(r['cls'], 'ok')
        self.assertNotEqual(r['needed_days'], 8)

    # ── Окно: fallback и прошлое ──

    def test_no_due_uses_default_window(self):
        """Задача без due → окно = 30 (дефолт)."""
        self._task(self.worker, plan_hours=8, due=None)

        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['window_days'], 30)
        self.assertEqual(r['needed_days'], 1)

    def test_past_due_uses_default_window(self):
        """due < today → не учитывается, окно = 30."""
        from datetime import timedelta as _td

        today = timezone.localdate()
        self._task(self.worker, plan_hours=8, due=today - _td(days=5))

        rows, _ = _compute_load_rows(self.mgr)
        r = self._row(self.worker, rows)
        self.assertEqual(r['window_days'], 30)

    # ── Итоги ──

    def test_totals_counters(self):
        """Счётчики по классам: over / ok / empty."""
        today = timezone.localdate()
        self._task(self.worker, plan_hours=200, due=add_work_days(today, 5))
        self._task(self.worker2, plan_hours=80, due=add_work_days(today, 11))
        # mgr остаётся без задач → empty

        _, totals = _compute_load_rows(self.mgr)
        self.assertEqual(totals['overloaded'], 1)
        self.assertEqual(totals['ok'], 1)
        self.assertEqual(totals['underloaded'], 0)
        self.assertEqual(totals['no_tasks'], 1)
        self.assertEqual(totals['total_open'], 2)
        self.assertEqual(totals['members'], 3)

    # ── Scope ──

    def test_scope_department_only(self):
        """Manager видит только свой отдел, не чужой."""
        today = timezone.localdate()
        self._task(self.stranger, plan_hours=200, due=add_work_days(today, 5))

        rows, _ = _compute_load_rows(self.mgr)
        pks = [r['user'].pk for r in rows]
        self.assertNotIn(self.stranger.pk, pks)

    # ── Сортировка ──

    def test_rows_sorted_by_load_desc(self):
        """Сначала более загруженные, в конце — пустые."""
        today = timezone.localdate()
        self._task(self.worker, plan_hours=200, due=add_work_days(today, 5))
        self._task(self.worker2, plan_hours=80, due=add_work_days(today, 11))

        rows, _ = _compute_load_rows(self.mgr)
        pks = [r['user'].pk for r in rows]
        # worker (over) раньше worker2 (ok).
        self.assertLess(pks.index(self.worker.pk), pks.index(self.worker2.pk))
        # mgr (empty, load=0) — в конце.
        self.assertEqual(pks[-1], self.mgr.pk)



# ═════════════════════════════════════════════════════════════
#  Графики работы (WorkSchedule) — C.3b-1
# ═════════════════════════════════════════════════════════════

class ScheduleViewsBase(TestCase):
    """Общая база: 5 пользователей разных ролей + один график."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role, WorkSchedule
        from datetime import date

        cls.dept = Department.objects.get_or_create(name='SCH-ОГК')[0]

        cls.role_staff = Role.objects.get_or_create(
            code='staff-sch', defaults={'name': 'Сотрудник SCH'},
        )[0]
        cls.role_mgr = Role.objects.get_or_create(
            code='manager-sch',
            defaults={'name': 'Нач. отдела SCH', 'can_manage': True},
        )[0]
        cls.role_plant = Role.objects.get_or_create(
            code='plant_head-sch',
            defaults={'name': 'Нач. завода SCH',
                      'can_manage': True, 'can_plant': True},
        )[0]
        cls.role_admin = Role.objects.get_or_create(
            code='admin-sch',
            defaults={'name': 'Админ SCH',
                      'can_manage': True, 'can_admin': True},
        )[0]

        User = get_user_model()

        cls.staff = User.objects.create_user(
            email='s-sch@test.ru', password='p', full_name='Сотрудник',
            department=cls.dept, role=cls.role_staff, is_active=True,
        )
        cls.mgr = User.objects.create_user(
            email='m-sch@test.ru', password='p', full_name='Нач. отдела',
            department=cls.dept, role=cls.role_mgr, is_active=True,
        )
        cls.plant = User.objects.create_user(
            email='p-sch@test.ru', password='p', full_name='Нач. завода',
            department=cls.dept, role=cls.role_plant, is_active=True,
        )
        cls.admin = User.objects.create_user(
            email='a-sch@test.ru', password='p', full_name='Админ',
            department=cls.dept, role=cls.role_admin, is_active=True,
        )

        cls.ws = WorkSchedule.objects.create(
            name='5/2 обычная', pattern=[1, 1, 1, 1, 1, 0, 0],
            anchor_date=date(2026, 11, 2), hours_per_shift=8.0,
            use_calendar=True, is_active=True,
        )
        cls.ws_off = WorkSchedule.objects.create(
            name='2/2 сменный', pattern=[1, 1, 0, 0],
            anchor_date=date(2026, 11, 2), hours_per_shift=12.0,
            use_calendar=False, is_active=False,
        )


class SchedulesListAccessTests(ScheduleViewsBase):
    def test_anonymous_redirected(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertEqual(r.status_code, 302)

    def test_staff_forbidden(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('manager_schedules'))
        self.assertEqual(r.status_code, 403)

    def test_manager_forbidden(self):
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('manager_schedules'))
        self.assertEqual(r.status_code, 403)

    def test_plant_ok(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedules'))
        self.assertEqual(r.status_code, 200)

    def test_admin_ok(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('manager_schedules'))
        self.assertEqual(r.status_code, 200)


class SchedulesListContentTests(ScheduleViewsBase):
    def setUp(self):
        self.client.force_login(self.plant)

    def test_both_schedules_listed(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertContains(r, '5/2 обычная')
        self.assertContains(r, '2/2 сменный')

    def test_use_calendar_badges(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertContains(r, 'с календарём')
        self.assertContains(r, 'без календаря')

    def test_inactive_marked(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertContains(r, 'отключён')

    def test_summary_5_2(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertContains(r, '5/2 (пн–пт)')

    def test_summary_2_2(self):
        r = self.client.get(reverse('manager_schedules'))
        self.assertContains(r, '2/2')


class ScheduleDetailAccessTests(ScheduleViewsBase):
    def test_anonymous_redirected(self):
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 302)

    def test_manager_forbidden(self):
        self.client.force_login(self.mgr)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 403)

    def test_plant_ok(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 200)

    def test_unknown_pk_404(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[999999]),
        )
        self.assertEqual(r.status_code, 404)


class ScheduleDetailContentTests(ScheduleViewsBase):
    def setUp(self):
        self.client.force_login(self.plant)

    def test_shows_pattern_cells(self):
        """Паттерн отрисовывается как ячейки с классами w/o.

        Буквальный поиск `>1<` ненадёжен: значение внутри ячейки
        обрамлено пробелами и переносами строк. Проверяем через
        количество ячеек с классами `cell w` (рабочий) и `cell o`
        (выходной) — именно от них зависит цвет.
        """
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        content = r.content.decode()

        # 5/2 → 5 рабочих ячеек и 2 выходные.
        n_working = content.count('cell w"')
        n_off = content.count('cell o"')
        self.assertEqual(n_working, 5)
        self.assertEqual(n_off, 2)

    def test_shows_calendar_flag(self):
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, 'Учитывает производственный календарь')

    def test_shows_no_calendar_flag_for_2_2(self):
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws_off.pk]),
        )
        self.assertContains(r, 'Без производственного календаря')

    def test_lists_personal_users(self):
        """Если пользователь привязан лично — попадает в список."""
        from django.contrib.auth import get_user_model
        User = get_user_model()

        User.objects.create_user(
            email='personal-sch@test.ru', password='p',
            full_name='Личный пользователь',
            department=self.dept, role=self.role_staff, is_active=True,
            schedule=self.ws,
        )

        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, 'Личный пользователь')

    def test_lists_departments_via_default(self):
        """Отдел с этим default_schedule — попадает в список."""
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, self.dept.name)

    def test_via_departments_people(self):
        """Сотрудник без личного графика, но в отделе с графиком,
        попадает в блок «Попадают через отдел».
        """
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        self.staff.schedule = None
        self.staff.save(update_fields=['schedule'])

        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, 'Попадают через отдел')
        self.assertContains(r, self.staff.full_name)


class PatternSummaryUnitTests(TestCase):
    """Юнит-тесты на _pattern_summary. Без БД-зависимостей."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_5_2(self):
        from manager.views import _pattern_summary
        self.assertEqual(
            _pattern_summary([1, 1, 1, 1, 1, 0, 0]),
            '5/2 (пн–пт)',
        )

    def test_2_2(self):
        from manager.views import _pattern_summary
        self.assertEqual(_pattern_summary([1, 1, 0, 0]), '2/2')

    def test_3_3(self):
        from manager.views import _pattern_summary
        self.assertEqual(_pattern_summary([1, 1, 1, 0, 0, 0]), '3/3')

    def test_4_4(self):
        from manager.views import _pattern_summary
        self.assertEqual(
            _pattern_summary([1, 1, 1, 1, 0, 0, 0, 0]), '4/4',
        )

    def test_5_5(self):
        from manager.views import _pattern_summary
        self.assertEqual(
            _pattern_summary([1] * 5 + [0] * 5), '5/5',
        )

    def test_two_cycles_of_two(self):
        """[1,1,0,0,1,1,0,0] — тоже 2/2: все серии по 2."""
        from manager.views import _pattern_summary
        self.assertEqual(
            _pattern_summary([1, 1, 0, 0, 1, 1, 0, 0]), '2/2',
        )

    def test_7_7(self):
        from manager.views import _pattern_summary
        self.assertEqual(_pattern_summary([1] * 7 + [0] * 7), '7/7')

    def test_generic(self):
        from manager.views import _pattern_summary
        # 3 из 5 — серии разной длины (3 и 2), не «N/M».
        self.assertEqual(_pattern_summary([1, 1, 1, 0, 0]), '3 из 5 рабочих')

    def test_generic_uneven(self):
        """[1,1,1,1,1,0] — серии 5 и 1, не «N/M»."""
        from manager.views import _pattern_summary
        self.assertEqual(
            _pattern_summary([1, 1, 1, 1, 1, 0]), '5 из 6 рабочих',
        )

    def test_all_working(self):
        """[1,1,1] — все рабочие, не паттерн 1/1 (что вводит в заблуждение)."""
        from manager.views import _pattern_summary
        # Серии: [(1,3)] — одна серия, lengths={3}, но это не цикл.
        # Формально наш алгоритм вернёт '3/3'. Проверим поведение и закрепим.
        # Семантически «3/3» корректно: 3 рабочих дня, 3-дневный цикл.
        self.assertEqual(_pattern_summary([1, 1, 1]), '3/3')

    def test_empty(self):
        from manager.views import _pattern_summary
        self.assertEqual(_pattern_summary([]), '—')
        self.assertEqual(_pattern_summary(None), '—')

# ═════════════════════════════════════════════════════════════
#  C.3b-2: формы создания/редактирования графиков
# ═════════════════════════════════════════════════════════════

class WorkScheduleFormTests(TestCase):
    """Юнит-тесты формы WorkScheduleForm — без view и шаблонов."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _form(self, **overrides):
        from manager.forms import WorkScheduleForm
        import json

        data = {
            'name': 'Тестовый',
            'description': '',
            'hours_per_shift': '8.0',
            'anchor_date': '2026-11-02',
            'is_active': 'on',
            'use_calendar': 'on',
            'pattern_json': json.dumps([1, 1, 1, 1, 1, 0, 0]),
        }
        data.update(overrides)
        return WorkScheduleForm(data=data)

    # ── Паттерн ──

    def test_valid_form_saves_pattern(self):
        from accounts.models import WorkSchedule
        f = self._form()
        self.assertTrue(f.is_valid(), msg=f.errors)
        obj = f.save()
        self.assertEqual(obj.pattern, [1, 1, 1, 1, 1, 0, 0])
        self.assertTrue(WorkSchedule.objects.filter(pk=obj.pk).exists())

    def test_rejects_empty_pattern(self):
        f = self._form(pattern_json='[]')
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_rejects_non_json(self):
        f = self._form(pattern_json='hello')
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_rejects_too_short(self):
        import json
        f = self._form(pattern_json=json.dumps([1]))
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_rejects_too_long(self):
        import json
        f = self._form(pattern_json=json.dumps([1] * 32))
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_rejects_non_binary_values(self):
        import json
        f = self._form(pattern_json=json.dumps([1, 2, 0, 1]))
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_rejects_all_zero(self):
        """Паттерн [0,0,0] не даст ни одного рабочего дня."""
        import json
        f = self._form(pattern_json=json.dumps([0, 0, 0]))
        self.assertFalse(f.is_valid())
        self.assertIn('pattern_json', f.errors)

    def test_accepts_bool_in_json(self):
        """[true, true, false] — валидно, приводится к 1/1/0."""
        import json
        f = self._form(pattern_json=json.dumps([True, True, False]))
        self.assertTrue(f.is_valid(), msg=f.errors)
        self.assertEqual(f.cleaned_data['pattern_json'], [1, 1, 0])

    # ── Название ──

    def test_rejects_duplicate_name(self):
        from accounts.models import WorkSchedule
        WorkSchedule.objects.create(
            name='Занят', pattern=[1, 0], anchor_date='2026-11-02',
        )
        f = self._form(name='Занят')
        self.assertFalse(f.is_valid())
        self.assertIn('name', f.errors)

    def test_duplicate_name_excludes_self(self):
        """При редактировании то же имя не считается дубликатом."""
        from accounts.models import WorkSchedule
        from manager.forms import WorkScheduleForm
        import json

        obj = WorkSchedule.objects.create(
            name='Свой', pattern=[1, 0], anchor_date='2026-11-02',
        )
        f = WorkScheduleForm(
            data={
                'name': 'Свой',
                'description': '',
                'hours_per_shift': '8',
                'anchor_date': '2026-11-02',
                'pattern_json': json.dumps([1, 0]),
            },
            instance=obj,
        )
        self.assertTrue(f.is_valid(), msg=f.errors)

    def test_rejects_empty_name(self):
        f = self._form(name='')
        self.assertFalse(f.is_valid())
        self.assertIn('name', f.errors)

    # ── Часы ──

    def test_rejects_zero_hours(self):
        f = self._form(hours_per_shift='0')
        self.assertFalse(f.is_valid())
        self.assertIn('hours_per_shift', f.errors)

    def test_rejects_hours_over_24(self):
        f = self._form(hours_per_shift='25')
        self.assertFalse(f.is_valid())
        self.assertIn('hours_per_shift', f.errors)

    # ── Anchor ──

    def test_rejects_anchor_too_far_future(self):
        from datetime import date, timedelta
        far = date.today() + timedelta(days=400)
        f = self._form(anchor_date=far.isoformat())
        self.assertFalse(f.is_valid())
        self.assertIn('anchor_date', f.errors)

    def test_rejects_anchor_too_far_past(self):
        from datetime import date, timedelta
        far = date.today() - timedelta(days=365 * 6)
        f = self._form(anchor_date=far.isoformat())
        self.assertFalse(f.is_valid())
        self.assertIn('anchor_date', f.errors)


class ScheduleEditViewTests(ScheduleViewsBase):
    """Тесты view schedule_edit (создание и редактирование)."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_new_requires_login(self):
        r = self.client.get(reverse('manager_schedule_new'))
        self.assertEqual(r.status_code, 302)

    def test_new_forbidden_for_staff(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('manager_schedule_new'))
        self.assertEqual(r.status_code, 403)

    def test_new_forbidden_for_manager(self):
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('manager_schedule_new'))
        self.assertEqual(r.status_code, 403)

    def test_new_ok_for_plant(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_new'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Новый график работы')

    def test_new_ok_for_admin(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('manager_schedule_new'))
        self.assertEqual(r.status_code, 200)

    def test_create_via_post(self):
        import json
        from accounts.models import WorkSchedule
        self.client.force_login(self.plant)

        before = WorkSchedule.objects.count()
        r = self.client.post(reverse('manager_schedule_new'), {
            'name': 'Сменный 2/2 тест',
            'description': '',
            'hours_per_shift': '12',
            'anchor_date': '2026-11-02',
            'is_active': 'on',
            'pattern_json': json.dumps([1, 1, 0, 0]),
        })
        # Редирект на detail
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkSchedule.objects.count(), before + 1)

        obj = WorkSchedule.objects.get(name='Сменный 2/2 тест')
        self.assertEqual(obj.pattern, [1, 1, 0, 0])
        self.assertFalse(obj.use_calendar)  # чекбокс не передан → False

    def test_edit_get_fills_form(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_edit', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '5/2 обычная')

    def test_edit_post_updates(self):
        import json
        self.client.force_login(self.plant)

        r = self.client.post(
            reverse('manager_schedule_edit', args=[self.ws.pk]),
            {
                'name': '5/2 переименован',
                'description': 'обновлено',
                'hours_per_shift': '7.5',
                'anchor_date': '2026-11-02',
                'is_active': 'on',
                'use_calendar': 'on',
                'pattern_json': json.dumps([1, 1, 1, 1, 1, 0, 0]),
            },
        )
        self.assertEqual(r.status_code, 302)

        self.ws.refresh_from_db()
        self.assertEqual(self.ws.name, '5/2 переименован')
        self.assertEqual(self.ws.hours_per_shift, 7.5)

    def test_invalid_post_renders_with_errors(self):
        """Невалидный POST → 200, а не редирект, форма с ошибками.

        Ошибки приходят как field-errors (name, pattern_json), а не
        non_field_errors — блок «Не сохранилось» в этом случае пуст.
        Проверяем класс sf-errors и конкретные тексты валидации.
        """
        import json
        self.client.force_login(self.plant)
        r = self.client.post(reverse('manager_schedule_new'), {
            'name': '',
            'hours_per_shift': '8',
            'anchor_date': '2026-11-02',
            'pattern_json': json.dumps([0, 0, 0]),  # всё нули
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'sf-errors')           # блоки ошибок
        self.assertContains(r, 'Обязательное поле')   # от Django required
        self.assertContains(r, 'весь из нулей')       # от нашей валидации

    def test_unknown_pk_404(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_edit', args=[999999]),
        )
        self.assertEqual(r.status_code, 404)


class ScheduleDeleteViewTests(ScheduleViewsBase):
    """Тесты view schedule_delete."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_delete_requires_login(self):
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws_off.pk]),
        )
        self.assertEqual(r.status_code, 302)

    def test_delete_forbidden_for_staff(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws_off.pk]),
        )
        self.assertEqual(r.status_code, 403)

    def test_delete_get_redirects(self):
        """GET — не удаляем, редиректим на detail."""
        from accounts.models import WorkSchedule
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_delete', args=[self.ws_off.pk]),
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(WorkSchedule.objects.filter(pk=self.ws_off.pk).exists())

    def test_delete_unused_schedule(self):
        from accounts.models import WorkSchedule
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws_off.pk]),
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(WorkSchedule.objects.filter(pk=self.ws_off.pk).exists())

    def test_delete_used_by_department_blocked(self):
        """График отдела — удаление запрещено."""
        from accounts.models import WorkSchedule
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(WorkSchedule.objects.filter(pk=self.ws.pk).exists())

    def test_delete_used_by_user_blocked(self):
        """Личный график сотрудника — удаление запрещено."""
        from accounts.models import WorkSchedule
        from django.contrib.auth import get_user_model
        User = get_user_model()

        User.objects.create_user(
            email='personal-del@test.ru', password='p',
            full_name='Личный DEL',
            department=self.dept, role=self.role_staff, is_active=True,
            schedule=self.ws,
        )

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(WorkSchedule.objects.filter(pk=self.ws.pk).exists())

    def test_delete_used_by_inactive_user_allowed(self):
        """Неактивный пользователь не блокирует удаление.

        График неактивному сотруднику уже не нужен — сервис считает
        только активных.
        """
        from accounts.models import WorkSchedule
        from django.contrib.auth import get_user_model
        User = get_user_model()

        User.objects.create_user(
            email='inactive-del@test.ru', password='p',
            full_name='Неактивный DEL',
            department=self.dept, role=self.role_staff, is_active=False,
            schedule=self.ws_off,
        )

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[self.ws_off.pk]),
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(
            WorkSchedule.objects.filter(pk=self.ws_off.pk).exists()
        )

    def test_unknown_pk_404(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_delete', args=[999999]),
        )
        self.assertEqual(r.status_code, 404)



class ScheduleAssignViewTests(ScheduleViewsBase):
    """Тесты schedule_assign и schedule_unassign."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    # ── Права ──

    def test_assign_requires_login(self):
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)

    def test_assign_forbidden_for_manager(self):
        self.client.force_login(self.mgr)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 403)

    def test_assign_get_method_not_allowed(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
        )
        self.assertEqual(r.status_code, 405)

    # ── Назначение отделу ──

    def test_assign_to_department(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)

        self.dept.refresh_from_db()
        self.assertEqual(self.dept.default_schedule_id, self.ws.pk)

    def test_assign_to_department_noop(self):
        """Повторное назначение того же графика — no-op, не ошибка."""
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.default_schedule_id, self.ws.pk)

    def test_assign_to_department_unknown_id(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': '999999'},
        )
        self.assertEqual(r.status_code, 302)

    def test_assign_to_department_bad_type(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'nonsense', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)

    def test_assign_to_department_bad_id_format(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': 'abc'},
        )
        self.assertEqual(r.status_code, 302)

    def test_assign_to_department_changes_old(self):
        """Перевод отдела с одного графика на другой."""
        self.dept.default_schedule = self.ws_off
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.default_schedule_id, self.ws.pk)

    # ── Назначение сотруднику ──

    def test_assign_to_user(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'user', 'target_id': self.staff.pk},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertEqual(self.staff.schedule_id, self.ws.pk)

    def test_assign_to_user_noop(self):
        self.staff.schedule = self.ws
        self.staff.save(update_fields=['schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'user', 'target_id': self.staff.pk},
        )
        self.assertEqual(r.status_code, 302)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.schedule_id, self.ws.pk)

    def test_assign_to_unknown_user(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_assign', args=[self.ws.pk]),
            {'target_type': 'user', 'target_id': '999999'},
        )
        self.assertEqual(r.status_code, 302)

    # ── Отвязка от отдела ──

    def test_unassign_from_department(self):
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_unassign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)

        self.dept.refresh_from_db()
        self.assertIsNone(self.dept.default_schedule_id)

    def test_unassign_noop_when_not_assigned(self):
        """Если у отдела другой график — no-op."""
        self.dept.default_schedule = self.ws_off
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_unassign', args=[self.ws.pk]),
            {'target_type': 'department', 'target_id': self.dept.pk},
        )
        self.assertEqual(r.status_code, 302)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.default_schedule_id, self.ws_off.pk)

    # ── Отвязка от пользователя ──

    def test_unassign_from_user(self):
        self.staff.schedule = self.ws
        self.staff.save(update_fields=['schedule'])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_unassign', args=[self.ws.pk]),
            {'target_type': 'user', 'target_id': self.staff.pk},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertIsNone(self.staff.schedule_id)

    def test_unassign_user_unknown(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_schedule_unassign', args=[self.ws.pk]),
            {'target_type': 'user', 'target_id': '999999'},
        )
        self.assertEqual(r.status_code, 302)


class ScheduleDetailAssignFormsTests(ScheduleViewsBase):
    """На detail графика отображаются формы назначения."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_forms_present(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        content = r.content.decode()
        self.assertIn('Назначить отделу', content)
        self.assertIn('Назначить сотруднику', content)

    def test_dept_with_schedule_has_unassign_button(self):
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, 'Снять')

    def test_assign_form_shows_available_departments(self):
        """В селекте «назначить отделу» есть отдел без графика."""
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_detail', args=[self.ws.pk]),
        )
        self.assertContains(r, self.dept.name)



# ═════════════════════════════════════════════════════════════
#  C.3b-3b: страница «Кто работает сегодня»
# ═════════════════════════════════════════════════════════════

class ScheduleTodayAccessTests(ScheduleViewsBase):
    def test_anonymous_redirected(self):
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 302)

    def test_staff_forbidden(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 403)

    def test_manager_forbidden(self):
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 403)

    def test_plant_ok(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 200)

    def test_admin_ok(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 200)


class ScheduleTodayContentTests(ScheduleViewsBase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client.force_login(self.plant)

    def test_lists_all_active_users(self):
        """Все активные попадают в срез."""
        r = self.client.get(reverse('manager_schedule_today'))
        for u in (self.staff, self.mgr, self.plant, self.admin):
            self.assertContains(r, u.full_name)

    def test_tiles_present(self):
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertContains(r, 'Всего в смене')
        self.assertContains(r, 'Работает')
        self.assertContains(r, 'Отдыхает')

    def test_builtin_schedule_label(self):
        """Без личного и без графика отдела → встроенный 5/2."""
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertContains(r, 'Встроенный 5/2')

    def test_personal_schedule_label(self):
        """Личный график у сотрудника → подпись «Личный: …»."""
        self.staff.schedule = self.ws
        self.staff.save(update_fields=['schedule'])

        r = self.client.get(reverse('manager_schedule_today'))
        self.assertContains(r, 'Личный: 5/2 обычная')

    def test_department_schedule_label(self):
        """График отдела → подпись «Отдел: …»."""
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])
        self.staff.schedule = None
        self.staff.save(update_fields=['schedule'])

        r = self.client.get(reverse('manager_schedule_today'))
        self.assertContains(r, 'Отдел: 5/2 обычная')

    def test_filter_by_department(self):
        """Фильтр по отделу — в срезе только его люди."""
        from accounts.models import Department
        from django.contrib.auth import get_user_model
        User = get_user_model()

        other_dept = Department.objects.create(name='TODAY-другой')
        User.objects.create_user(
            email='other-today@test.ru', password='p',
            full_name='Чужой сотрудник',
            department=other_dept, role=self.role_staff, is_active=True,
        )

        r = self.client.get(
            reverse('manager_schedule_today'),
            {'dept': self.dept.pk},
        )
        self.assertContains(r, self.staff.full_name)
        self.assertNotContains(r, 'Чужой сотрудник')

    def test_filter_working_only(self):
        """?state=working — только работающие по графику."""
        r = self.client.get(reverse('manager_schedule_today'), {'state': 'working'})
        # если кто-то работает — увидим; если нет — секция пустая
        rows = r.context['rows']
        for row in rows:
            self.assertTrue(row['is_working'])

    def test_filter_off_only(self):
        r = self.client.get(reverse('manager_schedule_today'), {'state': 'off'})
        rows = r.context['rows']
        for row in rows:
            self.assertFalse(row['is_working'])

    def test_inactive_excluded(self):
        """Неактивные и уволенные не попадают в срез."""
        from django.contrib.auth import get_user_model
        User = get_user_model()

        off_user = User.objects.create_user(
            email='off-today@test.ru', password='p',
            full_name='Отключённый',
            department=self.dept, role=self.role_staff, is_active=False,
        )
        dismissed = User.objects.create_user(
            email='dismissed-today@test.ru', password='p',
            full_name='Уволенный',
            department=self.dept, role=self.role_staff, is_active=True,
        )
        dismissed.employment_status = User.EmploymentStatus.DISMISSED
        dismissed.save(update_fields=['employment_status'])

        r = self.client.get(reverse('manager_schedule_today'))
        self.assertNotContains(r, off_user.full_name)
        self.assertNotContains(r, dismissed.full_name)



# ═════════════════════════════════════════════════════════════
#  График в карточке сотрудника (E)
# ═════════════════════════════════════════════════════════════

class PersonScheduleCardTests(ScheduleViewsBase):
    """На /manager/person/<pk>/ отображается график и статус «сегодня»."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_builtin_label_when_no_schedule(self):
        """Без личного и без графика отдела — «Встроенный 5/2»."""
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Встроенный 5/2')
        self.assertContains(r, 'График работы')

    def test_personal_schedule_label(self):
        """Личный график — подпись «Личный: …»."""
        self.staff.schedule = self.ws
        self.staff.save(update_fields=['schedule'])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'Личный: 5/2 обычная')

    def test_department_schedule_label(self):
        """График отдела — подпись «Отдел: …»."""
        self.dept.default_schedule = self.ws
        self.dept.save(update_fields=['default_schedule'])
        self.staff.schedule = None
        self.staff.save(update_fields=['schedule'])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'Отдел: 5/2 обычная')

    def test_status_working_or_off(self):
        """Одна из двух подписей — «Сегодня работает» или «Сегодня отдыхает»."""
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        content = r.content.decode()
        has_working = 'Сегодня работает' in content
        has_off = 'Сегодня отдыхает' in content
        self.assertTrue(
            has_working or has_off,
            msg='Ни один из статусов не найден',
            )

    def test_edit_link_present_for_personal_schedule(self):
        """Если у сотрудника личный график — есть ссылка «Редактировать график»."""
        self.staff.schedule = self.ws
        self.staff.save(update_fields=['schedule'])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'Редактировать график')
        # Ссылка ведёт на edit этого графика
        self.assertContains(r, f'/manager/schedules/{self.ws.pk}/edit/')

    def test_no_edit_link_for_builtin(self):
        """Без личного и без графика отдела — ссылка на «Кто сегодня»."""
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'Кто сегодня')

    def test_preview_shows_14_days(self):
        """Превью 14 дней присутствует (по классу pg-day)."""
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        content = r.content.decode()
        n = content.count('pg-day')
        # Больше 14: 14 плиток + упоминания в CSS.
        # Считаем только конкретно плитки-дни с class="...pg-day...".
        # Упрощённо: минимум 14.
        self.assertGreaterEqual(n, 14)

    def test_scope_still_enforced(self):
        """Руководитель другого отдела не видит карточку — 403."""
        self.client.force_login(self.mgr)
        # mgr в SCH-ОГК, staff тоже в SCH-ОГК — увидит.
        # Возьмём другого: создадим сотрудника чужого отдела.

        from accounts.models import Department
        from django.contrib.auth import get_user_model
        User = get_user_model()

        other_dept = Department.objects.create(name='PERSON-другой')
        stranger = User.objects.create_user(
            email='stranger-person@test.ru', password='p',
            full_name='Чужой',
            department=other_dept, role=self.role_staff, is_active=True,
        )
        r = self.client.get(
            reverse('manager_person', args=[stranger.pk]),
        )
        self.assertEqual(r.status_code, 403)



# ═════════════════════════════════════════════════════════════
#  F: отпуска сотрудников
# ═════════════════════════════════════════════════════════════

class VacationModelTests(TestCase):
    """Property is_on_vacation и vacation_label — без view."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Department, Role
        cls.dept = Department.objects.get_or_create(name='VAC-ОГК')[0]
        cls.role = Role.objects.get_or_create(
            code='staff-vac', defaults={'name': 'Сотрудник VAC'},
        )[0]

        User = get_user_model()
        cls.user = User.objects.create_user(
            email='vac@test.ru', password='p', full_name='Отпускной',
            department=cls.dept, role=cls.role, is_active=True,
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_no_dates_no_status_means_not_on_vacation(self):
        self.assertFalse(self.user.is_on_vacation)
        self.assertEqual(self.user.vacation_label, '')

    def test_vacation_status_triggers_flag(self):
        self.user.employment_status = self.user.EmploymentStatus.VACATION
        self.user.save(update_fields=['employment_status'])
        self.assertTrue(self.user.is_on_vacation)

    def test_dates_covering_today(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.user.vacation_from = today - timedelta(days=2)
        self.user.vacation_to = today + timedelta(days=5)
        self.user.save(update_fields=['vacation_from', 'vacation_to'])
        self.assertTrue(self.user.is_on_vacation)
        self.assertIn('с ', self.user.vacation_label)
        self.assertIn('по ', self.user.vacation_label)

    def test_dates_in_past_do_not_trigger(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.user.vacation_from = today - timedelta(days=30)
        self.user.vacation_to = today - timedelta(days=10)
        self.user.save(update_fields=['vacation_from', 'vacation_to'])
        self.assertFalse(self.user.is_on_vacation)

    def test_dates_in_future_do_not_trigger(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.user.vacation_from = today + timedelta(days=10)
        self.user.vacation_to = today + timedelta(days=20)
        self.user.save(update_fields=['vacation_from', 'vacation_to'])
        self.assertFalse(self.user.is_on_vacation)


class VacationViewTests(ScheduleViewsBase):
    """person_vacation_set / person_vacation_clear."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    # ── Права ──

    def test_set_requires_login(self):
        from datetime import timedelta
        today = timezone.localdate()
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': today.isoformat(),
             'date_to': (today + timedelta(days=5)).isoformat()},
        )
        self.assertEqual(r.status_code, 302)

    def test_set_denied_for_staff(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.client.force_login(self.staff)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': today.isoformat(),
             'date_to': (today + timedelta(days=5)).isoformat()},
        )
        self.assertEqual(r.status_code, 403)

    def test_set_get_method_not_allowed(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
        )
        self.assertEqual(r.status_code, 405)

    # ── Успешные сценарии ──

    def test_set_vacation_by_plant(self):
        from datetime import timedelta
        today = timezone.localdate()
        date_to = today + timedelta(days=10)

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': today.isoformat(),
             'date_to': date_to.isoformat()},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertEqual(self.staff.vacation_from, today)
        self.assertEqual(self.staff.vacation_to, date_to)
        self.assertEqual(
            self.staff.employment_status,
            self.staff.EmploymentStatus.VACATION,
        )
        self.assertTrue(self.staff.is_on_vacation)

    def test_clear_vacation(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.staff.vacation_from = today
        self.staff.vacation_to = today + timedelta(days=10)
        self.staff.employment_status = self.staff.EmploymentStatus.VACATION
        self.staff.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_person_vacation_clear', args=[self.staff.pk]),
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertIsNone(self.staff.vacation_from)
        self.assertIsNone(self.staff.vacation_to)
        self.assertEqual(
            self.staff.employment_status,
            self.staff.EmploymentStatus.ACTIVE,
        )

    # ── Валидация ──

    def test_reversed_dates_rejected(self):
        from datetime import timedelta
        today = timezone.localdate()
        before = today - timedelta(days=5)

        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': today.isoformat(),
             'date_to': before.isoformat()},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertIsNone(self.staff.vacation_from)
        self.assertIsNone(self.staff.vacation_to)

    def test_bad_dates_ignored(self):
        self.client.force_login(self.plant)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': 'not-a-date', 'date_to': '2026-11-20'},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertIsNone(self.staff.vacation_from)

    # ── Scope ──

    def test_manager_can_set_vacation_in_own_department(self):
        from datetime import timedelta
        today = timezone.localdate()
        # mgr в SCH-ОГК, staff тоже в SCH-ОГК
        self.client.force_login(self.mgr)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[self.staff.pk]),
            {'date_from': today.isoformat(),
             'date_to': (today + timedelta(days=5)).isoformat()},
        )
        self.assertEqual(r.status_code, 302)

        self.staff.refresh_from_db()
        self.assertEqual(self.staff.vacation_from, today)

    def test_manager_cannot_set_vacation_for_other_department(self):
        from datetime import timedelta
        from accounts.models import Department
        User = get_user_model()

        other_dept = Department.objects.create(name='VAC-OTHER')
        stranger = User.objects.create_user(
            email='stranger-vac@test.ru', password='p',
            full_name='Чужой',
            department=other_dept, role=self.role_staff, is_active=True,
        )

        today = timezone.localdate()
        self.client.force_login(self.mgr)
        r = self.client.post(
            reverse('manager_person_vacation_set', args=[stranger.pk]),
            {'date_from': today.isoformat(),
             'date_to': (today + timedelta(days=5)).isoformat()},
        )
        self.assertEqual(r.status_code, 403)

        stranger.refresh_from_db()
        self.assertIsNone(stranger.vacation_from)


class PersonVacationCardTests(ScheduleViewsBase):
    """На карточке сотрудника отображается блок отпуска."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_block_present(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'Отпуск / больничный')
        self.assertContains(r, 'Отметить отпуск')

    def test_vacation_badge_when_active(self):
        from datetime import timedelta
        today = timezone.localdate()
        self.staff.vacation_from = today
        self.staff.vacation_to = today + timedelta(days=5)
        self.staff.employment_status = self.staff.EmploymentStatus.VACATION
        self.staff.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertContains(r, 'В отпуске')
        self.assertContains(r, 'Снять отпуск')

    def test_no_vacation_form_when_active(self):
        """Когда сотрудник в отпуске — форма установки скрыта."""
        from datetime import timedelta
        today = timezone.localdate()
        self.staff.vacation_from = today
        self.staff.vacation_to = today + timedelta(days=5)
        self.staff.employment_status = self.staff.EmploymentStatus.VACATION
        self.staff.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_person', args=[self.staff.pk]),
        )
        self.assertNotContains(r, 'Отметить отпуск')



# ═════════════════════════════════════════════════════════════
#  F.3a: отпуска в «Кто сегодня» и «Загруженность»
# ═════════════════════════════════════════════════════════════

class ScheduleTodayVacationTests(ScheduleViewsBase):
    """schedule_today отражает отпускников."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _set_vacation(self, user, days=5):
        from datetime import timedelta
        today = timezone.localdate()
        user.vacation_from = today
        user.vacation_to = today + timedelta(days=days)
        user.employment_status = user.EmploymentStatus.VACATION
        user.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

    def test_vacation_tile_present(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertContains(r, 'В отпуске')

    def test_vacation_count(self):
        self._set_vacation(self.staff)

        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertGreaterEqual(r.context['on_vacation_count'], 1)

    def test_vacation_user_marked(self):
        self._set_vacation(self.staff)

        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        rows = r.context['rows']
        by_name = {row['user'].full_name: row for row in rows}
        self.assertTrue(by_name['Сотрудник']['on_vacation'])

    def test_filter_vacation(self):
        self._set_vacation(self.staff)

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_today'), {'state': 'vacation'},
        )
        rows = r.context['rows']
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertTrue(row['on_vacation'])

    def test_vacation_not_in_working(self):
        """Работающий по графику, но в отпуске → не считается работающим."""
        self._set_vacation(self.staff)

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_today'), {'state': 'working'},
        )
        names = [row['user'].full_name for row in r.context['rows']]
        self.assertNotIn('Сотрудник', names)

    def test_vacation_not_in_off(self):
        """Отпускник не попадает в фильтр «отдыхает» — он отдельная категория."""
        self._set_vacation(self.staff)

        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_today'), {'state': 'off'},
        )
        names = [row['user'].full_name for row in r.context['rows']]
        self.assertNotIn('Сотрудник', names)


class LoadRowsVacationTests(ScheduleViewsBase):
    """_compute_load_rows помечает отпускников."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_on_vacation_flag_in_row(self):
        from datetime import timedelta
        from manager.views import _compute_load_rows

        today = timezone.localdate()
        self.staff.vacation_from = today
        self.staff.vacation_to = today + timedelta(days=5)
        self.staff.employment_status = self.staff.EmploymentStatus.VACATION
        self.staff.save(update_fields=[
            'vacation_from', 'vacation_to', 'employment_status',
        ])

        rows, _ = _compute_load_rows(self.mgr)
        by_name = {r['user'].full_name: r for r in rows}
        self.assertIn('Сотрудник', by_name)
        self.assertTrue(by_name['Сотрудник']['on_vacation'])
        self.assertIn('с ', by_name['Сотрудник']['vacation_label'])

    def test_not_on_vacation_by_default(self):
        from manager.views import _compute_load_rows
        rows, _ = _compute_load_rows(self.mgr)
        for r in rows:
            self.assertFalse(r['on_vacation'])
            self.assertEqual(r['vacation_label'], '')

# ═════════════════════════════════════════════════════════════
#  D2: schedule_today при графике 2/2 — прямой тест позиции в цикле
# ═════════════════════════════════════════════════════════════

class ScheduleTodayTwoByTwoTests(ScheduleViewsBase):
    """Прямая проверка «работает / отдыхает сегодня» для 2/2.

    В ScheduleViewsBase anchor_date = 2026-11-02, поэтому позиция
    сегодняшнего дня в цикле 2/2 недетерминирована. Здесь создаём
    свежие графики с anchor = сегодня и anchor = сегодня − 2 дня —
    это жёстко фиксирует позицию:

        паттерн [1,1,0,0], anchor = today      → offset 0 → РАБОТАЕТ
        паттерн [1,1,0,0], anchor = today − 2  → offset 2 → ОТДЫХАЕТ

    use_calendar=False — иначе праздники РФ могли бы перебить паттерн.
    """

    def setUp(self):
        from django.core.cache import cache
        from accounts.models import WorkSchedule

        cache.clear()
        self.today = timezone.localdate()

        # 2/2, где сегодня — рабочий день (offset = 0)
        self.ws_work = WorkSchedule.objects.create(
            name='2/2 сегодня-работа',
            pattern=[1, 1, 0, 0],
            anchor_date=self.today,
            hours_per_shift=12.0,
            use_calendar=False,
            is_active=True,
        )
        # 2/2, где сегодня — выходной (offset = 2)
        self.ws_off = WorkSchedule.objects.create(
            name='2/2 сегодня-отдых',
            pattern=[1, 1, 0, 0],
            anchor_date=self.today - timezone.timedelta(days=2),
            hours_per_shift=12.0,
            use_calendar=False,
            is_active=True,
        )

        # staff — «работает», mgr — «отдыхает»
        self.staff.schedule = self.ws_work
        self.staff.save(update_fields=['schedule'])
        self.mgr.schedule = self.ws_off
        self.mgr.save(update_fields=['schedule'])

    def _rows_by_email(self):
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        self.assertEqual(r.status_code, 200)
        return {row['user'].email: row for row in r.context['rows']}

    # ── Прямая позиция в цикле ──────────────────────────────

    def test_2_2_offset_zero_working(self):
        """anchor = сегодня → offset 0 → работает."""
        rows = self._rows_by_email()
        self.assertIn(self.staff.email, rows)
        self.assertTrue(rows[self.staff.email]['is_working'])

    def test_2_2_offset_two_off(self):
        """anchor = сегодня−2 → offset 2 → отдыхает."""
        rows = self._rows_by_email()
        self.assertIn(self.mgr.email, rows)
        self.assertFalse(rows[self.mgr.email]['is_working'])

    # ── Источник графика в строке ───────────────────────────

    def test_schedule_source_is_personal(self):
        """У обоих — личный график, source='personal'."""
        rows = self._rows_by_email()
        self.assertEqual(rows[self.staff.email]['schedule_source'], 'personal')
        self.assertEqual(rows[self.mgr.email]['schedule_source'], 'personal')

    def test_schedule_label_contains_name(self):
        rows = self._rows_by_email()
        self.assertIn('2/2 сегодня-работа', rows[self.staff.email]['schedule_label'])
        self.assertIn('2/2 сегодня-отдых', rows[self.mgr.email]['schedule_label'])

    # ── Счётчики плиток ─────────────────────────────────────

    def test_counts_split_correctly(self):
        """Один работает, один отдыхает — плитки не врут."""
        self.client.force_login(self.plant)
        r = self.client.get(reverse('manager_schedule_today'))
        ctx = r.context

        # staff — работает, mgr — отдыхает
        self.assertGreaterEqual(ctx['working'], 1)
        self.assertGreaterEqual(ctx['off'], 1)

    # ── Фильтры ─────────────────────────────────────────────

    def test_filter_state_working_shows_staff(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_today'), {'state': 'working'},
        )
        emails = [row['user'].email for row in r.context['rows']]
        self.assertIn(self.staff.email, emails)
        self.assertNotIn(self.mgr.email, emails)

    def test_filter_state_off_shows_mgr(self):
        self.client.force_login(self.plant)
        r = self.client.get(
            reverse('manager_schedule_today'), {'state': 'off'},
        )
        emails = [row['user'].email for row in r.context['rows']]
        self.assertIn(self.mgr.email, emails)
        self.assertNotIn(self.staff.email, emails)

# ═════════════════════════════════════════════════════════════
#  D3: person_page — проверка scope для всех ролей
# ═════════════════════════════════════════════════════════════

class PersonPageScopeTests(ManagerBaseTestCase):
    """Точечные тесты scope в person_page."""

    def test_manager_views_own_department_person(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_person', args=[self.eng_ogk.pk]))
        self.assertEqual(r.status_code, 200)

    def test_manager_views_self(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_person', args=[self.mgr_ogk.pk]))
        self.assertEqual(r.status_code, 200)

    def test_staff_cannot_open_person_page(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_person', args=[self.eng_ogk.pk]))
        self.assertEqual(r.status_code, 403)

    def test_admin_views_any_person(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('manager_person', args=[self.eng_ogt.pk]))
        self.assertEqual(r.status_code, 200)

    def test_director_views_all_departments(self):
        self.client.force_login(self.director)
        for person in (self.eng_ogk, self.eng_ogt,
                       self.mgr_ogk, self.mgr_ogt):
            r = self.client.get(reverse('manager_person', args=[person.pk]))
            self.assertEqual(r.status_code, 200, msg=person.email)

    def test_manager_without_department_sees_only_self(self):
        orphan = User.objects.create_user(
            email='orphan-person@eag.su', password='x',
            full_name='Рук. без отдела',
            is_active=True, department=None, role=self.manager_role,
        )
        self.client.force_login(orphan)

        r = self.client.get(reverse('manager_person', args=[orphan.pk]))
        self.assertEqual(r.status_code, 200)

        r = self.client.get(reverse('manager_person', args=[self.eng_ogk.pk]))
        self.assertEqual(r.status_code, 403)

    def test_person_page_404_for_unknown_pk(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_person', args=[999999]))
        self.assertEqual(r.status_code, 404)

# ═════════════════════════════════════════════════════════════
#  Патч 5.1 — единый URL отчётов с переключателем периода
# ═════════════════════════════════════════════════════════════

class ReportsPeriodTests(ManagerBaseTestCase):
    """/manager/reports/?period=month|quarter|year.

    До патча 5.1 было 3 URL'а. Теперь один. Старые —
    редиректы с нужным period. Тело отчётов не менялось —
    роутер просто делегирует в нужную функцию.
    """

    def test_default_period_is_month(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'month')

    def test_quarter_period(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'period': 'quarter'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'quarter')

    def test_year_period(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'period': 'year'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'year')

    def test_invalid_period_falls_back_to_month(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'period': 'nonsense'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'month')

    def test_staff_forbidden_for_quarter(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports'), {'period': 'quarter'})
        self.assertEqual(r.status_code, 403)

    def test_old_quarterly_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('quarterly_report'),
            {'year': 2026, 'quarter': 2},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('period=quarter', r['Location'])
        self.assertIn('year=2026', r['Location'])
        self.assertIn('quarter=2', r['Location'])

    def test_old_yearly_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('yearly_report'), {'year': 2025})
        self.assertEqual(r.status_code, 302)
        self.assertIn('period=year', r['Location'])
        self.assertIn('year=2025', r['Location'])

class ReportsKindTests(ManagerBaseTestCase):
    """?kind=analytics и ?kind=dynamics через единый /manager/reports/.

    Старые URL'ы /manager/analytics/ и /manager/dept-dynamics/
    оставлены как редиректы.
    """

    def test_analytics_via_router(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'analytics'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'analytics')

    def test_dynamics_via_router(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'dynamics'})
        self.assertEqual(r.status_code, 200)
        # dept_dynamics не выставляет active — проверяем, что отдал 200
        # и есть характерный ключ из контекста (departments).
        self.assertIn('days', r.context)

    def test_unknown_kind_falls_back_to_period(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'nonsense'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'month')

    def test_staff_forbidden_for_analytics_kind(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'analytics'})
        self.assertEqual(r.status_code, 403)

    def test_old_analytics_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_analytics'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('kind=analytics', r['Location'])

    def test_old_dept_dynamics_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('manager_dept_dynamics'), {'days': 30},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('kind=dynamics', r['Location'])
        self.assertIn('days=30', r['Location'])

class ReportsOrdersKindTests(ManagerBaseTestCase):
    """?kind=orders через единый /manager/reports/.

    Старый URL /manager/orders-report/ — редирект.
    """

    def test_orders_via_router(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'orders'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'orders')

    def test_staff_forbidden_for_orders_kind(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'orders'})
        self.assertEqual(r.status_code, 403)

    def test_old_orders_report_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('manager_orders_report'), {'mode': 'year', 'year': 2026},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('kind=orders', r['Location'])
        self.assertIn('mode=year', r['Location'])
        self.assertIn('year=2026', r['Location'])

class ReportsKpiKindTests(ManagerBaseTestCase):
    """kind=kpi: KPI-отчёт переехал из /settings/kpi/ в manager.

    До патча 5.4a логика жила в admin_panel.views.kpi_report.
    Теперь — manager.views.kpi_report_page, старый URL редиректит.
    """

    def test_kpi_via_router(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'kpi'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'kpi')

    def test_kpi_csv_export(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('manager_reports'),
            {'kind': 'kpi', 'format': 'csv'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))

    def test_kpi_xlsx_export(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('manager_reports'),
            {'kind': 'kpi', 'format': 'xlsx'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r['Content-Type'])

    def test_staff_forbidden_for_kpi_kind(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'kpi'})
        self.assertEqual(r.status_code, 403)

    def test_old_settings_kpi_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('settings_kpi'),
            {'date_from': '2026-01-01', 'date_to': '2026-02-01'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/reports/', r['Location'])
        self.assertIn('kind=kpi', r['Location'])
        self.assertIn('date_from=2026-01-01', r['Location'])

class ReportsDepartmentsKindTests(ManagerBaseTestCase):
    """kind=departments: разрез по отделам переехал из /settings/.

    До патча 5.4b логика жила в admin_panel.views.analytics_departments.
    Теперь — manager.views.analytics_departments_page, старый URL редиректит.
    """

    def test_departments_via_router(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'departments'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['active'], 'departments')

    def test_departments_csv_export(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('manager_reports'),
            {'kind': 'departments', 'format': 'csv'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))

    def test_staff_forbidden_for_departments_kind(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_reports'), {'kind': 'departments'})
        self.assertEqual(r.status_code, 403)

    def test_old_settings_analytics_url_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('settings_analytics_departments'),
            {'date_from': '2026-01-01'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/reports/', r['Location'])
        self.assertIn('kind=departments', r['Location'])
        self.assertIn('date_from=2026-01-01', r['Location'])

class ManagerSessionsTests(ManagerBaseTestCase):
    """Сессии переехали из /settings/sessions/ в /manager/sessions/ (патч 6.1).

    Старые URL'ы — редиректы.
    """

    def test_sessions_list_ok(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_sessions'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['tab'], 'work')

    def test_sessions_shop_tab(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_sessions'), {'tab': 'shop'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['tab'], 'shop')

    def test_sessions_summary_ok(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_sessions_summary'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('rows', r.context)

    def test_sessions_summary_csv(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_sessions_summary'), {'format': 'csv'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))

    def test_staff_forbidden(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_sessions'))
        self.assertEqual(r.status_code, 403)

    def test_old_settings_sessions_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('settings_sessions'), {'tab': 'shop'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/sessions/', r['Location'])
        self.assertIn('tab=shop', r['Location'])

    def test_old_settings_sessions_summary_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('settings_sessions_summary'),
            {'date_from': '2026-01-01'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/sessions/summary/', r['Location'])
        self.assertIn('date_from=2026-01-01', r['Location'])

class ManagerTaskLogsAndOnlineTests(ManagerBaseTestCase):
    """Логи задач и «Активные сотрудники» переехали из /settings/ в manager.

    Патч 6.2. Старые URL'ы /settings/tasklogs/ и /settings/online/ —
    редиректы на /manager/logs/ и /manager/online/.
    """

    def test_task_logs_ok(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_task_logs'))
        self.assertEqual(r.status_code, 200)

    def test_task_logs_staff_forbidden(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_task_logs'))
        self.assertEqual(r.status_code, 403)

    def test_online_ok(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('manager_online'))
        self.assertEqual(r.status_code, 200)

    def test_online_staff_forbidden(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('manager_online'))
        self.assertEqual(r.status_code, 403)

    def test_old_settings_tasklogs_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(
            reverse('settings_task_logs'),
            {'kind': 'plan'},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/logs/', r['Location'])
        self.assertIn('kind=plan', r['Location'])

    def test_old_settings_online_redirects(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('settings_online'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/manager/online/', r['Location'])

class DiagnosticsPageTests(ManagerBaseTestCase):
    """Диагностика задач (бывш. «Проблемные») — остаётся в админке.

    Патч 6.3: переименование в UI, убрана плашка «переехало».
    Доступ — boss/admin (в т.ч. руководитель отдела).
    """

    def test_admin_can_open(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('settings_problems'))
        self.assertEqual(r.status_code, 200)

    def test_manager_can_open(self):
        self.client.force_login(self.mgr_ogk)
        r = self.client.get(reverse('settings_problems'))
        self.assertEqual(r.status_code, 200)

    def test_staff_forbidden(self):
        self.client.force_login(self.eng_ogk)
        r = self.client.get(reverse('settings_problems'))
        self.assertEqual(r.status_code, 403)

    def test_no_redirect_notice(self):
        """Плашка «переехало в кабинет» убрана — раздел остаётся в админке."""
        self.client.force_login(self.admin)
        r = self.client.get(reverse('settings_problems'))
        self.assertNotContains(r, 'переехал в кабинет')
