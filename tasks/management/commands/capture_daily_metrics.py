"""Ежедневный срез метрик по каждому подразделению.

Запускается из scheduler в 23:50. Идемпотентна: повторный запуск за
тот же день обновляет запись, а не создаёт дубликат.
"""
from datetime import datetime, time as dtime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import Department
from tasks.models import (
    DailyDeptMetrics, ShopSession, Task, TimeSession,
)


class Command(BaseCommand):
    help = 'Дневной срез метрик по подразделениям.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date', type=str, default='',
            help='Дата среза в формате ГГГГ-ММ-ДД (по умолчанию сегодня).',
        )

    def handle(self, *args, **opts):
        day = timezone.localdate()
        if opts['date']:
            try:
                day = datetime.strptime(opts['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError(f'Неверный формат даты: {opts["date"]}')

        start_dt = timezone.make_aware(
            datetime.combine(day, dtime.min), timezone.get_current_timezone(),
        )
        end_dt = timezone.make_aware(
            datetime.combine(day, dtime.max), timezone.get_current_timezone(),
        )

        created = updated = 0

        for dept in Department.objects.all():
            members = list(
                dept.users.filter(is_active=True).values_list('pk', flat=True)
            )
            if not members:
                # Пропускаем пустые отделы
                continue

            open_qs = (
                Task.objects.filter(executor_id__in=members)
                .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
            )
            tasks_open = open_qs.count()
            plan_hours_open = (
                    open_qs.aggregate(s=Sum('plan_hours'))['s'] or 0.0
            )

            done_today_qs = Task.objects.filter(
                executor_id__in=members,
                status=Task.Status.DONE,
                finished_at__gte=start_dt,
                finished_at__lte=end_dt,
            )
            tasks_done_today = done_today_qs.count()

            today = timezone.localdate()
            tasks_overdue = open_qs.filter(due__lt=today).count()

            # Часы за сегодня
            fact_agg = (
                TimeSession.objects
                .filter(
                    executor_id__in=members,
                    finished_at__gte=start_dt,
                    finished_at__lte=end_dt,
                )
                .aggregate(s=Sum('duration_hours'), c=Count('id'))
            )
            fact_hours_today = round(fact_agg['s'] or 0.0, 2)
            sessions_count = fact_agg['c'] or 0

            shop_agg = (
                ShopSession.objects
                .filter(
                    executor_id__in=members,
                    started_at__gte=start_dt,
                    started_at__lte=end_dt,
                    finished_at__isnull=False,
                )
                .aggregate(s=Sum('duration_hours'))
            )
            shop_hours_today = round(shop_agg['s'] or 0.0, 2)

            employees_active = (
                TimeSession.objects
                .filter(
                    executor_id__in=members,
                    finished_at__gte=start_dt,
                    finished_at__lte=end_dt,
                )
                .values('executor_id')
                .distinct()
                .count()
            )

            obj, was_created = DailyDeptMetrics.objects.update_or_create(
                date=day,
                department=dept,
                defaults={
                    'tasks_open': tasks_open,
                    'tasks_done_today': tasks_done_today,
                    'tasks_overdue': tasks_overdue,
                    'plan_hours_open': round(plan_hours_open, 2),
                    'fact_hours_today': fact_hours_today,
                    'shop_hours_today': shop_hours_today,
                    'sessions_count': sessions_count,
                    'employees_active': employees_active,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Срез на {day}: создано {created}, обновлено {updated}.'
        ))
