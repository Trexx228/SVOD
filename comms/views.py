import os

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.models import Department
from tasks.models import Task, TaskProgress

from .models import Attachment, Message, Notification, Thread
from .services import notify, task_participants, task_url

User = get_user_model()

ALLOWED_MIME = {
    'image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp', 'image/bmp',
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain', 'text/csv',
    'application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed',
}

ALLOWED_EXT = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.ppt', '.pptx', '.txt', '.csv', '.zip', '.rar', '.7z',
}


def _safe_redirect_url(request, fallback='/'):
    url = request.META.get('HTTP_REFERER')
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
        return url
    return fallback


def _admins():
    return list(
        User.objects.filter(
            Q(is_superuser=True) | Q(role__can_admin=True),
            is_active=True,
            ).distinct()
    )


def _bosses_of(user):
    if not user.department_id:
        return []
    return list(
        User.objects.filter(
            role__can_manage=True,
            department_id=user.department_id,
            is_active=True,
        ).exclude(pk=user.pk)
    )


def _active_recipients(users, exclude=None):
    exclude_pk = exclude.pk if exclude else None
    return [
        u for u in users
        if u and u.pk and u.is_active and u.pk != exclude_pk
    ]


@login_required
def poll(request):
    """Лёгкий поллинг для бейджей + список невзятых urgent-задач."""
    user = request.user

    notes = Notification.objects.filter(recipient=user, read=False).count()

    unread_qs = (
        Message.objects
        .filter(thread__participants=user)
        .exclude(author=user)
        .exclude(read_by=user)
    )
    unread_count = unread_qs.count()
    unread_chats = unread_qs.values('thread_id').distinct().count()

    top = (
        Notification.objects.filter(recipient=user, read=False)
        .order_by('-id').first()
    )

    urgent_list = []
    urgent_qs = (
        Task.objects.filter(
            executor=user,
            status=Task.Status.NEW,
            priority='urgent',
        )
        .select_related('requester')
        .order_by('created_at')[:5]
    )
    for t in urgent_qs:
        urgent_list.append({
            'id': t.pk,
            'title': t.title,
            'requester': t.requester.full_name if t.requester_id else '',
            'plan_hours': t.plan_hours or 0,
            'due': t.due.isoformat() if t.due else '',
            'created_at': t.created_at.isoformat() if t.created_at else '',
        })

    return JsonResponse({
        'notes': notes,
        'msgs': unread_chats,
        'msgs_total': unread_count,
        'chats': unread_chats,
        'top': {
            'id': top.id,
            'text': top.text,
            'url': top.url or '',
        } if top else None,
        'urgent': urgent_list,
    })


@login_required
def notifications(request):
    items = request.user.notifications.order_by('-created_at')[:100]
    return render(request, 'comms/notifications.html', {'items': items})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not n.read:
        n.read = True
        n.save(update_fields=['read'])
    if n.url and url_has_allowed_host_and_scheme(n.url, allowed_hosts={request.get_host()}):
        return redirect(n.url)
    return redirect('notifications')


@login_required
def notifications_read(request):
    if request.method == 'POST':
        request.user.notifications.filter(read=False).update(read=True)
    return redirect('notifications')


