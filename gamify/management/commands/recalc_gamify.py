from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gamify.services import check_achievements, ensure_catalog


class Command(BaseCommand):
    help = 'Пересчитать ачивки всем активным пользователям'

    def handle(self, *args, **options):
        try:
            ensure_catalog()
        except Exception as e:
            raise CommandError(f'Не удалось инициализировать каталог: {e}')

        User = get_user_model()
        processed = 0
        failed = 0
        last_pk = 0
        batch_size = 1000

        while True:
            batch = list(
                User.objects.filter(is_active=True, pk__gt=last_pk)
                .order_by('pk')[:batch_size]
            )

            if not batch:
                break

            for user in batch:
                try:
                    with transaction.atomic():
                        check_achievements(user)
                    processed += 1
                except Exception as e:
                    failed += 1
                    self.stderr.write(
                        self.style.ERROR(f'Ошибка для пользователя {user.pk}: {e}')
                    )

            last_pk = batch[-1].pk

        if failed:
            raise CommandError(
                f'Ачивки пересчитаны: {processed}, ошибок: {failed}.'
            )

        self.stdout.write(self.style.SUCCESS('Ачивки пересчитаны.'))
