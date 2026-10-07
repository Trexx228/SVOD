"""Личный кабинет: обзор, задачи, часы, KPI, действия, профиль, настройки.

Одна страница /me/ с табами через ?tab=. Старые URL'ы (/me/tasks/,
/me/kpi/, /me/settings/ и т.д.) — редиректы с нужным табом, чтобы
не ломать существующие ссылки.
"""
import csv
from datetime import datetime as _dt, time as _time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from gamify.models import Kudos, UserAchievement
from gamify.services import LEVELS, ensure_catalog, level_info
from tasks.models import (
    OvertimeRecord, ShopSession, Task, TaskLog, TimeSession,
)
from tasks.utils import kpi_percent, norm_hours

# ── Табы личного кабинета ──

ME_TABS = (
    ('overview',   'Обзор'),
    ('tasks',      'Мои задачи'),
    ('sessions',   'Мои часы'),
    ('kpi',        'Показатели и KPI'),
    ('motivation', 'Мотивация'),
    ('logs',       'Мои действия'),
    ('profile',    'Профиль'),
    ('settings',   'Настройки'),
)

ME_TABS_CODES = {code for code, _ in ME_TABS}


# ── Общие утилиты ──

def _parse_date(s):
    try:
        return _dt.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _aware_start(d):
    return timezone.make_aware(
        _dt.combine(d, _time.min), timezone.get_current_timezone(),
    )


def _aware_end(d):
    return timezone.make_aware(
        _dt.combine(d, _time.max), timezone.get_current_timezone(),
    )


def _default_period():
    """Текущий месяц."""
    today = timezone.localdate()
    return today.replace(day=1), today


# ═════════════════════════════════════════════════════════════
#  Таб: ОБЗОР
# ═════════════════════════════════════════════════════════════

def _tab_overview(request):
    user = request.user
    today = timezone.localdate()
    month_start = timezone.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )

    my_tasks_qs = Task.objects.filter(executor=user)

    task_stats = {
        'total': my_tasks_qs.count(),
        'in_progress': my_tasks_qs.filter(
            status=Task.Status.IN_PROGRESS,
        ).count(),
        'review': my_tasks_qs.filter(status=Task.Status.REVIEW).count(),
        'rework': my_tasks_qs.filter(status=Task.Status.REWORK).count(),
        'done': my_tasks_qs.filter(status=Task.Status.DONE).count(),
        'done_month': my_tasks_qs.filter(
            status=Task.Status.DONE, finished_at__gte=month_start,
        ).count(),
        'overdue': my_tasks_qs.filter(
            due__lt=today,
        ).exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED],
        ).count(),
    }

    work_hours_month = (
            TimeSession.objects
            .filter(executor=user, finished_at__gte=month_start)
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )
    shop_hours_month = (
            ShopSession.objects
            .filter(
                executor=user,
                started_at__gte=month_start,
                finished_at__isnull=False,
            )
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )

    active_task = my_tasks_qs.filter(
        session_started_at__isnull=False,
    ).first()
    active_shop = ShopSession.objects.filter(
        executor=user, finished_at__isnull=True,
    ).first()

    last_work = list(
        TimeSession.objects
        .select_related('task')
        .filter(executor=user)
        .order_by('-finished_at')[:5]
    )

    last_tasks = list(
        my_tasks_qs
        .select_related('requester', 'order')
        .order_by('-created_at')[:8]
    )

    today_tasks = list(
        my_tasks_qs
        .filter(due=today)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by('priority')
    )

    overtime_qs = (
        OvertimeRecord.objects
        .filter(user=user, date__gte=month_start.date())
        .select_related('task', 'created_by')
        .order_by('-date', '-created_at')
    )
    overtime_hours_month = round(
        sum(r.hours or 0 for r in overtime_qs), 1
    )
    overtime_count_month = overtime_qs.count()
    overtime_recent = list(overtime_qs[:5])
    overtime_task_ids = set(
        overtime_qs.exclude(task__isnull=True)
        .values_list('task_id', flat=True)
    )

    return {
        'task_stats': task_stats,
        'work_hours_month': round(work_hours_month, 2),
        'shop_hours_month': round(shop_hours_month, 2),
        'active_task': active_task,
        'active_shop': active_shop,
        'last_work': last_work,
        'last_tasks': last_tasks,
        'today_tasks': today_tasks,
        'today': today,
        'overtime_hours_month': overtime_hours_month,
        'overtime_count_month': overtime_count_month,
        'overtime_recent': overtime_recent,
        'overtime_task_ids': overtime_task_ids,
    }


