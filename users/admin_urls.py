from django.urls import path

from .admin_views import (
    AdminGroupListView,
    AdminUserDetailView,
    AdminUserGroupsView,
    AdminUserListCreateView,
    AdminUserRestoreView,
    AdminUserStatusView,
)

app_name = 'admin_api'

urlpatterns = [
    path('users/', AdminUserListCreateView.as_view(), name='user-list'),
    path('users/<int:pk>/', AdminUserDetailView.as_view(), name='user-detail'),
    path(
        'users/<int:pk>/restore/',
        AdminUserRestoreView.as_view(),
        name='user-restore',
    ),
    path(
        'users/<int:pk>/status/',
        AdminUserStatusView.as_view(),
        name='user-status',
    ),
    path(
        'users/<int:pk>/groups/',
        AdminUserGroupsView.as_view(),
        name='user-groups',
    ),
    path('groups/', AdminGroupListView.as_view(), name='group-list'),
]
