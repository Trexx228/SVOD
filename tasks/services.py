import logging

from django.db import transaction
from django.utils import timezone

from .models import Task, TimeSession

logger = logging.getLogger(__name__)


def _close_session(task, status_at_close):
    if not task.session_started_at:
        return

    end = timezone.now()
    hours = (end - task.session_started_at).total_seconds() / 3600

    TimeSession.objects.create(
        task=task,
        executor=task.executor,
        started_at=task.session_started_at,
        finished_at=end,
        duration_hours=hours,
        status_at_close=status_at_close,
    )

    task.accumulated_hours = (task.accumulated_hours or 0.0) + hours
    task.session_started_at = None


def _pause_others(user, except_task):
    for t in Task.objects.filter(
            executor=user, status=Task.Status.IN_PROGRESS
    ).exclude(pk=except_task.pk):
        _close_session(t, Task.Status.PAUSED)
        t.status = Task.Status.PAUSED
        t.save(update_fields=['accumulated_hours', 'session_started_at', 'status'])


def _reward(user, task):
    try:
        from gamify.services import check_achievements, touch_streak

        if task.due and timezone.localdate() <= task.due:
            touch_streak(user)
        check_achievements(user)
    except Exception:
        logger.exception('gamify reward failed for task %s', task.pk)


def _comms(fn_name, task):
    try:
        from comms import services as comms_services
        getattr(comms_services, fn_name)(task)
    except Exception:
        logger.exception('comms notify %s failed for task %s', fn_name, task.pk)


@transaction.atomic
def start_or_resume(task, user):
    if task.status == Task.Status.IN_PROGRESS and task.session_started_at:
        return

    _pause_others(user, task)

    if task.session_started_at is None:
        task.session_started_at = timezone.now()

    task.status = Task.Status.IN_PROGRESS
    task.save(update_fields=['session_started_at', 'status'])


@transaction.atomic
def pause(task, new_status=Task.Status.PAUSED):
    _close_session(task, new_status)
    task.status = new_status
    task.save(update_fields=['accumulated_hours', 'session_started_at', 'status'])


@transaction.atomic
def submit_for_review(task):
    pause(task, Task.Status.REVIEW)
    _comms('notify_review', task)


@transaction.atomic
def close_task(task):
    _close_session(task, Task.Status.DONE)
    task.status = Task.Status.DONE
    task.finished_at = timezone.now()
    task.save(update_fields=[
        'accumulated_hours', 'session_started_at',
        'status', 'finished_at',
    ])

    if task.executor:
        _reward(task.executor, task)
    _comms('notify_close', task)


@transaction.atomic
def manager_accept(task):
    _close_session(task, Task.Status.DONE)
    task.status = Task.Status.DONE
    task.finished_at = timezone.now()
    task.save(update_fields=[
        'accumulated_hours', 'session_started_at',
        'status', 'finished_at',
    ])

    if task.executor:
        _reward(task.executor, task)
    _comms('notify_accept', task)


@transaction.atomic
def manager_rework(task):
    pause(task, Task.Status.REWORK)
    _comms('notify_rework', task)


def auto_start(task):
    """Автостарт таймера при постановке задачи со сроком."""
    if task.due and task.status == Task.Status.NEW and not task.session_started_at:
        task.status = Task.Status.IN_PROGRESS
        task.session_started_at = timezone.now()
        task.save(update_fields=['status', 'session_started_at'])
