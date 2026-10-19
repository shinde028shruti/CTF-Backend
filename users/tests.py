import os
import shutil
import tempfile
from io import BytesIO, StringIO

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.permissions import DjangoModelPermissions
from rest_framework.test import APIClient, APIRequestFactory
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
        # Registration now auto-joins the default group, so the group must
        # exist for every registration-through-the-API test. Idempotent.
        run_setup_groups()
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


# ---------------------------------------------------------------------------
# Groups and permissions
# ---------------------------------------------------------------------------

# Canonical group names, sorted so every assertion compares order-independently.
GROUP_NAMES = sorted(['User', 'Challenge Creator', 'Admin'])

# Permissions every Admin group member holds.
ADMIN_GROUP_PERMS = {
    'users.view_user', 'users.add_user', 'users.change_user', 'users.delete_user',
    'auth.view_group', 'auth.add_group', 'auth.change_group', 'auth.delete_group',
    'challenges.view_challenge', 'challenges.add_challenge',
    'challenges.change_challenge', 'challenges.delete_challenge',
    'announcements.view_announcement', 'announcements.add_announcement',
    'announcements.change_announcement', 'announcements.delete_announcement',
    'achievements.view_achievement', 'achievements.add_achievement',
    'achievements.change_achievement', 'achievements.delete_achievement',
    'achievements.view_userachievement',
}

# Permissions a Challenge Creator holds. These are a strict subset of the Admin
# group's permissions (creators may not delete challenges).
CREATOR_GROUP_PERMS = {
    'challenges.view_challenge',
    'challenges.add_challenge',
    'challenges.change_challenge',
}

# Permissions that only an Admin holds; a Challenge Creator must never have
# these.
ADMIN_ONLY_PERMS = ADMIN_GROUP_PERMS - CREATOR_GROUP_PERMS


def run_setup_groups(*args):
    """Run setup_groups and return its captured stdout."""
    out = StringIO()
    call_command('setup_groups', *args, stdout=out, no_color=True)
    return out.getvalue()


class SetupGroupsTests(TestCase):
    """The management command itself: creation, idempotency, assignment."""

    def setUp(self):
        self.User = get_user_model()

    # -- group creation --------------------------------------------------

    def test_creates_the_three_groups(self):
        run_setup_groups()
        self.assertEqual(
            sorted(Group.objects.values_list('name', flat=True)), GROUP_NAMES,
        )

    def test_running_twice_creates_no_duplicates(self):
        run_setup_groups()
        run_setup_groups()
        self.assertEqual(Group.objects.count(), 3)
        self.assertEqual(
            sorted(Group.objects.values_list('name', flat=True)), GROUP_NAMES,
        )

    def test_second_run_reports_no_changes(self):
        run_setup_groups()
        output = run_setup_groups()
        self.assertEqual(output.count('unchanged'), 3)
        self.assertNotIn('+ ', output)

    def test_declared_group_names_are_exactly_the_required_ones(self):
        from users.management.commands.setup_groups import GRANTED
        self.assertEqual(sorted(GRANTED), GROUP_NAMES)

    # -- dry run ---------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        groups_before, perms_before = Group.objects.count(), Permission.objects.count()
        output = run_setup_groups('--dry-run')

        self.assertEqual(Group.objects.count(), groups_before)
        self.assertEqual(Permission.objects.count(), perms_before)
        self.assertIn('create  group  User', output)
        self.assertIn('no changes written', output)

    # -- assignment ------------------------------------------------------

    def test_assign_puts_a_user_in_a_group(self):
        user = self.User.objects.create_user(
            username='h4ck3r_x', email='a@example.com', password=VALID_PASSWORD,
        )
        run_setup_groups('--assign', 'h4ck3r_x=User')
        self.assertEqual([g.name for g in user.groups.all()], ['User'])

    def test_assign_does_not_duplicate_existing_membership(self):
        self.User.objects.create_user(
            username='h4ck3r_x', email='a@example.com', password=VALID_PASSWORD,
        )
        run_setup_groups('--assign', 'h4ck3r_x=User')
        run_setup_groups('--assign', 'h4ck3r_x=User')
        self.assertEqual(Group.objects.get(name='User').user_set.count(), 1)

    def test_assign_rejects_unknown_group(self):
        self.User.objects.create_user(
            username='om', email='o@example.com', password=VALID_PASSWORD,
        )
        with self.assertRaises(CommandError) as ctx:
            run_setup_groups('--assign', 'om=Superusers')
        self.assertIn('Unknown group', str(ctx.exception))
        self.assertEqual(Group.objects.count(), 0)

    def test_assign_rejects_unknown_user(self):
        with self.assertRaises(CommandError) as ctx:
            run_setup_groups('--assign', 'ghost=Admin')
        self.assertIn('Unknown user', str(ctx.exception))

    def test_assign_requires_username_equals_group(self):
        with self.assertRaises(CommandError) as ctx:
            run_setup_groups('--assign', 'h4ck3r_x')
        self.assertIn('USERNAME=GROUP', str(ctx.exception))

    def test_group_membership_is_a_standard_m2m_not_a_model_column(self):
        user = self.User.objects.create_user(
            username='mem', email='m@example.com', password=VALID_PASSWORD,
        )
        run_setup_groups('--assign', 'mem=User')
        user.refresh_from_db()

        # No bespoke single-group column was bolted onto User...
        self.assertFalse(hasattr(user, 'group'))
        # ...it is Django's own m2m, backed by the through-model Django
        # auto-generates (never one we declared by hand).
        self.assertEqual(user.groups.count(), 1)
        self.assertTrue(user.groups.through._meta.auto_created)

    # -- untouched behaviour ---------------------------------------------

    def test_setup_never_touches_role_staff_or_superuser(self):
        admin_user = self.User.objects.create_superuser(
            username='root', email='r@example.com', password=VALID_PASSWORD,
        )
        plain = self.User.objects.create_user(
            username='plain', email='p@example.com', password=VALID_PASSWORD,
        )
        plain.role = self.User.ROLE_ADMIN
        plain.save(update_fields=['role'])

        run_setup_groups('--assign', 'plain=User')

        plain.refresh_from_db()
        admin_user.refresh_from_db()
        self.assertEqual(plain.role, self.User.ROLE_ADMIN)
        self.assertFalse(plain.is_staff)
        self.assertFalse(plain.is_superuser)
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)

    def test_existing_fields_are_still_declared_on_the_user_model(self):
        for field in ('role', 'is_staff', 'is_superuser', 'onboardingCompleted'):
            self.assertIn(field, [f.name for f in self.User._meta.get_fields()])

    # -- reporting --------------------------------------------------------

    def test_output_reports_no_missing_permissions(self):
        # The Challenge model now exists, so nothing is reported as missing.
        output = run_setup_groups()
        self.assertNotIn('MISSING / NOT GRANTED', output)


