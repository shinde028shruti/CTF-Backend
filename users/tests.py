from django.conf import settings
from django.contrib import admin
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken


class UserModelTests(TestCase):
    def setUp(self):
        self.User = get_user_model()

    def test_custom_user_model_is_used(self):
        self.assertEqual(settings.AUTH_USER_MODEL, 'users.User')
        self.assertIs(self.User, get_user_model())
        self.assertEqual(self.User._meta.app_label, 'users')
        self.assertEqual(self.User.__name__, 'User')
        self.assertTrue(issubclass(self.User, models.Model))

    def test_user_can_be_created(self):
        user = self.User.objects.create_user(
            username='ctfplayer',
            password='sup3rs3cret!',
            email='player@example.com',
            first_name='Ada',
            last_name='Lovelace',
        )
        self.assertIsNotNone(user.pk)
        self.assertEqual(user.username, 'ctfplayer')
        self.assertEqual(user.email, 'player@example.com')
        self.assertEqual(user.first_name, 'Ada')
        self.assertEqual(user.last_name, 'Lovelace')
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertIsNotNone(user.date_joined)

    def test_points_defaults_to_zero(self):
        user = self.User.objects.create_user(username='newbie', password='sup3rs3cret!')
        self.assertEqual(user.points, 0)

    def test_points_can_be_set(self):
        user = self.User.objects.create_user(
            username='solver', password='sup3rs3cret!', points=350,
        )
        user.refresh_from_db()
        self.assertEqual(user.points, 350)

    def test_points_cannot_be_negative(self):
        user = self.User.objects.create_user(username='cheater', password='sup3rs3cret!')
        user.points = -1
        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_bio_is_optional(self):
        user = self.User.objects.create_user(username='quiet', password='sup3rs3cret!')
        self.assertEqual(user.bio, '')
        user.bio = 'Just here for the crypto challenges.'
        user.full_clean()
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.bio, 'Just here for the crypto challenges.')

    def test_avatar_is_optional(self):
        user = self.User.objects.create_user(username='anon', password='sup3rs3cret!')
        self.assertFalse(user.avatar)
        self.assertIsNone(user.avatar.name)
        user.full_clean()

    def test_avatar_upload_to_prefix(self):
        field = self.User._meta.get_field('avatar')
        self.assertEqual(field.upload_to, 'avatars/')

    def test_password_is_hashed(self):
        user = self.User.objects.create_user(username='hashme', password='sup3rs3cret!')
        self.assertNotEqual(user.password, 'sup3rs3cret!')
        self.assertNotIn('sup3rs3cret!', user.password)
        self.assertTrue(user.password.startswith('pbkdf2_'))
        self.assertTrue(user.check_password('sup3rs3cret!'))

    def test_username_authentication_still_works(self):
        self.User.objects.create_user(username='trylogin', password='sup3rs3cret!')
        user = authenticate(username='trylogin', password='sup3rs3cret!')
        self.assertIsNotNone(user)
        self.assertEqual(user.username, 'trylogin')
        self.assertIsNone(authenticate(username='trylogin', password='wrong-password'))

    def test_user_is_registered_in_admin(self):
        self.assertIn(self.User, admin.site._registry)


class UserPermissionsTests(TestCase):
    def setUp(self):
        self.User = get_user_model()

    def test_groups_and_permissions_are_available(self):
        user = self.User.objects.create_user(username='member', password='sup3rs3cret!')
        group = self._create_group()
        user.groups.add(group)
        user.save()
        user.refresh_from_db()
        self.assertIn(group, user.groups.all())
        self.assertEqual(list(user.user_permissions.all()), [])

    def test_superuser_flags(self):
        admin_user = self.User.objects.create_superuser(
            username='root', password='sup3rs3cret!', email='root@example.com',
        )
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_active)
        self.assertEqual(admin_user.points, 0)

    def _create_group(self):
        from django.contrib.auth.models import Group
        return Group.objects.create(name='players')


