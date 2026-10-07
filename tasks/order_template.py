"""Сервис генерации задач заказа по шаблону маршрута.

Рабочие дни считаются по графику исполнителя (см. schedule_for_user):
  - офисный 5/2 с use_calendar=True → паттерн + праздники РФ;
  - сменный 2/2, 7/7 с use_calendar=False → только паттерн;
  - график не задан → старая логика (пн–пт + праздники РФ).
"""
from __future__ import annotations

import logging
from datetime import date

from django.contrib.auth import get_user_model
from django.db import transaction

from .models import Order, OrderTemplate, Task, TaskBranch
from .utils import add_work_days, schedule_for_user

logger = logging.getLogger(__name__)
User = get_user_model()


ANCHOR_ATTR = {
    'contract_start': 'contract_start',
    'design_start':   'design_start',
    'design_end':     'design_end',
    'ship_due':       'ship_due',
}


def _anchor_date(order: Order, anchor_code: str) -> date | None:
    return getattr(order, ANCHOR_ATTR.get(anchor_code, ''), None)


def _resolve_executor(order: Order, role_code: str) -> User | None:
    """Найти исполнителя для этапа.

    Приоритет:
      1. Активный сотрудник нужной роли в отделе заказа
      2. Активный сотрудник нужной роли в любом отделе (fallback)
      3. None — задача без исполнителя не создаётся, этап пропускается
    """
    if not role_code:
        return None

    base = User.objects.filter(
        is_active=True, role__code=role_code,
    ).exclude(employment_status__in=[
        User.EmploymentStatus.DISMISSED,
        User.EmploymentStatus.ARCHIVED,
    ])

    # Отдел заказа определяем по requester'у первой задачи, если есть.
    # Иначе — по отделу requester'а этапа (его мы ещё не создали).
    # Практический компромисс: ищем в том же отделе, что и requester первой
    # уже созданной задачи, иначе — любой в этом отделе, иначе — любой.
    dept_id = (
        order.tasks.select_related('executor__department')
        .values_list('executor__department_id', flat=True)
        .first()
    ) if order.pk else None

    if dept_id:
        candidate = base.filter(department_id=dept_id).order_by('full_name').first()
        if candidate:
            return candidate

    return base.order_by('full_name').first()


def _calc_hours(stage, task_type) -> float:
    if stage.use_task_type_hours and task_type and task_type.plan_hours:
        return float(task_type.plan_hours)
    return float(stage.plan_hours or 0.0)


def _calc_kind(stage, task_type) -> str:
    if stage.kind:
        return stage.kind
    if task_type:
        return 'work'  # TaskType не хранит kind, поэтому дефолт
    return 'work'


@transaction.atomic
def generate_plan_from_template(
        order: Order,
        template: OrderTemplate,
        requester: User,
        *,
        skip_existing_branches: bool = True,
) -> dict:
    """Создаёт ветки и задачи заказа по шаблону.

    Возвращает:
        {
            'branches_created': int,
            'tasks_created':    int,
            'tasks_skipped':    int,
            'errors':           [str, ...],
        }
    """
    result = {
        'branches_created': 0,
        'tasks_created': 0,
        'tasks_skipped': 0,
        'errors': [],
    }

    stages = list(template.stages.select_related('task_type').order_by('order'))
    if not stages:
        result['errors'].append('В шаблоне нет этапов.')
        return result

    # Группируем по веткам, чтобы правильно расставлять stage_order.
    # Задачи вне ветки (branch_name == '') — отдельная «ветка» без модели.
    branch_cache: dict[str, TaskBranch | None] = {}
    stage_counter: dict[str, int] = {}

    for st in stages:
        anchor = _anchor_date(order, st.offset_anchor)
        if anchor is None:
            result['errors'].append(
                f'Этап «{st.title}»: у заказа не задана точка '
                f'«{st.get_offset_anchor_display()}».'
            )
            result['tasks_skipped'] += 1
            continue

        task_type = st.task_type
        plan_hours = _calc_hours(st, task_type)
        if plan_hours <= 0:
            result['errors'].append(
                f'Этап «{st.title}»: план часов = 0 (нет типовой или явного плана).'
            )
            result['tasks_skipped'] += 1
            continue

        executor = _resolve_executor(order, st.executor_role_code)
        if executor is None:
            result['errors'].append(
                f'Этап «{st.title}»: не найден исполнитель роли '
                f'«{st.executor_role_code}» — этап пропущен.'
            )
            result['tasks_skipped'] += 1
            continue

        # График работы исполнителя — от него зависят рабочие дни.
        # Один раз на этап, чтобы не дёргать БД повторно.
        schedule = schedule_for_user(executor)

        start_due = add_work_days(anchor, st.offset_days, schedule=schedule)
        due = add_work_days(
            start_due, max(1, st.duration_days) - 1, schedule=schedule,
                       )

        # Ветка
        branch: TaskBranch | None = None
        branch_name = (st.branch_name or '').strip()
        if branch_name:
            if branch_name in branch_cache:
                branch = branch_cache[branch_name]
            else:
                existing = TaskBranch.objects.filter(order=order, name=branch_name).first()
                if existing:
                    branch = existing
                else:
                    branch = TaskBranch.objects.create(order=order, name=branch_name)
                    result['branches_created'] += 1
                branch_cache[branch_name] = branch

        # stage_order: считаем по ветке в порядке шаблона
        key = branch_name or '__none__'
        stage_counter[key] = stage_counter.get(key, 0) + 1
        stage_order = stage_counter[key] if branch else None

        # Внутри одной ветки все этапы, кроме первого, ждут предыдущий
        blocked = bool(branch and stage_order and stage_order > 1)

        task = Task.objects.create(
            title=st.title,
            plan_hours=plan_hours,
            start_due=start_due,
            due=due,
            priority=st.priority,
            scale='s',
            kind=_calc_kind(st, task_type),
            order=order,
            branch=branch,
            stage_order=stage_order,
            blocked_by_stage=blocked,
            task_type=task_type,
            executor=executor,
            requester=requester,
            body=f'Создано из шаблона «{template.name}».',
        )

        # Уведомляем исполнителя только если не заблокирована.
        if not blocked:
            try:
                from comms.services import notify_task_created
                notify_task_created(task)
            except Exception:
                logger.exception(
                    'order_template: notify_task_created failed for task %s', task.pk
                )

        result['tasks_created'] += 1

    return result
