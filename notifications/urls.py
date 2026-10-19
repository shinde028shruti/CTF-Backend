from django.urls import path

from .views import (
    MarkAllReadView,
    NotificationDetailView,
    NotificationListView,
    NotificationReadView,
    UnreadCountView,
)

app_name = 'notifications_api'

urlpatterns = [
    path('', NotificationListView.as_view(), name='list'),
    path('unread-count/', UnreadCountView.as_view(), name='unread-count'),
    path('mark-all-read/', MarkAllReadView.as_view(), name='mark-all-read'),
    path('<int:pk>/read/', NotificationReadView.as_view(), name='read'),
    path('<int:pk>/', NotificationDetailView.as_view(), name='detail'),
]
