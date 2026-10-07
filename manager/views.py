import csv as _csv
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from statistics import median

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Prefetch, Q, Sum
from django.db.models.functions import TruncDate
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from tasks import plan_shift
from tasks.models import PlanShiftRequest, PlanShiftStep

from accounts.models import Department

from comms.services import notify_task_created
from gamify.services import user_kpi_percent, manager_kpi_percent

from .forms import WorkScheduleForm

from tasks.models import (
    Order, OvertimeRecord, ShopSession, Task, TaskBranch, TaskLog,
    TaskProgress, TimeSession, WeekCommit,
)
from tasks.utils import (
    kpi_percent,
    norm_hours,
    parse_plan,
    schedule_for_user,
)
from core.models import Holiday, Norm



User = get_user_model()


# ─────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────

def _month_name(m):
    return ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
            'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек'][m - 1]


def _parse_date(s):
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _safe_redirect_url(request, fallback=None):
    if fallback is None:
        fallback = reverse('manager_cabinet')
    url = request.META.get('HTTP_REFERER')
    if url and url_has_allowed_host_and_scheme(
            url, allowed_hosts={request.get_host()}
    ):
        return url
    return fallback


def _local_date(value):
    return timezone.localtime(value).date() if value else None


def _delta(cur, prev):
    return round((cur - prev) / prev * 100) if prev else None


def _scope_user_ids(user):
    """Кого видит пользователь в отчётах, кабинете, метриках.

    admin      → все активные
    plant      → все активные (весь завод)
    department → активные своего отдела
    self       → только себя
    """
    scope = user.scope

    if scope in ('admin', 'plant'):
        return list(User.objects.filter(is_active=True).values_list('id', flat=True))

    if scope == 'department' and user.department_id:
        return list(
            User.objects.filter(
                department_id=user.department_id, is_active=True
            ).values_list('id', flat=True)
        )

    return [user.id]


def _wasted_stats(member_ids, since=None):
    """Отменённые задачи с потраченным временем — «ресурс впустую»."""
    qs = (
        Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.CANCELLED,
            accumulated_hours__gt=0,
        )
        .select_related('executor', 'executor__department', 'requester')
    )
    if since:
        qs = qs.filter(finished_at__gte=since)
    return list(qs.order_by('-finished_at'))


def _wasted_totals(tasks):
    return {
        'count': len(tasks),
        'fact_hours': round(sum(t.accumulated_hours or 0 for t in tasks), 1),
        'plan_hours': round(sum(t.plan_hours or 0 for t in tasks), 1),
    }


def _deferred_urgent(member_ids):
    """Срочные задачи, назначенные, но не взятые в работу."""
    return list(
        Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.NEW,
            priority='urgent',
        )
        .select_related('executor', 'executor__department', 'requester')
        .order_by('created_at')
    )


def _check_stuck_urgent(member_ids, user):
    """Найти невзятые urgent-задачи старше 30 мин и один раз создать
    уведомление руководителю по каждой.

    Возвращает список {'task': Task, 'minutes': int} для отображения в кабинете.
    """
    from comms.models import Notification

    threshold = timezone.now() - timedelta(minutes=30)
    stuck = (
        Task.objects
        .filter(
            executor_id__in=member_ids,
            status=Task.Status.NEW,
            priority='urgent',
            created_at__lt=threshold,
        )
        .select_related('executor', 'executor__department', 'requester')
        .order_by('created_at')
    )

    result = []
    for t in stuck:
        url = f'/tasks/task/{t.pk}/'
        already = Notification.objects.filter(
            recipient=user,
            url=url,
            text__startswith='🔥 Невзятая срочная',
        ).exists()
        if not already:
            Notification.objects.create(
                recipient=user,
                text=f'🔥 Невзятая срочная: {t.title} ({t.executor.full_name})',
                url=url,
                kind=Notification.Kind.ALERT,
            )
        minutes = int((timezone.now() - t.created_at).total_seconds() / 60)
        result.append({'task': t, 'minutes': minutes})

    return result


def _compute_load_rows(user):
    """Карточка загруженности отдела: «здесь и сейчас».

    Для каждого сотрудника считаем:
      needed_days = max(1, ceil(Σ plan_hours открытых / norm_hours_for(user)))
      window_days = work_days_between(today, крайний_due), минимум 1
      load_pct    = needed_days / window_days × 100%

    Крайний срок:
      - max(due) по задачам, у которых due >= today;
      - обрезка сверху today + 90 рабочих дней — чтобы одна «далёкая»
        задача не растянула окно до года и не сделала load ≈ 0;
      - если у сотрудника нет ни одной задачи с due >= today —
        окно = 30 рабочих дней (разумный дефолт).

    Возвращает (rows, totals):
      rows   — список dict по сотрудникам, сортировка по load убыв.
      totals — сводные цифры для плитки в кабинете.
    """
    from tasks.utils import add_work_days, norm_hours_for, work_days_between

    today = timezone.localdate()
    member_ids = _scope_user_ids(user)

    members = list(
        User.objects
        .filter(pk__in=member_ids, is_active=True)
        .select_related('department', 'role')
        .order_by('full_name')
    )

    open_tasks = list(
        Task.objects
        .filter(executor_id__in=member_ids)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .select_related('executor', 'executor__department')
    )

    by_user = {}
    for t in open_tasks:
        by_user.setdefault(t.executor_id, []).append(t)

    default_window_days = 30

    rows = []
    for m in members:
        user_tasks = by_user.get(m.pk, [])
        n_open = len(user_tasks)

        norm = norm_hours_for(m) or 8.0
        plan_sum = sum(t.plan_hours or 0 for t in user_tasks)

        if plan_sum <= 0:
            needed_days = 0
        else:
            needed_days = max(1, int(math.ceil(plan_sum / norm)))

        # График работы сотрудника: личный → отдела → старая логика.
        schedule = schedule_for_user(m)

        # Обрезка окна сверху — не даём одной «далёкой» задаче растянуть
        # окно на год. 90 рабочих дней считаем по графику сотрудника.
        max_window_date = add_work_days(today, 90, schedule=schedule)

        due_dates = [
            t.due for t in user_tasks
            if t.due and t.due >= today
        ]

        if due_dates:
            max_due = max(due_dates)
            if max_due > max_window_date:
                max_due = max_window_date
            window_days = work_days_between(today, max_due, schedule=schedule) or 1
            if window_days < 1:
                window_days = 1
        else:
            window_days = default_window_days

        if needed_days == 0:
            load_pct = 0
            cls = 'empty'
            label = 'нет задач'
        else:
            load_pct = int(round(needed_days / window_days * 100))
            if load_pct > 110:
                cls = 'over'
                label = 'перегруз'
            elif load_pct < 60:
                cls = 'under'
                label = 'есть резерв'
            else:
                cls = 'ok'
                label = 'норма'

        nearest_due = min(due_dates) if due_dates else None

        rows.append({
            'user': m,
            'n_open': n_open,
            'plan_sum': round(plan_sum, 1),
            'norm': round(norm, 1),
            'needed_days': needed_days,
            'window_days': window_days,
            'load_pct': load_pct,
            'cls': cls,
            'label': label,
            'reserve_days': window_days - needed_days,
            'nearest_due': nearest_due,
            'on_vacation': m.is_on_vacation,
            'vacation_label': m.vacation_label,
        })

    rows.sort(key=lambda r: (-r['load_pct'], r['user'].full_name))

    totals = {
        'members': len(members),
        'overloaded': sum(1 for r in rows if r['cls'] == 'over'),
        'underloaded': sum(1 for r in rows if r['cls'] == 'under'),
        'ok': sum(1 for r in rows if r['cls'] == 'ok'),
        'no_tasks': sum(1 for r in rows if r['cls'] == 'empty'),
        'total_open': sum(r['n_open'] for r in rows),
    }

    return rows, totals


# ─────────────────────────────────────────────────────────────
# Редиректы со старых URL
# ─────────────────────────────────────────────────────────────

@login_required
def manager_dashboard(request):
    return redirect(reverse('manager_cabinet'))


@login_required
def review(request):
    return redirect(f"{reverse('manager_cabinet')}#review")


@login_required
def reports(request):
    return redirect(f"{reverse('manager_cabinet')}#reports")


# ─────────────────────────────────────────────────────────────
# Командный центр руководителя
# ─────────────────────────────────────────────────────────────

@login_required
def cabinet(request):
    """Кабинет руководителя: сводка, алерты, топ проблем, приёмка, график."""
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    my_kpi = manager_kpi_percent(user) if user.department_id else None

    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    since30 = now - timedelta(days=30)

    scope = user.scope
    member_ids = _scope_user_ids(user)

    # ── Плитки сводки ─────────────────────────────────────────

    closed_qs = Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        finished_at__gte=month_start,
    )
    closed_month = closed_qs.count()
    plan_month = closed_qs.aggregate(p=Sum('plan_hours'))['p'] or 0
    fact_month = closed_qs.aggregate(f=Sum('accumulated_hours'))['f'] or 0

    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    prev_agg = Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        finished_at__gte=prev_start,
        finished_at__lt=month_start,
    ).aggregate(c=Count('id'), h=Sum('accumulated_hours'))

    delta_cnt = _delta(closed_month, prev_agg['c'] or 0)
    delta_hours = _delta(fact_month, prev_agg['h'] or 0)

    overdue_qs = Task.objects.filter(
        executor_id__in=member_ids,
        due__lt=today,
    ).exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    ).select_related('executor', 'executor__department')

    overdue_all = list(overdue_qs.order_by('due'))
    overdue_count = len(overdue_all)
    overdue_list = overdue_all[:3]

    # Приёмка — по scope
    review_qs = Task.objects.filter(
        status=Task.Status.REVIEW
    ).select_related('executor', 'requester')

    if scope == 'department' and user.department_id:
        review_qs = review_qs.filter(
            Q(executor__department_id=user.department_id) | Q(requester=user)
        )
    elif scope == 'self':
        review_qs = review_qs.filter(requester=user)

    review_count = review_qs.count()
    review_queue = list(review_qs.order_by('-finished_at')[:5])

    with_due = list(Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        due__isnull=False,
        finished_at__gte=since30,
    ))
    otd = (
        round(
            sum(1 for t in with_due if _local_date(t.finished_at) <= t.due)
            / len(with_due) * 100
        )
        if with_due else None
    )

    closed30 = list(Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        finished_at__gte=since30,
    ))
    cycles = [
        (_local_date(t.finished_at) - _local_date(t.created_at)).days
        for t in closed30
    ]
    cycle_overall = median(cycles) if cycles else None

    # ── Алерты ────────────────────────────────────────────────

    critical_overdue = [t for t in overdue_all if (today - t.due).days >= 7][:10]

    stale_qs = Task.objects.filter(
        executor_id__in=member_ids,
        status__in=[
            Task.Status.NEW, Task.Status.IN_PROGRESS,
            Task.Status.PAUSED, Task.Status.REWORK,
        ],
    ).select_related('executor')

    # Последняя активность по каждой задаче — один запрос на таблицу
    last_progress = dict(
        TaskProgress.objects.filter(task__executor_id__in=member_ids)
        .values('task_id').annotate(m=Max('created_at'))
        .values_list('task_id', 'm')
    )
    last_log = dict(
        TaskLog.objects.filter(task__executor_id__in=member_ids)
        .values('task_id').annotate(m=Max('created_at'))
        .values_list('task_id', 'm')
    )

    stale = []
    for t in stale_qs:
        candidates = [t.created_at]
        if last_progress.get(t.pk):
            candidates.append(last_progress[t.pk])
        if last_log.get(t.pk):
            candidates.append(last_log[t.pk])
        last = max(candidates)
        idle_days = (now - last).days
        if idle_days >= 14:
            stale.append({'task': t, 'idle_days': idle_days})
    stale.sort(key=lambda x: -x['idle_days'])
    stale = stale[:10]

    anomalies = list(TimeSession.objects.filter(
        executor_id__in=member_ids,
        finished_at__gte=month_start,
    ).filter(
        Q(duration_hours__gt=12)
        | Q(started_at__hour__gte=22)
        | Q(started_at__hour__lte=5)
    ).select_related('task', 'executor').order_by('-finished_at')[:8])

    # ── Топ проблем ───────────────────────────────────────────

    by_user_overdue = Counter(t.executor_id for t in overdue_all)
    top_overdue = []
    for uid, cnt in by_user_overdue.most_common(3):
        u = User.objects.filter(pk=uid).first()
        if u:
            top_overdue.append({'user': u, 'cnt': cnt})

    top_rework = list(
        TaskLog.objects.filter(
            kind=TaskLog.Kind.REWORK,
            created_at__gte=month_start,
            task__executor_id__in=member_ids,
        )
        .values('task__executor', 'task__executor__full_name')
        .annotate(cnt=Count('id'))
        .order_by('-cnt')[:3]
    )

    # ── Плитки для директора завода ───────────────────────────

    plant_alert = None
    top_deps_overdue = []
    if scope in ('admin', 'plant'):
        rows, _ = _plant_rows()
        red = [
            r for r in rows
            if (r['kpi'] is not None and r['kpi'] < 80)
               or (r['otd'] is not None and r['otd'] < 70)
        ]
        plant_alert = len(red)
        top_deps_overdue = sorted(rows, key=lambda r: -r['overdue'])[:3]

    # ── График 8 недель ───────────────────────────────────────

    weeks = []
    for i in range(7, -1, -1):
        w_start = today - timedelta(days=today.weekday() + 7 * i)
        w_end = w_start + timedelta(days=7)
        agg = Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.DONE,
            finished_at__date__gte=w_start,
            finished_at__date__lt=w_end,
        ).aggregate(c=Count('id'), h=Sum('accumulated_hours'))
        weeks.append({
            'label': w_start.strftime('%d.%m'),
            'cnt': agg['c'] or 0,
            'hours': round(agg['h'] or 0, 1),
        })
    max_cnt = max([w['cnt'] for w in weeks] + [1])
    for w in weeks:
        w['pct'] = int(w['cnt'] / max_cnt * 100)

    # ── Дайджест ──────────────────────────────────────────────

    digest = (
        f'Закрыто за месяц: {closed_month} · '
        f'Просрочено: {overdue_count} · '
        f'В приёмке: {review_count}'
    )
    if otd is not None:
        digest += f' · OTD: {otd}%'

        # Этапы, требующие решения по сдвигу (просрочен предыдущий)
    pending_shift_qs = (
        Task.objects
        .filter(blocked_by_stage=True, pending_shift_days__isnull=False)
        .select_related(
            'executor', 'branch', 'branch__order',
            'pending_shift_from_task',
        )
    )
    if scope == 'department' and user.department_id:
        pending_shift_qs = pending_shift_qs.filter(
            executor__department_id=user.department_id
        )
    elif scope == 'self':
        pending_shift_qs = pending_shift_qs.none()

    pending_shift = list(pending_shift_qs.order_by('due'))

    deferred_urgent = _deferred_urgent(member_ids)
    stuck_urgent = _check_stuck_urgent(member_ids, user)
    wasted_qs = _wasted_stats(member_ids, since=month_start)
    wasted = _wasted_totals(wasted_qs)
    # ── Топ виновников сдвигов за 30 дней ──
    shift_violators = _shift_violators(member_ids, since30)
    shift_initiators = _shift_initiators(member_ids, since30)

    # ── Уволенные с открытыми задачами ──
    from tasks.models import Task as _Task
    dismissed_with_tasks = []
    if scope in ('admin', 'plant'):
        dismissed_qs = User.objects.filter(
            employment_status=User.EmploymentStatus.DISMISSED
        )
    elif scope == 'department' and user.department_id:
        dismissed_qs = User.objects.filter(
            employment_status=User.EmploymentStatus.DISMISSED,
            department_id=user.department_id,
        )
    else:
        dismissed_qs = User.objects.none()

    for u in dismissed_qs:
        cnt = _Task.objects.filter(executor=u).exclude(
            status__in=[_Task.Status.DONE, _Task.Status.CANCELLED]
        ).count()
        if cnt:
            dismissed_with_tasks.append({'user': u, 'count': cnt})

    dismissed_with_tasks.sort(key=lambda x: -x['count'])

    # ── Плитка «перегружено» в кабинете ──
    _, load_totals = _compute_load_rows(user)

    return render(request, 'manager/cabinet.html', {
        'scope': scope,
        'load_overloaded': load_totals['overloaded'],
        'month_label': now.strftime('%B %Y'),
        'pending_shift': pending_shift,

        'closed_month': closed_month,
        'delta_cnt': delta_cnt,
        'delta_hours': delta_hours,
        'plan_month': plan_month,
        'fact_month': fact_month,
        'kpi_month': kpi_percent(plan_month, fact_month),

        'overdue_count': overdue_count,
        'overdue_list': overdue_list,

        'review_count': review_count,
        'review_queue': review_queue,

        'otd': otd,
        'cycle_overall': cycle_overall,

        'critical_overdue': critical_overdue,
        'stale': stale,
        'anomalies': anomalies,

        'top_overdue': top_overdue,
        'top_rework': top_rework,

        'weeks': weeks,
        'digest': digest,

        'plant_alert': plant_alert,
        'top_deps_overdue': top_deps_overdue,
        'deferred_urgent': deferred_urgent,
        'stuck_urgent': stuck_urgent,
        'wasted': wasted,
        'wasted_list': wasted_qs[:10],
        'dismissed_with_tasks': dismissed_with_tasks,
        'my_kpi': my_kpi,
        'shift_violators': shift_violators,
        'shift_initiators': shift_initiators,
    })


# ─────────────────────────────────────────────────────────────
# Команда
# ─────────────────────────────────────────────────────────────

@login_required
def team_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    norm = norm_hours()

    bd_elapsed = sum(
        1 for d in range(1, today.day + 1)
        if now.replace(day=d).weekday() < 5
    )
    capacity = norm * max(bd_elapsed, 1)

    member_ids = _scope_user_ids(user)
    members = list(
        User.objects.filter(id__in=member_ids).order_by('full_name')
    )

    open_tasks = list(
        Task.objects.filter(executor_id__in=member_ids)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .select_related('executor')
    )

    closed_agg = {
        r['executor']: r
        for r in Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.DONE,
            finished_at__gte=month_start,
        ).values('executor').annotate(
            hours=Sum('accumulated_hours'),
            plan=Sum('plan_hours'),
            cnt=Count('id'),
        )
    }

    # Возвраты — один запрос на всех
    rework_by_task = {
        r['task_id']: r['cnt']
        for r in TaskLog.objects.filter(
            kind=TaskLog.Kind.REWORK,
            task__executor_id__in=member_ids,
            created_at__gte=month_start,
        ).values('task_id').annotate(cnt=Count('id'))
    }

    # Задачи по пользователям, чтобы посчитать возвраты без N+1
    tasks_by_user = {}
    for tid, uid in Task.objects.filter(
            executor_id__in=member_ids
    ).values_list('id', 'executor_id'):
        tasks_by_user.setdefault(uid, []).append(tid)

    # План недели — один запрос
    monday = today - timedelta(days=today.weekday())
    commits_agg = {}
    for r in WeekCommit.objects.filter(
            user_id__in=member_ids, week_start=monday
    ).values('user_id', 'task__status').annotate(cnt=Count('id')):
        key = r['user_id']
        commits_agg.setdefault(key, {'total': 0, 'done': 0})
        commits_agg[key]['total'] += r['cnt']
        if r['task__status'] == Task.Status.DONE:
            commits_agg[key]['done'] += r['cnt']

    team = []
    for m in members:
        my_open = [t for t in open_tasks if t.executor_id == m.pk]
        ca = closed_agg.get(m.pk, {})
        hours = ca.get('hours') or 0

        closed_list = list(Task.objects.filter(
            executor=m, status=Task.Status.DONE, finished_at__gte=month_start
        ))
        with_due = [t for t in closed_list if t.due]
        otd = (
            round(
                sum(1 for t in with_due if _local_date(t.finished_at) <= t.due)
                / len(with_due) * 100
            )
            if with_due else None
        )

        rework_n = sum(
            rework_by_task.get(tid, 0)
            for tid in tasks_by_user.get(m.pk, [])
        )
        util = round(hours / capacity * 100) if capacity else None

        team.append({
            'user': m,
            'open': len(my_open),
            'progress': len([t for t in my_open if t.status == Task.Status.IN_PROGRESS]),
            'overdue': len([t for t in my_open if t.due and t.due < today]),
            'hours': hours,
            'kpi': user_kpi_percent(m),
            'otd': otd,
            'rework': rework_n,
            'util': util,
            'commit_n': commits_agg.get(m.pk, {}).get('total', 0),
            'commit_done': commits_agg.get(m.pk, {}).get('done', 0),
        })

    return render(request, 'manager/team.html', {
        'team': team,
        'totals': {
            'open': len(open_tasks),
            'rework': sum(t['rework'] for t in team),
        },
        'capacity': capacity,
        'month_label': now.strftime('%B %Y'),
        'scope': user.scope,
    })


