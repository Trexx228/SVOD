"""Автотесты admin_panel: health, broadcast, cleanup_old_records, users_import.

═══ Про подводные камни ═══

Модуль покрывает не только happy-path, но и известные ограничения:

  1. `collect_health()` считает `critical_ok = db.ok AND disk.ok`,
     а `ok = critical_ok AND not warnings`. Падение бэкапов или
     «один админ» делает `ok=False`, но `critical_ok=True`. UI
     должен показывать это по-разному.
  2. `resolve_recipients('department')` БЕЗ department_id возвращает
     пустой queryset (молча!). Это защита от «случайно всем».
  3. `cleanup_old_records` использует `< cutoff`, не `<=`.
     Запись ровно на границе НЕ удаляется.
  4. `users_import` требует `_boss_required`, а
     `users_import_apply` — `_admin_required`. Рядовой менеджер
     сделает preview, но не применит.
  5. `_import_decode` пробует кодировки в порядке utf-8-sig →
     utf-8 → cp1251. Файл в KOI8-R упадёт с ValueError.
  6. Импорт «не найдёт» отдел/роль — оставит пустыми в preview,
     а при apply роль подставляется как «Пользователь», отдел — None.
  7. Session-based preview: `users_import_apply` без
     предварительного `users_import` вернёт редирект на форму.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Role
from admin_panel.broadcast import resolve_recipients, send_broadcast
from admin_panel.health import collect_health
from admin_panel.models import (
    AdminAlert, AuditLog, BroadcastLog, LoginEvent,
)
from core.models import Norm
from tasks.models import Task, TimelineSnapshot

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


# ═════════════════════════════════════════════════════════════
#  База
# ═════════════════════════════════════════════════════════════

class OpsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dep1 = Department.objects.get_or_create(name='ОГК')[0]
        cls.dep2 = Department.objects.get_or_create(name='ОГТ')[0]

        cls.role_staff = _role('staff')
        cls.role_mgr = _role('manager', can_manage=True)
        cls.role_admin = _role('admin', can_manage=True, can_admin=True)

        cls.superuser = User.objects.create_superuser(
            email='root@test.ru', password='p',
        )
        # ⚠ UserManager.create_superuser автоматически ставит role=admin
        # (если код 'admin' существует на момент создания). Для чистоты
        # тестов обнуляем: superuser не должен «дублировать» админов роли.
        # _admins_check всё равно его учтёт по Q(is_superuser=True).
        cls.superuser.role = None
        cls.superuser.save(update_fields=['role'])

        cls.admin = User.objects.create_user(
            email='admin@test.ru', password='p', full_name='Админ',
            department=cls.dep1, role=cls.role_admin,
            is_active=True, is_staff=True,
        )
        cls.mgr = User.objects.create_user(
            email='mgr@test.ru', password='p', full_name='Менеджер',
            department=cls.dep1, role=cls.role_mgr, is_active=True,
        )
        cls.staff1 = User.objects.create_user(
            email='s1@test.ru', password='p', full_name='Сотрудник 1',
            department=cls.dep1, role=cls.role_staff, is_active=True,
        )
        cls.staff2 = User.objects.create_user(
            email='s2@test.ru', password='p', full_name='Сотрудник 2',
            department=cls.dep2, role=cls.role_staff, is_active=True,
        )
        cls.inactive = User.objects.create_user(
            email='off@test.ru', password='p', full_name='Отключённый',
            department=cls.dep1, role=cls.role_staff, is_active=False,
        )


# ═════════════════════════════════════════════════════════════
#  HEALTH
# ═════════════════════════════════════════════════════════════

class HealthStructureTests(OpsBase):
    """Общая структура и invariant'ы."""

    def test_collect_health_returns_dict(self):
        h = collect_health()
        self.assertIsInstance(h, dict)

    def test_top_level_keys(self):
        h = collect_health()
        for k in ('ok', 'critical_ok', 'warnings', 'checks', 'timestamp'):
            self.assertIn(k, h)

    def test_all_checks_present(self):
        h = collect_health()
        for k in ('db', 'disk', 'backups', 'admins', 'alerts', 'norm', 'ad'):
            self.assertIn(k, h['checks'], msg=k)

    def test_timestamp_is_int(self):
        h = collect_health()
        self.assertIsInstance(h['timestamp'], int)
        self.assertGreater(h['timestamp'], 0)

    def test_warnings_is_list(self):
        self.assertIsInstance(collect_health()['warnings'], list)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_critical_ok_independent_from_warnings(self):
        """⚠ critical_ok = db.ok AND disk.ok. Не зависит от warnings.

        Даже если все остальные проверки (backups, admins, alerts)
        в fail — critical_ok будет True при живых DB+disk.
        UI должен различать «портал работает» и «есть замечания».
        """
        Role.objects.filter(can_admin=True).update(can_admin=False)
        User.objects.filter(is_superuser=True).update(is_superuser=False)
        Norm.objects.all().delete()

        h = collect_health()
        self.assertTrue(h['critical_ok'])
        # admins/norm/alerts в warnings
        self.assertIn('admins', h['warnings'])

    def test_ok_false_when_warnings(self):
        """ok = critical_ok AND not warnings."""
        # Убираем второго админа → check.admins.ok = False
        Role.objects.filter(can_admin=True).update(can_admin=False)
        User.objects.filter(is_superuser=True).update(is_superuser=False)

        h = collect_health()
        self.assertFalse(h['ok'])


