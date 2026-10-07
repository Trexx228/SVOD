from functools import wraps

from django.core.exceptions import PermissionDenied


def role_required(*roles):
    """'manager' — любое управление, 'admin' — админ, иначе точный код роли."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user

            if not user.is_authenticated or not user.is_active:
                raise PermissionDenied('Недостаточно прав для раздела.')

            if user.is_superuser:
                return view(request, *args, **kwargs)

            role_code = getattr(getattr(user, 'role', None), 'code', None)
            ok = False

            for r in roles:
                if r == 'manager' and getattr(user, 'is_boss', False):
                    ok = True
                    break

                if r == 'admin' and getattr(user, 'is_admin_role', False):
                    ok = True
                    break

                if role_code and role_code == r:
                    ok = True
                    break

            if ok:
                return view(request, *args, **kwargs)

            raise PermissionDenied('Недостаточно прав для раздела.')

        return wrapper

    return decorator
