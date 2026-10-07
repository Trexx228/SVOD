"""Редиректы: мотивация и рейтинг переехали в личный кабинет.

Старые URL'ы /motivation/ и /rating/ сохранены для обратной совместимости
(внешние закладки, старые ссылки в чатах). Оба ведут на /me/?tab=motivation.

Логика переехала в cabinet.views._tab_motivation.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


@login_required
def motivation(request):
    return redirect('/me/?tab=motivation')


@login_required
def rating(request):
    return redirect('/me/?tab=motivation')
