"""Централизованная отправка email-уведомлений по задачам."""
import logging

from django.conf import settings as dj_settings
from django.core.mail import send_mail
from django.utils import timezone
logger = logging.getLogger(__name__)


def _can_email(user):
    if not user or not user.is_active:
        return False
    if not user.email:
        return False
    if not getattr(user, 'email_notifications', True):
        return False
    return True


def _send(user, subject, body):
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=dj_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
        return True
    except Exception:
        logger.exception('Failed to send task email to %s', user.email)
        return False


def _task_url(task):
    base = getattr(dj_settings, 'SITE_URL', '') or ''
    if not base:
        return f'/tasks/task/{task.pk}/'
    return f'{base.rstrip("/")}/tasks/task/{task.pk}/'


def notify_task_assigned(task):
    """Исполнителю: назначена новая задача."""
    user = task.executor
    if not _can_email(user):
        return False

    subject = f'[СВОД] Новая задача: {task.title[:80]}'
    body = (
        f'Здравствуйте, {user.full_name}!\n\n'
        f'Вам назначена задача:\n\n'
        f'  {task.title}\n'
        f'  Постановщик: {task.requester.full_name if task.requester else "—"}\n'
        f'  Приоритет: {task.get_priority_display()}\n'
        f'  План: {task.plan_hours or 0:.1f} ч\n'
    )
    if task.due:
        body += f'  Срок: {task.due.strftime("%d.%m.%Y")}\n'
    if task.start_due:
        body += f'  Начать: {task.start_due.strftime("%d.%m.%Y")}\n'
    body += (
        f'\nОткрыть задачу: {_task_url(task)}\n\n'
        f'--\nОтключить письма: /me/settings/\n'
    )
    return _send(user, subject, body)


def notify_task_returned(task, comment=''):
    """Исполнителю: задача вернулась на доработку."""
    user = task.executor
    if not _can_email(user):
        return False

    subject = f'[СВОД] Задача возвращена: {task.title[:80]}'
    body = (
        f'Здравствуйте, {user.full_name}!\n\n'
        f'Задача вернулась на доработку:\n\n'
        f'  {task.title}\n'
    )
    if comment:
        body += f'\nКомментарий: {comment}\n'
    body += (
        f'\nОткрыть задачу: {_task_url(task)}\n\n'
        f'--\nОтключить письма: /me/settings/\n'
    )
    return _send(user, subject, body)


def notify_task_accepted(task):
    """Исполнителю: результат принят."""
    user = task.executor
    if not _can_email(user):
        return False

    subject = f'[СВОД] Задача принята: {task.title[:80]}'
    body = (
        f'Здравствуйте, {user.full_name}!\n\n'
        f'Ваша работа принята:\n\n'
        f'  {task.title}\n'
        f'\nОткрыть задачу: {_task_url(task)}\n'
    )
    return _send(user, subject, body)


def notify_task_review(task):
    """Постановщику: задача сдана на приёмку."""
    user = task.requester
    if not _can_email(user):
        return False

    subject = f'[СВОД] Сдано на приёмку: {task.title[:80]}'
    body = (
        f'Здравствуйте, {user.full_name}!\n\n'
        f'Исполнитель {task.executor.full_name if task.executor else "—"} '
        f'сдал работу на приёмку:\n\n'
        f'  {task.title}\n'
        f'  Потрачено: {task.accumulated_hours or 0:.2f} ч\n'
        f'\nОткрыть задачу: {_task_url(task)}\n'
    )
    return _send(user, subject, body)


# ─────────────────────────────────────────────────────────────
# Утренний дайджест
# ─────────────────────────────────────────────────────────────

def send_daily_digest(user, tasks_today, tasks_overdue, tasks_review):
    """Собрать и отправить одно письмо со списком дел на день.

    tasks_today   — QuerySet/список задач на сегодня (не завершённых)
    tasks_overdue — просроченные
    tasks_review  — ждут приёмки у этого пользователя
    """
    if not _can_email(user):
        return False

    today = timezone.localdate()
    total = len(tasks_today) + len(tasks_overdue) + len(tasks_review)

    if total == 0:
        # Не спамим пустыми письмами
        return False

    subject = f'[СВОД] Задачи на {today.strftime("%d.%m.%Y")} ({total})'

    lines = [f'Здравствуйте, {user.full_name}!\n']

    if tasks_overdue:
        lines.append(f'⚠ ПРОСРОЧЕНО ({len(tasks_overdue)}):')
        for t in tasks_overdue[:10]:
            due = t.due.strftime('%d.%m.%Y') if t.due else '—'
            lines.append(f'  • [{due}] {t.title}')
        if len(tasks_overdue) > 10:
            lines.append(f'  …и ещё {len(tasks_overdue) - 10}')
        lines.append('')

    if tasks_today:
        lines.append(f'📅 На сегодня ({len(tasks_today)}):')
        for t in tasks_today[:15]:
            prio = t.get_priority_display()
            lines.append(f'  • [{prio}] {t.title}')
        if len(tasks_today) > 15:
            lines.append(f'  …и ещё {len(tasks_today) - 15}')
        lines.append('')

    if tasks_review:
        lines.append(f'🔍 Ждут приёмки ({len(tasks_review)}):')
        for t in tasks_review[:10]:
            executor = t.executor.full_name if t.executor else '—'
            lines.append(f'  • {t.title} (от {executor})')
        if len(tasks_review) > 10:
            lines.append(f'  …и ещё {len(tasks_review) - 10}')
        lines.append('')

    lines.append('--')
    lines.append('Открыть портал: /')
    lines.append('Отключить дайджест: /me/settings/')

    return _send(user, subject, '\n'.join(lines))
