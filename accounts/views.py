from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from comms.models import Attachment
from .forms import RegistrationForm
from .models import SupportTicket, User

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .onboarding import get_progress



SALT = 'accounts.activation'
MAX_AGE = 48 * 3600


def make_token(user):
    return dumps({'uid': user.pk}, salt=SALT)


def _is_admin(user):
    return user.is_superuser or user.is_admin_role


def _safe_redirect_url(request, fallback='/'):
    url = request.META.get('HTTP_REFERER')
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
        return url
    return fallback


def _send_activation(request, user):
    link = request.build_absolute_uri(reverse('activate', args=[make_token(user)]))
    body = (
        f'Здравствуйте, {user.full_name}!\n\n'
        f'Активируйте аккаунт:\n{link}\n\n'
        'Ссылка действует 48 часов.'
    )

    try:
        send_mail(
            'СВОД: подтверждение регистрации',
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            [user.email],
            fail_silently=False,
        )
        return True
    except Exception:
        return False


def register(request):
    if not getattr(settings, 'REGISTRATION_OPEN', False):
        messages.info(
            request,
            'Регистрация закрыта: учётную запись создаёт администратор.',
        )
        return redirect('login')

    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.save()

            if _send_activation(request, user):
                messages.success(
                    request,
                    f'Письмо с подтверждением отправлено на {user.email}.',
                )
            else:
                messages.error(
                    request,
                    'Аккаунт создан, но письмо подтверждения не удалось отправить.',
                )

            return redirect('login')
    else:
        form = RegistrationForm()

    return render(request, 'accounts/register.html', {'form': form})


def activate(request, token):
    try:
        data = loads(token, salt=SALT, max_age=MAX_AGE)
        user = User.objects.get(pk=data['uid'], is_active=False)
    except (BadSignature, SignatureExpired, User.DoesNotExist, KeyError, ValueError, TypeError):
        messages.error(request, 'Ссылка недействительна или устарела.')
        return redirect('login')

    user.is_active = True
    user.save(update_fields=['is_active'])

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    messages.success(request, 'Аккаунт активирован. Добро пожаловать!')

    return redirect('dashboard')


def resend(request):
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        user = User.objects.filter(email=email, is_active=False).first()

        if user:
            _send_activation(request, user)

        messages.info(
            request,
            'Если такой неактивный аккаунт есть — письмо отправлено.',
        )
        return redirect('resend')

    return render(request, 'accounts/resend.html')


@login_required
def profile(request):
    return render(request, 'accounts/profile.html', {'me': request.user})


@login_required
def profile_save(request):
    if request.method != 'POST':
        return redirect('profile')

    me = request.user
    name = (request.POST.get('full_name') or '').strip()

    if name:
        me.full_name = name
        me.save(update_fields=['full_name'])
        messages.success(request, 'Профиль обновлён.')
    else:
        messages.error(request, 'ФИО не может быть пустым.')

    return redirect('profile')


