from datetime import timedelta

from django.utils import timezone


ONLINE_WINDOW_MIN = 15     # кого считаем «сейчас онлайн»
HEARTBEAT_MIN = 2          # как часто обновляем last_activity_at
IDLE_THRESHOLD_MIN = 5     # разрыв, после которого считаем «вернулся»


class ActivityMiddleware:
    """Обновляет User.last_activity_at без триггера save() модели.

    Правила:
    - обновляем не чаще HEARTBEAT_MIN минут (throttling);
    - пока пользователь активен — idle_since сбрасывается в None;
    - если запрос не GET/POST — не считаем активностью (например, HEAD, OPTIONS).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)

        if (
                user
                and user.is_authenticated
                and request.method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
                and not request.path.startswith('/static/')
                and not request.path.startswith('/media/')
        ):
            self._touch(user)

        return self.get_response(request)

    @staticmethod
    def _touch(user):
        try:
            from accounts.models import User
            now = timezone.now()
            last = user.last_activity_at

            # Throttle: если обновлялись только что — не дёргаем БД
            if last and (now - last) < timedelta(minutes=HEARTBEAT_MIN):
                return

            update_fields = {'last_activity_at': now}

            # Если разрыв больше порога и был установлен idle_since — сбрасываем
            if last and (now - last) >= timedelta(minutes=IDLE_THRESHOLD_MIN) \
                    and user.idle_since is not None:
                update_fields['idle_since'] = None

            User.objects.filter(pk=user.pk).update(**update_fields)

            # Локально синхронизируем объект (чтобы UI видел актуальное значение)
            user.last_activity_at = now
            if 'idle_since' in update_fields:
                user.idle_since = None
        except Exception:
            # Middleware не должна ломать запросы
            pass
