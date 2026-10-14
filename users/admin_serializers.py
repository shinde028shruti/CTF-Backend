"""Serializers for the admin user-management API.

These are deliberately separate from the auth serializers so the admin surface
can expose administrative fields (``is_active``, ``status``, ``groups``,
``is_superuser``) without ever widening what ``/auth/me/`` returns, and so the
privileged fields can be kept strictly read-only.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .groups import assign_default_group

User = get_user_model()


class AdminGroupSerializer(serializers.ModelSerializer):
    """Minimal group representation for admin lists and group pickers."""

    class Meta:
        model = Group
        fields = ('id', 'name')
        read_only_fields = fields


class AdminUserSerializer(serializers.ModelSerializer):
    """Read-only administrative representation of a user.

    Password material, tokens and other secrets are never listed. ``role`` is
    derived so a superuser reports as admin, matching ``/auth/me/``, without
    touching the stored column.
    """

    groups = AdminGroupSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = (
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'points',
            'bio',
            'avatar',
            'role',
            'is_active',
            'is_staff',
            'is_superuser',
            'status',
            'status_note',
            'date_joined',
            'last_login',
            'deleted_at',
            'onboardingCompleted',
            'groups',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.is_admin:
            data['role'] = User.ROLE_ADMIN
        return data


class AdminUserCreateSerializer(serializers.ModelSerializer):
    """Create a user from the admin panel.

    Only the whitelisted fields below are accepted, so ``role``, ``groups``,
    ``is_staff``, ``is_superuser`` and ``user_permissions`` can never be set
    here - privilege is granted exclusively through the group/permission model.
    """

    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name', 'password')

    def validate_username(self, value):
        # all_objects: soft-deleted accounts keep their UNIQUE username, so the
        # duplicate check must not be limited to the non-deleted default
        # manager (which would otherwise end in a raw IntegrityError, i.e. 500).
        if User.all_objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                'A user with that username already exists.'
            )
        return value

    def validate(self, attrs):
        candidate = User(
            username=attrs.get('username', ''),
            email=attrs.get('email', ''),
            first_name=attrs.get('first_name', ''),
            last_name=attrs.get('last_name', ''),
        )
        try:
            validate_password(attrs['password'], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            user = User.objects.create_user(**validated_data)
            assign_default_group(user)
        return user

    def to_representation(self, instance):
        return AdminUserSerializer(instance).data


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """Editable profile fields only.

    ``username``, ``role``, ``status``, ``is_staff``, ``is_superuser`` and
    ``groups`` are intentionally excluded; they have dedicated endpoints or are
    not editable through this API.
    """

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email')

    def to_representation(self, instance):
        return AdminUserSerializer(instance).data


class AdminUserStatusSerializer(serializers.Serializer):
    """Payload for PATCH /admin/users/{id}/status/."""

    status = serializers.ChoiceField(choices=User.STATUS_CHOICES)
    status_note = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=255,
    )


class AdminUserGroupsSerializer(serializers.Serializer):
    """Payload for PATCH /admin/users/{id}/groups/.

    ``PrimaryKeyRelatedField`` rejects unknown group ids with a 400 before any
    change is made.
    """

    groups = serializers.PrimaryKeyRelatedField(
        queryset=Group.objects.all(),
        many=True,
    )
