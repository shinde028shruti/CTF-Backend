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


class SubmissionsSummarySerializer(serializers.Serializer):
    """Correct vs total flag attempts, used to derive accuracy."""

    total = serializers.IntegerField()
    correct = serializers.IntegerField()


class CategoryProgressSerializer(serializers.Serializer):
    """One category's published total and the caller's own progress in it."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    color = serializers.CharField(allow_blank=True)
    solved = serializers.IntegerField()
    total = serializers.IntegerField()
    points = serializers.IntegerField()


class ActivityEntrySerializer(serializers.Serializer):
    """One solve or hint-unlock event in the caller's recent history.

    ``points`` is the gross reward for a solve, ``cost`` the amount spent on a
    hint; exactly one of them is non-zero per entry.
    """

    type = serializers.CharField()
    challengeId = serializers.IntegerField()
    challengeName = serializers.CharField()
    points = serializers.IntegerField()
    cost = serializers.IntegerField()
    timestamp = serializers.DateTimeField()


class ProgressPointSerializer(serializers.Serializer):
    label = serializers.CharField()
    points = serializers.IntegerField()


class ProgressSerializer(serializers.Serializer):
    weekly = ProgressPointSerializer(many=True)
    monthly = ProgressPointSerializer(many=True)


class ProgressionSerializer(serializers.Serializer):
    """The caller's derived level / tier / Elite progression.

    A pure projection of the caller's ``earnedPoints`` (see ``progression.py``);
    it involves no other user's data and no private account field.
    """

    level = serializers.IntegerField()
    tier = serializers.CharField()
    points = serializers.IntegerField()
    levelMin = serializers.IntegerField()
    nextLevelMin = serializers.IntegerField(allow_null=True)
    toNext = serializers.IntegerField()
    progressPct = serializers.IntegerField()
    elitePct = serializers.IntegerField()
    isMax = serializers.BooleanField()


class UserStatsSerializer(serializers.Serializer):
    """The caller's own private statistics.

    The field list is explicit and closed, so no other user's data and no
    private account field (email, password hash, tokens, staff/superuser flags)
    can ever be serialized here. ``rank`` is ``null`` when the caller has not
    solved anything yet; ``joinDate`` is the account's ``date_joined``.
    """

    points = serializers.IntegerField()
    earnedPoints = serializers.IntegerField()
    spentOnHints = serializers.IntegerField()
    solved = serializers.IntegerField()
    accuracy = serializers.IntegerField()
    submissions = SubmissionsSummarySerializer()
    rank = serializers.IntegerField(allow_null=True)
    joinDate = serializers.DateTimeField(allow_null=True)
    solvedChallengeIds = serializers.ListField(child=serializers.IntegerField())
    categoryProgress = CategoryProgressSerializer(many=True)
    recentActivity = ActivityEntrySerializer(many=True)
    progress = ProgressSerializer()
    progression = ProgressionSerializer()