# ═════════════════════════════════════════════════════════════
#  Таб: МОИ ЗАДАЧИ
# ═════════════════════════════════════════════════════════════

def _tab_tasks(request):
    user = request.user

    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    priority = request.GET.get('priority', '').strip()
    due_from = request.GET.get('due_from', '').strip()
    due_to = request.GET.get('due_to', '').strip()

    qs = (
        Task.objects
        .select_related('requester', 'order', 'branch', 'task_type')
        .filter(executor=user)
        .order_by('-created_at')
    )

    if q:
        from django.db.models import Q as _Q
        qs = qs.filter(_Q(title__icontains=q) | _Q(body__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    df = _parse_date(due_from)
    dt_ = _parse_date(due_to)
    if df:
        qs = qs.filter(due__gte=df)
    if dt_:
        qs = qs.filter(due__lte=dt_)

    total = qs.count()
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    params.pop('tab', None)
    page_qs = params.urlencode()

    page_task_ids = [t.pk for t in page.object_list]
    overtime_task_ids = set(
        OvertimeRecord.objects
        .filter(user=user, task_id__in=page_task_ids)
        .values_list('task_id', flat=True)
        .distinct()
    )

    return {
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'q': q,
        'status': status,
        'priority': priority,
        'due_from': due_from,
        'due_to': due_to,
        'status_choices': Task.Status.choices,
        'priority_choices': Task.Priority.choices,
        'today': timezone.localdate(),
        'overtime_task_ids': overtime_task_ids,
    }


# ═════════════════════════════════════════════════════════════
#  Таб: МОИ ЧАСЫ
# ═════════════════════════════════════════════════════════════

def _tab_sessions(request):
    user = request.user

    tab = request.GET.get('subtab', 'work').strip()
    if tab not in ('work', 'shop'):
        tab = 'work'

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    def_from, def_to = _default_period()
    df = _parse_date(date_from) or def_from
    dt_ = _parse_date(date_to) or def_to

    if tab == 'work':
        qs = (
            TimeSession.objects
            .select_related('task')
            .filter(executor=user)
            .order_by('-finished_at')
        )
        if df:
            qs = qs.filter(finished_at__gte=_aware_start(df))
        if dt_:
            qs = qs.filter(finished_at__lte=_aware_end(dt_))
    else:
        qs = (
            ShopSession.objects
            .select_related('task')
            .filter(executor=user)
            .order_by('-started_at')
        )
        if df:
            qs = qs.filter(started_at__gte=_aware_start(df))
        if dt_:
            qs = qs.filter(started_at__lte=_aware_end(dt_))

    total = qs.count()
    total_hours = sum(s.duration_hours or 0 for s in qs[:5000])
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    params.pop('tab', None)
    params.pop('subtab', None)
    page_qs = params.urlencode()

    return {
        'page': page,
        'total': total,
        'total_hours': round(total_hours, 2),
        'page_qs': page_qs,
        'subtab': tab,
        'date_from': df.isoformat() if df else '',
        'date_to': dt_.isoformat() if dt_ else '',
    }


# ═════════════════════════════════════════════════════════════
#  Таб: KPI
# ═════════════════════════════════════════════════════════════

def _kpi_data(user, df, dt_):
    """Считает summary и daily_rows для сотрудника за период.

    Используется и в HTML-табе /me/?tab=kpi, и в CSV-экспорте
    /me/kpi/export.csv. Один источник правды.
    """
    start_dt = _aware_start(df)
    end_dt = _aware_end(dt_)

    done_qs = list(
        Task.objects
        .select_related('order')
        .filter(
            executor=user,
            status=Task.Status.DONE,
            finished_at__gte=start_dt,
            finished_at__lte=end_dt,
        )
    )

    tasks_done = len(done_qs)
    plan_hours = sum(t.plan_hours or 0 for t in done_qs)
    fact_hours = sum(t.accumulated_hours or 0 for t in done_qs)
    on_time = sum(
        1 for t in done_qs
        if t.due and t.finished_at and t.finished_at.date() <= t.due
    )
    overdue = sum(
        1 for t in done_qs
        if t.due and t.finished_at and t.finished_at.date() > t.due
    )

    on_time_pct = int(round(100 * on_time / tasks_done)) if tasks_done else 0
    efficiency = int(round(100 * plan_hours / fact_hours)) if fact_hours > 0 else 0
    delta = round(fact_hours - plan_hours, 2)

    session_hours = (
            TimeSession.objects
            .filter(
                executor=user,
                finished_at__gte=start_dt,
                finished_at__lte=end_dt,
            )
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )

    summary = {
        'tasks_done': tasks_done,
        'plan_hours': round(plan_hours, 2),
        'fact_hours': round(fact_hours, 2),
        'delta': delta,
        'on_time': on_time,
        'overdue': overdue,
        'on_time_pct': on_time_pct,
        'efficiency': efficiency,
        'session_hours': round(session_hours, 2),
    }

    daily = {}
    for t in done_qs:
        if not t.finished_at:
            continue
        d = t.finished_at.date()
        daily.setdefault(d, {'plan': 0.0, 'fact': 0.0, 'count': 0})
        daily[d]['plan'] += t.plan_hours or 0
        daily[d]['fact'] += t.accumulated_hours or 0
        daily[d]['count'] += 1

    daily_rows = sorted(
        [
            {
                'date': d,
                'plan': round(v['plan'], 2),
                'fact': round(v['fact'], 2),
                'count': v['count'],
            }
            for d, v in daily.items()
        ],
        key=lambda x: x['date'],
    )

    return summary, daily_rows


def _tab_kpi(request):
    user = request.user

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    def_from, def_to = _default_period()
    df = _parse_date(date_from) or def_from
    dt_ = _parse_date(date_to) or def_to

    summary, daily_rows = _kpi_data(user, df, dt_)

    return {
        'summary': summary,
        'daily_rows': daily_rows,
        'date_from': df.isoformat(),
        'date_to': dt_.isoformat(),
    }


def _tab_motivation(request):
    """Мотивация + рейтинг: уровень, ритм, достижения, кудосы, топ-20.

    Раньше жило в gamify.views.motivation и gamify.views.rating.
    Теперь — вкладка кабинета. Логика не изменилась, только источник
    данных один — request.user.
    """
    user = request.user
    now = timezone.now()
    today = timezone.localdate()

    ensure_catalog()

    # ── Уровень ──
    level = level_info(user)
    hours = level['hours']

    levels = []
    for h, name in LEVELS:
        levels.append({
            'name': name,
            'hours': h,
            'reached': hours >= h,
            'current': name == level['name'],
        })

    # ── KPI за месяц ──
    from gamify.services import _month_bounds_for_user
    month_from, month_until = _month_bounds_for_user(user)

    closed30 = list(
        Task.objects.filter(
            executor=user,
            status=Task.Status.DONE,
            finished_at__gte=month_from,
            finished_at__lte=month_until,
        ).order_by('-finished_at')
    )

    kpi_plan = sum(t.plan_hours or 0 for t in closed30)
    kpi_fact = sum(t.accumulated_hours or 0 for t in closed30)
    kpi = kpi_percent(kpi_plan, kpi_fact)

    streak = getattr(user, 'streak', None)

    # ── Календарь 14 дней: где были задачи в срок ──
    first_day = today - timedelta(days=13)
    recent = Task.objects.filter(
        executor=user,
        status=Task.Status.DONE,
        finished_at__date__gte=first_day,
    ).values_list('finished_at', 'due')

    ok_dates = set()
    for finished_at, due in recent:
        if finished_at and due is not None:
            finished_date = timezone.localtime(finished_at).date()
            if due >= finished_date:
                ok_dates.add(finished_date)

    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        days.append({
            'label': d.strftime('%d.%m'),
            'ok': d in ok_dates,
        })

    # ── Достижения ──
    earned = {
        ua.achievement_id: ua.awarded_at
        for ua in UserAchievement.objects.filter(user=user).select_related('achievement')
    }

    from gamify.models import Achievement
    achievements = [
        {'ach': a, 'awarded': earned.get(a.pk)}
        for a in Achievement.objects.all().order_by('title')
    ]

    # ── Кудосы ──
    kudos_in = (
        Kudos.objects.filter(to_user=user)
        .select_related('from_user')
        .order_by('-created_at')
    )
    kudos_out = (
        Kudos.objects.filter(from_user=user)
        .select_related('to_user')
        .order_by('-created_at')
    )

    # ── Зеркало: OTD, точность, переработка ──
    norm = norm_hours()

    with_due = [t for t in closed30 if t.due]
    otd = (
        round(
            sum(
                1
                for t in with_due
                if timezone.localtime(t.finished_at).date() <= t.due
            )
            / len(with_due)
            * 100
        )
        if with_due
        else None
    )

    rework_n = TaskLog.objects.filter(
        kind=TaskLog.Kind.REWORK,
        task__executor=user,
        created_at__gte=now - timedelta(days=30),
    ).count()

    measured = [t for t in closed30 if t.plan_hours and t.accumulated_hours]
    accurate = [
        t
        for t in measured
        if abs(t.accumulated_hours - t.plan_hours) / t.plan_hours <= 0.2
    ]
    accuracy = round(len(accurate) / len(measured) * 100) if measured else None

    # ── Динамика 8 недель ──
    weeks_start = today - timedelta(days=today.weekday() + 49)
    hours_by_date = {
        row['day']: row['hours'] or 0
        for row in Task.objects.filter(
            executor=user,
            status=Task.Status.DONE,
            finished_at__date__gte=weeks_start,
        )
        .annotate(day=TruncDate('finished_at'))
        .values('day')
        .annotate(hours=Sum('accumulated_hours'))
    }

    weeks = []
    for i in range(7, -1, -1):
        w_start = today - timedelta(days=today.weekday() + 7 * i)
        h = sum(
            hours_by_date.get(w_start + timedelta(days=offset), 0)
            for offset in range(7)
        )
        weeks.append({
            'label': w_start.strftime('%d.%m'),
            'hours': round(h, 1),
        })

    max_h = max([w['hours'] for w in weeks] + [0.1])
    for w in weeks:
        w['pct'] = int(w['hours'] / max_h * 100)

    week_start = today - timedelta(days=today.weekday())
    bd_week = sum(
        1
        for i in range((today - week_start).days + 1)
        if (week_start + timedelta(days=i)).weekday() < 5
    )

    h_week = (
            TimeSession.objects.filter(executor=user, finished_at__date__gte=week_start)
            .aggregate(s=Sum('duration_hours'))['s'] or 0
    )

    over_week = h_week > norm * max(bd_week, 1) * 1.1

    # ── Точность по видам работ ──
    kind_acc = []
    for k in [c[0] for c in Task.Kind.choices]:
        ms = [
            t
            for t in closed30
            if t.kind == k and t.plan_hours and t.accumulated_hours
        ]

        if ms:
            good = sum(
                1
                for t in ms
                if abs(t.accumulated_hours - t.plan_hours) / t.plan_hours <= 0.2
            )
            kind_acc.append({
                'label': Task.Kind(k).label,
                'pct': round(good / len(ms) * 100),
                'n': len(ms),
            })

    worst = min(kind_acc, key=lambda r: r['pct']) if kind_acc else None

    next_task = (
        Task.objects.filter(
            executor=user,
            status__in=[Task.Status.NEW, Task.Status.PAUSED, Task.Status.REWORK],
        )
        .order_by('due', '-priority')
        .first()
    )

    # ── Рейтинг сотрудников за месяц (бывш. /rating/) ──
    from datetime import datetime as _dtt, time as _dtime
    month_start_for_rating = today.replace(day=1)
    since_rating = timezone.make_aware(
        _dtt.combine(month_start_for_rating, _dtime.min),
        timezone.get_current_timezone(),
    )

    rating_rows = list(
        Task.objects.filter(status=Task.Status.DONE, finished_at__gte=since_rating)
        .values(
            'executor__pk',
            'executor__full_name',
            'executor__role__name',
            'executor__department__name',
        )
        .annotate(
            closed=Count('id'),
            plan=Sum('plan_hours'),
            fact=Sum('accumulated_hours'),
        )
        .order_by('-closed', '-plan')[:20]
    )

    medals = {0: '🥇', 1: '🥈', 2: '🥉'}
    for i, r in enumerate(rating_rows):
        r['place'] = medals.get(i, f'{i + 1}.')
        r['kpi'] = kpi_percent(r['plan'] or 0, r['fact'] or 0)
        r['position'] = r.get('executor__role__name') or '—'
        r['department'] = r.get('executor__department__name') or '—'

    return {
        'level': level,
        'levels': levels,
        'kpi': kpi,
        'closed30': closed30,
        'kpi_plan': kpi_plan,
        'kpi_fact': kpi_fact,
        'streak': streak,
        'days': days,
        'kind_acc': kind_acc,
        'worst': worst,
        'next_task': next_task,
        'achievements': achievements,
        'kudos_in': kudos_in,
        'kudos_out': kudos_out,
        'otd': otd,
        'rework_n': rework_n,
        'accuracy': accuracy,
        'weeks': weeks,
        'h_week': h_week,
        'bd_week': bd_week,
        'norm': norm,
        'over_week': over_week,
        'rating_rows': rating_rows,
    }


@login_required
def me_kpi_export(request):
    """CSV-выгрузка личного KPI за период. UTF-8 с BOM, разделитель ';'.

    Период — из ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD.
    Без параметров — текущий месяц.
    """
    user = request.user

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    def_from, def_to = _default_period()
    df = _parse_date(date_from) or def_from
    dt_ = _parse_date(date_to) or def_to

    summary, _ = _kpi_data(user, df, dt_)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fname = f'me_kpi_{df.isoformat()}_{dt_.isoformat()}.csv'
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    response.write('\ufeff')

    w = csv.writer(response, delimiter=';')
    w.writerow(['Сотрудник', user.full_name])
    w.writerow([
        'Период',
        f'{df.strftime("%d.%m.%Y")} — {dt_.strftime("%d.%m.%Y")}',
    ])
    w.writerow([])
    w.writerow(['Показатель', 'Значение'])
    w.writerow(['Задач готово', summary['tasks_done']])
    w.writerow(['В срок', summary['on_time']])
    w.writerow(['Просрочено', summary['overdue']])
    w.writerow(['В срок, %', summary['on_time_pct']])
    w.writerow(['План, ч', f'{summary["plan_hours"]:.2f}'.replace('.', ',')])
    w.writerow(['Факт, ч', f'{summary["fact_hours"]:.2f}'.replace('.', ',')])
    w.writerow(['Δ, ч', f'{summary["delta"]:.2f}'.replace('.', ',')])
    w.writerow(['КПД, %', summary['efficiency']])
    w.writerow(['Сессий, ч', f'{summary["session_hours"]:.2f}'.replace('.', ',')])

    return response

# ═════════════════════════════════════════════════════════════
#  Таб: МОИ ДЕЙСТВИЯ
# ═════════════════════════════════════════════════════════════

def _tab_logs(request):
    user = request.user

    kind = request.GET.get('kind', '').strip()

    qs = (
        TaskLog.objects
        .select_related('task', 'source_task')
        .filter(author=user)
        .order_by('-created_at')
    )

    if kind:
        qs = qs.filter(kind=kind)

    total = qs.count()

    kind_stats = (
        qs.values('kind')
        .annotate(c=__import__('django.db.models', fromlist=['Count']).Count('id'))
        .order_by('-c')
    )
    kind_labels = dict(TaskLog.Kind.choices)
    kind_stats = [
        {
            'code': row['kind'],
            'label': kind_labels.get(row['kind'], row['kind']),
            'count': row['c'],
        }
        for row in kind_stats
    ]

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))

    params = request.GET.copy()
    params.pop('page', None)
    params.pop('tab', None)
    page_qs = params.urlencode()

    return {
        'page': page,
        'total': total,
        'page_qs': page_qs,
        'kind': kind,
        'kind_choices': TaskLog.Kind.choices,
        'kind_stats': kind_stats,
    }


