from datetime import datetime, time as dtime, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Sum, Value, When
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from gamify import services as gamify
from gamify.models import Kudos, UserAchievement
from tasks.models import Task, TimeSession, WeekCommit, TaskBranch
from tasks.utils import UNIT_LABELS, format_hours, norm_hours, valid_unit


RING_C = 339.292

PRIO = Case(
    When(priority='urgent', then=Value(-1)),
    When(priority='high', then=Value(0)),
    When(priority='medium', then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)


def _safe_redirect_url(request, fallback='/'):
    url = request.META.get('HTTP_REFERER')
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
        return url
    return fallback


@login_required
def dashboard(request):
    user = request.user
    unit = valid_unit(request.GET.get('unit'))
    norm = norm_hours()
    now = timezone.now()

    open_tasks = list(
        Task.objects.filter(executor=user, blocked_by_stage=False)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .annotate(prio=PRIO)
        .select_related('parent')
        .order_by('prio', 'due')
    )

    for t in open_tasks:
        t.spent_text = format_hours(t.spent_hours, unit, norm)

    focus = next(
        (t for t in open_tasks if t.status == Task.Status.IN_PROGRESS),
        None,
    ) or (open_tasks[0] if open_tasks else None)

    cards = [t for t in open_tasks if t != focus]

    my_review = list(
        Task.objects.filter(requester=user, status=Task.Status.REVIEW)
        .select_related('executor')
        .order_by('-finished_at')
    )

    for t in my_review:
        t.spent_text = format_hours(t.spent_hours, unit, norm)

    closed_recent = list(
        Task.objects.filter(
            executor=user,
            status__in=[Task.Status.DONE, Task.Status.CANCELLED],
        ).order_by('-finished_at')[:15]
    )

    for t in closed_recent:
        t.spent_text = format_hours(t.spent_hours, unit, norm)
        t.plan_text = format_hours(t.rolled_plan, unit, norm) if t.rolled_plan else 'оценка'

    alerts = []

    gamify.ensure_catalog()

    kpi = gamify.user_kpi_percent(user)
    from gamify.services import _month_bounds_for_user
    kpi_from, kpi_until = _month_bounds_for_user(user)
    kpi_agg = Task.objects.filter(
        executor=user,
        status=Task.Status.DONE,
        finished_at__gte=kpi_from,
        finished_at__lte=kpi_until,
    ).aggregate(p=Sum('plan_hours'), f=Sum('accumulated_hours'))

    level = gamify.level_info(user)
    streak = getattr(user, 'streak', None)

    achievements = (
        UserAchievement.objects.filter(user=user)
        .select_related('achievement')
        .order_by('-awarded_at')
    )

    kudos = (
        Kudos.objects.filter(to_user=user)
        .select_related('from_user')
        .order_by('-created_at')[:3]
    )

    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    cur_from = timezone.make_aware(datetime.combine(week_start, dtime.min))
    prev_from = cur_from - timedelta(days=7)

    h_cur = (
            TimeSession.objects.filter(executor=user, finished_at__gte=cur_from)
            .aggregate(s=Sum('duration_hours'))['s'] or 0
    )

    h_prev = (
            TimeSession.objects.filter(
                executor=user,
                finished_at__gte=prev_from,
                finished_at__lt=cur_from,
            ).aggregate(s=Sum('duration_hours'))['s'] or 0
    )

    done_cur = Task.objects.filter(
        executor=user,
        status=Task.Status.DONE,
        finished_at__gte=cur_from,
    ).count()

    digest = (
        f'Эта неделя: закрыто {done_cur}, время {format_hours(h_cur, unit, norm)}. '
        f'Прошлая неделя: {format_hours(h_prev, unit, norm)}.'
    )

    chart_start = today - timedelta(days=13)
    hours_by_day = {
        row['day']: row['hours'] or 0
        for row in TimeSession.objects.filter(
            executor=user,
            finished_at__date__gte=chart_start,
        )
        .annotate(day=TruncDate('finished_at'))
        .values('day')
        .annotate(hours=Sum('duration_hours'))
    }

    days, max_h = [], 0.0
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        h = hours_by_day.get(d, 0)
        days.append({'label': d.strftime('%d.%m'), 'hours': round(h, 2)})
        max_h = max(max_h, h)

    max_h = max_h or 1.0
    for d in days:
        d['pct'] = int(d['hours'] / max_h * 100)

    ring_dash = round(max(0, min(kpi or 0, 100)) / 100 * RING_C, 1)

    from comms.models import Notification

    if today.weekday() == 4:
        tag = '📅 Итог недели'
        if not Notification.objects.filter(
                recipient=user,
                text__startswith=tag,
                created_at__date__gte=week_start,
        ).exists():
            Notification.objects.create(
                recipient=user,
                text=f'{tag}: закрыто {done_cur}, время {format_hours(h_cur, unit, norm)}.',
                url='/',
                kind=getattr(Notification.Kind, 'INFO', 'info'),
            )

    committed = set(
        WeekCommit.objects.filter(user=user, week_start=week_start)
        .values_list('task_id', flat=True)
    )

    from tasks.models import ShopSession

    open_shop_sessions = {
        s.task_id: s
        for s in ShopSession.objects.filter(
            executor=user, finished_at__isnull=True
        )
    }
    shop_open_task_ids = set(open_shop_sessions.keys())

    # Привязка к задачам, чтобы в шаблоне был доступ к started_at
    for t in open_tasks:
        t.shop_open = open_shop_sessions.get(t.pk)


    # ── Мои цепочки этапов ──
    my_branch_ids = list(
        Task.objects.filter(
            executor=user, branch__isnull=False,
        ).exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED]
        ).values_list('branch_id', flat=True).distinct()
    )

    my_chains = []
    for br in TaskBranch.objects.filter(pk__in=my_branch_ids).select_related('order'):
        stages = list(
            Task.objects.filter(branch=br)
            .select_related('executor')
            .order_by('stage_order')
        )
        if not stages:
            continue
        done = sum(1 for t in stages if t.status == Task.Status.DONE)
        current = next(
            (t for t in stages
             if t.executor_id == user.pk
             and t.status not in (Task.Status.DONE, Task.Status.CANCELLED)),
            None,
        )
        if not current:
            continue
        my_chains.append({
            'branch': br,
            'total': len(stages),
            'done': done,
            'current': current,
            'current_index': stages.index(current) + 1,
            'pct': int(done / len(stages) * 100),
        })

    return render(request, 'staff/dashboard.html', {
        'focus_shop_open': open_shop_sessions.get(focus.pk) if focus else None,
        'focus': focus,
        'cards': cards,
        'my_review': my_review,
        'closed_recent': closed_recent,
        'alerts': alerts,
        'unit': unit,
        'norm': norm,
        'unit_title': UNIT_LABELS[unit],
        'kpi': kpi,
        'kpi_plan': kpi_agg['p'] or 0,
        'kpi_fact': kpi_agg['f'] or 0,
        'ring_dash': ring_dash,
        'committed': committed,
        'week_start': week_start,
        'level': level,
        'streak': streak,
        'achievements': achievements,
        'kudos': kudos,
        'digest': digest,
        'days': days,
        'shop_open_task_ids': shop_open_task_ids,
        'today': today,
        'my_chains': my_chains,
    })


@login_required
def history(request):
    user = request.user

    qs = Task.objects.filter(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    )

    if not user.is_admin_role:
        if user.role and user.role.can_manage and user.department_id:
            qs = qs.filter(executor__department=user.department)
        else:
            qs = qs.filter(executor=user)

    qs = qs.order_by('-finished_at').select_related(
        'executor',
        'executor__department',
    )

    unit = valid_unit(request.GET.get('unit'))
    norm = norm_hours()

    for t in qs:
        t.spent_text = format_hours(t.spent_hours, unit, norm)
        t.plan_text = format_hours(t.rolled_plan, unit, norm) if t.rolled_plan else 'оценка'

    return render(request, 'staff/history.html', {
        'tasks': qs,
        'unit': unit,
        'unit_title': UNIT_LABELS[unit],
    })
