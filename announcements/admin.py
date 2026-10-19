"""Django admin registration for announcements (browsable only).

The JSON admin API (``/api/v1/admin/announcements/``) is the management surface;
this registration exists so rows can be inspected from Django admin without
opening a second, divergent write path.
"""

from django.contrib import admin

from .models import Announcement


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'title', 'priority', 'status', 'published_at',
        'starts_at', 'ends_at', 'created_by',
    )
    list_filter = ('status', 'priority')
    search_fields = ('title', 'body')
    readonly_fields = (
        'title', 'body', 'priority', 'status', 'created_by',
        'created_at', 'updated_at', 'published_at', 'starts_at', 'ends_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
