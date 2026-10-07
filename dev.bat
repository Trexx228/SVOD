@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM ⚠️ Обязательно для разработки — иначе статика 404
set DJANGO_DEBUG=1
set DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,0.0.0.0

REM Планировщик в отдельном окне
start "SVOD Scheduler" cmd /k python manage.py run_scheduler

REM Основной сервер (передаём все аргументы, например runserver)
python manage.py %*
