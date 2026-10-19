"""Flag-submission, hint-reveal and leaderboard API.

    POST /api/v1/challenges/{id}/submit/                 {"flag": "..."}
    POST /api/v1/challenges/{id}/hints/{hint_id}/reveal/
    GET  /api/v1/leaderboard/                            (public, paginated)
    GET  /api/v1/leaderboard/me/                         (authenticated)
    GET  /api/v1/stats/me/                               (authenticated)

Only *published* challenges are reachable; a draft or an unknown id both return
the same 404, so nothing about unpublished content leaks. The submitted flag is
verified against the stored PBKDF2 hash (constant-time) and is never echoed
back, logged, or persisted. A successful solve is recorded exactly once per
user through ``services.award_solve``.

Hint text is only ever returned by the reveal endpoint, after the server has
confirmed (and charged for) the unlock. Point costs and the acting user come
from the database/request, never from the request body.

The leaderboard endpoints are read-only projections of the stored solves and
hint unlocks (see ``leaderboard.py``); they expose only public profile fields.
"""

from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from challenges.models import Category, Challenge, ChallengeHint

from .leaderboard import (
    ALL_TIME,
    PERIODS,
    rank_for,
    ranking_queryset,
)
from .models import HintUnlock, Submission
from .serializers import (
    FlagSubmissionSerializer,
    LeaderboardEntrySerializer,
    UserStatsSerializer,
)
from .services import (
    UNLOCK_STATUS_ALREADY,
    UNLOCK_STATUS_INSUFFICIENT,
    award_solve,
    unlock_hint,
)
from .stats import build_user_statistics
from .throttling import FlagSubmitThrottle, HintRevealThrottle


