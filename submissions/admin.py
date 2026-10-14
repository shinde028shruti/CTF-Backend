"""Django admin registration for submission/solve/hint-unlock records.

Read-only: rows are created by the flag-submission and hint-reveal flows, never
by hand, and their point values are historical facts. There is no plaintext
flag stored here to edit.
"""

from django.contrib import admin

from .models import HintUnlock, Solve, Submission


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'challenge', 'correct', 'created_at')
    list_filter = ('correct', 'created_at')
    search_fields = ('user__username', 'challenge__title')
    readonly_fields = ('user', 'challenge', 'correct', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(Solve)
class SolveAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'challenge', 'points', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'challenge__title')
    readonly_fields = ('user', 'challenge', 'points', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(HintUnlock)
class HintUnlockAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'challenge', 'hint', 'cost', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'challenge__title')
    readonly_fields = ('user', 'challenge', 'hint', 'cost', 'created_at')

    def has_add_permission(self, request):
        return False
