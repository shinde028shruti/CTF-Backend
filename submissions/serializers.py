"""Request validation for the flag-submission endpoint, and the public
leaderboard row serializer."""

from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()

MAX_FLAG_LENGTH = 256


class FlagSubmissionSerializer(serializers.Serializer):
    """Validate the submit body: ``{"flag": "..."}``.

    Only the *shape* of the request is validated here. Whether the value is the
    right flag is decided against the stored hash in the view, so this
    serializer never sees or produces a stored hash.
    """

    flag = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=MAX_FLAG_LENGTH,
        error_messages={
            'required': 'A flag is required.',
            'blank': 'A flag is required.',
            'max_length': 'That flag is too long.',
        },
    )

    def validate_flag(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError('A flag is required.')
        return value.strip()


class LeaderboardEntrySerializer(serializers.ModelSerializer):
    """One public leaderboard row.

    Exposes only the fields the leaderboard UI needs. An explicit, closed field
    list means an email address, password hash, token, role, staff/superuser
    flag or onboarding answer can never be serialized here, regardless of how
    the queryset is built.

    ``rank`` is *not* a model field: the view injects it once the page offset is
    known, so it stays a true global rank rather than a per-page index.
    """

    points = serializers.IntegerField(source='points_value')
    solved = serializers.IntegerField(source='solved_count')
    accuracy = serializers.SerializerMethodField()
    meta = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'points', 'solved', 'accuracy', 'meta')

    def get_accuracy(self, obj):
        """Correct attempts as a whole percentage (0 when there are none)."""
        total = obj.submissions_total or 0
        if not total:
            return 0
        return round((obj.submissions_correct or 0) * 100 / total)

    def get_meta(self, obj):
        """The scoped category name, when the board is category-scoped."""
        return self.context.get('meta', '')

