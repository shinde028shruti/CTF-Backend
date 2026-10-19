"""Tests for the achievements app.

Coverage is organised around the promises the feature makes:

* only the four supported criteria types are accepted, and malformed config,
  non-positive targets and dangling references are rejected;
* an award is created by server-side evaluation from stored solve history only -
  never on a wrong flag, a repeat submission, a hint unlock or a plain page load;
* evaluation is idempotent and only considers active definitions;
* the player API is per-user and cannot be influenced by the request body;
* the admin API reuses the platform's model permissions and never hard-deletes;
* the seeding and backfill commands are idempotent, non-destructive and report
  what they did.
"""

import io

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from challenges.models import Category, Challenge, ChallengeFlag, ChallengeHint
from notifications.models import Notification
from submissions.models import Solve
from submissions.services import award_solve, unlock_hint

from .criteria import describe, progress, validate_config
from .models import Achievement, UserAchievement
from .services import evaluate_achievements, pending_achievements

User = get_user_model()

USER_LIST_URL = '/api/v1/achievements/'
ADMIN_LIST_URL = '/api/v1/admin/achievements/'

ADMIN_PERMS = (
    'achievements.view_achievement',
    'achievements.add_achievement',
    'achievements.change_achievement',
    'achievements.delete_achievement',
    'achievements.view_userachievement',
)

SEED_CATEGORY_NAMES = [
    'Web Exploitation', 'Cryptography', 'Reverse Engineering',
    'Digital Forensics', 'OSINT', 'Networking', 'Binary Exploitation',
    'Mobile Security', 'Steganography', 'Miscellaneous',
]


def admin_detail_url(pk):
    return f'/api/v1/admin/achievements/{pk}/'


def make_challenge(title, category, flag, points=100):
    challenge = Challenge.objects.create(
        title=title,
        category=category,
        difficulty=Challenge.DIFFICULTY_EASY,
        points=points,
        status=Challenge.STATUS_PUBLISHED,
    )
    stored = ChallengeFlag(challenge=challenge)
    stored.set_flag(flag)
    stored.save()
    return challenge