# ═════════════════════════════════════════════════════════════
#  Таб: ПРОФИЛЬ
# ═════════════════════════════════════════════════════════════

def _tab_profile(request):
    user = request.user

    from admin_panel.models import LoginEvent

    month_start = timezone.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    my_tasks_qs = Task.objects.filter(executor=user)

    work_hours_month = (
            TimeSession.objects
            .filter(executor=user, finished_at__gte=month_start)
            .aggregate(s=Sum('duration_hours'))['s'] or 0.0
    )

    overtime_all_qs = (
        OvertimeRecord.objects
        .filter(user=user)
        .select_related('task', 'created_by', 'department')
        .order_by('-date', '-created_at')
    )
    overtime_hours_total = round(
        sum(r.hours or 0 for r in overtime_all_qs), 1,
    )
    overtime_count_total = overtime_all_qs.count()

    overtime_month_qs = overtime_all_qs.filter(
        date__gte=month_start.date(),
    )
    overtime_hours_month = round(
        sum(r.hours or 0 for r in overtime_month_qs), 1,
    )
    overtime_count_month = overtime_month_qs.count()

    paginator = Paginator(overtime_all_qs, 30)
    overtime_page = paginator.get_page(request.GET.get('overtime_page'))

    stats = {
        'tasks_total': my_tasks_qs.count(),
        'tasks_done_month': my_tasks_qs.filter(
            status=Task.Status.DONE, finished_at__gte=month_start,
        ).count(),
        'work_hours_month': round(work_hours_month, 1),
        'overtime_hours_total': overtime_hours_total,
        'overtime_count_total': overtime_count_total,
        'overtime_hours_month': overtime_hours_month,
        'overtime_count_month': overtime_count_month,
    }

    my_events = LoginEvent.objects.filter(
        Q(user=user) | Q(email_attempted__iexact=user.email)
    )

    logins = list(my_events.order_by('-created_at')[:10])
    logins_ok = my_events.filter(success=True).count()
    logins_failed = my_events.filter(success=False).count()

    return {
        'profile_user': user,
        'stats': stats,
        'overtime_page': overtime_page,
        'logins': logins,
        'logins_ok': logins_ok,
        'logins_failed': logins_failed,
    }


