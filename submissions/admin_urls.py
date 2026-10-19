from django.urls import path

from .admin_views import AdminAnalyticsView

app_name = 'submissions_admin'

urlpatterns = [
    path('analytics/', AdminAnalyticsView.as_view(), name='analytics'),
]
