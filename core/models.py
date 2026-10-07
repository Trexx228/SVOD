from django.core.validators import MinValueValidator
from django.db import models


class TaskType(models.Model):
    name = models.CharField('Название', max_length=200, unique=True)
    plan_hours = models.FloatField(
        'План, ч',
        validators=[MinValueValidator(0, 'План не может быть отрицательным.')],
    )
    due_days = models.PositiveIntegerField(
        'Срок, дней',
        default=1,
        validators=[MinValueValidator(1, 'Срок должен быть не меньше 1 дня.')],
    )
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Типовая задача'
        verbose_name_plural = 'Типовые задачи'
        ordering = ['name']

    def __str__(self):
        return self.name


class Norm(models.Model):
    hours_per_day = models.FloatField(
        'Часов в день',
        default=8.0,
        validators=[MinValueValidator(0, 'Норма часов не может быть отрицательной.')],
    )
    note = models.CharField('Примечание', max_length=200, blank=True)

    class Meta:
        verbose_name = 'Норма часов'
        verbose_name_plural = 'Нормы часов'

    def __str__(self):
        return f'{self.hours_per_day} ч/день'


class Holiday(models.Model):
    """Производственный календарь РФ: праздники и перенесённые дни.

    date        — конкретная дата
    name        — название праздника / причина переноса
    is_working  — True, если это рабочий день (перенос с выходного)
                  False (по умолчанию) — нерабочий праздничный день
    """
    date = models.DateField('Дата', unique=True, db_index=True)
    name = models.CharField('Название', max_length=100, blank=True)
    is_working = models.BooleanField(
        'Рабочий день',
        default=False,
        help_text='True — перенесённый рабочий день (выходной стал рабочим).',
    )

    class Meta:
        verbose_name = 'Производственный день'
        verbose_name_plural = 'Производственный календарь'
        ordering = ['date']

    def __str__(self):
        return f'{self.date:%d.%m.%Y} · {self.name}'
