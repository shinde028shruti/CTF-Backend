from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


class DeletedFilter(admin.SimpleListFilter):
    """Toggles between live and soft-deleted accounts in Django admin."""

    title = 'deleted'
    parameter_name = 'deleted'

    def lookups(self, request, model_admin):
        return (
            ('del', 'Deleted'),
            ('notdel', 'Not deleted'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'del':
            return queryset.filter(deleted_at__isnull=False)
        if self.value() == 'notdel':
            return queryset.filter(deleted_at__isnull=True)
        return queryset


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('CTF', {'fields': ('points', 'bio', 'avatar')}),
        ('Application role', {'fields': ('role',)}),
        ('Account status', {'fields': ('status', 'status_note')}),
    )
    list_display = UserAdmin.list_display + ('role', 'status')
    list_filter = UserAdmin.list_filter + ('role', 'status', DeletedFilter)

    def get_queryset(self, request):
        # ModelAdmin defaults to the filtered manager; use all_objects so the
        # DeletedFilter can surface soft-deleted accounts.
        return self.model.all_objects.all()

    def changelist_view(self, request, extra_context=None):
        # Default the list to live accounts so deleted ones stay out of the
        # way until explicitly requested.
        if 'deleted' not in request.GET:
            query = request.GET.copy()
            query['deleted'] = 'notdel'
            request.GET = query
        return super().changelist_view(request, extra_context=extra_context)