class AchievementFixtureMixin:
    """Two categories, three solved-able challenges, a player and an admin."""

    def setUp(self):
        cache.clear()
        self.web = Category.objects.create(name='Web Exploitation')
        self.crypto = Category.objects.create(name='Cryptography')
        self.web1 = make_challenge('Web One', self.web, 'CTF{web_one}')
        self.web2 = make_challenge('Web Two', self.web, 'CTF{web_two}')
        self.crypto1 = make_challenge('Crypto One', self.crypto, 'CTF{crypto_one}')

        self.player = User.objects.create_user(
            'player', 'player@example.com', 'pass12345'
        )
        self.admin = User.objects.create_user(
            'ach_admin', 'ach_admin@example.com', 'pass12345'
        )
        self._grant(self.admin, *ADMIN_PERMS)
        self.client = APIClient()

    def _grant(self, user, *refs):
        for ref in refs:
            app_label, codename = ref.split('.')
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        return user

    def make_achievement(self, **overrides):
        data = {
            'name': 'Test Badge',
            'criteria_type': Achievement.CRITERIA_FIRST_SOLVE,
            'criteria_config': {},
        }
        data.update(overrides)
        return Achievement.objects.create(**data)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class CriteriaValidationTests(AchievementFixtureMixin, TestCase):
    def test_supported_types_normalise(self):
        self.assertEqual(
            validate_config(Achievement.CRITERIA_FIRST_SOLVE, {}), {}
        )
        self.assertEqual(
            validate_config(Achievement.CRITERIA_TOTAL_SOLVES, {'target': '3'}),
            {'target': 3},
        )
        self.assertEqual(
            validate_config(
                Achievement.CRITERIA_CATEGORY_SOLVES,
                {'category': self.web.pk, 'target': 2},
            ),
            {'category': self.web.pk, 'target': 2},
        )
        self.assertEqual(
            validate_config(
                Achievement.CRITERIA_CHALLENGE_SOLVED,
                {'challenge': self.web1.pk},
            ),
            {'challenge': self.web1.pk},
        )

    def test_unsupported_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_config('arbitrary_expression', {'code': 'award_me()'})

    def test_non_positive_and_non_numeric_targets_rejected(self):
        for bad in (0, -1, 'abc', None, True):
            with self.assertRaises(ValidationError):
                validate_config(Achievement.CRITERIA_TOTAL_SOLVES, {'target': bad})

    def test_missing_target_rejected(self):
        with self.assertRaises(ValidationError):
            validate_config(Achievement.CRITERIA_TOTAL_SOLVES, {})

    def test_missing_category_or_challenge_rejected(self):
        with self.assertRaises(ValidationError):
            validate_config(
                Achievement.CRITERIA_CATEGORY_SOLVES,
                {'category': 999999, 'target': 1},
            )
        with self.assertRaises(ValidationError):
            validate_config(
                Achievement.CRITERIA_CHALLENGE_SOLVED, {'challenge': 999999}
            )

    def test_progress_is_pure_and_correct(self):
        solved = {self.web1.pk, self.web2.pk}
        per_category = {self.web.pk: 2}
        self.assertEqual(
            progress(Achievement.CRITERIA_FIRST_SOLVE, {}, solved, per_category),
            (2, 1),
        )
        self.assertEqual(
            progress(
                Achievement.CRITERIA_TOTAL_SOLVES, {'target': 5}, solved, per_category
            ),
            (2, 5),
        )
        self.assertEqual(
            progress(
                Achievement.CRITERIA_CATEGORY_SOLVES,
                {'category': self.web.pk, 'target': 3},
                solved, per_category,
            ),
            (2, 3),
        )
        self.assertEqual(
            progress(
                Achievement.CRITERIA_CHALLENGE_SOLVED,
                {'challenge': self.web1.pk},
                solved, per_category,
            ),
            (1, 1),
        )
        self.assertEqual(
            progress(
                Achievement.CRITERIA_CHALLENGE_SOLVED,
                {'challenge': self.crypto1.pk},
                solved, per_category,
            ),
            (0, 1),
        )

    def test_describe_uses_batched_names(self):
        label = describe(
            Achievement.CRITERIA_CATEGORY_SOLVES,
            {'category': self.web.pk, 'target': 2},
            {self.web.pk: 'Web Exploitation'},
        )
        self.assertIn('Web Exploitation', label)
        self.assertIn('2', label)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class AchievementModelTests(AchievementFixtureMixin, TestCase):
    def test_slug_is_derived_from_name_and_unique(self):
        first = self.make_achievement(name='First Blood')
        second = self.make_achievement(name='First Blood')
        self.assertEqual(first.slug, 'first-blood')
        self.assertNotEqual(second.slug, first.slug)

    def test_slug_is_unique_against_soft_deleted_rows(self):
        # A soft-deleted row keeps its slug at the database level, so creating a
        # new definition with the same name must move to the next free suffix
        # instead of raising an IntegrityError on the unique constraint.
        deleted = self.make_achievement(name='Welcome')
        deleted.delete()

        recreated = self.make_achievement(name='Welcome')
        self.assertEqual(deleted.slug, 'welcome')
        self.assertEqual(recreated.slug, 'welcome-2')

    def test_delete_is_soft_and_keeps_earned_records(self):
        achievement = self.make_achievement(name='Keeper')
        UserAchievement.objects.create(user=self.player, achievement=achievement)

        achievement.delete()

        # Hidden from the default manager...
        self.assertFalse(
            Achievement.objects.filter(pk=achievement.pk).exists()
        )
        # ...but the row, the debit timestamp and every award survive.
        survivor = Achievement.all_objects.get(pk=achievement.pk)
        self.assertIsNotNone(survivor.deleted_at)
        self.assertFalse(survivor.is_active)
        self.assertTrue(
            UserAchievement.objects.filter(
                user=self.player, achievement_id=achievement.pk
            ).exists()
        )


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class EvaluationServiceTests(AchievementFixtureMixin, TestCase):
    def _solve_directly(self, user, challenge):
        """Insert a Solve row without going through the live hook."""
        Solve.objects.create(user=user, challenge=challenge, points=challenge.points)

    def test_first_solve(self):
        achievement = self.make_achievement(
            name='Welcome', criteria_type=Achievement.CRITERIA_FIRST_SOLVE
        )
        self.assertEqual(pending_achievements(self.player), [])
        self._solve_directly(self.player, self.web1)
        earned = evaluate_achievements(self.player)
        self.assertEqual([r.achievement_id for r in earned], [achievement.pk])

    def test_total_solves_threshold(self):
        self.make_achievement(
            name='Solver', criteria_type=Achievement.CRITERIA_TOTAL_SOLVES,
            criteria_config={'target': 2},
        )
        self._solve_directly(self.player, self.web1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self._solve_directly(self.player, self.crypto1)
        self.assertEqual(len(evaluate_achievements(self.player)), 1)

    def test_category_solves_counts_only_that_category(self):
        achievement = self.make_achievement(
            name='Webbie', criteria_type=Achievement.CRITERIA_CATEGORY_SOLVES,
            criteria_config={'category': self.web.pk, 'target': 2},
        )
        self._solve_directly(self.player, self.crypto1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self._solve_directly(self.player, self.web1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self._solve_directly(self.player, self.web2)
        earned = evaluate_achievements(self.player)
        self.assertEqual([r.achievement_id for r in earned], [achievement.pk])

    def test_challenge_solved_specific(self):
        achievement = self.make_achievement(
            name='One Trick', criteria_type=Achievement.CRITERIA_CHALLENGE_SOLVED,
            criteria_config={'challenge': self.web2.pk},
        )
        self._solve_directly(self.player, self.web1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self._solve_directly(self.player, self.web2)
        earned = evaluate_achievements(self.player)
        self.assertEqual([r.achievement_id for r in earned], [achievement.pk])

    def test_evaluation_is_idempotent(self):
        self.make_achievement(name='Once')
        self._solve_directly(self.player, self.web1)
        self.assertEqual(len(evaluate_achievements(self.player)), 1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self.assertEqual(UserAchievement.objects.filter(user=self.player).count(), 1)

    def test_inactive_and_deleted_definitions_are_never_awarded(self):
        inactive = self.make_achievement(name='Off', is_active=False)
        deleted = self.make_achievement(name='Gone')
        deleted.delete()
        self._solve_directly(self.player, self.web1)
        self.assertEqual(evaluate_achievements(self.player), [])
        self.assertFalse(UserAchievement.objects.filter(achievement=inactive).exists())

    def test_no_definitions_is_a_no_op(self):
        self._solve_directly(self.player, self.web1)
        self.assertEqual(evaluate_achievements(self.player), [])


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class SolveIntegrationTests(AchievementFixtureMixin, TestCase):
    def test_solving_through_award_solve_awards(self):
        achievement = self.make_achievement(name='Welcome')
        created, _ = award_solve(self.player, self.web1)
        self.assertTrue(created)
        self.assertTrue(
            UserAchievement.objects.filter(
                user=self.player, achievement=achievement
            ).exists()
        )

    def test_repeat_solve_awards_nothing_new(self):
        self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        before = UserAchievement.objects.count()
        created, _ = award_solve(self.player, self.web1)
        self.assertFalse(created)
        self.assertEqual(UserAchievement.objects.count(), before)

    def test_http_submit_wrong_flag_awards_nothing(self):
        self.make_achievement(name='Welcome')
        self.client.force_authenticate(user=self.player)
        response = self.client.post(
            f'/api/v1/challenges/{self.web1.pk}/submit/',
            {'flag': 'CTF{nope}'},
            format='json',
        )
        self.assertIn(response.status_code, (200, 400))
        self.assertEqual(UserAchievement.objects.count(), 0)

    def test_http_submit_correct_flag_awards(self):
        self.make_achievement(name='Welcome')
        self.client.force_authenticate(user=self.player)
        response = self.client.post(
            f'/api/v1/challenges/{self.web1.pk}/submit/',
            {'flag': 'CTF{web_one}'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['correct'])
        self.assertEqual(UserAchievement.objects.count(), 1)

    def test_hint_unlock_awards_nothing(self):
        self.make_achievement(name='Welcome')
        hint = ChallengeHint.objects.create(
            challenge=self.web1, text='peek', cost=0, order=0
        )
        unlock_hint(self.player, hint)
        self.assertEqual(UserAchievement.objects.count(), 0)

    def test_unlock_creates_exactly_one_achievement_notification(self):
        self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.player,
                type=Notification.TYPE_ACHIEVEMENT,
            ).count(),
            1,
        )
        # A repeat solve does not add a second unlock notification.
        award_solve(self.player, self.web1)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.player,
                type=Notification.TYPE_ACHIEVEMENT,
            ).count(),
            1,
        )


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class UserAchievementApiTests(AchievementFixtureMixin, TestCase):
    def test_requires_authentication(self):
        response = self.client.get(USER_LIST_URL)
        self.assertIn(response.status_code, (401, 403))

    def test_lists_active_definitions_with_progress_and_earned(self):
        earned = self.make_achievement(
            name='Welcome', criteria_type=Achievement.CRITERIA_FIRST_SOLVE,
            sort_order=0,
        )
        self.make_achievement(
            name='Grinder', criteria_type=Achievement.CRITERIA_TOTAL_SOLVES,
            criteria_config={'target': 10}, sort_order=1,
        )
        award_solve(self.player, self.web1)

        self.client.force_authenticate(user=self.player)
        response = self.client.get(USER_LIST_URL)
        self.assertEqual(response.status_code, 200)
        body = {row['name']: row for row in response.json()}

        self.assertTrue(body['Welcome']['earned'])
        self.assertIsNotNone(body['Welcome']['unlocked_at'])
        self.assertEqual(body['Welcome']['progress'], {'gained': 1, 'target': 1})

        self.assertFalse(body['Grinder']['earned'])
        self.assertIsNone(body['Grinder']['unlocked_at'])
        self.assertEqual(body['Grinder']['progress'], {'gained': 1, 'target': 10})
        self.assertNotIn('criteria_config', body['Welcome'])

    def test_response_is_per_user(self):
        self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)

        other = User.objects.create_user('other', 'other@example.com', 'pass12345')
        self.client.force_authenticate(user=other)
        response = self.client.get(USER_LIST_URL)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['earned'])

    def test_browsing_does_not_create_awards(self):
        self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        before = UserAchievement.objects.count()
        self.client.force_authenticate(user=self.player)
        for _ in range(3):
            self.client.get(USER_LIST_URL)
        self.assertEqual(UserAchievement.objects.count(), before)

    def test_earned_definitions_stay_visible_after_deactivation(self):
        earned = self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        earned.is_active = False
        earned.save(update_fields=['is_active'])

        other = self.make_achievement(name='Other')  # never earned, stays active

        self.client.force_authenticate(user=self.player)
        names = {row['name'] for row in self.client.get(USER_LIST_URL).json()}
        self.assertIn('Welcome', names)
        self.assertIn('Other', names)

        # A different user only sees the still-active one.
        fresh = User.objects.create_user('fresh', 'fresh@example.com', 'pass12345')
        self.client.force_authenticate(user=fresh)
        names = {row['name'] for row in self.client.get(USER_LIST_URL).json()}
        self.assertNotIn('Welcome', names)
        self.assertIn('Other', names)

    def test_earned_definitions_stay_visible_after_soft_delete(self):
        # Deleting a definition must never strip a badge from the collection of
        # a player who already earned it...
        earned = self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        earned.delete()  # soft delete: is_active=False + deleted_at set

        other = self.make_achievement(name='Other')  # never earned, stays active

        self.client.force_authenticate(user=self.player)
        rows = {row['name']: row for row in self.client.get(USER_LIST_URL).json()}
        self.assertIn('Welcome', rows)
        self.assertTrue(rows['Welcome']['earned'])
        self.assertIn('Other', rows)

        # ...while a player who never earned it must not see the deleted rule.
        fresh = User.objects.create_user('fresh', 'fresh@example.com', 'pass12345')
        self.client.force_authenticate(user=fresh)
        names = {row['name'] for row in self.client.get(USER_LIST_URL).json()}
        self.assertNotIn('Welcome', names)
        self.assertIn('Other', names)

    def test_client_cannot_forge_an_award(self):
        self.make_achievement(name='Welcome')
        self.client.force_authenticate(user=self.player)
        # Trying to smuggle an earned flag in the body changes nothing.
        response = self.client.post(
            USER_LIST_URL, {'earned': True, 'progress': {'gained': 99}}, format='json'
        )
        self.assertIn(response.status_code, (404, 405))
        self.assertEqual(UserAchievement.objects.count(), 0)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class AdminAchievementApiTests(AchievementFixtureMixin, TestCase):
    def test_requires_admin_permission(self):
        self.client.force_authenticate(user=self.player)
        response = self.client.get(ADMIN_LIST_URL)
        self.assertEqual(response.status_code, 403)

    def test_anonymous_gets_401(self):
        # Session/JWT auth on this API returns 401 for anonymous requests.
        response = self.client.get(ADMIN_LIST_URL)
        self.assertIn(response.status_code, (401, 403))

    def test_admin_can_list_and_create(self):
        self.client.force_authenticate(user=self.admin)
        payload = {
            'name': 'Fresh Badge',
            'description': 'Solve two web challenges',
            'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
            'criteria_config': {'category': self.web.pk, 'target': 2},
            'icon': 'Globe',
            'gradient': ['#111111', '#222222'],
            'rarity': 'Rare',
            'reward': 10,
            'is_active': True,
        }
        response = self.client.post(ADMIN_LIST_URL, payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['name'], 'Fresh Badge')
        self.assertEqual(body['slug'], 'fresh-badge')
        self.assertEqual(
            body['criteria_config'], {'category': self.web.pk, 'target': 2}
        )
        self.assertIn('Web Exploitation', body['criteria_label'])
        self.assertEqual(body['earned_count'], 0)

        listing = self.client.get(ADMIN_LIST_URL).json()
        self.assertIn('Fresh Badge', {row['name'] for row in listing})

    def test_create_rejects_unsupported_type(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            ADMIN_LIST_URL,
            {'name': 'Bad', 'criteria_type': 'run_python',
             'criteria_config': {'code': 'award()'}},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('criteria_type', response.json())

    def test_create_rejects_bad_config(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            ADMIN_LIST_URL,
            {'name': 'Bad', 'criteria_type': Achievement.CRITERIA_TOTAL_SOLVES,
             'criteria_config': {'target': 0}},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('criteria_config', response.json())

    def test_create_rejects_missing_reference(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            ADMIN_LIST_URL,
            {'name': 'Bad', 'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
             'criteria_config': {'category': 999999, 'target': 1}},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('criteria_config', response.json())

    def test_slug_is_not_client_writable(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            ADMIN_LIST_URL,
            {'name': 'Named Badge', 'slug': 'client-controlled',
             'criteria_type': Achievement.CRITERIA_FIRST_SOLVE},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['slug'], 'named-badge')

    def test_earned_count_reflects_awards(self):
        achievement = self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        self.client.force_authenticate(user=self.admin)
        listing = self.client.get(ADMIN_LIST_URL).json()
        row = next(r for r in listing if r['id'] == achievement.pk)
        self.assertEqual(row['earned_count'], 1)

    def test_patch_updates_presentation_without_touching_rule(self):
        achievement = self.make_achievement(name='Welcome')
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            admin_detail_url(achievement.pk),
            {'name': 'Renamed', 'reward': 999},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        achievement.refresh_from_db()
        self.assertEqual(achievement.name, 'Renamed')
        self.assertEqual(achievement.reward, 999)
        self.assertEqual(achievement.slug, 'welcome')

    def test_criteria_is_frozen_once_earned(self):
        achievement = self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            admin_detail_url(achievement.pk),
            {'criteria_type': Achievement.CRITERIA_TOTAL_SOLVES,
             'criteria_config': {'target': 1}},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('criteria_type', response.json())
        achievement.refresh_from_db()
        self.assertEqual(
            achievement.criteria_type, Achievement.CRITERIA_FIRST_SOLVE
        )

    def test_criteria_can_change_before_any_award(self):
        achievement = self.make_achievement(name='Welcome')
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            admin_detail_url(achievement.pk),
            {'criteria_type': Achievement.CRITERIA_TOTAL_SOLVES,
             'criteria_config': {'target': 4}},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        achievement.refresh_from_db()
        self.assertEqual(achievement.criteria_type, Achievement.CRITERIA_TOTAL_SOLVES)
        self.assertEqual(achievement.criteria_config, {'target': 4})

    def test_delete_unearned_is_a_soft_delete(self):
        achievement = self.make_achievement(name='Welcome')  # never earned

        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(admin_detail_url(achievement.pk))
        self.assertEqual(response.status_code, 204)

        self.assertIn(
            achievement.pk,
            set(Achievement.all_objects.values_list('pk', flat=True)),
        )
        self.assertNotIn(
            achievement.pk,
            set(Achievement.objects.values_list('pk', flat=True)),
        )

        listing = {row['id'] for row in self.client.get(ADMIN_LIST_URL).json()}
        self.assertNotIn(achievement.pk, listing)

    def test_delete_earned_is_refused_and_deactivation_is_offered(self):
        achievement = self.make_achievement(name='Welcome')
        award_solve(self.player, self.web1)  # the definition is now earned

        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(admin_detail_url(achievement.pk))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('earned', response.json()['detail'].lower())

        # Nothing was removed and the definition is still listed.
        achievement.refresh_from_db()
        self.assertIsNone(achievement.deleted_at)
        self.assertTrue(achievement.is_active)
        self.assertTrue(
            UserAchievement.objects.filter(achievement=achievement).exists()
        )
        self.assertIn(
            achievement.pk,
            {row['id'] for row in self.client.get(ADMIN_LIST_URL).json()},
        )

        # Deactivation (PATCH) remains the supported way to hide it.
        patch = self.client.patch(
            admin_detail_url(achievement.pk), {'is_active': False}, format='json'
        )
        self.assertEqual(patch.status_code, 200, patch.content)
        achievement.refresh_from_db()
        self.assertFalse(achievement.is_active)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class BackfillCommandTests(AchievementFixtureMixin, TestCase):
    def _historical_solve(self, user, challenge):
        Solve.objects.create(user=user, challenge=challenge, points=challenge.points)

    def test_backfill_awards_existing_solves_and_is_idempotent(self):
        self.make_achievement(name='Welcome')
        self._historical_solve(self.player, self.web1)

        out = io.StringIO()
        call_command('backfill_achievements', stdout=out)
        self.assertIn('1 award(s) created', out.getvalue())
        self.assertEqual(UserAchievement.objects.filter(user=self.player).count(), 1)

        out = io.StringIO()
        call_command('backfill_achievements', stdout=out)
        self.assertIn('0 award(s) created', out.getvalue())
        self.assertEqual(UserAchievement.objects.filter(user=self.player).count(), 1)

    def test_dry_run_writes_nothing(self):
        self.make_achievement(name='Welcome')
        self._historical_solve(self.player, self.web1)

        out = io.StringIO()
        call_command('backfill_achievements', '--dry-run', stdout=out)
        self.assertIn('would be created', out.getvalue())
        self.assertEqual(UserAchievement.objects.count(), 0)

    def test_username_filter(self):
        self.make_achievement(name='Welcome')
        self._historical_solve(self.player, self.web1)
        other = User.objects.create_user('solver2', 's2@example.com', 'pass12345')
        self._historical_solve(other, self.crypto1)

        call_command('backfill_achievements', '--username', 'player', stdout=io.StringIO())
        self.assertTrue(UserAchievement.objects.filter(user=self.player).exists())
        self.assertFalse(UserAchievement.objects.filter(user=other).exists())


class SeedCommandTests(TestCase):
    def test_seed_creates_builtins_and_is_idempotent(self):
        for index, name in enumerate(SEED_CATEGORY_NAMES):
            Category.objects.create(name=name, order=index)

        out = io.StringIO()
        call_command('seed_achievements', stdout=out)
        self.assertEqual(Achievement.objects.count(), 10)
        self.assertTrue(Achievement.objects.filter(slug='first-blood').exists())

        out = io.StringIO()
        call_command('seed_achievements', stdout=out)
        self.assertEqual(Achievement.objects.count(), 10)
        self.assertIn('0 created, 10 already present', out.getvalue())

    def test_seed_skips_categories_that_do_not_exist(self):
        # No categories at all: every category rule is skipped, but the
        # category-independent ``first_solve`` built-in is still created.
        out = io.StringIO()
        call_command('seed_achievements', stdout=out)
        self.assertEqual(Achievement.objects.count(), 1)
        self.assertTrue(Achievement.objects.filter(slug='first-blood').exists())
        self.assertIn('9 skipped', out.getvalue())

    def test_seed_respects_soft_deleted_definitions(self):
        for index, name in enumerate(SEED_CATEGORY_NAMES):
            Category.objects.create(name=name, order=index)
        call_command('seed_achievements', stdout=io.StringIO())
        first = Achievement.objects.get(slug='first-blood')
        first.delete()

        out = io.StringIO()
        call_command('seed_achievements', stdout=out)
        # The deleted definition is left deleted, not resurrected.
        self.assertEqual(Achievement.objects.count(), 9)
        self.assertIn('soft-deleted', out.getvalue())
