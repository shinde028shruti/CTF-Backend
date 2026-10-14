from django.urls import path

from .views import (
    PublicCategoryListView,
    PublicChallengeDetailView,
    PublicChallengeListView,
)

app_name = 'challenges_api'

urlpatterns = [
    path('challenges/', PublicChallengeListView.as_view(), name='challenge-list'),
    path(
        'challenges/<int:pk>/',
        PublicChallengeDetailView.as_view(),
        name='challenge-detail',
    ),
    path('categories/', PublicCategoryListView.as_view(), name='category-list'),
]
