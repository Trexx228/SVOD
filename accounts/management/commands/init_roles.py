from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Role


# (code, name, can_manage, can_admin, can_plant)
DEFAULTS = [
    ('user',    'Пользователь',               False, False, False),
    ('staff',   'Инженер-конструктор',        False, False, False),
    ('manager', 'Руководитель подразделения', True,  False, False),
    ('admin',   'Администратор',              True,  True,  False),
]


class Command(BaseCommand):
    help = 'Создать/обновить базовые роли и распределить пользователей'

    def handle(self, *args, **options):
        roles = {}

        with transaction.atomic():
            for code, name, manage, admin_flag, plant in DEFAULTS:
                role, created = Role.objects.get_or_create(
                    code=code,
                    defaults={
                        'name': name,
                        'can_manage': manage,
                        'can_admin': admin_flag,
                        'can_plant': plant,
                    },
                )
                if not created:
                    role.name = name
                    role.can_manage = manage
                    role.can_admin = admin_flag
                    role.can_plant = plant
                    role.save(update_fields=['name', 'can_manage', 'can_admin', 'can_plant'])
                roles[code] = role

            User = get_user_model()

            # Юзеры без роли — назначаем «Пользователь»
            User.objects.filter(role__isnull=True, is_superuser=False).update(role=roles['user'])

            # Суперюзеры без роли — «Администратор»
            User.objects.filter(is_superuser=True).exclude(role=roles['admin']).update(role=roles['admin'])

        self.stdout.write(self.style.SUCCESS(
            'Роли созданы/обновлены. Все пользователи без роли → «Пользователь».'
        ))