@login_required
def workload_page(request):
    """Сетка «сотрудник × неделя»: плановые часы открытых задач.

    Помогает руководителю увидеть, кто перегружен, а кто свободен.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()

    # ── Период ──
    try:
        weeks_count = int(request.GET.get('weeks', 4))
    except (TypeError, ValueError):
        weeks_count = 4
    if weeks_count not in (4, 8):
        weeks_count = 4

    # Понедельник текущей недели
    this_monday = today - timedelta(days=today.weekday())
    # Сдвиг вперёд/назад по кнопкам «← / →»
    try:
        offset_weeks = int(request.GET.get('offset', 0))
    except (TypeError, ValueError):
        offset_weeks = 0
    offset_weeks = max(-26, min(52, offset_weeks))  # ±полгода — разумный предел

    start_monday = this_monday + timedelta(weeks=offset_weeks)
    # Последняя неделя (воскресенье)
    end_sunday = start_monday + timedelta(weeks=weeks_count) - timedelta(days=1)

    # Заголовки колонок — по одной на неделю
    week_cols = []
    for i in range(weeks_count):
        w_start = start_monday + timedelta(weeks=i)
        w_end = w_start + timedelta(days=6)
        week_cols.append({
            'index': i,
            'start': w_start,
            'end': w_end,
            'label': f'{w_start:%d.%m} – {w_end:%d.%m}',
            'is_current': w_start <= today <= w_end,
            'is_past': w_end < today,
        })

    # ── Сотрудники ──
    member_ids = _scope_user_ids(user)
    members = list(
        User.objects
        .filter(pk__in=member_ids, is_active=True)
        .select_related('department', 'role')
        .order_by('department__name', 'full_name')
    )

    # ── Задачи, попадающие в окно ──
    tasks = list(
        Task.objects
        .filter(
            executor_id__in=member_ids,
            status__in=[
                Task.Status.NEW, Task.Status.IN_PROGRESS,
                Task.Status.PAUSED, Task.Status.REWORK,
                Task.Status.REVIEW, Task.Status.PENDING_APPROVAL,
            ],
        )
        .exclude(plan_hours__lte=0)
        .select_related('executor', 'order')
    )

    norm = norm_hours()
    week_norm = norm * 5

    # ── Распределение часов по ячейкам ──
    # cells[user_id][week_index] = {
    #   'hours': X, 'tasks': [...], 'overdue_count': N,
    # }
    cells = {}
    for m in members:
        cells[m.pk] = {
            i: {'hours': 0.0, 'tasks': [], 'overdue_count': 0}
            for i in range(weeks_count)
        }

    for t in tasks:
        # Интервал задачи (календарные дни)
        t_start = t.start_due or _local_date(t.created_at) or today
        t_end = t.due or t.start_due or t_start

        if t_end < t_start:
            t_start, t_end = t_end, t_start

        # Если интервал не пересекается с окном — пропуск
        if t_end < start_monday or t_start > end_sunday:
            # но если due < сегодня — это просрочка, хотим её видеть
            if t.due and t.due < today:
                # просроченную задачу кладём в первую неделю окна
                t_start = start_monday
                t_end = start_monday
            else:
                continue

        # Просрочка на сегодня
        is_overdue = bool(t.due and t.due < today)

        # Длительность в календарных днях (включительно)
        total_days = (t_end - t_start).days + 1

        # Часы на день = равномерно
        hours_per_day = (t.plan_hours or 0) / total_days if total_days > 0 else 0

        # Проходим по каждой неделе окна и считаем пересечение
        for wi, col in enumerate(week_cols):
            w_start = col['start']
            w_end = col['end']

            # Пересечение интервала задачи с неделей
            overlap_start = max(t_start, w_start)
            overlap_end = min(t_end, w_end)

            if overlap_start > overlap_end:
                continue

            overlap_days = (overlap_end - overlap_start).days + 1
            hours = hours_per_day * overlap_days

            cell = cells[t.executor_id][wi]
            cell['hours'] += hours
            cell['tasks'].append({
                'task': t,
                'hours': round(hours, 1),
                'is_overdue': is_overdue,
            })
            if is_overdue:
                cell['overdue_count'] += 1

    # ── Собираем строки ──
    rows = []
    for m in members:
        row_cells = []
        row_total = 0.0
        for wi, col in enumerate(week_cols):
            cell = cells[m.pk][wi]
            hours = round(cell['hours'], 1)
            row_total += hours

            # Процент загрузки от недельной нормы
            pct = int(round(hours / week_norm * 100)) if week_norm > 0 else 0

            # Класс для цвета
            if hours <= 0:
                cls = 'empty'
            elif pct > 110:
                cls = 'over'
            elif pct < 60:
                cls = 'under'
            else:
                cls = 'ok'

            # Просрочка подсвечивает строку недели отдельным бейджем
            row_cells.append({
                'week_index': wi,
                'hours': hours,
                'pct': pct,
                'cls': cls,
                'tasks_count': len(cell['tasks']),
                'overdue_count': cell['overdue_count'],
                'tasks': cell['tasks'],
                'is_past': col['is_past'],
            })

        rows.append({
            'user': m,
            'cells': row_cells,
            'total_hours': round(row_total, 1),
            'avg_pct': int(round(row_total / (week_norm * weeks_count) * 100))
            if week_norm > 0 and weeks_count else 0,
        })

    # ── Итоги по неделям (нижняя строка) ──
    week_totals = []
    for wi in range(weeks_count):
        hours = round(sum(
            cells[m.pk][wi]['hours'] for m in members
        ), 1)
        pct = int(round(hours / (week_norm * len(members)) * 100)) \
            if week_norm > 0 and members else 0
        week_totals.append({'hours': hours, 'pct': pct})

    # ── Прав/лево для навигации ──
    prev_offset = offset_weeks - weeks_count
    next_offset = offset_weeks + weeks_count

    return render(request, 'manager/workload.html', {
        'today': today,
        'week_cols': week_cols,
        'rows': rows,
        'week_totals': week_totals,
        'weeks_count': weeks_count,
        'offset_weeks': offset_weeks,
        'prev_offset': prev_offset,
        'next_offset': next_offset,
        'start_monday': start_monday,
        'end_sunday': end_sunday,
        'week_norm': week_norm,
        'norm': norm,
        'total_members': len(members),
        'scope': user.scope,
    })


@login_required
def load_page(request):
    """Карточка «Загруженность отдела»: одна цифра на сотрудника.

    В отличие от /manager/workload/ (сетка «сотрудник × неделя»),
    эта страница отвечает на вопрос «кто завален, а кто свободен»
    за 5 секунд, без разглядывания сетки.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    rows, totals = _compute_load_rows(user)

    return render(request, 'manager/load.html', {
        'rows': rows,
        'totals': totals,
        'today': timezone.localdate(),
        'scope': user.scope,
        'page_title': 'Загруженность отдела',
    })


# ─────────────────────────────────────────────────────────────
# Сверхурочные
# ─────────────────────────────────────────────────────────────

def _parse_month_param(request):
    """Разбирает ?month=YYYY-MM. По умолчанию — текущий месяц.

    Возвращает (year, month, first_day, last_day).
    """
    import calendar as cal_mod

    today = timezone.localdate()
    raw = (request.GET.get('month') or '').strip()

    y, m = today.year, today.month
    if raw:
        try:
            y_s, m_s = raw.split('-')
            yy, mm = int(y_s), int(m_s)
            if 1 <= mm <= 12 and 2000 <= yy <= 2100:
                y, m = yy, mm
        except (ValueError, AttributeError):
            pass

    last_day = cal_mod.monthrange(y, m)[1]
    first = timezone.datetime(y, m, 1).date()
    last = timezone.datetime(y, m, last_day).date()
    return y, m, first, last


def _overtime_scope_user_ids(user):
    """Кому user может вносить сверхурочные.

    - суперюзер / админ портала: все активные;
    - начальник завода (can_plant): НИКОМУ — он только смотрит свод;
    - руководитель отдела с department_id: только свой отдел;
    - все прочие: никому.
    """
    if user.is_superuser or user.is_admin_role:
        return list(User.objects.filter(is_active=True).values_list('id', flat=True))

    # Начальник завода / директор — только чтение, не вносит.
    if user.can_plant:
        return []

    if user.is_boss and user.department_id:
        return list(
            User.objects.filter(
                department_id=user.department_id, is_active=True
            ).values_list('id', flat=True)
        )

    return []


def _prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _overtime_back(request):
    """Возврат на /manager/overtime/ с сохранением месяца и фильтра.

    Читает из POST и GET (POST приоритетнее). Возвращает URL с query.
    """
    back = reverse('manager_overtime')
    month = (request.POST.get('month') or request.GET.get('month') or '').strip()
    user_pk = (
            request.POST.get('scope_user') or request.GET.get('user') or ''
    ).strip()

    params = []
    if month:
        params.append(f'month={month}')
    if user_pk and user_pk.isdigit():
        params.append(f'user={user_pk}')

    if params:
        back += '?' + '&'.join(params)
    return back

@login_required
def overtime_page(request):
    """Сверхурочные отдела: список сотрудников + журнал записей.

    Доступно руководителю отдела (is_boss с department_id) и админу портала.
    Начальник завода сюда не ходит — он видит свод на /manager/plant/.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    y, m, first, last = _parse_month_param(request)
    member_ids = _overtime_scope_user_ids(user)

    if not member_ids:
        raise PermissionDenied(
            'Вносить сверхурочные может только руководитель отдела.'
        )

    scope_members = list(
        User.objects
        .filter(pk__in=member_ids, is_active=True)
        .select_related('department', 'role')
        .order_by('full_name')
    )

    # ── Фильтр по конкретному сотруднику ──
    user_filter = (request.GET.get('user') or '').strip()
    filter_user_id = None
    if user_filter.isdigit():
        candidate = int(user_filter)
        if candidate in set(member_ids):
            filter_user_id = candidate

    if filter_user_id is not None:
        members = [m for m in scope_members if m.pk == filter_user_id]
        target_ids = [filter_user_id]
    else:
        members = scope_members
        target_ids = member_ids

    qs = (
        OvertimeRecord.objects
        .filter(user_id__in=target_ids, date__gte=first, date__lte=last)
        .select_related('user', 'user__department', 'task', 'created_by')
        .order_by('-date', '-created_at')
    )

    by_user = {}
    for r in qs:
        by_user.setdefault(r.user_id, []).append(r)

    # Для формы создания — открытые задачи по каждому сотруднику scope
    open_tasks_by_user = {}
    for t in (
            Task.objects
                    .filter(executor_id__in=member_ids)
                    .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
                    .select_related('executor')
                    .order_by('executor_id', 'due', '-created_at')
    ):
        open_tasks_by_user.setdefault(t.executor_id, []).append(t)

    rows = []
    for mem in members:
        recs = by_user.get(mem.pk, [])
        total = round(sum(r.hours or 0 for r in recs), 1)
        last_date = recs[0].date if recs else None
        rows.append({
            'user': mem,
            'records': recs,
            'count': len(recs),
            'total_hours': total,
            'last_date': last_date,
            'open_tasks': open_tasks_by_user.get(mem.pk, []),
        })

    rows.sort(key=lambda r: (-r['total_hours'], r['user'].full_name))

    totals = {
        'hours': round(sum(r['total_hours'] for r in rows), 1),
        'records': sum(r['count'] for r in rows),
        'people': sum(1 for r in rows if r['count']),
        'members': len(scope_members),
    }

    prev_y, prev_m = _prev_month(y, m)
    next_y, next_m = _next_month(y, m)

    return render(request, 'manager/overtime.html', {
        'page_title': 'Сверхурочные',
        'y': y,
        'm': m,
        'month_label': first.strftime('%B %Y'),
        'prev': {'y': prev_y, 'm': prev_m},
        'next': {'y': next_y, 'm': next_m},
        'rows': rows,
        'totals': totals,
        'members': scope_members,
        'filter_user_id': filter_user_id,
        'today': timezone.localdate(),
        'scope': user.scope,
    })


@login_required
@require_POST
def overtime_create(request):
    """Создать запись о сверхурочной.

    Право: is_boss (руководитель отдела или админ портала).
    Сотрудник должен быть в scope создателя.
    """
    user = request.user

    overtime_ids = _overtime_scope_user_ids(user)
    if not overtime_ids:
        raise PermissionDenied(
            'Вносить сверхурочные может только руководитель отдела.'
        )
    member_ids = set(overtime_ids)

    user_pk = (request.POST.get('user') or '').strip()
    if not user_pk.isdigit():
        messages.error(request, 'Выберите сотрудника.')
        return redirect('manager_overtime')

    target = User.objects.filter(pk=int(user_pk), is_active=True).first()
    if not target:
        messages.error(request, 'Сотрудник не найден.')
        return redirect('manager_overtime')

    if target.pk not in member_ids:
        messages.error(request, 'Этот сотрудник вне вашей зоны ответственности.')
        return redirect('manager_overtime')

    date_str = (request.POST.get('date') or '').strip()
    try:
        rec_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        messages.error(request, 'Некорректная дата.')
        return redirect('manager_overtime')

    hours_raw = (request.POST.get('hours') or '').strip().replace(',', '.')
    try:
        hours = float(hours_raw)
    except ValueError:
        messages.error(request, 'Некорректное число часов.')
        return redirect('manager_overtime')

    if hours <= 0:
        messages.error(request, 'Часы должны быть больше нуля.')
        return redirect('manager_overtime')

    if hours > 24:
        messages.error(request, 'Часов не может быть больше 24 в сутки.')
        return redirect('manager_overtime')

    reason = (request.POST.get('reason') or '').strip()[:500]

    task_pk = (request.POST.get('task') or '').strip()
    task = None
    if task_pk.isdigit():
        task = Task.objects.filter(
            pk=int(task_pk), executor=target,
        ).first()

    order_file = request.FILES.get('order_file')

    OvertimeRecord.objects.create(
        user=target,
        department=target.department,
        task=task,
        date=rec_date,
        hours=hours,
        reason=reason,
        order_file=order_file,
        created_by=user,
    )

    messages.success(
        request,
        f'Сверхурочная внесена: {target.full_name} · '
        f'{rec_date:%d.%m.%Y} · {hours:.1f} ч.',
    )

    # Если пришли с карточки задачи — вернуться туда
    next_task = (request.POST.get('next_task') or '').strip()
    if next_task.isdigit():
        return redirect('task_detail', pk=int(next_task))

    return redirect(_overtime_back(request))


@login_required
@require_POST
def overtime_delete(request, pk):
    """Удалить запись о сверхурочной.

    Право: is_boss (руководитель отдела или админ портала).
    Только для записей сотрудников в своём scope.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Только руководитель может удалять сверхурочные.')

    rec = get_object_or_404(OvertimeRecord, pk=pk)

    if rec.user_id not in set(_overtime_scope_user_ids(user)):
        raise PermissionDenied('Эта запись вне вашей зоны ответственности.')

    info = f'{rec.user.full_name} · {rec.date:%d.%m.%Y} · {rec.hours:.1f} ч'
    rec.delete()

    messages.success(request, f'Запись удалена: {info}.')

    return redirect(_overtime_back(request))


@login_required
@require_POST
def overtime_edit(request, pk):
    """Изменить существующую запись о сверхурочной.

    Право: is_boss и запись по сотруднику в его scope.
    Сотрудник (user) не меняется: если ошиблись адресатом —
    удалите запись и создайте новую.
    Новый файл приказа — заменяет старый, если приложен.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Только руководитель может изменять сверхурочные.')

    overtime_ids = _overtime_scope_user_ids(user)
    if not overtime_ids:
        raise PermissionDenied(
            'Раздел доступен только руководителю отдела.'
        )

    rec = get_object_or_404(OvertimeRecord, pk=pk)

    if rec.user_id not in set(overtime_ids):
        raise PermissionDenied('Эта запись вне вашей зоны ответственности.')

    # ── Валидация ──
    date_str = (request.POST.get('date') or '').strip()
    try:
        rec_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        messages.error(request, 'Некорректная дата.')
        return redirect(_overtime_back(request))

    hours_raw = (request.POST.get('hours') or '').strip().replace(',', '.')
    try:
        hours = float(hours_raw)
    except ValueError:
        messages.error(request, 'Некорректное число часов.')
        return redirect(_overtime_back(request))

    if hours <= 0:
        messages.error(request, 'Часы должны быть больше нуля.')
        return redirect(_overtime_back(request))

    if hours > 24:
        messages.error(request, 'Часов не может быть больше 24 в сутки.')
        return redirect(_overtime_back(request))

    reason = (request.POST.get('reason') or '').strip()[:500]

    task_pk = (request.POST.get('task') or '').strip()
    task = None
    if task_pk.isdigit():
        task = Task.objects.filter(
            pk=int(task_pk), executor=rec.user,
        ).first()

    rec.date = rec_date
    rec.hours = hours
    rec.reason = reason
    rec.task = task

    new_file = request.FILES.get('order_file')
    if new_file:
        rec.order_file = new_file

    rec.save()

    messages.success(
        request,
        f'Запись обновлена: {rec.user.full_name} · '
        f'{rec.date:%d.%m.%Y} · {rec.hours:.1f} ч.',
    )
    return redirect(_overtime_back(request))


@login_required
def overtime_csv(request):
    """CSV-выгрузка сверхурочных за месяц с учётом scope и фильтра.

    Формат: UTF-8 с BOM + разделитель «;» — открывается в Excel как есть.
    Права те же, что у страницы: руководитель отдела — свой отдел,
    админ портала — все, начальник завода — не имеет доступа.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    member_ids = _overtime_scope_user_ids(user)
    if not member_ids:
        raise PermissionDenied(
            'Раздел доступен только руководителю отдела.'
        )

    y, m, first, last = _parse_month_param(request)

    # Фильтр по сотруднику (тот же, что и на странице)
    user_filter = (request.GET.get('user') or '').strip()
    filter_user_id = None
    if user_filter.isdigit():
        candidate = int(user_filter)
        if candidate in set(member_ids):
            filter_user_id = candidate

    qs = (
        OvertimeRecord.objects
        .filter(
            user_id__in=([filter_user_id] if filter_user_id else member_ids),
            date__gte=first,
            date__lte=last,
        )
        .select_related('user', 'user__department', 'task', 'created_by')
        .order_by('date', 'user__full_name')
    )

    response = HttpResponse(
        content_type='text/csv; charset=utf-8-sig',
    )
    fname = f'overtime_{y}_{m:02d}'
    if filter_user_id:
        fname += f'_user{filter_user_id}'
    response['Content-Disposition'] = (
        f'attachment; filename="{fname}.csv"'
    )
    response.write('\ufeff')

    w = _csv.writer(response, delimiter=';')
    w.writerow([
        'Дата', 'Сотрудник', 'Отдел', 'Часы',
        'Задача', 'Причина', 'Кто внёс', 'Когда внесено',
    ])

    for r in qs:
        dep_name = ''
        if r.department:
            dep_name = r.department.name
        elif r.user.department:
            dep_name = r.user.department.name

        task_label = ''
        if r.task:
            task_label = f'#{r.task.pk} {r.task.title}'

        w.writerow([
            r.date.strftime('%d.%m.%Y'),
            r.user.full_name,
            dep_name,
            f'{r.hours:.2f}'.replace('.', ','),
            task_label,
            (r.reason or '').replace('\n', ' '),
            r.created_by.full_name if r.created_by else '',
            r.created_at.strftime('%d.%m.%Y %H:%M') if r.created_at else '',
        ])

    # Итоговая строка
    total = round(sum(r.hours or 0 for r in qs), 2)
    w.writerow([])
    w.writerow([
        'ИТОГО', '', '', f'{total:.2f}'.replace('.', ','),
        '', '', '', '',
    ])

    return response


# ─────────────────────────────────────────────────────────────
# Приёмка
# ─────────────────────────────────────────────────────────────

@login_required
def review_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    member_ids = _scope_user_ids(user)
    scope = user.scope

    review_qs = Task.objects.filter(
        status=Task.Status.REVIEW
    ).select_related('executor', 'requester')

    if scope == 'department':
        review_qs = review_qs.filter(
            Q(executor__department_id=user.department_id) | Q(requester=user)
        )
    elif scope == 'self':
        review_qs = review_qs.filter(requester=user)

    review_list = list(review_qs.order_by('-finished_at'))
    for t in review_list:
        t.spent_text = f'{(t.accumulated_hours or 0):.2f} ч'

    rework_reasons = list(
        TaskLog.objects.filter(
            kind=TaskLog.Kind.REWORK,
            created_at__gte=month_start,
            task__executor_id__in=member_ids,
        ).select_related('task', 'author').order_by('-created_at')[:10]
    )

    return render(request, 'manager/review.html', {
        'review_list': review_list,
        'rework_reasons': rework_reasons,
        'month_label': now.strftime('%B %Y'),
        'scope': scope,
    })


# ─────────────────────────────────────────────────────────────
# Поток
# ─────────────────────────────────────────────────────────────

@login_required
def flow_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    today = timezone.localdate()
    member_ids = _scope_user_ids(user)

    open_tasks = list(
        Task.objects.filter(executor_id__in=member_ids)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
    )

    buckets = {'0–3': 0, '4–7': 0, '8–21': 0, '22+': 0}
    for t in open_tasks:
        age = (today - _local_date(t.created_at)).days
        if age <= 3:
            buckets['0–3'] += 1
        elif age <= 7:
            buckets['4–7'] += 1
        elif age <= 21:
            buckets['8–21'] += 1
        else:
            buckets['22+'] += 1

    weeks = []
    for i in range(7, -1, -1):
        w_start = today - timedelta(days=today.weekday() + 7 * i)
        w_end = w_start + timedelta(days=7)
        agg = Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.DONE,
            finished_at__date__gte=w_start,
            finished_at__date__lt=w_end,
        ).aggregate(c=Count('id'), h=Sum('accumulated_hours'))
        weeks.append({
            'label': w_start.strftime('%d.%m'),
            'cnt': agg['c'] or 0,
            'hours': round(agg['h'] or 0, 1),
        })

    max_cnt = max([w['cnt'] for w in weeks] + [1])
    for w in weeks:
        w['pct'] = int(w['cnt'] / max_cnt * 100)

    return render(request, 'manager/flow.html', {
        'buckets': buckets,
        'weeks': weeks,
        'month_label': now.strftime('%B %Y'),
        'scope': user.scope,
    })


# ─────────────────────────────────────────────────────────────
# Динамика
# ─────────────────────────────────────────────────────────────

@login_required
def dept_dynamics(request):
    """Динамика отдела по ежедневным срезам DailyDeptMetrics.

    Scope:
      - department manager — только свой отдел
      - plant/admin       — все отделы + селектор
    """
    from tasks.models import DailyDeptMetrics

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()

    # ── Период ──
    try:
        days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90):
        days = 30
    start = today - timedelta(days=days - 1)

    # ── Список отделов по scope ──
    if user.scope in ('admin', 'plant'):
        dept_qs = (
            Department.objects
            .filter(daily_metrics__isnull=False)
            .distinct()
            .order_by('name')
        )
    elif user.department_id:
        dept_qs = Department.objects.filter(pk=user.department_id)
    else:
        dept_qs = Department.objects.none()

    # ── Выбранный отдел ──
    dept_pk = request.GET.get('dept', '').strip()
    selected_dept = None
    if dept_pk.isdigit():
        selected_dept = dept_qs.filter(pk=int(dept_pk)).first()
    if not selected_dept:
        selected_dept = dept_qs.first()

    if not selected_dept:
        return render(request, 'manager/dept_dynamics.html', {
            'days': days,
            'today': today,
            'departments': dept_qs,
            'selected_dept': None,
            'rows': [],
            'grand': None,
            'is_full_scope': user.scope in ('admin', 'plant'),
            'active': 'dynamics',
        })

    # ── Данные за период ──
    qs = (
        DailyDeptMetrics.objects
        .filter(department=selected_dept, date__gte=start, date__lte=today)
        .order_by('date')
    )
    by_date = {m.date: m for m in qs}

    # Заполняем пустые дни — чтобы график не рвался
    rows = []
    cur = start
    while cur <= today:
        m = by_date.get(cur)
        rows.append({
            'date': cur,
            'metrics': m,
            'done': m.tasks_done_today if m else 0,
            'open': m.tasks_open if m else None,
            'overdue': m.tasks_overdue if m else None,
            'plan_open': round(m.plan_hours_open or 0, 1) if m else None,
            'fact': round(m.fact_hours_today or 0, 2) if m else 0,
            'shop': round(m.shop_hours_today or 0, 2) if m else 0,
            'sessions': m.sessions_count if m else 0,
            'employees': m.employees_active if m else 0,
        })
        cur += timedelta(days=1)

    # ── Итоги ──
    grand = {
        'done': sum(r['done'] for r in rows),
        'fact': round(sum(r['fact'] for r in rows), 1),
        'shop': round(sum(r['shop'] for r in rows), 1),
        'sessions': sum(r['sessions'] for r in rows),
        'days_with_data': sum(1 for r in rows if r['metrics'] is not None),
        'avg_done': (
            round(sum(r['done'] for r in rows) / max(1, days), 1)
        ),
        'avg_fact': (
            round(sum(r['fact'] for r in rows) / max(1, days), 2)
        ),
    }
    # последний срез — для «сейчас открытых / просрочено»
    last_with_data = next(
        (r for r in reversed(rows) if r['metrics'] is not None), None,
    )
    grand['open_now'] = last_with_data['open'] if last_with_data else None
    grand['overdue_now'] = last_with_data['overdue'] if last_with_data else None

    # ── Максимумы для графиков ──
    max_done = max([r['done'] for r in rows] + [1])
    for r in rows:
        r['done_pct'] = int(r['done'] / max_done * 100)

    max_hours = max(
        [max(r['fact'], r['shop']) for r in rows] + [1.0]
    )
    for r in rows:
        r['fact_pct'] = int(r['fact'] / max_hours * 100)
        r['shop_pct'] = int(r['shop'] / max_hours * 100)

    return render(request, 'manager/dept_dynamics.html', {
        'days': days,
        'today': today,
        'start': start,
        'departments': dept_qs,
        'selected_dept': selected_dept,
        'rows': rows,
        'grand': grand,
        'is_full_scope': user.scope in ('admin', 'plant'),
        'max_done': max_done,
        'max_hours': max_hours,
        'active': 'dynamics',
    })

@login_required
@require_POST
def capture_dept_snapshot(request):
    """Собрать срез DailyDeptMetrics прямо сейчас.

    Запускает management-команду capture_daily_metrics.
    Только для can_plant / is_admin_role — обычный руководитель
    отдела смотрит готовое.
    """
    from io import StringIO
    from django.core.management import call_command

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен руководителю завода.')

    buf = StringIO()
    try:
        call_command('capture_daily_metrics', stdout=buf)
        output = buf.getvalue().strip() or 'Готово.'
        messages.success(request, f'Срез обновлён. {output}')
    except Exception as e:
        messages.error(request, f'Ошибка сбора среза: {e}')

    # Возврат на ту же страницу с теми же параметрами
    days = request.POST.get('days', '30')
    dept = request.POST.get('dept', '')
    back = reverse('manager_dept_dynamics') + f'?days={days}'
    if dept:
        back += f'&dept={dept}'
    return redirect(back)


