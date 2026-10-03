from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

User = get_user_model()


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
            'role',
            'onboardingCompleted',
            'onboarding',
        )
        read_only_fields = fields


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
        if User.objects.filter(username__iexact=value).exists():
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
        return User.objects.create_user(**validated_data)

    def to_representation(self, instance):
        # Respond with the same safe shape used by /auth/me/, so a freshly
        # registered client gets back the full public profile.
        return UserSerializer(instance).data