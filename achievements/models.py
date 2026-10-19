"""Achievement definitions and the persistent record of who earned them.

Two tables, deliberately separate:

* :class:`Achievement` is an administrator-authored *definition* - a name, badge
  presentation and a small, validated unlock rule (``criteria_type`` +
  ``criteria_config``). It never carries a per-user verdict.
* :class:`UserAchievement` is the *award*: one row per (user, achievement),
  enforced by a database unique constraint, written only by server-side
  evaluation after a real solve. An achievement is never considered earned
  merely because the frontend displays it.

Deletion is a **soft delete** (mirroring ``users.models``): a deleted definition
keeps its row, its slug and every earned record, but disappears from the admin
list and from the player-facing grid. The API further protects earned
definitions: one that has any award cannot be deleted at all (the admin must
deactivate it instead). Nothing is ever hard-deleted through the API, so
historical awards can never be destroyed.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from challenges.models import unique_slug


class AchievementQuerySet(models.QuerySet):
    """Query helpers for definition visibility."""

    def active(self):
        """Definitions that are switched on and not soft-deleted."""
        return self.filter(is_active=True, deleted_at__isnull=True)

    def delete(self):
        """Bulk soft delete: hide the rows instead of removing them."""
        count = self.filter(deleted_at__isnull=True).update(
            deleted_at=timezone.now(),
            is_active=False,
        )
        return (count, {'achievements.Achievement': count})


class ActiveAchievementManager(models.Manager.from_queryset(AchievementQuerySet)):
    """Default manager: only definitions that have not been soft-deleted."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllAchievementManager(models.Manager.from_queryset(AchievementQuerySet)):
    """Manager that also exposes soft-deleted definitions."""


class Achievement(models.Model):
    """An administrator-authored, database-backed achievement definition."""

    # Supported unlock rules. The evaluator dispatches on these values only -
    # never on an achievement's name - so a newly created achievement works
    # without a code change as long as its type is one of these.
    CRITERIA_FIRST_SOLVE = 'first_solve'
    CRITERIA_TOTAL_SOLVES = 'total_solves'
    CRITERIA_CATEGORY_SOLVES = 'category_solves'
    CRITERIA_CHALLENGE_SOLVED = 'challenge_solved'
    CRITERIA_CHOICES = [
        (CRITERIA_FIRST_SOLVE, 'Solve the first challenge'),
        (CRITERIA_TOTAL_SOLVES, 'Solve a number of challenges'),
        (CRITERIA_CATEGORY_SOLVES, 'Solve challenges in a category'),
        (CRITERIA_CHALLENGE_SOLVED, 'Solve a specific challenge'),
    ]

    RARITY_CHOICES = [
        ('Common', 'Common'),
        ('Uncommon', 'Uncommon'),
        ('Rare', 'Rare'),
        ('Epic', 'Epic'),
        ('Legendary', 'Legendary'),
    ]

    name = models.CharField(max_length=120)
    # Human-friendly stable identifier. Auto-generated from ``name`` and kept
    # unique; the frontend addresses achievements by numeric id.
    slug = models.SlugField(max_length=140, unique=True, blank=True)

    description = models.TextField(blank=True, default='')

    # Badge presentation. The existing ``AchievementBadge`` component renders an
    # icon over a two-stop gradient, and prefers ``badge_image`` when set.
    icon = models.CharField(max_length=60, blank=True, default='Trophy')
    gradient = models.JSONField(default=list, blank=True)
    rarity = models.CharField(
        max_length=20, choices=RARITY_CHOICES, default='Common'
    )
    # Presentation grouping shown as a chip on the card (e.g. "General").
    category = models.CharField(max_length=100, blank=True, default='General')
    badge_image = models.CharField(max_length=300, blank=True, default='')

    # The unlock rule. ``criteria_config`` is validated JSON; its shape depends
    # on ``criteria_type`` (see ``criteria.py``).
    criteria_type = models.CharField(
        max_length=30, choices=CRITERIA_CHOICES, default=CRITERIA_FIRST_SOLVE
    )
    criteria_config = models.JSONField(default=dict, blank=True)

    # Display-only bonus points. Deliberately not credited to ``User.points``:
    # that balance feeds the leaderboard, and changing scoring is out of scope.
    reward = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ActiveAchievementManager()
    all_objects = AllAchievementManager()

    class Meta:
        ordering = ['sort_order', 'id']
        indexes = [
            # The evaluator's "active definitions" lookup.
            models.Index(fields=['is_active', 'deleted_at']),
        ]
        # FK traversal / fixtures must see every row, deleted or not, so the
        # filtered default manager is only ever the application view.
        base_manager_name = 'all_objects'
        verbose_name = 'achievement'
        verbose_name_plural = 'achievements'

    def __str__(self):
        return f'{self.name} ({self.criteria_type})'

    def save(self, *args, **kwargs):
        if not self.slug:
            # Include soft-deleted rows: they still hold their slug at the
            # database level, so a name reused after a delete must pick the
            # next free suffix instead of colliding.
            self.slug = unique_slug(
                Achievement,
                self.name or 'achievement',
                self.pk,
                queryset=Achievement.all_objects.all(),
            )
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        """Soft delete: hide the definition instead of removing the row."""
        if self.pk is not None and self.deleted_at is None:
            self.deleted_at = timezone.now()
            self.is_active = False
            self.save(update_fields=['deleted_at', 'is_active'], using=using)
        return (1, {'achievements.Achievement': 1})

    def hard_delete(self, using=None, keep_parents=False):
        """Permanently remove the row; internal/admin use only."""
        return super().delete(using=using, keep_parents=keep_parents)

    def clean(self):
        """Validate the unlock rule as a whole (used by the serializer too)."""
        from django.core.exceptions import ValidationError

        from .criteria import validate_config

        try:
            self.criteria_config = validate_config(
                self.criteria_type, self.criteria_config
            )
        except ValidationError as exc:
            raise ValidationError({'criteria_config': exc.messages})


class UserAchievement(models.Model):
    """One award: ``user`` earned ``achievement`` at ``unlocked_at``.

    The ``unique_achievement_per_user`` constraint makes the award idempotent at
    the database level, so a repeated or concurrent evaluation can never create
    a second row.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='achievements',
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='earned_by',
    )
    unlocked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-unlocked_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'achievement'],
                name='unique_achievement_per_user',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-unlocked_at']),
        ]

    def __str__(self):
        return f'{self.user_id} earned achievement {self.achievement_id}'
