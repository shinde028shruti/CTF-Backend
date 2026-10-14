"""Django admin registration for the challenge models.

This is separate from the JSON API (which lives in ``views.py``); it exists so
the models are browsable/maintainable from Django's own admin site. Flags are
managed through the API (or the seeder) because only a hash is stored - a plain
model form could not meaningfully edit one.
"""

from django.contrib import admin

from .models import Category, Challenge, ChallengeAttachment, ChallengeHint


class ChallengeHintInline(admin.TabularInline):
    model = ChallengeHint
    extra = 0


class ChallengeAttachmentInline(admin.TabularInline):
    model = ChallengeAttachment
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'order')
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'difficulty', 'points', 'status', 'solves')
    list_filter = ('status', 'difficulty', 'category', 'featured', 'trial')
    search_fields = ('title', 'author')
    inlines = [ChallengeHintInline, ChallengeAttachmentInline]
