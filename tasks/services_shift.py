"""Сервис пересчёта сдвига сроков задач.

Логика:
    - У каждой открытой задачи есть original_due — базовый срок.
    - У исполнителя есть shift_credit_days — сколько рабочих дней он занят
      срочными сверх плановой нагрузки.
    - Задачи, у которых резерв меньше этой нагрузки, сдвигаются вперёд
      на недостающее число рабочих дней.
    - Сдвиг считается от original_due, а не от текущего due — это позволяет
      пересчитывать сколько угодно раз без накопления ошибки.
"""
import logging
import math

from django.db import transaction
from django.utils import timezone

from .models import Task, TaskLog
from .utils import (
    add_work_days,
    norm_hours_for,
    schedule_for_user,
    work_days_between,
)

logger = logging.getLogger(__name__)


# ── Статусы, у которых сдвиг разрешён ──────────────────────────────────
SHIFTABLE_STATUSES = (
    Task.Status.NEW,
    Task.Status.PAUSED,
    Task.Status.REWORK,
)


def _needed_days(task):
    """Сколько рабочих дней нужно на задачу с учётом нормы исполнителя."""
    norm = norm_hours_for(task.executor)
    hours = task.plan_hours or 0
    if hours <= 0:
        return 1  # минимум 1 день даже для «пустой» оценки
    return max(1, int(math.ceil(hours / norm)))


def _window_days(task, schedule=None):
    """Окно задачи в рабочих днях.

    Приоритет:
      1. original_start_due → original_due
      2. дата создания → original_due
      3. None — если нет дедлайна (∞ окно, не сдвигаем).

    Если due раньше start — окно битое, не сдвигаем. Явная проверка
    нужна потому, что work_days_between на диапазоне из нерабочих
    дней может вернуть 0 вместо отрицательного значения (например,
    суббота → воскресенье). Тогда исходное условие `window < 0`
    не сработает, и мы получим nonsense-сдвиг.

    schedule: WorkSchedule исполнителя (или None — старая логика).
    """
    if task.original_due is None:
        return None

    start = task.original_start_due
    if start is None:
        start = timezone.localtime(task.created_at).date()

    # Дедлайн раньше старта — некорректная задача, не сдвигаем.
    if task.original_due < start:
        return None

    window = work_days_between(start, task.original_due, schedule=schedule)
    if window is None or window < 0:
        return None
    return window


def compute_task_shift(task, shift_credit_days, schedule=None):
    """На сколько рабочих дней сдвинуть задачу при данной нагрузке.

    Формула: shift = max(0, credit - reserve),
    где reserve = window - needed.

    Возвращает 0, если нагрузки нет, окно не валидно или reserve >= credit.

    schedule: WorkSchedule исполнителя (или None — старая логика).
    """
    if shift_credit_days <= 0:
        return 0

    window = _window_days(task, schedule=schedule)
    if window is None:
        return 0

    needed = _needed_days(task)
    reserve = window - needed

    return max(0, shift_credit_days - reserve)


def apply_shift_to_task(task, shift_days, schedule=None):
    """Применить сдвиг к задаче — от original_*, а не от текущего значения.

    Идемпотентная: вызов с одним и тем же shift_days N раз даёт один
    и тот же результат.

    schedule: WorkSchedule исполнителя (или None — старая логика).
    """
    update_fields = []

    if task.original_start_due is not None:
        target_start = (
            add_work_days(task.original_start_due, shift_days, schedule=schedule)
            if shift_days > 0 else task.original_start_due
        )
        if task.start_due != target_start:
            task.start_due = target_start
            update_fields.append('start_due')

    if task.original_due is not None:
        target_due = (
            add_work_days(task.original_due, shift_days, schedule=schedule)
            if shift_days > 0 else task.original_due
        )
        if task.due != target_due:
            task.due = target_due
            update_fields.append('due')

    if update_fields:
        task.save(update_fields=update_fields)
        return True
    return False


@transaction.atomic
def recompute_shift_for_executor(
        executor, actor=None, source_task=None, exclude_task_pk=None,
):
    """Пересчитать сдвиг всех подходящих задач исполнителя.

    Параметры:
        executor        — User, чей credit берём
        actor           — User для журнала (обычно постановщик срочной)
        source_task     — Task срочной, которая вызвала пересчёт (для лога)
        exclude_task_pk — pk срочной задачи, которую НЕ сдвигаем

    Возвращает число изменённых задач.

    График исполнителя определяется один раз (schedule_for_user) и
    прокидывается во все расчёты: иначе на каждой задаче будет
    отдельный запрос к WorkSchedule.
    """
    credit = executor.shift_credit_days or 0
    schedule = schedule_for_user(executor)

    qs = Task.objects.filter(
        executor=executor,
        status__in=SHIFTABLE_STATUSES,
        original_due__isnull=False,
    )
    if exclude_task_pk is not None:
        qs = qs.exclude(pk=exclude_task_pk)

    changed = 0
    for t in qs:
        target = compute_task_shift(t, credit, schedule=schedule)
        prev_due = t.due
        if apply_shift_to_task(t, target, schedule=schedule):
            changed += 1
            # Лог пишем только если известен автор — это аудит «кто сдвинул».
            # Внутренние пересчёты без актора (например, из management-команды)
            # журнал не засоряют.
            if actor is not None and t.due != prev_due:
                TaskLog.objects.create(
                    task=t,
                    kind=TaskLog.Kind.DUE,
                    author=actor,
                    source_task=source_task,
                    shift_days=target,
                    comment=(
                        f'Пересчёт сдвига: нагрузка {credit} раб. дн., '
                        f'задача сдвинута на {target} раб. дн. '
                        f'({prev_due or "—"} → {t.due or "—"})'
                    ),
                )

    return changed
