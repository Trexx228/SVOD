#!/usr/bin/env python
"""Локальный валидатор .gitlab-ci.yml через GitLab CI Lint API.

Запуск вручную:
    python scripts/gitlab_ci_lint.py

Запускается pre-commit-хуком на изменение `.gitlab-ci.yml`.

Особенности:
  * При отсутствии сети или недоступности GitLab API — exit 0
    (не блокируем коммит, чтобы работа офлайн не страдала).
  * При невалидном YAML — exit 1 + печать ошибок от GitLab.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CI_FILE = BASE / '.gitlab-ci.yml'

if not CI_FILE.is_file():
    print(f'[gitlab-ci-lint] Файл не найден: {CI_FILE}')
    sys.exit(0)  # не блокируем, файла может не быть

content = CI_FILE.read_text(encoding='utf-8')
data = json.dumps({'content': content}).encode('utf-8')

req = urllib.request.Request(
    'https://gitlab.com/api/v4/ci/lint',
    data=data,
    headers={'Content-Type': 'application/json'},
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
    # Нет сети / GitLab лежит — не блокируем коммит
    print(f'[gitlab-ci-lint] Пропуск — API недоступен: {e}')
    sys.exit(0)

if payload.get('valid'):
    print('[gitlab-ci-lint] .gitlab-ci.yml валиден')
    sys.exit(0)

print('[gitlab-ci-lint] ❌ .gitlab-ci.yml невалиден:')
for err in payload.get('errors', []):
    print(f'  • {err}')
sys.exit(1)