@login_required
def dialogs(request):
    """Чаты в стиле Telegram: список слева, открытый чат справа.

    URL: /comms/?t=<thread_pk>  — открыть конкретный чат.
    """
    user = request.user
    q = request.GET.get('q', '').strip()
    dept = request.GET.get('dept', '').strip()
    thread_pk = request.GET.get('t', '').strip()

    read_by_me = Prefetch(
        'read_by',
        queryset=User.objects.filter(pk=user.pk),
        to_attr='read_by_me',
    )
    messages_prefetch = Prefetch(
        'messages',
        queryset=Message.objects.select_related('author').prefetch_related(read_by_me),
        to_attr='messages_list',
    )

    threads = []
    for t in user.threads.prefetch_related('participants', messages_prefetch):
        msgs = getattr(t, 'messages_list', [])
        last = msgs[-1] if msgs else None
        unread = sum(
            1 for m in msgs
            if m.author_id != user.pk and not getattr(m, 'read_by_me', [])
        )
        threads.append({
            'thread': t,
            'title': t.display_title(user),
            'last': last,
            'unread': unread,
        })

    threads.sort(
        key=lambda x: x['last'].created_at if x['last'] else x['thread'].created_at,
        reverse=True,
    )

    active_thread = None
    active_items = []
    active_last_id = 0

    if thread_pk.isdigit():
        active_thread = Thread.objects.filter(
            pk=int(thread_pk), participants=user
        ).select_related('task').first()

        if active_thread:
            unread = active_thread.messages.exclude(
                author=user).exclude(read_by=user)
            if unread.exists():
                through = Message.read_by.through
                through.objects.bulk_create(
                    [through(message_id=m.id, user_id=user.id) for m in unread],
                    ignore_conflicts=True,
                )

            last_day = None
            for m in active_thread.messages.select_related('author').prefetch_related('attachments'):
                day = timezone.localtime(m.created_at).date()
                if day != last_day:
                    active_items.append({'sep': day})
                    last_day = day
                active_items.append({
                    'msg': m,
                    'own': m.author_id == user.pk,
                })

            active_last_id = (
                    active_thread.messages.order_by('-id')
                    .values_list('id', flat=True).first() or 0
            )

    people = (
        User.objects.filter(is_active=True)
        .exclude(pk=user.pk)
        .select_related('department')
        .order_by('full_name')
    )
    if q:
        people = people.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    if dept.isdigit():
        people = people.filter(department_id=int(dept))

    by_dept = {}
    for u in people:
        key = u.department.name if u.department else 'Без подразделения'
        by_dept.setdefault(key, []).append(u)

    departments = (
        Department.objects.filter(users__is_active=True)
        .distinct().order_by('name')
    )

    return render(request, 'comms/dialogs.html', {
        'threads': threads,
        'active_thread': active_thread,
        'active_items': active_items,
        'active_last_id': active_last_id,
        'people': people,
        'by_dept': by_dept,
        'departments': departments,
        'q': q,
        'dept': dept,
        'dept_id': int(dept) if dept.isdigit() else None,
    })


@login_required
def start_direct(request, user_pk=None):
    target_pk = user_pk or request.GET.get('to') or request.POST.get('to') or 0

    if not str(target_pk).isdigit():
        messages.error(request, 'Некорректный пользователь.')
        return redirect('dialogs')

    other = get_object_or_404(User, pk=target_pk, is_active=True)

    if other.pk == request.user.pk:
        return redirect('dialogs')

    thread = (
        Thread.objects.filter(direct=True)
        .filter(participants=request.user)
        .filter(participants=other)
        .annotate(participants_count=Count('participants', distinct=True))
        .filter(participants_count=2)
        .first()
    )

    if not thread:
        thread = Thread.objects.create(direct=True)
        thread.participants.add(request.user, other)

    return redirect(f"{reverse('dialogs')}?t={thread.pk}")


@login_required
def task_chat(request, pk):
    task = get_object_or_404(Task, pk=pk)
    user = request.user

    executor_department_id = task.executor.department_id if task.executor_id else None

    allowed = (
            user.is_superuser
            or user.is_admin_role
            or task.executor_id == user.pk
            or task.requester_id == user.pk
            or (
                    user.role
                    and user.role.can_manage
                    and user.department_id
                    and executor_department_id
                    and executor_department_id == user.department_id
            )
    )

    if not allowed:
        messages.error(request, 'Нет доступа к чату этой задачи.')
        return redirect('dashboard')

    thread = Thread.objects.filter(task=task).first()
    if not thread:
        thread = Thread.objects.create(task=task, title=task.title)
        for u in (task.executor, task.requester):
            if u:
                thread.participants.add(u)

    thread.participants.add(user)

    return redirect(f"{reverse('dialogs')}?t={thread.pk}")


