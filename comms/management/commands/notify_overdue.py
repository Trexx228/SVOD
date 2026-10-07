from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from comms.models import Notification
from comms.services import notify
from tasks.models import Task
from tasks.utils import format_overdue

User = get_user_model()

OPEN_ST = [
    Task.Status.NEW,
    Task.Status.IN_PROGRESS,
    Task.Status.PAUSED,
    Task.Status.REWORK,
]


class Command(BaseCommand):
    help = 'Однократно оповестить о появившейся просрочке'

    def handle(self, *args, **options):
        sent = 0
        bosses_cache = {}

        tasks = (
            Task.objects.filter(status__in=OPEN_ST, notified_overdue=False)
            .select_related('executor', 'executor__department')
        )

        for t in tasks:
            try:
                txt = format_overdue(t)
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f'Задача {t.pk}: ошибка форматирования просрочки: {e}'
                    )
                )
                continue

            if not txt:
                continue

            recipients = []

            if t.executor_id and t.executor and t.executor.is_active:
                recipients.append(t.executor)

            department_id = getattr(t.executor, 'department_id', None)
            if department_id:
                if department_id not in bosses_cache:
                    bosses_cache[department_id] = list(
                        User.objects.filter(
                            role__can_manage=True,
                            department_id=department_id,
                            is_active=True,
                        )
                    )

                recipients.extend(bosses_cache[department_id])

            seen = set()
            unique_recipients = []

            for user in recipients:
                if user and user.pk not in seen:
                    seen.add(user.pk)
                    unique_recipients.append(user)

            if not unique_recipients:
                continue

            try:
                notify(
                    unique_recipients,
                    f'Просрочка по задаче {t.title}: {txt}',
                    f'/tasks/task/{t.pk}/',
                    Notification.Kind.ALERT,
                )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f'Задача {t.pk}: не удалось отправить уведомление: {e}'
                    )
                )
                continue

            t.notified_overdue = True
            t.save(update_fields=['notified_overdue'])
            sent += 1

        self.stdout.write(
            self.style.SUCCESS(f'Оповещений о просрочке: {sent}')
        )
