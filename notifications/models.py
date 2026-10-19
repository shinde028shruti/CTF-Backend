"""Persistent, per-recipient in-app notifications.

A notification always belongs to exactly one recipient (a foreign key to the
project's custom user model). Every API query is scoped to ``request.user``, so
one account can never read, mark or delete another account's rows.

Only what the UI needs is stored. ``type`` is a small, closed vocabulary the
client maps to an icon/badge rather than interpreting free text.
``destination_url`` is restricted to same-application paths
(:func:`validate_destination_url`): the value is rendered by the client and
must never be able to become an external redirect or a ``javascript:`` URL.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def validate_destination_url(value):
    """Allow only a same-application path (or blank).

    A safe destination starts with a single ``/`` (so ``//host`` and any
    ``scheme:`` value are rejected) and contains no backslash (which some
    browsers treat as a path separator and normalise unexpectedly).
    """
    if not value:
        return
    if not value.startswith('/') or value.startswith('//'):
        raise ValidationError(
            'Destination must be an internal application path starting with "/".'
        )
    if '\\' in value:
        raise ValidationError('Destination must not contain a backslash.')


class Notification(models.Model):
    """One in-app notification for one recipient."""

    TYPE_SYSTEM = 'system'
    TYPE_CHALLENGE = 'challenge'
    TYPE_ACHIEVEMENT = 'achievement'
    TYPE_ANNOUNCEMENT = 'announcement'
    TYPE_CHOICES = [
        (TYPE_SYSTEM, 'System'),
        (TYPE_CHALLENGE, 'Challenge'),
        (TYPE_ACHIEVEMENT, 'Achievement'),
        (TYPE_ANNOUNCEMENT, 'Announcement'),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=TYPE_SYSTEM,
        db_index=True,
    )
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=500)
    # Optional same-app route (e.g. "/challenges/12"); blank when there is
    # nowhere sensible to send the reader.
    destination_url = models.CharField(
        max_length=200,
        blank=True,
        default='',
        validators=[validate_destination_url],
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            # Unread lookups and the unread badge.
            models.Index(fields=['recipient', 'is_read']),
            # Newest-first listing per recipient.
            models.Index(fields=['recipient', '-created_at']),
        ]

    def __str__(self):
        state = 'read' if self.is_read else 'unread'
        return f'{state} {self.type} notification for {self.recipient_id}: {self.title}'

    def mark_read(self):
        """Mark read idempotently, stamping ``read_at`` only on the transition.

        Returns ``True`` when this call changed the row.
        """
        if self.is_read:
            return False

        self.is_read = True
        self.read_at = timezone.now()
        self.save(update_fields=['is_read', 'read_at'])
        return True