VALID_PASSWORD = 'Str0ng-Passw0rd!42'


class AuthAPITestCase(TestCase):
    """Shared helpers for the authentication endpoint tests."""

    def setUp(self):
        self.User = get_user_model()
        self.client = APIClient()
        self.register_url = reverse('auth:register')
        self.login_url = reverse('auth:login')
        self.refresh_url = reverse('auth:refresh')
        self.me_url = reverse('auth:me')
        self.onboarding_url = reverse('auth:onboarding')

    def register(self, username='om', email='om@example.com',
                 password=VALID_PASSWORD, password2=None):
        return self.client.post(
            self.register_url,
            {
                'username': username,
                'email': email,
                'password': password,
                'password2': password2 if password2 is not None else password,
            },
            format='json',
        )

    def auth_header_for(self, user):
        refresh = RefreshToken.for_user(user)
        return {'HTTP_AUTHORIZATION': 'Bearer ' + str(refresh.access_token)}


class RegisterTests(AuthAPITestCase):
    def test_registration_succeeds_and_returns_201(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.User.objects.filter(username='om').exists())

    def test_registration_response_exposes_no_password(self):
        response = self.register()
        self.assertNotIn('password', response.data)
        self.assertNotIn('password2', response.data)
        self.assertEqual(response.data['username'], 'om')
        self.assertEqual(response.data['points'], 0)

    def test_password_is_hashed_and_not_stored_plain_text(self):
        self.register(password=VALID_PASSWORD)
        user = self.User.objects.get(username='om')
        self.assertNotEqual(user.password, VALID_PASSWORD)
        self.assertNotIn(VALID_PASSWORD, user.password)
        self.assertTrue(user.check_password(VALID_PASSWORD))

    def test_duplicate_username_is_rejected(self):
        self.register()
        response = self.register(email='other@example.com')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('username', response.data)
        self.assertEqual(self.User.objects.filter(username='om').count(), 1)

    def test_password_confirmation_mismatch_is_rejected(self):
        response = self.register(password2='Something-Else!99')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password2', response.data)
        self.assertFalse(self.User.objects.filter(username='om').exists())

    def test_weak_password_is_rejected(self):
        response = self.register(password='123')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password', response.data)
        self.assertFalse(self.User.objects.filter(username='om').exists())

    def test_invalid_email_is_rejected(self):
        response = self.register(email='not-an-email')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', response.data)

    def test_missing_required_fields_are_rejected(self):
        response = self.client.post(self.register_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        for field in ('username', 'password', 'password2'):
            self.assertIn(field, response.data)


class LoginTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )

    def test_valid_credentials_return_jwt_pair(self):
        response = self.client.post(
            self.login_url,
            {'username': 'om', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_login_response_exposes_no_password(self):
        response = self.client.post(
            self.login_url,
            {'username': 'om', 'password': VALID_PASSWORD},
            format='json',
        )
        body = response.content.decode()
        self.assertNotIn('password', response.data)
        self.assertNotIn(VALID_PASSWORD, body)
        self.assertNotIn(self.user.password, body)

    def test_invalid_password_is_rejected(self):
        response = self.client.post(
            self.login_url,
            {'username': 'om', 'password': 'wrong-password'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn('access', response.data)

    def test_unknown_username_is_rejected(self):
        response = self.client.post(
            self.login_url,
            {'username': 'nobody', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(
            self.login_url,
            {'username': 'om', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class MeTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_request_returns_current_user(self):
        response = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'om')
        self.assertEqual(response.data['email'], 'om@example.com')
        self.assertEqual(response.data['points'], 0)
        self.assertEqual(response.data['bio'], '')
        self.assertIsNone(response.data['avatar'])

    def test_response_never_includes_password_or_sensitive_flags(self):
        response = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertNotIn('password', response.data)
        self.assertNotIn('is_superuser', response.data)
        self.assertNotIn('is_staff', response.data)

    def test_invalid_token_is_rejected(self):
        response = self.client.get(
            self.me_url, HTTP_AUTHORIZATION='Bearer not-a-real-token',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_token_cannot_read_another_users_data(self):
        other = self.User.objects.create_user(
            username='mallory', email='m@example.com', password=VALID_PASSWORD,
        )
        response = self.client.get(self.me_url, **self.auth_header_for(other))
        self.assertEqual(response.data['username'], 'mallory')
        self.assertNotEqual(response.data['username'], self.user.username)


class RefreshTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )
        self.refresh_token = str(RefreshToken.for_user(self.user))

    def test_valid_refresh_token_returns_new_access_token(self):
        response = self.client.post(
            self.refresh_url, {'refresh': self.refresh_token}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        AccessToken(response.data['access'])

    def test_invalid_refresh_token_is_rejected(self):
        response = self.client.post(
            self.refresh_url, {'refresh': 'not-a-real-token'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_refresh_token_is_rejected(self):
        response = self.client.post(self.refresh_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refreshed_access_token_authenticates_me_endpoint(self):
        refreshed = self.client.post(
            self.refresh_url, {'refresh': self.refresh_token}, format='json',
        )
        response = self.client.get(
            self.me_url,
            HTTP_AUTHORIZATION='Bearer ' + refreshed.data['access'],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'om')


class OnboardingModelDefaultTests(TestCase):
    """New accounts must start with onboarding incomplete so they get asked."""

    def setUp(self):
        self.User = get_user_model()

    def test_new_user_has_onboarding_incomplete(self):
        user = self.User.objects.create_user(
            username='fresh', email='fresh@example.com', password=VALID_PASSWORD,
        )
        self.assertFalse(user.onboardingCompleted)
        self.assertEqual(user.onboarding, {})

    def test_role_defaults_to_user(self):
        user = self.User.objects.create_user(
            username='plain', email='plain@example.com', password=VALID_PASSWORD,
        )
        self.assertEqual(user.role, self.User.ROLE_USER)
        self.assertFalse(user.is_admin)


class OnboardingEndpointTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )
        self.payload = {
            'skillLevel': 'intermediate',
            'teamInterest': 'both',
            'focusAreas': ['OSINT', 'Cryptography'],
            'purpose': 'competitive',
            'experienceType': 'professional',
            'userType': 'competitor',
        }

    def submit(self, payload=None, token=None):
        headers = self.auth_header_for(self.user) if token is None else {
            'HTTP_AUTHORIZATION': 'Bearer ' + token
        }
        return self.client.post(
            self.onboarding_url,
            payload if payload is not None else self.payload,
            format='json',
            **headers,
        )

    def test_submitting_questionnaire_marks_onboarding_complete(self):
        response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.onboardingCompleted)
        self.assertEqual(self.user.onboarding['skillLevel'], 'intermediate')
        self.assertEqual(
            self.user.onboarding['focusAreas'], ['OSINT', 'Cryptography'],
        )

    def test_completion_survives_a_fresh_read_of_me(self):
        """Guards against the /onboarding redirect loop on reload."""
        self.submit()
        response = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['onboardingCompleted'])
        self.assertEqual(response.data['onboarding']['userType'], 'competitor')

    def test_incomplete_questionnaire_is_rejected(self):
        response = self.submit({'skillLevel': 'beginner'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.onboardingCompleted)

    def test_empty_focus_areas_is_rejected(self):
        payload = dict(self.payload, focusAreas=[])
        response = self.submit(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.onboardingCompleted)

    def test_anonymous_submission_is_rejected(self):
        response = self.client.post(
            self.onboarding_url, self.payload, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_submission_only_affects_the_calling_user(self):
        other = self.User.objects.create_user(
            username='other', email='other@example.com', password=VALID_PASSWORD,
        )
        self.submit()
        other.refresh_from_db()
        self.assertFalse(other.onboardingCompleted)

    def test_cannot_escalate_role_through_onboarding_payload(self):
        response = self.submit(dict(self.payload, role='admin'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, self.User.ROLE_USER)