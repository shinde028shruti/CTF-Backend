from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    points = models.PositiveIntegerField(default=0)
    bio = models.TextField(blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)

    # Consumed by the frontend to gate routes and render the admin panel.
    # Django's built-in `is_staff` is deliberately not reused so the API
    # exposes a single, stable role vocabulary.
    ROLE_USER = 'user'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_USER, 'User'),
        (ROLE_ADMIN, 'Admin'),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_USER,
    )

    # The React app redirects to /onboarding while this is false. New accounts
    # therefore start incomplete and are walked through the questionnaire; the
    # flag is flipped by POST /api/v1/auth/onboarding/ once they finish, which
    # is what stops them being asked again on the next session.
    onboardingCompleted = models.BooleanField(default=False)

    # The questionnaire answers (skillLevel, teamInterest, focusAreas, purpose,
    # experienceType, userType). Stored as JSON because the shape is defined by
    # the frontend flow and Dashboard/Challenges read focusAreas from it.
    onboarding = models.JSONField(default=dict, blank=True)

    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN