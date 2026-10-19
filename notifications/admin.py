"""Django admin registration for notifications (read-only).

Notifications are produced by the application, never hand-authored, so this
registration is browsable only. It is not a management surface: the JSON API
deliberately exposes no admin/notification-management endpoints.
"""

from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipient', 'type', 'title', 'is_read', 'created_at')
    list_filter = ('type', 'is_read')
    search_fields = ('title', 'message', 'recipient__username')
    readonly_fields = (
        'recipient', 'type', 'title', 'message', 'destination_url',
        'is_read', 'created_at', 'read_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
