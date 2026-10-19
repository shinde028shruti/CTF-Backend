from django.urls import path

from .views import (
    AdminAnnouncementDetailView,
    AdminAnnouncementListCreateView,
)

app_name = 'announcements_admin'

urlpatterns = [
    path(
        'announcements/',
        AdminAnnouncementListCreateView.as_view(),
        name='announcement-list',
    ),
    path(
        'announcements/<int:pk>/',
        AdminAnnouncementDetailView.as_view(),
        name='announcement-detail',
    ),
]