@login_required
def dept_dynamics_csv(request):
    """CSV-экспорт динамики отдела."""
    import csv
    from tasks.models import DailyDeptMetrics

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()
    try:
        days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90):
        days = 30
    start = today - timedelta(days=days - 1)

    if user.scope in ('admin', 'plant'):
        dept_qs = Department.objects.filter(daily_metrics__isnull=False).distinct()
    elif user.department_id:
        dept_qs = Department.objects.filter(pk=user.department_id)
    else:
        dept_qs = Department.objects.none()

    dept_pk = request.GET.get('dept', '').strip()
    selected_dept = None
    if dept_pk.isdigit():
        selected_dept = dept_qs.filter(pk=int(dept_pk)).first()
    if not selected_dept:
        selected_dept = dept_qs.first()

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fname = f'dept_dynamics_{selected_dept.pk if selected_dept else "all"}_{today:%Y%m%d}.csv'
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    response.write('\ufeff')

    w = csv.writer(response, delimiter=';')
    w.writerow([f'Динамика отдела: {selected_dept.name if selected_dept else "—"}'])
    w.writerow(['Период', f'{start:%d.%m.%Y} — {today:%d.%m.%Y}'])
    w.writerow([])

    if not selected_dept:
        w.writerow(['Нет данных'])
        return response

    w.writerow([
        'Дата', 'Открыто', 'Просрочено', 'Закрыто за день',
        'План открытых, ч', 'Факт за день, ч', 'Цех за день, ч',
        'Сеансов', 'Сотрудников работало',
    ])

    qs = DailyDeptMetrics.objects.filter(
        department=selected_dept, date__gte=start, date__lte=today,
    ).order_by('date')

    for m in qs:
        w.writerow([
            m.date.strftime('%d.%m.%Y'),
            m.tasks_open,
            m.tasks_overdue,
            m.tasks_done_today,
            f'{m.plan_hours_open:.1f}'.replace('.', ','),
            f'{m.fact_hours_today:.2f}'.replace('.', ','),
            f'{m.shop_hours_today:.2f}'.replace('.', ','),
            m.sessions_count,
            m.employees_active,
        ])

    return response


@login_required
def analytics_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    member_ids = _scope_user_ids(user)

    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    prev_agg = Task.objects.filter(
        executor_id__in=member_ids, status=Task.Status.DONE,
        finished_at__gte=prev_start, finished_at__lt=month_start,
    ).aggregate(c=Count('id'), h=Sum('accumulated_hours'))

    cur_agg = Task.objects.filter(
        executor_id__in=member_ids, status=Task.Status.DONE,
        finished_at__gte=month_start,
    ).aggregate(c=Count('id'), h=Sum('accumulated_hours'))

    deltas = {
        'cnt': _delta(cur_agg['c'] or 0, prev_agg['c'] or 0),
        'hours': _delta(cur_agg['h'] or 0, prev_agg['h'] or 0),
    }

    late_weeks = []
    for i in range(7, -1, -1):
        w_start = today - timedelta(days=today.weekday() + 7 * i)
        w_end = w_start + timedelta(days=7)
        n = Task.objects.filter(
            executor_id__in=member_ids, status=Task.Status.DONE,
            due__isnull=False, finished_at__date__gte=w_start,
            finished_at__date__lt=w_end,
        ).annotate(fd=TruncDate('finished_at')).filter(fd__gt=F('due')).count()
        late_weeks.append({'label': w_start.strftime('%d.%m'), 'n': n})

    max_late = max([w['n'] for w in late_weeks] + [1])
    for w in late_weeks:
        w['pct'] = int(w['n'] / max_late * 100)

    closed_month_qs = list(
        Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.DONE,
            finished_at__gte=month_start,
        )
    )

    cycle = []
    for k in [c[0] for c in Task.Kind.choices]:
        ds = [
            (_local_date(t.finished_at) - _local_date(t.created_at)).days
            for t in closed_month_qs if t.kind == k
        ]
        if ds:
            cycle.append({'label': Task.Kind(k).label, 'median': median(ds), 'n': len(ds)})

    all_ds = [
        (_local_date(t.finished_at) - _local_date(t.created_at)).days
        for t in closed_month_qs
    ]
    cycle_overall = median(all_ds) if all_ds else None

    acc_rows = []
    for k in [c[0] for c in Task.Kind.choices]:
        ms = [t for t in closed_month_qs if t.plan_hours and t.accumulated_hours and t.kind == k]
        if ms:
            good = sum(
                1 for t in ms
                if abs(t.accumulated_hours - t.plan_hours) / t.plan_hours <= 0.2
            )
            acc_rows.append({
                'label': Task.Kind(k).label,
                'pct': round(good / len(ms) * 100),
                'n': len(ms),
                'revise': good / len(ms) < 0.5,
            })

    anom_qs = TimeSession.objects.filter(
        executor_id__in=member_ids, finished_at__gte=month_start
    ).filter(
        Q(duration_hours__gt=12)
        | Q(started_at__hour__gte=22)
        | Q(started_at__hour__lte=5)
    ).select_related('task', 'executor')

    return render(request, 'manager/analytics.html', {
        'deltas': deltas,
        'late_weeks': late_weeks,
        'cycle': cycle,
        'cycle_overall': cycle_overall,
        'acc_rows': acc_rows,
        'anomalies': list(anom_qs.order_by('-finished_at')[:8]),
        'anomalies_count': anom_qs.count(),
        'month_label': now.strftime('%B %Y'),
        'scope': user.scope,
        'active': 'analytics',
    })


# ─────────────────────────────────────────────────────────────
# Отчёт за месяц
# ─────────────────────────────────────────────────────────────

@login_required
def reports_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    member_ids = _scope_user_ids(user)
    members = list(User.objects.filter(id__in=member_ids).order_by('full_name'))

    task_aggs = {
        r['executor']: r
        for r in Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.DONE,
            finished_at__gte=month_start,
        ).values('executor').annotate(
            plan=Sum('plan_hours'),
            fact=Sum('accumulated_hours'),
            cnt=Count('id'),
        )
    }

    shop_by_user = {}
    for s in ShopSession.objects.filter(
            executor_id__in=member_ids, started_at__gte=month_start
    ):
        shop_by_user.setdefault(s.executor_id, 0.0)
        if s.finished_at:
            shop_by_user[s.executor_id] += s.duration_hours
        else:
            shop_by_user[s.executor_id] += (now - s.started_at).total_seconds() / 3600

    reports_rows = []
    for m in members:
        agg = task_aggs.get(m.pk, {})
        plan = agg.get('plan') or 0
        fact = agg.get('fact') or 0
        reports_rows.append({
            'user': m,
            'cnt': agg.get('cnt') or 0,
            'plan': plan,
            'fact': fact,
            'shop': shop_by_user.get(m.pk, 0.0),
            'kpi': kpi_percent(plan, fact),
        })

    totals = {
        'cnt': sum(r['cnt'] for r in reports_rows),
        'plan': sum(r['plan'] for r in reports_rows),
        'fact': sum(r['fact'] for r in reports_rows),
        'shop': sum(r['shop'] for r in reports_rows),
    }
    totals['kpi'] = kpi_percent(totals['plan'], totals['fact'])
    wasted_qs = _wasted_stats(member_ids, since=month_start)
    wasted = _wasted_totals(wasted_qs)

    return render(request, 'manager/reports.html', {
        'reports': reports_rows,
        'totals': totals,
        'month_label': now.strftime('%B %Y'),
        'active': 'month',
        'cur_year': now.year,
        'years_available': list(range(2026, now.year + 1)),
        'scope': user.scope,
        'wasted': wasted,
    })


# ─────────────────────────────────────────────────────────────
# Благодарности
# ─────────────────────────────────────────────────────────────

@login_required
def kudos_send(request):
    if request.method != 'POST':
        return redirect('manager_cabinet')

    if not request.user.is_boss:
        messages.error(request, 'Только руководитель может отправлять благодарности.')
        return redirect('manager_cabinet')

    to_user_id = request.POST.get('to_user')
    text = request.POST.get('text', '').strip()

    if not to_user_id or not text:
        messages.error(request, 'Выберите сотрудника и введите текст благодарности.')
        return redirect(_safe_redirect_url(request))

    if not str(to_user_id).isdigit():
        messages.error(request, 'Некорректный сотрудник.')
        return redirect(_safe_redirect_url(request))

    to_user = get_object_or_404(User, pk=to_user_id, is_active=True)

    # Нельзя благодарить вне своего scope
    if to_user.pk not in set(_scope_user_ids(request.user)) and to_user.pk != request.user.pk:
        messages.error(request, 'Этот сотрудник вне вашей зоны ответственности.')
        return redirect(_safe_redirect_url(request))

    if to_user == request.user:
        messages.error(request, 'Нельзя благодарить самого себя.')
        return redirect(_safe_redirect_url(request))

    from gamify.models import Kudos
    Kudos.objects.create(from_user=request.user, to_user=to_user, text=text)
    messages.success(request, f'Благодарность отправлена для {to_user.full_name}.')
    return redirect(_safe_redirect_url(request))


# ─────────────────────────────────────────────────────────────
# Заказы
# ─────────────────────────────────────────────────────────────

@login_required
def orders_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    # ═══════════ POST: создание заказа ═══════════
    if request.method == 'POST':
        number = request.POST.get('number', '').strip()
        product = request.POST.get('product', '').strip()

        owner_pk = (request.POST.get('owner') or '').strip()
        owner = None
        if owner_pk.isdigit():
            owner = User.objects.filter(pk=int(owner_pk), is_active=True).first()

        contract_start = _parse_date(request.POST.get('contract_start', ''))
        design_start   = _parse_date(request.POST.get('design_start', ''))
        design_end     = _parse_date(request.POST.get('design_end', ''))
        ship_due       = _parse_date(request.POST.get('ship_due', ''))
        comment        = request.POST.get('comment', '').strip()

        errs = []
        if not number:
            errs.append('Укажите номер заказа.')
        if not product:
            errs.append('Укажите изделие.')
        elif Order.objects.filter(number__iexact=number).exists():
            errs.append('Заказ с таким номером уже есть.')
        if not owner:
            errs.append('Выберите ответственного за заказ — он будет получать уведомления о контрольных точках.')

        dates = [
            ('Дата начала контракта', contract_start),
            ('Срок начала проектирования', design_start),
            ('Срок окончания проектирования', design_end),
            ('Срок отгрузки', ship_due),
        ]
        filled = [(label, d) for label, d in dates if d]
        for i in range(len(filled) - 1):
            if filled[i][1] > filled[i + 1][1]:
                errs.append(
                    f'«{filled[i][0]}» позже «{filled[i + 1][0]}». '
                    f'Проверьте порядок.'
                )
                break

        if errs:
            for e in errs:
                messages.error(request, e)
        else:
            Order.objects.create(
                number=number,
                product=product,
                owner=owner,
                contract_start=contract_start,
                design_start=design_start,
                design_end=design_end,
                ship_due=ship_due,
                comment=comment,
            )
            messages.success(request, f'Заказ «{number}» создан.')
            return redirect('manager_orders')

    # ═══════════ Список заказов ═══════════
    today = timezone.localdate()
    orders = []
    for o in Order.objects.all().prefetch_related('tasks__executor__department'):
        tasks = list(o.tasks.all())
        done = sum(1 for t in tasks if t.status == Task.Status.DONE)

        # lanes по подразделениям
        by_dep = {}
        for t in tasks:
            dep = t.executor.department.name if t.executor.department else 'без подразделения'
            lane = by_dep.setdefault(dep, {
                'total': 0, 'done': 0, 'open_hours': 0.0, 'oldest': 0,
            })
            lane['total'] += 1
            if t.status == Task.Status.DONE:
                lane['done'] += 1
            else:
                lane['open_hours'] += t.plan_hours or 0
                age = (today - _local_date(t.created_at)).days
                if age > lane['oldest']:
                    lane['oldest'] = age

        # узкое место — отдел с макс. открытых часов
        bottleneck = None
        max_open = 0.0
        for dep, lane in by_dep.items():
            if lane['open_hours'] > max_open:
                max_open = lane['open_hours']
                bottleneck = dep

        orders.append({
            'order': o,
            'total': len(tasks),
            'done': done,
            'pct': round(done / len(tasks) * 100) if tasks else 0,
            'lanes': list(by_dep.items()),
            'bottleneck': bottleneck,
        })

    # ═══════════ Праздники для JS ═══════════
    import holidays as _hol

    holidays_json = [
        (h.date.isoformat(), h.is_working, h.name)
        for h in Holiday.objects.all()
    ]

    # Дополняем фиксированными праздниками текущего и следующего года
    for _year in (today.year, today.year + 1):
        for _dt, _name in _hol.RU(years=_year).items():
            holidays_json.append((_dt.isoformat(), False, str(_name)))

    # ═══════════ Рендер ═══════════
    return render(request, 'manager/orders.html', {
        'orders': orders,
        'holidays_json': holidays_json,
        'owners': User.objects.filter(
            is_active=True, role__can_manage=True,
        ).select_related('department').order_by('full_name'),
    })


@login_required
def orders_timeline(request):
    """Диаграмма Ганта по всем заказам: контрольные точки на одной шкале."""
    import calendar as cal_mod
    from datetime import date

    from tasks.models import Order
    from tasks.utils import work_days_between

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()

    # Период — месяц/квартал/год
    period = request.GET.get('period', 'quarter')
    if period not in ('month', 'quarter', 'year'):
        period = 'quarter'

    try:
        y = int(request.GET.get('y', today.year))
        m = int(request.GET.get('m', today.month))
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        y, m = today.year, today.month

    if period == 'month':
        start = date(y, m, 1)
        end = date(y, m, cal_mod.monthrange(y, m)[1])
        period_label = start.strftime('%B %Y')
    elif period == 'quarter':
        q = (m - 1) // 3
        qm = q * 3 + 1
        start = date(y, qm, 1)
        em = qm + 2
        end = date(y, em, cal_mod.monthrange(y, em)[1])
        period_label = f'Квартал {q + 1} · {y}'
    else:
        start = date(y, 1, 1)
        end = date(y, 12, 31)
        period_label = f'{y} год'

    total_days = (end - start).days + 1

    # Заголовки колонок
    cols = []
    if period == 'month':
        for dnum in range(1, end.day + 1):
            d = date(y, m, dnum)
            cols.append({
                'label': dnum,
                'weekend': d.weekday() >= 5,
                'today': d == today,
            })
    elif period == 'quarter':
        cur = start - timedelta(days=start.weekday())
        while cur <= end:
            cols.append({
                'label': cur.strftime('%d.%m'),
                'weekend': False,
                'today': cur <= today <= cur + timedelta(days=6),
            })
            cur += timedelta(days=7)
    else:
        names = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
                 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']
        for mm in range(1, 13):
            cols.append({
                'label': names[mm - 1],
                'weekend': False,
                'today': mm == today.month and y == today.year,
            })

    # Фильтр по состоянию
    state = request.GET.get('state', 'active')
    if state not in ('active', 'overdue', 'all'):
        state = 'active'

    qs = Order.objects.all()
    if state == 'overdue':
        qs = qs.filter(ship_due__lt=today)
    elif state == 'active':
        # Только заказы, где есть хотя бы одна незакрытая задача
        open_order_ids = (
            Task.objects
            .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
            .exclude(order__isnull=True)
            .values_list('order_id', flat=True)
            .distinct()
        )
        qs = qs.filter(pk__in=list(open_order_ids))

    orders = []
    for o in qs.order_by('ship_due', 'number'):
        if not o.ship_due and not o.contract_start:
            continue

        # Границы заказа для попадания в период
        o_start = o.contract_start or o.design_start or o.ship_due
        o_end = o.ship_due or o.design_end or o_start
        if o_end < start or o_start > end:
            continue

        # Полосы
        bars = []

        # 1. Контракт → начало проектирования
        if o.contract_start and o.design_start:
            s = max(o.contract_start, start)
            e = min(o.design_start, end)
            if s <= e:
                left = (s - start).days / total_days * 100
                width = ((e - s).days + 1) / total_days * 100
                bars.append({
                    'cls': 'contract',
                    'style': f'left:{left:.2f}%;width:{max(width, 0.4):.2f}%',
                    'title': f'Контракт → старт проектирования: '
                             f'{o.contract_start:%d.%m.%Y} — {o.design_start:%d.%m.%Y}',
                })

        # 2. Проектирование
        if o.design_start and o.design_end:
            s = max(o.design_start, start)
            e = min(o.design_end, end)
            if s <= e:
                left = (s - start).days / total_days * 100
                width = ((e - s).days + 1) / total_days * 100
                bars.append({
                    'cls': 'design',
                    'style': f'left:{left:.2f}%;width:{max(width, 0.4):.2f}%',
                    'title': f'Проектирование: '
                             f'{o.design_start:%d.%m.%Y} — {o.design_end:%d.%m.%Y}',
                })

        # 3. Производство: от конца проектирования до отгрузки
        if o.design_end and o.ship_due:
            s = max(o.design_end, start)
            e = min(o.ship_due, end)
            if s <= e:
                left = (s - start).days / total_days * 100
                width = ((e - s).days + 1) / total_days * 100
                bars.append({
                    'cls': 'prod',
                    'style': f'left:{left:.2f}%;width:{max(width, 0.4):.2f}%',
                    'title': f'Производство: '
                             f'{o.design_end:%d.%m.%Y} — {o.ship_due:%d.%m.%Y}',
                })

        # Вехи: ромбики
        milestones = []
        for label, dt, cls in [
            ('Начало контракта', o.contract_start, 'milestone-start'),
            ('Старт проектирования', o.design_start, 'milestone-design'),
            ('Конец проектирования', o.design_end, 'milestone-design-end'),
            ('Отгрузка', o.ship_due, 'milestone-ship'),
        ]:
            if dt and start <= dt <= end:
                left = (dt - start).days / total_days * 100
                milestones.append({
                    'cls': cls,
                    'style': f'left:{left:.2f}%',
                    'title': f'{label}: {dt:%d.%m.%Y}',
                })

        # Дни до отгрузки (раб. дни)
        days_to_ship = None
        if o.ship_due:
            if o.ship_due >= today:
                days_to_ship = work_days_between(today, o.ship_due)
            else:
                days_to_ship = -work_days_between(o.ship_due, today)

        stage = o.stage_label

        orders.append({
            'order': o,
            'bars': bars,
            'milestones': milestones,
            'days_to_ship': days_to_ship,
            'stage': stage,
            'overdue': bool(o.ship_due and o.ship_due < today),
        })

    # Prev/next
    prev_m = m - 1 or 12
    prev_y = y if m > 1 else y - 1
    next_m = m + 1 if m < 12 else 1
    next_y = y if m < 12 else y + 1

    return render(request, 'manager/orders_timeline.html', {
        'period_label': period_label,
        'y': y, 'm': m, 'period': period,
        'cols': cols,
        'orders': orders,
        'state': state,
        'prev': {'y': prev_y, 'm': prev_m},
        'next': {'y': next_y, 'm': next_m},
        'today': today,
    })


@login_required
def orders_timeline_csv(request):
    """CSV-экспорт Ганта по заказам."""
    import csv

    from tasks.models import Order
    from tasks.utils import work_days_between

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fname = f'orders_timeline_{today:%Y%m%d}.csv'
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    response.write('\ufeff')

    w = csv.writer(response, delimiter=';')
    w.writerow([
        'Заказ', 'Изделие', 'Этап',
        'Контракт', 'Старт проект.', 'Конец проект.', 'Отгрузка',
        'Раб. дней до отгрузки', 'Всего раб. дней',
    ])

    for o in Order.objects.order_by('ship_due', 'number'):
        stage = o.stage_label
        days_to_ship = ''
        if o.ship_due:
            if o.ship_due >= today:
                days_to_ship = work_days_between(today, o.ship_due)
            else:
                days_to_ship = -work_days_between(o.ship_due, today)

        w.writerow([
            o.number,
            o.product,
            stage['label'],
            o.contract_start.strftime('%d.%m.%Y') if o.contract_start else '',
            o.design_start.strftime('%d.%m.%Y') if o.design_start else '',
            o.design_end.strftime('%d.%m.%Y') if o.design_end else '',
            o.ship_due.strftime('%d.%m.%Y') if o.ship_due else '',
            days_to_ship,
            o.work_days_to_ship or '',
            ])

    return response


@login_required
def order_plan(request, pk):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    order = get_object_or_404(Order, pk=pk)

    if request.method == 'POST':
        titles = request.POST.getlist('row_title')
        exec_ids = request.POST.getlist('row_executor')
        kinds = request.POST.getlist('row_kind')
        plans = request.POST.getlist('row_plan')
        starts = request.POST.getlist('row_start')
        dues = request.POST.getlist('row_due')

        created, errors = 0, []

        for i, raw_title in enumerate(titles):
            title = (raw_title or '').strip()
            if not title:
                continue

            ex_pk = exec_ids[i] if i < len(exec_ids) else ''
            ex = (
                User.objects.filter(pk=ex_pk, is_active=True).first()
                if ex_pk.isdigit() else None
            )
            if ex is None:
                errors.append(f'Строка {i + 1}: не выбран исполнитель.')
                continue

            plan = parse_plan(plans[i] if i < len(plans) else '', 's', norm_hours()) or 0
            start = _parse_date(starts[i] if i < len(starts) else '')
            due = _parse_date(dues[i] if i < len(dues) else '')

            if start and due and start > due:
                errors.append(f'Строка {i + 1}: начало позже дедлайна.')
                continue

            kind = kinds[i] if i < len(kinds) and kinds[i] in Task.Kind.values else 'work'

            t = Task.objects.create(
                title=title, plan_hours=plan, start_due=start, due=due,
                priority='medium', scale='s', kind=kind,
                order=order, executor=ex, requester=user,
            )
            notify_task_created(t)
            created += 1

        if created:
            messages.success(request, f'В план заказа добавлено работ: {created}.')
        for e in errors:
            messages.error(request, e)

        return redirect('order_plan', pk=order.pk)

    # ── Список задач и их суммарные показатели ──
    tasks = list(
        order.tasks.select_related('executor', 'requester', 'branch')
        .order_by('start_due', 'due', 'priority')
    )
    plan_sum = sum(t.plan_hours or 0 for t in tasks)
    fact_sum = sum(t.spent_hours for t in tasks)
    done_cnt = sum(1 for t in tasks if t.status == Task.Status.DONE)

    # ── Ветки заказа с этапами ──
    branches = list(
        TaskBranch.objects.filter(order=order)
        .prefetch_related(
            Prefetch(
                'tasks',
                queryset=(
                    Task.objects
                    .select_related('executor')
                    .order_by('stage_order', 'created_at')
                ),
            )
        )
        .order_by('name')
    )

    # ── Первый незавершённый этап (что делать сейчас) ──
    current_stage = (
        Task.objects
        .filter(order=order, blocked_by_stage=False)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('stage_order', 'created_at')
        .first()
    )

    return render(request, 'manager/order_plan.html', {
        'order': order,
        'tasks': tasks,
        'branches': branches,
        'plan_sum': plan_sum,
        'fact_sum': fact_sum,
        'done_cnt': done_cnt,
        'kinds': Task.Kind.choices,
        'priorities': Task.Priority.choices,
        'users': User.objects.filter(id__in=_scope_user_ids(user))
                  .select_related('department').order_by('full_name'),
        'departments': (
            Department.objects
            .filter(users__in=User.objects.filter(id__in=_scope_user_ids(user)))
            .distinct()
            .order_by('name')
        ),
        'current_stage': current_stage,
        'today': timezone.localdate(),
    })


@login_required
def order_generate_from_template(request, pk):
    """Применить шаблон маршрута к заказу — сгенерировать ветки и задачи."""
    from tasks.order_template import generate_plan_from_template
    from tasks.models import OrderTemplate

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    order = get_object_or_404(Order, pk=pk)

    if request.method == 'POST':
        template_pk = (request.POST.get('template') or '').strip()
        template = None
        if template_pk.isdigit():
            template = OrderTemplate.objects.filter(
                pk=int(template_pk), is_active=True,
            ).first()

        if not template:
            messages.error(request, 'Выберите шаблон.')
            return redirect('order_plan', pk=order.pk)

        result = generate_plan_from_template(order, template, requester=user)

        if result['tasks_created']:
            messages.success(
                request,
                f'Создано задач: {result["tasks_created"]}, '
                f'веток: {result["branches_created"]}.'
            )
        else:
            messages.warning(
                request,
                'Ни одной задачи не создано — проверьте шаблон.'
            )
        for err in result['errors'][:5]:
            messages.warning(request, err)

        return redirect('order_plan', pk=order.pk)

    templates = OrderTemplate.objects.filter(is_active=True).order_by('-is_default', 'name')

    return render(request, 'manager/order_generate.html', {
        'order': order,
        'templates': templates,
        'has_tasks': order.tasks.exists(),
    })


