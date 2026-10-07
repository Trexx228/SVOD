"""Ежедневный снимок задач для диаграммы Ганта и динамики.

Запускается из планировщика каждый день в 23:55.
    python manage.py capture_snapshot
    python manage.py capture_snapshot --date=2026-09-30
"""
from datetime import datetime, time as dtime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from tasks.models import Task, TimeSession, TimelineSnapshot


class Command(BaseCommand):
    help = 'Ежедневный снимок задач с метриками дня.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date', type=str, default='',
            help='Дата снимка в формате ГГГГ-ММ-ДД (по умолчанию сегодня).',
        )
        parser.add_argument(
            '--all', action='store_true',
            help='Снимать и закрытые задачи (по умолчанию — только открытые + закрытые за последние 30 дней).',
        )

    def handle(self, *args, **opts):
        day = timezone.localdate()

        if opts['date']:
            try:
                day = datetime.strptime(opts['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError(
                    f'Неверный формат даты: «{opts["date"]}». Ожидается ГГГГ-ММ-ДД.'
                )

        # Что снимаем
        if opts['all']:
            qs = Task.objects.all()
        else:
            from datetime import timedelta
            cutoff = day - timedelta(days=30)
            qs = (
                Task.objects
                .exclude(
                    status__in=[Task.Status.DONE, Task.Status.CANCELLED],
                    finished_at__date__lt=cutoff,
                )
            )

        qs = qs.select_related('executor')

        # Списания за день
        start_dt = timezone.make_aware(
            datetime.combine(day, dtime.min),
            timezone.get_current_timezone(),
        )
        end_dt = timezone.make_aware(
            datetime.combine(day, dtime.max),
            timezone.get_current_timezone(),
        )

        hours_by_task = dict(
            TimeSession.objects
            .filter(finished_at__gte=start_dt, finished_at__lte=end_dt)
            .values('task_id')
            .annotate(h=Sum('duration_hours'))
            .values_list('task_id', 'h')
        )

        # Предыдущие снимки для сравнения
        prev_date = day  # ищем последний снимок ДО day
        prev = {}
        for snap in (
                TimelineSnapshot.objects
                        .filter(snapshot_date__lt=day, task__in=qs.values('pk'))
                        .order_by('task_id', '-snapshot_date')
        ):
            if snap.task_id not in prev:
                prev[snap.task_id] = snap

        created = updated = 0

        with transaction.atomic():
            for t in qs:
                hours_today = round(hours_by_task.get(t.pk, 0.0) or 0.0, 2)

                old = prev.get(t.pk)
                status_changed = bool(old and old.status != t.status)
                due_before = None
                if old and old.due_date != t.due:
                    due_before = old.due_date

                obj, was_created = TimelineSnapshot.objects.update_or_create(
                    snapshot_date=day,
                    task=t,
                    defaults={
                        'executor': t.executor,
                        'start_date': t.start_due or timezone.localtime(t.created_at).date(),
                        'due_date': t.due,
                        'status': t.status,
                        'priority': t.priority,
                        'plan_hours': t.plan_hours or 0,
                        'accumulated_hours': round(t.accumulated_hours or 0.0, 2),
                        'hours_today': hours_today,
                        'status_changed': status_changed,
                        'due_before': due_before,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Снимок на {day}: создано {created}, обновлено {updated} '
            f'(всего задач {qs.count()}).'
        ))
