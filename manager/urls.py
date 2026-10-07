from django.urls import path

from . import views

urlpatterns = [
    path('', views.manager_dashboard, name='manager_dashboard'),
    path('cabinet/', views.cabinet, name='manager_cabinet'),
    path('team/', views.team_page, name='manager_team'),
    path('workload/', views.workload_page, name='manager_workload'),
    path('load/', views.load_page, name='manager_load'),

    # ── Сверхурочные (вносит руководитель отдела) ──
    path('sessions/', views.sessions_list, name='manager_sessions'),
    path('sessions/summary/', views.sessions_summary, name='manager_sessions_summary'),

    path('overtime/', views.overtime_page, name='manager_overtime'),
    path('overtime.csv', views.overtime_csv, name='manager_overtime_csv'),
    path('overtime/create/', views.overtime_create, name='manager_overtime_create'),
    path('overtime/<int:pk>/edit/', views.overtime_edit, name='manager_overtime_edit'),
    path('overtime/<int:pk>/delete/', views.overtime_delete, name='manager_overtime_delete'),
    path('review/', views.review_page, name='manager_review'),
    path('flow/', views.flow_page, name='manager_flow'),
    path('analytics/', views.analytics_redirect, name='manager_analytics'),
    path('dept-dynamics/', views.dept_dynamics_redirect, name='manager_dept_dynamics'),
    path('dept-dynamics/capture/', views.capture_dept_snapshot, name='manager_dept_dynamics_capture'),
    path('dept-dynamics.csv', views.dept_dynamics_csv, name='manager_dept_dynamics_csv'),
    path('logs/', views.task_logs_page, name='manager_task_logs'),
    path('online/', views.online_page, name='manager_online'),
    path('reports/', views.reports_router, name='manager_reports'),

    path('review-old/', views.review, name='review'),
    path('reports-old/', views.reports, name='reports'),

    path('orders/', views.orders_page, name='manager_orders'),
    path('orders/timeline/', views.orders_timeline, name='manager_orders_timeline'),
    path('orders/timeline.csv', views.orders_timeline_csv, name='manager_orders_timeline_csv'),
    path('orders/<int:pk>/plan/', views.order_plan, name='order_plan'),
    path('orders/<int:pk>/stages/reorder/', views.order_stages_reorder, name='order_stages_reorder'),

    # ── Ветки и этапы заказа ──
    path('orders/<int:pk>/branch/add/', views.order_branch_add, name='order_branch_add'),
    path('orders/<int:pk>/branch/<int:branch_pk>/delete/', views.order_branch_delete, name='order_branch_delete'),
    path('orders/<int:pk>/branch/<int:branch_pk>/stages/add/', views.order_branch_stages_add, name='order_branch_stages_add'),
    path('stage/<int:pk>/move/<str:direction>/', views.stage_move, name='stage_move'),

    path('person/<int:pk>/', views.person_page, name='manager_person'),
    path('person/<int:pk>/vacation/set/', views.person_vacation_set, name='manager_person_vacation_set'),
    path('person/<int:pk>/vacation/clear/', views.person_vacation_clear, name='manager_person_vacation_clear'),

    path('reports.csv', views.reports_csv, name='manager_reports_csv'),

    path('plant/', views.plant_stats, name='manager_plant'),
    path('plant/live/', views.plant_live, name='manager_plant_live'),
    path('plant.csv', views.plant_csv, name='manager_plant_csv'),

    path('norms/', views.norms_page, name='manager_norms'),
    path('departments/hours/', views.department_hours_page, name='manager_department_hours'),

    # ── Графики работы (WorkSchedule) ──
    # ВАЖНО: 'new/' идёт до '<int:pk>/' — иначе 'new' не заматчится
    # как int и упадёт 404.
    path('schedules/', views.schedules_page, name='manager_schedules'),
    path('schedules/today/', views.schedule_today, name='manager_schedule_today'),
    path('schedules/new/', views.schedule_edit, name='manager_schedule_new'),
    path('schedules/<int:pk>/', views.schedule_detail, name='manager_schedule_detail'),
    path('schedules/<int:pk>/edit/', views.schedule_edit, name='manager_schedule_edit'),
    path('schedules/<int:pk>/delete/', views.schedule_delete, name='manager_schedule_delete'),
    path('schedules/<int:pk>/assign/', views.schedule_assign, name='manager_schedule_assign'),
    path('schedules/<int:pk>/unassign/', views.schedule_unassign, name='manager_schedule_unassign'),
    path('kudos/', views.kudos_send, name='kudos_send'),

    path('quarterly/', views.quarterly_redirect, name='quarterly_report'),
    path('quarterly.csv', views.quarterly_report_csv, name='quarterly_report_csv'),
    path('orders-report/', views.orders_report_redirect, name='manager_orders_report'),
    path('orders-report.csv', views.orders_report_csv, name='manager_orders_report_csv'),

    path('yearly/', views.yearly_redirect, name='yearly_report'),
    path('yearly.csv', views.yearly_report_csv, name='yearly_report_csv'),

    path('history/', views.historical_reports, name='historical_reports'),
    path('history/capture/', views.capture_timeline_snapshot, name='capture_timeline'),

    # ── Заявки на сдвиг плана ──
    path('shift-requests/', views.shift_requests_list, name='shift_requests_list'),
    path('shift-requests/<int:pk>/', views.shift_request_detail, name='shift_request_detail'),
    path('shift-requests/<int:pk>/approve/', views.shift_request_approve, name='shift_request_approve'),
    path('shift-requests/<int:pk>/reject/', views.shift_request_reject, name='shift_request_reject'),
    path('shift-requests/<int:pk>/squeeze/', views.shift_request_squeeze, name='shift_request_squeeze'),


    path('orders/<int:pk>/generate/', views.order_generate_from_template, name='order_generate_from_template'),
]
