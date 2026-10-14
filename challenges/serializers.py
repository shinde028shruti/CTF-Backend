"""Serializers for the challenge API.

There are three families:

* ``ChallengePublicSerializer`` - what an ordinary (or anonymous) user sees for
  a *published* challenge. It never contains a flag, a flag hash, a flag note,
  a hint's text, or any draft-only field.
* ``ChallengeAdminSerializer`` - the administrative view. It adds status,
  scheduling, ``has_flag`` and hint text, but still never exposes a flag.
* ``ChallengeWriteSerializer`` - create/update input. It accepts the plaintext
  flag and immediately hands it to ``ChallengeFlag.set_flag`` (which hashes it);
  the plaintext is never persisted or returned.
"""

import re

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from .models import (
    Category,
    Challenge,
    ChallengeAttachment,
    ChallengeFlag,
    ChallengeHint,
)

User = get_user_model()

FLAG_PATTERN = re.compile(r'^CTF\{.+\}$')


def human_size(num):
    """Format a byte count the way the frontend's file list expects."""
    num = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if num < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(num)} B'
            return f'{num:.1f} {unit}'
        num /= 1024


class CategorySerializer(serializers.ModelSerializer):
    count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ('id', 'name', 'slug', 'description', 'icon', 'color', 'order', 'count')

    def get_count(self, obj):
        annotated = getattr(obj, 'challenge_count', None)
        if annotated is not None:
            return annotated
        return obj.challenges.filter(status=Challenge.STATUS_PUBLISHED).count()


class PublicHintSerializer(serializers.ModelSerializer):
    """A hint as seen by players.

    Only metadata (``id``, ``order``, ``cost``) plus an unlock ``revealed`` flag
    is exposed. ``text`` is returned *only* for hints the requesting user has
    already unlocked (the reveal endpoint injects ``revealed_hint_ids``); an
    unrevealed hint always serialises ``text`` as an empty string.
    """

    revealed = serializers.SerializerMethodField()
    text = serializers.SerializerMethodField()

    class Meta:
        model = ChallengeHint
        fields = ('id', 'cost', 'order', 'revealed', 'text')

    def _revealed_ids(self):
        return self.context.get('revealed_hint_ids') or set()

    def get_revealed(self, obj):
        return obj.pk in self._revealed_ids()

    def get_text(self, obj):
        if obj.pk in self._revealed_ids():
            return obj.text
        return ''


class AdminHintSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChallengeHint
        fields = ('id', 'text', 'cost', 'order')


class AttachmentSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    size = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = ChallengeAttachment
        fields = ('id', 'name', 'size', 'type', 'url')

    def get_name(self, obj):
        return obj.display_name

    def get_size(self, obj):
        try:
            return human_size(obj.file.size)
        except (OSError, ValueError):
            return ''

    def get_type(self, obj):
        return obj.extension

    def get_url(self, obj):
        try:
            url = obj.file.url
        except ValueError:
            return ''
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request else url


class ChallengePublicSerializer(serializers.ModelSerializer):
    category = serializers.CharField(source='category.name', read_only=True)
    category_id = serializers.IntegerField(read_only=True)
    connectionInfo = serializers.JSONField(source='connection_info', read_only=True)
    createdAt = serializers.SerializerMethodField()
    solved = serializers.SerializerMethodField()
    hints = PublicHintSerializer(many=True, read_only=True)
    files = AttachmentSerializer(source='attachments', many=True, read_only=True)

    class Meta:
        model = Challenge
        fields = (
            'id', 'slug', 'title', 'category', 'category_id', 'difficulty',
            'points', 'description', 'scenario', 'objectives', 'instructions',
            'author', 'tags', 'featured', 'trial', 'solves', 'solved',
            'connectionInfo', 'cheatsheet', 'hints', 'files', 'createdAt',
            'status',
        )

    def get_createdAt(self, obj):
        return obj.created_at.date().isoformat() if obj.created_at else None

    def get_solved(self, obj):
        # The list/detail views inject the requesting user's solved challenge
        # ids as ``solved_ids`` to avoid a query per row. When absent (e.g. an
        # anonymous request) fall back to a single check.
        solved_ids = self.context.get('solved_ids')
        if solved_ids is not None:
            return obj.pk in solved_ids
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        from submissions.models import Solve
        return Solve.objects.filter(user=user, challenge_id=obj.pk).exists()


class ChallengeAdminSerializer(ChallengePublicSerializer):
    hints = AdminHintSerializer(many=True, read_only=True)
    has_flag = serializers.BooleanField(read_only=True)
    flag_note = serializers.SerializerMethodField()
    publishAt = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta(ChallengePublicSerializer.Meta):
        fields = ChallengePublicSerializer.Meta.fields + (
            'publishAt', 'has_flag', 'flag_note', 'created_by', 'updated_at',
        )

    def get_flag_note(self, obj):
        flag = getattr(obj, 'flag', None)
        return flag.note if flag else ''

    def get_publishAt(self, obj):
        return obj.publish_at.strftime('%Y-%m-%dT%H:%M') if obj.publish_at else ''

    def get_created_by(self, obj):
        return obj.created_by.username if obj.created_by else ''


class ChallengeHintWriteSerializer(serializers.Serializer):
    text = serializers.CharField()
    cost = serializers.IntegerField(min_value=0, required=False, default=0)


class ChallengeWriteSerializer(serializers.ModelSerializer):
    category = serializers.SlugRelatedField(
        slug_field='name',
        queryset=Category.objects.all(),
    )
    objectives = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    connectionInfo = serializers.JSONField(
        source='connection_info',
        required=False,
        allow_null=True,
    )
    publishAt = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
    )
    flag = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        trim_whitespace=True,
    )
    flag_note = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        max_length=255,
    )
    hints = ChallengeHintWriteSerializer(many=True, required=False, write_only=True)

    class Meta:
        model = Challenge
        fields = (
            'title', 'category', 'difficulty', 'points', 'description',
            'scenario', 'objectives', 'instructions', 'author', 'tags',
            'featured', 'trial', 'connectionInfo', 'cheatsheet', 'status',
            'publishAt', 'flag', 'flag_note', 'hints',
        )

    def validate(self, attrs):
        flag = attrs.get('flag')
        if self.instance is None and not (flag or '').strip():
            raise serializers.ValidationError(
                {'flag': 'A flag is required to create a challenge.'}
            )
        if flag and not FLAG_PATTERN.match(flag.strip()):
            raise serializers.ValidationError(
                {'flag': 'The flag must be of the form CTF{...}.'}
            )

        if 'publishAt' in attrs:
            raw = (attrs.pop('publishAt') or '').strip()
            if not raw:
                attrs['publish_at'] = None
            else:
                parsed = parse_datetime(raw)
                if parsed is None:
                    raise serializers.ValidationError(
                        {'publishAt': 'Invalid date/time value.'}
                    )
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(
                        parsed, timezone.get_current_timezone()
                    )
                attrs['publish_at'] = parsed

        status_value = attrs.get(
            'status',
            self.instance.status if self.instance else Challenge.STATUS_DRAFT,
        )
        if 'publish_at' in attrs:
            publish_at = attrs['publish_at']
        elif self.instance is not None:
            publish_at = self.instance.publish_at
        else:
            publish_at = None
        if status_value == Challenge.STATUS_SCHEDULED and publish_at is None:
            raise serializers.ValidationError(
                {'publishAt': 'A publish time is required to schedule a challenge.'}
            )
        return attrs

    def _write_hints(self, challenge, hints):
        challenge.hints.all().delete()
        ChallengeHint.objects.bulk_create([
            ChallengeHint(
                challenge=challenge,
                text=item['text'],
                cost=item.get('cost', 0),
                order=index,
            )
            for index, item in enumerate(hints)
        ])

    def create(self, validated_data):
        flag = (validated_data.pop('flag', '') or '').strip()
        flag_note = validated_data.pop('flag_note', '')
        hints = validated_data.pop('hints', [])
        challenge = Challenge.objects.create(**validated_data)
        if flag:
            stored = ChallengeFlag(challenge=challenge, note=flag_note or '')
            stored.set_flag(flag)
            stored.save()
        self._write_hints(challenge, hints)
        return challenge

    def update(self, instance, validated_data):
        flag = validated_data.pop('flag', None)
        flag_note = validated_data.pop('flag_note', None)
        hints = validated_data.pop('hints', None)

        instance = super().update(instance, validated_data)

        if flag is not None and flag.strip():
            stored, _ = ChallengeFlag.objects.get_or_create(challenge=instance)
            if flag_note is not None:
                stored.note = flag_note
            stored.set_flag(flag)
            stored.save()
        elif flag_note is not None:
            stored = getattr(instance, 'flag', None)
            if stored is not None:
                stored.note = flag_note
                stored.save(update_fields=['note', 'updated_at'])

        if hints is not None:
            self._write_hints(instance, hints)

        return instance

    def to_representation(self, instance):
        return ChallengeAdminSerializer(instance, context=self.context).data
