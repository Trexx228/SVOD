
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from tasks.models import Task
from comms.email import send_daily_digest


class Command(BaseCommand):
    help = (
        'Отправляет утренний дайджест пользователям с email_digest_daily=True. '
        'Собирает задачи на сегодня, просрочки и очередь на приёмку.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы отправлено, но не отправлять.',
        )
        parser.add_argument(
            '--user', type=int, default=None,
            help='Отправить только конкретному пользователю (pk).',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        today = timezone.localdate()
        dry = options['dry_run']

        qs = User.objects.filter(
            is_active=True,
            email_digest_daily=True,
        ).exclude(email='')

        if options['user']:
            qs = qs.filter(pk=options['user'])

        total_sent = 0
        total_skipped = 0

        for user in qs:
            # Задачи, где он исполнитель
            mine = Task.objects.filter(executor=user).exclude(
                status__in=[Task.Status.DONE, Task.Status.CANCELLED]
            )

            tasks_today = list(
                mine.filter(due=today).order_by('priority')
            )
            tasks_overdue = list(
                mine.filter(due__lt=today).order_by('due')
            )

            # Приёмка: задачи, где он постановщик и статус REVIEW
            tasks_review = list(
                Task.objects.filter(
                    requester=user, status=Task.Status.REVIEW,
                ).select_related('executor')
            )

            total = len(tasks_today) + len(tasks_overdue) + len(tasks_review)

            if total == 0:
                total_skipped += 1
                if dry:
                    self.stdout.write(
                        f'  [skip] {user.email} — нет задач'
                    )
                continue

            if dry:
                self.stdout.write(
                    f'  [dry] {user.email} — '
                    f'сегодня: {len(tasks_today)}, '
                    f'просрочено: {len(tasks_overdue)}, '
                    f'приёмка: {len(tasks_review)}'
                )
                total_sent += 1
                continue

            ok = send_daily_digest(user, tasks_today, tasks_overdue, tasks_review)
            if ok:
                total_sent += 1
                self.stdout.write(
                    self.style.SUCCESS(f'  ✓ {user.email} ({total})')
                )
            else:
                self.stderr.write(f'  ✗ {user.email}')

        self.stdout.write(self.style.SUCCESS(
            f'Дайджест завершён. Отправлено: {total_sent}, пропущено: {total_skipped}.'
        ))
