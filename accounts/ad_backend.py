"""
Active Directory authentication backend.
Аутентификация через LDAP (NTLM bind)
JIT-создание пользователя в БД при первом входе
Маппинг групп AD → роли по AD_GROUP_MAP из settings
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db import transaction
from ldap3 import ALL, NTLM, SUBTREE, Connection, Server
from ldap3.utils.conv import escape_filter_chars

logger = logging.getLogger(__name__)
User = get_user_model()


class ADBackend(ModelBackend):
    """Аутентификация через AD. Принимает логин или email."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not getattr(settings, 'AD_ENABLED', False):
            return None

        if username is None or password is None:
            return None

        username = username.strip()
        if not username or not password:
            return None

        ad_server = getattr(settings, 'AD_SERVER', None)
        ad_domain = getattr(settings, 'AD_DOMAIN', None)
        ad_user_base = getattr(settings, 'AD_USER_BASE', None)

        if not ad_server or not ad_domain or not ad_user_base:
            logger.error('AD_SERVER, AD_DOMAIN or AD_USER_BASE is not configured')
            return None

        escaped_username = escape_filter_chars(username)
        is_email = '@' in username

        if is_email:
            search_filter = f'(|(userPrincipalName={escaped_username})(mail={escaped_username}))'
            bind_user = username
        else:
            search_filter = f'(sAMAccountName={escaped_username})'
            bind_user = f'{ad_domain}\\{username}'

        conn = None
        try:
            server = Server(ad_server, get_info=ALL)
            conn = Connection(
                server,
                user=bind_user,
                password=password,
                authentication=NTLM,
                auto_bind=True,
                read_only=True,
            )
        except Exception as e:
            logger.warning('AD bind failed for %s: %s', username, e)
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:
                    pass
            return None

        try:
            conn.search(
                search_base=ad_user_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=[
                    'sAMAccountName',
                    'userPrincipalName',
                    'displayName',
                    'mail',
                    'memberOf',
                ],
            )

            if not conn.entries:
                logger.warning('AD user not found: %s', username)
                return None

            entry = conn.entries[0]
            ad_attrs = {
                'displayName': _attr(entry, 'displayName'),
                'mail': _attr(entry, 'mail'),
                'userPrincipalName': _attr(entry, 'userPrincipalName'),
                'memberOf': _list_attr(entry, 'memberOf'),
            }
        except Exception as e:
            logger.error('AD search error for %s: %s', username, e)
            return None
        finally:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:
                    pass

        email = (ad_attrs['mail'] or ad_attrs['userPrincipalName'] or '').strip().lower()
        if not email:
            logger.warning('AD user %s has no email', username)
            return None

        display_name = (ad_attrs['displayName'] or '').strip()
        full_name = display_name or username
        role_code = _resolve_role(ad_attrs['memberOf'])

        try:
            role = None
            if role_code:
                from accounts.models import Role

                role = Role.objects.filter(code=role_code).first()
                if role is None:
                    logger.warning('Role %s not found in DB', role_code)

            with transaction.atomic():
                user = User.objects.filter(email__iexact=email).first()

                if user is None:
                    user = User(
                        email=email,
                        full_name=full_name,
                        is_active=True,
                    )

                    if role is not None:
                        user.role = role

                    # Если роль админская — сразу даём staff, чтобы
                    # пользовательский save() (без update_fields) это записал.
                    if user.is_admin_role:
                        user.is_staff = True

                    user.set_unusable_password()
                    user.save()
                    logger.info('AD user created: %s', email)
                else:
                    if not user.is_active:
                        logger.warning('AD user %s is deactivated', email)
                        return None

                    update_fields = []

                    if user.email != email:
                        user.email = email
                        update_fields.append('email')

                    if display_name and user.full_name != display_name:
                        user.full_name = display_name
                        update_fields.append('full_name')

                    if role is not None and user.role_id != role.pk:
                        user.role = role
                        update_fields.append('role')

                    # save(update_fields=[...]) не вызовет остальную логику
                    # пользовательского save() модели (в частности is_staff),
                    # поэтому синхронизируем флаг здесь явно.
                    if user.is_admin_role and not user.is_staff:
                        user.is_staff = True
                        if 'is_staff' not in update_fields:
                            update_fields.append('is_staff')

                    if update_fields:
                        user.save(update_fields=update_fields)

        except Exception as e:
            logger.error('Failed to provision AD user %s: %s', email, e)
            return None

        return user


def _to_str(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'ignore')
    return str(value)


def _attr(entry, name):
    try:
        value = entry[name].value
    except Exception:
        return None

    if value is None:
        return None

    if isinstance(value, (list, tuple, set)):
        value = next(iter(value), None)

    if value is None:
        return None

    value = _to_str(value).strip()
    return value or None


def _list_attr(entry, name):
    try:
        values = entry[name].values
    except Exception:
        return []

    if not values:
        return []

    if not isinstance(values, (list, tuple, set)):
        values = [values]

    values = [_to_str(value).strip() for value in values if value is not None]
    return [value for value in values if value]


def _resolve_role(member_of):
    group_map = getattr(settings, 'AD_GROUP_MAP', {}) or {}
    if not member_of:
        return None

    # Предварительно опускаем ключи карты в нижний регистр —
    # избегаем повторного .lower() внутри вложенного цикла.
    lowered_map = {str(ad_group).lower(): code for ad_group, code in group_map.items() if code}

    matched = set()
    for group_dn in member_of:
        group_dn_lower = _to_str(group_dn).lower()
        for ad_group_lower, role_code in lowered_map.items():
            if ad_group_lower in group_dn_lower:
                matched.add(role_code)

    if not matched:
        return None

    if 'admin' in matched:
        return 'admin'
    if 'manager' in matched:
        return 'manager'
    return 'staff'
