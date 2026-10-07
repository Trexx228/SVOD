"""Автотесты comms: чаты, уведомления, поллинг, вложения."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, Role
from comms.models import Message, Notification, Thread

User = get_user_model()


def _role(code, **kw):
    r, _ = Role.objects.get_or_create(
        code=code, defaults={'name': code.capitalize(), **kw}
    )
    return r


class CommsBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='ОГК')
        cls.role = _role('staff')
        cls.u1 = User.objects.create_user(
            email='u1@eag.su', password='x', full_name='U1',
            is_active=True, department=cls.dept, role=cls.role,
        )
        cls.u2 = User.objects.create_user(
            email='u2@eag.su', password='x', full_name='U2',
            is_active=True, department=cls.dept, role=cls.role,
        )
        cls.u3 = User.objects.create_user(
            email='u3@eag.su', password='x', full_name='U3',
            is_active=True, department=cls.dept, role=cls.role,
        )


class PollTests(CommsBaseTestCase):

    def test_poll_requires_login(self):
        r = self.client.get(reverse('comms_poll'))
        self.assertEqual(r.status_code, 302)

    def test_poll_returns_json(self):
        self.client.force_login(self.u1)
        r = self.client.get(reverse('comms_poll'))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn('notes', data)
        self.assertIn('msgs', data)

    def test_poll_counts_unread(self):
        Notification.objects.create(recipient=self.u1, text='A')
        Notification.objects.create(recipient=self.u1, text='B')
        Notification.objects.create(recipient=self.u1, text='C', read=True)
        self.client.force_login(self.u1)
        data = self.client.get(reverse('comms_poll')).json()
        self.assertEqual(data['notes'], 2)


class NotificationTests(CommsBaseTestCase):

    def test_list_requires_login(self):
        r = self.client.get(reverse('notifications'))
        self.assertEqual(r.status_code, 302)

    def test_mark_all_read(self):
        Notification.objects.create(recipient=self.u1, text='X')
        Notification.objects.create(recipient=self.u1, text='Y')
        self.client.force_login(self.u1)
        self.client.post(reverse('notifications_read'))
        self.assertEqual(
            Notification.objects.filter(recipient=self.u1, read=False).count(), 0
        )

    def test_open_marks_read(self):
        n = Notification.objects.create(recipient=self.u1, text='Z', url='/')
        self.client.force_login(self.u1)
        self.client.get(reverse('notification_open', args=[n.pk]))
        n.refresh_from_db()
        self.assertTrue(n.read)

    def test_cannot_open_foreign(self):
        n = Notification.objects.create(recipient=self.u1, text='Z')
        self.client.force_login(self.u2)
        r = self.client.get(reverse('notification_open', args=[n.pk]))
        self.assertEqual(r.status_code, 404)


class ChatTests(CommsBaseTestCase):

    def test_dialogs_requires_login(self):
        r = self.client.get(reverse('dialogs'))
        self.assertEqual(r.status_code, 302)

    def test_dialogs_ok(self):
        self.client.force_login(self.u1)
        r = self.client.get(reverse('dialogs'))
        self.assertEqual(r.status_code, 200)

    def test_start_direct_creates_thread(self):
        self.client.force_login(self.u1)
        r = self.client.post(reverse('start_direct'), {'to': self.u2.pk})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            Thread.objects.filter(direct=True, participants=self.u1)
            .filter(participants=self.u2).exists()
        )

    def test_send_message(self):
        t = Thread.objects.create()
        t.participants.add(self.u1, self.u2)
        self.client.force_login(self.u1)
        self.client.post(
            reverse('thread_send', args=[t.pk]),
            {'text': 'Привет'},
        )
        self.assertEqual(Message.objects.filter(thread=t, text='Привет').count(), 1)

    def test_foreign_thread_forbidden(self):
        t = Thread.objects.create()
        t.participants.add(self.u1, self.u2)
        self.client.force_login(self.u3)
        r = self.client.get(reverse('thread_open', args=[t.pk]))
        self.assertEqual(r.status_code, 302)

    def test_thread_poll_json(self):
        t = Thread.objects.create()
        t.participants.add(self.u1, self.u2)
        Message.objects.create(thread=t, author=self.u2, text='A')
        Message.objects.create(thread=t, author=self.u2, text='B')
        self.client.force_login(self.u1)
        r = self.client.get(reverse('thread_poll', args=[t.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['messages']), 2)


class UploadTests(CommsBaseTestCase):

    def test_upload_requires_login(self):
        r = self.client.post(reverse('upload_attach'))
        self.assertEqual(r.status_code, 302)

    def test_upload_rejects_oversize(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        big = SimpleUploadedFile('x.txt', b'a' * (11 * 1024 * 1024))
        self.client.force_login(self.u1)
        r = self.client.post(reverse('upload_attach'), {'file': big})
        self.assertEqual(r.status_code, 400)

    def test_upload_rejects_bad_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = SimpleUploadedFile('x.exe', b'\x00')
        self.client.force_login(self.u1)
        r = self.client.post(reverse('upload_attach'), {'file': bad})
        self.assertEqual(r.status_code, 400)

    def test_upload_accepts_png(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # минимальный валидный PNG
        png = bytes.fromhex(
            '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
            '1f15c4890000000d49444154789c63000100000005000101a5f6e5c700'
            '0000000049454e44ae426082'
        )
        f = SimpleUploadedFile('t.png', png, content_type='image/png')
        self.client.force_login(self.u1)
        r = self.client.post(reverse('upload_attach'), {'file': f})
        self.assertEqual(r.status_code, 200)
        self.assertIn('id', r.json())