# ─────────────────────────────────────────────────────────────
# Карточка сотрудника
# ─────────────────────────────────────────────────────────────

@login_required
def person_page(request, pk):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    person = get_object_or_404(User, pk=pk)
    scope = user.scope

    if scope == 'department' and person.department_id != user.department_id:
        raise PermissionDenied('Сотрудник не из вашего подразделения.')
    if scope == 'self' and person.pk != user.pk:
        raise PermissionDenied('Нет доступа к карточке другого сотрудника.')

    manager_kpi = None
    if person.is_boss and person.department_id:
        manager_kpi = manager_kpi_percent(person)

    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    norm = norm_hours()

    bd = sum(1 for d in range(1, today.day + 1) if now.replace(day=d).weekday() < 5)
    capacity = norm * max(bd, 1)

    open_tasks = list(
        Task.objects.filter(executor=person)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('due')
    )

    closed30 = list(
        Task.objects.filter(
            executor=person, status=Task.Status.DONE,
            finished_at__gte=now - timedelta(days=30),
        ).order_by('-finished_at')
    )

    hours = sum(
        t.accumulated_hours or 0
        for t in Task.objects.filter(
            executor=person, status=Task.Status.DONE, finished_at__gte=month_start
        )
    )

    with_due = [t for t in closed30 if t.due]
    otd = (
        round(
            sum(1 for t in with_due if _local_date(t.finished_at) <= t.due)
            / len(with_due) * 100
        )
        if with_due else None
    )

    rework = TaskLog.objects.filter(
        kind=TaskLog.Kind.REWORK,
        task__executor=person,
        created_at__gte=month_start,
    ).count()

    util = round(hours / capacity * 100) if capacity else None

    weeks = []
    for i in range(7, -1, -1):
        w_start = today - timedelta(days=today.weekday() + 7 * i)
        w_end = w_start + timedelta(days=7)
        h = Task.objects.filter(
            executor=person, status=Task.Status.DONE,
            finished_at__date__gte=w_start, finished_at__date__lt=w_end,
        ).aggregate(s=Sum('accumulated_hours'))['s'] or 0
        weeks.append({'label': w_start.strftime('%d.%m'), 'hours': round(h, 1)})

    max_h = max([w['hours'] for w in weeks] + [0.1])
    for w in weeks:
        w['pct'] = int(w['hours'] / max_h * 100)

    monday = today - timedelta(days=today.weekday())
    commits = list(
        WeekCommit.objects.filter(user=person, week_start=monday)
        .select_related('task')
    )

    # ── График работы сотрудника ──
    from tasks.utils import day_info, schedule_for_user

    schedule = schedule_for_user(person)
    today_info = day_info(today, schedule=schedule)
    # ── Отпуск: если у человека стоит статус VACATION или даты
    #    покрывают сегодня — покажем отдельным бейджем.
    on_vacation = person.is_on_vacation
    vacation_label = person.vacation_label

    if schedule is None:
        schedule_source = 'builtin'
        schedule_label = 'Встроенный 5/2'
        schedule_edit_url = None
    elif person.schedule_id == schedule.pk:
        schedule_source = 'personal'
        schedule_label = f'Личный: {schedule.name}'
        schedule_edit_url = reverse(
            'manager_schedule_edit', args=[schedule.pk],
        )
    else:
        schedule_source = 'department'
        schedule_label = f'Отдел: {schedule.name}'
        schedule_edit_url = reverse(
            'manager_schedule_edit', args=[schedule.pk],
        )

    # Превью ближайших 14 дней — по тому же графику.
    schedule_preview_14 = _schedule_preview(schedule, days=14)

    return render(request, 'manager/person.html', {
        'person': person,
        'open_tasks': open_tasks,
        'closed30': closed30,
        'hours': hours,
        'otd': otd,
        'rework': rework,
        'util': util,
        'kpi': user_kpi_percent(person),
        'manager_kpi': manager_kpi,
        'weeks': weeks,
        'commits': commits,
        'capacity': capacity,
        'today': today,
        'schedule': schedule,
        'schedule_source': schedule_source,
        'schedule_label': schedule_label,
        'schedule_edit_url': schedule_edit_url,
        'today_info': today_info,
        'schedule_preview_14': schedule_preview_14,
        'on_vacation': on_vacation,
        'vacation_label': vacation_label,
    })

# ─────────────────────────────────────────────────────────────
# CSV-отчёты
# ─────────────────────────────────────────────────────────────

@login_required
def reports_csv(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    member_ids = _scope_user_ids(user)
    members = User.objects.filter(id__in=member_ids).order_by('full_name')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="report_{now:%Y-%m}.csv"'
    response.write('\ufeff')
    w = _csv.writer(response, delimiter=';')

    closed_qs = Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        finished_at__gte=month_start,
    )

    agg_total = closed_qs.aggregate(
        p=Sum('plan_hours'), f=Sum('accumulated_hours'), c=Count('id')
    )
    plan_total = agg_total['p'] or 0
    fact_total = agg_total['f'] or 0
    closed_total = agg_total['c'] or 0

    with_due = closed_qs.filter(due__isnull=False)
    on_time = with_due.annotate(fd=TruncDate('finished_at')).filter(fd__lte=F('due')).count()
    otd_pct = round(on_time / with_due.count() * 100, 1) if with_due.exists() else 0

    rework_cnt = TaskLog.objects.filter(
        kind=TaskLog.Kind.REWORK,
        task__executor_id__in=member_ids,
        created_at__gte=month_start,
    ).count()

    w.writerow(['ОТЧЁТ ПО ОТДЕЛУ', user.department.name if user.department else 'Все подразделения'])
    w.writerow(['Период', now.strftime('%B %Y')])
    w.writerow([])
    w.writerow(['СВОДНЫЕ ПОКАЗАТЕЛИ'])
    w.writerow(['Показатель', 'Значение'])
    w.writerow(['Закрыто задач', closed_total])
    w.writerow(['План, ч', f'{plan_total:.1f}'])
    w.writerow(['Факт, ч', f'{fact_total:.1f}'])
    w.writerow(['KPI план/факт', f'{kpi_percent(plan_total, fact_total) or 0}%'])
    w.writerow(['OTD (в срок), %', otd_pct])
    w.writerow(['Возвратов на доработку', rework_cnt])
    w.writerow([])

    # Часы в цеху по сотрудникам за месяц (для отчёта)
    shop_by_user = {}
    for s in ShopSession.objects.filter(
            executor_id__in=member_ids, started_at__gte=month_start):
        if s.finished_at:
            shop_by_user[s.executor_id] = (
                    shop_by_user.get(s.executor_id, 0.0) + s.duration_hours
            )
        else:
            shop_by_user[s.executor_id] = (
                    shop_by_user.get(s.executor_id, 0.0)
                    + (now - s.started_at).total_seconds() / 3600
            )

    w.writerow(['СОТРУДНИКИ'])
    w.writerow(['Сотрудник', 'Закрыто', 'План, ч', 'Факт, ч', 'Цех, ч', 'KPI'])
    for m in members:
        agg = Task.objects.filter(
            executor=m, status=Task.Status.DONE, finished_at__gte=month_start
        ).aggregate(p=Sum('plan_hours'), f=Sum('accumulated_hours'), c=Count('id'))
        w.writerow([
            m.full_name, agg['c'] or 0,
            f'{agg["p"] or 0:.1f}', f'{agg["f"] or 0:.1f}',
            f'{shop_by_user.get(m.pk, 0.0):.1f}',
            f'{kpi_percent(agg["p"] or 0, agg["f"] or 0) or 0}%',
                         ])
    w.writerow([])

    w.writerow(['ЗАКРЫТЫЕ ЗАДАЧИ'])
    w.writerow(['Задача', 'Исполнитель', 'Постановщик', 'План, ч', 'Факт, ч', 'Закрыта', 'Срок', 'В срок?'])
    for t in closed_qs.select_related('executor', 'requester').order_by('-finished_at'):
        in_time = (
            'Да' if (t.due and _local_date(t.finished_at) <= t.due)
            else ('Нет' if t.due else '—')
        )
        w.writerow([
            t.title, t.executor.full_name, t.requester.full_name,
            f'{t.plan_hours or 0:.1f}', f'{t.accumulated_hours or 0:.1f}',
            t.finished_at.strftime('%d.%m.%Y'),
            t.due.strftime('%d.%m.%Y') if t.due else '',
            in_time,
        ])
    w.writerow([])

    overdue_qs = Task.objects.filter(
        executor_id__in=member_ids, due__lt=today
    ).exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    ).select_related('executor').order_by('due')

    w.writerow(['ПРОСРОЧЕННЫЕ ЗАДАЧИ'])
    w.writerow(['Задача', 'Исполнитель', 'Срок', 'Дней просрочки'])
    for t in overdue_qs:
        days = (today - t.due).days
        w.writerow([t.title, t.executor.full_name, t.due.strftime('%d.%m.%Y'), days])
    w.writerow([])

    rework_logs = TaskLog.objects.filter(
        kind=TaskLog.Kind.REWORK,
        task__executor_id__in=member_ids,
        created_at__gte=month_start,
    ).select_related('task', 'author').order_by('-created_at')

    w.writerow(['ВОЗВРАТЫ НА ДОРАБОТКУ'])
    w.writerow(['Дата', 'Задача', 'Исполнитель', 'Вернул', 'Причина'])
    for r in rework_logs:
        w.writerow([
            r.created_at.strftime('%d.%m.%Y'), r.task.title,
            r.task.executor.full_name, r.author.full_name,
            r.comment or '',
            ])
    w.writerow([])

    active_qs = Task.objects.filter(
        executor_id__in=member_ids
    ).exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    ).select_related('executor').order_by('due', 'priority')

    w.writerow(['АКТИВНЫЕ ЗАДАЧИ'])
    w.writerow(['Задача', 'Исполнитель', 'Статус', 'Приоритет', 'Срок', 'План, ч', 'Факт, ч'])
    for t in active_qs:
        w.writerow([
            t.title, t.executor.full_name, t.get_status_display(),
            t.get_priority_display(),
            t.due.strftime('%d.%m.%Y') if t.due else '',
            f'{t.plan_hours or 0:.1f}', f'{t.accumulated_hours or 0:.1f}',
        ])

    return response


# ─────────────────────────────────────────────────────────────
# Нормы
# ─────────────────────────────────────────────────────────────

@login_required
def norms_page(request):
    from core.models import TaskType

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    since = timezone.now() - timedelta(days=90)
    rows = []

    for tt in TaskType.objects.all():
        closed = list(Task.objects.filter(
            task_type=tt, status=Task.Status.DONE, finished_at__gte=since
        ))
        measured = [t for t in closed if t.plan_hours and t.accumulated_hours]
        acc = (
            round(
                sum(
                    1 for t in measured
                    if abs(t.accumulated_hours - t.plan_hours) / t.plan_hours <= 0.2
                ) / len(measured) * 100
            )
            if measured else None
        )
        facts = [t.accumulated_hours for t in measured]
        rows.append({
            'tt': tt,
            'n': len(closed),
            'acc': acc,
            'median_fact': median(facts) if facts else None,
            'revise': acc is not None and acc < 50,
        })

    return render(request, 'manager/norms.html', {'rows': rows})

# ─────────────────────────────────────────────────────────────
# Живой экран: кто чем занят прямо сейчас
# ─────────────────────────────────────────────────────────────

@login_required
def plant_live(request):
    """Живой экран директора: онлайн, задачи в работе, кто в цеху, невзятые срочные."""
    user = request.user
    if not user.can_plant:
        raise PermissionDenied('Раздел доступен только с правом «Свод по заводу».')

    now = timezone.now()
    online_threshold = now - timedelta(minutes=15)
    filter_key = request.GET.get('filter', '')

    members = list(
        User.objects.filter(is_active=True)
        .select_related('department', 'role')
        .order_by('department__name', 'full_name')
    )
    member_ids = [m.pk for m in members]

    in_progress = {
        t.executor_id: t
        for t in Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.IN_PROGRESS,
        ).select_related('order')
    }

    shop_sessions = {
        s.executor_id: s
        for s in ShopSession.objects.filter(
            executor_id__in=member_ids, finished_at__isnull=True
        ).select_related('task')
    }

    urgent_new = {}
    for t in Task.objects.filter(
            executor_id__in=member_ids,
            status=Task.Status.NEW,
            priority='urgent',
    ).order_by('created_at'):
        urgent_new.setdefault(t.executor_id, []).append(t)

    overdue_count = {}
    for row in Task.objects.filter(
            executor_id__in=member_ids, due__lt=timezone.localdate(),
    ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED]) \
            .values('executor_id').annotate(c=Count('id')):
        overdue_count[row['executor_id']] = row['c']

    people = []
    for m in members:
        is_online = bool(m.last_activity_at and m.last_activity_at >= online_threshold)
        shop = shop_sessions.get(m.pk)
        urgent_list = urgent_new.get(m.pk, [])

        urgent_oldest_min = None
        if urgent_list:
            oldest = urgent_list[0]
            urgent_oldest_min = int((now - oldest.created_at).total_seconds() / 60)

        people.append({
            'user': m,
            'online': is_online,
            'last_activity': m.last_activity_at,
            'current': in_progress.get(m.pk),
            'shop': shop,
            'shop_minutes': int((now - shop.started_at).total_seconds() / 60) if shop else 0,
            'urgent_new': urgent_list,
            'urgent_oldest_min': urgent_oldest_min,
            'overdue': overdue_count.get(m.pk, 0),
        })

    # Общие счётчики — считаются до фильтра
    total_online = sum(1 for p in people if p['online'])
    total_in_progress = sum(1 for p in people if p['current'])
    total_in_shop = sum(1 for p in people if p['shop'])
    total_urgent_stuck = sum(1 for p in people if p['urgent_new'])

    def _match(p, key):
        if key == 'online':
            return p['online']
        if key == 'work':
            return bool(p['current'])
        if key == 'shop':
            return bool(p['shop'])
        if key == 'urgent':
            return bool(p['urgent_new'])
        return True

    if filter_key in ('online', 'work', 'shop', 'urgent'):
        people = [p for p in people if _match(p, filter_key)]
    else:
        filter_key = ''

    by_dept = {}
    for p in people:
        key = p['user'].department.name if p['user'].department else 'Без подразделения'
        by_dept.setdefault(key, []).append(p)

    return render(request, 'manager/plant_live.html', {
        'by_dept': by_dept,
        'total_online': total_online,
        'total_in_progress': total_in_progress,
        'total_in_shop': total_in_shop,
        'total_urgent_stuck': total_urgent_stuck,
        'total_people': len(members),
        'filter': filter_key,
        'now': now,
    })


# ─────────────────────────────────────────────────────────────
#  Метрики сдвигов
# ─────────────────────────────────────────────────────────────

def _shift_violators(member_ids, since):
    """Топ исполнителей, чьи срочные задачи сдвинули чужие планы.

    Считаем по одобренным заявкам за период.
    """
    from tasks.models import PlanShiftRequest
    from django.db.models import Sum, Count

    rows = (
        PlanShiftRequest.objects
        .filter(
            status=PlanShiftRequest.Status.APPROVED,
            resolved_at__gte=since,
            source_task__executor_id__in=member_ids,
        )
        .values(
            'source_task__executor__pk',
            'source_task__executor__full_name',
            'source_task__executor__department__name',
        )
        .annotate(
            shift_count=Count('id', distinct=True),
            total_days=Sum('shift_days'),
        )
        .order_by('-shift_count', '-total_days')[:5]
    )

    result = []
    for r in rows:
        # Кол-во затронутых задач по всем заявкам исполнителя
        affected = (
                PlanShiftRequest.objects
                .filter(
                    status=PlanShiftRequest.Status.APPROVED,
                    resolved_at__gte=since,
                    source_task__executor_id=r['source_task__executor__pk'],
                )
                .aggregate(c=Count('affected_tasks', distinct=True))['c'] or 0
        )
        result.append({
            'executor_pk': r['source_task__executor__pk'],
            'executor_name': r['source_task__executor__full_name'],
            'department': r['source_task__executor__department__name'] or '—',
            'shift_count': r['shift_count'],
            'total_days': r['total_days'] or 0,
            'affected_count': affected,
        })
    return result


