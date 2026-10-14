"""Leaderboard ranking service (read-only).

Ranks players by the points they have *actually* earned, following the existing
scoring rule: ``User.points == sum(Solve.points) - sum(HintUnlock.cost)``.

There is deliberately no second scoring system and no new model. Two derived
scopes read the same historical solve/hint snapshots:

* the all-time board reads the denormalised ``User.points`` counter, which is
  already net of hint spending and is kept in sync in the same transaction as
  every solve/unlock;
* the weekly and category boards have no stored counter, so they recompute the
  same formula over the relevant subset of rows.

Subqueries (rather than a single multi-relation ``annotate``) are used on
purpose: aggregating over several reverse foreign keys in one queryset builds a
cartesian join, which corrupts ``Sum`` and non-distinct ``Count``.

Ranking is deterministic:

1. total points, descending;
2. challenges solved, descending;
3. who reached the tied score earliest, i.e. the earlier of a player's most
   recent solve and most recent hint unlock, ascending;
4. primary key, ascending (a final, stable tiebreak).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import (
    Count,
    DateTimeField,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from .models import HintUnlock, Solve, Submission

User = get_user_model()

ALL_TIME = 'all'
WEEKLY = 'week'
PERIODS = (ALL_TIME, WEEKLY)

WEEKLY_WINDOW_DAYS = 7
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _int_subquery(expression):
    """Wrap a scalar subquery so it renders ``0`` instead of ``NULL``."""
    return Coalesce(expression, Value(0), output_field=IntegerField())


def _count_of(model, **filters):
    """A per-user ``Count`` of ``model`` rows matching ``filters``.

    The correlated subquery is scoped to ``user=OuterRef('pk')`` by the caller,
    so it can never count another player's rows.
    """
    return _int_subquery(
        Subquery(
            model.objects.filter(**filters)
            .values('user')
            .annotate(total=Count('*'))
            .values('total'),
            output_field=IntegerField(),
        )
    )


def _sum_of(model, field, **filters):
    """A per-user ``Sum`` of ``field`` over ``model`` rows matching ``filters``."""
    return _int_subquery(
        Subquery(
            model.objects.filter(**filters)
            .values('user')
            .annotate(total=Sum(field))
            .values('total'),
            output_field=IntegerField(),
        )
    )


def _latest_at(model, **filters):
    """The most recent ``created_at`` for a user's ``model`` rows (or NULL)."""
    return Subquery(
        model.objects.filter(**filters)
        .order_by('-created_at', '-id')
        .values('created_at')[:1],
        output_field=DateTimeField(),
    )


def _scope_filters(period, category_id, now):
    """Build the ``created_at`` / ``category`` filters for a ranking scope."""
    solve = {}
    hint = {}
    submission = {}

    if period == WEEKLY:
        cutoff = now - timedelta(days=WEEKLY_WINDOW_DAYS)
        solve['created_at__gte'] = cutoff
        hint['created_at__gte'] = cutoff
        submission['created_at__gte'] = cutoff

    if category_id is not None:
        solve['challenge__category_id'] = category_id
        hint['challenge__category_id'] = category_id
        submission['challenge__category_id'] = category_id

    return solve, hint, submission


def ranking_queryset(*, period=ALL_TIME, category_id=None, now=None):
    """Return the ordered, annotated queryset backing one leaderboard scope.

    Each row is a :class:`~django.contrib.auth.models.User` annotated with
    ``points_value``, ``solved_count``, ``submissions_total``,
    ``submissions_correct`` and ``last_event_at``. Only active, non-admin
    accounts with at least one solve in scope are included.
    """
    if period not in PERIODS:
        period = ALL_TIME
    now = now or timezone.now()

    solve_filters, hint_filters, submission_filters = _scope_filters(
        period, category_id, now
    )
    scoped = period == WEEKLY or category_id is not None

    user_ref = OuterRef('pk')
    solved_count = _count_of(Solve, user=user_ref, **solve_filters)
    submissions_total = _count_of(Submission, user=user_ref, **submission_filters)
    submissions_correct = _count_of(
        Submission, user=user_ref, correct=True, **submission_filters
    )
    last_solve = _latest_at(Solve, user=user_ref, **solve_filters)
    last_hint = _latest_at(HintUnlock, user=user_ref, **hint_filters)

    if scoped:
        # No stored counter for a window/subset: recompute the same formula.
        points_value = (
            _sum_of(Solve, 'points', user=user_ref, **solve_filters)
            - _sum_of(HintUnlock, 'cost', user=user_ref, **hint_filters)
        )
    else:
        points_value = F('points')

    return (
        User.objects.filter(
            is_active=True,
            role=User.ROLE_USER,
            is_superuser=False,
        )
        .annotate(
            solved_count=solved_count,
            submissions_total=submissions_total,
            submissions_correct=submissions_correct,
            points_value=points_value,
            # The later of the player's most recent solve and hint unlock. On
            # SQLite a two-argument MAX() is NULL if either side is NULL, so
            # fall back to the solve (always present for a ranked player).
            last_event_at=Coalesce(
                Greatest(last_solve, last_hint),
                last_solve,
                output_field=DateTimeField(),
            ),
        )
        .filter(solved_count__gt=0)
        .order_by('-points_value', '-solved_count', 'last_event_at', 'pk')
    )


def rank_for(user, *, period=ALL_TIME, category_id=None, now=None):
    """Return ``user``'s 1-based rank in the scope, or ``None`` if unranked."""
    queryset = ranking_queryset(period=period, category_id=category_id, now=now)
    mine = (
        queryset.filter(pk=user.pk)
        .values('points_value', 'solved_count', 'last_event_at')
        .first()
    )
    if mine is None:
        return None

    points = mine['points_value']
    solved = mine['solved_count']
    reached_at = mine['last_event_at']

    ahead = queryset.filter(
        Q(points_value__gt=points)
        | Q(points_value=points, solved_count__gt=solved)
        | Q(points_value=points, solved_count=solved, last_event_at__lt=reached_at)
        | Q(
            points_value=points,
            solved_count=solved,
            last_event_at=reached_at,
            pk__lt=user.pk,
        )
    ).count()
    return ahead + 1