class GroupPermissionTests(TestCase):
    """What each group is actually allowed to do."""

    def setUp(self):
        self.User = get_user_model()
        run_setup_groups()

        self.normal = self.User.objects.create_user(
            username='pleb', email='p@example.com', password=VALID_PASSWORD,
        )
        self.normal.groups.add(Group.objects.get(name='User'))

        self.creator = self.User.objects.create_user(
            username='creator01', email='c@example.com', password=VALID_PASSWORD,
        )
        self.creator.groups.add(Group.objects.get(name='Challenge Creator'))

        # Deliberately NOT a superuser: superuser bypasses every permission
        # check, so it would prove nothing about the group's permissions.
        self.admin_member = self.User.objects.create_user(
            username='siteadmin', email='a@example.com', password=VALID_PASSWORD,
        )
        self.admin_member.groups.add(Group.objects.get(name='Admin'))

    # -- User group -------------------------------------------------------

    def test_user_group_exists(self):
        self.assertTrue(Group.objects.filter(name='User').exists())

    def test_user_group_has_no_administrative_permissions(self):
        self.assertEqual(
            set(Group.objects.get(name='User').permissions.values_list(
                'content_type__app_label', 'codename',
            )),
            set(),
        )

    def test_normal_user_cannot_use_admin_permissions(self):
        for ref in ADMIN_GROUP_PERMS:
            self.assertFalse(
                self.normal.has_perm(ref),
                f'plain member should not hold {ref}',
            )

    # -- Challenge Creator group ------------------------------------------

    def test_challenge_creator_group_exists(self):
        self.assertTrue(Group.objects.filter(name='Challenge Creator').exists())

    def test_challenge_creator_holds_the_challenge_permissions(self):
        # The Challenge model exists, so Django generated its model permissions
        # and setup_groups granted the creator view/add/change (but not delete).
        held = {
            f'{app}.{codename}'
            for app, codename in Group.objects.get(
                name='Challenge Creator'
            ).permissions.values_list(
                'content_type__app_label', 'codename',
            )
        }
        self.assertEqual(held, CREATOR_GROUP_PERMS)

    def test_challenge_creator_is_not_an_admin(self):
        for ref in ADMIN_ONLY_PERMS:
            self.assertFalse(self.creator.has_perm(ref), f'creator holds {ref}')

    # -- Admin group -------------------------------------------------------

    def test_admin_group_holds_exactly_the_declared_permissions(self):
        group = Group.objects.get(name='Admin')
        held = {
            f'{app}.{codename}'
            for app, codename in group.permissions.values_list(
                'content_type__app_label', 'codename',
            )
        }
        self.assertEqual(held, ADMIN_GROUP_PERMS)

    def test_admin_group_member_can_use_its_permissions(self):
        for ref in ADMIN_GROUP_PERMS:
            self.assertTrue(self.admin_member.has_perm(ref), f'missing {ref}')

    def test_admin_group_member_is_not_a_superuser_or_staff(self):
        self.assertFalse(self.admin_member.is_superuser)
        self.assertFalse(self.admin_member.is_staff)

    def test_group_permissions_resolve_through_the_custom_auth_backend(self):
        # UsernameOrEmailBackend subclasses ModelBackend; if that inheritance
        # ever broke, get_all_permissions() would silently return nothing.
        by_email = authenticate(
            username='a@example.com', password=VALID_PASSWORD,
        )
        by_username = authenticate(
            username='siteadmin', password=VALID_PASSWORD,
        )
        self.assertIsNotNone(by_email)
        self.assertIsNotNone(by_username)
        self.assertEqual(by_email.pk, self.admin_member.pk)

        for authed in (by_email, by_username):
            self.assertTrue(authed.has_perm('users.change_user'))
            self.assertTrue(authed.has_perm('auth.change_group'))

    def test_user_permissions_override_field_is_untouched(self):
        self.assertEqual(list(self.normal.user_permissions.all()), [])

    # -- registration cannot elevate ---------------------------------------

    def test_registration_cannot_assign_groups_or_role(self):
        client = APIClient()
        response = client.post(reverse('auth:register'), {
            'username': 'sneaky',
            'email': 'sneaky@example.com',
            'password': VALID_PASSWORD,
            'password2': VALID_PASSWORD,
            # Privilege-escalation attempts: all must be ignored.
            'role': 'admin',
            'groups': ['Admin'],
            'user_permissions': [1],
            'is_staff': True,
            'is_superuser': True,
            'onboardingCompleted': True,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = self.User.objects.get(username='sneaky')
        self.assertEqual(user.role, self.User.ROLE_USER)
        # Auto-assigned to the default group only; the attempted Admin group is
        # never applied.
        self.assertEqual([g.name for g in user.groups.all()], ['User'])
        self.assertFalse(
            user.groups.filter(name__in=['Admin', 'Challenge Creator']).exists(),
        )
        self.assertEqual(list(user.user_permissions.all()), [])
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_a_brand_new_account_earns_no_permissions_by_default(self):
        run_setup_groups()
        user = self.User.objects.create_user(
            username='fresh', email='f@example.com', password=VALID_PASSWORD,
        )
        self.assertFalse(user.has_perm('users.change_user'))
        self.assertFalse(user.has_perm('users.delete_user'))

    # -- Django admin ------------------------------------------------------

    def test_groups_and_permissions_are_managed_in_django_admin(self):
        # Requirement: admins can create/view groups, assign permissions to
        # groups, and assign users to groups - all from Django's stock admin.
        self.assertIn(Group, admin.site._registry)

        group_admin = admin.site._registry[Group]
        self.assertIn('permissions', group_admin.filter_horizontal)

        from users.admin import CustomUserAdmin
        self.assertIn('groups', CustomUserAdmin.filter_horizontal)

        # Stock Django ships no PermissionAdmin: permissions are edited
        # through the Group and User forms above, which is intentional. We do
        # not add one - that would be a bespoke admin surface for models we
        # were told not to extend.
        from django.contrib.auth.models import Permission as DjangoPermission
        self.assertNotIn(DjangoPermission, admin.site._registry)

    def test_user_admin_exposes_groups_field(self):
        from users.admin import CustomUserAdmin

        field_names = [
            field
            for _, options in CustomUserAdmin.fieldsets
            for field in options['fields']
        ]
        self.assertIn('groups', field_names)
        self.assertIn('user_permissions', field_names)
        self.assertIn('groups', CustomUserAdmin.filter_horizontal)
        # Admins can pick groups from a list with checkboxes, not raw pks.
        self.assertIn('groups', CustomUserAdmin.list_filter)

    # -- DRF readiness (no endpoint created) -------------------------------

    def test_drf_model_permissions_deny_a_plain_user(self):
        class DummyView:
            queryset = self.User.objects.all()

        request = APIRequestFactory().post('/')
        request.user = self.normal

        self.assertFalse(
            DjangoModelPermissions().has_permission(request, DummyView()),
        )

    def test_drf_model_permissions_allow_an_admin_group_member(self):
        class DummyView:
            queryset = self.User.objects.all()

        request = APIRequestFactory().post('/')
        request.user = self.admin_member

        self.assertTrue(
            DjangoModelPermissions().has_permission(request, DummyView()),
        )


# ---------------------------------------------------------------------------
# Automatic default-group assignment on registration
# ---------------------------------------------------------------------------


class RegistrationGroupAssignmentTests(AuthAPITestCase):
    """A successful registration joins the configured default group."""

    def test_new_user_belongs_to_the_user_group(self):
        response = self.register(username='fresh')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = self.User.objects.get(username='fresh')
        self.assertEqual([g.name for g in user.groups.all()], ['User'])

    def test_new_user_receives_the_groups_permissions(self):
        # Grant the User group a permission, then prove a fresh registrant
        # actually inherits it (rather than only the membership row).
        perm = Permission.objects.get(
            content_type__app_label='users', codename='view_user',
        )
        Group.objects.get(name='User').permissions.add(perm)

        self.register(username='permuser')
        user = self.User.objects.get(username='permuser')
        self.assertTrue(user.has_perm('users.view_user'))

    def test_registration_cannot_grant_admin_or_challenge_creator(self):
        response = self.client.post(self.register_url, {
            'username': 'sneaky',
            'email': 'sneaky@example.com',
            'password': VALID_PASSWORD,
            'password2': VALID_PASSWORD,
            # Privilege-escalation attempts: each must be ignored.
            'groups': ['Admin', 'Challenge Creator'],
            'role': 'admin',
            'user_permissions': [1],
            'is_staff': True,
            'is_superuser': True,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = self.User.objects.get(username='sneaky')
        self.assertEqual([g.name for g in user.groups.all()], ['User'])
        self.assertFalse(user.groups.filter(name='Admin').exists())
        self.assertFalse(user.groups.filter(name='Challenge Creator').exists())
        self.assertEqual(user.role, self.User.ROLE_USER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_login_and_me_still_work_for_an_auto_assigned_user(self):
        self.register(username='loginme', password=VALID_PASSWORD)

        login = self.client.post(
            self.login_url,
            {'username': 'loginme', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertIn('access', login.data)

        me = self.client.get(
            self.me_url,
            HTTP_AUTHORIZATION='Bearer ' + login.data['access'],
        )
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.data['username'], 'loginme')

    def test_registration_fails_safely_when_group_is_missing(self):
        # Simulate an operator who has not run `setup_groups` yet.
        Group.objects.filter(name='User').delete()

        response = self.register(username='orphan')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn('setup_groups', str(response.data))
        # The transaction rolled back: no half-created account survives.
        self.assertFalse(self.User.objects.filter(username='orphan').exists())


class AssignDefaultGroupCommandTests(TestCase):
    """The one-time backfill for accounts created before auto-assignment."""

    def setUp(self):
        self.User = get_user_model()
        run_setup_groups()

        self.ordinary = self.User.objects.create_user(
            username='ordinary', email='o@example.com', password=VALID_PASSWORD,
        )
        self.staff = self.User.objects.create_user(
            username='staffer', email='s@example.com', password=VALID_PASSWORD,
            is_staff=True,
        )
        self.root = self.User.objects.create_superuser(
            username='root', email='r@example.com', password=VALID_PASSWORD,
        )
        self.grouped = self.User.objects.create_user(
            username='grouped', email='g@example.com', password=VALID_PASSWORD,
        )
        self.grouped.groups.add(Group.objects.get(name='Admin'))

    def run_backfill(self, *args):
        out = StringIO()
        call_command('assign_default_group', *args, stdout=out, no_color=True)
        return out.getvalue()

    def test_assigns_ungrouped_ordinary_users(self):
        self.run_backfill()
        self.assertEqual([g.name for g in self.ordinary.groups.all()], ['User'])

    def test_skips_staff_and_superuser(self):
        self.run_backfill()
        self.assertEqual(self.staff.groups.count(), 0)
        self.assertEqual(self.root.groups.count(), 0)

    def test_skips_users_already_in_a_group(self):
        self.run_backfill()
        self.assertEqual([g.name for g in self.grouped.groups.all()], ['Admin'])

    def test_is_idempotent(self):
        self.run_backfill()
        output = self.run_backfill()
        self.assertIn('No ungrouped ordinary users', output)
        self.assertEqual(self.ordinary.groups.count(), 1)

    def test_dry_run_writes_nothing(self):
        output = self.run_backfill('--dry-run')
        self.assertIn('would assign', output)
        self.assertEqual(self.ordinary.groups.count(), 0)

    def test_errors_when_group_is_missing(self):
        Group.objects.filter(name='User').delete()
        with self.assertRaises(CommandError) as ctx:
            self.run_backfill()
        self.assertIn('setup_groups', str(ctx.exception))


# ---------------------------------------------------------------------------
# App-admin derivation (role / superuser - never groups)
# ---------------------------------------------------------------------------


class AppAdminRoleTests(AuthAPITestCase):
    """`role` is derived from the stored role or superuser status only."""

    def me(self, user):
        return self.client.get(self.me_url, **self.auth_header_for(user))

    def test_plain_user_is_not_app_admin(self):
        user = self.User.objects.create_user(
            username='plain', email='p@example.com', password=VALID_PASSWORD,
        )
        self.assertEqual(self.me(user).data['role'], self.User.ROLE_USER)

    def test_stored_role_admin_is_reported(self):
        user = self.User.objects.create_user(
            username='boss', email='b@example.com', password=VALID_PASSWORD,
            role=self.User.ROLE_ADMIN,
        )
        self.assertEqual(self.me(user).data['role'], self.User.ROLE_ADMIN)

    def test_superuser_is_app_admin_without_a_stored_role(self):
        user = self.User.objects.create_superuser(
            username='root', email='r@example.com', password=VALID_PASSWORD,
        )
        self.assertEqual(self.me(user).data['role'], self.User.ROLE_ADMIN)

        # Derivation is read-time only: the stored column is untouched.
        user.refresh_from_db()
        self.assertEqual(user.role, self.User.ROLE_USER)

    def test_admin_group_membership_does_not_grant_app_admin(self):
        # Groups and role are deliberately independent.
        user = self.User.objects.create_user(
            username='grouped', email='g@example.com', password=VALID_PASSWORD,
        )
        user.groups.add(Group.objects.get(name='Admin'))
        self.assertEqual(self.me(user).data['role'], self.User.ROLE_USER)

    def test_registration_reports_the_plain_user_role(self):
        response = self.register(username='fresh')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['role'], self.User.ROLE_USER)

    def test_django_admin_exposes_the_role_field(self):
        from users.admin import CustomUserAdmin

        field_names = [
            field
            for _, options in CustomUserAdmin.fieldsets
            for field in options['fields']
        ]
        self.assertIn('role', field_names)
        self.assertIn('role', CustomUserAdmin.list_display)
        self.assertIn('role', CustomUserAdmin.list_filter)


# ---------------------------------------------------------------------------
# Admin user-management API (/api/v1/admin/)
# ---------------------------------------------------------------------------


class AdminUserAPITestCase(AuthAPITestCase):
    """Shared fixtures: an Admin-group member, a superuser and a plain user.

    ``admin_member`` holds the Django permissions via the Admin group but is
    deliberately NOT a superuser, so the tests prove authorization comes from
    the group/permission model - never from ``role`` or a flag.
    """

    def setUp(self):
        super().setUp()
        self.list_url = reverse('admin_api:user-list')
        self.groups_url = reverse('admin_api:group-list')

        self.superuser = self.User.objects.create_superuser(
            username='root', email='root@example.com', password=VALID_PASSWORD,
        )
        self.admin_member = self.User.objects.create_user(
            username='siteadmin', email='admin@example.com', password=VALID_PASSWORD,
        )
        self.admin_member.groups.add(Group.objects.get(name='Admin'))
        self.ordinary = self.User.objects.create_user(
            username='pleb', email='pleb@example.com', password=VALID_PASSWORD,
        )
        self.ordinary.groups.add(Group.objects.get(name='User'))

    def detail_url(self, user):
        return reverse('admin_api:user-detail', args=[user.pk])

    def status_url(self, user):
        return reverse('admin_api:user-status', args=[user.pk])

    def groups_for_url(self, user):
        return reverse('admin_api:user-groups', args=[user.pk])


class AdminUserAuthTests(AdminUserAPITestCase):
    """Authentication and permission gates."""

    def test_list_requires_authentication(self):
        self.assertEqual(
            self.client.get(self.list_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_plain_user_is_forbidden_from_list(self):
        self.assertEqual(
            self.client.get(self.list_url, **self.auth_header_for(self.ordinary)).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_plain_user_cannot_create(self):
        response = self.client.post(
            self.list_url,
            {'username': 'x', 'password': VALID_PASSWORD},
            format='json',
            **self.auth_header_for(self.ordinary),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_role_admin_without_permissions_is_still_forbidden(self):
        # `role='admin'` alone must grant nothing: Django permissions decide.
        impostor = self.User.objects.create_user(
            username='impostor', email='i@example.com', password=VALID_PASSWORD,
            role=self.User.ROLE_ADMIN,
        )
        response = self.client.get(self.list_url, **self.auth_header_for(impostor))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_group_member_can_list(self):
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_superuser_can_list(self):
        response = self.client.get(self.list_url, **self.auth_header_for(self.superuser))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AdminUserListTests(AdminUserAPITestCase):
    def test_response_is_paginated_with_summary(self):
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ('count', 'next', 'previous', 'summary', 'results'):
            self.assertIn(key, response.data)
        for key in ('total', 'active', 'suspended', 'banned', 'deleted'):
            self.assertIn(key, response.data['summary'])

    def test_results_never_expose_password(self):
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        self.assertTrue(response.data['results'])
        for row in response.data['results']:
            self.assertNotIn('password', row)
            self.assertNotIn('user_permissions', row)

    def test_page_size_parameter_is_honoured(self):
        for i in range(30):
            self.User.objects.create_user(
                username=f'pageuser{i}', email=f'p{i}@example.com',
                password=VALID_PASSWORD,
            )
        response = self.client.get(
            self.list_url, {'page_size': 5}, **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(len(response.data['results']), 5)
        self.assertIsNotNone(response.data['next'])

    def test_search_filters_by_username(self):
        response = self.client.get(
            self.list_url, {'search': 'pleb'}, **self.auth_header_for(self.admin_member),
        )
        usernames = [row['username'] for row in response.data['results']]
        self.assertEqual(usernames, ['pleb'])

    def test_search_filters_by_email(self):
        response = self.client.get(
            self.list_url, {'search': 'admin@example.com'},
            **self.auth_header_for(self.admin_member),
        )
        usernames = [row['username'] for row in response.data['results']]
        self.assertIn('siteadmin', usernames)

    def test_each_row_carries_status_and_groups(self):
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        row = next(r for r in response.data['results'] if r['username'] == 'pleb')
        self.assertEqual(row['status'], self.User.STATUS_ACTIVE)
        self.assertEqual([g['name'] for g in row['groups']], ['User'])


class AdminUserCreateTests(AdminUserAPITestCase):
    def payload(self, **overrides):
        data = {
            'username': 'newcomer',
            'email': 'new@example.com',
            'first_name': 'New',
            'last_name': 'Comer',
            'password': VALID_PASSWORD,
        }
        data.update(overrides)
        return data

    def test_create_returns_201_and_hashes_password(self):
        response = self.client.post(
            self.list_url, self.payload(), format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('password', response.data)

        user = self.User.objects.get(username='newcomer')
        self.assertTrue(user.check_password(VALID_PASSWORD))
        self.assertNotIn(VALID_PASSWORD, user.password)

    def test_new_admin_created_user_joins_the_default_group(self):
        self.client.post(
            self.list_url, self.payload(), format='json',
            **self.auth_header_for(self.admin_member),
        )
        user = self.User.objects.get(username='newcomer')
        self.assertEqual([g.name for g in user.groups.all()], ['User'])

    def test_create_ignores_privilege_fields(self):
        response = self.client.post(
            self.list_url,
            self.payload(role='admin', is_staff=True, is_superuser=True,
                         groups=[Group.objects.get(name='Admin').pk],
                         user_permissions=[1]),
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = self.User.objects.get(username='newcomer')
        self.assertEqual(user.role, self.User.ROLE_USER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.filter(name='Admin').exists())

    def test_duplicate_username_is_rejected(self):
        response = self.client.post(
            self.list_url, self.payload(username='pleb'), format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('username', response.data)

    def test_weak_password_is_rejected(self):
        response = self.client.post(
            self.list_url, self.payload(password='123'), format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password', response.data)


class AdminUserDetailTests(AdminUserAPITestCase):
    def test_retrieve_returns_the_user(self):
        response = self.client.get(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'pleb')
        self.assertNotIn('password', response.data)

    def test_unknown_user_is_404(self):
        response = self.client.get(
            reverse('admin_api:user-detail', args=[999999]),
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_patch_updates_profile_fields(self):
        response = self.client.patch(
            self.detail_url(self.ordinary),
            {'first_name': 'Ada', 'last_name': 'Lovelace', 'email': 'ada@example.com'},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ordinary.refresh_from_db()
        self.assertEqual(self.ordinary.first_name, 'Ada')
        self.assertEqual(self.ordinary.email, 'ada@example.com')

    def test_patch_cannot_escalate_role_or_superuser(self):
        response = self.client.patch(
            self.detail_url(self.ordinary),
            {'role': 'admin', 'is_superuser': True, 'is_staff': True},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ordinary.refresh_from_db()
        self.assertEqual(self.ordinary.role, self.User.ROLE_USER)
        self.assertFalse(self.ordinary.is_superuser)
        self.assertFalse(self.ordinary.is_staff)

    def test_admin_member_cannot_modify_a_superuser(self):
        response = self.client.patch(
            self.detail_url(self.superuser), {'first_name': 'Hacked'},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_can_modify_another_superuser(self):
        second = self.User.objects.create_superuser(
            username='root2', email='root2@example.com', password=VALID_PASSWORD,
        )
        response = self.client.patch(
            self.detail_url(second), {'first_name': 'Root'},
            format='json',
            **self.auth_header_for(self.superuser),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second.refresh_from_db()
        self.assertEqual(second.first_name, 'Root')


class AdminUserStatusTests(AdminUserAPITestCase):
    def test_suspend_sets_status_and_blocks_login(self):
        response = self.client.patch(
            self.status_url(self.ordinary),
            {'status': 'suspended', 'status_note': 'spam'},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ordinary.refresh_from_db()
        self.assertEqual(self.ordinary.status, self.User.STATUS_SUSPENDED)
        self.assertEqual(self.ordinary.status_note, 'spam')
        self.assertFalse(self.ordinary.is_active)

    def test_reactivate_clears_inactive_flag(self):
        self.client.patch(
            self.status_url(self.ordinary), {'status': 'banned'}, format='json',
            **self.auth_header_for(self.admin_member),
        )
        response = self.client.patch(
            self.status_url(self.ordinary), {'status': 'active'}, format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ordinary.refresh_from_db()
        self.assertEqual(self.ordinary.status, self.User.STATUS_ACTIVE)
        self.assertTrue(self.ordinary.is_active)

    def test_invalid_status_is_rejected(self):
        response = self.client.patch(
            self.status_url(self.ordinary), {'status': 'nonsense'}, format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_deactivate_own_account(self):
        response = self.client.patch(
            self.status_url(self.admin_member), {'status': 'suspended'}, format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_member_cannot_change_a_superuser(self):
        response = self.client.patch(
            self.status_url(self.superuser), {'status': 'banned'}, format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminUserGroupsTests(AdminUserAPITestCase):
    def test_assign_groups_replaces_membership(self):
        admin_group = Group.objects.get(name='Admin')
        response = self.client.patch(
            self.groups_for_url(self.ordinary),
            {'groups': [admin_group.pk]},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([g.name for g in self.ordinary.groups.all()], ['Admin'])

    def test_unknown_group_id_is_rejected(self):
        response = self.client.patch(
            self.groups_for_url(self.ordinary),
            {'groups': [999999]},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plain_user_without_group_permission_is_forbidden(self):
        # `ordinary` has users.view_user? No - a plain member holds nothing.
        response = self.client.patch(
            self.groups_for_url(self.ordinary),
            {'groups': []},
            format='json',
            **self.auth_header_for(self.ordinary),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_member_cannot_change_a_superuser_groups(self):
        response = self.client.patch(
            self.groups_for_url(self.superuser),
            {'groups': [Group.objects.get(name='User').pk]},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_can_change_groups(self):
        response = self.client.patch(
            self.groups_for_url(self.ordinary),
            {'groups': [Group.objects.get(name='Admin').pk]},
            format='json',
            **self.auth_header_for(self.superuser),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AdminUserDeleteTests(AdminUserAPITestCase):
    def test_delete_is_soft_and_hides_the_user(self):
        response = self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Gone from the default manager (and therefore lists, login, JWT)...
        self.assertFalse(self.User.objects.filter(username='pleb').exists())
        # ...but still in the database, marked as deleted.
        deleted = self.User.all_objects.get(username='pleb')
        self.assertIsNotNone(deleted.deleted_at)
        self.assertFalse(deleted.is_active)

    def test_deleted_user_cannot_log_in(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.client.post(
            self.login_url,
            {'username': 'pleb', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deleted_user_detail_is_404(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.client.get(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleted_username_stays_reserved(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.register(username='pleb', email='other@example.com')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('username', response.data)

    def test_default_list_excludes_deleted_users(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        usernames = [row['username'] for row in response.data['results']]
        self.assertNotIn('pleb', usernames)

    def test_cannot_delete_own_account(self):
        response = self.client.delete(
            self.detail_url(self.admin_member), **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self.User.objects.filter(username='siteadmin').exists())

    def test_admin_member_cannot_delete_a_superuser(self):
        response = self.client.delete(
            self.detail_url(self.superuser), **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(self.User.all_objects.filter(username='root').exists())

    def test_superuser_can_soft_delete_another_user(self):
        response = self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.superuser),
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(self.User.all_objects.filter(username='pleb', deleted_at__isnull=False).exists())


class AdminUserRestoreTests(AdminUserAPITestCase):
    def restore(self, user, user_headers):
        return self.client.post(
            reverse('admin_api:user-restore', args=[user.pk]),
            {},
            format='json',
            **user_headers,
        )

    def soft_delete(self, user):
        self.client.delete(
            self.detail_url(user), **self.auth_header_for(self.admin_member),
        )

    def test_restore_readds_the_user(self):
        self.soft_delete(self.ordinary)
        response = self.restore(self.ordinary, self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], self.User.STATUS_ACTIVE)

        restored = self.User.all_objects.get(username='pleb')
        self.assertIsNone(restored.deleted_at)
        self.assertTrue(restored.is_active)
        self.assertTrue(self.User.objects.filter(username='pleb').exists())

    def test_restored_user_can_log_in_again(self):
        self.soft_delete(self.ordinary)
        self.restore(self.ordinary, self.auth_header_for(self.admin_member))
        response = self.client.post(
            self.login_url,
            {'username': 'pleb', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

    def test_restore_clears_the_status_note(self):
        self.ordinary.status = self.User.STATUS_BANNED
        self.ordinary.status_note = 'old note'
        self.ordinary.save()
        self.soft_delete(self.ordinary)
        response = self.restore(self.ordinary, self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ordinary.refresh_from_db()
        self.assertEqual(self.ordinary.status, self.User.STATUS_ACTIVE)
        self.assertEqual(self.ordinary.status_note, '')

    def test_restore_unknown_user_is_404(self):
        response = self.client.post(
            reverse('admin_api:user-restore', args=[999999]),
            {},
            format='json',
            **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_restore_a_live_account(self):
        response = self.restore(self.ordinary, self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plain_user_cannot_restore(self):
        # The deleted account can no longer authenticate, so use a live plain user.
        bystander = self.User.objects.create_user(
            username='bystander', email='bystander@example.com', password=VALID_PASSWORD,
        )
        self.soft_delete(self.ordinary)
        response = self.restore(self.ordinary, self.auth_header_for(bystander))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_member_cannot_restore_a_superuser(self):
        doomed = self.User.objects.create_superuser(
            username='doomed', email='doomed@example.com', password=VALID_PASSWORD,
        )
        # Only a superuser can delete another superuser.
        self.client.delete(
            self.detail_url(doomed), **self.auth_header_for(self.superuser),
        )
        response = self.restore(doomed, self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminDeletedListTests(AdminUserAPITestCase):
    def test_deleted_view_lists_only_deleted_users(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.client.get(
            self.list_url, {'deleted': 'true'}, **self.auth_header_for(self.admin_member),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [row['username'] for row in response.data['results']]
        self.assertIn('pleb', usernames)
        self.assertNotIn('siteadmin', usernames)

    def test_deleted_view_excludes_live_users_and_counts_them(self):
        response = self.client.get(
            self.list_url, {'deleted': 'true'},
            **self.auth_header_for(self.admin_member),
        )
        usernames = [row['username'] for row in response.data['results']]
        self.assertNotIn('siteadmin', usernames)
        self.assertEqual(response.data['summary']['total'], 0)

    def test_deleted_summary_count_is_global_in_the_active_view(self):
        self.client.delete(
            self.detail_url(self.ordinary), **self.auth_header_for(self.admin_member),
        )
        response = self.client.get(self.list_url, **self.auth_header_for(self.admin_member))
        self.assertEqual(response.data['summary']['deleted'], 1)


class AdminGroupListTests(AdminUserAPITestCase):
    def test_requires_authentication(self):
        self.assertEqual(
            self.client.get(self.groups_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_plain_user_is_forbidden(self):
        response = self.client.get(self.groups_url, **self.auth_header_for(self.ordinary))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_member_sees_the_groups(self):
        response = self.client.get(self.groups_url, **self.auth_header_for(self.admin_member))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [g['name'] for g in response.data]
        self.assertIn('Admin', names)
        self.assertIn('User', names)


# ---------------------------------------------------------------------------
# Editable profile: persistence, authorization, validation, partial updates
# ---------------------------------------------------------------------------


def make_png_file(name='avatar.png', size=(8, 8)):
    """A real, tiny PNG the ImageField/Pillow will accept."""
    buffer = BytesIO()
    Image.new('RGB', size, (12, 34, 56)).save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


def make_large_png_file(name='large.png'):
    """A genuine PNG that exceeds the 2 MB avatar limit.

    Random pixels barely compress, so a 1000x1000 RGB image reliably exceeds
    the limit while still being a valid image Pillow opens successfully.
    """
    image = Image.frombytes('RGB', (1000, 1000), os.urandom(1000 * 1000 * 3))
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


class ProfileModelFieldTests(TestCase):
    def test_new_profile_fields_default_to_blank(self):
        user = get_user_model().objects.create_user(
            username='blank', email='blank@example.com', password=VALID_PASSWORD,
        )
        self.assertEqual(user.display_name, '')
        self.assertEqual(user.website, '')
        self.assertEqual(user.twitter, '')
        self.assertEqual(user.github, '')


class ProfileUpdateTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )
        self.onboarding_payload = {
            'skillLevel': 'beginner',
            'teamInterest': 'both',
            'focusAreas': ['OSINT'],
            'purpose': 'competitive',
            'experienceType': 'student',
        }

    def patch(self, payload, user=None):
        return self.client.patch(
            self.me_url, payload, format='json',
            **self.auth_header_for(user or self.user),
        )

    # -- persistence -----------------------------------------------------
    def test_update_persists_across_a_fresh_read(self):
        response = self.patch({
            'display_name': 'Om the Great',
            'bio': 'CTF enthusiast',
            'website': 'https://om.example.com',
            'twitter': '@om',
            'github': 'om',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, 'Om the Great')
        self.assertEqual(self.user.bio, 'CTF enthusiast')
        self.assertEqual(self.user.website, 'https://om.example.com')
        self.assertEqual(self.user.twitter, '@om')
        self.assertEqual(self.user.github, 'om')

        # The response echoes the saved values...
        self.assertEqual(response.data['display_name'], 'Om the Great')
        self.assertEqual(response.data['website'], 'https://om.example.com')
        # ...and so does a completely fresh GET (the reload case).
        again = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertEqual(again.data['bio'], 'CTF enthusiast')
        self.assertEqual(again.data['github'], 'om')

    def test_display_name_is_trimmed(self):
        self.patch({'display_name': '  Padded  '})
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, 'Padded')

    # -- authorization ---------------------------------------------------
    def test_anonymous_update_is_rejected(self):
        response = self.client.patch(self.me_url, {'bio': 'sneaky'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.user.refresh_from_db()
        self.assertEqual(self.user.bio, '')

    def test_update_only_affects_the_calling_user(self):
        other = self.User.objects.create_user(
            username='other', email='other@example.com', password=VALID_PASSWORD,
        )
        self.patch({'bio': 'mine'})
        other.refresh_from_db()
        self.assertEqual(other.bio, '')

    def test_privileged_fields_cannot_be_changed(self):
        response = self.patch({
            'role': 'admin',
            'points': 999999,
            'status': 'banned',
            'is_active': False,
            'is_staff': True,
            'is_superuser': True,
            'onboardingCompleted': True,
            'username': 'hacked',
            'email': 'attacker@example.com',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, self.User.ROLE_USER)
        self.assertEqual(self.user.points, 0)
        self.assertEqual(self.user.status, self.User.STATUS_ACTIVE)
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.onboardingCompleted)
        self.assertEqual(self.user.username, 'om')
        self.assertEqual(self.user.email, 'om@example.com')

    # -- partial updates -------------------------------------------------
    def test_partial_update_leaves_other_fields_untouched(self):
        self.patch({
            'display_name': 'Om',
            'website': 'https://a.example.com',
            'twitter': '@a',
        })
        response = self.patch({'bio': 'only the bio changed'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.bio, 'only the bio changed')
        self.assertEqual(self.user.display_name, 'Om')
        self.assertEqual(self.user.website, 'https://a.example.com')
        self.assertEqual(self.user.twitter, '@a')

    def test_update_does_not_clear_onboarding_answers(self):
        self.client.post(
            self.onboarding_url, self.onboarding_payload, format='json',
            **self.auth_header_for(self.user),
        )
        self.patch({'bio': 'still here'})
        self.user.refresh_from_db()
        self.assertTrue(self.user.onboardingCompleted)
        self.assertEqual(self.user.onboarding['skillLevel'], 'beginner')
        self.assertEqual(self.user.onboarding['focusAreas'], ['OSINT'])

    # -- validation ------------------------------------------------------
    def test_invalid_website_url_is_rejected(self):
        response = self.patch({'website': 'not a url'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('website', response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.website, '')

    def test_blank_display_name_is_rejected(self):
        response = self.patch({'display_name': '   '})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('display_name', response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, '')

    def test_overlong_bio_is_rejected(self):
        response = self.patch({'bio': 'x' * 501})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('bio', response.data)

    def test_overlong_display_name_is_rejected(self):
        response = self.patch({'display_name': 'x' * 81})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('display_name', response.data)

    # -- method surface --------------------------------------------------
    def test_put_is_not_allowed(self):
        response = self.client.put(
            self.me_url, {'bio': 'x'}, format='json',
            **self.auth_header_for(self.user),
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class AvatarUploadTests(AuthAPITestCase):
    """Avatar upload, replacement, deletion and file cleanup."""

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix='ctf-avatars-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self._override = override_settings(MEDIA_ROOT=self.media_root)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )
        self.avatar_url = reverse('auth:me-avatar')

    def upload(self, file, user=None):
        return self.client.patch(
            self.me_url, {'avatar': file}, format='multipart',
            **self.auth_header_for(user or self.user),
        )

    def test_valid_image_is_stored_and_url_returned(self):
        response = self.upload(make_png_file('me.png'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertTrue(self.user.avatar)
        self.assertTrue(self.user.avatar.name.startswith('avatars/'))
        self.assertTrue(os.path.exists(self.user.avatar.path))
        self.assertIn('/media/avatars/', response.data['avatar'])

    def test_avatar_survives_a_fresh_read(self):
        self.upload(make_png_file())
        fresh = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertTrue(fresh.data['avatar'])
        self.assertIn('/media/', fresh.data['avatar'])

    def test_replacing_the_avatar_deletes_the_old_file(self):
        self.upload(make_png_file('first.png'))
        self.user.refresh_from_db()
        old_name, old_path = self.user.avatar.name, self.user.avatar.path
        self.assertTrue(os.path.exists(old_path))

        self.upload(make_png_file('second.png'))
        self.user.refresh_from_db()

        self.assertNotEqual(self.user.avatar.name, old_name)
        self.assertFalse(os.path.exists(old_path), 'old avatar file should be removed')
        self.assertTrue(os.path.exists(self.user.avatar.path))

    def test_failed_replacement_keeps_the_existing_avatar(self):
        self.upload(make_png_file('keep.png'))
        self.user.refresh_from_db()
        old_name, old_path = self.user.avatar.name, self.user.avatar.path

        response = self.upload(
            SimpleUploadedFile('bad.txt', b'not an image', content_type='text/plain'),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar.name, old_name)
        self.assertTrue(os.path.exists(old_path))

    def test_delete_endpoint_removes_file_and_clears_field(self):
        self.upload(make_png_file())
        self.user.refresh_from_db()
        path = self.user.avatar.path

        response = self.client.delete(
            self.avatar_url, **self.auth_header_for(self.user),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)
        self.assertIsNone(response.data['avatar'])
        self.assertFalse(os.path.exists(path))

    def test_patching_avatar_null_clears_it(self):
        self.upload(make_png_file())
        self.user.refresh_from_db()
        path = self.user.avatar.path

        response = self.client.patch(
            self.me_url, {'avatar': None}, format='json',
            **self.auth_header_for(self.user),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)
        self.assertFalse(os.path.exists(path))

    def test_oversized_image_is_rejected(self):
        response = self.upload(make_large_png_file())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('avatar', response.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_non_image_upload_is_rejected(self):
        response = self.upload(
            SimpleUploadedFile('notes.txt', b'just text', content_type='text/plain'),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('avatar', response.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_malformed_image_is_rejected(self):
        response = self.upload(
            SimpleUploadedFile('broken.png', b'not really a png', content_type='image/png'),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('avatar', response.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_anonymous_upload_is_rejected(self):
        response = self.client.patch(
            self.me_url, {'avatar': make_png_file()}, format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_upload_only_affects_the_calling_user(self):
        other = self.User.objects.create_user(
            username='other', email='other@example.com', password=VALID_PASSWORD,
        )
        self.upload(make_png_file())
        other.refresh_from_db()
        self.assertFalse(other.avatar)

    def test_anonymous_avatar_delete_is_rejected(self):
        response = self.client.delete(self.avatar_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class OnboardingValidationTests(AuthAPITestCase):
    """Malformed questionnaire payloads are rejected without side effects."""

    def setUp(self):
        super().setUp()
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )
        self.valid = {
            'skillLevel': 'beginner',
            'teamInterest': 'both',
            'focusAreas': ['OSINT'],
            'purpose': 'competitive',
            'experienceType': 'student',
        }

    def submit(self, payload):
        return self.client.post(
            self.onboarding_url, payload, format='json',
            **self.auth_header_for(self.user),
        )

    def test_focus_areas_wrong_type_is_rejected(self):
        response = self.submit(dict(self.valid, focusAreas='OSINT'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.onboardingCompleted)

    def test_overlong_answer_is_rejected(self):
        response = self.submit(dict(self.valid, skillLevel='x' * 100))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.onboardingCompleted)


class UserPreferencesModelTests(TestCase):
    """The new preference columns default sensibly for every existing row."""

    def test_defaults_are_safe(self):
        user = get_user_model().objects.create_user(
            username='prefs', email='prefs@example.com', password=VALID_PASSWORD,
        )
        self.assertEqual(user.notification_prefs, {})
        self.assertEqual(
            user.profile_visibility, get_user_model().VISIBILITY_PUBLIC,
        )


class PreferencesEndpointTests(AuthAPITestCase):
    """PATCH /auth/me/preferences/ - persistence, validation and isolation."""

    def setUp(self):
        super().setUp()
        self.url = reverse('auth:me-preferences')
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )

    def patch(self, payload, user=None):
        return self.client.patch(
            self.url, payload, format='json',
            **self.auth_header_for(user or self.user),
        )

    # -- authorization ---------------------------------------------------
    def test_requires_authentication(self):
        response = self.client.patch(
            self.url, {'profile_visibility': 'private'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_only_affects_the_calling_user(self):
        other = self.User.objects.create_user(
            username='other', email='other@example.com', password=VALID_PASSWORD,
        )
        self.patch({
            'notification_prefs': {'rank_changes': True},
            'profile_visibility': 'private',
        })
        other.refresh_from_db()
        self.assertEqual(other.notification_prefs, {})
        self.assertEqual(other.profile_visibility, self.User.VISIBILITY_PUBLIC)

        # The response reflects the caller, never another account.
        mine = self.client.get(self.me_url, **self.auth_header_for(other))
        self.assertEqual(mine.data['profile_visibility'], 'public')

    # -- defaults & persistence -----------------------------------------
    def test_defaults_are_returned_before_anything_is_saved(self):
        response = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertEqual(response.data['profile_visibility'], 'public')
        self.assertEqual(response.data['notification_prefs'], {
            'solves': True,
            'achievements': True,
            'new_challenges': True,
            'events': True,
            'rank_changes': False,
        })

    def test_persists_notification_preferences(self):
        response = self.patch({
            'notification_prefs': {'rank_changes': True, 'events': False},
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['notification_prefs']['rank_changes'])
        self.assertFalse(response.data['notification_prefs']['events'])
        # A flag that was not sent keeps its default rather than being dropped.
        self.assertTrue(response.data['notification_prefs']['solves'])

        self.user.refresh_from_db()
        self.assertTrue(self.user.notification_prefs['rank_changes'])
        self.assertFalse(self.user.notification_prefs['events'])

        # A completely fresh /me/ read (the reload/login case) shows the value.
        again = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertTrue(again.data['notification_prefs']['rank_changes'])

    def test_partial_update_does_not_clear_other_flags(self):
        self.patch({'notification_prefs': {'rank_changes': True}})
        self.patch({'notification_prefs': {'events': False}})
        self.user.refresh_from_db()
        self.assertTrue(self.user.notification_prefs['rank_changes'])
        self.assertFalse(self.user.notification_prefs['events'])

    def test_persists_profile_visibility(self):
        response = self.patch({'profile_visibility': 'private'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['profile_visibility'], 'private')
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_visibility, 'private')
        again = self.client.get(self.me_url, **self.auth_header_for(self.user))
        self.assertEqual(again.data['profile_visibility'], 'private')

    # -- validation ------------------------------------------------------
    def test_unknown_preference_key_is_rejected(self):
        response = self.patch({'notification_prefs': {'telepathy': True}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('notification_prefs', response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.notification_prefs, {})

    def test_non_boolean_preference_value_is_rejected(self):
        response = self.patch({'notification_prefs': {'solves': 'yes'}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.notification_prefs, {})

    def test_non_object_preferences_are_rejected(self):
        for bad in ([], 'solves', 5):
            response = self.patch({'notification_prefs': bad})
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, bad,
            )

    def test_invalid_visibility_is_rejected(self):
        response = self.patch({'profile_visibility': 'invisible'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_visibility, 'public')

    def test_privileged_fields_cannot_be_changed_through_preferences(self):
        response = self.patch({
            'role': 'admin',
            'points': 999999,
            'email': 'attacker@example.com',
            'is_superuser': True,
            'username': 'hacked',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, self.User.ROLE_USER)
        self.assertEqual(self.user.points, 0)
        self.assertNotEqual(self.user.email, 'attacker@example.com')
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.username, 'om')


class ChangePasswordEndpointTests(AuthAPITestCase):
    """POST /auth/password/ - ownership, validation and token revocation."""

    NEW_PASSWORD = 'An0ther-Str0ng!7'

    def setUp(self):
        super().setUp()
        # The throttle cache is process-global and is not rolled back with the
        # database, so clear it to keep each test independent of the last.
        cache.clear()
        self.url = reverse('auth:password-change')
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )

    def change(self, payload, user=None):
        return self.client.post(
            self.url, payload, format='json',
            **self.auth_header_for(user or self.user),
        )

    def payload(self, current=VALID_PASSWORD, new=NEW_PASSWORD, confirm=None):
        return {
            'current_password': current,
            'new_password': new,
            'new_password2': new if confirm is None else confirm,
        }

    # -- authorization ---------------------------------------------------
    def test_requires_authentication(self):
        response = self.client.post(self.url, self.payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_only_affects_the_calling_user(self):
        other = self.User.objects.create_user(
            username='other', email='other@example.com', password=VALID_PASSWORD,
        )
        self.change(self.payload())
        other.refresh_from_db()
        self.assertTrue(other.check_password(VALID_PASSWORD))

    # -- success ---------------------------------------------------------
    def test_changes_the_password(self):
        response = self.change(self.payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.NEW_PASSWORD))
        self.assertIsNone(authenticate(username='om', password=VALID_PASSWORD))
        self.assertIsNotNone(authenticate(username='om', password=self.NEW_PASSWORD))

    def test_response_never_contains_a_password(self):
        response = self.change(self.payload())
        text = response.content.decode()
        self.assertNotIn(self.NEW_PASSWORD, text)
        self.assertNotIn(VALID_PASSWORD, text)
        self.assertNotIn('new_password', text)
        self.assertNotIn('password', response.data)

    # -- validation ------------------------------------------------------
    def test_wrong_current_password_is_rejected(self):
        response = self.change(self.payload(current='not-my-password'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('current_password', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PASSWORD))

    def test_mismatched_confirmation_is_rejected(self):
        response = self.change(self.payload(confirm='D1fferent-Str0ng!7'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password2', response.data)

    def test_weak_new_password_is_rejected(self):
        # Django's NumericPasswordValidator / MinimumLengthValidator reject this.
        response = self.change(self.payload(new='12345678'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PASSWORD))

    def test_reusing_the_current_password_is_rejected(self):
        response = self.change(self.payload(new=VALID_PASSWORD))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password', response.data)

    # -- token behaviour -------------------------------------------------
    def test_blacklists_the_refresh_token_from_login(self):
        login = self.client.post(
            self.login_url,
            {'username': 'om', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK, login.content)
        old_refresh = login.data['refresh']

        # Usable before the change...
        before = self.client.post(
            self.refresh_url, {'refresh': old_refresh}, format='json',
        )
        self.assertEqual(before.status_code, status.HTTP_200_OK, before.content)

        changed = self.change(self.payload())
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.content)

        # ...and revoked afterwards.
        after = self.client.post(
            self.refresh_url, {'refresh': old_refresh}, format='json',
        )
        self.assertEqual(after.status_code, status.HTTP_401_UNAUTHORIZED, after.content)

    def test_blacklists_refresh_tokens_minted_for_the_user(self):
        token = str(RefreshToken.for_user(self.user))
        changed = self.change(self.payload())
        self.assertEqual(changed.status_code, status.HTTP_200_OK)
        after = self.client.post(
            self.refresh_url, {'refresh': token}, format='json',
        )
        self.assertEqual(after.status_code, status.HTTP_401_UNAUTHORIZED)


class LogoutEndpointTests(AuthAPITestCase):
    """POST /auth/logout/ - revokes a refresh token, safely and idempotently."""

    def setUp(self):
        super().setUp()
        self.url = reverse('auth:logout')
        self.user = self.User.objects.create_user(
            username='om', email='om@example.com', password=VALID_PASSWORD,
        )

    def login_refresh(self):
        login = self.client.post(
            self.login_url,
            {'username': 'om', 'password': VALID_PASSWORD},
            format='json',
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK, login.content)
        return login.data['refresh']

    def test_blacklists_a_valid_refresh_token(self):
        token = self.login_refresh()
        response = self.client.post(self.url, {'refresh': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        after = self.client.post(self.refresh_url, {'refresh': token}, format='json')
        self.assertEqual(after.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_does_not_require_authentication(self):
        token = str(RefreshToken.for_user(self.user))
        response = self.client.post(self.url, {'refresh': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_missing_refresh_token_is_safe(self):
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_malformed_refresh_token_is_safe(self):
        response = self.client.post(
            self.url, {'refresh': 'not-a-real-token'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_already_blacklisted_token_is_safe(self):
        token = str(RefreshToken.for_user(self.user))
        first = self.client.post(self.url, {'refresh': token}, format='json')
        second = self.client.post(self.url, {'refresh': token}, format='json')
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)

    def test_logout_token_cannot_refresh_afterwards(self):
        token = str(RefreshToken.for_user(self.user))
        self.client.post(self.url, {'refresh': token}, format='json')
        after = self.client.post(
            self.refresh_url, {'refresh': token}, format='json',
        )
        self.assertEqual(after.status_code, status.HTTP_401_UNAUTHORIZED)

