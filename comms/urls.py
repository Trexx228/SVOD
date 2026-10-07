from django.urls import path

from . import views

urlpatterns = [
    path('', views.dialogs, name='dialogs'),
    path('poll/', views.poll, name='comms_poll'),
    path('upload/', views.upload_attach, name='upload_attach'),

    path('notifications/', views.notifications, name='notifications'),
    path('notifications/read/', views.notifications_read, name='notifications_read'),
    path('notifications/<int:pk>/open/', views.notification_open, name='notification_open'),

    path('direct/', views.start_direct, name='start_direct'),
    path('direct/<int:user_pk>/', views.start_direct, name='start_direct_user'),

    path('thread/<int:pk>/', views.thread_open, name='thread_open'),
    path('thread/<int:pk>/send/', views.thread_send, name='thread_send'),
    path('thread/<int:pk>/poll/', views.thread_poll, name='thread_poll'),

    path('task/<int:pk>/chat/', views.task_chat, name='task_chat'),
    path('task/<int:pk>/event/', views.task_event, name='task_event'),
    path('task/<int:pk>/stakeholders/', views.stakeholders_send, name='stakeholders_send'),

    path('people/', views.people_json, name='people_json'),
]
