"""Administrative analytics API (read-only).

    GET /api/v1/admin/analytics/

One aggregate read powers both the admin Dashboard and the admin Analytics page.
Every figure is derived from authoritative rows:

* ``Solve`` - the unique per-(user, challenge) successful-solve history -
  supplies the solve total, the per-challenge solve counts, the unique-solver
  count, the per-day solve-activity series and the first-blood attribution. The
  denormalised ``Challenge.solves`` counter is deliberately never read: it is
  seeded/adjusted administratively and therefore not trustworthy.
* ``Submission`` - the append-only record of every flag attempt - supplies the
  attempt count, the correct-attempt count, the success rate and the
  recent-activity feed (which shows misses as well as solves).
* ``User`` supplies the leaderboard (points plus a real solve count per account).

Soft-deleted accounts (``deleted_at IS NULL``) are excluded everywhere, matching
the default admin user list, so a removed player stops inflating the platform
numbers while their immutable history rows stay untouched in the database.

Authorization reuses the platform's existing Django model permissions: the view
requires the same pair the Admin group already holds on the two admin surfaces
this data is drawn from - ``users.view_user`` and ``challenges.view_challenge``.
The frontend ``role='admin'`` value grants nothing here.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from challenges.models import Challenge

from .models import Solve, Submission

User = get_user_model()

# The solve-activity chart spans the same trailing window the UI renders.
SERIES_DAYS = 14
# The "Recent Solves" feed shows the newest submissions regardless of outcome.
RECENT_ACTIVITY_LIMIT = 8
TOP_SOLVERS = 6

# Django permissions an account must hold to read the platform analytics. Both
# are granted to the Admin group by ``setup_groups``; ordinary users hold
# neither, so this denies them without touching any group configuration.
ADMIN_ANALYTICS_PERMISSIONS = ('users.view_user', 'challenges.view_challenge')

# Rows belonging to an account that still exists (not soft-deleted).
LIVE_USER = Q(user__deleted_at__isnull=True)


class HasAdminAnalyticsPermission(BasePermission):
    """Allow only accounts holding every permission the Admin group holds."""

    message = (
        'You need the "users.view_user" and "challenges.view_challenge" '
        'permissions to view platform analytics.'
    )

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        return bool(
            user
            and user.is_authenticated
            and all(user.has_perm(perm) for perm in ADMIN_ANALYTICS_PERMISSIONS)
        )


def _success_rate(correct, attempts):
    """Whole-percent success rate, 0 when there were no attempts."""
    return round(correct * 100 / attempts) if attempts else 0


def _solve_series(now):
    """Counts of ``Solve`` rows per local day over the trailing window.

    Always returns :data:`SERIES_DAYS` entries (oldest first), with ``0`` for a
    day that had no solve, so the chart renders rest days rather than a gap.
    """
    today = timezone.localdate(now)
    start = today - timedelta(days=SERIES_DAYS - 1)
    start_dt = timezone.make_aware(
        datetime.combine(start, time.min), timezone.get_current_timezone()
    )

    counts = {
        row['day']: row['count']
        for row in (
            Solve.objects.filter(LIVE_USER, created_at__gte=start_dt)
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
        )
    }

    return [
        {
            'date': day.isoformat(),
            'label': f'{day.month}/{day.day}',
            'count': counts.get(day, 0),
        }
        for day in (start + timedelta(days=offset) for offset in range(SERIES_DAYS))
    ]


def _per_challenge():
    """Per-challenge performance, computed from solves/submissions (not counters).

    The first solver is resolved with a correlated subquery so the whole table
    costs a constant number of queries instead of one per challenge.
    """
    solve_counts = {
        row['challenge_id']: row['count']
        for row in (
            Solve.objects.filter(LIVE_USER)
            .values('challenge_id')
            .annotate(count=Count('id'))
        )
    }
    submission_counts = {
        row['challenge_id']: row
        for row in (
            Submission.objects.filter(LIVE_USER)
            .values('challenge_id')
            .annotate(
                attempts=Count('id'),
                correct=Count('id', filter=Q(correct=True)),
            )
        )
    }

    first_solve = (
        Solve.objects.filter(challenge=OuterRef('pk'), user__deleted_at__isnull=True)
        .order_by('created_at', 'id')
    )
    challenges = (
        Challenge.objects.select_related('category')
        .annotate(
            first_user=Subquery(first_solve.values('user__username')[:1]),
            first_at=Subquery(first_solve.values('created_at')[:1]),
        )
        .order_by('id')
    )

    rows = []
    for challenge in challenges:
        submission = submission_counts.get(challenge.id)
        attempts = submission['attempts'] if submission else 0
        correct = submission['correct'] if submission else 0
        first_user = challenge.first_user
        first_at = challenge.first_at
        rows.append({
            'id': challenge.id,
            'title': challenge.title,
            'category': challenge.category.name if challenge.category_id else '',
            'difficulty': challenge.difficulty,
            'points': challenge.points,
            'solve_count': solve_counts.get(challenge.id, 0),
            'attempts': attempts,
            'correct': correct,
            # ``None`` (not 0) when nothing was attempted, so the UI can show a
            # dash instead of a misleading "0%".
            'success_rate': _success_rate(correct, attempts) if attempts else None,
            'first_blood': (
                {
                    'username': first_user,
                    'at': first_at.isoformat() if first_at else None,
                }
                if first_user else None
            ),
        })
    return rows


def _leaderboard():
    """Every non-deleted account with its real points and solve count.

    Ordered by points so the dashboard's CSV export can consume it directly;
    the Analytics "Top Solvers" list re-sorts the same rows by solve count.
    """
    return [
        {
            'username': row['username'],
            'email': row['email'],
            'role': row['role'],
            'status': row['status'],
            'points': row['points'],
            'solves': row['solved_count'],
        }
        for row in (
            User.objects.annotate(solved_count=Count('solves'))
            .order_by('-points', 'username')
            .values('username', 'email', 'role', 'status', 'points', 'solved_count')
        )
    ]


def _recent_activity():
    """The newest flag attempts (solves *and* misses) across the platform."""
    rows = (
        Submission.objects.filter(LIVE_USER)
        .order_by('-created_at', '-id')
        .values(
            'id',
            'user__username',
            'challenge_id',
            'challenge__title',
            'correct',
            'created_at',
        )[:RECENT_ACTIVITY_LIMIT]
    )
    return [
        {
            'id': row['id'],
            'username': row['user__username'],
            'challenge_id': row['challenge_id'],
            'challenge_title': row['challenge__title'],
            'correct': row['correct'],
            'created_at': row['created_at'].isoformat(),
        }
        for row in rows
    ]


def _top_solvers(leaderboard):
    """The players with the most real solves, ranked (ties: points, then name)."""
    ranked = sorted(
        (row for row in leaderboard if row['solves'] > 0),
        key=lambda row: (-row['solves'], -row['points'], row['username']),
    )
    return [
        {
            'rank': index + 1,
            'username': row['username'],
            'points': row['points'],
            'solves': row['solves'],
        }
        for index, row in enumerate(ranked[:TOP_SOLVERS])
    ]


def build_admin_analytics(*, now=None):
    """Return the admin Dashboard/Analytics aggregate payload as a plain dict."""
    now = now or timezone.now()

    submissions = Submission.objects.filter(LIVE_USER).aggregate(
        attempts=Count('id'),
        correct=Count('id', filter=Q(correct=True)),
    )
    attempts = submissions['attempts'] or 0
    correct = submissions['correct'] or 0
    leaderboard = _leaderboard()

    return {
        'totals': {
            'solves': Solve.objects.filter(LIVE_USER).count(),
            'attempts': attempts,
            'correct_submissions': correct,
            'success_rate': _success_rate(correct, attempts),
            'unique_solvers': (
                Solve.objects.filter(LIVE_USER).values('user_id').distinct().count()
            ),
        },
        'solve_series': _solve_series(now),
        'per_challenge': _per_challenge(),
        'top_solvers': _top_solvers(leaderboard),
        'recent_activity': _recent_activity(),
        'leaderboard': leaderboard,
    }


class AdminAnalyticsView(APIView):
    """GET /api/v1/admin/analytics/ - platform-wide, read-only aggregates."""

    permission_classes = [IsAuthenticated, HasAdminAnalyticsPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        return Response(build_admin_analytics())
