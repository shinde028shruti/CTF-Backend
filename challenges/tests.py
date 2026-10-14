"""Tests for the challenges app: models, serializers and both API surfaces.

The security-critical invariants get the most attention:

* a flag is only ever stored as a hash and is never serialized;
* ordinary users can only ever see *published* challenges, and never a flag,
  a flag hash/note or a hint's text;
* admin-only mutations are gated by Django model permissions.
"""

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Category, Challenge, ChallengeAttachment, ChallengeFlag, ChallengeHint

User = get_user_model()


class ChallengeTestMixin:
    """Shared fixtures: two categories, a published and a draft challenge."""

    def setUp(self):
        self.web = Category.objects.create(name='Web', order=0)
        self.crypto = Category.objects.create(name='Crypto', order=1)

        self.published = Challenge.objects.create(
            title='Published One',
            category=self.web,
            difficulty=Challenge.DIFFICULTY_EASY,
            points=100,
            status=Challenge.STATUS_PUBLISHED,
            tags=['web'],
        )
        flag = ChallengeFlag(challenge=self.published, note='the real one')
        flag.set_flag('CTF{published_flag}')
        flag.save()
        ChallengeHint.objects.create(
            challenge=self.published, text='Secret hint text', cost=10, order=0
        )

        self.draft = Challenge.objects.create(
            title='Draft One',
            category=self.web,
            difficulty=Challenge.DIFFICULTY_MEDIUM,
            points=200,
            status=Challenge.STATUS_DRAFT,
        )
        draft_flag = ChallengeFlag(challenge=self.draft)
        draft_flag.set_flag('CTF{draft_flag}')
        draft_flag.save()

        self.client = APIClient()

    def grant(self, user, *refs):
        for ref in refs:
            app_label, codename = ref.split('.')
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
            user.user_permissions.add(permission)
        return user

    def _make_user(self, username, *refs):
        user = User.objects.create_user(
            username=username, email=f'{username}@example.com', password='pass12345'
        )
        if refs:
            self.grant(user, *refs)
        return user

    def make_admin(self):
        return self._make_user(
            'ch_admin',
            'challenges.view_challenge',
            'challenges.add_challenge',
            'challenges.change_challenge',
            'challenges.delete_challenge',
        )

    def make_creator(self):
        return self._make_user(
            'ch_creator',
            'challenges.view_challenge',
            'challenges.add_challenge',
            'challenges.change_challenge',
        )

    def make_plain_user(self):
        return self._make_user('ch_plain')

    def payload(self, **overrides):
        data = {
            'title': 'New Challenge',
            'category': 'Web',
            'difficulty': 'Hard',
            'points': 400,
            'description': 'Do the thing',
            'status': 'draft',
            'flag': 'CTF{brand_new_flag}',
            'hints': [{'text': 'look closer', 'cost': 5}],
        }
        data.update(overrides)
        return data


class CategoryModelTests(TestCase):
    def test_slug_is_generated_and_unique(self):
        first = Category.objects.create(name='Web Exploitation')
        second = Category.objects.create(name='Web Exploitation!!')
        self.assertEqual(first.slug, 'web-exploitation')
        self.assertEqual(second.slug, 'web-exploitation-2')


class ChallengeModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Web')

    def test_slug_is_generated(self):
        challenge = Challenge.objects.create(title='Hello World', category=self.category)
        self.assertEqual(challenge.slug, 'hello-world')

    def test_has_flag_reflects_stored_flag(self):
        challenge = Challenge.objects.create(title='No Flag', category=self.category)
        self.assertFalse(challenge.has_flag)
        flag = ChallengeFlag(challenge=challenge)
        flag.set_flag('CTF{abc}')
        flag.save()
        self.assertTrue(Challenge.objects.get(pk=challenge.pk).has_flag)


class ChallengeFlagTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Crypto')
        self.challenge = Challenge.objects.create(title='Flag', category=self.category)

    def test_flag_is_hashed_not_stored_in_plaintext(self):
        flag = ChallengeFlag(challenge=self.challenge)
        flag.set_flag('CTF{super_secret}')
        flag.save()
        self.assertNotEqual(flag.hash, 'CTF{super_secret}')
        self.assertNotIn('super_secret', flag.hash)

    def test_verify_accepts_the_exact_flag(self):
        flag = ChallengeFlag(challenge=self.challenge)
        flag.set_flag('CTF{super_secret}')
        flag.save()
        self.assertTrue(flag.verify('CTF{super_secret}'))

    def test_verify_strips_surrounding_whitespace(self):
        flag = ChallengeFlag(challenge=self.challenge)
        flag.set_flag('CTF{super_secret}')
        flag.save()
        self.assertTrue(flag.verify('  CTF{super_secret}\n'))

    def test_verify_is_case_sensitive(self):
        flag = ChallengeFlag(challenge=self.challenge)
        flag.set_flag('CTF{Super_Secret}')
        flag.save()
        self.assertFalse(flag.verify('CTF{super_secret}'))

    def test_verify_rejects_a_wrong_flag(self):
        flag = ChallengeFlag(challenge=self.challenge)
        flag.set_flag('CTF{super_secret}')
        flag.save()
        self.assertFalse(flag.verify('CTF{nope}'))
        self.assertFalse(flag.verify(''))


class AttachmentModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Misc')
        self.challenge = Challenge.objects.create(title='Files', category=self.category)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_display_name_and_extension(self):
        with override_settings(MEDIA_ROOT=self.tmp):
            attachment = ChallengeAttachment.objects.create(
                challenge=self.challenge,
                file=SimpleUploadedFile('sample.zip', b'data'),
            )
            self.assertEqual(attachment.display_name, 'sample.zip')
            self.assertEqual(attachment.extension, 'zip')

    def test_file_is_deleted_with_the_row(self):
        import os

        with override_settings(MEDIA_ROOT=self.tmp):
            attachment = ChallengeAttachment.objects.create(
                challenge=self.challenge,
                file=SimpleUploadedFile('gone.txt', b'data'),
            )
            path = attachment.file.path
            self.assertTrue(os.path.exists(path))
            attachment.delete()
            self.assertFalse(os.path.exists(path))


