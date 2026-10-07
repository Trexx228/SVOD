from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User


DEFAULT_IDLE_MIN = 5


class Command(BaseCommand):
    help = (
        'Пересчитывает User.idle_since по last_activity_at. '
        'Ставит idle_since тем, кто давно не проявлял активности, '
        'и сбрасывает тем, кто вернулся.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--minutes', type=int, default=DEFAULT_IDLE_MIN,
            help=f'Порог простоя в минутах (по умолчанию {DEFAULT_IDLE_MIN}).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы изменено, но не сохранять.',
        )

    def handle(self, *args, **options):
        minutes = options['minutes']
        dry_run = options['dry_run']

        now = timezone.now()
        idle_threshold = now - timedelta(minutes=minutes)

        # 1) Кто должен получить idle_since (активен, last_activity_at старый, idle_since пуст)
        to_set = User.objects.filter(
            is_active=True,
            last_activity_at__lt=idle_threshold,
            idle_since__isnull=True,
            last_activity_at__isnull=False,
        )
        set_count = to_set.count()

        # 2) Кто вернулся (last_activity_at свежий, но idle_since ещё стоит)
        to_clear = User.objects.filter(
            is_active=True,
            last_activity_at__gte=idle_threshold,
            idle_since__isnull=False,
        )
        clear_count = to_clear.count()

        if dry_run:
            self.stdout.write(self.style.WARNING(f'[dry-run] минут: {minutes}'))
            self.stdout.write(f'  получили бы idle_since: {set_count}')
            self.stdout.write(f'  сбросили бы idle_since: {clear_count}')
            return

        # Ставим idle_since = момент последней активности (условно «ушёл тогда»)
        updated_set = 0
        for u in to_set.only('pk', 'last_activity_at'):
            u.idle_since = u.last_activity_at
            u.save(update_fields=['idle_since'])
            updated_set += 1

        updated_clear = to_clear.update(idle_since=None)

        self.stdout.write(self.style.SUCCESS(
            f'idle_since: установлено {updated_set}, сброшено {updated_clear}.'
        ))
