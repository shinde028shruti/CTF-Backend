from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AvatarView,
    ChangePasswordView,
    LogoutView,
    MeView,
    OnboardingView,
    PreferencesView,
    RegisterView,
)

app_name = 'auth'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', TokenObtainPairView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('password/', ChangePasswordView.as_view(), name='password-change'),
    path('me/', MeView.as_view(), name='me'),
    path('me/preferences/', PreferencesView.as_view(), name='me-preferences'),
    path('me/avatar/', AvatarView.as_view(), name='me-avatar'),
    path('onboarding/', OnboardingView.as_view(), name='onboarding'),
]
