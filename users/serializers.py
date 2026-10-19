from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework import serializers

from .groups import assign_default_group
from .models import DEFAULT_NOTIFICATION_PREFS, NOTIFICATION_PREF_KEYS

User = get_user_model()

# Avatar upload limits. Enforced server-side so a crafted request cannot bypass
# the client's `accept` attribute or the size hint shown in Settings.
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_AVATAR_TYPES = {
    'image/png',
    'image/jpeg',
    'image/jpg',
    'image/gif',
    'image/webp',
}


class UserSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a user.

    Only safe fields are listed, so the password hash can never be serialized.
    """

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
            'display_name',
            'website',
            'twitter',
            'github',
            'role',
            'onboardingCompleted',
            'onboarding',
            'notification_prefs',
            'profile_visibility',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        # Report app-admin for a stored role of 'admin' or for a Django
        # superuser, so the frontend's `user.role === 'admin'` gate unlocks for
        # both. Read-time only: the stored `role` column is never changed, and
        # group membership is not consulted (role and groups stay independent).
        data = super().to_representation(instance)
        if instance.is_admin:
            data['role'] = User.ROLE_ADMIN
        # Always echo a complete preference set: a stored `{}` (the default for
        # every existing row) or a partially-written dict is filled in with the
        # canonical defaults, so the client never has to invent missing keys.
        data['notification_prefs'] = {
            **DEFAULT_NOTIFICATION_PREFS,
            **(instance.notification_prefs or {}),
        }
        return data


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """
    Partial update of the caller's *own* profile.

    Deliberately narrow: only the fields a user may edit about themselves are
    listed, so a crafted payload cannot reach ``role``, ``points``,
    ``is_active``, ``is_superuser``, ``status`` or ``onboardingCompleted``.
    Those keys are silently ignored by DRF rather than raising, which keeps the
    endpoint tolerant of clients that echo back a full user object.

    Used with ``partial=True`` (PATCH), so an omitted field is never cleared -
    editing the bio cannot blank the website, and vice versa.
    """

    class Meta:
        model = User
        fields = (
            'display_name',
            'bio',
            'website',
            'twitter',
            'github',
            'avatar',
        )
        extra_kwargs = {
            'display_name': {'required': False, 'allow_blank': True},
            'bio': {'required': False, 'allow_blank': True},
            'website': {'required': False, 'allow_blank': True},
            'twitter': {'required': False, 'allow_blank': True},
            'github': {'required': False, 'allow_blank': True},
            # `allow_null` lets the owner clear the avatar with {"avatar": null}.
            'avatar': {'required': False, 'allow_null': True},
        }

    def validate_display_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Display name cannot be empty.')
        if len(value) > 80:
            raise serializers.ValidationError(
                'Display name must be 80 characters or fewer.'
            )
        return value

    def validate_bio(self, value):
        if value and len(value) > 500:
            raise serializers.ValidationError(
                'Bio must be 500 characters or fewer.'
            )
        return value

    def validate_avatar(self, value):
        # ``None`` is the explicit "remove my avatar" signal.
        if value is None:
            return None

        # ImageField has already opened the file with Pillow by this point, so
        # this only adds the size/type checks Pillow does not perform.
        if value.size > MAX_AVATAR_BYTES:
            raise serializers.ValidationError(
                'Image is too large. The maximum size is 2 MB.'
            )

        content_type = getattr(value, 'content_type', '')
        if content_type and content_type not in ALLOWED_AVATAR_TYPES:
            raise serializers.ValidationError(
                'Unsupported image type. Use PNG, JPEG, GIF or WebP.'
            )

        return value

    def update(self, instance, validated_data):
        # Snapshot the old file name so it can be removed once the row points
        # at the new one. Covers both replacement and clearing (avatar=None).
        old_name = instance.avatar.name if instance.avatar else None

        instance = super().update(instance, validated_data)

        new_name = instance.avatar.name if instance.avatar else None
        if old_name and old_name != new_name:
            # Best-effort: a storage hiccup must not fail an otherwise valid
            # profile update, so the orphan is removed quietly.
            default_storage.delete(old_name)

        return instance


class PreferencesSerializer(serializers.ModelSerializer):
    """
    Partial update of the caller's own account preferences.

    Exposes only the two preference fields, so a crafted payload cannot reach
    ``role``, ``points``, ``email`` or any other column. Always bound to
    ``request.user`` by the view, so one user can never write another's
    preferences.

    ``notification_prefs`` is validated as a *closed* key set with boolean
    values; unknown keys and non-boolean values are rejected rather than
    silently dropped. A partial update is merged over the stored dict (and the
    canonical defaults), so sending one flag never clears the others.
    """

    class Meta:
        model = User
        fields = ('notification_prefs', 'profile_visibility')
        extra_kwargs = {
            'notification_prefs': {'required': False},
            'profile_visibility': {'required': False},
        }

    def validate_notification_prefs(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'Notification preferences must be an object.'
            )
        unknown = sorted(set(value) - set(NOTIFICATION_PREF_KEYS))
        if unknown:
            raise serializers.ValidationError(
                'Unknown notification preference(s): '
                + ', '.join(unknown)
                + '.'
            )
        for key, flag in value.items():
            if not isinstance(flag, bool):
                raise serializers.ValidationError(
                    f'"{key}" must be true or false.'
                )
        return value

    def update(self, instance, validated_data):
        prefs = validated_data.pop('notification_prefs', None)
        if prefs is not None:
            instance.notification_prefs = {
                **DEFAULT_NOTIFICATION_PREFS,
                **(instance.notification_prefs or {}),
                **prefs,
            }
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    """
    Validate an authenticated password change.

    The caller must prove ownership with the current password, and the new
    password is checked against every validator in ``AUTH_PASSWORD_VALIDATORS``
    using the real user (so ``UserAttributeSimilarityValidator`` can compare it
    against the username/email). All three inputs are write-only and are never
    echoed back or logged.
    """

    current_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'},
    )
    new_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'},
    )
    new_password2 = serializers.CharField(
        write_only=True, style={'input_type': 'password'},
    )

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            # Deliberately generic: never reveal whether the account exists,
            # only that this credential did not match.
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password2']:
            raise serializers.ValidationError(
                {'new_password2': "Passwords don't match."}
            )
        if attrs['new_password'] == attrs['current_password']:
            raise serializers.ValidationError(
                {'new_password': 'New password must be different from the '
                                  'current password.'}
            )

        user = self.context['request'].user
        try:
            validate_password(attrs['new_password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {'new_password': list(exc.messages)}
            ) from exc
        return attrs

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password'])
        return user


class OnboardingSerializer(serializers.Serializer):
    """
    Validates the questionnaire submitted by a brand-new account.

    The answers are stored as-is on ``User.onboarding``; only their presence
    and shape are validated, since the option sets live in the React flow.
    """

    skillLevel = serializers.CharField(max_length=32, required=False, allow_blank=True)
    teamInterest = serializers.CharField(max_length=32, required=False, allow_blank=True)
    focusAreas = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
    )
    purpose = serializers.CharField(max_length=32, required=False, allow_blank=True)
    experienceType = serializers.CharField(max_length=32, required=False, allow_blank=True)
    userType = serializers.CharField(max_length=32, required=False, allow_blank=True)

    def validate(self, attrs):
        # The flow requires an answer to each single-select question, and at
        # least one focus area, before "Complete Setup" becomes clickable.
        required = ('skillLevel', 'teamInterest', 'purpose', 'experienceType')
        missing = [f for f in required if not attrs.get(f)]
        if missing:
            raise serializers.ValidationError(
                {'detail': 'Please answer every question before completing setup.'}
            )
        if not attrs.get('focusAreas'):
            raise serializers.ValidationError(
                {'detail': 'Please select at least one focus area.'}
            )
        return attrs


class RegisterSerializer(serializers.ModelSerializer):
    """
    Validates and creates a new user account.

    Both password fields are write_only, so they are accepted on input but
    never included in the output.
    """

    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
    )
    password2 = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'password2')

    def validate_username(self, value):
        # all_objects: a soft-deleted account still occupies its (UNIQUE)
        # username, so rejecting only against `objects` would let validation
        # pass and then blow up with a DB IntegrityError.
        if User.all_objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('A user with that username already exists.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError(
                {'password2': "Passwords don't match."}
            )

        # Runs every validator listed in AUTH_PASSWORD_VALIDATORS.
        # A throwaway instance is passed so UserAttributeSimilarityValidator can
        # compare the password against the submitted username and email.
        candidate = User(
            username=attrs.get('username', ''),
            email=attrs.get('email', ''),
        )
        try:
            validate_password(attrs['password'], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)}) from exc

        return attrs

    def create(self, validated_data):
        # create_user() hashes the password via set_password().
        validated_data.pop('password2')

        # The account and its default-group membership are written together:
        # if the group is not configured, assign_default_group() raises and the
        # transaction rolls the account back, so no orphan user is left behind.
        with transaction.atomic():
            user = User.objects.create_user(**validated_data)
            assign_default_group(user)
        return user

    def to_representation(self, instance):
        # Respond with the same safe shape used by /auth/me/, so a freshly
        # registered client gets back the full public profile. The request
        # context is forwarded so the avatar URL is absolute, not a bare
        # "/media/..." path the browser would resolve against the wrong origin.
        return UserSerializer(instance, context=self.context).data