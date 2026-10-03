from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    OnboardingSerializer,
    RegisterSerializer,
    UserSerializer,
)


class RegisterView(generics.CreateAPIView):
    """Create a new user account. Public endpoint."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(generics.RetrieveAPIView):
    """Return the currently authenticated user. JWT access token required."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


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

        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)