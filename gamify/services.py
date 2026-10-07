from django.db.models import F, Sum

from tasks.models import Task, TaskLog
from tasks.utils import kpi_percent

from datetime import datetime, time as dtime, timedelta
from django.utils import timezone

from django.db import models
from django.contrib.auth import get_user_model
User = get_user_model()

LEVELS = [
    (0, 'Стажёр'),
    (20, 'Специалист'),
    (60, 'Профи'),
    (150, 'Мастер'),
]

ACHIEVEMENTS = [
    ('first', '🎯', 'Первый результат', 'Закрыл первую задачу'),
    ('speedy', '⚡', 'Скоростной', '3 задачи за неделю в срок или быстрее плана'),
    ('sniper', '🥇', 'Снайпер', 'KPI 95–105% за месяц при 3+ закрытых задачах'),
    ('reliable', '🛡️', 'Надёжный', '5+ задач за месяц без возвратов на доработку'),
    ('streak7', '🔥', 'Неделя на ритме', '7 дней подряд без просрочек'),
    ('streak30', '🏔️', 'Месяц на ритме', '30 дней подряд без просрочек'),
]


def ensure_catalog():
    from .models import Achievement

    for code, icon, title, desc in ACHIEVEMENTS:
        Achievement.objects.get_or_create(
            code=code,
            defaults={
                'icon': icon,
                'title': title,
                'description': desc,
            },
        )


def closed_tasks(user, since=None):
    qs = Task.objects.filter(executor=user, status=Task.Status.DONE)

    if since:
        qs = qs.filter(finished_at__gte=since)

    return qs


def _month_bounds_for_user(user, now=None):
    """Границы текущего календарного месяца с учётом даты устройства.

    Если сотрудник устроился в этом месяце — начало = дата устройства.
    Иначе — первое число месяца.

    Возвращает (start_dt, end_dt) — aware datetime.
    """
    now = now or timezone.now()
    today = timezone.localdate()
    month_start = today.replace(day=1)

    date_joined = getattr(user, 'date_joined', None)
    if date_joined:
        joined = timezone.localtime(date_joined).date()
        if joined > month_start:
            month_start = joined

    start_dt = timezone.make_aware(
        datetime.combine(month_start, dtime.min),
        timezone.get_current_timezone(),
    )
    return start_dt, now


def user_kpi_percent(user, since=None, until=None):
    """KPI = план / факт за период.

    По умолчанию — текущий календарный месяц с учётом даты устройства.
    Возвращает процент (int) или None, если данных нет.
    """
    if since is None and until is None:
        since, until = _month_bounds_for_user(user)

    qs = Task.objects.filter(
        executor=user,
        status=Task.Status.DONE,
        finished_at__gte=since,
    )
    if until is not None:
        qs = qs.filter(finished_at__lte=until)

    agg = qs.aggregate(
        plan=Sum('plan_hours'),
        fact=Sum('accumulated_hours'),
    )
    return kpi_percent(agg['plan'] or 0, agg['fact'] or 0)


def manager_kpi_percent(user):
    """Композитный KPI руководителя отдела.

    Компоненты:
      40% — KPI отдела (план/факт по всем задачам отдела)
      30% — OTD отдела (закрытые в срок)
      20% — точность планирования (план/факт по поставленным им задачам)
      10% — личный KPI (его собственные задачи как исполнителя)

    Возвращает int (0-100) или None, если данных нет.
    """
    if not user.department_id:
        return None

    since, until = _month_bounds_for_user(user)

    # Сотрудники его отдела
    member_ids = list(
        User.objects.filter(
            department_id=user.department_id, is_active=True
        ).values_list('id', flat=True)
    )

    # ── 1. KPI отдела ──
    team_qs = Task.objects.filter(
        executor_id__in=member_ids,
        status=Task.Status.DONE,
        finished_at__gte=since,
        finished_at__lte=until,
    )
    team_agg = team_qs.aggregate(p=Sum('plan_hours'), f=Sum('accumulated_hours'))
    team_kpi = kpi_percent(team_agg['p'] or 0, team_agg['f'] or 0)

    # ── 2. OTD отдела ──
    with_due = team_qs.filter(due__isnull=False)
    total_due = with_due.count()
    on_time = with_due.filter(
        finished_at__date__lte=models.F('due')
    ).count() if total_due else 0
    otd = int(round(100 * on_time / total_due)) if total_due else None

    # ── 3. Точность планирования (его задачи как постановщика) ──
    req_qs = Task.objects.filter(
        requester=user,
        status=Task.Status.DONE,
        finished_at__gte=since,
        finished_at__lte=until,
    )
    req_agg = req_qs.aggregate(p=Sum('plan_hours'), f=Sum('accumulated_hours'))
    planning_kpi = kpi_percent(req_agg['p'] or 0, req_agg['f'] or 0)

    # ── 4. Личный KPI ──
    personal_kpi = user_kpi_percent(user, since, until)

    # ── Композит ──
    parts = []
    if team_kpi is not None:
        parts.append((team_kpi, 0.4))
    if otd is not None:
        parts.append((otd, 0.3))
    if planning_kpi is not None:
        parts.append((planning_kpi, 0.2))
    if personal_kpi is not None:
        parts.append((personal_kpi, 0.1))

    if not parts:
        return None

    # Нормируем веса, если не все компоненты доступны
    total_w = sum(w for _, w in parts)
    result = sum(v * w for v, w in parts) / total_w
    return int(round(result))


def level_info(user):
    hours = closed_tasks(user).aggregate(s=Sum('accumulated_hours'))['s'] or 0

    current, next_lvl = LEVELS[0], None

    for i, (h, name) in enumerate(LEVELS):
        if hours >= h:
            current = (h, name)
            next_lvl = LEVELS[i + 1] if i + 1 < len(LEVELS) else None

    if next_lvl:
        progress = (hours - current[0]) / (next_lvl[0] - current[0]) * 100
        label = f'{hours:.0f} / {next_lvl[0]} ч до уровня «{next_lvl[1]}»'
    else:
        progress, label = 100, f'{hours:.0f} ч — максимальный уровень'

    return {
        'name': current[1],
        'hours': hours,
        'progress': min(100, max(0, progress)),
        'label': label,
    }


def touch_streak(user):
    from .models import Streak

    streak, _ = Streak.objects.get_or_create(user=user)
    today = timezone.localdate()

    if streak.last_ok_date == today:
        return streak

    if streak.last_ok_date and (today - streak.last_ok_date).days == 1:
        streak.current_days += 1
    else:
        streak.current_days = 1

    streak.best_days = max(streak.best_days, streak.current_days)
    streak.last_ok_date = today
    streak.save(update_fields=['current_days', 'best_days', 'last_ok_date'])

    return streak


def _award(user, code):
    from .models import Achievement, UserAchievement

    ach = Achievement.objects.filter(code=code).first()
    if ach:
        UserAchievement.objects.get_or_create(user=user, achievement=ach)


def check_achievements(user):
    from .models import UserAchievement

    ensure_catalog()

    have = set(
        UserAchievement.objects.filter(user=user)
        .values_list('achievement__code', flat=True)
    )

    now = timezone.now()

    if 'first' not in have and closed_tasks(user).exists():
        _award(user, 'first')

    week = closed_tasks(user, now - timedelta(days=7))
    if (
            'speedy' not in have
            and week.filter(
        plan_hours__gt=0,
        accumulated_hours__lte=F('plan_hours'),
    ).count() >= 3
    ):
        _award(user, 'speedy')

    closed30 = closed_tasks(user, now - timedelta(days=30))
    closed30_count = closed30.count()
    kpi = user_kpi_percent(user, since=now - timedelta(days=30))

    if 'sniper' not in have and closed30_count >= 3 and kpi and 95 <= kpi <= 105:
        _award(user, 'sniper')

    reworks = TaskLog.objects.filter(
        kind=TaskLog.Kind.REWORK,
        task__executor=user,
        created_at__gte=now - timedelta(days=30),
    ).exists()

    if 'reliable' not in have and closed30_count >= 5 and not reworks:
        _award(user, 'reliable')

    streak = getattr(user, 'streak', None)
    if streak:
        if 'streak7' not in have and streak.current_days >= 7:
            _award(user, 'streak7')

        if 'streak30' not in have and streak.current_days >= 30:
            _award(user, 'streak30')
