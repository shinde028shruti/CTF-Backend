from django.urls import path

from .views import (
    AdminAchievementDetailView,
    AdminAchievementListCreateView,
)

app_name = 'achievements_admin'

urlpatterns = [
    path(
        'achievements/',
        AdminAchievementListCreateView.as_view(),
        name='achievement-list',
    ),
    path(
        'achievements/<int:pk>/',
        AdminAchievementDetailView.as_view(),
        name='achievement-detail',
    ),
]
