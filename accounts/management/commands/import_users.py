import csv
from django.core.management.base import BaseCommand
from django.db import transaction
from accounts.models import Department, Role, User

class Command(BaseCommand):
    help = 'Массовое создание пользователей. CSV: email,full_name,department,role(код)'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--password', default='Eag@2026', help='Временный пароль для всех')

    def handle(self, *args, **options):
        # Кэширование для избежания N+1 запросов
        departments_cache = {d.name: d for d in Department.objects.all()}
        roles_cache = {r.code: r for r in Role.objects.all()}
        default_role = roles_cache.get('staff')

        with open(options['csv_path'], encoding='utf-8-sig') as f:
            # Обертываем в транзакцию для атомарности и ускорения записи
            with transaction.atomic():
                for row in csv.DictReader(f):
                    email = (row.get('email') or '').strip().lower()
                    full_name = (row.get('full_name') or '').strip()
                    dep_name = (row.get('department') or '').strip()
                    code = (row.get('role') or 'staff').strip()

                    if not email:
                        self.stdout.write(self.style.WARNING(f"Пропущена строка без email: {row}"))
                        continue

                    # Получаем или создаем Department (с кэшированием)
                    if dep_name not in departments_cache:
                        dep, _ = Department.objects.get_or_create(name=dep_name)
                        departments_cache[dep_name] = dep
                    dep = departments_cache[dep_name]

                    # Получаем Role из кэша
                    role = roles_cache.get(code) or default_role

                    user, created = User.objects.get_or_create(
                        email=email,
                        defaults={
                            'full_name': full_name,
                            'department': dep,
                            'department_hint': dep_name,
                            'role': role,
                            'is_active': True
                        }
                    )

                    if created:
                        user.set_password(options['password'])
                        user.save()

                    self.stdout.write(f"{'Создан' if created else 'Уже был'}: {user.email}")

        self.stdout.write(self.style.SUCCESS('Импорт завершён.'))
