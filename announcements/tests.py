"""Tests for the announcements app.

Covers the public visibility rules (active published only, drafts/future/expired
hidden, window boundaries), the administrative authorization boundary, full CRUD,
validation (title/body/status/priority/date range) and the creator-derivation
rule.
"""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from .models import Announcement

User = get_user_model()

PUBLIC_URL = '/api/v1/announcements/'
ADMIN_URL = '/api/v1/admin/announcements/'

PUBLIC_FIELDS = {
    'id', 'title', 'body', 'priority', 'starts_at', 'ends_at', 'published_at',
}


def detail_url(pk):
    return f'/api/v1/admin/announcements/{pk}/'


class AnnouncementsTestBase(TestCase):
    """Shared fixtures: the real Admin/User groups plus one member of each."""

    @classmethod
    def setUpTestData(cls):
        # Build the platform's actual groups/permissions so the Admin-group
        # member is authorized exactly the way production is.
        call_command('setup_groups', stdout=StringIO())

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            'ann_admin', 'ann_admin@example.com', 'pass12345'
        )
        self.admin.groups.add(Group.objects.get(name='Admin'))
        self.ordinary = User.objects.create_user(
            'ann_user', 'ann_user@example.com', 'pass12345'
        )
        self.ordinary.groups.add(Group.objects.get(name='User'))

    def as_admin(self):
        self.client.force_authenticate(user=self.admin)

    def as_ordinary(self):
        self.client.force_authenticate(user=self.ordinary)

    def as_anonymous(self):
        self.client.force_authenticate(user=None)

    def make(self, **kwargs):
        defaults = {
            'title': 'Hello',
            'body': 'World',
            'status': Announcement.STATUS_PUBLISHED,
        }
        defaults.update(kwargs)
        return Announcement.objects.create(**defaults)


class PublicListingTests(AnnouncementsTestBase):
    """The banner surface: only active published rows, read by anyone."""

    def test_anonymous_visitor_can_read_the_list(self):
        self.make(title='Live')
        self.as_anonymous()
        response = self.client.get(PUBLIC_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['title'] for row in response.data], ['Live'])

    def test_only_active_published_rows_are_returned(self):
        now = timezone.now()
        self.make(title='Live')
        self.make(title='Draft', status=Announcement.STATUS_DRAFT)
        self.make(title='Future', starts_at=now + timedelta(hours=1))
        self.make(title='Expired', ends_at=now - timedelta(hours=1))
        self.as_anonymous()
        response = self.client.get(PUBLIC_URL)
        self.assertEqual([row['title'] for row in response.data], ['Live'])

    def test_draft_never_appears_publicly(self):
        self.make(title='Draft', status=Announcement.STATUS_DRAFT)
        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).data, [])

    def test_future_announcement_is_hidden(self):
        self.make(title='Soon', starts_at=timezone.now() + timedelta(days=1))
        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).data, [])

    def test_expired_announcement_is_hidden(self):
        self.make(title='Over', ends_at=timezone.now() - timedelta(minutes=1))
        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).data, [])

    def test_newest_published_first(self):
        older = self.make(title='Older')
        older.published_at = timezone.now() - timedelta(hours=1)
        older.save(update_fields=['published_at'])
        self.make(title='Newer')
        self.as_anonymous()
        titles = [row['title'] for row in self.client.get(PUBLIC_URL).data]
        self.assertEqual(titles, ['Newer', 'Older'])

    def test_public_payload_omits_admin_only_fields(self):
        self.make(title='Live')
        self.as_anonymous()
        row = self.client.get(PUBLIC_URL).data[0]
        self.assertEqual(set(row), PUBLIC_FIELDS)


