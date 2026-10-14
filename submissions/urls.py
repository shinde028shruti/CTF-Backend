from django.urls import path

from .views import (
    FlagSubmitView,
    HintRevealView,
    LeaderboardMeView,
    LeaderboardView,
)

app_name = 'submissions_api'

urlpatterns = [
    path(
        'challenges/<int:pk>/submit/',
        FlagSubmitView.as_view(),
        name='flag-submit',
    ),
    path(
        'challenges/<int:pk>/hints/<int:hint_pk>/reveal/',
        HintRevealView.as_view(),
        name='hint-reveal',
    ),
    path('leaderboard/', LeaderboardView.as_view(), name='leaderboard'),
    path('leaderboard/me/', LeaderboardMeView.as_view(), name='leaderboard-me'),
]
