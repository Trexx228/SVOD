import os

from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        INFO = 'info', 'Инфо'
        ACTION = 'action', 'Нужно действие'
        ARRIVAL = 'arrival', 'Прибытие / запуск'
        ALERT = 'alert', 'Затор / риск'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Получатель',
    )
    text = models.CharField('Текст', max_length=300)
    url = models.CharField('Ссылка', max_length=300, blank=True)
    kind = models.CharField(
        'Тип',
        max_length=10,
        choices=Kind.choices,
        default=Kind.INFO,
    )
    read = models.BooleanField('Прочитано', default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Уведомление'
        verbose_name_plural = 'Уведомления'
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['recipient', 'read'],
                name='notif_recip_read_idx',
            ),
        ]

    def __str__(self):
        return f'{self.recipient}: {self.text[:40]}'


class Thread(models.Model):
    title = models.CharField('Название', max_length=300, blank=True)
    task = models.ForeignKey(
        'tasks.Task',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='threads',
        verbose_name='Задача',
    )
    direct = models.BooleanField('Личный диалог', default=False)
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='threads',
        verbose_name='Участники',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Чат'
        verbose_name_plural = 'Чаты'

    def display_title(self, user=None):
        if self.task_id:
            return f'По задаче: {self.task.title}'

        if user:
            others = [
                p.full_name
                for p in self.participants.all()
                if p != user
            ]
            return ', '.join(others) or 'Диалог'

        return self.title or 'Чат'

    def __str__(self):
        return self.title or f'Чат #{self.pk}'


class Message(models.Model):
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name='messages',
        verbose_name='Чат',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='messages',
        verbose_name='Автор',
    )
    text = models.TextField('Сообщение', max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    read_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='read_messages',
        blank=True,
    )

    class Meta:
        verbose_name = 'Сообщение'
        verbose_name_plural = 'Сообщения'
        ordering = ['created_at']
        indexes = [
            models.Index(
                fields=['thread', 'created_at'],
                name='msg_thread_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.author}: {self.text[:40]}'


class Attachment(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Автор',
    )
    file = models.FileField('Файл', upload_to='attach/%Y/%m/')
    name = models.CharField('Имя', max_length=150)
    size = models.PositiveIntegerField('Размер, байт', default=0)
    image = models.BooleanField('Изображение', default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    task = models.ForeignKey(
        'tasks.Task',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Задача',
    )
    message = models.ForeignKey(
        'Message',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Сообщение',
    )
    progress = models.ForeignKey(
        'tasks.TaskProgress',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Запись процесса',
    )
    ticket = models.ForeignKey(
        'accounts.SupportTicket',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Обращение',
    )

    class Meta:
        verbose_name = 'Вложение'
        verbose_name_plural = 'Вложения'

    def save(self, *args, **kwargs):
        if self.file:
            try:
                self.size = self.file.size
            except OSError:
                pass

            if not self.name:
                self.name = os.path.basename(self.file.name)[:150]

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
