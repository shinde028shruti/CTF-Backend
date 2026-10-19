"""Tests for the notifications app.

Covers the API surface (auth, per-user isolation, idempotency, ordering and
pagination), the validation rules of the creation helper, and the exactly-once
integration with the solve path (a successful new solve notifies once; a failed
or repeated submission never does).
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from challenges.models import Category, Challenge, ChallengeFlag, ChallengeHint

from .models import Notification, validate_destination_url
from .services import notify

User = get_user_model()

CORRECT_FLAG = 'CTF{notifications_are_fun}'
LIST_URL = '/api/v1/notifications/'
UNREAD_URL = '/api/v1/notifications/unread-count/'
MARK_ALL_URL = '/api/v1/notifications/mark-all-read/'


def read_url(pk):
    return f'/api/v1/notifications/{pk}/read/'


def detail_url(pk):
    return f'/api/v1/notifications/{pk}/'


class NotificationApiTests(TestCase):
    """Endpoint behaviour for an authenticated caller."""

    def setUp(self):
        self.alice = User.objects.create_user(
            'alice', 'alice@example.com', 'pass12345'
        )
        self.bob = User.objects.create_user(
            'bob', 'bob@example.com', 'pass12345'
        )
        self.client = APIClient()

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def make(self, recipient, **kwargs):
        # Creation always starts unread (that is the service contract); a test
        # that needs a read row creates it and then marks it read, exactly as
        # the API would.
        read = kwargs.pop('is_read', False)
        kwargs.setdefault('type', Notification.TYPE_SYSTEM)
        kwargs.setdefault('title', 'Hello')
        kwargs.setdefault('message', 'Body')
        notification = notify(recipient, **kwargs)
        if read:
            notification.mark_read()
        return notification

    # ── authentication ───────────────────────────────────────────────────

    def test_all_endpoints_require_authentication(self):
        notification = self.make(self.alice)
        self.client.force_authenticate(user=None)
        checks = [
            self.client.get(LIST_URL),
            self.client.get(UNREAD_URL),
            self.client.post(MARK_ALL_URL, {}, format='json'),
            self.client.patch(read_url(notification.pk), {}, format='json'),
            self.client.delete(detail_url(notification.pk)),
        ]
        for response in checks:
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ── list ─────────────────────────────────────────────────────────────

    def test_list_returns_only_own_notifications_newest_first(self):
        mine_a = self.make(self.alice, title='A')
        mine_b = self.make(self.alice, title='B')
        self.make(self.bob, title='secret')

        self.auth(self.alice)
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results']
        self.assertEqual([r['id'] for r in results], [mine_b.pk, mine_a.pk])
        self.assertEqual([r['title'] for r in results], ['B', 'A'])
        self.assertNotIn(
            'secret', [r['title'] for r in results],
            'another user\'s notification leaked into the list',
        )

    def test_list_is_paginated_with_the_standard_envelope(self):
        for i in range(25):
            self.make(self.alice, title=f'N{i}')

        self.auth(self.alice)
        response = self.client.get(LIST_URL, {'limit': 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ('count', 'next', 'previous', 'results'):
            self.assertIn(key, response.data)
        self.assertEqual(response.data['count'], 25)
        self.assertEqual(len(response.data['results']), 10)
        self.assertIsNotNone(response.data['next'])

    def test_list_serialises_the_closed_field_set(self):
        self.make(self.alice, title='Shape', destination_url='/challenges/1')
        self.auth(self.alice)
        row = self.client.get(LIST_URL).data['results'][0]
        self.assertEqual(
            set(row.keys()),
            {
                'id', 'type', 'title', 'message', 'destination_url',
                'is_read', 'created_at', 'read_at',
            },
        )
        self.assertNotIn('recipient', row)

    # ── unread count ─────────────────────────────────────────────────────

    def test_unread_count_is_scoped_to_the_caller(self):
        self.make(self.alice)
        self.make(self.alice, is_read=True)
        self.make(self.bob)

        self.auth(self.alice)
        response = self.client.get(UNREAD_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'unread_count': 1})

    # ── mark one read ────────────────────────────────────────────────────

    def test_mark_read_sets_flag_and_timestamp(self):
        notification = self.make(self.alice)
        self.assertIsNone(notification.read_at)

        self.auth(self.alice)
        response = self.client.patch(read_url(notification.pk), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['is_read'])
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)

    def test_mark_read_is_idempotent(self):
        notification = self.make(self.alice)
        self.auth(self.alice)

        first = self.client.patch(read_url(notification.pk), {}, format='json')
        second = self.client.patch(read_url(notification.pk), {}, format='json')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data['read_at'], second.data['read_at'])
        self.assertEqual(
            self.client.get(UNREAD_URL).data['unread_count'], 0
        )

    def test_mark_read_ignores_client_supplied_content(self):
        notification = self.make(self.alice, title='Original')
        self.auth(self.alice)

        self.client.patch(
            read_url(notification.pk),
            {
                'title': 'hacked',
                'message': 'hacked',
                'type': Notification.TYPE_SYSTEM,
                'recipient': self.bob.pk,
                'is_read': False,
                'destination_url': 'https://evil.example/',
            },
            format='json',
        )

        notification.refresh_from_db()
        self.assertEqual(notification.title, 'Original')
        self.assertEqual(notification.recipient_id, self.alice.pk)
        self.assertTrue(notification.is_read)

    def test_cannot_read_another_users_notification(self):
        bobs = self.make(self.bob)
        self.auth(self.alice)
        response = self.client.patch(read_url(bobs.pk), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        bobs.refresh_from_db()
        self.assertFalse(bobs.is_read)

    # ── mark all read ────────────────────────────────────────────────────

    def test_mark_all_read_only_affects_the_caller(self):
        self.make(self.alice)
        self.make(self.alice)
        self.make(self.alice, is_read=True)
        bobs = self.make(self.bob)

        self.auth(self.alice)
        response = self.client.post(MARK_ALL_URL, {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)
        self.assertEqual(response.data['unread_count'], 0)
        self.assertEqual(
            self.client.get(UNREAD_URL).data['unread_count'], 0
        )
        bobs.refresh_from_db()
        self.assertFalse(bobs.is_read, 'another user\'s row was modified')

    def test_mark_all_read_is_safe_to_repeat(self):
        self.make(self.alice)
        self.auth(self.alice)
        first = self.client.post(MARK_ALL_URL, {}, format='json')
        second = self.client.post(MARK_ALL_URL, {}, format='json')
        self.assertEqual(first.data['updated'], 1)
        self.assertEqual(second.data['updated'], 0)

    # ── delete ───────────────────────────────────────────────────────────

    def test_delete_own_notification_returns_204_and_updates_count(self):
        notification = self.make(self.alice)
        self.auth(self.alice)

        response = self.client.delete(detail_url(notification.pk))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Notification.objects.filter(pk=notification.pk).exists())
        self.assertEqual(
            self.client.get(UNREAD_URL).data['unread_count'], 0
        )

        # A second delete of the same id is a 404, not a 500.
        again = self.client.delete(detail_url(notification.pk))
        self.assertEqual(again.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_delete_another_users_notification(self):
        bobs = self.make(self.bob)
        self.auth(self.alice)
        response = self.client.delete(detail_url(bobs.pk))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Notification.objects.filter(pk=bobs.pk).exists())


class NotificationServiceTests(TestCase):
    """Validation performed by the creation helper and the model field."""

    def setUp(self):
        self.user = User.objects.create_user(
            'carol', 'carol@example.com', 'pass12345'
        )

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValidationError):
            notify(self.user, type='solve', title='t', message='m')

    def test_rejects_over_long_title_and_message(self):
        with self.assertRaises(ValidationError):
            notify(self.user, title='x' * 121, message='ok')
        with self.assertRaises(ValidationError):
            notify(self.user, title='ok', message='x' * 501)

    def test_rejects_external_or_unsafe_destinations(self):
        for bad in ('https://evil.example/', '//evil.example/',
                    'javascript:alert(1)', 'challenges/1', '/a\\b'):
            with self.assertRaises(ValidationError, msg=bad):
                notify(
                    self.user, title='t', message='m', destination_url=bad,
                )

    def test_accepts_internal_destination_and_blank(self):
        internal = notify(
            self.user, title='t', message='m', destination_url='/challenges/7',
        )
        blank = notify(self.user, title='t', message='m')
        self.assertEqual(internal.destination_url, '/challenges/7')
        self.assertEqual(blank.destination_url, '')

    def test_validator_allows_blank(self):
        # Should not raise.
        validate_destination_url('')
        validate_destination_url('/dashboard')


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
)
class SolveNotificationTests(TestCase):
    """The solve path creates notifications without touching scoring."""

    def setUp(self):
        cache.clear()  # throttle state outlives a test otherwise
        self.category = Category.objects.create(name='Web')
        self.challenge = self._make_challenge('Submit Me', points=150)
        self.user = User.objects.create_user(
            'player', 'player@example.com', 'pass12345'
        )
        self.client = APIClient()

    def _make_challenge(self, title, *, points):
        challenge = Challenge.objects.create(
            title=title,
            category=self.category,
            difficulty=Challenge.DIFFICULTY_EASY,
            points=points,
            status=Challenge.STATUS_PUBLISHED,
        )
        stored = ChallengeFlag(challenge=challenge)
        stored.set_flag(CORRECT_FLAG)
        stored.save()
        return challenge

    def submit(self, flag):
        self.client.force_authenticate(user=self.user)
        return self.client.post(
            f'/api/v1/challenges/{self.challenge.pk}/submit/',
            {'flag': flag},
            format='json',
        )

    def notifications(self):
        return Notification.objects.filter(recipient=self.user)

    def test_successful_solve_creates_exactly_one_notification(self):
        response = self.submit(CORRECT_FLAG)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['correct'])
        self.assertFalse(response.data['alreadySolved'])

        rows = self.notifications()
        self.assertEqual(rows.count(), 1)
        notification = rows.get()
        self.assertEqual(notification.type, Notification.TYPE_CHALLENGE)
        self.assertFalse(notification.is_read)
        self.assertEqual(
            notification.destination_url, f'/challenges/{self.challenge.pk}'
        )
        self.assertIn('Submit Me', notification.message)
        # The flag itself must never be stored in (or leaked by) a notification.
        self.assertNotIn(CORRECT_FLAG, notification.message)
        self.assertNotIn(CORRECT_FLAG, notification.title)

    def test_failed_submission_creates_no_notification(self):
        response = self.submit('CTF{wrong}')
        self.assertFalse(response.data['correct'])
        self.assertEqual(self.notifications().count(), 0)

    def test_repeat_submit_does_not_duplicate_notification_solve_or_points(self):
        from submissions.models import Solve

        self.submit(CORRECT_FLAG)
        self.user.refresh_from_db()
        points_after_first = self.user.points

        response = self.submit(CORRECT_FLAG)
        self.assertTrue(response.data['alreadySolved'])
        self.assertEqual(response.data['points'], 0)

        self.user.refresh_from_db()
        self.assertEqual(self.notifications().count(), 1)
        self.assertEqual(
            Solve.objects.filter(user=self.user, challenge=self.challenge).count(),
            1,
        )
        self.assertEqual(self.user.points, points_after_first)

    def test_hint_reveal_creates_no_solve_notification(self):
        hint = ChallengeHint.objects.create(
            challenge=self.challenge, order=1, cost=0, text='Look closer.',
        )
        # Give the player enough points to reveal a paid hint if needed.
        self.client.force_authenticate(user=self.user)
        self.client.post(
            f'/api/v1/challenges/{self.challenge.pk}/hints/{hint.pk}/reveal/',
            {},
            format='json',
        )
        self.assertEqual(self.notifications().count(), 0)
