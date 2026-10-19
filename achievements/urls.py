from django.urls import path

from .views import UserAchievementListView

app_name = 'achievements_api'

urlpatterns = [
    path('achievements/', UserAchievementListView.as_view(), name='list'),
]
