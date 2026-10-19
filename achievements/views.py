"""Achievement API.

    GET    /api/v1/achievements/                       (authenticated, player)
    GET    /api/v1/admin/achievements/                 (admin, every definition)
    POST   /api/v1/admin/achievements/                 (admin, create)
    GET    /api/v1/admin/achievements/{id}/            (admin, retrieve)
    PATCH  /api/v1/admin/achievements/{id}/            (admin, partial update)
    DELETE /api/v1/admin/achievements/{id}/            (admin, soft delete)

The player surface returns a plain array of the definitions a player may see
(active ones, plus any they have already earned so history stays visible) with a
server-computed ``earned`` flag and ``progress``. The administrative surface
reuses the platform's established admin authorization
(``DjangoModelPermissionsWithView`` on the ``Admin`` group). Nothing is ever
hard-deleted through the API: an *unearned* definition is soft-deleted, while an
*earned* one is protected and must be deactivated instead.

Both listings compute ``earned``/``progress`` from stored ``Solve`` history in a
constant number of queries; category/challenge display names are fetched once per
response rather than per row.
"""

from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from users.permissions import DjangoModelPermissionsWithView

from .criteria import CATEGORY_SOLVES, CHALLENGE_SOLVED
from .models import Achievement, UserAchievement
from .serializers import (
    AchievementAdminSerializer,
    AchievementUserSerializer,
    AchievementWriteSerializer,
)
from .services import progress_context


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _label_context(definitions):
    """Batch the category/challenge names needed to label a page of rules."""
    from challenges.models import Category, Challenge

    category_ids, challenge_ids = set(), set()
    for achievement in definitions:
        config = achievement.criteria_config or {}
        if achievement.criteria_type == CATEGORY_SOLVES:
            value = _as_int(config.get('category'))
            if value is not None:
                category_ids.add(value)
        elif achievement.criteria_type == CHALLENGE_SOLVED:
            value = _as_int(config.get('challenge'))
            if value is not None:
                challenge_ids.add(value)

    return {
        'category_names': dict(
            Category.objects.filter(id__in=category_ids).values_list('id', 'name')
        ),
        'challenge_titles': dict(
            Challenge.objects.filter(id__in=challenge_ids).values_list('id', 'title')
        ),
    }


class UserAchievementListView(generics.ListAPIView):
    """GET /api/v1/achievements/ - the caller's achievement board.

    Returns active definitions plus any the caller has already earned (so a
    later deactivation *or* soft delete never erases a displayed award).
    Progress and earned state are derived from the caller's own solve history
    only.

    ``all_objects`` is used deliberately: a soft-deleted definition must stay
    visible to the players who already earned it, otherwise administratively
    removing a rule would silently strip badges from their collections. A
    definition the caller has not earned is still governed by ``is_active``
    (which a soft delete clears), so deleted rows never leak to other players.
    """

    serializer_class = AchievementUserSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
    http_method_names = ['get', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        earned_ids = UserAchievement.objects.filter(user=user).values_list(
            'achievement_id', flat=True
        )
        return Achievement.all_objects.filter(
            Q(is_active=True) | Q(pk__in=earned_ids)
        ).order_by('sort_order', 'id')

    def _definitions(self):
        # Materialise once and reuse for both the label context and the payload.
        if not hasattr(self, '_definitions_cache'):
            self._definitions_cache = list(self.get_queryset())
        return self._definitions_cache

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        earned_map = dict(
            UserAchievement.objects.filter(user=user).values_list(
                'achievement_id', 'unlocked_at'
            )
        )
        solved_ids, per_category = progress_context(user)
        context.update(
            {
                'earned_map': earned_map,
                'solved_ids': solved_ids,
                'per_category': per_category,
                **_label_context(self._definitions()),
            }
        )
        return context

    def list(self, request, *args, **kwargs):
        # Plain array (public/player surfaces in this project are unpaginated).
        serializer = self.get_serializer(self._definitions(), many=True)
        return Response(serializer.data)


class AdminAchievementListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/v1/admin/achievements/ - list every definition, or create."""

    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    pagination_class = None

    def get_queryset(self):
        queryset = Achievement.objects.annotate(earned_count=Count('earned_by'))
        active = self.request.query_params.get('active')
        if active in ('true', '1'):
            queryset = queryset.filter(is_active=True)
        elif active in ('false', '0'):
            queryset = queryset.filter(is_active=False)
        return queryset.order_by('sort_order', 'id')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return AchievementWriteSerializer
        return AchievementAdminSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update(_label_context(self.get_queryset()))
        return context


class AdminAchievementDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/v1/admin/achievements/{id}/.

    An achievement that has **never** been earned can be deleted, and that
    delete is a soft delete (``Achievement.delete``): the definition row and its
    slug are kept and simply hidden. An achievement that **has** been earned is
    protected - ``DELETE`` returns ``409`` and the administrator must deactivate
    it (``is_active=False``) instead, so an earned badge can never be taken away
    or silently removed.
    """

    queryset = Achievement.objects.all()
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete a definition that already has awards."""
        instance = self.get_object()
        if instance.earned_by.exists():
            return Response(
                {
                    'detail': (
                        'This achievement has already been earned and cannot be '
                        'deleted. Deactivate it instead to hide it from players.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return AchievementWriteSerializer
        return AchievementAdminSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update(_label_context([self.get_object()]))
        return context
