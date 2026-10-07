# Generated manually — vacation_from / vacation_to on User.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_workschedule_use_calendar'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='vacation_from',
            field=models.DateField(
                blank=True,
                null=True,
                help_text=(
                    'Первый день отпуска / больничного. Используется для '
                    'отображения статуса и для проверок при постановке задач.'
                ),
                verbose_name='Отпуск с',
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='vacation_to',
            field=models.DateField(
                blank=True,
                null=True,
                help_text=(
                    'Последний день отпуска. Если пусто — считается '
                    'бессрочным до ручного снятия.'
                ),
                verbose_name='Отпуск по',
            ),
        ),
    ]
