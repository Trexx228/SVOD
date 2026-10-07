# Generated manually — Role.is_developer.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0013_user_vacation_dates'),
    ]

    operations = [
        migrations.AddField(
            model_name='role',
            name='is_developer',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Доступ к техническому разделу: логи, состояние '
                    'планировщика, список моделей, дампы. Не даёт прав '
                    'руководителя или админа — только инструменты '
                    'диагностики.'
                ),
                verbose_name='Разработчик',
            ),
        ),
    ]
