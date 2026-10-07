from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Achievement(models.Model):
    code = models.SlugField('Код', unique=True)
    title = models.CharField('Название', max_length=100)
    icon = models.CharField('Иконка', max_length=10, blank=True)
    description = models.TextField('Описание', blank=True)

    class Meta:
        verbose_name = 'Достижение'
        verbose_name_plural = 'Достижения'

    def __str__(self):
        return self.title


class UserAchievement(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='achievements',
        verbose_name='Сотрудник',
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='awards',
        verbose_name='Достижение',
    )
    awarded_at = models.DateTimeField('Выдано', auto_now_add=True)

    class Meta:
        verbose_name = 'Выданное достижение'
        verbose_name_plural = 'Выданные достижения'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'achievement'],
                name='unique_user_achievement',
            ),
        ]

    def __str__(self):
        return f'{self.user} — {self.achievement}'


class Kudos(models.Model):
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='kudos_sent',
        verbose_name='Кто поблагодарил',
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='kudos_received',
        verbose_name='Кому',
    )
    text = models.CharField('Текст', max_length=200)
    created_at = models.DateTimeField('Когда', auto_now_add=True)

    class Meta:
        verbose_name = 'Благодарность'
        verbose_name_plural = 'Благодарности'
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_user=models.F('to_user')),
                name='kudos_not_self',
            ),
        ]
        indexes = [
            models.Index(
                fields=['to_user', 'created_at'],
                name='kudos_to_user_created_idx',
            ),
        ]

    def clean(self):
        if self.from_user_id and self.to_user_id and self.from_user_id == self.to_user_id:
            raise ValidationError('Нельзя отправить благодарность самому себе.')

    def __str__(self):
        return f'{self.from_user} → {self.to_user}'


class Streak(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='streak',
        verbose_name='Сотрудник',
    )
    current_days = models.PositiveIntegerField('Текущая серия, дней', default=0)
    best_days = models.PositiveIntegerField('Рекордная серия, дней', default=0)
    last_ok_date = models.DateField('Последний день без просрочки', null=True, blank=True)

    class Meta:
        verbose_name = 'Серия без просрочек'
        verbose_name_plural = 'Серии без просрочек'

    def __str__(self):
        return f'{self.user}: {self.current_days} дн.'
