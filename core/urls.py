from django.urls import path

from . import views

urlpatterns = [
    path('api/videos/<uuid:video_id>/source/', views.video_source, name='video_source'),
    path('api/videos/<uuid:video_id>/', views.video_status, name='video_status'),
    path('api/videos/<uuid:video_id>/result/', views.video_result, name='video_result'),
    path('api/transform/', views.transform, name='transform'),
    path('', views.home, name='home'),
    path('api/hello/', views.hello, name='hello'),
    path('api/health/', views.health, name='health'),
]