class PublicChallengeApiTests(ChallengeTestMixin, TestCase):
    def test_list_is_a_plain_array_of_published_only(self):
        response = self.client.get('/api/v1/challenges/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsInstance(body, list)
        ids = [item['id'] for item in body]
        self.assertIn(self.published.id, ids)
        self.assertNotIn(self.draft.id, ids)

    def test_list_never_exposes_flag_or_hint_text(self):
        response = self.client.get('/api/v1/challenges/')
        item = next(
            row for row in response.json() if row['id'] == self.published.id
        )
        self.assertNotIn('flag', item)
        self.assertNotIn('hash', item)
        self.assertNotIn('flag_note', item)
        for hint in item['hints']:
            self.assertEqual(
                set(hint), {'id', 'cost', 'order', 'revealed', 'text'}
            )
            # Anonymous callers never see any hint text.
            self.assertFalse(hint['revealed'])
            self.assertEqual(hint['text'], '')
        self.assertFalse(item['solved'])
        self.assertIn('category', item)
        self.assertIn('createdAt', item)

    def test_filter_by_category_and_search(self):
        by_category = self.client.get('/api/v1/challenges/', {'category': 'Web'}).json()
        self.assertEqual([c['id'] for c in by_category], [self.published.id])

        by_search = self.client.get(
            '/api/v1/challenges/', {'search': 'Published'}
        ).json()
        self.assertEqual([c['id'] for c in by_search], [self.published.id])

        misses = self.client.get('/api/v1/challenges/', {'search': 'Draft'}).json()
        self.assertEqual(misses, [])

    def test_detail_returns_published_and_hides_sensitive_fields(self):
        response = self.client.get(f'/api/v1/challenges/{self.published.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('hash', response.json())

    def test_detail_of_a_draft_is_404(self):
        response = self.client.get(f'/api/v1/challenges/{self.draft.id}/')
        self.assertEqual(response.status_code, 404)

    def test_categories_list(self):
        response = self.client.get('/api/v1/categories/')
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)


class AdminChallengeApiTests(ChallengeTestMixin, TestCase):
    def test_anonymous_is_denied(self):
        response = self.client.get('/api/v1/admin/challenges/')
        self.assertIn(response.status_code, (401, 403))

    def test_plain_user_is_denied(self):
        self.client.force_authenticate(user=self.make_plain_user())
        response = self.client.get('/api/v1/admin/challenges/')
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_drafts_too(self):
        self.client.force_authenticate(user=self.make_admin())
        body = self.client.get('/api/v1/admin/challenges/').json()
        self.assertIsInstance(body, list)
        ids = [row['id'] for row in body]
        self.assertIn(self.draft.id, ids)
        self.assertIn(self.published.id, ids)

    def test_creator_can_create_and_flag_is_hashed(self):
        self.client.force_authenticate(user=self.make_creator())
        response = self.client.post(
            '/api/v1/admin/challenges/', self.payload(), format='json'
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertNotIn('flag', body)
        self.assertNotIn('hash', body)
        self.assertTrue(body['has_flag'])

        challenge = Challenge.objects.get(pk=body['id'])
        self.assertTrue(challenge.verify_flag('CTF{brand_new_flag}'))
        self.assertNotIn('CTF{brand_new_flag}', challenge.flag.hash)
        self.assertEqual(challenge.hints.count(), 1)

    def test_create_requires_a_well_formed_flag(self):
        self.client.force_authenticate(user=self.make_admin())
        missing = self.payload(flag='')
        self.assertEqual(
            self.client.post(
                '/api/v1/admin/challenges/', missing, format='json'
            ).status_code,
            400,
        )
        malformed = self.payload(flag='not-a-flag')
        self.assertEqual(
            self.client.post(
                '/api/v1/admin/challenges/', malformed, format='json'
            ).status_code,
            400,
        )

    def test_update_does_not_require_the_flag_again(self):
        self.client.force_authenticate(user=self.make_admin())
        response = self.client.patch(
            f'/api/v1/admin/challenges/{self.published.id}/',
            {'points': 999},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.published.refresh_from_db()
        self.assertEqual(self.published.points, 999)
        self.assertTrue(self.published.has_flag)

    def test_update_can_replace_the_flag(self):
        self.client.force_authenticate(user=self.make_admin())
        self.client.patch(
            f'/api/v1/admin/challenges/{self.published.id}/',
            {'flag': 'CTF{replaced}'},
            format='json',
        )
        self.published.refresh_from_db()
        self.assertTrue(self.published.verify_flag('CTF{replaced}'))
        self.assertFalse(self.published.verify_flag('CTF{published_flag}'))

    def test_scheduling_requires_a_publish_time(self):
        self.client.force_authenticate(user=self.make_admin())
        response = self.client.post(
            '/api/v1/admin/challenges/',
            self.payload(status='scheduled', flag='CTF{x}'),
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_status_endpoint_publishes(self):
        self.client.force_authenticate(user=self.make_admin())
        response = self.client.patch(
            f'/api/v1/admin/challenges/{self.draft.id}/status/',
            {'status': 'published'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Challenge.STATUS_PUBLISHED)

    def test_delete_draft_cascades_and_removes_files(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with override_settings(MEDIA_ROOT=tmp):
            attachment = ChallengeAttachment.objects.create(
                challenge=self.draft,
                file=SimpleUploadedFile('payload.txt', b'data'),
            )
            path = attachment.file.path
            self.client.force_authenticate(user=self.make_admin())
            response = self.client.delete(f'/api/v1/admin/challenges/{self.draft.id}/')
            self.assertEqual(response.status_code, 204, response.content)
            self.assertFalse(Challenge.objects.filter(pk=self.draft.pk).exists())
            self.assertFalse(ChallengeAttachment.objects.filter(pk=attachment.pk).exists())
            self.assertFalse(__import__('os').path.exists(path))

    def test_delete_published_is_refused(self):
        self.client.force_authenticate(user=self.make_admin())
        response = self.client.delete(
            f'/api/v1/admin/challenges/{self.published.id}/'
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Challenge.objects.filter(pk=self.published.pk).exists())

    def test_creator_without_delete_permission_is_refused(self):
        self.client.force_authenticate(user=self.make_creator())
        response = self.client.delete(f'/api/v1/admin/challenges/{self.draft.id}/')
        self.assertEqual(response.status_code, 403)

    def test_attachment_upload_and_delete(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with override_settings(MEDIA_ROOT=tmp):
            self.client.force_authenticate(user=self.make_admin())
            response = self.client.post(
                f'/api/v1/admin/challenges/{self.draft.id}/attachments/',
                {'file': SimpleUploadedFile('notes.txt', b'hello')},
                format='multipart',
            )
            self.assertEqual(response.status_code, 201, response.content)
            attachment_id = response.json()['id']

            deleted = self.client.delete(
                f'/api/v1/admin/challenges/{self.draft.id}/'
                f'attachments/{attachment_id}/'
            )
            self.assertEqual(deleted.status_code, 204)
            self.assertFalse(
                ChallengeAttachment.objects.filter(pk=attachment_id).exists()
            )
