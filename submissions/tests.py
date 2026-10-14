"""Tests for flag submission, solve tracking and scoring.

Focus areas: authentication, published-only visibility, input validation,
correct/incorrect handling, one-solve-per-user (including concurrency), the
historical points rule, throttle limits, and that neither the submitted
plaintext nor the stored hash ever appears in a response.
"""

import threading
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from challenges.models import Category, Challenge, ChallengeFlag, ChallengeHint

from .models import HintUnlock, Solve, Submission
from .services import (
    UNLOCK_STATUS_ALREADY,
    UNLOCK_STATUS_INSUFFICIENT,
    UNLOCK_STATUS_UNLOCKED,
    award_solve,
    unlock_hint,
)

User = get_user_model()

CORRECT_FLAG = 'CTF{correct_horse_battery_staple}'
DRAFT_FLAG = 'CTF{draft_flag}'


class SubmissionFixtureMixin:
    """Two categories-free fixtures: a published and a draft challenge."""

    def setUp(self):
        # Throttle state lives in the (non-transactional) cache, so clear it
        # between tests to keep rate-limit assertions deterministic.
        cache.clear()
        self.category = Category.objects.create(name='Web')
        self.challenge = self._make_challenge(
            'Submit Me', points=150, status=Challenge.STATUS_PUBLISHED,
            flag=CORRECT_FLAG,
        )
        self.draft = self._make_challenge(
            'Hidden Draft', points=500, status=Challenge.STATUS_DRAFT,
            flag=DRAFT_FLAG,
        )
        self.user = User.objects.create_user(
            'player', 'player@example.com', 'pass12345'
        )
        self.client = APIClient()

    def _make_challenge(self, title, *, points, status, flag=None):
        challenge = Challenge.objects.create(
            title=title,
            category=self.category,
            difficulty=Challenge.DIFFICULTY_EASY,
            points=points,
            status=status,
        )
        if flag is not None:
            stored = ChallengeFlag(challenge=challenge)
            stored.set_flag(flag)
            stored.save()
        return challenge

    def url(self, challenge_id):
        return f'/api/v1/challenges/{challenge_id}/submit/'

    def submit(self, challenge_id, flag, user=None):
        self.client.force_authenticate(
            user=self.user if user is None else user
        )
        return self.client.post(
            self.url(challenge_id), {'flag': flag}, format='json'
        )


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class FlagSubmissionTests(SubmissionFixtureMixin, TestCase):
    # Flag verification uses Django's password hashers. A cheap hasher keeps
    # this suite fast; the real PBKDF2 path is covered in challenges/tests.py.

    # ── Authentication ────────────────────────────────────────────────

    def test_anonymous_is_rejected(self):
        response = self.client.post(
            self.url(self.challenge.id), {'flag': CORRECT_FLAG}, format='json'
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(Submission.objects.count(), 0)
        self.assertEqual(Solve.objects.count(), 0)

    # ── Correct submissions ───────────────────────────────────────────

    def test_correct_flag_awards_points_and_records_a_solve(self):
        response = self.submit(self.challenge.id, CORRECT_FLAG)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['correct'])
        self.assertFalse(body['alreadySolved'])
        self.assertEqual(body['points'], 150)

        self.assertEqual(
            Solve.objects.filter(user=self.user, challenge=self.challenge).count(),
            1,
        )
        self.user.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(self.user.points, 150)
        self.assertEqual(self.challenge.solves, 1)

    def test_surrounding_whitespace_is_stripped(self):
        response = self.submit(self.challenge.id, f'  {CORRECT_FLAG}\n')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['correct'])

    def test_response_reveals_neither_the_flag_nor_the_hash(self):
        response = self.submit(self.challenge.id, CORRECT_FLAG)
        text = response.content.decode()
        self.assertNotIn(CORRECT_FLAG, text)
        self.assertNotIn(self.challenge.flag.hash, text)
        self.assertNotIn('hash', response.json())

    # ── Incorrect submissions ─────────────────────────────────────────

    def test_incorrect_flag_records_an_attempt_without_a_solve(self):
        response = self.submit(self.challenge.id, 'CTF{nope}')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['correct'])
        self.assertEqual(body['points'], 0)

        self.assertEqual(
            Submission.objects.filter(user=self.user, correct=False).count(), 1
        )
        self.assertFalse(Solve.objects.exists())
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 0)

    def test_challenge_without_a_flag_can_never_be_solved(self):
        bare = self._make_challenge(
            'No Flag', points=10, status=Challenge.STATUS_PUBLISHED
        )
        response = self.submit(bare.id, CORRECT_FLAG)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['correct'])
        self.assertFalse(Solve.objects.exists())

    # ── Input validation ──────────────────────────────────────────────

    def test_empty_flag_is_rejected(self):
        response = self.submit(self.challenge.id, '')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Submission.objects.count(), 0)

    def test_whitespace_only_flag_is_rejected(self):
        response = self.submit(self.challenge.id, '     ')
        self.assertEqual(response.status_code, 400)

    def test_missing_flag_key_is_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.url(self.challenge.id), {}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_oversized_flag_is_rejected(self):
        response = self.submit(self.challenge.id, 'CTF{' + 'a' * 400 + '}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Submission.objects.count(), 0)

    # ── Visibility of unpublished challenges ──────────────────────────

    def test_draft_challenge_is_hidden(self):
        response = self.submit(self.draft.id, DRAFT_FLAG)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Submission.objects.count(), 0)

    def test_unknown_challenge_is_404(self):
        response = self.submit(999999, CORRECT_FLAG)
        self.assertEqual(response.status_code, 404)

    def test_draft_and_unknown_return_the_same_status(self):
        draft = self.submit(self.draft.id, DRAFT_FLAG)
        unknown = self.submit(999999, DRAFT_FLAG)
        self.assertEqual(draft.status_code, unknown.status_code)

    # ── Duplicate / repeated submissions ──────────────────────────────

    def test_repeated_correct_submission_awards_points_once(self):
        first = self.submit(self.challenge.id, CORRECT_FLAG)
        self.assertTrue(first.json()['correct'])
        self.assertFalse(first.json()['alreadySolved'])

        second = self.submit(self.challenge.id, CORRECT_FLAG)
        body = second.json()
        self.assertEqual(second.status_code, 200)
        self.assertTrue(body['correct'])
        self.assertTrue(body['alreadySolved'])
        self.assertEqual(body['points'], 0)

        self.assertEqual(Solve.objects.filter(user=self.user).count(), 1)
        self.user.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(self.user.points, 150)
        self.assertEqual(self.challenge.solves, 1)
        # Both attempts remain in the append-only history.
        self.assertEqual(Submission.objects.filter(user=self.user).count(), 2)

    def test_wrong_then_right_still_awards_once(self):
        self.submit(self.challenge.id, 'CTF{wrong}')
        response = self.submit(self.challenge.id, CORRECT_FLAG)
        self.assertTrue(response.json()['correct'])
        self.assertFalse(response.json()['alreadySolved'])
        self.assertEqual(Solve.objects.filter(user=self.user).count(), 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 150)

    # ── Historical scoring rule ───────────────────────────────────────

    def test_solve_points_are_a_snapshot(self):
        self.submit(self.challenge.id, CORRECT_FLAG)
        self.challenge.points = 999
        self.challenge.save(update_fields=['points'])

        solve = Solve.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(solve.points, 150)
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 150)

    def test_later_solvers_get_the_current_points(self):
        self.submit(self.challenge.id, CORRECT_FLAG)

        self.challenge.points = 999
        self.challenge.save(update_fields=['points'])
        other = User.objects.create_user(
            'late', 'late@example.com', 'pass12345'
        )
        response = self.submit(self.challenge.id, CORRECT_FLAG, user=other)
        self.assertEqual(response.json()['points'], 999)

    # ── Database constraint / idempotent service ──────────────────────

    def test_unique_constraint_blocks_a_second_solve_row(self):
        Solve.objects.create(
            user=self.user, challenge=self.challenge, points=150
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Solve.objects.create(
                    user=self.user, challenge=self.challenge, points=150
                )

    def test_award_solve_is_idempotent(self):
        created_first, solve_first = award_solve(self.user, self.challenge)
        created_second, solve_second = award_solve(self.user, self.challenge)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(solve_first.pk, solve_second.pk)
        self.assertEqual(
            Solve.objects.filter(user=self.user, challenge=self.challenge).count(),
            1,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 150)

    # ── Solved state surfaced to the frontend ─────────────────────────

    def test_public_detail_reports_solved_for_the_solver_only(self):
        anonymous = APIClient().get(
            f'/api/v1/challenges/{self.challenge.id}/'
        )
        self.assertFalse(anonymous.json()['solved'])

        self.submit(self.challenge.id, CORRECT_FLAG)

        other = User.objects.create_user(
            'other', 'other@example.com', 'pass12345'
        )
        client = APIClient()
        client.force_authenticate(user=other)
        self.assertFalse(
            client.get(f'/api/v1/challenges/{self.challenge.id}/').json()['solved']
        )

        client.force_authenticate(user=self.user)
        self.assertTrue(
            client.get(f'/api/v1/challenges/{self.challenge.id}/').json()['solved']
        )

    def test_public_list_reports_solved(self):
        self.submit(self.challenge.id, CORRECT_FLAG)
        client = APIClient()
        client.force_authenticate(user=self.user)
        rows = client.get('/api/v1/challenges/').json()
        row = next(r for r in rows if r['id'] == self.challenge.id)
        self.assertTrue(row['solved'])

    # ── Rate limiting ─────────────────────────────────────────────────

    def test_rate_limit_blocks_after_five_attempts(self):
        for _ in range(5):
            response = self.submit(self.challenge.id, 'CTF{guess}')
            self.assertEqual(response.status_code, 200, response.content)

        blocked = self.submit(self.challenge.id, 'CTF{guess}')
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('detail', blocked.json())

    def test_rate_limit_is_per_user(self):
        for _ in range(6):
            self.submit(self.challenge.id, 'CTF{guess}')

        other = User.objects.create_user(
            'fresh', 'fresh@example.com', 'pass12345'
        )
        response = self.submit(self.challenge.id, 'CTF{guess}', user=other)
        self.assertEqual(response.status_code, 200)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class ConcurrentSolveTests(SubmissionFixtureMixin, TransactionTestCase):
    """Real threads hitting the endpoint at once must still award once."""

    def test_simultaneous_correct_submissions_award_once(self):
        bodies = []
        errors = []

        # SQLite's shared-cache test database can return a table-lock error
        # instead of serialising competing writers, so a worker may fail
        # spuriously. A bounded retry (the submission is idempotent) keeps the
        # test about the *database invariant* rather than SQLite scheduling.
        def worker():
            for _ in range(8):
                client = APIClient()
                client.force_authenticate(user=self.user)
                try:
                    response = client.post(
                        self.url(self.challenge.id),
                        {'flag': CORRECT_FLAG},
                        format='json',
                    )
                    if response.status_code == 200:
                        bodies.append(response.json())
                        break
                except Exception as exc:  # pragma: no cover - backend dependent
                    errors.append(str(exc))
                finally:
                    connection.close()
                time.sleep(0.05)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # The database invariants must hold regardless of scheduling.
        self.assertEqual(
            Solve.objects.filter(user=self.user, challenge=self.challenge).count(),
            1,
        )
        self.user.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(self.user.points, 150)
        self.assertEqual(self.challenge.solves, 1)

        # At most one request may report a brand-new solve.
        awarded = [b for b in bodies if b['correct'] and not b['alreadySolved']]
        self.assertLessEqual(len(awarded), 1)


class HintRevealFixtureMixin:
    """A published challenge with two hints, plus a draft and a foreign hint."""

    def setUp(self):
        cache.clear()
        self.category = Category.objects.create(name='Web')
        self.challenge = Challenge.objects.create(
            title='Hinted', category=self.category,
            difficulty=Challenge.DIFFICULTY_EASY, points=100,
            status=Challenge.STATUS_PUBLISHED,
        )
        self.hint = ChallengeHint.objects.create(
            challenge=self.challenge, text='The secret is 42', cost=30, order=0
        )
        self.free_hint = ChallengeHint.objects.create(
            challenge=self.challenge, text='A free clue', cost=0, order=1
        )
        self.second_hint = ChallengeHint.objects.create(
            challenge=self.challenge, text='Second secret', cost=20, order=2
        )

        self.other_challenge = Challenge.objects.create(
            title='Other', category=self.category,
            difficulty=Challenge.DIFFICULTY_EASY, points=100,
            status=Challenge.STATUS_PUBLISHED,
        )
        self.foreign_hint = ChallengeHint.objects.create(
            challenge=self.other_challenge, text='Foreign secret', cost=10, order=0
        )

        self.draft = Challenge.objects.create(
            title='Draft', category=self.category,
            difficulty=Challenge.DIFFICULTY_EASY, points=100,
            status=Challenge.STATUS_DRAFT,
        )
        self.draft_hint = ChallengeHint.objects.create(
            challenge=self.draft, text='Draft secret', cost=10, order=0
        )

        self.user = User.objects.create_user(
            'player', 'player@example.com', 'pass12345'
        )
        # Give the player a spendable balance to work with.
        self.user.points = 100
        self.user.save(update_fields=['points'])
        self.client = APIClient()

    def url(self, challenge_id=None, hint_id=None):
        challenge_id = self.challenge.id if challenge_id is None else challenge_id
        hint_id = self.hint.id if hint_id is None else hint_id
        return f'/api/v1/challenges/{challenge_id}/hints/{hint_id}/reveal/'

    def reveal(self, hint_id=None, challenge_id=None, user=None, body=None):
        self.client.force_authenticate(
            user=self.user if user is None else user
        )
        return self.client.post(
            self.url(challenge_id, hint_id), body if body is not None else {},
            format='json',
        )

    def balance(self):
        self.user.refresh_from_db()
        return self.user.points


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class HintRevealTests(HintRevealFixtureMixin, TestCase):
    # ── Authentication ────────────────────────────────────────────────

    def test_anonymous_is_rejected(self):
        response = self.client.post(self.url(), {}, format='json')
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(HintUnlock.objects.count(), 0)
        self.assertEqual(self.balance(), 100)

    # ── Successful unlock ─────────────────────────────────────────────

    def test_reveal_deducts_points_once_and_returns_text(self):
        response = self.reveal()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['revealed'])
        self.assertFalse(body['alreadyUnlocked'])
        self.assertEqual(body['hint']['text'], 'The secret is 42')
        self.assertEqual(body['hint']['cost'], 30)
        self.assertEqual(body['hint']['order'], 0)
        self.assertEqual(body['pointsSpent'], 30)
        self.assertEqual(body['remainingPoints'], 70)

        self.assertEqual(HintUnlock.objects.count(), 1)
        unlock = HintUnlock.objects.get()
        self.assertEqual(unlock.user, self.user)
        self.assertEqual(unlock.challenge, self.challenge)
        self.assertEqual(unlock.hint, self.hint)
        self.assertEqual(unlock.cost, 30)
        self.assertEqual(self.balance(), 70)

    def test_repeat_reveal_does_not_charge_again(self):
        first = self.reveal()
        self.assertFalse(first.json()['alreadyUnlocked'])

        second = self.reveal()
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body['alreadyUnlocked'])
        self.assertEqual(body['pointsSpent'], 0)
        self.assertEqual(body['remainingPoints'], 70)
        # Already unlocked, so the text is still returned.
        self.assertEqual(body['hint']['text'], 'The secret is 42')

        self.assertEqual(HintUnlock.objects.count(), 1)
        self.assertEqual(self.balance(), 70)

    def test_zero_cost_hint_is_free_even_at_zero_balance(self):
        self.user.points = 0
        self.user.save(update_fields=['points'])
        response = self.reveal(hint_id=self.free_hint.id)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body['alreadyUnlocked'])
        self.assertEqual(body['pointsSpent'], 0)
        self.assertEqual(body['remainingPoints'], 0)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(HintUnlock.objects.get().cost, 0)

    # ── Insufficient balance ──────────────────────────────────────────

    def test_insufficient_points_is_refused_without_charging(self):
        self.user.points = 10
        self.user.save(update_fields=['points'])
        response = self.reveal()
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body['required'], 30)
        self.assertEqual(body['available'], 10)
        self.assertIn('detail', body)
        self.assertEqual(HintUnlock.objects.count(), 0)
        self.assertEqual(self.balance(), 10)

    def test_balance_never_goes_negative(self):
        self.user.points = 30
        self.user.save(update_fields=['points'])
        self.reveal()  # exactly affordable
        self.assertEqual(self.balance(), 0)
        # A second, paid hint now fails cleanly instead of going negative.
        response = self.reveal(hint_id=self.second_hint.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['required'], 20)
        self.assertEqual(response.json()['available'], 0)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(HintUnlock.objects.count(), 1)

    # ── Invalid / unavailable hints ───────────────────────────────────

    def test_unknown_hint_is_404(self):
        response = self.reveal(hint_id=999999)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(HintUnlock.objects.count(), 0)

    def test_hint_of_another_challenge_is_404(self):
        # The foreign hint exists and is published, but belongs elsewhere.
        response = self.reveal(hint_id=self.foreign_hint.id)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(HintUnlock.objects.count(), 0)

    def test_draft_challenge_hint_is_404(self):
        response = self.reveal(
            challenge_id=self.draft.id, hint_id=self.draft_hint.id
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(HintUnlock.objects.count(), 0)

    def test_draft_and_unknown_challenges_are_indistinguishable(self):
        draft = self.reveal(
            challenge_id=self.draft.id, hint_id=self.draft_hint.id
        )
        unknown = self.reveal(challenge_id=999999, hint_id=self.hint.id)
        self.assertEqual(draft.status_code, unknown.status_code)

    # ── Authoritative values ──────────────────────────────────────────

    def test_cost_snapshot_survives_a_later_price_change(self):
        self.reveal()
        self.hint.cost = 999
        self.hint.save(update_fields=['cost'])

        unlock = HintUnlock.objects.get()
        self.assertEqual(unlock.cost, 30)
        self.assertEqual(self.balance(), 70)

    def test_client_supplied_cost_and_user_are_ignored(self):
        response = self.reveal(body={'cost': 0, 'user': 999999, 'points': 999})
        self.assertEqual(response.status_code, 200, response.content)
        # The real cost is charged to the real user.
        self.assertEqual(response.json()['pointsSpent'], 30)
        self.assertEqual(self.balance(), 70)
        self.assertEqual(HintUnlock.objects.get().user, self.user)

    # ── Hint-text secrecy ─────────────────────────────────────────────

    def test_public_detail_hides_text_until_unlocked(self):
        anon = APIClient().get(f'/api/v1/challenges/{self.challenge.id}/')
        hint = next(
            h for h in anon.json()['hints'] if h['id'] == self.hint.id
        )
        self.assertFalse(hint['revealed'])
        self.assertEqual(hint['text'], '')

        self.reveal()

        self.client.force_authenticate(user=self.user)
        detail = self.client.get(
            f'/api/v1/challenges/{self.challenge.id}/'
        ).json()
        revealed = next(h for h in detail['hints'] if h['id'] == self.hint.id)
        self.assertTrue(revealed['revealed'])
        self.assertEqual(revealed['text'], 'The secret is 42')

    def test_unlocked_text_is_not_shown_to_another_user(self):
        self.reveal()
        other = User.objects.create_user(
            'nosy', 'nosy@example.com', 'pass12345'
        )
        client = APIClient()
        client.force_authenticate(user=other)
        detail = client.get(f'/api/v1/challenges/{self.challenge.id}/').json()
        hint = next(h for h in detail['hints'] if h['id'] == self.hint.id)
        self.assertFalse(hint['revealed'])
        self.assertEqual(hint['text'], '')

    def test_only_the_unlocked_hint_is_revealed(self):
        self.reveal()
        self.client.force_authenticate(user=self.user)
        detail = self.client.get(
            f'/api/v1/challenges/{self.challenge.id}/'
        ).json()
        untouched = next(
            h for h in detail['hints'] if h['id'] == self.free_hint.id
        )
        self.assertFalse(untouched['revealed'])
        self.assertEqual(untouched['text'], '')

    # ── Idempotent service ────────────────────────────────────────────

    def test_unlock_hint_service_reports_each_outcome(self):
        status, unlock, remaining = unlock_hint(self.user, self.hint)
        self.assertEqual(status, UNLOCK_STATUS_UNLOCKED)
        self.assertEqual(remaining, 70)

        status, unlock, remaining = unlock_hint(self.user, self.hint)
        self.assertEqual(status, UNLOCK_STATUS_ALREADY)
        self.assertEqual(remaining, 70)

        self.user.points = 0
        self.user.save(update_fields=['points'])
        status, unlock, remaining = unlock_hint(self.user, self.foreign_hint)
        self.assertEqual(status, UNLOCK_STATUS_INSUFFICIENT)
        self.assertIsNone(unlock)
        self.assertEqual(remaining, 0)
        self.assertEqual(
            HintUnlock.objects.filter(user=self.user).count(), 1
        )


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class ConcurrentHintUnlockTests(HintRevealFixtureMixin, TransactionTestCase):
    """Real threads revealing the same hint must charge exactly once."""

    def setUp(self):
        super().setUp()
        # Plenty of balance so no thread is refused for affordability.
        self.user.points = 1000
        self.user.save(update_fields=['points'])

    def test_simultaneous_reveals_charge_once(self):
        bodies = []
        errors = []

        # See ConcurrentSolveTests: a reveal is idempotent, so a bounded retry
        # absorbs SQLite's shared-cache table-lock errors without weakening the
        # invariant under test (at most one charge).
        def worker():
            for _ in range(8):
                client = APIClient()
                client.force_authenticate(user=self.user)
                try:
                    response = client.post(self.url(), {}, format='json')
                    if response.status_code == 200:
                        bodies.append(response.json())
                        break
                except Exception as exc:  # pragma: no cover - backend dependent
                    errors.append(str(exc))
                finally:
                    connection.close()
                time.sleep(0.05)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            HintUnlock.objects.filter(user=self.user, hint=self.hint).count(), 1
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 970)

        # At most one request may report a brand-new unlock.
        fresh = [b for b in bodies if not b['alreadyUnlocked']]
        self.assertLessEqual(len(fresh), 1)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class NetPointsOnSolveTests(SubmissionFixtureMixin, TestCase):
    """The reward reported on solve is net of hint points already spent.

    Hint costs are still deducted at reveal time, so ``User.points`` is already
    net. These tests pin down the *reported* reward (``netPoints``/``hintCost``)
    that the UI shows, and confirm the credited ``points`` stay gross so the
    balance is never double-charged.
    """

    def _hint(self, *, cost, order=0, text='clue'):
        return ChallengeHint.objects.create(
            challenge=self.challenge, text=text, cost=cost, order=order
        )

    def _fund(self, amount):
        self.user.points = amount
        self.user.save(update_fields=['points'])

    def test_without_hints_net_equals_gross(self):
        body = self.submit(self.challenge.id, CORRECT_FLAG).json()
        self.assertEqual(body['points'], 150)
        self.assertEqual(body['hintCost'], 0)
        self.assertEqual(body['netPoints'], 150)

    def test_net_subtracts_unlocked_hint_cost(self):
        hint = self._hint(cost=30)
        self._fund(100)
        unlock_hint(self.user, hint)  # 100 -> 70
        self.user.refresh_from_db()
        self.assertEqual(self.user.points, 70)

        body = self.submit(self.challenge.id, CORRECT_FLAG).json()
        self.assertEqual(body['points'], 150)     # gross credited to balance
        self.assertEqual(body['hintCost'], 30)
        self.assertEqual(body['netPoints'], 120)  # what the UI reports
        self.assertIn('120', body['message'])

        self.user.refresh_from_db()
        # 70 + 150 == 100 + 150 - 30: charged once, not twice.
        self.assertEqual(self.user.points, 220)

    def test_net_sums_multiple_hints(self):
        self._fund(100)
        unlock_hint(self.user, self._hint(cost=10, order=0))
        unlock_hint(self.user, self._hint(cost=20, order=1))
        unlock_hint(self.user, self._hint(cost=5, order=2))

        body = self.submit(self.challenge.id, CORRECT_FLAG).json()
        self.assertEqual(body['hintCost'], 35)
        self.assertEqual(body['netPoints'], 115)

    def test_net_is_never_negative(self):
        self._fund(500)
        hint = self._hint(cost=400)  # costs more than the challenge (150)
        unlock_hint(self.user, hint)

        body = self.submit(self.challenge.id, CORRECT_FLAG).json()
        self.assertEqual(body['hintCost'], 400)
        self.assertEqual(body['netPoints'], 0)

    def test_already_solved_also_reports_net(self):
        hint = self._hint(cost=30)
        self._fund(100)
        unlock_hint(self.user, hint)
        self.submit(self.challenge.id, CORRECT_FLAG)

        body = self.submit(self.challenge.id, CORRECT_FLAG).json()
        self.assertTrue(body['alreadySolved'])
        self.assertEqual(body['points'], 0)
        self.assertEqual(body['hintCost'], 30)
        self.assertEqual(body['netPoints'], 120)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class LeaderboardTests(TestCase):
    """Public ranking: order, tie-breaks, privacy, pagination and scoping.

    Ranking is a read-only projection of the stored solves/hint unlocks, so the
    fixtures build those rows directly and mirror the denormalised
    ``User.points`` counter the all-time board reads.
    """

    LIST_URL = '/api/v1/leaderboard/'
    ME_URL = '/api/v1/leaderboard/me/'
    PASSWORD = 'pass12345'

    def setUp(self):
        cache.clear()
        self.web = Category.objects.create(name='Web Exploitation', order=1)
        self.crypto = Category.objects.create(name='Cryptography', order=2)
        self.web_one = self._challenge('Web One', self.web, 100)
        self.web_two = self._challenge('Web Two', self.web, 50)
        self.web_three = self._challenge('Web Three', self.web, 50)
        self.crypto_one = self._challenge('Crypto One', self.crypto, 80)
        self.client = APIClient()

    # ── Fixture helpers ───────────────────────────────────────────────

    def _challenge(self, title, category, points, status=None):
        return Challenge.objects.create(
            title=title,
            category=category,
            points=points,
            status=status or Challenge.STATUS_PUBLISHED,
        )

    def _user(self, username, **kwargs):
        return User.objects.create_user(
            username,
            f'{username}@example.com',
            self.PASSWORD,
            onboardingCompleted=True,
            **kwargs,
        )

    def _solve(self, user, challenge, *, created_at=None):
        solve = Solve.objects.create(
            user=user, challenge=challenge, points=challenge.points
        )
        if created_at is not None:
            Solve.objects.filter(pk=solve.pk).update(created_at=created_at)
        return solve

    def _player(self, username, challenges=()):
        """A user who has solved ``challenges`` (points maintained by the service)."""
        user = self._user(username)
        for challenge in challenges:
            award_solve(user, challenge)
        user.refresh_from_db()
        return user

    def _submission(self, user, challenge, correct):
        return Submission.objects.create(
            user=user, challenge=challenge, correct=correct
        )

    def _rows(self, query=''):
        response = self.client.get(f'{self.LIST_URL}{query}')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _usernames(self, body):
        return [row['username'] for row in body['results']]

    # ── Ordering & tie-breaks ─────────────────────────────────────────

    def test_orders_by_points_descending(self):
        self._player('high', [self.web_one])          # 100
        self._player('low', [self.web_two])           # 50
        body = self._rows()
        self.assertEqual(self._usernames(body), ['high', 'low'])
        self.assertEqual(body['results'][0]['rank'], 1)
        self.assertEqual(body['results'][0]['points'], 100)
        self.assertEqual(body['results'][1]['rank'], 2)

    def test_tie_on_points_breaks_by_solved_count(self):
        # 100 points in one solve vs 100 points across two solves.
        self._player('one_big', [self.web_one])
        self._player('two_small', [self.web_two, self.web_three])
        self.assertEqual(
            self._usernames(self._rows()), ['two_small', 'one_big']
        )

    def test_tie_on_points_and_solved_breaks_by_earliest_reach(self):
        early = self._user('early')
        self._solve(
            early, self.web_one, created_at=timezone.now() - timedelta(days=10)
        )
        early.points = 100
        early.save(update_fields=['points'])

        late = self._user('late')
        self._solve(
            late, self.web_one, created_at=timezone.now() - timedelta(days=1)
        )
        late.points = 100
        late.save(update_fields=['points'])

        self.assertEqual(self._usernames(self._rows()), ['early', 'late'])

    def test_hint_spending_reduces_points_and_rank(self):
        self._player('clean', [self.web_one])          # 100, no hints
        spender = self._user('spender')
        award_solve(spender, self.web_one)             # 100
        spender.refresh_from_db()
        hint = ChallengeHint.objects.create(
            challenge=self.web_one, text='clue', cost=30, order=0
        )
        unlock_hint(spender, hint)                     # 100 -> 70
        spender.refresh_from_db()
        self.assertEqual(spender.points, 70)

        body = self._rows()
        self.assertEqual(self._usernames(body), ['clean', 'spender'])
        self.assertEqual(body['results'][1]['points'], 70)

    # ── Duplicate / manipulation resistance ───────────────────────────

    def test_repeated_correct_submissions_count_once(self):
        challenge = self._challenge('Flagged', self.web, 120)
        stored = ChallengeFlag(challenge=challenge)
        stored.set_flag('CTF{dup}')
        stored.save()

        user = self._user('solver')
        self.client.force_authenticate(user=user)
        url = f'/api/v1/challenges/{challenge.id}/submit/'
        first = self.client.post(url, {'flag': 'CTF{dup}'}, format='json')
        second = self.client.post(url, {'flag': 'CTF{dup}'}, format='json')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()['alreadySolved'])

        row = self._rows()['results'][0]
        self.assertEqual(row['solved'], 1)
        self.assertEqual(row['points'], 120)
        user.refresh_from_db()
        self.assertEqual(user.points, 120)

    def test_client_cannot_inject_points_via_query_parameters(self):
        # A caller-supplied points/score is never read: only filters are.
        self._player('player', [self.web_two])         # 50
        body = self._rows('?points=99999&score=99999')
        self.assertEqual(body['results'][0]['points'], 50)

    # ── Accuracy & privacy ────────────────────────────────────────────

    def test_accuracy_is_correct_over_total_submissions(self):
        user = self._user('acc')
        award_solve(user, self.web_one)
        self._submission(user, self.web_one, True)
        self._submission(user, self.web_two, True)
        self._submission(user, self.web_two, False)
        # 2 correct of 3 attempts -> round(66.67) == 67.
        self.assertEqual(self._rows()['results'][0]['accuracy'], 67)

    def test_exposes_only_public_fields(self):
        self._player('privacy', [self.web_one])
        response = self.client.get(self.LIST_URL)
        self.assertEqual(response.status_code, 200)
        row = response.json()['results'][0]

        self.assertEqual(
            set(row.keys()),
            {'id', 'username', 'points', 'solved', 'accuracy', 'meta', 'rank'},
        )
        text = response.content.decode()
        self.assertNotIn('privacy@example.com', text)
        for forbidden in (
            'email', 'password', 'role', 'is_staff', 'is_superuser',
            'onboarding', 'last_login', 'deleted_at', 'status',
        ):
            self.assertNotIn(forbidden, row)

    # ── Pagination ────────────────────────────────────────────────────

    def test_pagination_splits_pages_and_continues_rank(self):
        self._player('p1', [self.web_one])     # 100
        self._player('p2', [self.crypto_one])  # 80
        self._player('p3', [self.web_two])     # 50

        first = self._rows('?limit=2')
        self.assertEqual(first['count'], 3)
        self.assertEqual(len(first['results']), 2)
        self.assertIsNotNone(first['next'])
        self.assertEqual([r['rank'] for r in first['results']], [1, 2])

        second = self._rows('?limit=2&page=2')
        self.assertEqual(len(second['results']), 1)
        self.assertEqual(second['results'][0]['rank'], 3)

    def test_page_size_is_capped(self):
        from rest_framework.request import Request

        from .views import LeaderboardPagination

        self.assertEqual(LeaderboardPagination.page_size, 50)
        self.assertEqual(LeaderboardPagination.max_page_size, 100)

        raw = APIRequestFactory().get(self.LIST_URL, {'limit': 1000})
        request = Request(raw)
        self.assertEqual(LeaderboardPagination().get_page_size(request), 100)

    # ── Access control ────────────────────────────────────────────────

    def test_anonymous_can_read_the_board(self):
        self._player('anon', [self.web_one])
        response = self.client.get(self.LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 1)

    def test_me_requires_authentication(self):
        response = self.client.get(self.ME_URL)
        self.assertIn(response.status_code, (401, 403))

    def test_me_returns_rank_even_when_off_the_page(self):
        self._player('top', [self.web_one])     # 100
        self._player('mid', [self.crypto_one])  # 80
        low = self._player('low', [self.web_two])  # 50

        self.assertEqual(self._usernames(self._rows('?limit=1')), ['top'])

        self.client.force_authenticate(user=low)
        response = self.client.get(self.ME_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['rank'], 3)
        self.assertEqual(body['username'], 'low')
        self.assertEqual(body['points'], 50)

    def test_me_is_null_rank_when_unranked(self):
        user = self._user('nobody')
        self.client.force_authenticate(user=user)
        body = self.client.get(self.ME_URL).json()
        self.assertIsNone(body['rank'])
        self.assertEqual(body['points'], 0)
        self.assertEqual(body['solved'], 0)

    def test_excludes_admin_superuser_suspended_and_deleted(self):
        self._player('player', [self.web_one])

        admin = self._user('boss', role=User.ROLE_ADMIN)
        award_solve(admin, self.web_one)

        root = User.objects.create_superuser('root', 'root@example.com', self.PASSWORD)
        award_solve(root, self.web_one)

        suspended = self._user('suspended')
        award_solve(suspended, self.web_one)
        suspended.is_active = False
        suspended.status = User.STATUS_SUSPENDED
        suspended.save(update_fields=['is_active', 'status'])

        deleted = self._player('deleted', [self.web_one])
        deleted.delete()  # soft delete

        self.assertEqual(self._usernames(self._rows()), ['player'])

    # ── Weekly scope ──────────────────────────────────────────────────

    def test_weekly_board_only_counts_the_last_seven_days(self):
        old = self._user('old')
        self._solve(
            old, self.web_one, created_at=timezone.now() - timedelta(days=20)
        )
        old.points = 100
        old.save(update_fields=['points'])

        fresh = self._user('fresh')
        self._solve(
            fresh, self.crypto_one, created_at=timezone.now() - timedelta(days=1)
        )
        fresh.points = 80
        fresh.save(update_fields=['points'])

        self.assertEqual(set(self._usernames(self._rows())), {'old', 'fresh'})
        week = self._rows('?period=week')
        self.assertEqual(self._usernames(week), ['fresh'])
        self.assertEqual(week['results'][0]['points'], 80)

    def test_weekly_board_nets_out_hints_in_the_window(self):
        user = self._user('weekly')
        self._solve(
            user, self.web_one, created_at=timezone.now() - timedelta(days=1)
        )
        user.points = 100
        user.save(update_fields=['points'])
        hint = ChallengeHint.objects.create(
            challenge=self.web_one, text='clue', cost=30, order=0
        )
        unlock = HintUnlock.objects.create(
            user=user, challenge=self.web_one, hint=hint, cost=30
        )
        HintUnlock.objects.filter(pk=unlock.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )

        week = self._rows('?period=week')
        self.assertEqual(week['results'][0]['points'], 70)

    # ── Category scope ────────────────────────────────────────────────

    def test_category_board_scopes_points_to_that_category(self):
        self._player('webber', [self.web_one])   # web, 100
        self._player('crypt', [self.crypto_one])  # crypto, 80

        web = self._rows(f'?category={self.web.id}')
        self.assertEqual(self._usernames(web), ['webber'])
        self.assertEqual(web['results'][0]['points'], 100)
        self.assertEqual(web['results'][0]['meta'], 'Web Exploitation')

        crypto = self._rows(f'?category={self.crypto.id}')
        self.assertEqual(self._usernames(crypto), ['crypt'])
        self.assertEqual(crypto['results'][0]['points'], 80)

    def test_category_accepts_a_name_and_all_means_everything(self):
        self._player('webber', [self.web_one])
        self.assertEqual(
            self._usernames(self._rows('?category=Web+Exploitation')), ['webber']
        )
        self.assertEqual(self._usernames(self._rows('?category=All')), ['webber'])
        # An unknown category falls back to the all-time board rather than 500.
        self.assertEqual(self._usernames(self._rows('?category=Nope')), ['webber'])

    def test_me_respects_the_category_scope(self):
        self._player('webber', [self.web_one])
        crypt = self._player('crypt', [self.crypto_one])

        self.client.force_authenticate(user=crypt)
        body = self.client.get(f'{self.ME_URL}?category={self.crypto.id}').json()
        self.assertEqual(body['rank'], 1)
        self.assertEqual(body['points'], 80)
        self.assertEqual(body['meta'], 'Cryptography')
