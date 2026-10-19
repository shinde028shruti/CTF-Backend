"""Persistent platform announcements.

An announcement is a single broadcast authored by an administrator. It carries a
publication ``status`` (draft/published) and an optional scheduling window
(``starts_at``/``ends_at``). Whether a row is *active* is decided exclusively by
the database query in :meth:`AnnouncementQuerySet.active` - never by the
browser - so a draft, a future or an expired announcement can never leak into the
public listing.

``created_by`` is a nullable audit reference to the administrator who authored
the row. The API derives it from the authenticated request and never accepts it
from a payload.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class AnnouncementQuerySet(models.QuerySet):
    """Query helpers for publication visibility."""

    def published(self):
        return self.filter(status=Announcement.STATUS_PUBLISHED)

    def active(self, now=None):
        """Published rows that are inside their (optional) scheduling window.

        ``starts_at``/``ends_at`` are inclusive/exclusive respectively: a row
        becomes active the moment ``starts_at`` is reached and stops being
        active once ``ends_at`` passes. ``now`` is injectable so boundary
        behaviour can be tested deterministically.
        """

        now = now or timezone.now()
        return self.published().filter(
            models.Q(starts_at__isnull=True) | models.Q(starts_at__lte=now),
            models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=now),
        )


class Announcement(models.Model):
    """A broadcast that can be drafted, scheduled, published and expired."""

    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PUBLISHED, 'Published'),
    ]

    # Presentation style only; the frontend maps each value to a colour/icon.
    PRIORITY_INFO = 'info'
    PRIORITY_UPDATE = 'update'
    PRIORITY_MAINTENANCE = 'maintenance'
    PRIORITY_URGENT = 'urgent'
    PRIORITY_CHOICES = [
        (PRIORITY_INFO, 'Info'),
        (PRIORITY_UPDATE, 'Update'),
        (PRIORITY_MAINTENANCE, 'Maintenance'),
        (PRIORITY_URGENT, 'Urgent'),
    ]

    title = models.CharField(max_length=120)
    body = models.TextField(blank=True, default='')
    priority = models.CharField(
        max_length=20,
        choices=PRIORITY_CHOICES,
        default=PRIORITY_INFO,
    )
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='announcements',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set the first time the row is published; kept as a historical marker when
    # it is later unpublished, so "newest published first" stays meaningful.
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    objects = AnnouncementQuerySet.as_manager()

    class Meta:
        # ``created_at`` is never null, so it orders identically on SQLite and
        # PostgreSQL (unlike a nullable ``published_at``).
        ordering = ['-created_at', '-id']
        indexes = [
            # The public active() lookup filters on all three columns.
            models.Index(fields=['status', 'starts_at', 'ends_at']),
            # Newest-published-first public listing.
            models.Index(fields=['-published_at']),
        ]
        constraints = [
            # Belt-and-braces alongside serializer/``clean`` validation: the
            # database itself refuses an inverted window.
            models.CheckConstraint(
                condition=(
                    models.Q(starts_at__isnull=True)
                    | models.Q(ends_at__isnull=True)
                    | models.Q(ends_at__gt=models.F('starts_at'))
                ),
                name='announcement_ends_after_starts',
            ),
        ]

    def __str__(self):
        return f'[{self.status}] {self.title}'

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError(
                {'ends_at': 'The end time must be later than the start time.'}
            )

    def save(self, *args, **kwargs):
        # Stamp the first publication. The historical value is not rewritten on
        # later edits. ``update_fields`` is widened so a targeted save cannot
        # drop the stamp.
        if self.status == self.STATUS_PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'published_at'}
        super().save(*args, **kwargs)
