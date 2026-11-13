from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/hello/', views.hello, name='hello'),
    path('api/health/', views.health, name='health'),
]
