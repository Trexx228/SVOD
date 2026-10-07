from datetime import date

from django.db import migrations


DEFAULT_NAME = 'Стандартный 5/2'
DEFAULT_PATTERN = [1, 1, 1, 1, 1, 0, 0]   # пн–пт, сб–вс
DEFAULT_HOURS = 8.0
# 2024-01-01 — понедельник. Любой понедельник сгодится как якорь.
DEFAULT_ANCHOR = date(2024, 1, 1)


def create_default_schedule(apps, schema_editor):
    """Создаёт seed-график 5/2×8 и привязывает ко всем отделам,
    у которых график не задан. Идемпотентна: повторный запуск
    не создаёт дубликатов и не перезатирает уже привязанные."""
    WorkSchedule = apps.get_model('accounts', 'WorkSchedule')
    Department = apps.get_model('accounts', 'Department')

    ws, _ = WorkSchedule.objects.get_or_create(
        name=DEFAULT_NAME,
        defaults={
            'pattern': DEFAULT_PATTERN,
            'hours_per_shift': DEFAULT_HOURS,
            'anchor_date': DEFAULT_ANCHOR,
            'is_active': True,
            'description': 'Установлен автоматически при миграции. Пятидневка, 8 ч.',
        },
    )

    Department.objects.filter(default_schedule__isnull=True).update(
        default_schedule=ws,
    )


def drop_default_schedule(apps, schema_editor):
    """Откат: отвязываем отделы от seed-графика и удаляем сам график.

    Пользовательские правки не трогаем — если кто-то переназначил
    отдел на другой график, откат не должен ломать его выбор.
    """
    WorkSchedule = apps.get_model('accounts', 'WorkSchedule')
    Department = apps.get_model('accounts', 'Department')

    ws = WorkSchedule.objects.filter(name=DEFAULT_NAME).first()
    if not ws:
        return

    Department.objects.filter(default_schedule=ws).update(default_schedule=None)
    ws.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_workschedule'),
    ]

    operations = [
        migrations.RunPython(create_default_schedule, drop_default_schedule),
    ]
