"""Django admin registration for achievements (browsable only).

The JSON admin API (``/api/v1/admin/achievements/``) is the management surface;
this registration exists so definitions and awards can be inspected from Django
admin without opening a second, divergent write path. Definition deletion is a
soft delete through the API, so hard delete is disabled here too.
"""

from django.contrib import admin

from .models import Achievement, UserAchievement


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'name', 'criteria_type', 'is_active', 'sort_order',
        'rarity', 'reward', 'created_at',
    )
    list_filter = ('is_active', 'criteria_type', 'rarity')
    search_fields = ('name', 'slug', 'description')
    readonly_fields = (
        'name', 'slug', 'description', 'icon', 'gradient', 'rarity', 'category',
        'badge_image', 'criteria_type', 'criteria_config', 'reward', 'is_active',
        'sort_order', 'created_at', 'updated_at', 'deleted_at',
    )

    def get_queryset(self, request):
        return Achievement.all_objects.all()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserAchievement)
class UserAchievementAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'achievement', 'unlocked_at')
    list_filter = ('achievement',)
    search_fields = ('user__username', 'achievement__name')
    readonly_fields = ('user', 'achievement', 'unlocked_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
