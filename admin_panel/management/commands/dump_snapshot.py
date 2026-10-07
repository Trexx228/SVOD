import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Выгружает справочники и логи в JSON для offline-восстановления. '
        'Это не бэкап БД — только данные админки (роли, подразделения, '
        'типовые, нормы, аудит, алерты). Пользователи и задачи — опционально.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', type=str, required=True,
            help='Путь к JSON-файлу (например, dumps/snapshot.json).',
        )
        parser.add_argument(
            '--include-users', action='store_true',
            help='Включить пользователей (без паролей — только профили).',
        )
        parser.add_argument(
            '--include-tasks', action='store_true',
            help='Включить задачи (может быть много).',
        )

    def handle(self, *args, **options):
        from accounts.models import Department, Role, User
        from core.models import TaskType, Norm
        from tasks.models import Task
        from admin_panel.models import AuditLog, LoginEvent, AdminAlert

        out_path = Path(options['output'])
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            'meta': {
                'dumped_at': timezone.now().isoformat(),
                'django_version': __import__('django').get_version(),
                'python_version': __import__('platform').python_version(),
                'settings_module': settings.SETTINGS_MODULE,
                'include_users': options['include_users'],
                'include_tasks': options['include_tasks'],
            },
            'roles': list(
                Role.objects.values('id', 'name', 'code', 'can_manage',
                                    'can_admin', 'can_plant', 'description')
            ),
            'departments': list(
                Department.objects.values('id', 'name', 'created_at')
            ),
            'task_types': list(
                TaskType.objects.values('id', 'name', 'plan_hours',
                                        'due_days', 'is_active')
            ),
            'norms': list(
                Norm.objects.values('id', 'hours_per_day', 'note')
            ),
            'audit': [
                {
                    'created_at': a.created_at.isoformat(),
                    'actor_email': a.actor.email if a.actor else None,
                    'action': a.action,
                    'target_model': a.target_model,
                    'target_id': a.target_id,
                    'target_repr': a.target_repr,
                    'changes': a.changes,
                    'ip': a.ip,
                }
                for a in AuditLog.objects.select_related('actor').order_by('-created_at')[:10000]
            ],
            'logins': [
                {
                    'created_at': e.created_at.isoformat(),
                    'user_email': e.user.email if e.user else None,
                    'email_attempted': e.email_attempted,
                    'success': e.success,
                    'ip': e.ip,
                    'user_agent': e.user_agent,
                }
                for e in LoginEvent.objects.select_related('user').order_by('-created_at')[:10000]
            ],
            'alerts': [
                {
                    'kind': a.kind,
                    'key': a.key,
                    'severity': a.severity,
                    'title': a.title,
                    'message': a.message,
                    'url': a.url,
                    'count': a.count,
                    'seen': a.seen,
                    'created_at': a.created_at.isoformat(),
                    'last_seen_at': a.last_seen_at.isoformat(),
                }
                for a in AdminAlert.objects.all()
            ],
        }

        if options['include_users']:
            payload['users'] = [
                {
                    'email': u.email,
                    'full_name': u.full_name,
                    'is_active': u.is_active,
                    'is_superuser': u.is_superuser,
                    'role_code': u.role.code if u.role else None,
                    'department_name': u.department.name if u.department else None,
                    'date_joined': u.date_joined.isoformat() if u.date_joined else None,
                    'last_activity_at': u.last_activity_at.isoformat() if u.last_activity_at else None,
                }
                for u in User.objects.select_related('role', 'department')
                .order_by('email')
            ]

        if options['include_tasks']:
            payload['tasks'] = [
                {
                    'id': t.pk,
                    'title': t.title,
                    'status': t.status,
                    'priority': t.priority,
                    'scale': t.scale,
                    'kind': t.kind,
                    'plan_hours': t.plan_hours,
                    'accumulated_hours': t.accumulated_hours,
                    'start_due': t.start_due.isoformat() if t.start_due else None,
                    'due': t.due.isoformat() if t.due else None,
                    'executor_email': t.executor.email if t.executor else None,
                    'requester_email': t.requester.email if t.requester else None,
                    'task_type_name': t.task_type.name if t.task_type else None,
                    'created_at': t.created_at.isoformat() if t.created_at else None,
                    'finished_at': t.finished_at.isoformat() if t.finished_at else None,
                }
                for t in Task.objects.select_related(
                    'executor', 'requester', 'task_type'
                ).order_by('pk')
            ]

        with out_path.open('w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        self.stdout.write(self.style.SUCCESS(
            f'Дамп сохранён: {out_path} ({out_path.stat().st_size} байт).'
        ))
        self.stdout.write(f'  роли: {len(payload["roles"])}')
        self.stdout.write(f'  подразделения: {len(payload["departments"])}')
        self.stdout.write(f'  типовые: {len(payload["task_types"])}')
        self.stdout.write(f'  нормы: {len(payload["norms"])}')
        self.stdout.write(f'  аудит: {len(payload["audit"])}')
        self.stdout.write(f'  входы: {len(payload["logins"])}')
        self.stdout.write(f'  алерты: {len(payload["alerts"])}')
        if options['include_users']:
            self.stdout.write(f'  пользователи: {len(payload["users"])}')
        if options['include_tasks']:
            self.stdout.write(f'  задачи: {len(payload["tasks"])}')