# ═════════════════════════════════════════════════════════════
#  Таб: НАСТРОЙКИ
# ═════════════════════════════════════════════════════════════

THEMES = {'dark', 'light', 'graphite', 'ocean', 'forest', 'sand', 'contrast'}
FONTS = {'s', 'm', 'l'}
BG_STYLES = {'grad', 'solid', 'mesh', 'grid', 'image'}


def _handle_settings_post(request):
    """POST-обработка таба «Настройки». Возвращает Response или None."""
    user = request.user

    if request.method != 'POST':
        return None

    changed = []
    old_bg_image = None

    theme = (request.POST.get('theme') or '').strip()
    if theme in THEMES and theme != user.theme:
        user.theme = theme
        changed.append('theme')

    font_size = (request.POST.get('font_size') or '').strip()
    if font_size in FONTS and font_size != user.font_size:
        user.font_size = font_size
        changed.append('font_size')

    sound_on = request.POST.get('sound_on') == '1'
    if sound_on != user.sound_on:
        user.sound_on = sound_on
        changed.append('sound_on')

    email_notifications = request.POST.get('email_notifications') == '1'
    if email_notifications != user.email_notifications:
        user.email_notifications = email_notifications
        changed.append('email_notifications')

    email_digest_daily = request.POST.get('email_digest_daily') == '1'
    if email_digest_daily != user.email_digest_daily:
        user.email_digest_daily = email_digest_daily
        changed.append('email_digest_daily')

    bg_style = (request.POST.get('bg_style') or '').strip()
    if bg_style == 'custom':
        bg_style = 'solid'   # обратная совместимость
    if bg_style in BG_STYLES and bg_style != user.bg_style:
        user.bg_style = bg_style
        changed.append('bg_style')

    use_bg_color = request.POST.get('use_bg_color') == '1'
    bg_color = (request.POST.get('bg_color') or '').strip()

    if not use_bg_color:
        bg_color = ''

    if len(bg_color) <= 7 and bg_color != user.bg_color:
        user.bg_color = bg_color
        changed.append('bg_color')

    bg_file = request.FILES.get('bg_image')
    if bg_file:
        if bg_file.size > 5 * 1024 * 1024:
            messages.error(
                request,
                f'Картинка фона — не больше 5 МБ '
                f'(ваш файл {bg_file.size // 1024} КБ).',
            )
            return redirect('/me/?tab=settings')

        ct = (bg_file.content_type or '').lower()
        name_lower = (bg_file.name or '').lower()
        ext_ok = name_lower.endswith(
            ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'),
        )
        ct_ok = ct.startswith('image/') or ct in (
            '', 'application/octet-stream',
        )

        if not (ext_ok or ct_ok):
            messages.error(
                request,
                f'Фон должен быть картинкой. '
                f'Ваш файл: {bg_file.name} ({ct or "тип не определён"}).',
            )
            return redirect('/me/?tab=settings')

        if user.bg_image:
            old_bg_image = user.bg_image.name

        user.bg_image = bg_file
        user.bg_style = 'image'
        user.bg_color = ''

        for field in ('bg_image', 'bg_style', 'bg_color'):
            if field not in changed:
                changed.append(field)

        messages.success(request, f'Картинка фона обновлена: {bg_file.name}')

    if request.POST.get('bg_image_clear') == '1' and user.bg_image:
        old_bg_image = user.bg_image.name
        user.bg_image = None
        if user.bg_style == 'image':
            user.bg_style = 'grad'
            if 'bg_style' not in changed:
                changed.append('bg_style')
        if 'bg_image' not in changed:
            changed.append('bg_image')

    if changed:
        user.save(update_fields=changed)

        if old_bg_image and old_bg_image != (
                user.bg_image.name if user.bg_image else None,
        ):
            try:
                from django.core.files.storage import default_storage
                default_storage.delete(old_bg_image)
            except Exception:
                pass

        if 'bg_image' not in changed:
            messages.success(request, 'Настройки сохранены.')
    else:
        messages.info(request, 'Изменений нет.')

    return redirect('/me/?tab=settings')