class ActiveWindowBoundaryTests(AnnouncementsTestBase):
    """Deterministic boundary checks against a fixed ``now``."""

    def test_start_and_end_boundaries(self):
        now = timezone.now()
        inside = self.make(
            title='Inside',
            starts_at=now - timedelta(seconds=1),
            ends_at=now + timedelta(seconds=1),
        )
        starts_now = self.make(title='StartsNow', starts_at=now)
        ends_now = self.make(title='EndsNow', ends_at=now)

        active = Announcement.objects.active(now=now)
        titles = set(active.values_list('title', flat=True))
        # starts_at is inclusive, ends_at is exclusive.
        self.assertEqual(titles, {'Inside', 'StartsNow'})
        self.assertIn(inside, active)
        self.assertIn(starts_now, active)
        self.assertNotIn(ends_now, active)

    def test_null_window_is_always_active_once_published(self):
        row = self.make(title='Forever')
        self.assertIn(row, Announcement.objects.active())

    def test_unpublished_row_is_never_active(self):
        row = self.make(title='Hidden', status=Announcement.STATUS_DRAFT)
        self.assertNotIn(row, Announcement.objects.active())


class AdminAuthorizationTests(AnnouncementsTestBase):
    """Only the Admin group may use the administrative surface."""

    def test_anonymous_is_rejected(self):
        row = self.make()
        self.as_anonymous()
        responses = [
            self.client.get(ADMIN_URL),
            self.client.post(ADMIN_URL, {'title': 'x'}, format='json'),
            self.client.get(detail_url(row.pk)),
            self.client.patch(detail_url(row.pk), {'title': 'y'}, format='json'),
            self.client.delete(detail_url(row.pk)),
        ]
        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_ordinary_user_is_forbidden(self):
        row = self.make()
        self.as_ordinary()
        responses = [
            self.client.get(ADMIN_URL),
            self.client.post(ADMIN_URL, {'title': 'x'}, format='json'),
            self.client.get(detail_url(row.pk)),
            self.client.patch(detail_url(row.pk), {'title': 'y'}, format='json'),
            self.client.delete(detail_url(row.pk)),
        ]
        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_forbidden_mutations_do_not_change_data(self):
        row = self.make(title='Original')
        self.as_ordinary()
        self.client.patch(detail_url(row.pk), {'title': 'Hacked'}, format='json')
        self.client.delete(detail_url(row.pk))
        row.refresh_from_db()
        self.assertEqual(row.title, 'Original')
        self.assertTrue(Announcement.objects.filter(pk=row.pk).exists())


