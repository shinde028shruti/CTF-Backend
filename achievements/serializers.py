"""Serializers for the achievement API.

Three shapes:

* :class:`AchievementAdminSerializer` - the full administrative representation
  (criteria config, active flag, earned count). Read-only.
* :class:`AchievementWriteSerializer` - the only writable serializer. It cannot
  accept ``slug`` (derived server-side), ``deleted_at`` or ``earned_count``, and
  it validates the unlock rule through :func:`achievements.criteria.validate_config`.
* :class:`AchievementUserSerializer` - the closed player view. ``earned``,
  ``unlocked_at`` and ``progress`` are computed by the server from stored solve
  history; the request body can never influence them.
"""

from rest_framework import serializers

from .criteria import (
    CATEGORY_SOLVES,
    CHALLENGE_SOLVED,
    CRITERIA_TYPES,
    describe,
    progress,
    validate_config,
)
from .models import Achievement


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _CriteriaLabelMixin:
    """Shared ``criteria_label`` resolution.

    The view pre-batches the category/challenge names referenced by a *page* of
    definitions (so a listing is a constant number of queries). A freshly created
    or retrieved object may not be in that batch, because a serializer's context
    is built before the row is written on ``POST``; the missing name is therefore
    filled in lazily, one small query, only on a cache miss.
    """

    def _criteria_label(self, obj):
        from challenges.models import Category, Challenge

        categories = self.context.get('_category_name_cache')
        if categories is None:
            categories = dict(self.context.get('category_names') or {})
            self.context['_category_name_cache'] = categories
        challenges = self.context.get('_challenge_title_cache')
        if challenges is None:
            challenges = dict(self.context.get('challenge_titles') or {})
            self.context['_challenge_title_cache'] = challenges

        config = obj.criteria_config or {}
        if obj.criteria_type == CATEGORY_SOLVES:
            pk = _as_int(config.get('category'))
            if pk is not None and pk not in categories:
                categories[pk] = (
                    Category.objects.filter(pk=pk)
                    .values_list('name', flat=True)
                    .first()
                )
        elif obj.criteria_type == CHALLENGE_SOLVED:
            pk = _as_int(config.get('challenge'))
            if pk is not None and pk not in challenges:
                challenges[pk] = (
                    Challenge.objects.filter(pk=pk)
                    .values_list('title', flat=True)
                    .first()
                )

        return describe(obj.criteria_type, config, categories, challenges)


class AchievementAdminSerializer(_CriteriaLabelMixin, serializers.ModelSerializer):
    """Full administrative representation of a definition."""

    criteria_label = serializers.SerializerMethodField()
    earned_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Achievement
        fields = (
            'id',
            'name',
            'slug',
            'description',
            'icon',
            'gradient',
            'rarity',
            'category',
            'badge_image',
            'reward',
            'criteria_type',
            'criteria_config',
            'criteria_label',
            'is_active',
            'sort_order',
            'earned_count',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_criteria_label(self, obj):
        return self._criteria_label(obj)


class AchievementWriteSerializer(AchievementAdminSerializer):
    """Create/update input for an administrator.

    Writable: ``name``, ``description``, badge presentation, ``reward``,
    ``criteria_type``/``criteria_config``, ``is_active`` and ``sort_order``.
    ``slug`` is always derived server-side; ``earned_count`` is not writable.
    """

    gradient = serializers.JSONField(required=False)

    class Meta(AchievementAdminSerializer.Meta):
        read_only_fields = (
            'id',
            'slug',
            'criteria_label',
            'earned_count',
            'created_at',
            'updated_at',
        )

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A name is required.')
        return value

    def validate_gradient(self, value):
        if value in (None, ''):
            return []
        if not isinstance(value, list) or len(value) != 2:
            raise serializers.ValidationError(
                'Provide exactly two colours, e.g. ["#4F1F71", "#7764E0"].'
            )
        for colour in value:
            if not isinstance(colour, str) or not colour.strip():
                raise serializers.ValidationError('Each colour must be a string.')
        return [colour.strip() for colour in value]

    def validate(self, attrs):
        instance = self.instance
        criteria_type = attrs.get(
            'criteria_type',
            instance.criteria_type if instance else Achievement.CRITERIA_FIRST_SOLVE,
        )
        raw_config = attrs.get(
            'criteria_config',
            instance.criteria_config if instance else {},
        )

        try:
            normalised = validate_config(criteria_type, raw_config)
        except serializers.ValidationError:
            raise
        except Exception as exc:  # django ValidationError
            raise serializers.ValidationError({'criteria_config': exc.messages})
        attrs['criteria_config'] = normalised

        # An earned achievement's rule is frozen: changing it could otherwise be
        # used to retroactively fabricate awards via the backfill command.
        if instance is not None:
            changed = criteria_type != instance.criteria_type or normalised != (
                instance.criteria_config or {}
            )
            if changed and instance.earned_by.exists():
                raise serializers.ValidationError(
                    {
                        'criteria_type': (
                            'This achievement has already been earned, so its '
                            'unlock rule cannot change. Deactivate it and create '
                            'a new achievement instead.'
                        )
                    }
                )
        return attrs


class AchievementUserSerializer(_CriteriaLabelMixin, serializers.ModelSerializer):
    """The player-facing view of one active achievement.

    ``earned``/``unlocked_at``/``progress`` come from serializer context built by
    the view from authoritative data, never from the client.
    """

    criteria_label = serializers.SerializerMethodField()
    earned = serializers.SerializerMethodField()
    unlocked_at = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Achievement
        fields = (
            'id',
            'slug',
            'name',
            'description',
            'icon',
            'gradient',
            'rarity',
            'category',
            'badge_image',
            'reward',
            'criteria_type',
            'criteria_label',
            'sort_order',
            'earned',
            'unlocked_at',
            'progress',
        )
        read_only_fields = fields

    def get_criteria_label(self, obj):
        return self._criteria_label(obj)

    def get_earned(self, obj):
        return obj.pk in self.context.get('earned_map', {})

    def get_unlocked_at(self, obj):
        unlocked = self.context.get('earned_map', {}).get(obj.pk)
        return unlocked.isoformat() if unlocked else None

    def get_progress(self, obj):
        gained, target = progress(
            obj.criteria_type,
            obj.criteria_config,
            self.context.get('solved_ids', set()),
            self.context.get('per_category', {}),
        )
        return {'gained': gained, 'target': target}


__all__ = [
    'AchievementAdminSerializer',
    'AchievementWriteSerializer',
    'AchievementUserSerializer',
    'CRITERIA_TYPES',
]