def support_send(request):
    if request.method != 'POST':
        return redirect(_safe_redirect_url(request))

    text = (request.POST.get('text') or '').strip()
    kind = request.POST.get('kind', 'bug')

    if not text:
        messages.error(request, 'Опишите обращение — пустые не отправляются.')
        return redirect(_safe_redirect_url(request))

    ticket = SupportTicket.objects.create(
        author=request.user if request.user.is_authenticated else None,
        kind=kind if kind in SupportTicket.Kind.values else 'bug',
        text=text[:2000],
        page=request.META.get('HTTP_REFERER', '')[:200],
    )

    if request.user.is_authenticated:
        attach_ids = [
            x
            for x in (request.POST.get('attach_ids') or '').split(',')
            if x.isdigit()
        ]

        if attach_ids:
            Attachment.objects.filter(
                pk__in=attach_ids,
                owner=request.user,
                task__isnull=True,
                message__isnull=True,
                progress__isnull=True,
                ticket__isnull=True,
            ).update(ticket=ticket)

    names = list(
        Attachment.objects.filter(ticket=ticket).values_list('name', flat=True)
    )

    who = request.user.full_name if request.user.is_authenticated else 'гость'
    mail = request.user.email if request.user.is_authenticated else 'без учётки'

    support_email = getattr(settings, 'SUPPORT_EMAIL', '')
    if support_email:
        to = [support_email]
    else:
        to = list(
            User.objects.filter(is_superuser=True)
            .exclude(email='')
            .values_list('email', flat=True)
        )

    body = f'От: {who} ({mail})\nСтраница: {ticket.page or "—"}\n\n{text}'
    if names:
        body += '\n\nВложения: ' + ', '.join(names)

    if not to:
        messages.warning(
            request,
            'Обращение сохранено, но получатель не настроен.',
        )
        return redirect(_safe_redirect_url(request))

    try:
        send_mail(
            f'СВОД · техподдержка: {ticket.get_kind_display()}',
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to,
            fail_silently=False,
        )
        messages.success(request, 'Обращение отправлено в техподдержку. Спасибо!')
    except Exception:
        messages.warning(
            request,
            'Обращение сохранено, но письмо не удалось отправить.',
        )

    return redirect(_safe_redirect_url(request))


@login_required
def support_inbox(request):
    """Входящий ящик техподдержки: видят только администраторы."""
    if not _is_admin(request.user):
        raise PermissionDenied('Раздел доступен только администраторам.')

    if request.method == 'POST':
        ticket_id = request.POST.get('ticket') or ''

        if str(ticket_id).isdigit():
            t = SupportTicket.objects.filter(pk=ticket_id).first()
            if t:
                t.done = not t.done
                t.save(update_fields=['done'])
                messages.success(request, 'Статус обращения обновлён.')

        return redirect('support_inbox')

    tickets = (
        SupportTicket.objects.select_related('author')
        .prefetch_related('attachments')
        .all()
    )

    return render(request, 'accounts/support_inbox.html', {'tickets': tickets})

@login_required
@require_POST
def tour_mark_seen(request, tour_id):
    tour_id = (tour_id or '').strip()[:64]
    if not tour_id:
        return JsonResponse({'ok': False, 'error': 'no id'}, status=400)

    seen = dict(request.user.tour_seen or {})
    seen[tour_id] = timezone.now().isoformat()
    request.user.tour_seen = seen
    request.user.save(update_fields=['tour_seen'])
    return JsonResponse({'ok': True})


@login_required
@require_POST
def tour_reset(request):
    request.user.tour_seen = {}
    request.user.onboarding_done = False
    request.user.save(update_fields=['tour_seen', 'onboarding_done'])
    return JsonResponse({'ok': True})


@login_required
def onboarding_start(request):
    from django.shortcuts import redirect
    from django.urls import reverse

    _, _, next_step, is_done = get_progress(request.user)
    if is_done or not next_step:
        return redirect('onboarding_finish')

    try:
        target = reverse(next_step['url_name'])
    except Exception:
        target = next_step['url']
    return redirect(target)


@login_required
def onboarding_next(request):
    done, total, next_step, is_done = get_progress(request.user)
    return JsonResponse({
        'done': done,
        'total': total,
        'is_done': is_done,
        'already_done': request.user.onboarding_done,
        'next': next_step,
    })


@login_required
@require_POST
def onboarding_finish(request):
    request.user.onboarding_done = True
    request.user.save(update_fields=['onboarding_done'])
    return JsonResponse({'ok': True})


@login_required
@require_POST
def onboarding_skip(request):
    # Ничего не помечаем — карточка появится снова при следующем заходе.
    return JsonResponse({'ok': True})


@login_required
@require_POST
def hints_mark_seen(request):
    """Отметить, что пользователь увидел пульсирующие подсказки."""
    from django.utils import timezone
    seen = dict(request.user.tour_seen or {})
    seen['hints_home'] = timezone.now().isoformat()
    request.user.tour_seen = seen
    request.user.save(update_fields=['tour_seen'])
    return JsonResponse({'ok': True})
