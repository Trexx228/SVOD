import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        'Восстанавливает справочники из JSON, созданного dump_snapshot. '
        'По умолчанию — только справочники (роли, отделы, типовые, нормы). '
        'Пользователи/задачи — по флагам. Аудит/логи — только по --include-logs.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--input', type=str, required=True,
            help='Путь к JSON-файлу.',
        )
        parser.add_argument(
            '--include-users', action='store_true',
            help='Импортировать пользователей (создаёт новых; существующих не трогает).',
        )
        parser.add_argument(
            '--include-logs', action='store_true',
            help='Импортировать аудит и события входов (только добавление).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы создано, но не сохранять.',
        )

    def handle(self, *args, **options):
        from accounts.models import Department, Role, User
        from core.models import TaskType, Norm

        in_path = Path(options['input'])
        if not in_path.is_file():
            raise CommandError(f'Файл не найден: {in_path}')

        with in_path.open('r', encoding='utf-8') as f:
            data = json.load(f)

        dry = options['dry_run']

        stats = {
            'roles_created': 0, 'roles_updated': 0,
            'depts_created': 0, 'depts_updated': 0,
            'tt_created': 0, 'tt_updated': 0,
            'norms_created': 0,
            'users_created': 0, 'users_skipped': 0,
        }

        @transaction.atomic
        def _run():
            # ── Роли ──
            for r in data.get('roles', []):
                obj = Role.objects.filter(code=r['code']).first()
                if obj:
                    obj.name = r['name']
                    obj.can_manage = r.get('can_manage', False)
                    obj.can_admin = r.get('can_admin', False)
                    obj.can_plant = r.get('can_plant', False)
                    obj.description = r.get('description', '')
                    if not dry:
                        obj.save()
                    stats['roles_updated'] += 1
                else:
                    if not dry:
                        Role.objects.create(
                            name=r['name'], code=r['code'],
                            can_manage=r.get('can_manage', False),
                            can_admin=r.get('can_admin', False),
                            can_plant=r.get('can_plant', False),
                            description=r.get('description', ''),
                        )
                    stats['roles_created'] += 1

            # ── Подразделения ──
            for d in data.get('departments', []):
                obj = Department.objects.filter(name=d['name']).first()
                if obj:
                    stats['depts_updated'] += 1
                else:
                    if not dry:
                        Department.objects.create(name=d['name'])
                    stats['depts_created'] += 1

            # ── Типовые задачи ──
            for t in data.get('task_types', []):
                obj = TaskType.objects.filter(name=t['name']).first()
                if obj:
                    obj.plan_hours = t['plan_hours']
                    obj.due_days = t['due_days']
                    obj.is_active = t.get('is_active', True)
                    if not dry:
                        obj.save()
                    stats['tt_updated'] += 1
                else:
                    if not dry:
                        TaskType.objects.create(
                            name=t['name'],
                            plan_hours=t['plan_hours'],
                            due_days=t['due_days'],
                            is_active=t.get('is_active', True),
                        )
                    stats['tt_created'] += 1

            # ── Нормы ──
            for n in data.get('norms', []):
                if not dry:
                    Norm.objects.create(
                        hours_per_day=n['hours_per_day'],
                        note=n.get('note', ''),
                    )
                stats['norms_created'] += 1

            # ── Пользователи (опционально) ──
            if options['include_users']:
                role_map = {r.code: r for r in Role.objects.all()}
                dept_map = {d.name: d for d in Department.objects.all()}

                for u in data.get('users', []):
                    email = (u.get('email') or '').strip().lower()
                    if not email:
                        stats['users_skipped'] += 1
                        continue
                    if User.objects.filter(email__iexact=email).exists():
                        stats['users_skipped'] += 1
                        continue

                    if not dry:
                        nu = User(
                            email=email,
                            full_name=u.get('full_name') or email,
                            is_active=bool(u.get('is_active', True)),
                            is_superuser=bool(u.get('is_superuser', False)),
                        )
                        r_code = u.get('role_code')
                        if r_code and r_code in role_map:
                            nu.role = role_map[r_code]
                        d_name = u.get('department_name')
                        if d_name and d_name in dept_map:
                            nu.department = dept_map[d_name]
                        nu.set_unusable_password()
                        nu.save()
                    stats['users_created'] += 1

            # ── Логи (опционально) ──
            if options['include_logs']:
                from admin_panel.models import AuditLog, LoginEvent
                from django.utils.dateparse import parse_datetime

                for a in data.get('audit', []):
                    if dry:
                        continue
                    actor_email = a.get('actor_email')
                    actor = User.objects.filter(email__iexact=actor_email).first() if actor_email else None
                    AuditLog.objects.create(
                        actor=actor,
                        action=a.get('action') or AuditLog.Action.UPDATE,
                        target_model=a.get('target_model') or '',
                        target_id=a.get('target_id') or '',
                        target_repr=a.get('target_repr') or '',
                        changes=a.get('changes'),
                        ip=a.get('ip') or None,
                        created_at=parse_datetime(a['created_at']) if a.get('created_at') else None,
                    )

                for e in data.get('logins', []):
                    if dry:
                        continue
                    user_email = e.get('user_email')
                    user = User.objects.filter(email__iexact=user_email).first() if user_email else None
                    LoginEvent.objects.create(
                        user=user,
                        email_attempted=e.get('email_attempted') or '',
                        success=bool(e.get('success')),
                        ip=e.get('ip') or None,
                        user_agent=e.get('user_agent') or '',
                        created_at=parse_datetime(e['created_at']) if e.get('created_at') else None,
                    )

        _run()

        prefix = '[dry-run] ' if dry else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}Восстановление завершено.'))
        for k, v in stats.items():
            if v:
                self.stdout.write(f'  {k}: {v}')
