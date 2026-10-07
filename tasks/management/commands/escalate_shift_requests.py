"""Эскалация заявок на сдвиг.

Запускать по cron раз в 5–10 минут:
    python manage.py escalate_shift_requests
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from tasks import plan_shift
from tasks.models import PlanShiftRequest


class Command(BaseCommand):
    help = 'Эскалирует заявки, у которых истёк таймаут на текущем уровне.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        now = timezone.now()
        qs = (
            PlanShiftRequest.objects
            .filter(status=PlanShiftRequest.Status.PENDING, escalate_at__lte=now)
            .select_related('source_task', 'department')
        )

        dry = options['dry_run']
        escalated = 0

        for req in qs:
            if dry:
                self.stdout.write(
                    f'[dry] #{req.pk} {req.current_level} '
                    f'→ следующий уровень'
                )
                escalated += 1
                continue
            try:
                if plan_shift.escalate(req):
                    escalated += 1
                    self.stdout.write(f'#{req.pk}: эскалировано → {req.current_level}')
            except Exception as e:
                self.stderr.write(f'#{req.pk}: ошибка {e}')

        self.stdout.write(self.style.SUCCESS(f'Эскалировано: {escalated}'))
