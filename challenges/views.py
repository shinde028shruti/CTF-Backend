"""Challenge API views.

Two surfaces:

* Public (``AllowAny``): the published challenge list, a published challenge's
  detail, and the category list. Both challenge endpoints return a plain JSON
  array / object (no DRF pagination) to match the existing frontend service.
* Admin (Django model permissions): full CRUD, publish/unpublish, scheduling
  and attachment management. Reads require ``challenges.view_challenge``;
  writes require ``challenges.add/change/delete_challenge``.
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import DjangoModelPermissionsWithView

from .models import Category, Challenge, ChallengeAttachment
from .permissions import HasChallengeChangePermission
from .serializers import (
    AttachmentSerializer,
    CategorySerializer,
    ChallengeAdminSerializer,
    ChallengePublicSerializer,
    ChallengeWriteSerializer,
)

PUBLISHED = Challenge.STATUS_PUBLISHED


def _solved_ids(user):
    """Ids of the challenges ``user`` has solved (empty for anonymous)."""
    if user is None or not user.is_authenticated:
        return set()
    from submissions.models import Solve

    return set(
        Solve.objects.filter(user=user).values_list('challenge_id', flat=True)
    )


def _revealed_hint_ids(user):
    """Ids of the hints ``user`` has unlocked (empty for anonymous).

    Used so a published challenge can show the text of hints the requesting
    user already paid for, while never exposing anyone else's/undeocked text.
    """
    if user is None or not user.is_authenticated:
        return set()
    from submissions.models import HintUnlock

    return set(
        HintUnlock.objects.filter(user=user).values_list('hint_id', flat=True)
    )


class SolvedContextMixin:
    """Add the requesting user's solved-challenge and revealed-hint ids to
    serializer context.

    Lets ``ChallengePublicSerializer.get_solved`` and ``PublicHintSerializer``
    answer in one bulk query each instead of one query per row.
    """

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        context['solved_ids'] = _solved_ids(user)
        context['revealed_hint_ids'] = _revealed_hint_ids(user)
        return context


def _published_queryset():
    return (
        Challenge.objects.published()
        .select_related('category')
        .prefetch_related('hints', 'attachments')
    )


class PublicChallengeListView(SolvedContextMixin, generics.ListAPIView):
    """GET /api/v1/challenges/ - every published challenge as a plain array."""

    serializer_class = ChallengePublicSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        queryset = _published_queryset()
        params = self.request.query_params

        category = params.get('category')
        if category and category != 'All':
            queryset = queryset.filter(category__name=category)

        difficulty = params.get('difficulty')
        if difficulty and difficulty != 'All':
            queryset = queryset.filter(difficulty=difficulty)

        search = (params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(category__name__icontains=search)
            )
        return queryset


class PublicChallengeDetailView(SolvedContextMixin, generics.RetrieveAPIView):
    """GET /api/v1/challenges/{id}/ - a published challenge, else 404."""

    serializer_class = ChallengePublicSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return _published_queryset()


class PublicCategoryListView(generics.ListAPIView):
    """GET /api/v1/categories/ - the configured categories."""

    serializer_class = CategorySerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        return Category.objects.annotate(
            challenge_count=Count(
                'challenges',
                filter=Q(challenges__status=PUBLISHED),
            )
        )


class AdminChallengeListCreateView(SolvedContextMixin, generics.ListCreateAPIView):
    """GET/POST /api/v1/admin/challenges/ - list all, or create."""

    serializer_class = ChallengeAdminSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    pagination_class = None
    queryset = Challenge.objects.all()

    def get_queryset(self):
        queryset = (
            Challenge.objects.all()
            .select_related('category', 'created_by')
            .prefetch_related('hints', 'attachments', 'flag')
        )
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)

        search = (self.request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(category__name__icontains=search)
                | Q(author__icontains=search)
            )
        return queryset

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ChallengeWriteSerializer
        return ChallengeAdminSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AdminChallengeDetailView(SolvedContextMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/v1/admin/challenges/{id}/."""

    queryset = Challenge.objects.all()
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return (
            Challenge.objects.all()
            .select_related('category', 'created_by')
            .prefetch_related('hints', 'attachments', 'flag')
        )

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return ChallengeWriteSerializer
        return ChallengeAdminSerializer

    def destroy(self, request, *args, **kwargs):
        challenge = self.get_object()
        if challenge.status != Challenge.STATUS_DRAFT:
            raise ValidationError({
                'detail': (
                    'Only draft challenges can be deleted. Unpublish this '
                    'challenge first.'
                )
            })
        # ChallengeFlag, ChallengeHint and ChallengeAttachment are owned by the
        # challenge and cascade-delete safely; attachment files are removed by
        # a post_delete signal. There are no submissions/leaderboard rows yet,
        # so nothing unrelated is lost.
        return super().destroy(request, *args, **kwargs)


class AdminChallengeStatusView(APIView):
    """PATCH /api/v1/admin/challenges/{id}/status/ - publish/unpublish/schedule."""

    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    queryset = Challenge.objects.all()

    def patch(self, request, pk):
        challenge = get_object_or_404(Challenge, pk=pk)
        new_status = request.data.get('status')
        valid = dict(Challenge.STATUS_CHOICES)
        if new_status not in valid:
            raise ValidationError({
                'status': f'Choose one of: {", ".join(valid)}.'
            })

        publish_at = challenge.publish_at
        if 'publishAt' in request.data:
            from django.utils import timezone
            from django.utils.dateparse import parse_datetime

            raw = (request.data.get('publishAt') or '').strip()
            if not raw:
                publish_at = None
            else:
                parsed = parse_datetime(raw)
                if parsed is None:
                    raise ValidationError({'publishAt': 'Invalid date/time value.'})
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(
                        parsed, timezone.get_current_timezone()
                    )
                publish_at = parsed

        if new_status == Challenge.STATUS_SCHEDULED and publish_at is None:
            raise ValidationError({
                'publishAt': 'A publish time is required to schedule a challenge.'
            })

        challenge.status = new_status
        challenge.publish_at = publish_at
        challenge.save(update_fields=['status', 'publish_at', 'updated_at'])
        return Response(ChallengeAdminSerializer(challenge).data)


class AdminChallengeAttachmentView(APIView):
    """POST /api/v1/admin/challenges/{id}/attachments/ - upload a file."""

    permission_classes = [IsAuthenticated, HasChallengeChangePermission]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        challenge = get_object_or_404(Challenge, pk=pk)
        upload = request.FILES.get('file')
        if upload is None:
            raise ValidationError({'file': 'A file is required.'})
        name = (request.data.get('name') or '').strip()
        attachment = ChallengeAttachment.objects.create(
            challenge=challenge,
            file=upload,
            name=name,
        )
        return Response(
            AttachmentSerializer(attachment, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class AdminChallengeAttachmentDetailView(APIView):
    """DELETE /api/v1/admin/challenges/{id}/attachments/{attachment_id}/."""

    permission_classes = [IsAuthenticated, HasChallengeChangePermission]

    def delete(self, request, pk, attachment_pk):
        attachment = get_object_or_404(
            ChallengeAttachment, pk=attachment_pk, challenge_id=pk
        )
        attachment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
