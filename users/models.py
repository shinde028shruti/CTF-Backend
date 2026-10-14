from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet that turns a bulk ``.delete()`` into a soft delete."""

    def delete(self):
        count = self.filter(deleted_at__isnull=True).update(
            deleted_at=timezone.now(),
            is_active=False,
        )
        return (count, {'users.User': count})


class ActiveUserManager(UserManager):
    """Default manager: only accounts that have not been soft-deleted."""

    def get_queryset(self):
        return (
            SoftDeleteQuerySet(self.model, using=self._db)
            .filter(deleted_at__isnull=True)
        )


class AllUserManager(UserManager):
    """Manager that also exposes soft-deleted accounts."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)


class User(AbstractUser):
    points = models.PositiveIntegerField(default=0)
    bio = models.TextField(blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)

    # Consumed by the frontend to gate routes and render the admin panel.
    # Django's built-in `is_staff` is deliberately not reused so the API
    # exposes a single, stable role vocabulary.
    ROLE_USER = 'user'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_USER, 'User'),
        (ROLE_ADMIN, 'Admin'),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_USER,
    )

    # Administrative account state shown in the Admin -> Users panel. Kept in
    # lockstep with Django's `is_active` login gate: only STATUS_ACTIVE may log
    # in, suspended/banned may not.
    STATUS_ACTIVE = 'active'
    STATUS_SUSPENDED = 'suspended'
    STATUS_BANNED = 'banned'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_SUSPENDED, 'Suspended'),
        (STATUS_BANNED, 'Banned'),
    ]

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    status_note = models.CharField(max_length=255, blank=True, default='')

    # Soft-delete marker. Rows with a value are hidden from the default
    # manager (and therefore from login, JWT auth, lists and detail) but stay
    # in the database so they can be restored. Their username/email remain
    # reserved (the columns are UNIQUE), so nothing silently collides.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ActiveUserManager()
    all_objects = AllUserManager()

    class Meta(AbstractUser.Meta):
        abstract = False
        # Django uses this as the base manager for FK traversal and fixtures;
        # it must see every row, deleted or not, so the filtered default
        # manager is only ever the *application* view.
        base_manager_name = 'all_objects'

    def delete(self, using=None, keep_parents=False):
        """Soft delete: hide the account instead of removing the row."""
        if self.pk is not None and self.deleted_at is None:
            self.deleted_at = timezone.now()
            self.is_active = False
            self.save(update_fields=['deleted_at', 'is_active'], using=using)
        return (1, {'users.User': 1})

    def hard_delete(self, using=None, keep_parents=False):
        """Permanently remove the row; only for internal/admin use."""
        return super().delete(using=using, keep_parents=keep_parents)

    # The React app redirects to /onboarding while this is false. New accounts
    # therefore start incomplete and are walked through the questionnaire; the
    # flag is flipped by POST /api/v1/auth/onboarding/ once they finish, which
    # is what stops them being asked again on the next session.
    onboardingCompleted = models.BooleanField(default=False)

    # The questionnaire answers (skillLevel, teamInterest, focusAreas, purpose,
    # experienceType, userType). Stored as JSON because the shape is defined by
    # the frontend flow and Dashboard/Challenges read focusAreas from it.
    onboarding = models.JSONField(default=dict, blank=True)

    @property
    def is_admin(self):
        # App-admin is driven by the stored role or by Django superuser status.
        # Group membership deliberately plays no part: `role` and groups are
        # independent, and groups govern Django-side authorization instead.
        return self.role == self.ROLE_ADMIN or self.is_superuser