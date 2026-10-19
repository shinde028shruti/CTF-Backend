from django.urls import path

from .views import PublicAnnouncementListView

app_name = 'announcements_api'

urlpatterns = [
    path('announcements/', PublicAnnouncementListView.as_view(), name='list'),
]
