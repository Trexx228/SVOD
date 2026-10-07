from django.urls import path

from . import views

urlpatterns = [
    path('', views.settings_dashboard, name='settings_home'),

    # ── Пользователи ──
    # Сначала статические пути — потом параметрические.
    path('users/', views.users_list, name='settings_users'),
    path('users/new/', views.user_new, name='settings_user_new'),
    path('users/bulk/', views.users_bulk, name='settings_users_bulk'),
    path('users/export/', views.users_export, name='settings_users_export'),
    path('users/import/', views.users_import, name='settings_users_import'),
    path('users/import/apply/', views.users_import_apply, name='settings_users_import_apply'),
    path('users/<int:pk>/', views.user_edit, name='settings_user_edit'),
    path('users/<int:pk>/detail/', views.user_detail, name='settings_user_detail'),
    path('users/<int:pk>/toggle/', views.user_toggle, name='settings_user_toggle'),

    # ── Роли ──
    path('roles/', views.roles_list, name='settings_roles'),
    path('roles/new/', views.role_edit, name='settings_role_new'),
    path('roles/<int:pk>/reassign/', views.role_reassign, name='settings_role_reassign'),
    path('roles/<int:pk>/', views.role_edit, name='settings_role_edit'),
    path('roles/<int:pk>/delete/', views.role_delete, name='settings_role_delete'),

    # ── Подразделения ──
    path('departments/', views.departments_list, name='settings_departments'),
    path('departments/new/', views.department_edit, name='settings_department_new'),
    path('departments/<int:pk>/', views.department_edit, name='settings_department_edit'),
    path('departments/<int:pk>/delete/', views.department_delete, name='settings_department_delete'),

    # ── Типовые задачи ──
    path('task-types/', views.task_types_list, name='settings_task_types'),
    path('task-types/new/', views.task_type_edit, name='settings_task_type_new'),
    path('task-types/<int:pk>/', views.task_type_edit, name='settings_task_type_edit'),
    path('task-types/<int:pk>/delete/', views.task_type_delete, name='settings_task_type_delete'),

    # ── Нормы часов ──
    path('norms/', views.norms_list, name='settings_norms'),
    path('norms/new/', views.norm_edit, name='settings_norm_new'),
    path('norms/<int:pk>/', views.norm_edit, name='settings_norm_edit'),
    path('norms/<int:pk>/delete/', views.norm_delete, name='settings_norm_delete'),

    # ── Заказы ──
    path('orders/', views.orders_list, name='settings_orders'),
    path('orders/new/', views.order_edit, name='settings_order_new'),
    path('orders/<int:pk>/', views.order_detail, name='settings_order_detail'),
    path('orders/<int:pk>/edit/', views.order_edit, name='settings_order_edit'),
    path('orders/<int:pk>/delete/', views.order_delete, name='settings_order_delete'),
    path('orders/<int:order_pk>/branches/new/', views.branch_edit, name='settings_branch_new'),
    path('orders/<int:order_pk>/branches/<int:pk>/', views.branch_edit, name='settings_branch_edit'),
    path('orders/<int:order_pk>/branches/<int:pk>/delete/', views.branch_delete, name='settings_branch_delete'),

    # ── Задачи ──
    path('tasks/', views.tasks_admin_list, name='settings_tasks_page'),
    path('tasks/bulk/', views.tasks_bulk, name='settings_tasks_bulk'),
    path('tasks/export/', views.tasks_export, name='settings_tasks_export'),
    path('problems/', views.problematic_tasks, name='settings_problems'),

    # ── Логи ──
    path('audit/', views.audit_list, name='settings_audit'),
    path('tasklogs/', views.task_logs_list, name='settings_task_logs'),
    path('logins/', views.logins_list, name='settings_logins'),
    path('online/', views.online_list, name='settings_online'),
    path('online/recompute/', views.online_recompute, name='settings_online_recompute'),

    # ── Сессии ──
    path('sessions/', views.sessions_list, name='settings_sessions'),
    path('sessions/export/', views.sessions_export, name='settings_sessions_export'),
    path('sessions/summary/', views.sessions_summary, name='settings_sessions_summary'),

    # ── KPI ──
    path('kpi/', views.kpi_report, name='settings_kpi'),
    path('analytics/departments/', views.analytics_departments, name='settings_analytics_departments'),

    # ── Уведомления ──
    path('alerts/', views.alerts_list, name='settings_alerts'),
    path('alerts/settings/', views.alert_settings_edit, name='settings_alert_settings'),
    path('alerts/settings/test/', views.alert_test_email, name='settings_alert_test_email'),
    path('alerts/seen-all/', views.alerts_mark_all_seen, name='settings_alerts_mark_all_seen'),
    path('alerts/<int:pk>/seen/', views.alerts_mark_seen, name='settings_alerts_mark_seen'),

    # ── Поддержка ──
    path('tickets/', views.tickets_list, name='settings_tickets'),
    path('tickets/<int:pk>/toggle/', views.ticket_toggle, name='settings_ticket_toggle'),
    path('broadcast/', views.broadcast_page, name='settings_broadcast'),

    # ── Разработка ──
    path('dev/', views.dev_home, name='settings_dev'),

    # ── Обслуживание ──
    path('health/', views.health_view, name='settings_health'),
    path('system/', views.system_info, name='settings_system'),
    path('system/cleanup/', views.system_cleanup, name='settings_system_cleanup'),
    path('maintenance/', views.maintenance_page, name='settings_maintenance'),
    path('integrity/', views.integrity_check, name='settings_integrity'),

    # ── Бэкапы ──
    path('backups/', views.backups_list, name='settings_backups'),
    path('backups/create/', views.backup_create, name='settings_backup_create'),
    path('backups/<str:name>/download/', views.backup_download, name='settings_backup_download'),
    path('backups/<str:name>/delete/', views.backup_delete, name='settings_backup_delete'),
    path('export/snapshot/', views.export_snapshot_zip, name='settings_export_snapshot'),

    # ── Поиск ──
    path('search/', views.global_search, name='settings_search'),

    path('onboarding-stats/', views.onboarding_stats, name='settings_onboarding_stats'),
    path('onboarding-stats/<int:pk>/reset/', views.onboarding_reset_user, name='settings_onboarding_reset'),
    path('users/<int:pk>/dismiss/',  views.user_dismiss,         name='settings_user_dismiss'),
    path('users/<int:pk>/restore/',  views.user_restore,         name='settings_user_restore'),
    path('users/<int:pk>/reassign/', views.user_reassign_tasks,  name='settings_user_reassign_tasks'),
]