@login_required
def thread_open(request, pk):
    """Открыть чат — редирект в двухпанельный /comms/?t=<pk>."""
    thread = get_object_or_404(Thread, pk=pk)
    if not thread.participants.filter(pk=request.user.pk).exists():
        messages.error(request, 'Вы не участник этого чата.')
        return redirect('dialogs')
    return redirect(f"{reverse('dialogs')}?t={thread.pk}")


@login_required
def thread_send(request, pk):
    thread = get_object_or_404(Thread, pk=pk)

    if not thread.participants.filter(pk=request.user.pk).exists():
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'forbidden'}, status=403)
        messages.error(request, 'Вы не участник этого чата.')
        return redirect('dialogs')

    if request.method != 'POST':
        return redirect(f"{reverse('dialogs')}?t={thread.pk}")

    text = request.POST.get('text', '').strip()
    if not text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'empty'}, status=400)
        messages.error(request, 'Пустое сообщение не отправлено.')
        return redirect(f"{reverse('dialogs')}?t={thread.pk}")

    msg = Message.objects.create(
        thread=thread, author=request.user, text=text[:2000],
    )

    attach_ids = [
        x for x in (request.POST.get('attach_ids') or '').split(',')
        if x.isdigit()
    ]
    if attach_ids:
        Attachment.objects.filter(
            pk__in=attach_ids, owner=request.user,
            task__isnull=True, message__isnull=True,
            progress__isnull=True, ticket__isnull=True,
        ).update(message=msg)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'id': msg.pk,
            'author': msg.author.full_name,
            'text': msg.text,
            'own': True,
            'ts': timezone.localtime(msg.created_at).strftime('%d.%m %H:%M'),
        })

    return redirect(f"{reverse('dialogs')}?t={thread.pk}")


@login_required
def thread_poll(request, pk):
    """JSON новых сообщений для AJAX-обновления открытого чата."""
    thread = get_object_or_404(Thread, pk=pk)

    if not thread.participants.filter(pk=request.user.pk).exists():
        return JsonResponse({'error': 'forbidden'}, status=403)

    try:
        since = int(request.GET.get('since') or 0)
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0

    rows = []
    for m in thread.messages.filter(id__gt=since).select_related('author'):
        rows.append({
            'id': m.id,
            'author': m.author.full_name,
            'own': m.author_id == request.user.pk,
            'text': m.text,
            'ts': timezone.localtime(m.created_at).strftime('%d.%m %H:%M'),
        })

    return JsonResponse({'messages': rows})


@login_required
def task_event(request, pk):
    task = get_object_or_404(Task, pk=pk)
    user = request.user
    fallback = task_url(task) or f'/tasks/task/{task.pk}/'

    if request.method != 'POST':
        return redirect(_safe_redirect_url(request, fallback))

    kind = request.POST.get('kind', '')
    text = request.POST.get('text', '').strip()

    participant = task.executor_id == user.pk or task.requester_id == user.pk
    boss = user.is_boss

    if not (participant or boss):
        messages.error(request, 'Недостаточно прав для этого действия.')
        return redirect(_safe_redirect_url(request, fallback))

    if kind == 'stakeholders':
        if not text:
            messages.error(request, 'Введите текст оповещения.')
        else:
            others = _active_recipients(task_participants(task), exclude=user)
            notify(others, f'{user.full_name}: {text}', task_url(task),
                   Notification.Kind.ACTION)
            messages.success(request, 'Заинтересованные оповещены.')

    elif kind == 'arrival' and task.kind == 'supply' and (task.executor_id == user.pk or boss):
        task.warehouse = True
        task.save(update_fields=['warehouse'])
        TaskProgress.objects.create(
            task=task, author=user,
            text='Прибыло на склад. Можно запускать в производство.',
        )
        others = _active_recipients(task_participants(task), exclude=user)
        notify(others, f'Прибыло на склад: {task.title} — можно запускать в производство.',
               task_url(task), Notification.Kind.ARRIVAL)
        messages.success(request, 'Отметили прибытие и оповестили заинтересованных.')

    elif kind == 'launch' and (task.executor_id == user.pk or boss):
        TaskProgress.objects.create(task=task, author=user,
                                    text='Можно запускать в производство.')
        others = _active_recipients(task_participants(task, extra=_admins()), exclude=user)
        notify(others, f'Можно запускать в производство: {task.title}',
               task_url(task), Notification.Kind.ARRIVAL)
        messages.success(request, 'Оповещение о запуске отправлено.')

    elif kind == 'block':
        if not text:
            messages.error(request, 'Опишите, что мешает.')
        else:
            TaskProgress.objects.create(task=task, author=user, text=f'Затор: {text}')
            recipients = _active_recipients(_bosses_of(user) + _admins(), exclude=user)
            notify(recipients, f'Затор по задаче {task.title}: {text}',
                   task_url(task), Notification.Kind.ALERT)
            messages.success(request, 'Руководство и администраторы оповещены о заторе.')

    else:
        messages.error(request, 'Неизвестное действие.')

    return redirect(_safe_redirect_url(request, fallback))


@login_required
def upload_attach(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method'}, status=405)

    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'error': 'nofile'}, status=400)
    if f.size > 10 * 1024 * 1024:
        return JsonResponse({'error': 'big'}, status=400)

    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED_EXT:
        return JsonResponse({'error': 'ext'}, status=400)

    try:
        import magic
        mime = magic.from_buffer(f.read(1024), mime=True)
        f.seek(0)
        if mime not in ALLOWED_MIME:
            return JsonResponse({'error': 'mime'}, status=400)
    except ImportError:
        pass
    except Exception:
        try:
            f.seek(0)
        except Exception:
            pass

    content_type = f.content_type or ''
    a = Attachment.objects.create(
        owner=request.user,
        file=f,
        name=f.name[:150],
        size=f.size,
        image=content_type.startswith('image/'),
    )
    return JsonResponse({
        'id': a.pk,
        'url': a.file.url,
        'name': a.name,
        'image': a.image,
    })


@login_required
def people_json(request):
    users = User.objects.filter(is_active=True).order_by('full_name')
    departments = Department.objects.all().order_by('name')
    return JsonResponse({
        'users': [
            {'id': u.pk, 'name': u.full_name, 'dept': u.department_id}
            for u in users
        ],
        'depts': [
            {'id': d.pk, 'name': d.name}
            for d in departments
        ],
    })


@login_required
def stakeholders_send(request, pk):
    task = get_object_or_404(Task, pk=pk)
    user = request.user
    fallback = task_url(task) or f'/tasks/task/{task.pk}/'

    if request.method != 'POST':
        return redirect(_safe_redirect_url(request, fallback))

    participant = task.executor_id == user.pk or task.requester_id == user.pk
    if not (participant or user.is_boss):
        messages.error(request, 'Недостаточно прав для этого действия.')
        return redirect(_safe_redirect_url(request, fallback))

    text = request.POST.get('text', '').strip()
    ids = [x for x in (request.POST.get('exec_ids') or '').split(',') if x.isdigit()]

    targets = list(User.objects.filter(pk__in=ids, is_active=True).exclude(pk=user.pk))

    if not text:
        messages.error(request, 'Введите текст оповещения.')
    elif not targets:
        messages.error(request, 'Отметьте, кого уведомить.')
    else:
        thread = Thread.objects.filter(task=task).first()
        url = reverse('thread_open', args=[thread.pk]) if thread else task_url(task)

        notify(targets, f'📣 {user.full_name} по задаче «{task.title}»: {text}',
               url, Notification.Kind.ALERT)

        if thread:
            thread.participants.add(user)
            Message.objects.create(
                thread=thread, author=user,
                text=f'📣 Оповещение заинтересованных: {text}',
            )
        messages.success(request, f'Оповещение отправлено: {len(targets)} чел.')

    return redirect(_safe_redirect_url(request, fallback))
