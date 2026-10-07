"""Middleware: разлогинивает уволенных и неактивных пользователей."""
from django.contrib.auth import logout
from django.shortcuts import redirect


class DismissedUserLogoutMiddleware:
    """Если пользователь уволен/деактивирован, но сессия жива — принудительный logout.

    Проверка на каждом запросе. Работает поверх ModelBackend и ADBackend.
    """

    EXEMPT_PATHS = (
        '/accounts/login/',
        '/accounts/logout/',
        '/accounts/password_reset/',
        '/accounts/password_change/',
        '/static/',
        '/media/',
        '/admin/login/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)

        if user and user.is_authenticated:
            path_ok = not any(
                request.path.startswith(p) for p in self.EXEMPT_PATHS
            )
            if path_ok and (
                    not user.is_active
                    or getattr(user, 'is_dismissed', False)
                    or getattr(user, 'is_archived', False)
            ):
                logout(request)
                return redirect('login')

        return self.get_response(request)