def _tab_settings(request):
    user = request.user

    # Если ссылка на картинку есть, а файла нет — обнулить
    if user.bg_image:
        try:
            if not user.bg_image.storage.exists(user.bg_image.name):
                user.bg_image = None
                if user.bg_style == 'image':
                    user.bg_style = 'grad'
                user.save(update_fields=['bg_image', 'bg_style'])
        except Exception:
            pass

    return {
        'theme_choices': [
            ('dark', 'Тёмная'),
            ('light', 'Светлая'),
            ('graphite', 'Графит'),
            ('ocean', 'Океан'),
            ('forest', 'Тайга'),
            ('sand', 'Песок'),
            ('contrast', 'Контрастная'),
        ],
        'font_choices': [
            ('s', 'Компактный'),
            ('m', 'Обычный'),
            ('l', 'Крупный'),
        ],
        'bg_style_choices': [
            ('grad', 'Градиент темы'),
            ('solid', 'Однотонный'),
            ('mesh', 'Мягкие пятна'),
            ('grid', 'Сетка'),
            ('custom', 'Свой цвет'),
            ('image', 'Своя картинка'),
        ],
    }


# ═════════════════════════════════════════════════════════════
#  Главный view /me/
# ═════════════════════════════════════════════════════════════

TAB_HANDLERS = {
    'overview':   _tab_overview,
    'tasks':      _tab_tasks,
    'sessions':   _tab_sessions,
    'kpi':        _tab_kpi,
    'motivation': _tab_motivation,
    'logs':       _tab_logs,
    'profile':    _tab_profile,
    'settings':   _tab_settings,
}


@login_required
def me_home(request):
    tab = (request.GET.get('tab') or 'overview').strip()
    if tab not in ME_TABS_CODES:
        tab = 'overview'

    # POST-обработка для настроек
    if tab == 'settings' and request.method == 'POST':
        resp = _handle_settings_post(request)
        if resp is not None:
            return resp

    data = {
        'tab': tab,
        'me_tabs': ME_TABS,
    }
    data.update(TAB_HANDLERS[tab](request))

    return render(request, 'cabinet/me.html', data)


# ═════════════════════════════════════════════════════════════
#  Алиасы-редиректы для старых URL'ов
# ═════════════════════════════════════════════════════════════

def _make_me_redirect(tab_code):
    """Возвращает view-функцию, редиректящую на /me/?tab=<tab_code>."""
    def _view(request):
        return redirect(f'/me/?tab={tab_code}')
    return _view


me_tasks    = login_required(_make_me_redirect('tasks'))
me_sessions = login_required(_make_me_redirect('sessions'))
me_kpi      = login_required(_make_me_redirect('kpi'))
me_logs     = login_required(_make_me_redirect('logs'))
me_profile  = login_required(_make_me_redirect('profile'))
me_settings = login_required(_make_me_redirect('settings'))