class AdminCrudTests(AnnouncementsTestBase):
    """An authorized administrator can manage every state."""

    def test_admin_lists_drafts_and_published(self):
        self.make(title='D1', status=Announcement.STATUS_DRAFT)
        self.make(title='P1')
        self.as_admin()
        response = self.client.get(ADMIN_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row['title'] for row in response.data}, {'D1', 'P1'}
        )
        self.assertIn('status', response.data[0])

    def test_admin_can_create_a_draft(self):
        self.as_admin()
        response = self.client.post(
            ADMIN_URL,
            {'title': 'New', 'body': 'Body', 'status': 'draft'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'draft')
        self.assertEqual(response.data['created_by'], self.admin.pk)
        self.assertEqual(response.data['created_by_username'], 'ann_admin')
        self.assertIsNone(response.data['published_at'])
        self.assertTrue(Announcement.objects.filter(title='New').exists())

    def test_creating_published_stamps_published_at(self):
        self.as_admin()
        response = self.client.post(
            ADMIN_URL, {'title': 'Live', 'status': 'published'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data['published_at'])

    def test_creator_is_derived_and_payload_value_is_ignored(self):
        self.as_admin()
        response = self.client.post(
            ADMIN_URL,
            {'title': 'X', 'status': 'draft', 'created_by': self.ordinary.pk},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        row = Announcement.objects.get(pk=response.data['id'])
        self.assertEqual(row.created_by, self.admin)

    def test_admin_retrieves_updates_and_deletes(self):
        row = self.make(title='Before')
        self.as_admin()
        self.assertEqual(
            self.client.get(detail_url(row.pk)).status_code, status.HTTP_200_OK
        )
        patch = self.client.patch(
            detail_url(row.pk), {'title': 'After'}, format='json'
        )
        self.assertEqual(patch.status_code, status.HTTP_200_OK)
        self.assertEqual(patch.data['title'], 'After')
        delete = self.client.delete(detail_url(row.pk))
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Announcement.objects.filter(pk=row.pk).exists())

    def test_missing_record_returns_404(self):
        self.as_admin()
        self.assertEqual(
            self.client.get(detail_url(999999)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.patch(
                detail_url(999999), {'title': 'x'}, format='json'
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.delete(detail_url(999999)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_patch_preserves_omitted_fields(self):
        row = self.make(
            title='Keep', body='Body text', priority='urgent',
            status=Announcement.STATUS_PUBLISHED,
        )
        self.as_admin()
        response = self.client.patch(
            detail_url(row.pk), {'title': 'Changed'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row.refresh_from_db()
        self.assertEqual(row.title, 'Changed')
        self.assertEqual(row.body, 'Body text')
        self.assertEqual(row.priority, 'urgent')
        self.assertEqual(row.status, Announcement.STATUS_PUBLISHED)


class ValidationTests(AnnouncementsTestBase):
    """Bad input is rejected before anything is written."""

    def setUp(self):
        super().setUp()
        self.as_admin()

    def post(self, payload):
        return self.client.post(ADMIN_URL, payload, format='json')

    def test_blank_title_rejected(self):
        self.assertEqual(
            self.post({'title': '   ', 'status': 'draft'}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_missing_title_rejected(self):
        self.assertEqual(
            self.post({'status': 'draft'}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_overlong_title_rejected(self):
        self.assertEqual(
            self.post({'title': 'x' * 121, 'status': 'draft'}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_overlong_body_rejected(self):
        self.assertEqual(
            self.post(
                {'title': 'ok', 'body': 'x' * 2001, 'status': 'draft'}
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_invalid_status_rejected(self):
        self.assertEqual(
            self.post({'title': 'ok', 'status': 'archived'}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_invalid_priority_rejected(self):
        self.assertEqual(
            self.post(
                {'title': 'ok', 'priority': 'shiny', 'status': 'draft'}
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_inverted_window_rejected_on_create(self):
        now = timezone.now()
        response = self.post({
            'title': 'Bad',
            'status': 'published',
            'starts_at': now.isoformat(),
            'ends_at': (now - timedelta(hours=1)).isoformat(),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('ends_at', response.data)

    def test_equal_window_rejected(self):
        now = timezone.now()
        response = self.post({
            'title': 'Bad',
            'status': 'draft',
            'starts_at': now.isoformat(),
            'ends_at': now.isoformat(),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_cannot_invert_window(self):
        now = timezone.now()
        row = self.make(starts_at=now, ends_at=now + timedelta(hours=2))
        response = self.client.patch(
            detail_url(row.pk),
            {'ends_at': (now - timedelta(hours=1)).isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        row.refresh_from_db()
        self.assertGreater(row.ends_at, row.starts_at)

    def test_model_clean_rejects_inverted_window(self):
        now = timezone.now()
        row = Announcement(
            title='Bad', starts_at=now, ends_at=now - timedelta(hours=1)
        )
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_database_constraint_rejects_inverted_window(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Announcement.objects.create(
                    title='Bad',
                    status=Announcement.STATUS_DRAFT,
                    starts_at=now,
                    ends_at=now - timedelta(hours=1),
                )


class PublicationLifecycleTests(AnnouncementsTestBase):
    """Publish / unpublish really changes public visibility."""

    def test_unpublishing_removes_from_the_public_list(self):
        row = self.make(title='Live')
        self.as_anonymous()
        self.assertEqual(len(self.client.get(PUBLIC_URL).data), 1)

        self.as_admin()
        self.client.patch(
            detail_url(row.pk), {'status': 'draft'}, format='json'
        )

        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).data, [])

    def test_publishing_a_draft_makes_it_public(self):
        row = self.make(title='Soon', status=Announcement.STATUS_DRAFT)
        self.as_admin()
        response = self.client.patch(
            detail_url(row.pk), {'status': 'published'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.as_anonymous()
        titles = [r['title'] for r in self.client.get(PUBLIC_URL).data]
        self.assertEqual(titles, ['Soon'])

    def test_scheduled_announcement_appears_only_after_starts_at(self):
        now = timezone.now()
        row = self.make(title='Scheduled', starts_at=now + timedelta(hours=1))
        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).data, [])

        # Fast-forward past the start time by editing the row's window.
        row.starts_at = now - timedelta(seconds=1)
        row.save(update_fields=['starts_at'])
        self.assertEqual(
            [r['title'] for r in self.client.get(PUBLIC_URL).data],
            ['Scheduled'],
        )