class FlagSubmitView(APIView):
    """POST a flag guess for a published challenge."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [FlagSubmitThrottle]

    def post(self, request, pk):
        serializer = FlagSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submitted = serializer.validated_data['flag']

        # Published-only: a draft is indistinguishable from a missing id.
        challenge = get_object_or_404(Challenge.objects.published(), pk=pk)

        correct = challenge.verify_flag(submitted)
        # Record the attempt regardless of outcome; the submitted plaintext is
        # never stored.
        Submission.objects.create(
            user=request.user,
            challenge=challenge,
            correct=correct,
        )

        if not correct:
            return Response({
                'correct': False,
                'alreadySolved': False,
                'points': 0,
                'message': 'Incorrect flag. Keep investigating.',
            })

        created, solve = award_solve(request.user, challenge)

        # The reward *shown* to the player is the challenge's value net of the
        # hint points this user already spent on it. Hint costs were deducted
        # at reveal time, so the balance is already net; ``points`` stays gross
        # (the amount actually credited to the balance) while ``netPoints`` is
        # what the UI reports as earned.
        hint_cost = (
            HintUnlock.objects
            .filter(user=request.user, challenge=challenge)
            .aggregate(total=Sum('cost'))['total']
            or 0
        )
        net_points = max(solve.points - hint_cost, 0)

        if not created:
            return Response({
                'correct': True,
                'alreadySolved': True,
                'points': 0,
                'hintCost': hint_cost,
                'netPoints': net_points,
                'message': 'You have already solved this challenge.',
            })

        if hint_cost:
            message = (
                f'Correct flag! +{net_points} points '
                f'({solve.points} minus {hint_cost} spent on hints).'
            )
        else:
            message = f'Correct flag! +{net_points} points.'

        return Response({
            'correct': True,
            'alreadySolved': False,
            'points': solve.points,
            'hintCost': hint_cost,
            'netPoints': net_points,
            'message': message,
        })


class HintRevealView(APIView):
    """POST to reveal (and pay for) a hint of a published challenge.

    The request body is ignored entirely: the hint, its cost and the acting
    user are all resolved server-side, so a client cannot discount a hint or
    unlock one on someone else's behalf.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [HintRevealThrottle]

    def post(self, request, pk, hint_pk):
        # Published-only, and the hint must belong to *this* challenge; an
        # unpublished challenge or a foreign/unknown hint both 404 identically.
        challenge = get_object_or_404(Challenge.objects.published(), pk=pk)
        hint = get_object_or_404(
            ChallengeHint, pk=hint_pk, challenge=challenge
        )

        outcome, unlock, remaining = unlock_hint(request.user, hint)

        if outcome == UNLOCK_STATUS_INSUFFICIENT:
            return Response(
                {
                    'detail': (
                        'Not enough points to unlock this hint. '
                        f'It costs {hint.cost} and you have {remaining}.'
                    ),
                    'required': hint.cost,
                    'available': remaining,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        already = outcome == UNLOCK_STATUS_ALREADY
        points_spent = 0 if already else unlock.cost
        return Response({
            'revealed': True,
            'alreadyUnlocked': already,
            'hint': {
                'id': hint.id,
                'order': hint.order,
                'cost': hint.cost,
                'text': hint.text,
            },
            'pointsSpent': points_spent,
            'remainingPoints': remaining,
            'message': (
                'You have already unlocked this hint.'
                if already
                else f'Hint unlocked. -{points_spent} points.'
            ),
        })


# ─── Leaderboard ─────────────────────────────────────────────────────────


class LeaderboardPagination(PageNumberPagination):
    """Page-number pagination for the leaderboard.

    ``?limit=`` overrides the page size up to :data:`MAX_PAGE_SIZE` (100); a
    larger value is silently clamped rather than rejected.
    """

    page_size = 50
    page_size_query_param = 'limit'
    max_page_size = 100


def _resolve_category(value):
    """Map a ``?category=`` value (numeric id, name or slug) -> ``(id, name)``.

    An absent, ``All`` or unknown value yields ``(None, '')``, which means "no
    category filter" (the all-time board).
    """
    if not value or value == 'All':
        return None, ''
    category = None
    if str(value).isdigit():
        category = Category.objects.filter(pk=int(value)).first()
    if category is None:
        category = (
            Category.objects.filter(name__iexact=value).first()
            or Category.objects.filter(slug=value).first()
        )
    if category is None:
        return None, ''
    return category.id, category.name


def _period_of(request):
    period = request.query_params.get('period', ALL_TIME)
    return period if period in PERIODS else ALL_TIME


class LeaderboardView(APIView):
    """GET /api/v1/leaderboard/ - public, read-only ranking.

    Query parameters (never trusted as scoring input, only as filters):

    * ``period=all`` (default) or ``period=week``;
    * ``category=<id|name>`` to scope the board to one category;
    * ``page`` / ``limit`` for pagination.

    The response is a paginated envelope ``{count, next, previous, results}``.
    Ranking is computed entirely server-side from stored solves/hint unlocks.
    """

    permission_classes = [AllowAny]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        period = _period_of(request)
        category_id, category_name = _resolve_category(
            request.query_params.get('category')
        )
        queryset = ranking_queryset(period=period, category_id=category_id)

        paginator = LeaderboardPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = LeaderboardEntrySerializer(
            page, many=True, context={'meta': category_name}
        )

        # Rank is global: page 2 continues after page 1 rather than restarting.
        rows = serializer.data
        start = paginator.page.start_index()
        for offset, row in enumerate(rows):
            row['rank'] = start + offset
        return paginator.get_paginated_response(rows)


class LeaderboardMeView(APIView):
    """GET /api/v1/leaderboard/me/ - the caller's own standing.

    Authenticated only. Accepts the same ``period`` / ``category`` filters so
    the "your rank" card matches whichever board is on screen. Works even when
    the player is beyond the first page (or entirely unranked, in which case
    ``rank`` is ``null``).
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        period = _period_of(request)
        category_id, category_name = _resolve_category(
            request.query_params.get('category')
        )
        user = request.user
        queryset = ranking_queryset(period=period, category_id=category_id)
        row = queryset.filter(pk=user.pk).first()

        if row is None:
            return Response({
                'id': user.id,
                'username': user.username,
                'points': 0,
                'solved': 0,
                'accuracy': 0,
                'meta': category_name,
                'rank': None,
            })

        data = LeaderboardEntrySerializer(
            row, context={'meta': category_name}
        ).data
        data['rank'] = rank_for(user, period=period, category_id=category_id)
        return Response(data)


# ─── Per-user statistics ─────────────────────────────────────────────────


class UserStatsView(APIView):
    """GET /api/v1/stats/me/ - the caller's own dashboard/profile statistics.

    Authenticated only. Every value is derived server-side from the caller's
    own solves, hint unlocks and submissions (see ``stats.py``); the response is
    a closed projection that never exposes another user's data or any private
    account field. Returns ``rank: null`` for a player who has not solved
    anything yet.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        stats = build_user_statistics(request.user)
        return Response(UserStatsSerializer(stats).data)
