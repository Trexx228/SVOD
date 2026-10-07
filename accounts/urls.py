from django.urls import include, path

from . import views

urlpatterns = [
    path('register/', views.register, name='register'),
    path('activate/<str:token>/', views.activate, name='activate'),
    path('resend/', views.resend, name='resend'),
    path('profile/', views.profile, name='profile'),
    path('profile/save/', views.profile_save, name='profile_save'),
    path('support/inbox/', views.support_inbox, name='support_inbox'),
    path('support/', views.support_send, name='support_send'),
    path('', include('django.contrib.auth.urls')),

    # ── Обучалка ──
    path('tour/<slug:tour_id>/seen/', views.tour_mark_seen, name='tour_mark_seen'),
    path('tour/reset/',               views.tour_reset,     name='tour_reset'),
    path('hints/seen/', views.hints_mark_seen, name='hints_mark_seen'),

    # ── Онбординг ──
    path('onboarding/start/',  views.onboarding_start,  name='onboarding_start'),
    path('onboarding/next/',   views.onboarding_next,   name='onboarding_next'),
    path('onboarding/finish/', views.onboarding_finish, name='onboarding_finish'),
    path('onboarding/skip/',   views.onboarding_skip,   name='onboarding_skip'),
]
