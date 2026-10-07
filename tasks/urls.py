from django.urls import path

from . import views

urlpatterns = [
    # Реестр: единая точка. По умолчанию view=tree.
    path('', views.tasks_registry, name='tasks_registry'),

    path('create/', views.task_create, name='task_create'),

    # Старые URL'ы реестра — редиректы на /tasks/?view=...
    # Имена сохранены: {% url 'task_tree' %} по-прежнему валиден.
    path('tree/', views.task_tree, name='task_tree'),
    path('timeline/', views.timeline, name='task_timeline'),
    path('calendar/', views.calendar_view, name='task_calendar'),

    # Экспорт — это файлы, не страницы. Оставляем как есть.
    path('tree/export/', views.tree_export, name='tree_export'),
    path('timeline.csv', views.timeline_export, name='timeline_export'),
    path('search/', views.search, name='search'),
    path('task/<int:pk>/progress/', views.task_progress_add, name='task_progress_add'),
    path('task/<int:pk>/shift/', views.task_shift, name='task_shift'),
    path('task/<int:pk>/edit/', views.task_edit, name='task_edit'),
    path('task/<int:pk>/rework-add/', views.task_rework_create, name='task_rework_create'),
    path('task/<int:pk>/week/', views.week_toggle, name='week_toggle'),
    path('task/<int:pk>/<str:action>/', views.task_action, name='task_action'),
    path('task/<int:pk>/', views.task_detail, name='task_detail'),
    path('stage/<int:pk>/shift/<str:action>/', views.stage_shift_resolve, name='stage_shift_resolve'),
]
