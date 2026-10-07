#!/usr/bin/env sh
# Точка входа контейнера СВОД.
# 1. Миграции — всегда.
# 2. Планировщик — если RUN_SCHEDULER=1 (пилот: в одном контейнере с web).
# 3. exec "$@" — gunicorn в foreground, чтобы сигналы доходили корректно.

set -e

python manage.py migrate --noinput

if [ "${RUN_SCHEDULER:-0}" = "1" ]; then
    echo "[entrypoint] планировщик в фоне"
    python manage.py run_scheduler &
fi

exec "$@"
