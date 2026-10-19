"""Read serializer for notifications.

Notification rows are created only by server-side code (the reusable
:func:`notifications.services.notify` helper), so there is no write serializer:
every notification endpoint is either a read or a state change (read/unread),
and none of them accept client-supplied content.
"""

from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    """The wire shape of one notification, for its owner only.

    The field list is explicit and closed. ``recipient`` is never serialized, so
    a response cannot be used to discover which other account a row belongs to.
    """

    class Meta:
        model = Notification
        fields = (
            'id',
            'type',
            'title',
            'message',
            'destination_url',
            'is_read',
            'created_at',
            'read_at',
        )
        read_only_fields = fields