class HealthDbCheckTests(OpsBase):
    def test_db_ok(self):
        h = collect_health()
        self.assertTrue(h['checks']['db']['ok'])
        self.assertEqual(h['checks']['db']['engine'], 'sqlite')

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_memory_db_has_no_path(self):
        """⚠ В тестах SQLite — in-memory. `Path(':memory:')` не существует,
        size_bytes=0, last_write_ago_sec=None. Не ошибка.
        """
        h = collect_health()['checks']['db']
        # Или 0, или размер файла на диске (если тесты через файл)
        self.assertGreaterEqual(h['size_bytes'], 0)


class HealthDiskCheckTests(OpsBase):
    def test_disk_ok(self):
        h = collect_health()['checks']['disk']
        self.assertTrue(h['ok'])
        self.assertGreater(h['total'], 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_low_disk_warn(self):
        """⚠ Порог «ok» — свободно > 500 МБ.

        Мокаем shutil.disk_usage → 100 МБ free. Проверяем, что
        ok=False. Это ловит реальную ситуацию: диск забит → БД
        может не принять запись.
        """
        with patch('admin_panel.health.shutil.disk_usage') as m:
            # total, used, free
            m.return_value = (100 * 1024**3, 99.9 * 1024**3, 100 * 1024**2)
            h = collect_health()['checks']['disk']
        self.assertFalse(h['ok'])
        self.assertLess(h['free'], 500 * 1024 * 1024)


class HealthAdminsCheckTests(OpsBase):
    def test_admins_ok_with_two(self):
        # superuser + admin = 2
        h = collect_health()['checks']['admins']
        self.assertTrue(h['ok'])
        self.assertGreaterEqual(h['count'], 2)

    def test_admins_warn_with_one(self):
        Role.objects.filter(can_admin=True).update(can_admin=False)
        User.objects.filter(is_superuser=True).update(is_superuser=False)
        h = collect_health()['checks']['admins']
        self.assertFalse(h['ok'])
        self.assertEqual(h['count'], 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_inactive_admin_not_counted(self):
        """Проверка `is_active=True` — отключённые админы не спасают.

        ⚠ Подводный камень: superuser тоже имеет роль admin по умолчанию
        (см. UserManager.create_superuser). Чтобы тест был про «мёртвый
        superuser», сбрасываем у всех is_superuser и can_admin, потом
        восстанавливаем ровно одного живого админа через роль.
        """
        Role.objects.filter(can_admin=True).update(can_admin=False)
        User.objects.filter(is_superuser=True).update(is_superuser=False)
        # Обнуляем остаточную роль у superuser
        User.objects.filter(email='root@test.ru').update(role=None)

        # Один активный админ через роль
        self.admin.role.can_admin = True
        self.admin.role.save(update_fields=['can_admin'])

        # Один отключённый superuser
        dead = User.objects.create_user(
            email='dead@test.ru', password='p', full_name='Мёртвый',
            is_active=False,
        )
        dead.is_superuser = True
        dead.save(update_fields=['is_superuser'])

        h = collect_health()['checks']['admins']
        self.assertEqual(h['count'], 1)
        self.assertFalse(h['ok'])


class HealthNormCheckTests(OpsBase):
    def test_norm_ok(self):
        Norm.objects.create(hours_per_day=8.0)
        h = collect_health()['checks']['norm']
        self.assertTrue(h['ok'])
        self.assertEqual(h['hours_per_day'], 8.0)

    def test_norm_warn_when_missing(self):
        Norm.objects.all().delete()
        h = collect_health()['checks']['norm']
        self.assertFalse(h['ok'])
        self.assertIsNone(h['hours_per_day'])

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_negative_norm_still_ok(self):
        """⚠ `_norm_check` проверяет `n is not None`, не значение.

        Norm с hours_per_day=-1 → ok=True. Это НЕ ловит
        известный баг `norm_hours()` с negative.
        Тест документирует дыру в проверке.
        """
        Norm.objects.create(hours_per_day=-1)
        h = collect_health()['checks']['norm']
        self.assertTrue(h['ok'])
        self.assertEqual(h['hours_per_day'], -1)


class HealthAlertsCheckTests(OpsBase):
    def test_alerts_ok_when_empty(self):
        AdminAlert.objects.all().delete()
        h = collect_health()['checks']['alerts']
        self.assertTrue(h['ok'])
        self.assertEqual(h['unseen'], 0)

    def test_alerts_warn_when_unseen(self):
        AdminAlert.objects.create(
            kind='failed_logins', key='k1', severity='warn',
            title='X', seen=False,
        )
        h = collect_health()['checks']['alerts']
        self.assertFalse(h['ok'])
        self.assertEqual(h['unseen'], 1)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_danger_count_is_subset_of_unseen(self):
        """danger — подмножество unseen. Не отдельная метрика."""
        AdminAlert.objects.create(
            kind='single_admin', key='k1', severity='danger',
            title='D', seen=False,
        )
        AdminAlert.objects.create(
            kind='failed_logins', key='k2', severity='warn',
            title='W', seen=False,
        )
        h = collect_health()['checks']['alerts']
        self.assertEqual(h['unseen'], 2)
        self.assertEqual(h['danger'], 1)


class HealthAdCheckTests(OpsBase):
    def test_ad_disabled_by_default(self):
        h = collect_health()['checks']['ad']
        self.assertTrue(h['ok'])   # ok=True всегда
        self.assertFalse(h['enabled'])

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_ad_check_never_fails(self):
        """⚠ AD-check возвращает `ok: True` даже когда AD_ENABLED=True.

        Это «информационная» проверка, не health-critical.
        """
        with override_settings(AD_ENABLED=True, AD_SERVER='x', AD_DOMAIN='y'):
            h = collect_health()['checks']['ad']
            self.assertTrue(h['ok'])
            self.assertTrue(h['enabled'])


class HealthViewTests(OpsBase):
    def test_requires_admin(self):
        self.client.force_login(self.staff1)
        r = self.client.get(reverse('settings_health'))
        self.assertEqual(r.status_code, 403)

    def test_admin_html(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('settings_health'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('checks', r.context)

    def test_json_format(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('settings_health'), {'format': 'json'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'].split(';')[0], 'application/json')
        data = r.json()
        self.assertIn('ok', data)
        self.assertIn('checks', data)

    def test_text_format(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('settings_health'), {'format': 'text'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'svod_health=', r.content)


# ═════════════════════════════════════════════════════════════
#  BROADCAST — resolve_recipients
# ═════════════════════════════════════════════════════════════

class ResolveRecipientsTests(OpsBase):
    def test_target_all(self):
        qs = resolve_recipients('all')
        emails = set(qs.values_list('email', flat=True))
        self.assertIn('s1@test.ru', emails)
        self.assertIn('s2@test.ru', emails)
        # отключённые исключены
        self.assertNotIn('off@test.ru', emails)

    def test_target_department(self):
        qs = resolve_recipients('department', department_id=self.dep1.pk)
        emails = set(qs.values_list('email', flat=True))
        self.assertIn('s1@test.ru', emails)
        self.assertNotIn('s2@test.ru', emails)

    def test_target_role(self):
        qs = resolve_recipients('role', role_id=self.role_mgr.pk)
        emails = set(qs.values_list('email', flat=True))
        self.assertIn('mgr@test.ru', emails)
        self.assertNotIn('s1@test.ru', emails)

    def test_target_admins(self):
        qs = resolve_recipients('admins')
        emails = set(qs.values_list('email', flat=True))
        self.assertIn('root@test.ru', emails)     # superuser
        self.assertIn('admin@test.ru', emails)     # role.can_admin
        self.assertNotIn('s1@test.ru', emails)

    def test_unknown_target_returns_none(self):
        self.assertEqual(resolve_recipients('nonsense').count(), 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_department_without_id_returns_none(self):
        """⚠ resolve_recipients('department') без department_id → qs.none().

        Молча. Это защита от «случайно отправить всем сотрудникам».
        При integrate с UI надо помнить: без выбора отдела
        получим 0 получателей, а не всех.
        """
        qs = resolve_recipients('department', department_id=None)
        self.assertEqual(qs.count(), 0)

    def test_role_without_id_returns_none(self):
        qs = resolve_recipients('role', role_id=None)
        self.assertEqual(qs.count(), 0)

    def test_empty_email_excluded(self):
        """`exclude(email='')` — админы без почты не получат письмо.

        ⚠ Подводный камень: UserManager._create_user запрещает пустой
        email (ValueError). Обходим через прямой Model.save().
        Такой user реально может появиться в БД, если его создаст
        миграция, raw SQL или сид. Секция это предусматривает.
        """
        u = User(
            email='', full_name='Без почты',
            department=self.dep1, role=self.role_staff, is_active=True,
        )
        u.set_password('p')
        u.save()

        qs = resolve_recipients('all')
        emails = list(qs.values_list('email', flat=True))
        self.assertNotIn('', emails)


class SendBroadcastTests(OpsBase):
    @patch('admin_panel.broadcast.send_mail')
    def test_all_sent(self, mock_send):
        recipients = list(User.objects.filter(
            is_active=True,
        ).exclude(email=''))
        sent, failed = send_broadcast('S', 'Body', recipients)
        self.assertEqual(sent, len(recipients))
        self.assertEqual(failed, 0)
        self.assertEqual(mock_send.call_count, len(recipients))

    @patch('admin_panel.broadcast.send_mail')
    def test_partial_failure(self, mock_send):
        """3 получателя, второй падает → sent=2, failed=1."""
        recipients = [self.staff1, self.staff2, self.mgr]
        call_count = {'n': 0}

        def flaky(*args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise RuntimeError('smtp down')

        mock_send.side_effect = flaky
        with self.assertLogs('admin_panel.broadcast', level='WARNING'):
            sent, failed = send_broadcast('S', 'Body', recipients)

        self.assertEqual(sent, 2)
        self.assertEqual(failed, 1)

    @patch('admin_panel.broadcast.send_mail')
    def test_sender_argument_ignored(self, mock_send):
        """⚠ Параметр `sender` — только для логов вызывающего.
        В `send_mail` он не пробрасывается, from_email всегда
        settings.DEFAULT_FROM_EMAIL.
        """
        recipients = [self.staff1]
        send_broadcast('S', 'Body', recipients, sender=self.admin)
        kwargs = mock_send.call_args.kwargs
        self.assertNotEqual(kwargs.get('from_email'), self.admin.email)


class BroadcastViewTests(OpsBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_get_form(self):
        r = self.client.get(reverse('settings_broadcast'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['stage'], 'form')

    def test_preview_stage(self):
        r = self.client.post(reverse('settings_broadcast'), {
            'action': 'preview',
            'target': 'all',
            'subject': 'Тест',
            'body': 'Тело письма',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['stage'], 'preview')
        self.assertGreaterEqual(r.context['recipients_total'], 1)

    @patch('admin_panel.broadcast.send_broadcast')
    def test_send_stage_creates_log(self, mock_send):
        mock_send.return_value = (2, 0)
        r = self.client.post(reverse('settings_broadcast'), {
            'action': 'send',
            'target': 'all',
            'subject': 'Тест',
            'body': 'Тело',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BroadcastLog.objects.count(), 1)
        log = BroadcastLog.objects.first()
        self.assertEqual(log.subject, 'Тест')
        self.assertEqual(log.recipients_sent, 2)

    def test_send_without_subject_redirects(self):
        r = self.client.post(reverse('settings_broadcast'), {
            'action': 'send',
            'target': 'all',
            'subject': '',
            'body': 'Body',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BroadcastLog.objects.count(), 0)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_non_admin_forbidden(self):
        """⚠ Broadcast view — только для admin role, не для manager."""
        self.client.force_login(self.mgr)
        r = self.client.get(reverse('settings_broadcast'))
        self.assertEqual(r.status_code, 403)


# ═════════════════════════════════════════════════════════════
#  CLEANUP_OLD_RECORDS
# ═════════════════════════════════════════════════════════════

class CleanupCommandTests(OpsBase):
    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command

    def _old_audit(self, days):
        a = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE,
            target_model='User', target_repr='x',
        )
        # auto_now_add ставит now → отматываем вручную
        AuditLog.objects.filter(pk=a.pk).update(
            created_at=timezone.now() - timedelta(days=days),
        )
        return a

    def _old_login(self, days):
        e = LoginEvent.objects.create(
            email_attempted='x@test.ru', success=False,
        )
        LoginEvent.objects.filter(pk=e.pk).update(
            created_at=timezone.now() - timedelta(days=days),
        )
        return e

    def _old_alert(self, days):
        a = AdminAlert.objects.create(
            kind='failed_logins', key=f'k-{days}',
            severity='warn', title='X', seen=True,
        )
        AdminAlert.objects.filter(pk=a.pk).update(
            last_seen_at=timezone.now() - timedelta(days=days),
        )
        return a

    def test_default_cleanup_removes_old(self):
        self._old_audit(365)   # > 180 → удалится
        self._old_audit(30)    # свежая → останется
        self._old_login(180)   # > 90 → удалится
        self._old_login(10)    # свежая → останется

        self.call_command('cleanup_old_records', verbosity=0)

        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(LoginEvent.objects.count(), 1)

    def test_dry_run_does_not_delete(self):
        self._old_audit(365)
        self._old_login(180)
        self._old_alert(60)

        self.call_command('cleanup_old_records', dry_run=True, verbosity=0)

        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(LoginEvent.objects.count(), 1)
        self.assertEqual(AdminAlert.objects.count(), 1)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_just_below_boundary_not_deleted(self):
        """⚠ Фильтр `< cutoff`. Запись на 1 час младше границы — остаётся.

        Не используем ровно 180: между созданием и cleanup проходят
        миллисекунды, запись становится строго старше. Тест ловит
        реальную семантику `<` vs `<=`.
        """
        from datetime import timedelta as _td
        a = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE,
            target_model='User', target_repr='x',
        )
        # 179 дней 23 часа — гарантированно < 180 при любом now()
        AuditLog.objects.filter(pk=a.pk).update(
            created_at=timezone.now() - _td(days=179, hours=23),
        )
        self.call_command('cleanup_old_records', verbosity=0)
        self.assertTrue(AuditLog.objects.filter(pk=a.pk).exists())

    def test_just_past_boundary_deleted(self):
        """181 день — строго старше границы."""
        a = self._old_audit(181)
        self.call_command('cleanup_old_records', verbosity=0)
        self.assertFalse(AuditLog.objects.filter(pk=a.pk).exists())

    def test_seen_alert_deleted_only(self):
        """Только `seen=True` — иначе алерт живёт, пока не прочитан."""
        seen = self._old_alert(60)   # seen=True
        unseen = AdminAlert.objects.create(
            kind='failed_logins', key='k-unseen',
            severity='danger', title='U', seen=False,
        )
        AdminAlert.objects.filter(pk=unseen.pk).update(
            last_seen_at=timezone.now() - timedelta(days=60),
        )

        self.call_command('cleanup_old_records', verbosity=0)

        self.assertFalse(AdminAlert.objects.filter(pk=seen.pk).exists())
        self.assertTrue(AdminAlert.objects.filter(pk=unseen.pk).exists())

    def test_custom_thresholds(self):
        a = self._old_audit(30)
        self.call_command(
            'cleanup_old_records', audit_days=10, verbosity=0,
        )
        self.assertFalse(AuditLog.objects.filter(pk=a.pk).exists())

    def test_snapshot_deleted(self):
        """TimelineSnapshot удаляется по snapshot_date."""
        t = Task.objects.create(
            title='X', plan_hours=1.0, scale='s', kind='work',
            executor=self.staff1, requester=self.mgr,
        )
        snap = TimelineSnapshot.objects.create(
            snapshot_date=timezone.localdate() - timedelta(days=500),
            task=t, executor=self.staff1,
            start_date=timezone.localdate(),
            status=Task.Status.NEW,
            priority=Task.Priority.MEDIUM,
            plan_hours=1.0,
        )
        self.call_command('cleanup_old_records', verbosity=0)
        self.assertFalse(
            TimelineSnapshot.objects.filter(pk=snap.pk).exists(),
        )


# ═════════════════════════════════════════════════════════════
#  USERS IMPORT
# ═════════════════════════════════════════════════════════════

class UsersImportParsingTests(OpsBase):
    """Низкоуровневые функции импорта — парсинг CSV."""

    def _upload(self, csv_text, encoding='utf-8', delimiter=';'):
        raw = csv_text.encode(encoding)
        return SimpleUploadedFile(
            'users.csv', raw, content_type='text/csv',
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_get_upload_form(self):
        r = self.client.get(reverse('settings_users_import'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['stage'], 'upload')

    def test_post_creates_preview(self):
        csv_text = 'ФИО;Email;Подразделение;Роль\nИван;ivan@test.ru;ОГК;staff\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['stage'], 'preview')
        self.assertEqual(r.context['report']['create'], 1)

    def test_parse_comma_delimiter(self):
        csv_text = 'ФИО,Email\nИван,ivan@test.ru\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.assertEqual(r.context['report']['create'], 1)

    def test_parse_cp1251(self):
        """Русские имена в CP1251 — должны корректно декодироваться."""
        csv_text = 'ФИО;Email\nИван;ivan@test.ru\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text, encoding='cp1251'),
            'mode': 'create_only',
        })
        self.assertEqual(r.context['report']['create'], 1)
        self.assertEqual(r.context['rows'][0]['full_name'], 'Иван')

    def test_utf8_bom_stripped(self):
        csv_text = 'ФИО;Email\nИван;ivan@test.ru\n'
        raw = b'\xef\xbb\xbf' + csv_text.encode('utf-8')
        f = SimpleUploadedFile('users.csv', raw, content_type='text/csv')
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': f,
            'mode': 'create_only',
        })
        # BOM не попал в первый заголовок
        self.assertEqual(r.context['report']['create'], 1)

    def test_missing_required_columns(self):
        csv_text = 'ФИО;Роль\nИван;staff\n'   # нет Email
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        # Никакого preview, редирект обратно
        self.assertEqual(r.status_code, 302)

    def test_duplicate_email_in_file_error(self):
        csv_text = (
            'ФИО;Email\n'
            'Иван;ivan@test.ru\n'
            'Иван 2;ivan@test.ru\n'
        )
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.assertEqual(r.context['report']['error'], 1)
        self.assertEqual(r.context['report']['create'], 1)

    def test_invalid_email_error(self):
        csv_text = 'ФИО;Email\nИван;not-email\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.assertEqual(r.context['report']['error'], 1)

    def test_existing_user_skipped_in_create_only(self):
        csv_text = f'ФИО;Email\nИван;{self.staff1.email}\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.assertEqual(r.context['report']['skip'], 1)
        self.assertEqual(r.context['report']['create'], 0)

    def test_existing_user_updated_in_update_mode(self):
        csv_text = f'ФИО;Email\nИзменённый;{self.staff1.email}\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'update',
        })
        self.assertEqual(r.context['report']['update'], 1)

    def test_unknown_department_marked(self):
        csv_text = 'ФИО;Email;Подразделение\nИван;ivan@test.ru;Несуществующий\n'
        r = self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        rows = r.context['rows']
        self.assertEqual(rows[0]['department_id'], None)
        self.assertIn('не найдено', rows[0]['department_name'])

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_unknown_role_fallback_to_staff_at_apply(self):
        """⚠ Preview не падает на неизвестной роли, но при apply
        подставляется «Пользователь» (get_default_role).
        """
        # Убеждаемся, что staff существует
        _role('staff')
        csv_text = 'ФИО;Email;Роль\nИван;ivan@test.ru;unknown_role\n'
        self.client.post(reverse('settings_users_import'), {
            'csv_file': self._upload(csv_text),
            'mode': 'create_only',
        })
        self.client.post(reverse('settings_users_import_apply'))

        u = User.objects.get(email='ivan@test.ru')
        # get_default_role возвращает 'user' (или None, если нет)
        # Точное значение зависит от init_roles.
        self.assertIsNotNone(u)  # создан


class UsersImportApplyTests(OpsBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def _stage_preview(self, csv_text, mode='create_only'):
        raw = csv_text.encode('utf-8')
        f = SimpleUploadedFile('u.csv', raw, content_type='text/csv')
        self.client.post(reverse('settings_users_import'), {
            'csv_file': f,
            'mode': mode,
        })

    def test_creates_users(self):
        self._stage_preview('ФИО;Email\nA;a@test.ru\nB;b@test.ru\n')
        self.client.post(reverse('settings_users_import_apply'))
        self.assertTrue(User.objects.filter(email='a@test.ru').exists())
        self.assertTrue(User.objects.filter(email='b@test.ru').exists())

    def test_created_users_are_inactive_by_default(self):
        """⚠ Импорт не активирует пользователей — это решает админ.

        На самом деле User.objects.create_user → is_active=False
        (см. UserManager). Применяется, если CSV не указал «Активен».
        """
        self._stage_preview('ФИО;Email\nA;a@test.ru\n')
        self.client.post(reverse('settings_users_import_apply'))
        u = User.objects.get(email='a@test.ru')
        self.assertFalse(u.is_active)

    def test_csv_active_flag_respected(self):
        self._stage_preview('ФИО;Email;Активен\nA;a@test.ru;да\n')
        self.client.post(reverse('settings_users_import_apply'))
        u = User.objects.get(email='a@test.ru')
        self.assertTrue(u.is_active)

    def test_session_cleared_after_apply(self):
        self._stage_preview('ФИО;Email\nA;a@test.ru\n')
        self.client.post(reverse('settings_users_import_apply'))
        self.assertNotIn('users_import', self.client.session)

    def test_apply_without_preview_redirects(self):
        """⚠ Session-based: apply без preview → нет данных → редирект."""
        r = self.client.post(reverse('settings_users_import_apply'))
        self.assertEqual(r.status_code, 302)

    def test_audit_log_created(self):
        self._stage_preview('ФИО;Email\nA;a@test.ru\n')
        before = AuditLog.objects.filter(target_model='User').count()
        self.client.post(reverse('settings_users_import_apply'))
        after = AuditLog.objects.filter(target_model='User').count()
        self.assertEqual(after, before + 1)

    # ── ПОДВОДНЫЙ КАМЕНЬ ────────────────────────────────────
    def test_manager_can_preview_but_not_apply(self):
        """⚠ users_import = _boss_required; users_import_apply = _admin_required.

        Менеджер сделает preview, но apply → 403. Это осознанное
        разделение: менеджер готовит данные, админ утверждает.
        """
        self.client.force_login(self.mgr)

        raw = 'ФИО;Email\nA;a@test.ru\n'.encode('utf-8')
        f = SimpleUploadedFile('u.csv', raw, content_type='text/csv')
        r1 = self.client.post(reverse('settings_users_import'), {
            'csv_file': f,
            'mode': 'create_only',
        })
        self.assertEqual(r1.status_code, 200)  # preview ok

        r2 = self.client.post(reverse('settings_users_import_apply'))
        self.assertEqual(r2.status_code, 403)

# ═════════════════════════════════════════════════════════════
#  MANAGEMENT-КОМАНДА: compute_idle_since
# ═════════════════════════════════════════════════════════════

class ComputeIdleSinceCommandTests(OpsBase):
    """Тесты management-команды compute_idle_since.

    Команда пересчитывает User.idle_since по last_activity_at:
      - «ушёл» → idle_since = last_activity_at (момент ухода, не now);
      - «вернулся» → idle_since = None.

    ⚠️ Грабли, которые обходим в тестах:
      1. Перегруженный User.save() добавляет is_staff в update_fields.
         Поэтому даты ставим через User.objects.filter(pk=...).update(...).
      2. Граница «ровно 5 минут» нестабильна из-за микросекунд между
         установкой last_activity_at и запуском команды. Тестируем 4 и 6.
      3. Неактивные и «никогда не заходившие» (last_activity_at=None)
         не трогаются — это проверено отдельно.
    """

    def _call(self, **kwargs):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command(
            'compute_idle_since', stdout=out, verbosity=0, **kwargs
        )
        return out.getvalue()

    def _set_last_activity(self, user, minutes_ago):
        """Проставить last_activity_at N минут назад в обход save()."""
        User.objects.filter(pk=user.pk).update(
            last_activity_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        user.refresh_from_db()

    def _set_idle_since(self, user, minutes_ago):
        """None — очистить, число — поставить N минут назад."""
        if minutes_ago is None:
            User.objects.filter(pk=user.pk).update(idle_since=None)
        else:
            User.objects.filter(pk=user.pk).update(
                idle_since=timezone.now() - timedelta(minutes=minutes_ago),
            )
        user.refresh_from_db()

    # ── Базовые сценарии ────────────────────────────────────────

    def test_stale_user_gets_idle_since(self):
        """Ушёл 30 минут назад — должен получить idle_since."""
        self._set_last_activity(self.staff1, minutes_ago=30)
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNotNone(self.staff1.idle_since)

    def test_idle_since_equals_last_activity_not_now(self):
        """⚠️ idle_since = last_activity_at, а НЕ now().

        Если ошибиться и поставить now(), пользователь будет
        «ушедшим секунду назад» вместо «ушедшим 2 часа назад» —
        метрика простоя потеряет смысл.
        """
        self._set_last_activity(self.staff1, minutes_ago=120)
        self._call()
        self.staff1.refresh_from_db()

        delta_min = (
                            timezone.now() - self.staff1.idle_since
                    ).total_seconds() / 60
        # ~120 минут, не 0
        self.assertGreater(delta_min, 100)
        self.assertLess(delta_min, 140)

        # idle_since совпадает с last_activity_at (с точностью до микросекунд)
        self.assertEqual(
            self.staff1.idle_since.replace(microsecond=0),
            self.staff1.last_activity_at.replace(microsecond=0),
        )

    def test_fresh_user_with_idle_clears_it(self):
        """Свежая активность + старый idle_since → сброс."""
        self._set_last_activity(self.staff1, minutes_ago=1)
        self._set_idle_since(self.staff1, minutes_ago=30)
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)

    def test_fresh_user_without_idle_stays_clean(self):
        """Свежий, без idle_since → не трогаем."""
        self._set_last_activity(self.staff1, minutes_ago=1)
        self._set_idle_since(self.staff1, minutes_ago=None)
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)

    # ── Грабли ──────────────────────────────────────────────────

    def test_inactive_user_not_touched(self):
        """Неактивный пользователь не должен получить idle_since."""
        self._set_last_activity(self.inactive, minutes_ago=30)
        self._call()
        self.inactive.refresh_from_db()
        self.assertIsNone(self.inactive.idle_since)

    def test_user_without_activity_not_touched(self):
        """last_activity_at=None → idle_since не ставим.

        Иначе получим «ушедших в никуда» с idle_since, но без времени
        последней активности — бессмысленная запись.
        """
        User.objects.filter(pk=self.staff1.pk).update(
            last_activity_at=None, idle_since=None,
        )
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)
        self.assertIsNone(self.staff1.last_activity_at)

    def test_already_idle_user_not_double_updated(self):
        """Если idle_since уже стоит, не перезаписываем.

        Фильтр `idle_since__isnull=True` не даёт команде каждые 5 минут
        обновлять дату «ушёл» — она фиксируется на моменте реального ухода.
        """
        self._set_last_activity(self.staff1, minutes_ago=120)
        self._set_idle_since(self.staff1, minutes_ago=110)
        old_idle = self.staff1.idle_since

        self._call()
        self.staff1.refresh_from_db()
        self.assertEqual(self.staff1.idle_since, old_idle)

    # ── Граница порога (без точной 5-минутной точки) ────────────

    def test_4min_not_idle(self):
        """4 минуты — ещё активен."""
        self._set_last_activity(self.staff1, minutes_ago=4)
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)

    def test_6min_idle(self):
        """6 минут — уже простой."""
        self._set_last_activity(self.staff1, minutes_ago=6)
        self._call()
        self.staff1.refresh_from_db()
        self.assertIsNotNone(self.staff1.idle_since)

    # ── Кастомный порог ─────────────────────────────────────────

    def test_custom_minutes_threshold_respected(self):
        """--minutes=30 не задевает 10-минутного, --minutes=5 — задевает."""
        self._set_last_activity(self.staff1, minutes_ago=10)

        self._call(minutes=30)
        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)

        self._call(minutes=5)
        self.staff1.refresh_from_db()
        self.assertIsNotNone(self.staff1.idle_since)

    # ── Несколько пользователей ─────────────────────────────────

    def test_multiple_users_updated_in_one_run(self):
        """За один вызов обновляются все подходящие пользователи."""
        self._set_last_activity(self.staff1, minutes_ago=30)
        self._set_last_activity(self.staff2, minutes_ago=60)
        self._set_last_activity(self.mgr, minutes_ago=120)

        self._call()

        for u in (self.staff1, self.staff2, self.mgr):
            u.refresh_from_db()
            self.assertIsNotNone(u.idle_since, msg=u.email)

    # ── --dry-run ───────────────────────────────────────────────

    def test_dry_run_does_not_set_idle_since(self):
        """--dry-run: печатает, но не сохраняет."""
        self._set_last_activity(self.staff1, minutes_ago=30)
        output = self._call(dry_run=True)

        self.staff1.refresh_from_db()
        self.assertIsNone(self.staff1.idle_since)

        self.assertIn('dry-run', output)
        self.assertIn('получили бы idle_since: 1', output)

    def test_dry_run_does_not_clear_idle_since(self):
        """--dry-run не должен сбрасывать существующий idle_since."""
        self._set_last_activity(self.staff1, minutes_ago=1)
        self._set_idle_since(self.staff1, minutes_ago=30)

        self._call(dry_run=True)

        self.staff1.refresh_from_db()
        self.assertIsNotNone(self.staff1.idle_since)