def _shift_initiators(member_ids, since):
    """Топ постановщиков, чьи задачи ломают план.

    Считаем по initiated_by — это постановщик срочной задачи.
    """
    from tasks.models import PlanShiftRequest
    from django.db.models import Sum, Count

    rows = (
        PlanShiftRequest.objects
        .filter(
            status=PlanShiftRequest.Status.APPROVED,
            resolved_at__gte=since,
            initiated_by_id__in=member_ids,
        )
        .values(
            'initiated_by__pk',
            'initiated_by__full_name',
            'initiated_by__department__name',
        )
        .annotate(
            shift_count=Count('id', distinct=True),
            total_days=Sum('shift_days'),
        )
        .order_by('-shift_count', '-total_days')[:5]
    )

    return [
        {
            'user_pk': r['initiated_by__pk'],
            'user_name': r['initiated_by__full_name'],
            'department': r['initiated_by__department__name'] or '—',
            'shift_count': r['shift_count'],
            'total_days': r['total_days'] or 0,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────
# Свод по заводу
# ─────────────────────────────────────────────────────────────

def _plant_rows():
    now = timezone.now()
    today = timezone.localdate()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_date = today.replace(day=1)
    norm = norm_hours()

    bd_elapsed = sum(
        1 for d in range(1, today.day + 1)
        if now.replace(day=d).weekday() < 5
    )
    capacity = norm * max(bd_elapsed, 1)

    members_all = list(
        User.objects.filter(is_active=True).select_related('department')
    )

    deps = {}
    for m in members_all:
        key = m.department.name if m.department else 'без подразделения'
        deps.setdefault(key, []).append(m)

    rows = []
    for dep_name, members in sorted(deps.items()):
        ids = [m.pk for m in members]

        open_tasks = list(
            Task.objects.filter(executor_id__in=ids)
            .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        )
        overdue = [t for t in open_tasks if t.due and t.due < today]

        closed_month = list(
            Task.objects.filter(
                executor_id__in=ids,
                status=Task.Status.DONE,
                finished_at__gte=month_start,
            )
        )

        hours = sum(t.accumulated_hours or 0 for t in closed_month)
        plan = sum(t.plan_hours or 0 for t in closed_month)

        with_due = [t for t in closed_month if t.due]
        otd = (
            round(
                sum(1 for t in with_due if _local_date(t.finished_at) <= t.due)
                / len(with_due) * 100
            )
            if with_due else None
        )

        rework = TaskLog.objects.filter(
            kind=TaskLog.Kind.REWORK,
            task__executor_id__in=ids,
            created_at__gte=month_start,
        ).count()

        wasted = list(
            Task.objects.filter(
                executor_id__in=ids,
                status=Task.Status.CANCELLED,
                accumulated_hours__gt=0,
                finished_at__gte=month_start,
            )
        )
        wasted_fact = round(sum(t.accumulated_hours or 0 for t in wasted), 1)
        wasted_plan = round(sum(t.plan_hours or 0 for t in wasted), 1)
        deferred = Task.objects.filter(
            executor_id__in=ids,
            status=Task.Status.NEW,
            priority='urgent',
        ).count()

        shop = 0.0
        for s in ShopSession.objects.filter(
                executor_id__in=ids, started_at__gte=month_start
        ):
            if s.finished_at:
                shop += s.duration_hours
            else:
                shop += (now - s.started_at).total_seconds() / 3600

        util = (
            round(hours / (capacity * len(members)) * 100)
            if capacity and members else None
        )

        # ── Сверхурочные за текущий месяц ──
        ot_qs = OvertimeRecord.objects.filter(
            user_id__in=ids,
            date__gte=month_start_date,
            date__lte=today,
        )
        overtime_hours = round(
            sum(r.hours or 0 for r in ot_qs), 1
        )
        overtime_count = ot_qs.count()
        overtime_people = (
            ot_qs.values('user_id').distinct().count()
        )

        rows.append({
            'dep': dep_name,
            'people': len(members),
            'open': len(open_tasks),
            'overdue': len(overdue),
            'closed': len(closed_month),
            'plan': plan,
            'fact': hours,
            'kpi': kpi_percent(plan, hours),
            'otd': otd,
            'rework': rework,
            'shop': shop,
            'util': util,
            'wasted_fact': wasted_fact,
            'wasted_plan': wasted_plan,
            'deferred_urgent': deferred,
            'overtime_hours': overtime_hours,
            'overtime_count': overtime_count,
            'overtime_people': overtime_people,
        })

    return rows, now


def _overtime_by_day():
    """Сверхурочные за текущий месяц: разбивка по дням + топ отделов.

    Возвращает:
      days    — [{'date': d, 'hours': X, 'count': N, 'deps': {название: часы}}]
                только дни, где есть записи. Пустые дни не тянем — их
                всё равно покажет календарь в шаблоне.
      by_dep  — [{'dep': 'ОГК', 'hours': X, 'count': N, 'people': K}]
                отсортировано по часам убыв.
      grand   — {'hours': X, 'count': N, 'days': K, 'departments': K}
    """
    today = timezone.localdate()
    month_start = today.replace(day=1)

    qs = (
        OvertimeRecord.objects
        .filter(date__gte=month_start, date__lte=today)
        .select_related('user', 'user__department')
    )

    days_map = {}
    deps_map = {}

    for r in qs:
        d = r.date
        dep_name = (
            r.department.name if r.department
            else (r.user.department.name if r.user.department else '—')
        )

        row = days_map.setdefault(d, {
            'date': d, 'hours': 0.0, 'count': 0, 'deps': {},
        })
        row['hours'] = round(row['hours'] + (r.hours or 0), 1)
        row['count'] += 1
        row['deps'][dep_name] = round(
            row['deps'].get(dep_name, 0.0) + (r.hours or 0), 1
        )

        dep = deps_map.setdefault(dep_name, {
            'dep': dep_name,
            'hours': 0.0,
            'count': 0,
            'people': set(),
        })
        dep['hours'] = round(dep['hours'] + (r.hours or 0), 1)
        dep['count'] += 1
        dep['people'].add(r.user_id)

    days = sorted(days_map.values(), key=lambda x: -x['date'].toordinal())

    by_dep = []
    for v in deps_map.values():
        by_dep.append({
            'dep': v['dep'],
            'hours': v['hours'],
            'count': v['count'],
            'people': len(v['people']),
        })
    by_dep.sort(key=lambda x: -x['hours'])

    grand = {
        'hours': round(sum(d['hours'] for d in days), 1),
        'count': sum(d['count'] for d in days),
        'days': len(days),
        'departments': len(by_dep),
    }

    return days, by_dep, grand


@login_required
def plant_stats(request):
    if not request.user.can_plant:
        raise PermissionDenied('Раздел доступен только с правом «Свод по заводу».')

    rows, now = _plant_rows()
    overtime_days, overtime_by_dep, overtime_grand = _overtime_by_day()

    # Максимум по дню — для пропорций в полосках
    max_day_hours = max([d['hours'] for d in overtime_days] + [1.0])

    return render(request, 'manager/plant.html', {
        'rows': rows,
        'month_label': now.strftime('%B %Y'),
        'overtime_days': overtime_days,
        'overtime_by_dep': overtime_by_dep,
        'overtime_grand': overtime_grand,
        'max_day_hours': max_day_hours,
        'overtime_month': now.strftime('%B %Y'),
    })


@login_required
def plant_csv(request):
    if not request.user.can_plant:
        raise PermissionDenied('Раздел доступен только с правом «Свод по заводу».')

    rows, now = _plant_rows()

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="plant_{now:%Y-%m}.csv"'
    response.write('\ufeff')
    w = _csv.writer(response, delimiter=';')

    w.writerow([
        'Подразделение', 'Людей', 'Открытых', 'Просрочено', 'Закрыто',
        'План, ч', 'Факт, ч', 'KPI, %', 'OTD, %', 'Возвраты',
        'Цех, ч', 'Загрузка, %',
    ])

    for r in rows:
        w.writerow([
            r['dep'], r['people'], r['open'], r['overdue'], r['closed'],
            f"{r['plan']:.1f}", f"{r['fact']:.1f}",
            r['kpi'] if r['kpi'] is not None else '',
            r['otd'] if r['otd'] is not None else '',
            r['rework'],
            f"{r['shop']:.1f}",
            r['util'] if r['util'] is not None else '',
        ])

    return response


@login_required
def orders_report(request):
    """Отчёт по заказам за квартал/год.

    Метрики:
      - всего заказов в периоде
      - отгружено / в работе / просрочено отгрузка
      - OTD (отгружено в срок), медиана цикла «контракт → отгрузка»
      - разрез по подразделениям (по задачам заказов)
      - топ проблемных заказов (просрочены, с большим числом сдвигов)
    """
    from datetime import date as _date
    from statistics import median
    from tasks.models import PlanShiftRequest

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()
    now = timezone.now()

    # ── Период: квартал или год ──
    mode = request.GET.get('mode', 'quarter')
    if mode not in ('quarter', 'year'):
        mode = 'quarter'

    try:
        year = int(request.GET.get('year', now.year))
    except (TypeError, ValueError):
        year = now.year

    try:
        quarter = int(request.GET.get('quarter', (now.month - 1) // 3 + 1))
    except (TypeError, ValueError):
        quarter = (now.month - 1) // 3 + 1
    quarter = max(1, min(4, quarter))

    if mode == 'quarter':
        q_start = _date(year, (quarter - 1) * 3 + 1, 1)
        q_end_month = quarter * 3
        if q_end_month == 12:
            q_end = _date(year + 1, 1, 1)
        else:
            q_end = _date(year, q_end_month + 1, 1)
        period_label = f'Квартал {quarter} · {year}'
    else:
        q_start = _date(year, 1, 1)
        q_end = _date(year + 1, 1, 1)
        period_label = f'{year} год'

    # ── Заказы, попадающие в период ──
    # Критерий: ship_due в периоде ИЛИ (ship_due пусто и contract_start в периоде)
    orders = list(
        Order.objects
        .filter(
            Q(ship_due__gte=q_start, ship_due__lt=q_end)
            | Q(ship_due__isnull=True, contract_start__gte=q_start, contract_start__lt=q_end)
        )
        .select_related('owner')
        .prefetch_related('tasks__executor__department')
        .order_by('ship_due', 'number')
    )

    # ── Общие счётчики ──
    total_orders = len(orders)
    shipped_on_time = 0
    shipped_late = 0
    in_progress = 0

    cycles = []  # список дней «контракт → отгрузка» для отгруженных

    # ── Разрез по отделам ──
    dept_stats = {}  # {name: {'orders': set(), 'tasks': N, 'done': N, 'plan': X, 'fact': X, 'overdue': N}}

    # ── Топ проблемных ──
    problem_orders = []

    for o in orders:
        stage = o.stage_label
        tasks = list(o.tasks.all())

        # OTD
        if o.ship_due and o.ship_due < today:
            # Заказ отгружен (или просрочен — считаем по факту закрытия задач)
            done_tasks = [t for t in tasks if t.status == Task.Status.DONE]
            last_finish = None
            for t in done_tasks:
                if t.finished_at and (last_finish is None or t.finished_at > last_finish):
                    last_finish = t.finished_at
            if last_finish:
                finish_date = _local_date(last_finish)
                if finish_date <= o.ship_due:
                    shipped_on_time += 1
                else:
                    shipped_late += 1
                # Цикл
                if o.contract_start:
                    cycles.append((finish_date - o.contract_start).days)
            else:
                # отгрузка прошла, но задачи не закрыты — считаем просрочкой
                shipped_late += 1
        else:
            # ship_due в будущем или пусто
            has_open = any(
                t.status not in (Task.Status.DONE, Task.Status.CANCELLED)
                for t in tasks
            )
            if has_open:
                in_progress += 1

        # Разрез по отделам
        for t in tasks:
            dep = t.executor.department.name if t.executor.department else 'без подразделения'
            row = dept_stats.setdefault(dep, {
                'orders': set(),
                'tasks': 0,
                'done': 0,
                'plan': 0.0,
                'fact': 0.0,
                'overdue': 0,
            })
            row['orders'].add(o.pk)
            row['tasks'] += 1
            if t.status == Task.Status.DONE:
                row['done'] += 1
            row['plan'] += t.plan_hours or 0
            row['fact'] += t.accumulated_hours or 0
            if t.due and t.due < today and t.status not in (Task.Status.DONE, Task.Status.CANCELLED):
                row['overdue'] += 1

        # Топ проблемных: просрочен или много сдвигов
        is_overdue_now = bool(o.ship_due and o.ship_due < today)
        shifts = PlanShiftRequest.objects.filter(
            status=PlanShiftRequest.Status.APPROVED,
            source_task__order=o,
        ).count() if o.pk else 0

        # Задачи в заказе без исполнителя/без срока/с большим фактом
        problems = []
        if is_overdue_now:
            problems.append(f'просрочен отгрузка на {(today - o.ship_due).days} д.')
        if shifts >= 3:
            problems.append(f'{shifts} сдвигов в плане')
        bad_tasks = sum(
            1 for t in tasks
            if t.status not in (Task.Status.DONE, Task.Status.CANCELLED)
            and (not t.due or t.plan_hours <= 0)
        )
        if bad_tasks:
            problems.append(f'{bad_tasks} задач без срока/плана')

        if problems:
            problem_orders.append({
                'order': o,
                'problems': problems,
                'shifts': shifts,
                'is_overdue': is_overdue_now,
                'days_overdue': (today - o.ship_due).days if is_overdue_now else 0,
            })

    problem_orders.sort(
        key=lambda x: (-x['is_overdue'], -x['days_overdue'], -x['shifts'])
    )

    # ── Обогащаем заказы агрегатами по задачам ──
    order_rows = []
    series_map = {}       # {series: {'orders': set(), 'plan': X, 'fact': X, 'on_time': N, 'late': N}}
    month_stats = [
        {'month': m, 'label': _month_name(m), 'total': 0, 'on_time': 0, 'late': 0, 'in_progress': 0}
        for m in range(1, 13)
    ]

    for o in orders:
        tasks = list(o.tasks.all())
        tasks_count = len(tasks)
        done_count = sum(1 for t in tasks if t.status == Task.Status.DONE)
        plan_total = round(sum(t.plan_hours or 0 for t in tasks), 1)
        fact_total = round(sum(t.accumulated_hours or 0 for t in tasks), 1)

        # Статус по срокам
        stage = o.stage_label
        is_shipped_on_time = False
        is_shipped_late = False
        if o.ship_due and o.ship_due < today:
            done_tasks = [t for t in tasks if t.status == Task.Status.DONE and t.finished_at]
            last_finish = max((t.finished_at for t in done_tasks), default=None)
            if last_finish:
                if _local_date(last_finish) <= o.ship_due:
                    is_shipped_on_time = True
                else:
                    is_shipped_late = True
            else:
                is_shipped_late = True

        order_rows.append({
            'order': o,
            'tasks_count': tasks_count,
            'done_count': done_count,
            'plan_total': plan_total,
            'fact_total': fact_total,
            'delta': round(fact_total - plan_total, 1),
            'is_on_time': is_shipped_on_time,
            'is_late': is_shipped_late,
            'stage': stage,
        })

        # ── Серия изделия: первое слово product ──
        product = (o.product or '').strip()
        series = product.split()[0] if product else 'без серии'
        series = series.strip('«»"\'')[:40] or 'без серии'
        s = series_map.setdefault(series, {
            'orders': set(), 'plan': 0.0, 'fact': 0.0, 'on_time': 0, 'late': 0,
        })
        s['orders'].add(o.pk)
        s['plan'] += plan_total
        s['fact'] += fact_total
        if is_shipped_on_time:
            s['on_time'] += 1
        elif is_shipped_late:
            s['late'] += 1

        # ── Помесячная статистика по ship_due ──
        if o.ship_due and q_start <= o.ship_due < q_end:
            mrow = month_stats[o.ship_due.month - 1]
            mrow['total'] += 1
            if is_shipped_on_time:
                mrow['on_time'] += 1
            elif is_shipped_late:
                mrow['late'] += 1
            else:
                mrow['in_progress'] += 1

    # ── Формируем строки разбивки по отделам ──
    dept_rows = []
    for name, s in sorted(dept_stats.items()):
        dept_rows.append({
            'name': name,
            'orders': len(s['orders']),
            'tasks': s['tasks'],
            'done': s['done'],
            'plan': round(s['plan'], 1),
            'fact': round(s['fact'], 1),
            'overdue': s['overdue'],
        })
    dept_rows.sort(key=lambda r: -r['tasks'])

    # ── Формируем строки разбивки по сериям ──
    series_rows = []
    for name, s in series_map.items():
        total_shipped = s['on_time'] + s['late']
        otd = int(round(s['on_time'] / total_shipped * 100)) if total_shipped else None
        series_rows.append({
            'name': name,
            'orders': len(s['orders']),
            'plan': round(s['plan'], 1),
            'fact': round(s['fact'], 1),
            'on_time': s['on_time'],
            'late': s['late'],
            'otd': otd,
        })
    series_rows.sort(key=lambda r: -r['orders'])

    # Максимум для графика по месяцам
    max_month_total = max((m['total'] for m in month_stats), default=1) or 1
    for m in month_stats:
        m['pct_total'] = int(m['total'] / max_month_total * 100) if m['total'] else 0
        m['pct_on_time'] = int(m['on_time'] / max_month_total * 100) if m['on_time'] else 0
        m['pct_late'] = int(m['late'] / max_month_total * 100) if m['late'] else 0

    # ── Итоги ──
    otd = (
        int(round(shipped_on_time / (shipped_on_time + shipped_late) * 100))
        if (shipped_on_time + shipped_late) else None
    )
    median_cycle = int(median(cycles)) if cycles else None
    total_plan = round(sum(r['plan'] for r in dept_rows), 1)
    total_fact = round(sum(r['fact'] for r in dept_rows), 1)

    grand = {
        'total_orders': total_orders,
        'shipped_on_time': shipped_on_time,
        'shipped_late': shipped_late,
        'in_progress': in_progress,
        'overdue_now': len([p for p in problem_orders if p['is_overdue']]),
        'otd': otd,
        'median_cycle': median_cycle,
        'total_plan': total_plan,
        'total_fact': total_fact,
    }

    # ── Список годов для селектора ──
    years_available = list(range(2025, now.year + 1))

    return render(request, 'manager/orders_report.html', {
        'mode': mode,
        'year': year,
        'quarter': quarter,
        'years_available': years_available,
        'period_label': period_label,
        'q_start': q_start,
        'q_end': q_end,
        'today': today,
        'orders': orders,
        'order_rows': order_rows,
        'dept_rows': dept_rows,
        'series_rows': series_rows,
        'month_stats': month_stats,
        'problem_orders': problem_orders[:15],
        'grand': grand,
        'scope': user.scope,
        'active': 'orders',
    })


@login_required
def orders_report_csv(request):
    """CSV-экспорт отчёта по заказам."""
    import csv as _csv
    from datetime import date as _date
    from tasks.models import PlanShiftRequest

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    today = timezone.localdate()
    now = timezone.now()

    mode = request.GET.get('mode', 'quarter')
    if mode not in ('quarter', 'year'):
        mode = 'quarter'
    try:
        year = int(request.GET.get('year', now.year))
    except (TypeError, ValueError):
        year = now.year
    try:
        quarter = int(request.GET.get('quarter', (now.month - 1) // 3 + 1))
    except (TypeError, ValueError):
        quarter = (now.month - 1) // 3 + 1
    quarter = max(1, min(4, quarter))

    if mode == 'quarter':
        q_start = _date(year, (quarter - 1) * 3 + 1, 1)
        q_end_month = quarter * 3
        if q_end_month == 12:
            q_end = _date(year + 1, 1, 1)
        else:
            q_end = _date(year, q_end_month + 1, 1)
        label = f'Квартал {quarter} · {year}'
    else:
        q_start = _date(year, 1, 1)
        q_end = _date(year + 1, 1, 1)
        label = f'{year} год'

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fname = f'orders_report_{year}_{mode}.csv'
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    response.write('\ufeff')

    w = _csv.writer(response, delimiter=';')

    w.writerow([f'ОТЧЁТ ПО ЗАКАЗАМ: {label}'])
    w.writerow(['Период', f'{q_start:%d.%m.%Y} — {(q_end - timedelta(days=1)):%d.%m.%Y}'])
    w.writerow([])
    w.writerow([
        'Заказ', 'Изделие', 'Ответственный', 'Этап',
        'Контракт', 'Старт проект.', 'Конец проект.', 'Отгрузка',
        'Задач', 'Закрыто', 'План, ч', 'Факт, ч',
        'Сдвигов', 'Просрочено',
    ])

    orders = list(
        Order.objects
        .filter(
            Q(ship_due__gte=q_start, ship_due__lt=q_end)
            | Q(ship_due__isnull=True, contract_start__gte=q_start, contract_start__lt=q_end)
        )
        .select_related('owner')
        .prefetch_related('tasks')
        .order_by('ship_due', 'number')
    )

    for o in orders:
        stage = o.stage_label
        tasks = list(o.tasks.all())
        done = sum(1 for t in tasks if t.status == Task.Status.DONE)
        plan = sum(t.plan_hours or 0 for t in tasks)
        fact = sum(t.accumulated_hours or 0 for t in tasks)
        shifts = PlanShiftRequest.objects.filter(
            status=PlanShiftRequest.Status.APPROVED,
            source_task__order=o,
        ).count()
        overdue_n = sum(
            1 for t in tasks
            if t.due and t.due < today and t.status not in (Task.Status.DONE, Task.Status.CANCELLED)
        )

        w.writerow([
            o.number,
            o.product,
            o.owner.full_name if o.owner else '',
            stage['label'],
            o.contract_start.strftime('%d.%m.%Y') if o.contract_start else '',
            o.design_start.strftime('%d.%m.%Y') if o.design_start else '',
            o.design_end.strftime('%d.%m.%Y') if o.design_end else '',
            o.ship_due.strftime('%d.%m.%Y') if o.ship_due else '',
            len(tasks),
            done,
            f'{plan:.1f}'.replace('.', ','),
            f'{fact:.1f}'.replace('.', ','),
            shifts,
            overdue_n,
        ])

    return response


# ─────────────────────────────────────────────────────────────
# Квартальные и годовые отчёты
# ─────────────────────────────────────────────────────────────

@login_required
def quarterly_report(request):
    from datetime import date as date_cls

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()

    try:
        year = int(request.GET.get('year', now.year))
        quarter = int(request.GET.get('quarter', (now.month - 1) // 3 + 1))
    except (TypeError, ValueError):
        year = now.year
        quarter = (now.month - 1) // 3 + 1

    quarter = max(1, min(4, quarter))

    q_start = date_cls(year, (quarter - 1) * 3 + 1, 1)
    q_end_month = quarter * 3
    if q_end_month == 12:
        q_end = date_cls(year + 1, 1, 1)
    else:
        q_end = date_cls(year, q_end_month + 1, 1)

    member_ids = _scope_user_ids(user)

    tasks = list(
        Task.objects.filter(
            Q(start_due__gte=q_start, start_due__lt=q_end)
            | Q(due__gte=q_start, due__lt=q_end)
            | Q(start_due__lte=q_start, due__gte=q_end)
        ).filter(executor_id__in=member_ids)
        .exclude(status=Task.Status.CANCELLED)
        .select_related('executor', 'executor__department')
        .order_by('start_due', 'due')
    )

    dept_stats = {}
    for t in tasks:
        dep = t.executor.department.name if t.executor.department else 'без подразделения'
        if dep not in dept_stats:
            dept_stats[dep] = {'total': 0, 'done': 0, 'plan': 0.0, 'fact': 0.0, 'overdue': 0}
        dept_stats[dep]['total'] += 1
        if t.status == Task.Status.DONE:
            dept_stats[dep]['done'] += 1
        dept_stats[dep]['plan'] += t.plan_hours or 0
        dept_stats[dep]['fact'] += t.accumulated_hours or 0
        if t.due and t.due < timezone.localdate() and t.status != Task.Status.DONE:
            dept_stats[dep]['overdue'] += 1

    total_plan = sum(t.plan_hours or 0 for t in tasks)
    total_fact = sum(t.accumulated_hours or 0 for t in tasks)
    total_done = sum(1 for t in tasks if t.status == Task.Status.DONE)

    with_due = [t for t in tasks if t.due and t.status == Task.Status.DONE]
    on_time = sum(
        1 for t in with_due
        if t.finished_at and _local_date(t.finished_at) <= t.due
    )
    otd = round(on_time / len(with_due) * 100) if with_due else 0

    return render(request, 'manager/quarterly_report.html', {
        'year': year,
        'quarter': quarter,
        'q_start': q_start,
        'q_end': q_end,
        'tasks': tasks,
        'dept_stats': dept_stats,
        'total_plan': total_plan,
        'total_fact': total_fact,
        'total_done': total_done,
        'otd': otd,
        'kpi': kpi_percent(total_plan, total_fact),
        'active': 'quarter',
        'cur_year': now.year,
        'years_available': list(range(2026, now.year + 1)),
        'scope': user.scope,
    })


@login_required
def yearly_report(request):
    from datetime import date as date_cls

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()

    try:
        year = int(request.GET.get('year', now.year))
    except (TypeError, ValueError):
        year = now.year

    year_start = date_cls(year, 1, 1)
    year_end = date_cls(year + 1, 1, 1)
    member_ids = _scope_user_ids(user)

    tasks = list(
        Task.objects.filter(
            Q(start_due__gte=year_start, start_due__lt=year_end)
            | Q(due__gte=year_start, due__lt=year_end)
            | Q(start_due__lte=year_start, due__gte=year_end)
        ).filter(executor_id__in=member_ids)
        .exclude(status=Task.Status.CANCELLED)
        .select_related('executor', 'executor__department')
    )

    months = []
    for m in range(1, 13):
        m_start = date_cls(year, m, 1)
        m_end = date_cls(year + 1, 1, 1) if m == 12 else date_cls(year, m + 1, 1)
        mt = [
            t for t in tasks
            if t.start_due and t.start_due < m_end
               and (t.due is None or t.due >= m_start)
        ]
        m_done = sum(1 for t in mt if t.status == Task.Status.DONE)
        m_plan = sum(t.plan_hours or 0 for t in mt)
        m_fact = sum(t.accumulated_hours or 0 for t in mt)
        months.append({
            'label': m_start.strftime('%B'),
            'tasks': len(mt),
            'done': m_done,
            'plan': round(m_plan, 1),
            'fact': round(m_fact, 1),
            'kpi': kpi_percent(m_plan, m_fact),
        })

    dept_stats = {}
    for t in tasks:
        dep = t.executor.department.name if t.executor.department else 'без подразделения'
        if dep not in dept_stats:
            dept_stats[dep] = {'total': 0, 'done': 0, 'plan': 0.0, 'fact': 0.0}
        dept_stats[dep]['total'] += 1
        if t.status == Task.Status.DONE:
            dept_stats[dep]['done'] += 1
        dept_stats[dep]['plan'] += t.plan_hours or 0
        dept_stats[dep]['fact'] += t.accumulated_hours or 0

    total_plan = sum(t.plan_hours or 0 for t in tasks)
    total_fact = sum(t.accumulated_hours or 0 for t in tasks)
    total_done = sum(1 for t in tasks if t.status == Task.Status.DONE)

    with_due = [t for t in tasks if t.due and t.status == Task.Status.DONE]
    on_time = sum(
        1 for t in with_due
        if t.finished_at and _local_date(t.finished_at) <= t.due
    )
    otd = round(on_time / len(with_due) * 100) if with_due else 0

    return render(request, 'manager/yearly_report.html', {
        'year': year,
        'year_start': year_start,
        'year_end': year_end,
        'tasks': tasks,
        'months': months,
        'dept_stats': dept_stats,
        'total_plan': total_plan,
        'total_fact': total_fact,
        'total_done': total_done,
        'otd': otd,
        'kpi': kpi_percent(total_plan, total_fact),
        'active': 'year',
        'cur_year': now.year,
        'years_available': list(range(2026, now.year + 1)),
        'scope': user.scope,
    })


@login_required
def quarterly_report_csv(request):
    from datetime import date as date_cls

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()

    try:
        year = int(request.GET.get('year', now.year))
        quarter = int(request.GET.get('quarter', (now.month - 1) // 3 + 1))
    except (TypeError, ValueError):
        year = now.year
        quarter = (now.month - 1) // 3 + 1

    quarter = max(1, min(4, quarter))

    q_start = date_cls(year, (quarter - 1) * 3 + 1, 1)
    q_end_month = quarter * 3
    if q_end_month == 12:
        q_end = date_cls(year + 1, 1, 1)
    else:
        q_end = date_cls(year, q_end_month + 1, 1)

    member_ids = _scope_user_ids(user)

    tasks = Task.objects.filter(
        Q(start_due__gte=q_start, start_due__lt=q_end)
        | Q(due__gte=q_start, due__lt=q_end)
        | Q(start_due__lte=q_start, due__gte=q_end)
    ).filter(executor_id__in=member_ids).exclude(
        status=Task.Status.CANCELLED
    ).select_related('executor', 'executor__department')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="quarterly_{year}_Q{quarter}.csv"'
    response.write('\ufeff')
    w = _csv.writer(response, delimiter=';')

    w.writerow([f'КВАРТАЛЬНЫЙ ОТЧЁТ: {year} год, {quarter} квартал'])
    w.writerow(['Период', f'{q_start.strftime("%d.%m.%Y")} — {q_end.strftime("%d.%m.%Y")}'])
    w.writerow([])
    w.writerow([
        'Задача', 'Исполнитель', 'Подразделение', 'Статус',
        'Вид работы', 'Начало', 'Дедлайн', 'План, ч', 'Факт, ч', 'KPI',
    ])

    for t in tasks.order_by('start_due'):
        w.writerow([
            t.title,
            t.executor.full_name,
            t.executor.department.name if t.executor.department else '',
            t.get_status_display(),
            t.get_kind_display(),
            t.start_due.strftime('%d.%m.%Y') if t.start_due else '',
            t.due.strftime('%d.%m.%Y') if t.due else '',
            f'{t.plan_hours or 0:.1f}',
            f'{t.accumulated_hours or 0:.1f}',
            f'{kpi_percent(t.plan_hours or 0, t.accumulated_hours or 0) or 0}%',
        ])

    return response


@login_required
def yearly_report_csv(request):
    from datetime import date as date_cls

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()

    try:
        year = int(request.GET.get('year', now.year))
    except (TypeError, ValueError):
        year = now.year

    year_start = date_cls(year, 1, 1)
    year_end = date_cls(year + 1, 1, 1)
    member_ids = _scope_user_ids(user)

    tasks = list(
        Task.objects.filter(
            Q(start_due__gte=year_start, start_due__lt=year_end)
            | Q(due__gte=year_start, due__lt=year_end)
            | Q(start_due__lte=year_start, due__gte=year_end)
        ).filter(executor_id__in=member_ids)
        .exclude(status=Task.Status.CANCELLED)
        .select_related('executor', 'executor__department')
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="yearly_{year}.csv"'
    response.write('\ufeff')
    w = _csv.writer(response, delimiter=';')

    w.writerow([f'ГОДОВОЙ ОТЧЁТ: {year}'])
    w.writerow([])
    w.writerow(['Месяц', 'Задач', 'Закрыто', 'План, ч', 'Факт, ч', 'KPI'])

    for m in range(1, 13):
        m_start = date_cls(year, m, 1)
        m_end = date_cls(year + 1, 1, 1) if m == 12 else date_cls(year, m + 1, 1)
        mt = [
            t for t in tasks
            if t.start_due and t.start_due < m_end
               and (t.due is None or t.due >= m_start)
        ]
        m_plan = sum(t.plan_hours or 0 for t in mt)
        m_fact = sum(t.accumulated_hours or 0 for t in mt)
        w.writerow([
            m_start.strftime('%B'), len(mt),
            sum(1 for t in mt if t.status == Task.Status.DONE),
            f'{m_plan:.1f}', f'{m_fact:.1f}',
            f'{kpi_percent(m_plan, m_fact) or 0}%',
        ])

    w.writerow([])
    w.writerow([
        'Задача', 'Исполнитель', 'Подразделение', 'Статус',
        'Начало', 'Дедлайн', 'План, ч', 'Факт, ч',
    ])

    for t in tasks:
        w.writerow([
            t.title,
            t.executor.full_name,
            t.executor.department.name if t.executor.department else '',
            t.get_status_display(),
            t.start_due.strftime('%d.%m.%Y') if t.start_due else '',
            t.due.strftime('%d.%m.%Y') if t.due else '',
            f'{t.plan_hours or 0:.1f}',
            f'{t.accumulated_hours or 0:.1f}',
        ])

    return response


# ─────────────────────────────────────────────────────────────
# Исторические отчёты и снимки
# ─────────────────────────────────────────────────────────────

@login_required
def historical_reports(request):
    from datetime import date as date_cls
    from django.db.models import Max
    from tasks.models import TimelineSnapshot

    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    def month_picks(year, month):
        start = date_cls(year, month, 1)
        end = date_cls(year + 1, 1, 1) if month == 12 else date_cls(year, month + 1, 1)
        last = TimelineSnapshot.objects.filter(
            snapshot_date__gte=start, snapshot_date__lt=end
        ).aggregate(l=Max('snapshot_date'))['l']
        if not last:
            return []
        return list(
            TimelineSnapshot.objects.filter(snapshot_date=last)
            .select_related('task', 'executor', 'executor__department')
        )

    available_dates = TimelineSnapshot.objects.dates(
        'snapshot_date', 'month', order='DESC'
    )

    period = request.GET.get('period', 'month')
    selected_date = request.GET.get('date', '')

    context = {
        'available_dates': available_dates,
        'period': period,
        'selected_date': selected_date,
    }

    if selected_date:
        try:
            snap = datetime.strptime(selected_date, '%Y-%m').date()
        except ValueError:
            messages.error(request, 'Неверный формат даты.')
            snap = None

        if snap:
            if period == 'month':
                picks = month_picks(snap.year, snap.month)
                by_executor = {}
                for s in picks:
                    r = by_executor.setdefault(
                        s.executor.full_name,
                        {'tasks': 0, 'done': 0, 'plan': 0.0},
                    )
                    r['tasks'] += 1
                    if s.status == Task.Status.DONE:
                        r['done'] += 1
                    r['plan'] += s.plan_hours
                context.update({
                    'snapshots': picks,
                    'by_executor': by_executor,
                    'month_label': snap.strftime('%B %Y'),
                    'snap_date': picks[0].snapshot_date if picks else None,
                })

            elif period == 'quarter':
                q = (snap.month - 1) // 3 + 1
                months = [(q - 1) * 3 + i + 1 for i in range(3)]
                by_month, picks_all = {}, []
                for m in months:
                    picks = month_picks(snap.year, m)
                    picks_all += picks
                    r = by_month.setdefault(
                        f'{snap.year}-{m:02d}',
                        {'tasks': 0, 'done': 0, 'plan': 0.0},
                    )
                    for s in picks:
                        r['tasks'] += 1
                        if s.status == Task.Status.DONE:
                            r['done'] += 1
                        r['plan'] += s.plan_hours
                context.update({
                    'snapshots': picks_all,
                    'by_month': by_month,
                    'quarter': q,
                    'year': snap.year,
                })

            else:
                by_month, picks_all = {}, []
                for m in range(1, 13):
                    picks = month_picks(snap.year, m)
                    picks_all += picks
                    r = by_month.setdefault(
                        f'{snap.year}-{m:02d}',
                        {'tasks': 0, 'done': 0, 'plan': 0.0},
                    )
                    for s in picks:
                        r['tasks'] += 1
                        if s.status == Task.Status.DONE:
                            r['done'] += 1
                        r['plan'] += s.plan_hours
                context.update({
                    'snapshots': picks_all,
                    'by_month': by_month,
                    'year': snap.year,
                })

    return render(request, 'manager/historical_reports.html', context)

# ═════════════════════════════════════════════════════════════
#  Сессии работы и цеха (перенос из admin_panel, патч 6.1)
# ═════════════════════════════════════════════════════════════
#
#  Раньше жили в /settings/sessions/ и /settings/sessions/summary/.
#  Теперь — /manager/sessions/ и /manager/sessions/summary/.
#  Старые URL'ы — редиректы.
#
#  Экспорт CSV остался в admin_panel (settings_sessions_export) —
#  это файл, не страница, отдельного смысла тащить его в manager нет.
# ═════════════════════════════════════════════════════════════


@login_required
def sessions_list(request):
    """Список сессий работы (TimeSession) или цеха (ShopSession) с фильтрами."""
    if not request.user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    from datetime import datetime as _dt, time as _time
    from tasks.models import TimeSession, ShopSession

    tab = request.GET.get('tab', 'work').strip()
    if tab not in ('work', 'shop'):
        tab = 'work'

    user_pk = request.GET.get('user', '').strip()
    task_pk = request.GET.get('task', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    min_hours = request.GET.get('min_hours', '').strip()

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    df = _parse_date(date_from)
    dt_ = _parse_date(date_to)

    def _aware_start(d):
        return timezone.make_aware(_dt.combine(d, _time.min), timezone.get_current_timezone())

    def _aware_end(d):
        return timezone.make_aware(_dt.combine(d, _time.max), timezone.get_current_timezone())

    try:
        min_h = float(min_hours.replace(',', '.')) if min_hours else None
    except ValueError:
        min_h = None

    if tab == 'work':
        qs = TimeSession.objects.select_related(
            'task', 'task__executor', 'executor'
        ).order_by('-finished_at')
        qs = _apply_boss_scope(qs, request.user, 'executor__department')
        if df:
            qs = qs.filter(finished_at__gte=_aware_start(df))
        if dt_:
            qs = qs.filter(finished_at__lte=_aware_end(dt_))
        if user_pk.isdigit():
            qs = qs.filter(executor_id=int(user_pk))
        if task_pk.isdigit():
            qs = qs.filter(task_id=int(task_pk))
        if min_h is not None:
            qs = qs.filter(duration_hours__gte=min_h)
    else:
        qs = ShopSession.objects.select_related(
            'task', 'task__executor', 'executor'
        ).order_by('-started_at')
        qs = _apply_boss_scope(qs, request.user, 'executor__department')
        if df:
            qs = qs.filter(started_at__gte=_aware_start(df))
        if dt_:
            qs = qs.filter(started_at__lte=_aware_end(dt_))
        if user_pk.isdigit():
            qs = qs.filter(executor_id=int(user_pk))
        if task_pk.isdigit():
            qs = qs.filter(task_id=int(task_pk))
        if min_h is not None:
            qs = qs.filter(duration_hours__gte=min_h)

    total = qs.count()
    total_hours = sum(s.duration_hours or 0 for s in qs[:5000])
    paginator = Paginator(qs, 100)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'manager/sessions.html', {
        'page': page,
        'total': total,
        'total_hours': total_hours,
        'page_qs': page_qs,
        'tab': tab,
        'user_pk': user_pk,
        'task_pk': task_pk,
        'date_from': date_from,
        'date_to': date_to,
        'min_hours': min_hours,
        'users': User.objects.filter(is_active=True).order_by('full_name'),
        'is_narrow_scope': _is_narrow_scope(request.user),
        'scope_department': request.user.department if _is_narrow_scope(request.user) else None,
    })


@login_required
def sessions_summary(request):
    """Сводка по сотрудникам за период: работа + цех, часы, сессии."""
    if not request.user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    from datetime import datetime as _dt, time as _time
    from tasks.models import TimeSession, ShopSession

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    df = _parse_date(date_from)
    dt_ = _parse_date(date_to)

    def _aware_start(d):
        return timezone.make_aware(_dt.combine(d, _time.min), timezone.get_current_timezone())

    def _aware_end(d):
        return timezone.make_aware(_dt.combine(d, _time.max), timezone.get_current_timezone())

    work_qs = TimeSession.objects.select_related('executor')
    shop_qs = ShopSession.objects.select_related('executor')
    work_qs = _apply_boss_scope(work_qs, request.user, 'executor__department')
    shop_qs = _apply_boss_scope(shop_qs, request.user, 'executor__department')

    if df:
        work_qs = work_qs.filter(finished_at__gte=_aware_start(df))
        shop_qs = shop_qs.filter(started_at__gte=_aware_start(df))
    if dt_:
        work_qs = work_qs.filter(finished_at__lte=_aware_end(dt_))
        shop_qs = shop_qs.filter(started_at__lte=_aware_end(dt_))

    work_agg = (
        work_qs.values('executor_id', 'executor__full_name', 'executor__email')
        .annotate(hours=Sum('duration_hours'), sessions=Count('id'))
        .order_by('-hours')
    )
    shop_agg = (
        shop_qs.values('executor_id', 'executor__full_name', 'executor__email')
        .annotate(hours=Sum('duration_hours'), sessions=Count('id'))
        .order_by('-hours')
    )

    by_user = {}
    for row in work_agg:
        by_user[row['executor_id']] = {
            'name': row['executor__full_name'] or '—',
            'email': row['executor__email'] or '',
            'work_hours': round(row['hours'] or 0, 2),
            'work_sessions': row['sessions'],
            'shop_hours': 0.0,
            'shop_sessions': 0,
        }
    for row in shop_agg:
        uid = row['executor_id']
        if uid in by_user:
            by_user[uid]['shop_hours'] = round(row['hours'] or 0, 2)
            by_user[uid]['shop_sessions'] = row['sessions']
        else:
            by_user[uid] = {
                'name': row['executor__full_name'] or '—',
                'email': row['executor__email'] or '',
                'work_hours': 0.0,
                'work_sessions': 0,
                'shop_hours': round(row['hours'] or 0, 2),
                'shop_sessions': row['sessions'],
            }

    rows = sorted(
        by_user.values(),
        key=lambda x: -(x['work_hours'] + x['shop_hours']),
    )

    grand = {
        'work_hours': round(sum(r['work_hours'] for r in rows), 2),
        'shop_hours': round(sum(r['shop_hours'] for r in rows), 2),
    }
    grand['total_hours'] = round(grand['work_hours'] + grand['shop_hours'], 2)

    # XLSX-экспорт
    if request.GET.get('format') == 'xlsx':
        from admin_panel.excel import build_xlsx, xlsx_response

        header = [
            'Сотрудник', 'Email',
            'Работа, ч', 'Сессий (работа)',
            'Цех, ч', 'Сессий (цех)',
            'Итого, ч',
        ]
        body = []
        for r in rows:
            total_h = round(r['work_hours'] + r['shop_hours'], 2)
            body.append([
                r['name'], r['email'],
                r['work_hours'], r['work_sessions'],
                r['shop_hours'], r['shop_sessions'],
                total_h,
            ])
        total = [
            'ИТОГО', '',
            round(grand['work_hours'], 2), '',
            round(grand['shop_hours'], 2), '',
            round(grand['total_hours'], 2),
        ]
        title = f'Сводка по сессиям {date_from or "—"} — {date_to or "—"}'
        data = build_xlsx(
            header=header, rows=body, total_row=total,
            sheet_name='Сессии',
            title=title,
            column_widths=[28, 30, 12, 16, 12, 14, 12],
        )
        fname = f'sessions_summary_{_dt.now().strftime("%Y%m%d_%H%M")}.xlsx'
        return xlsx_response(data, fname)

    # CSV-экспорт
    if request.GET.get('format') == 'csv':
        import csv as _csv
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'sessions_summary_{_dt.now().strftime("%Y%m%d_%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = _csv.writer(response, delimiter=';')
        writer.writerow([
            'Сотрудник', 'Email',
            'Работа, ч', 'Сессий (работа)',
            'Цех, ч', 'Сессий (цех)',
            'Итого, ч',
        ])
        for r in rows:
            total_h = r['work_hours'] + r['shop_hours']
            writer.writerow([
                r['name'], r['email'],
                f'{r["work_hours"]:.2f}'.replace('.', ','),
                r['work_sessions'],
                f'{r["shop_hours"]:.2f}'.replace('.', ','),
                r['shop_sessions'],
                f'{total_h:.2f}'.replace('.', ','),
            ])
        return response

    return render(request, 'manager/sessions_summary.html', {
        'rows': rows,
        'date_from': date_from,
        'date_to': date_to,
        'grand': grand,
    })

# ═════════════════════════════════════════════════════════════
#  Аналитика по подразделениям (перенос из admin_panel, патч 5.4b)
# ═════════════════════════════════════════════════════════════
#
#  Раньше жил в /settings/analytics/departments/.
#  Теперь — /manager/reports/?kind=departments.
#  Старый URL — редирект.
# ═════════════════════════════════════════════════════════════


@login_required
def analytics_departments_page(request):
    """Сводка по отделам за период: люди, задачи, план/факт, OTD.

    Параметры:
        ?date_from=ГГГГ-ММ-ДД  — начало периода (по умолчанию — 1-е текущего месяца)
        ?date_to=ГГГГ-ММ-ДД    — конец периода (по умолчанию — сегодня)
        ?format=csv            — экспорт
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    from datetime import datetime as _dt, time as _time

    today = timezone.localdate()
    default_from = today.replace(day=1)

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    date_from = _parse_date(request.GET.get('date_from', '')) or default_from
    date_to = _parse_date(request.GET.get('date_to', '')) or today

    start_dt = timezone.make_aware(
        _dt.combine(date_from, _time.min), timezone.get_current_timezone(),
    )
    end_dt = timezone.make_aware(
        _dt.combine(date_to, _time.max), timezone.get_current_timezone(),
    )

    # Все подразделения с активными сотрудниками
    depts = Department.objects.annotate(
        people_total=Count('users', distinct=True),
    ).filter(people_total__gt=0).order_by('name')

    # Boss — видит только своё подразделение
    if _is_narrow_scope(user):
        depts = depts.filter(pk=user.department_id)

    # Завершённые задачи в периоде
    done_qs = Task.objects.filter(
        status=Task.Status.DONE,
        finished_at__gte=start_dt, finished_at__lte=end_dt,
    )
    if _is_narrow_scope(user):
        done_qs = done_qs.filter(executor__department_id=user.department_id)

    # Задачи в работе (для просрочек)
    active_qs = Task.objects.exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    )
    if _is_narrow_scope(user):
        active_qs = active_qs.filter(executor__department_id=user.department_id)

    rows = []
    for d in depts:
        people_active = User.objects.filter(is_active=True, department=d).count()
        dept_done = list(done_qs.filter(executor__department=d))

        tasks_done = len(dept_done)
        plan_hours = sum(t.plan_hours or 0 for t in dept_done)
        fact_hours = sum(t.accumulated_hours or 0 for t in dept_done)
        on_time = sum(
            1 for t in dept_done
            if t.due and t.finished_at and t.finished_at.date() <= t.due
        )
        overdue_done = sum(
            1 for t in dept_done
            if t.due and t.finished_at and t.finished_at.date() > t.due
        )

        on_time_pct = int(round(100 * on_time / tasks_done)) if tasks_done else 0
        efficiency = int(round(100 * plan_hours / fact_hours)) if fact_hours > 0 else 0

        rows.append({
            'department': d,
            'people_total': d.people_total,
            'people_active': people_active,
            'tasks_done': tasks_done,
            'tasks_overdue_all': active_qs.filter(
                executor__department=d, due__lt=today,
            ).count(),
            'plan_hours': round(plan_hours, 2),
            'fact_hours': round(fact_hours, 2),
            'delta': round(fact_hours - plan_hours, 2),
            'on_time_pct': on_time_pct,
            'on_time': on_time,
            'overdue_done': overdue_done,
            'efficiency': efficiency,
            'plan_pct': 0,
            'fact_pct': 0,
        })

    max_hours = max((r['plan_hours'] + r['fact_hours'] for r in rows), default=0) or 1
    for r in rows:
        r['plan_pct'] = min(100, int(round(100 * r['plan_hours'] / max_hours)))
        r['fact_pct'] = min(100, int(round(100 * r['fact_hours'] / max_hours)))

    grand = {
        'people_total': sum(r['people_total'] for r in rows),
        'people_active': sum(r['people_active'] for r in rows),
        'tasks_done': sum(r['tasks_done'] for r in rows),
        'plan_hours': round(sum(r['plan_hours'] for r in rows), 2),
        'fact_hours': round(sum(r['fact_hours'] for r in rows), 2),
        'on_time': sum(r['on_time'] for r in rows),
    }
    grand['delta'] = round(grand['fact_hours'] - grand['plan_hours'], 2)
    grand['on_time_pct'] = (
        int(round(100 * grand['on_time'] / grand['tasks_done']))
        if grand['tasks_done'] else 0
    )
    grand['efficiency'] = (
        int(round(100 * grand['plan_hours'] / grand['fact_hours']))
        if grand['fact_hours'] > 0 else 0
    )

    # CSV-экспорт
    if request.GET.get('format') == 'csv':
        import csv as _csv
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'departments_{date_from}_{date_to}_{_dt.now().strftime("%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = _csv.writer(response, delimiter=';')
        writer.writerow([
            'Подразделение', 'Сотрудников (акт./всего)',
            'Задач готово', 'В срок', 'Просрочено', 'В срок, %',
            'План, ч', 'Факт, ч', 'Δ, ч', 'КПД, %',
        ])
        for r in rows:
            writer.writerow([
                r['department'].name,
                f'{r["people_active"]}/{r["people_total"]}',
                r['tasks_done'],
                r['on_time'],
                r['overdue_done'],
                r['on_time_pct'],
                f'{r["plan_hours"]:.2f}'.replace('.', ','),
                f'{r["fact_hours"]:.2f}'.replace('.', ','),
                f'{r["delta"]:.2f}'.replace('.', ','),
                r['efficiency'],
            ])
        writer.writerow([
            'ИТОГО', f'{grand["people_active"]}/{grand["people_total"]}',
            grand['tasks_done'], grand['on_time'], '', grand['on_time_pct'],
            f'{grand["plan_hours"]:.2f}'.replace('.', ','),
            f'{grand["fact_hours"]:.2f}'.replace('.', ','),
            f'{grand["delta"]:.2f}'.replace('.', ','),
            grand['efficiency'],
        ])
        return response

    return render(request, 'manager/analytics_departments.html', {
        'rows': rows,
        'grand': grand,
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'is_narrow_scope': _is_narrow_scope(user),
        'scope_department': user.department if _is_narrow_scope(user) else None,
        'active': 'departments',
    })

# ═════════════════════════════════════════════════════════════
#  KPI-отчёт (перенос из admin_panel, патч 5.4a)
# ═════════════════════════════════════════════════════════════
#
#  Раньше жил в /settings/kpi/. Теперь — /manager/reports/?kind=kpi.
#  Логика не менялась. Старый URL /settings/kpi/ — редирект.
# ═════════════════════════════════════════════════════════════


@login_required
def kpi_report_page(request):
    """KPI-отчёт по сотрудникам с фильтром по периоду и подразделению.

    Параметры:
        ?date_from=ГГГГ-ММ-ДД  — начало периода (по умолчанию — 1-е текущего месяца)
        ?date_to=ГГГГ-ММ-ДД    — конец периода (по умолчанию — сегодня)
        ?dept=<pk>             — фильтр по подразделению (пусто = все)
        ?format=csv|xlsx       — экспорт
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    from datetime import datetime as _dt, time as _time
    from tasks.models import Task, TimeSession
    from admin_panel.audit import log_action
    from admin_panel.models import AuditLog

    # Период — по умолчанию текущий месяц
    today = timezone.localdate()
    default_from = today.replace(day=1)
    default_to = today

    def _parse_date(s):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    date_from = _parse_date(request.GET.get('date_from', '')) or default_from
    date_to = _parse_date(request.GET.get('date_to', '')) or default_to
    dept_pk = (request.GET.get('dept') or '').strip()

    start_dt = timezone.make_aware(
        _dt.combine(date_from, _time.min), timezone.get_current_timezone(),
    )
    end_dt = timezone.make_aware(
        _dt.combine(date_to, _time.max), timezone.get_current_timezone(),
    )

    # Базовые query — только завершённые задачи в периоде
    done_tasks = Task.objects.filter(
        status=Task.Status.DONE,
        finished_at__gte=start_dt,
        finished_at__lte=end_dt,
    ).select_related('executor', 'executor__department')

    if dept_pk.isdigit():
        done_tasks = done_tasks.filter(executor__department_id=int(dept_pk))
    done_tasks = _apply_boss_scope(done_tasks, request.user, 'executor__department')

    # Считаем по сотрудникам
    per_user = {}
    for t in done_tasks:
        u = t.executor
        if not u:
            continue
        row = per_user.setdefault(u.pk, {
            'user': u,
            'tasks_done': 0,
            'plan_hours': 0.0,
            'fact_hours': 0.0,
            'on_time': 0,
            'overdue': 0,
        })
        row['tasks_done'] += 1
        row['plan_hours'] += t.plan_hours or 0
        row['fact_hours'] += t.accumulated_hours or 0
        if t.due and t.finished_at and t.finished_at.date() <= t.due:
            row['on_time'] += 1
        elif t.due and t.finished_at:
            row['overdue'] += 1

    # Дополняем часами из TimeSession (на случай если accumulated не полный)
    sessions_agg = (
        TimeSession.objects
        .filter(finished_at__gte=start_dt, finished_at__lte=end_dt)
        .values('executor_id')
        .annotate(h=Sum('duration_hours'))
    )
    session_hours = {row['executor_id']: row['h'] or 0.0 for row in sessions_agg}

    rows = list(per_user.values())
    for r in rows:
        r['session_hours'] = round(session_hours.get(r['user'].pk, 0.0), 2)
        r['fact_hours'] = round(r['fact_hours'], 2)
        r['plan_hours'] = round(r['plan_hours'], 2)
        r['delta'] = round(r['fact_hours'] - r['plan_hours'], 2)
        r['on_time_pct'] = (
            int(round(100 * r['on_time'] / r['tasks_done']))
            if r['tasks_done'] else 0
        )
        r['efficiency'] = (
            int(round(100 * r['plan_hours'] / r['fact_hours']))
            if r['fact_hours'] > 0 else 0
        )

    rows.sort(key=lambda x: -x['fact_hours'])

    # Топ переработок (факт > план сильно)
    top_over = sorted(
        [r for r in rows if r['fact_hours'] > r['plan_hours'] and r['fact_hours'] > 0],
        key=lambda x: -(x['fact_hours'] - x['plan_hours']),
    )[:5]

    # Топ переделок (план > факт — сделал быстрее плана)
    top_fast = sorted(
        [r for r in rows if r['plan_hours'] > r['fact_hours'] and r['fact_hours'] > 0],
        key=lambda x: -(x['plan_hours'] - x['fact_hours']),
    )[:5]

    # Топ просроченных задач в периоде (все статусы)
    overdue_tasks_qs = (
        Task.objects
        .select_related('executor', 'requester')
        .filter(due__gte=date_from, due__lte=date_to, due__lt=today)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('due')
    )
    overdue_tasks_qs = _apply_boss_scope(overdue_tasks_qs, request.user, 'executor__department')
    overdue_tasks = overdue_tasks_qs[:10]

    # ── Общие итоги ──
    grand = {
        'tasks_done': sum(r['tasks_done'] for r in rows),
        'plan_hours': round(sum(r['plan_hours'] for r in rows), 2),
        'fact_hours': round(sum(r['fact_hours'] for r in rows), 2),
        'on_time': sum(r['on_time'] for r in rows),
        'overdue': sum(r['overdue'] for r in rows),
    }
    grand['delta'] = round(grand['fact_hours'] - grand['plan_hours'], 2)
    grand['on_time_pct'] = (
        int(round(100 * grand['on_time'] / grand['tasks_done']))
        if grand['tasks_done'] else 0
    )

    # ── Экспорт XLSX ──
    if request.GET.get('format') == 'xlsx':
        from admin_panel.excel import build_xlsx, xlsx_response

        header = [
            'Сотрудник', 'Подразделение', 'Роль',
            'Задач готово', 'В срок', 'Просрочено', 'В срок, %',
            'План, ч', 'Факт, ч', 'Δ, ч', 'КПД, %', 'Сессий, ч',
        ]
        body = []
        for r in rows:
            body.append([
                r['user'].full_name,
                r['user'].department.name if r['user'].department else '',
                r['user'].role.name if r['user'].role else '',
                r['tasks_done'],
                r['on_time'],
                r['overdue'],
                r['on_time_pct'],
                r['plan_hours'],
                r['fact_hours'],
                r['delta'],
                r['efficiency'],
                r['session_hours'],
            ])
        total = [
            'ИТОГО', '', '',
            grand['tasks_done'], grand['on_time'], grand['overdue'],
            grand['on_time_pct'],
            grand['plan_hours'], grand['fact_hours'], grand['delta'],
            '', '',
        ]
        data = build_xlsx(
            header=header, rows=body, total_row=total,
            sheet_name='KPI',
            title=f'KPI {date_from} — {date_to}',
            column_widths=[28, 22, 20, 12, 10, 12, 10, 12, 12, 10, 10, 12],
        )
        fname = f'kpi_{date_from}_{date_to}_{_dt.now().strftime("%H%M")}.xlsx'
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'kpi_xlsx',
            'period': f'{date_from}..{date_to}',
            'rows': len(rows),
        })
        return xlsx_response(data, fname)

    # ── Экспорт CSV ──
    if request.GET.get('format') == 'csv':
        import csv as _csv
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        fname = f'kpi_{date_from}_{date_to}_{_dt.now().strftime("%H%M")}.csv'
        response['Content-Disposition'] = f'attachment; filename="{fname}"'
        response.write('\ufeff')
        writer = _csv.writer(response, delimiter=';')
        writer.writerow([
            'Сотрудник', 'Подразделение', 'Роль',
            'Задач готово', 'В срок', 'Просрочено', 'В срок, %',
            'План, ч', 'Факт, ч', 'Δ, ч', 'КПД, %', 'Сессий, ч',
        ])
        for r in rows:
            writer.writerow([
                r['user'].full_name,
                r['user'].department.name if r['user'].department else '',
                r['user'].role.name if r['user'].role else '',
                r['tasks_done'],
                r['on_time'],
                r['overdue'],
                r['on_time_pct'],
                f'{r["plan_hours"]:.2f}'.replace('.', ','),
                f'{r["fact_hours"]:.2f}'.replace('.', ','),
                f'{r["delta"]:.2f}'.replace('.', ','),
                r['efficiency'],
                f'{r["session_hours"]:.2f}'.replace('.', ','),
            ])
        writer.writerow([
            'ИТОГО', '', '',
            grand['tasks_done'], grand['on_time'], grand['overdue'], grand['on_time_pct'],
            f'{grand["plan_hours"]:.2f}'.replace('.', ','),
            f'{grand["fact_hours"]:.2f}'.replace('.', ','),
            f'{grand["delta"]:.2f}'.replace('.', ','),
            '', '',
        ])
        log_action(request, AuditLog.Action.UPDATE, request.user, changes={
            'export': 'kpi_csv',
            'period': f'{date_from}..{date_to}',
            'rows': len(rows),
        })
        return response

    return render(request, 'manager/kpi_report.html', {
        'rows': rows,
        'grand': grand,
        'top_over': top_over,
        'top_fast': top_fast,
        'overdue_tasks': overdue_tasks,
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'dept_pk': dept_pk,
        'departments': Department.objects.all().order_by('name'),
        'today': today,
        'is_narrow_scope': _is_narrow_scope(request.user),
        'scope_department': request.user.department if _is_narrow_scope(request.user) else None,
        'active': 'kpi',
    })

def _is_narrow_scope(user):
    return bool(
        not user.is_superuser
        and not user.can_plant
        and not user.is_admin_role
        and user.is_boss
        and user.department_id
    )


def _apply_boss_scope(qs, user, dept_path):
    """Сузить queryset по scope смотрящего.

    admin/plant → всё видно, фильтр не применяется.
    department-manager → только свой отдел (через dept_path).
    boss без отдела → пусто (нельзя показать «неизвестно чей» отдел).
    """
    if user.is_superuser or user.can_plant or user.is_admin_role:
        return qs
    if user.department_id:
        return qs.filter(**{dept_path: user.department_id})
    return qs.none()

# ═════════════════════════════════════════════════════════════
#  Отчёты: единый URL с переключателем периода
# ═════════════════════════════════════════════════════════════
#
#  До патча 5.1 было 3 URL'а:
#      /manager/reports/    — месяц
#      /manager/quarterly/  — квартал
#      /manager/yearly/     — год
#
#  Теперь одна точка входа:
#      /manager/reports/?period=month|quarter|year
#
#  Тело каждой страницы не менялось — роутер просто делегирует
#  в нужную функцию. Старые URL'ы сохранены как редиректы,
#  чтобы не ломать внешние закладки и ссылки в чатах.
# ═════════════════════════════════════════════════════════════


@login_required
def reports_router(request):
    """Единая точка отчётов.

    Параметры:
        ?kind=period|analytics|dynamics|orders|kpi|departments
            period      — сводка за период (по умолчанию)
            analytics   — динамика и качество (бывш. /manager/analytics/)
            dynamics    — динамика отдела по дням (бывш. /manager/dept-dynamics/)
            orders      — отчёт по заказам (бывш. /manager/orders-report/)
            kpi         — KPI-отчёт по сотрудникам (бывш. /settings/kpi/)
            departments — разрез по подразделениям (бывш. /settings/analytics/departments/)

        ?period=month|quarter|year
            только для kind=period (по умолчанию). Управляет тем,
            какой из трёх отчётов показать: месячный, квартальный, годовой.
    """
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    kind = (request.GET.get('kind') or 'period').strip()

    if kind == 'analytics':
        return analytics_page(request)
    if kind == 'dynamics':
        return dept_dynamics(request)
    if kind == 'orders':
        return orders_report(request)
    if kind == 'kpi':
        return kpi_report_page(request)
    if kind == 'departments':
        return analytics_departments_page(request)

    # kind == 'period' — обычная сводка
    period = (request.GET.get('period') or 'month').strip()
    if period == 'quarter':
        return quarterly_report(request)
    if period == 'year':
        return yearly_report(request)
    return reports_page(request)


@login_required
def analytics_redirect(request):
    """Старый /manager/analytics/ → /manager/reports/?kind=analytics."""
    qs = request.GET.copy()
    qs['kind'] = 'analytics'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")


@login_required
def dept_dynamics_redirect(request):
    """Старый /manager/dept-dynamics/ → /manager/reports/?kind=dynamics."""
    qs = request.GET.copy()
    qs['kind'] = 'dynamics'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")


@login_required
def orders_report_redirect(request):
    """Старый /manager/orders-report/ → /manager/reports/?kind=orders."""
    qs = request.GET.copy()
    qs['kind'] = 'orders'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")

@login_required
def quarterly_redirect(request):
    """Старый /manager/quarterly/ → /manager/reports/?period=quarter.

    Сохраняет все GET-параметры (year, quarter и т.п.).
    """
    qs = request.GET.copy()
    qs['period'] = 'quarter'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")


@login_required
def yearly_redirect(request):
    """Старый /manager/yearly/ → /manager/reports/?period=year.

    Сохраняет все GET-параметры (year и т.п.).
    """
    qs = request.GET.copy()
    qs['period'] = 'year'
    return redirect(f"{reverse('manager_reports')}?{qs.urlencode()}")

@login_required
def capture_timeline_snapshot(request):
    from tasks.models import TimelineSnapshot

    user = request.user
    if not (user.is_boss or user.is_admin_role):
        raise PermissionDenied('Доступно руководителю или администратору.')

    if request.method != 'POST':
        return redirect(_safe_redirect_url(request))

    snapshot_date = timezone.localdate().replace(day=1)

    qs = Task.objects.filter(
        Q(start_due__isnull=False) | Q(due__isnull=False)
    ).select_related('executor')

    count = 0
    for t in qs:
        TimelineSnapshot.objects.update_or_create(
            snapshot_date=snapshot_date,
            task=t,
            defaults={
                'executor': t.executor,
                'start_date': t.start_due or _local_date(t.created_at),
                'due_date': t.due,
                'status': t.status,
                'priority': t.priority,
                'plan_hours': t.plan_hours or 0,
            },
        )
        count += 1

    messages.success(request, f'Снимок на {snapshot_date:%d.%m.%Y}: {count} задач.')
    return redirect(_safe_redirect_url(request))
# ─────────────────────────────────────────────────────────────
# Ветки заказа: конструктор этапов
# ─────────────────────────────────────────────────────────────

@login_required
def order_branch_add(request, pk):
    """Создать ветку в заказе (POST)."""
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    order = get_object_or_404(Order, pk=pk)

    if request.method != 'POST':
        return redirect('order_plan', pk=order.pk)

    name = (request.POST.get('name') or '').strip()[:150]
    if not name:
        messages.error(request, 'Укажите название ветки.')
        return redirect('order_plan', pk=order.pk)

    if TaskBranch.objects.filter(order=order, name__iexact=name).exists():
        messages.error(request, f'Ветка «{name}» уже есть в этом заказе.')
        return redirect('order_plan', pk=order.pk)

    TaskBranch.objects.create(order=order, name=name)
    messages.success(request, f'Ветка «{name}» создана.')
    return redirect('order_plan', pk=order.pk)


@login_required
def order_branch_delete(request, pk, branch_pk):
    """Удалить ветку (POST). Этапы отвязываются от ветки, задачи не удаляются."""
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    order = get_object_or_404(Order, pk=pk)
    branch = get_object_or_404(TaskBranch, pk=branch_pk, order=order)

    if request.method != 'POST':
        return redirect('order_plan', pk=order.pk)

    # Отвязываем задачи от ветки
    Task.objects.filter(branch=branch).update(
        branch=None, stage_order=None, blocked_by_stage=False,
    )

    name = branch.name
    branch.delete()
    messages.success(request, f'Ветка «{name}» удалена, задачи отвязаны.')
    return redirect('order_plan', pk=order.pk)


@login_required
def order_branch_stages_add(request, pk, branch_pk):
    """Добавить этапы в ветку (POST). Первый этап партии — активен, остальные заблокированы."""
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    order = get_object_or_404(Order, pk=pk)
    branch = get_object_or_404(TaskBranch, pk=branch_pk, order=order)

    if request.method != 'POST':
        return redirect('order_plan', pk=order.pk)

    titles = request.POST.getlist('stage_title')
    exec_ids = request.POST.getlist('stage_executor')
    kinds = request.POST.getlist('stage_kind')
    prios = request.POST.getlist('stage_priority')
    plans = request.POST.getlist('stage_plan')
    starts = request.POST.getlist('stage_start')
    dues = request.POST.getlist('stage_due')

    # С какого stage_order начинать
    last = Task.objects.filter(branch=branch).order_by('-stage_order').first()
    next_order = (last.stage_order + 1) if last and last.stage_order else 1

    created, errors = 0, []
    is_first_in_batch = (next_order == 1)

    for i, raw_title in enumerate(titles):
        title = (raw_title or '').strip()
        if not title:
            continue

        ex_pk = exec_ids[i] if i < len(exec_ids) else ''
        ex = (
            User.objects.filter(pk=ex_pk, is_active=True).first()
            if ex_pk.isdigit() else None
        )
        if ex is None:
            errors.append(f'Строка {i + 1}: не выбран исполнитель.')
            continue

        plan = parse_plan(plans[i] if i < len(plans) else '', 's', norm_hours()) or 0
        start = _parse_date(starts[i] if i < len(starts) else '')
        due = _parse_date(dues[i] if i < len(dues) else '')

        if start and due and start > due:
            errors.append(f'Строка {i + 1}: начало позже дедлайна.')
            continue

        kind = kinds[i] if i < len(kinds) and kinds[i] in Task.Kind.values else 'work'
        prio = prios[i] if i < len(prios) and prios[i] in Task.Priority.values else 'medium'

        stage_order = next_order + created
        # Первый в партии — активен, если в ветке ещё ничего нет
        blocked = not (created == 0 and is_first_in_batch)

        t = Task.objects.create(
            title=title, plan_hours=plan, start_due=start, due=due,
            priority=prio, scale='s', kind=kind,
            order=order, branch=branch, stage_order=stage_order,
            blocked_by_stage=blocked,
            executor=ex, requester=user,
        )

        if blocked:
            TaskLog.objects.create(
                task=t, kind=TaskLog.Kind.DUE, author=user,
                comment=f'Ожидает завершения предыдущего этапа ветки «{branch.name}».',
            )
        else:
            try:
                notify_task_created(t)
            except Exception:
                pass

        created += 1

    if created:
        messages.success(request, f'Добавлено этапов в ветку «{branch.name}»: {created}.')
    for e in errors:
        messages.error(request, e)

    return redirect('order_plan', pk=order.pk)


@login_required
def stage_move(request, pk, direction):
    """Поменять этап местами с соседом в ветке. direction: 'up' | 'down'."""
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    task = get_object_or_404(Task, pk=pk)
    back = request.META.get('HTTP_REFERER', '/')

    if not task.branch_id or task.stage_order is None:
        messages.error(request, 'Задача не в ветке.')
        return redirect(back)

    if direction == 'up':
        other = (
            Task.objects.filter(
                branch_id=task.branch_id,
                stage_order__lt=task.stage_order,
            ).order_by('-stage_order').first()
        )
    elif direction == 'down':
        other = (
            Task.objects.filter(
                branch_id=task.branch_id,
                stage_order__gt=task.stage_order,
            ).order_by('stage_order').first()
        )
    else:
        other = None

    if other is None:
        messages.info(request, 'Нечего менять — этап уже крайний.')
        return redirect(back)

    # Меняем местами stage_order
    a, b = task.stage_order, other.stage_order
    task.stage_order = b
    other.stage_order = a
    task.save(update_fields=['stage_order'])
    other.save(update_fields=['stage_order'])

    messages.success(request, 'Порядок этапов обновлён.')
    return redirect(back)
# ─────────────────────────────────────────────────────────────
# Логи задач отдела
# ─────────────────────────────────────────────────────────────

@login_required
def task_logs_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    member_ids = _scope_user_ids(user)

    kind = request.GET.get('kind', '').strip()
    author_pk = request.GET.get('author', '').strip()
    task_pk = request.GET.get('task', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    qs = (
        TaskLog.objects
        .select_related('task', 'author', 'source_task')
        .filter(task__executor_id__in=member_ids)
        .order_by('-created_at')
    )
    if kind:
        qs = qs.filter(kind=kind)
    if author_pk.isdigit():
        qs = qs.filter(author_id=int(author_pk))
    if task_pk.isdigit():
        qs = qs.filter(task_id=int(task_pk))

    df = _parse_date(date_from)
    dt_ = _parse_date(date_to)
    if df:
        qs = qs.filter(created_at__gte=timezone.make_aware(
            datetime.combine(df, datetime.min.time()),
            timezone.get_current_timezone(),
        ))
    if dt_:
        qs = qs.filter(created_at__lte=timezone.make_aware(
            datetime.combine(dt_, datetime.max.time()),
            timezone.get_current_timezone(),
        ))

    total = qs.count()

    kind_stats = (
        qs.values('kind').annotate(c=Count('id')).order_by('-c')
    )
    kind_labels = dict(TaskLog.Kind.choices)
    kind_stats = [
        {'code': row['kind'], 'label': kind_labels.get(row['kind'], row['kind']),
         'count': row['c']}
        for row in kind_stats
    ]

    paginator = Paginator(qs, 100)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    page_qs = params.urlencode()

    return render(request, 'manager/task_logs.html', {
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'kind': kind,
        'author_pk': author_pk,
        'task_pk': task_pk,
        'date_from': date_from,
        'date_to': date_to,
        'kind_choices': TaskLog.Kind.choices,
        'kind_stats': kind_stats,
        'users': User.objects.filter(
            pk__in=member_ids, is_active=True,
        ).order_by('full_name'),
        'page_title': 'Логи задач',
    })
# ─────────────────────────────────────────────────────────────
# Кто онлайн (отдел руководителя)
# ─────────────────────────────────────────────────────────────

@login_required
def online_page(request):
    user = request.user
    if not user.is_boss:
        raise PermissionDenied('Раздел доступен руководителю.')

    now = timezone.now()
    online_from = now - timedelta(minutes=15)
    recent_from = now - timedelta(hours=1)

    member_ids = _scope_user_ids(user)

    online_users = list(
        User.objects
        .select_related('department', 'role')
        .filter(pk__in=member_ids, is_active=True, last_activity_at__gte=online_from)
        .order_by('-last_activity_at')
    )

    active_task_map = {}
    for u in online_users:
        if u.idle_since:
            continue
        t = Task.objects.filter(
            executor=u, status=Task.Status.IN_PROGRESS,
        ).first()
        if t:
            active_task_map[u.pk] = t

    active_shop_map = {
        s.executor_id: s
        for s in ShopSession.objects.filter(
            executor_id__in=member_ids, finished_at__isnull=True,
        ).select_related('task')
    }

    online_rows = []
    for u in online_users:
        online_rows.append({
            'user': u,
            'task': active_task_map.get(u.pk),
            'shop': active_shop_map.get(u.pk),
            'idle_since': u.idle_since,
            'last_activity_at': u.last_activity_at,
        })

    recent_users = list(
        User.objects
        .select_related('department', 'role')
        .filter(
            pk__in=member_ids, is_active=True,
            last_activity_at__lt=online_from,
            last_activity_at__gte=recent_from,
        )
        .order_by('-last_activity_at')[:50]
    )

    stale_users = list(
        User.objects
        .select_related('department', 'role')
        .filter(pk__in=member_ids, is_active=True)
        .exclude(last_activity_at__gte=online_from)
        .order_by('last_activity_at')[:10]
    )

    return render(request, 'manager/online.html', {
        'online_rows': online_rows,
        'recent_users': recent_users,
        'stale_users': stale_users,
        'online_count': len(online_rows),
        'now': now,
        'page_title': 'Кто онлайн',
    })
# ─────────────────────────────────────────────────────────────
#  Drag-and-drop: сохранение порядка этапов
# ─────────────────────────────────────────────────────────────

@login_required
def order_stages_reorder(request, pk):
    """Сохраняет новый порядок этапов после drag-and-drop.

    Принимает JSON:
    {
      "items": [
        {"branch_id": 5, "task_ids": [12, 15, 17]},
        {"branch_id": 6, "task_ids": [20]}
      ]
    }
    """
    user = request.user
    if not user.is_boss:
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'method'}, status=405)

    order = get_object_or_404(Order, pk=pk)

    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'bad json'}, status=400)

    items = data.get('items') or []
    updated = 0
    moved_between = 0

    with transaction.atomic():
        # 1. Обновляем порядок и ветки
        for item in items:
            branch_id = item.get('branch_id')
            task_ids = item.get('task_ids') or []

            branch = None
            if branch_id:
                branch = TaskBranch.objects.filter(pk=branch_id, order=order).first()
                if not branch:
                    continue

            for idx, tid in enumerate(task_ids, start=1):
                t = Task.objects.filter(pk=tid, order=order).first()
                if not t:
                    continue
                if t.branch_id != branch_id:
                    moved_between += 1
                t.branch = branch
                t.stage_order = idx
                t.save(update_fields=['branch', 'stage_order'])
                updated += 1

        # 2. Пересчитываем blocked_by_stage во всех ветках
        for branch in TaskBranch.objects.filter(order=order):
            stages = list(
                Task.objects
                .filter(branch=branch, order=order)
                .exclude(status=Task.Status.CANCELLED)
                .order_by('stage_order')
            )
            prev_done = True
            for i, t in enumerate(stages):
                new_order = i + 1
                should_block = (i > 0) and (not prev_done)

                fields = []
                if t.stage_order != new_order:
                    t.stage_order = new_order
                    fields.append('stage_order')
                if t.blocked_by_stage != should_block:
                    t.blocked_by_stage = should_block
                    fields.append('blocked_by_stage')
                if fields:
                    t.save(update_fields=fields)
                prev_done = (t.status == Task.Status.DONE)

    return JsonResponse({
        'ok': True,
        'updated': updated,
        'moved_between': moved_between,
    })
# ─────────────────────────────────────────────────────────────
#  Заявки на сдвиг плана
# ─────────────────────────────────────────────────────────────

def _can_review_shift(user):
    """Проверяет, является ли пользователь согласующим хоть по одной заявке."""
    return PlanShiftRequest.objects.filter(
        recipients=user,
        status=PlanShiftRequest.Status.PENDING,
    ).exists()


@login_required
def shift_requests_list(request):
    """Список заявок: «мои на согласование» и «все в отделе»."""
    user = request.user
    if not user.is_boss and not _can_review_shift(user):
        raise PermissionDenied('Раздел доступен согласующим.')

    my_qs = (
        PlanShiftRequest.objects
        .filter(recipients=user, status=PlanShiftRequest.Status.PENDING)
        .select_related('source_task', 'source_task__executor', 'department',
                        'initiated_by')
        .order_by('escalate_at')
    )

    # Для админов/начальника завода — ещё и все активные по заводу
    all_qs = PlanShiftRequest.objects.none()
    if user.is_admin_role or user.can_plant:
        all_qs = (
            PlanShiftRequest.objects
            .filter(status=PlanShiftRequest.Status.PENDING)
            .exclude(recipients=user)
            .select_related('source_task', 'source_task__executor', 'department',
                            'initiated_by')
            .order_by('escalate_at')
        )

    history_qs = (
        PlanShiftRequest.objects
        .filter(resolved_by=user)
        .select_related('source_task', 'resolved_by')
        .order_by('-resolved_at')[:50]
    )

    return render(request, 'manager/shift_requests.html', {
        'my_requests': list(my_qs),
        'all_requests': list(all_qs),
        'history': list(history_qs),
        'page_title': 'Заявки на сдвиг',
    })


@login_required
def shift_request_detail(request, pk):
    req = get_object_or_404(
        PlanShiftRequest.objects.select_related(
            'source_task', 'source_task__executor', 'source_task__executor__department',
            'department', 'initiated_by',
        ),
        pk=pk,
    )

    user = request.user
    can_review = (
            req.status == PlanShiftRequest.Status.PENDING
            and req.recipients.filter(pk=user.pk).exists()
    )
    can_view = can_review or user.is_boss or user.is_admin_role

    if not can_view:
        raise PermissionDenied('Нет доступа к заявке.')

    # Кто уже проигнорировал на предыдущих уровнях
    ignored = list(
        PlanShiftStep.objects
        .filter(request=req, decision=PlanShiftStep.Decision.TIMEOUT)
        .select_related('reviewer')
        .order_by('sent_at')
    )

    affected = list(
        req.affected_tasks.select_related('executor').order_by('due')
    )

    return render(request, 'manager/shift_request_detail.html', {
        'req': req,
        'can_review': can_review,
        'affected': affected,
        'ignored': ignored,
        'page_title': f'Заявка #{req.pk}',
    })


@login_required
@require_POST
def shift_request_approve(request, pk):
    req = get_object_or_404(PlanShiftRequest, pk=pk)
    if not req.recipients.filter(pk=request.user.pk).exists():
        raise PermissionDenied('Нет прав.')

    note = (request.POST.get('note') or '').strip()[:500]
    if plan_shift.approve(req, request.user, note):
        messages.success(request, f'Заявка #{req.pk} одобрена. Сдвиг применён.')
    else:
        messages.error(request, 'Заявка уже решена.')

    return redirect('shift_requests_list')


@login_required
@require_POST
def shift_request_reject(request, pk):
    req = get_object_or_404(PlanShiftRequest, pk=pk)
    if not req.recipients.filter(pk=request.user.pk).exists():
        raise PermissionDenied('Нет прав.')

    note = (request.POST.get('note') or '').strip()[:500]
    if plan_shift.reject(req, request.user, note):
        messages.success(request, f'Заявка #{req.pk} отклонена. Задача отменена.')
    else:
        messages.error(request, 'Заявка уже решена.')

    return redirect('shift_requests_list')


@login_required
def shift_request_squeeze(request, pk):
    """Страница «Ужать сроки»: показать варианты и применить выбранные."""
    req = get_object_or_404(
        PlanShiftRequest.objects.select_related(
            'source_task', 'source_task__executor',
            'department', 'initiated_by',
        ),
        pk=pk,
    )

    if not req.recipients.filter(pk=request.user.pk).exists():
        raise PermissionDenied('Нет прав.')

    if req.status != PlanShiftRequest.Status.PENDING:
        messages.info(request, 'Заявка уже решена.')
        return redirect('shift_request_detail', pk=req.pk)

    source = req.source_task

    # Кандидаты на перекидывание
    candidates = plan_shift.find_reassign_candidates(source)

    # Задачи исполнителя, которые можно перекинуть
    open_tasks = list(
        Task.objects
        .filter(executor=source.executor)
        .exclude(pk=source.pk)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('due', 'priority')
    )

    # Следующие этапы цепочки
    chain_tasks = plan_shift.find_chain_tasks(source)

    # Можно ли разбить
    split_ok = plan_shift.can_split(source)

    if request.method == 'POST':
        note = (request.POST.get('note') or '').strip()[:500]

        # Перекидывание
        reassign = {}
        for key, value in request.POST.items():
            if key.startswith('reassign_') and value.isdigit():
                task_id = key.replace('reassign_', '')
                reassign[int(task_id)] = int(value)

        # Разблокировка
        unblock = [
            int(x) for x in request.POST.getlist('unblock')
            if x.isdigit()
        ]

        # Разбиение
        split_action = None
        if split_ok and request.POST.get('split_enabled') == '1':
            try:
                first_hours = float(request.POST.get('split_first') or 0)
                second_days = int(request.POST.get('split_second_days') or 0)
            except ValueError:
                first_hours, second_days = 0, 0
            if first_hours > 0 and second_days > 0:
                split_action = {
                    'first_hours': first_hours,
                    'second_days': second_days,
                }

        if not reassign and not unblock and not split_action:
            messages.error(request, 'Ничего не выбрано.')
        else:
            result = plan_shift.apply_squeeze(
                req, request.user,
                {
                    'reassign': reassign,
                    'unblock': unblock,
                    'split': split_action,
                },
                note=note,
            )
            messages.success(
                request,
                f'Ужали: перекинуто {result["moved"]}, '
                f'разблокировано {result["unblocked"]}'
                + (', source разбит' if result['split'] else '') + '.'
            )
            return redirect('shift_request_detail', pk=req.pk)

    return render(request, 'manager/shift_request_squeeze.html', {
        'req': req,
        'source': source,
        'candidates': candidates,
        'open_tasks': open_tasks,
        'chain_tasks': chain_tasks,
        'split_ok': split_ok,
        'min_split_hours': plan_shift.MIN_SPLIT_HOURS,
        'page_title': 'Ужать сроки',
    })


@login_required
def department_hours_page(request):
    """Управление нормой часов по подразделениям.

    Доступно начальнику завода (can_plant) и админам портала.
    Руководитель отдела видит нормы, но не меняет — они приходят сверху.

    Логика:
      - пустое значение для отдела → используется глобальная норма;
      - число > 0 → override для отдела;
      - глобальная норма задаётся отдельно через core.Norm.
    """
    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    if request.method == 'POST':
        target = (request.POST.get('target') or '').strip()
        raw = (request.POST.get('hours_per_day') or '').strip().replace(',', '.')

        if target == 'global':
            # ── Глобальная норма (core.Norm) ──
            try:
                val = float(raw) if raw else None
            except ValueError:
                messages.error(request, 'Некорректное число.')
                return redirect('manager_department_hours')

            if val is None or val <= 0:
                messages.error(
                    request,
                    'Глобальная норма должна быть числом больше нуля.',
                )
                return redirect('manager_department_hours')

            norm = Norm.objects.order_by('-id').first()
            if norm is None:
                norm = Norm()
            norm.hours_per_day = val
            norm.save()

            # Сбросить кэш — сигнала на Norm пока нет.
            from django.core.cache import cache
            cache.delete('norm_hours_per_day')

            messages.success(request, f'Глобальная норма: {val} ч/день.')
            return redirect('manager_department_hours')

        if target.isdigit():
            # ── Override для отдела ──
            dept = Department.objects.filter(pk=int(target)).first()
            if dept is None:
                messages.error(request, 'Подразделение не найдено.')
                return redirect('manager_department_hours')

            if not raw:
                # Пустое значение → снять override.
                if dept.hours_per_day is not None:
                    dept.hours_per_day = None
                    dept.save(update_fields=['hours_per_day'])
                    messages.success(
                        request,
                        f'«{dept.name}»: вернулись к глобальной норме.',
                    )
                else:
                    messages.info(request, 'Изменений нет.')
                return redirect('manager_department_hours')

            try:
                val = float(raw)
            except ValueError:
                messages.error(request, 'Некорректное число.')
                return redirect('manager_department_hours')

            if val <= 0:
                messages.error(request, 'Норма должна быть больше нуля.')
                return redirect('manager_department_hours')

            dept.hours_per_day = val
            dept.save(update_fields=['hours_per_day'])
            messages.success(request, f'«{dept.name}»: {val} ч/день.')
            return redirect('manager_department_hours')

        messages.error(request, 'Не указано подразделение.')
        return redirect('manager_department_hours')

    # ── GET ──
    departments = (
        Department.objects
        .annotate(users_total=Count('users', distinct=True))
        .order_by('name')
    )

    return render(request, 'manager/department_hours.html', {
        'departments': departments,
        'global_norm': norm_hours(),
    })
# ─────────────────────────────────────────────────────────────
#  Графики работы (WorkSchedule)
# ─────────────────────────────────────────────────────────────

def _pattern_summary(pattern):
    """Короткая человекочитаемая расшифровка паттерна.

    [1,1,1,1,1,0,0]  → «5/2 (пн–пт)»
    [1]*7 + [0]*7    → «7/7»
    [1,1,0,0]        → «2/2»
    [1,1,1,0,0,0]    → «3/3»
    [1,1,1,1,0,0,0,0] → «4/4»
    [1,1,0,0,1,1,0,0] → «2/2» (два цикла по 2)
    Иначе            → «N из M рабочих»
    """
    if not pattern:
        return '—'

    pattern = list(pattern)
    n = len(pattern)
    working = sum(1 for x in pattern if x)

    # Специальные «именные» графики
    if n == 7 and pattern == [1, 1, 1, 1, 1, 0, 0]:
        return '5/2 (пн–пт)'
    if n == 14 and pattern == [1] * 7 + [0] * 7:
        return '7/7'

    # Раскладываем на серии одинаковых значений
    runs = []
    cur_val = pattern[0]
    cur_len = 0
    for x in pattern:
        if x == cur_val:
            cur_len += 1
        else:
            runs.append((cur_val, cur_len))
            cur_val, cur_len = x, 1
    runs.append((cur_val, cur_len))

    # Паттерн «N/M»: все серии одинаковой длины N.
    # Примеры: [1,1,0,0] → 2/2; [1,1,1,0,0,0] → 3/3;
    # [1,1,0,0,1,1,0,0] → 2/2 (4 серии по 2).
    # Не подходит, если серии разной длины: [1,1,1,0,0] → 3 ≠ 2.
    lengths = {length for _, length in runs}
    if len(lengths) == 1:
        return f'{next(iter(lengths))}/{next(iter(lengths))}'

    return f'{working} из {n} рабочих'


def _schedule_usage(schedule):
    """Кто использует график: отделы (default_schedule) и люди (личный schedule)."""
    from accounts.models import Department

    departments = list(
        Department.objects
        .filter(default_schedule=schedule)
        .order_by('name')
    )
    personal_users = list(
        schedule.users.select_related('department').order_by('full_name')
    )

    # Сотрудники, попадающие через отдел (без личного графика)
    from django.contrib.auth import get_user_model
    User = get_user_model()

    dept_ids = [d.pk for d in departments]
    via_departments = list(
        User.objects
        .filter(
            department_id__in=dept_ids,
            schedule__isnull=True,
            is_active=True,
        )
        .select_related('department')
        .order_by('full_name')
    )

    return {
        'departments': departments,
        'personal_users': personal_users,
        'via_departments': via_departments,
        'people_count': len(personal_users) + len(via_departments),
    }


def _schedule_preview(schedule, days=14):
    """Список ближайших `days` дней с флагом «рабочий ли по этому графику».

    Учитывает use_calendar (праздники РФ переопределяют паттерн).
    """
    from tasks.utils import day_info

    today = timezone.localdate()
    items = []
    for i in range(days):
        d = today + timedelta(days=i)
        info = day_info(d, schedule=schedule)
        items.append({
            'date': d,
            'weekday': d.strftime('%a'),
            'day': d.day,
            'month': d.month,
            'is_working': info['is_working'],
            'name': info['name'],
            'source': info['source'],
            'is_today': i == 0,
        })
    return items


@login_required
def schedules_page(request):
    """Список графиков работы (WorkSchedule).

    Доступно начальнику завода и админам портала. Руководитель отдела
    сюда не ходит: он видит график своего отдела, но не управляет им.
    """
    from accounts.models import WorkSchedule

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedules = list(WorkSchedule.objects.order_by('-is_active', 'name'))

    rows = []
    for s in schedules:
        usage = _schedule_usage(s)
        rows.append({
            'schedule': s,
            'summary': _pattern_summary(s.pattern or []),
            'cycle_length': len(s.pattern or []),
            'departments_count': len(usage['departments']),
            'people_count': usage['people_count'],
            'preview_7': _schedule_preview(s, days=7),
        })

    return render(request, 'manager/schedules.html', {
        'rows': rows,
        'total': len(rows),
    })


@login_required
def schedule_detail(request, pk):
    """Детальная карточка графика: параметры, использование, превью."""
    from accounts.models import WorkSchedule

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedule = get_object_or_404(WorkSchedule, pk=pk)
    usage = _schedule_usage(schedule)

    # Списки для форм назначения: отделы и активные пользователи.
    from accounts.models import Department
    from django.contrib.auth import get_user_model
    User = get_user_model()

    all_departments = list(Department.objects.order_by('name'))
    all_users = list(
        User.objects
        .filter(is_active=True)
        .select_related('department')
        .order_by('full_name')
    )

    return render(request, 'manager/schedule_detail.html', {
        'schedule': schedule,
        'summary': _pattern_summary(schedule.pattern or []),
        'cycle_length': len(schedule.pattern or []),
        'departments': usage['departments'],
        'personal_users': usage['personal_users'],
        'via_departments': usage['via_departments'],
        'people_count': usage['people_count'],
        'preview_28': _schedule_preview(schedule, days=28),
        'all_departments': all_departments,
        'all_users': all_users,
    })


def _schedule_is_used(schedule):
    """Есть ли у графика хоть один пользователь (отдел или человек).

    Возвращает (departments_count, users_count, is_used).
    """
    from accounts.models import Department
    from django.contrib.auth import get_user_model
    User = get_user_model()

    deps = Department.objects.filter(default_schedule=schedule).count()
    users = User.objects.filter(schedule=schedule, is_active=True).count()
    return deps, users, (deps + users) > 0


@login_required
def schedule_edit(request, pk=None):
    """Создание (pk=None) или редактирование графика работы.

    Доступно начальнику завода и админам портала.
    """
    from accounts.models import WorkSchedule

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedule = get_object_or_404(WorkSchedule, pk=pk) if pk else None
    is_new = schedule is None

    if request.method == 'POST':
        form = WorkScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            obj = form.save()
            messages.success(
                request,
                ('График «%s» создан.' if is_new else 'График «%s» сохранён.')
                % obj.name,
                )
            return redirect('manager_schedule_detail', pk=obj.pk)
        # Если форма невалидна — упадём на рендер с ошибками.
    else:
        form = WorkScheduleForm(instance=schedule)

    # Пресеты паттернов для UI.
    pattern_presets = [
        {'label': '5/2 (пн–пт)', 'pattern': [1, 1, 1, 1, 1, 0, 0]},
        {'label': '2/2',         'pattern': [1, 1, 0, 0]},
        {'label': '3/3',         'pattern': [1, 1, 1, 0, 0, 0]},
        {'label': '4/4',         'pattern': [1, 1, 1, 1, 0, 0, 0, 0]},
        {'label': '7/7',         'pattern': [1] * 7 + [0] * 7},
    ]

    deps_count = users_count = 0
    if schedule:
        deps_count, users_count, _ = _schedule_is_used(schedule)

    return render(request, 'manager/schedule_form.html', {
        'form': form,
        'schedule': schedule,
        'is_new': is_new,
        'pattern_presets': pattern_presets,
        'departments_count': deps_count,
        'users_count': users_count,
    })


@login_required
def schedule_delete(request, pk):
    """Удаление графика. Только если он нигде не используется."""
    from accounts.models import WorkSchedule

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedule = get_object_or_404(WorkSchedule, pk=pk)

    if request.method != 'POST':
        return redirect('manager_schedule_detail', pk=pk)

    deps, users, is_used = _schedule_is_used(schedule)
    if is_used:
        parts = []
        if deps:
            parts.append(f'{deps} отдел(ов)')
        if users:
            parts.append(f'{users} сотрудник(ов)')
        messages.error(
            request,
            f'Нельзя удалить: график используется — {", ".join(parts)}. '
            f'Сначала переназначьте их на другой график или снимите привязку.',
        )
        return redirect('manager_schedule_detail', pk=pk)

    name = schedule.name
    schedule.delete()
    messages.success(request, f'График «{name}» удалён.')
    return redirect('manager_schedules')


@login_required
@require_POST
def schedule_assign(request, pk):
    """Привязать график к отделу или сотруднику.

    POST-параметры:
        target_type  — 'department' | 'user'
        target_id    — pk отдела или пользователя

    Ошибки, если pk/type не найден, отдают редирект с messages.error.
    Успех — messages.success и редирект обратно на detail графика.
    """
    from accounts.models import Department, WorkSchedule
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedule = get_object_or_404(WorkSchedule, pk=pk)
    back = reverse('manager_schedule_detail', args=[schedule.pk])

    target_type = (request.POST.get('target_type') or '').strip()
    target_id_raw = (request.POST.get('target_id') or '').strip()

    if target_type not in ('department', 'user'):
        messages.error(request, 'Некорректный тип цели.')
        return redirect(back)

    if not target_id_raw.isdigit():
        messages.error(request, 'Выберите, кому назначить.')
        return redirect(back)

    target_id = int(target_id_raw)

    if target_type == 'department':
        dept = Department.objects.filter(pk=target_id).first()
        if dept is None:
            messages.error(request, 'Подразделение не найдено.')
            return redirect(back)

        if dept.default_schedule_id == schedule.pk:
            messages.info(
                request,
                f'«{dept.name}» уже использует график «{schedule.name}».',
            )
            return redirect(back)

        old = dept.default_schedule
        dept.default_schedule = schedule
        dept.save(update_fields=['default_schedule'])

        if old:
            messages.success(
                request,
                f'График «{schedule.name}» назначен отделу «{dept.name}» '
                f'(был «{old.name}»).',
            )
        else:
            messages.success(
                request,
                f'График «{schedule.name}» назначен отделу «{dept.name}».',
            )
        return redirect(back)

    # target_type == 'user'
    target = User.objects.filter(pk=target_id).first()
    if target is None:
        messages.error(request, 'Пользователь не найден.')
        return redirect(back)

    if target.schedule_id == schedule.pk:
        messages.info(
            request,
            f'У «{target.full_name}» уже этот график.',
        )
        return redirect(back)

    old = target.schedule
    target.schedule = schedule
    target.save(update_fields=['schedule'])

    if old:
        messages.success(
            request,
            f'«{target.full_name}»: график «{schedule.name}» '
            f'(был «{old.name}»).',
        )
    else:
        messages.success(
            request,
            f'«{target.full_name}»: личный график «{schedule.name}».',
        )
    return redirect(back)


@login_required
@require_POST
def schedule_unassign(request, pk):
    """Снять график с отдела или сотрудника.

    POST-параметры:
        target_type  — 'department' | 'user'
        target_id    — pk отдела или пользователя

    Отдел без графика → использует встроенный 5/2 + праздники РФ.
    Сотрудник без личного графика → использует график отдела.
    """
    from accounts.models import Department, WorkSchedule
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    schedule = get_object_or_404(WorkSchedule, pk=pk)
    back = reverse('manager_schedule_detail', args=[schedule.pk])

    target_type = (request.POST.get('target_type') or '').strip()
    target_id_raw = (request.POST.get('target_id') or '').strip()

    if target_type not in ('department', 'user'):
        messages.error(request, 'Некорректный тип цели.')
        return redirect(back)

    if not target_id_raw.isdigit():
        messages.error(request, 'Некорректная цель.')
        return redirect(back)

    target_id = int(target_id_raw)

    if target_type == 'department':
        dept = Department.objects.filter(pk=target_id).first()
        if dept is None:
            messages.error(request, 'Подразделение не найдено.')
            return redirect(back)

        if dept.default_schedule_id != schedule.pk:
            messages.info(
                request,
                f'«{dept.name}» не использует этот график.',
            )
            return redirect(back)

        dept.default_schedule = None
        dept.save(update_fields=['default_schedule'])
        messages.success(
            request,
            f'Отдел «{dept.name}»: график снят, работает встроенный 5/2 + '
            f'праздники РФ.',
        )
        return redirect(back)

    target = User.objects.filter(pk=target_id).first()
    if target is None:
        messages.error(request, 'Пользователь не найден.')
        return redirect(back)

    if target.schedule_id != schedule.pk:
        messages.info(request, f'У «{target.full_name}» другой график.')
        return redirect(back)

    target.schedule = None
    target.save(update_fields=['schedule'])
    messages.success(
        request,
        f'«{target.full_name}»: личный график снят, работает по отделу.',
    )
    return redirect(back)


@login_required
def schedule_today(request):
    """Свод «Кто работает сегодня».

    Показывает всех активных сотрудников с указанием:
      - какой график применяется (личный / отдела / встроенный 5/2);
      - работает ли сегодня (учитывая use_calendar и праздники).

    Фильтры: ?dept=<pk>  — только один отдел; ?state=working|off — только
    работающих или только отдыхающих.
    """
    from accounts.models import Department
    from tasks.utils import day_info, schedule_for_user

    user = request.user
    if not (user.can_plant or user.is_admin_role):
        raise PermissionDenied('Раздел доступен начальнику завода.')

    today = timezone.localdate()
    dept_filter = (request.GET.get('dept') or '').strip()
    state_filter = (request.GET.get('state') or '').strip()

    dept_id = int(dept_filter) if dept_filter.isdigit() else None
    if state_filter not in ('working', 'off', 'vacation'):
        state_filter = ''

    # Кандидаты — активные сотрудники, не уволенные / не в архиве.
    qs = (
        User.objects
        .filter(is_active=True)
        .exclude(employment_status__in=[
            User.EmploymentStatus.DISMISSED,
            User.EmploymentStatus.ARCHIVED,
        ])
        .select_related('department', 'role', 'schedule')
        .order_by('department__name', 'full_name')
    )
    if dept_id:
        qs = qs.filter(department_id=dept_id)

    # Собираем строки. schedule_for_user делает обращение к department,
    # поэтому select_related('department') обязателен, иначе N+1.
    rows = []
    for u in qs:
        sch = schedule_for_user(u)
        info = day_info(today, schedule=sch)

        if sch is None:
            source = 'builtin'
            source_label = 'Встроенный 5/2'
        elif u.schedule_id == sch.pk:
            source = 'personal'
            source_label = f'Личный: {sch.name}'
        else:
            source = 'department'
            source_label = f'Отдел: {sch.name}'

        # Отпуск перебивает график: сотрудник не работает сегодня,
        # даже если по паттерну день рабочий.
        on_vacation = u.is_on_vacation

        rows.append({
            'user': u,
            'is_working': info['is_working'] and not on_vacation,
            'raw_working': info['is_working'],
            'on_vacation': on_vacation,
            'vacation_label': u.vacation_label,
            'day_name': info['name'],
            'day_source': info['source'],
            'schedule': sch,
            'schedule_source': source,
            'schedule_label': source_label,
        })

    # Счётчики до фильтра — по всему срезу (или по выбранному отделу).
    total = len(rows)
    on_vacation_count = sum(1 for r in rows if r['on_vacation'])
    working = sum(1 for r in rows if r['is_working'])
    off = total - working - on_vacation_count
    by_personal = sum(1 for r in rows if r['schedule_source'] == 'personal')
    by_department = sum(1 for r in rows if r['schedule_source'] == 'department')
    by_builtin = sum(1 for r in rows if r['schedule_source'] == 'builtin')

    # Применяем фильтр по статусу
    if state_filter == 'working':
        rows = [r for r in rows if r['is_working']]
    elif state_filter == 'off':
        rows = [r for r in rows if not r['is_working'] and not r['on_vacation']]
    elif state_filter == 'vacation':
        rows = [r for r in rows if r['on_vacation']]

    departments = Department.objects.order_by('name')

    return render(request, 'manager/schedule_today.html', {
        'today': today,
        'rows': rows,
        'departments': departments,
        'dept_filter': dept_id,
        'state_filter': state_filter,
        'total': total,
        'working': working,
        'off': off,
        'on_vacation_count': on_vacation_count,
        'by_personal': by_personal,
        'by_department': by_department,
        'by_builtin': by_builtin,
    })
# ─────────────────────────────────────────────────────────────
#  Отпуска / статусы занятости (F)
# ─────────────────────────────────────────────────────────────

def _person_scope_check(user, person):
    """Право руководителя на карточку сотрудника.

    plant/admin → любой; boss → только свой отдел.
    Возвращает True, если доступ разрешён.
    """
    if user.can_plant or user.is_admin_role:
        return True
    if not user.is_boss:
        return False
    if not user.department_id:
        return False
    return person.department_id == user.department_id


@login_required
@require_POST
def person_vacation_set(request, pk):
    """Поставить сотруднику отпуск / больничный с датами.

    POST:
        date_from — ISO-дата, обязательно
        date_to   — ISO-дата, обязательно

    Ставит employment_status=VACATION. Снимается отдельной кнопкой
    (person_vacation_clear).
    """
    from datetime import datetime
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user = request.user
    person = get_object_or_404(User, pk=pk)

    if not _person_scope_check(user, person):
        raise PermissionDenied('Нет доступа к этому сотруднику.')

    date_from_raw = (request.POST.get('date_from') or '').strip()
    date_to_raw = (request.POST.get('date_to') or '').strip()

    try:
        date_from = datetime.strptime(date_from_raw, '%Y-%m-%d').date()
        date_to = datetime.strptime(date_to_raw, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        messages.error(request, 'Некорректные даты.')
        return redirect('manager_person', pk=person.pk)

    if date_to < date_from:
        messages.error(
            request,
            'Дата окончания раньше даты начала — проверьте.',
        )
        return redirect('manager_person', pk=person.pk)

    person.vacation_from = date_from
    person.vacation_to = date_to
    person.employment_status = person.EmploymentStatus.VACATION
    person.save(update_fields=[
        'vacation_from', 'vacation_to', 'employment_status',
    ])

    messages.success(
        request,
        f'«{person.full_name}»: отпуск с {date_from:%d.%m.%Y} '
        f'по {date_to:%d.%m.%Y}.',
    )
    return redirect('manager_person', pk=person.pk)


@login_required
@require_POST
def person_vacation_clear(request, pk):
    """Снять отпуск с сотрудника.

    Обнуляет vacation_from, vacation_to. Если employment_status
    был VACATION — возвращает в ACTIVE.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user = request.user
    person = get_object_or_404(User, pk=pk)

    if not _person_scope_check(user, person):
        raise PermissionDenied('Нет доступа к этому сотруднику.')

    was_vacation = person.employment_status == person.EmploymentStatus.VACATION

    person.vacation_from = None
    person.vacation_to = None
    if was_vacation:
        person.employment_status = person.EmploymentStatus.ACTIVE

    person.save(update_fields=[
        'vacation_from', 'vacation_to', 'employment_status',
    ])

    messages.success(request, f'«{person.full_name}»: отпуск снят.')
    return redirect('manager_person', pk=person.pk)
