# Generated manually for use_calendar flag on WorkSchedule.

from django.db import migrations, models


def set_use_calendar_by_pattern(apps, schema_editor):
    """Существующим графикам выставить флаг по паттерну.

    Правило: паттерн [1,1,1,1,1,0,0] (пн–пт) → use_calendar=True,
    любые другие (2/2, 7/7, 3/3, seed с другим anchor) → False.
    Обоснование: сменные режимы существуют именно потому, что
    «пн–пт и 1 января — выходной» к ним не применяется.
    """
    WS = apps.get_model('accounts', 'WorkSchedule')
    STANDARD_5_2 = [1, 1, 1, 1, 1, 0, 0]

    for ws in WS.objects.all():
        pattern = list(ws.pattern or [])
        ws.use_calendar = (pattern == STANDARD_5_2)
        ws.save(update_fields=['use_calendar'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_seed_default_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='workschedule',
            name='use_calendar',
            field=models.BooleanField(
                default=True,
                help_text=(
                    'Включено — праздники РФ (core.Holiday и гос. календарь) '
                    'переопределяют паттерн: праздник → выходной, перенос → рабочий. '
                    'Подходит для 5/2 и офисных сотрудников. '
                    'Выключите для сменных графиков (2/2, 7/7), где работа в праздник — '
                    'норма и оплачивается х2.'
                ),
                verbose_name='Учитывать производственный календарь',
            ),
        ),
        migrations.RunPython(set_use_calendar_by_pattern, migrations.RunPython.noop),
    ]
