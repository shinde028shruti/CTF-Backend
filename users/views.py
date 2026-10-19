from django.db import transaction
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    ChangePasswordSerializer,
    OnboardingSerializer,
    PreferencesSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    UserSerializer,
)
from .throttling import PasswordChangeThrottle


def _blacklist_refresh_tokens(user):
    """Invalidate every refresh token ever issued to ``user``.

    Access tokens are stateless and stay valid until they expire (15 minutes by
    default) - that is a documented SimpleJWT property, not a bug. Blacklisting
    the refresh tokens is what stops the session being renewed, so the caller
    and every other device must sign in again once their access token lapses.
    """
    with transaction.atomic():
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)



class RegisterView(generics.CreateAPIView):
    """Create a new user account. Public endpoint."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(generics.RetrieveUpdateAPIView):
    """
    Read or partially update the currently authenticated user.

    The target is always ``request.user`` (there is no id in the URL), so a
    caller can only ever read or edit their own record. Writes go through
    ``ProfileUpdateSerializer``, which only exposes self-editable fields; reads
    return the full public representation.

    PUT is disabled on purpose: the profile is updated field-by-field, and PUT's
    replace semantics would let an omitted field wipe existing data.
    """

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return ProfileUpdateSerializer
        return UserSerializer

    def update(self, request, *args, **kwargs):
        # PATCH is always partial, but pass it explicitly so a future PUT
        # (if ever enabled) cannot silently fall back to replace semantics.
        partial = kwargs.pop('partial', True)
        instance = self.get_object()
        serializer = self.get_serializer(
            instance, data=request.data, partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Echo the full read shape (absolute avatar URL, computed role) so the
        # client can refresh its cached user in one round-trip.
        return Response(
            UserSerializer(serializer.instance, context={'request': request}).data
        )


class AvatarView(APIView):
    """
    Remove the caller's avatar and delete the stored file.

    Uploading happens through ``PATCH /auth/me/`` (multipart); this endpoint is
    the explicit "delete the current image" action so the referenced file never
    becomes an orphan.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        user = request.user
        if user.avatar and user.avatar.name:
            user.avatar.storage.delete(user.avatar.name)
        user.avatar = None
        user.save(update_fields=['avatar'])

        return Response(
            UserSerializer(user, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )


class PreferencesView(APIView):
    """
    PATCH /auth/me/preferences/ - update the caller's account preferences.

    Body accepts any subset of ``notification_prefs`` and
    ``profile_visibility``. The target is always ``request.user``, so one
    account can never write another's preferences, and the serializer only
    exposes those two fields. The canonical user is echoed back so the client
    can refresh its cache in one round-trip.
    """

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        user = request.user
        serializer = PreferencesSerializer(
            user, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            UserSerializer(user, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )


class ChangePasswordView(APIView):
    """
    POST /auth/password/ - change the caller's password.

    Requires the current password and validates the new one against
    ``AUTH_PASSWORD_VALIDATORS``. On success every outstanding refresh token is
    blacklisted, so all sessions (including this one) must re-authenticate.
    Passwords are write-only and are never returned or logged.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [PasswordChangeThrottle]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        _blacklist_refresh_tokens(user)
        return Response(
            {'detail': 'Password changed. Please sign in again.'},
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """
    POST /auth/logout/ - best-effort server-side session revocation.

    Blacklists the submitted refresh token so it can no longer be exchanged for
    an access token. The endpoint is intentionally unauthenticated and
    idempotent: the access token may already have expired, and a missing,
    malformed, expired or already-blacklisted refresh token must not stop the
    client from clearing its local session. It always answers 200.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        raw = request.data.get('refresh')
        if isinstance(raw, str) and raw.strip():
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                # Expired, malformed or already blacklisted: the desired end
                # state (this token can no longer be used) already holds.
                pass
        return Response(
            {'detail': 'Signed out.'}, status=status.HTTP_200_OK,
        )


class OnboardingView(APIView):
    """
    Store the questionnaire answers and mark onboarding as complete.

    The frontend only reflects ``onboardingCompleted`` locally, so without this
    endpoint the flag would come back ``False`` from ``/me/`` on the next page
    load and the user would be dragged through the questionnaire again.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = OnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.onboarding = dict(serializer.validated_data)
        user.onboardingCompleted = True
        user.save(update_fields=['onboarding', 'onboardingCompleted'])

        return Response(
            UserSerializer(user, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )
