"""Serializers for the announcement API.

Two read shapes exist because the public and administrative surfaces must not
leak into each other:

* :class:`AnnouncementPublicSerializer` is the closed wire shape shown to
  everyone. Administrative-only state (``status``, ``created_by``,
  ``updated_at``) is never included.
* :class:`AnnouncementAdminSerializer` is the full representation an
  administrator may see.

:class:`AnnouncementWriteSerializer` is the only writable serializer. It cannot
accept ``created_by`` - the view derives the creator from the authenticated
administrator - and it re-validates the scheduling window even on a partial
``PATCH``.
"""

from rest_framework import serializers

from .models import Announcement

# Bounds that are not encoded in the column itself (a TextField has no length).
MAX_BODY_LENGTH = 2000


class AnnouncementPublicSerializer(serializers.ModelSerializer):
    """The active announcement shown to every visitor.

    The field list is explicit and closed. ``status`` is omitted because the
    public queryset already guarantees ``published``, and ``created_by`` /
    ``updated_at`` are administrative-only.
    """

    class Meta:
        model = Announcement
        fields = (
            'id',
            'title',
            'body',
            'priority',
            'starts_at',
            'ends_at',
            'published_at',
        )
        read_only_fields = fields


class AnnouncementAdminSerializer(serializers.ModelSerializer):
    """Full administrative representation, including publication state."""

    created_by_username = serializers.SerializerMethodField()

    class Meta:
        model = Announcement
        fields = (
            'id',
            'title',
            'body',
            'priority',
            'status',
            'created_by',
            'created_by_username',
            'created_at',
            'updated_at',
            'published_at',
            'starts_at',
            'ends_at',
        )
        read_only_fields = fields

    def get_created_by_username(self, obj):
        return obj.created_by.username if obj.created_by_id else None


class AnnouncementWriteSerializer(AnnouncementAdminSerializer):
    """Create/update serializer.

    Writable fields are ``title``, ``body``, ``priority``, ``status``,
    ``starts_at`` and ``ends_at``. Everything else stays read-only, so a client
    can never forge the creator or the publication timestamp.
    """

    class Meta(AnnouncementAdminSerializer.Meta):
        read_only_fields = (
            'id',
            'created_by',
            'created_by_username',
            'created_at',
            'updated_at',
            'published_at',
        )

    def validate_title(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A title is required.')
        return value

    def validate_body(self, value):
        value = (value or '').strip()
        if len(value) > MAX_BODY_LENGTH:
            raise serializers.ValidationError(
                f'This message must be {MAX_BODY_LENGTH} characters or fewer.'
            )
        return value

    def validate(self, attrs):
        # Merge with the stored values so a PATCH that changes only one end of
        # the window is still checked as a whole.
        starts = attrs.get(
            'starts_at', getattr(self.instance, 'starts_at', None)
        )
        ends = attrs.get('ends_at', getattr(self.instance, 'ends_at', None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError(
                {'ends_at': 'The end time must be later than the start time.'}
            )
        return attrs
