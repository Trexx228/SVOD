from django.urls import path

from . import views

urlpatterns = [
    path('', views.motivation, name='motivation'),
    path('rating/', views.rating, name='rating'),
]
