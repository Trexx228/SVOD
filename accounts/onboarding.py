"""Маршрут онбординга: порядок туров, прогресс, следующий шаг."""

# (tour_id, url_name, url, заголовок, что показывает)
ONBOARDING_ROUTE = [
    ('dashboard',    'dashboard',   '/',                'Мои задачи',
     'Таймер, фокусная задача, действия'),
    ('task_create',  'task_create', '/tasks/create/',   'Постановка задачи',
     'Заказ, срок, исполнитель, оценка времени'),
    ('task_detail',  'task_tree',   '/tasks/tree/',     'Дерево и карточка задачи',
     'Как устроена задача внутри и её декомпозиция'),
    ('me_kpi',       'me_kpi',      '/me/kpi/',         'Мой KPI',
     'Как читать план, факт и эффективность'),
    ('motivation',   'motivation',  '/motivation/',     'Мотивация',
     'Уровни, серии, достижения, благодарности'),
    ('dialogs',      'dialogs',     '/comms/',          'Чаты',
     'Личные и по задачам, вложения'),
]


def get_progress(user):
    """Возвращает (step_index, total, next_step, is_done).

    Идём строго по порядку: как только находим первый непройденный —
    останавливаемся, он и есть следующий.
    """
    seen = user.tour_seen or {}

    done = 0
    for tour_id, *_ in ONBOARDING_ROUTE:
        if tour_id in seen:
            done += 1
        else:
            break

    total = len(ONBOARDING_ROUTE)

    if done >= total:
        return done, total, None, True

    step = ONBOARDING_ROUTE[done]
    next_step = {
        'index': done + 1,
        'tour_id': step[0],
        'url_name': step[1],
        'url': step[2],
        'title': step[3],
        'hint': step[4],
    }
    return done, total, next_step, False


def should_show_offer(user):
    """Показывать ли карточку «Пройти обучение»."""
    if user.onboarding_done:
        return False
    done, total, _next, is_done = get_progress(user)
    if is_done:
        return False
    return done < total
