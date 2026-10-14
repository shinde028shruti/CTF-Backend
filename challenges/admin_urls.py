from django.urls import path

from .views import (
    AdminChallengeAttachmentDetailView,
    AdminChallengeAttachmentView,
    AdminChallengeDetailView,
    AdminChallengeListCreateView,
    AdminChallengeStatusView,
)

app_name = 'challenges_admin'

urlpatterns = [
    path(
        'challenges/',
        AdminChallengeListCreateView.as_view(),
        name='challenge-list',
    ),
    path(
        'challenges/<int:pk>/',
        AdminChallengeDetailView.as_view(),
        name='challenge-detail',
    ),
    path(
        'challenges/<int:pk>/status/',
        AdminChallengeStatusView.as_view(),
        name='challenge-status',
    ),
    path(
        'challenges/<int:pk>/attachments/',
        AdminChallengeAttachmentView.as_view(),
        name='challenge-attachments',
    ),
    path(
        'challenges/<int:pk>/attachments/<int:attachment_pk>/',
        AdminChallengeAttachmentDetailView.as_view(),
        name='challenge-attachment-detail',
    ),
]
