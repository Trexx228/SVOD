from django.urls import path

from . import views

urlpatterns = [
    path('', views.me_home, name='me_home'),
    path('kpi/export.csv', views.me_kpi_export, name='me_kpi_export'),
    path('settings/', views.me_settings, name='me_settings'),
    path('tasks/', views.me_tasks, name='me_tasks'),
    path('sessions/', views.me_sessions, name='me_sessions'),
    path('kpi/', views.me_kpi, name='me_kpi'),
    path('logs/', views.me_logs, name='me_logs'),
    path('profile/', views.me_profile, name='me_profile'),
]
