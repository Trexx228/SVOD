import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default='0'):
    return os.environ.get(name, default) == '1'


# ─── Dev-режим: локальная разработка ───
# Если в корне проекта есть файл `.dev-mode` — DEBUG включается сам,
# без возни с переменными окружения. На продакшене этого файла нет.
_DEV_MARKER = BASE_DIR / '.dev-mode'
DEBUG = _env_bool('DJANGO_DEBUG', '1' if _DEV_MARKER.exists() else '0')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'dev-insecure-key-change-me'
    else:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set when DEBUG is disabled'
        )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if host.strip()
]

# За HTTPS-прокси домены нужно перечислять явно, иначе POST-формы
# (включая логин) получат 403 CSRF. Локально можно оставить пустым.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

INSTALLED_APPS = [
    'config.apps.SvodAdminConfig',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'core',
    'tasks',
    'dashboard',
    'manager',
    'gamify',
    'comms',
    'admin_panel',
    'cabinet',
]

# ===== Active Directory (внутренний контур) =====
AD_ENABLED = _env_bool('AD_ENABLED', '0')
AD_SERVER = os.environ.get('AD_SERVER', 'dc.corp.local')
AD_DOMAIN = os.environ.get('AD_DOMAIN', 'CORP')
AD_USER_BASE = os.environ.get('AD_USER_BASE', 'ou=users,dc=corp,dc=local')
AD_MAIL_DOMAIN = os.environ.get('AD_MAIL_DOMAIN', 'eag.su')

AD_GROUP_MAP = {
    'TT-Admins': 'admin',
    'TT-Managers': 'manager',
    'TT-Staff': 'staff',
}

ALLOWED_EMAIL_DOMAINS = [
    d.strip() for d in os.environ.get('ALLOWED_EMAIL_DOMAINS', 'eag.su').split(',')
    if d.strip()
]

AUTHENTICATION_BACKENDS = [
    'accounts.ad_backend.ADBackend',
    'django.contrib.auth.backends.ModelBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',                 # ← статика из контейнера
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.DismissedUserLogoutMiddleware',          # ← новая
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'admin_panel.middleware.ActivityMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'admin_panel.context_processors.admin_alerts',
                'admin_panel.context_processors.shift_requests_badge',
                'accounts.context_processors.tours',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# В Docker БД лежит на volume — путь задаём через env.
# Локально (без DJANGO_DB_PATH) — прежнее поведение, файл рядом с manage.py.
DB_PATH = os.environ.get('DJANGO_DB_PATH') or str(BASE_DIR / 'db.sqlite3')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DB_PATH,
    }
}

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'collected_static'

# Django ≥ 5.1: STATICFILES_STORAGE убран, используем STORAGES.
# CompressedStaticFilesStorage (без манифеста) — безопаснее:
# опечатка в {% static %} не уронит прод 500-кой.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# Регистрация по умолчанию выключена.
# Включить: REGISTRATION_OPEN=1 в переменных окружения.
REGISTRATION_OPEN = _env_bool('REGISTRATION_OPEN', '0')

EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = os.environ.get('SMTP_HOST', 'smtp.eag.su')
EMAIL_PORT = _env_int('SMTP_PORT', 587)
EMAIL_USE_TLS = _env_bool('SMTP_TLS', '1')
EMAIL_HOST_USER = os.environ.get('SMTP_USER', 'noreply@eag.su')
EMAIL_HOST_PASSWORD = os.environ.get('SMTP_PASS', '')
DEFAULT_FROM_EMAIL = 'Task-tracker <noreply@eag.su>'

SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', 'admin@eag.su')

MEDIA_URL = '/media/'
# В Docker media лежит на volume. Локально — прежнее поведение.
MEDIA_ROOT = os.environ.get('DJANGO_MEDIA_ROOT') or str(BASE_DIR / 'media')

# 10 МБ — лимит вложений в UI. Даём небольшой запас для multipart-обёртки.
FILE_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

# ===== Безопасность в продакшене =====
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

    # HSTS: включай ТОЛЬКО если портал реально работает по HTTPS
    # и у тебя есть валидный сертификат. Иначе браузер закеширует правило
    # на год и будет насильно редиректить http→https.
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

    # За nginx'ом / reverse-proxy с TLS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# В тестах не ходим в AD.
import sys
if 'test' in sys.argv:
    AD_ENABLED = False
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
