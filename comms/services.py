from django.contrib.auth import get_user_model
from django.urls import NoReverseMatch, reverse

from .models import Notification


def task_url(task):
    pk = getattr(task, 'pk', None)
    if pk is None:
        return ''

    try:
        return reverse('task_detail', args=[pk])
    except NoReverseMatch:
        return f'/tasks/task/{pk}/'


def notify(users, text, url='', kind=Notification.Kind.INFO):
    if not users:
        return

    text = (text or '')[:300]
    url = (url or '')[:300]
    seen = set()

    for u in users:
        if not u or not u.pk or u.pk in seen:
            continue

        seen.add(u.pk)
        Notification.objects.create(
            recipient=u,
            text=text,
            url=url,
            kind=kind,
        )


def task_participants(task, extra=()):
    people = {task.executor, task.requester}
    people.update(extra)
    return {u for u in people if u and u.pk}


def _notify_requester_and_managers(task, actor, text, kind=Notification.Kind.INFO):
    recipients = []

    if getattr(task, 'requester_id', None):
        requester = task.requester
        if requester and requester.is_active:
            recipients.append(requester)

    if getattr(task, 'executor_id', None):
        executor = task.executor
        if executor and getattr(executor, 'department_id', None):
            User = get_user_model()
            managers = User.objects.filter(
                department_id=executor.department_id,
                role__can_manage=True,
                is_active=True,
            )
            recipients.extend(managers)

    if actor and getattr(actor, 'pk', None):
        recipients = [u for u in recipients if u and u.pk != actor.pk]

    notify(recipients, text, task_url(task), kind)


def notify_task_created(task):
    notify(
        [task.executor],
        f'Новая задача: {task.title}',
        task_url(task),
        Notification.Kind.ACTION,
    )
    _try_email('notify_task_assigned', task)


def notify_review(task):
    notify(
        [task.requester],
        f'Работа сдана на приёмку: {task.title}',
        task_url(task),
        Notification.Kind.ACTION,
    )
    _try_email('notify_task_review', task)


def notify_accept(task):
    notify(
        [task.executor],
        f'Задача принята: {task.title}',
        task_url(task),
        Notification.Kind.INFO,
    )
    _try_email('notify_task_accepted', task)


def notify_rework(task):
    notify(
        [task.executor],
        f'Возврат на доработку: {task.title}',
        task_url(task),
        Notification.Kind.ALERT,
    )
    comment = ''
    if hasattr(task, '_rework_comment'):
        comment = task._rework_comment
    _try_email('notify_task_returned', task, comment=comment)


def notify_close(task):
    notify(
        [task.requester],
        f'Задача закрыта: {task.title}',
        task_url(task),
        Notification.Kind.INFO,
    )


def notify_task_started(task, actor, resumed=False):
    """Оповестить постановщика и руководителя подразделения,
    что исполнитель взял задачу в работу (или возобновил после паузы)."""
    actor_name = actor.full_name if actor else 'Пользователь'
    verb = 'возобновил(а) работу над задачей' if resumed else 'взял(а) в работу задачу'
    text = f'▶️ {actor_name} {verb} «{task.title}».'

    _notify_requester_and_managers(
        task,
        actor,
        text,
        Notification.Kind.INFO,
    )


def notify_shop_started(task, actor):
    """Оповестить постановщика и руководителя подразделения, что сотрудник ушёл в цех."""
    actor_name = actor.full_name if actor else 'Пользователь'
    text = f'🏭 {actor_name} вышел(ла) в цех по задаче «{task.title}».'

    _notify_requester_and_managers(
        task,
        actor,
        text,
        Notification.Kind.INFO,
    )


    # ─────────────────────────────────────────────────────────────
# Email-обёртка (безопасная: любые ошибки глотаются)
# ─────────────────────────────────────────────────────────────

def _try_email(func_name, task, **kwargs):
    """Вызывает comms.email.<func_name>(task, **kwargs), игнорируя ошибки.

    Email — некритичный побочный эффект. Падение письма не должно ломать
    основной флоу (создание/возврат/приёмку задачи).
    """
    try:
        from . import email as email_helpers
        func = getattr(email_helpers, func_name, None)
        if func is None:
            return False
        return func(task, **kwargs)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'email hook failed: %s', func_name,
        )
        return False
