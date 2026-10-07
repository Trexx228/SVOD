from django.conf import settings
from django.db import models


# ═════════════════════════════════════════════════════════════
#  АУДИТ
# ═════════════════════════════════════════════════════════════

class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = 'create', 'Создание'
        UPDATE = 'update', 'Изменение'
        DELETE = 'delete', 'Удаление'
        TOGGLE = 'toggle', 'Вкл/выкл'
        LOGIN_AS = 'login_as', 'Вход от имени'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs',
        verbose_name='Кто',
    )
    action = models.CharField('Действие', max_length=15, choices=Action.choices)
    target_model = models.CharField('Модель', max_length=60)
    target_id = models.CharField('ID объекта', max_length=64, blank=True)
    target_repr = models.CharField('Объект', max_length=300, blank=True)
    changes = models.JSONField('Изменения', null=True, blank=True)
    ip = models.GenericIPAddressField('IP', null=True, blank=True)
    created_at = models.DateTimeField('Когда', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Запись аудита'
        verbose_name_plural = 'Журнал аудита'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['target_model', 'target_id']),
        ]

    def __str__(self):
        return (
            f'{self.created_at:%Y-%m-%d %H:%M} · {self.actor} · '
            f'{self.get_action_display()} {self.target_repr}'
        )


# ═════════════════════════════════════════════════════════════
#  ЛОГ ВХОДОВ
# ═════════════════════════════════════════════════════════════

class LoginEvent(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='login_events',
        verbose_name='Пользователь',
    )
    email_attempted = models.CharField('Email при попытке', max_length=254, blank=True)
    success = models.BooleanField('Успех', default=False)
    ip = models.GenericIPAddressField('IP', null=True, blank=True)
    user_agent = models.CharField('User-Agent', max_length=300, blank=True)
    created_at = models.DateTimeField('Когда', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Событие входа'
        verbose_name_plural = 'Журнал входов'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['success', 'created_at']),
        ]

    def __str__(self):
        state = 'ок' if self.success else 'отказ'
        who = self.user.full_name if self.user else (self.email_attempted or '—')
        return f'{self.created_at:%Y-%m-%d %H:%M} · {who} · {state}'


# ═════════════════════════════════════════════════════════════
#  УВЕДОМЛЕНИЯ АДМИНУ
# ═════════════════════════════════════════════════════════════

class AdminAlert(models.Model):
    class Kind(models.TextChoices):
        FAILED_LOGINS = 'failed_logins', 'Серия отказов входа'
        SINGLE_ADMIN = 'single_admin', 'Один администратор'
        NO_NORM = 'no_norm', 'Нет нормы часов'

    class Severity(models.TextChoices):
        INFO = 'info', 'Информация'
        WARN = 'warn', 'Внимание'
        DANGER = 'danger', 'Тревога'

    kind = models.CharField('Тип', max_length=30, choices=Kind.choices)
    key = models.CharField('Ключ дедупликации', max_length=200, unique=True)
    severity = models.CharField(
        'Серьёзность', max_length=10, choices=Severity.choices,
        default=Severity.WARN,
    )
    title = models.CharField('Заголовок', max_length=200)
    message = models.CharField('Сообщение', max_length=500, blank=True)
    url = models.CharField('Ссылка', max_length=300, blank=True)
    count = models.PositiveIntegerField('Срабатываний', default=1)
    seen = models.BooleanField('Прочитано', default=False, db_index=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    last_seen_at = models.DateTimeField('Последнее срабатывание', auto_now=True)

    class Meta:
        verbose_name = 'Уведомление администратора'
        verbose_name_plural = 'Уведомления администратора'
        ordering = ['-last_seen_at']

    def __str__(self):
        return f'[{self.severity}] {self.title}'


class AdminAlertSettings(models.Model):
    """Singleton-настройки уведомлений админа."""
    failed_logins_threshold = models.PositiveIntegerField(
        'Порог отказов входа', default=5,
        help_text='Сколько неудачных попыток за окно считать тревогой.',
    )
    failed_logins_window_min = models.PositiveIntegerField(
        'Окно, мин', default=15,
        help_text='За сколько минут считать отказы.',
    )
    single_admin_alert = models.BooleanField(
        'Оповещать про одного админа', default=True,
    )
    no_norm_alert = models.BooleanField(
        'Оповещать про отсутствие нормы', default=True,
    )
    notify_email = models.EmailField(
        'Email для уведомлений', blank=True,
        help_text='Если задан — при новых алертах будет отправляться письмо.',
    )
    notify_on_danger_only = models.BooleanField(
        'Только тревожные алерты', default=False,
        help_text='Если включено — письма только для severity=danger.',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Настройки уведомлений'
        verbose_name_plural = 'Настройки уведомлений'

    def __str__(self):
        return 'Настройки уведомлений админа'

    @classmethod
    def get_solo(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class BroadcastLog(models.Model):
    class Target(models.TextChoices):
        ALL = 'all', 'Все пользователи'
        DEPARTMENT = 'department', 'Подразделение'
        ROLE = 'role', 'Роль'
        ADMINS = 'admins', 'Только админы'

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='broadcasts',
        verbose_name='Отправитель',
    )
    target = models.CharField('Кому', max_length=20, choices=Target.choices)
    target_label = models.CharField('Сегмент', max_length=200, blank=True)
    subject = models.CharField('Тема', max_length=200)
    body = models.TextField('Текст')
    recipients_total = models.PositiveIntegerField('Всего получателей', default=0)
    recipients_sent = models.PositiveIntegerField('Отправлено', default=0)
    recipients_failed = models.PositiveIntegerField('Ошибок', default=0)
    created_at = models.DateTimeField('Когда', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Рассылка'
        verbose_name_plural = 'Рассылки'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} · {self.subject}'
