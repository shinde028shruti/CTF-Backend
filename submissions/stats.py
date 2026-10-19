"""Per-user statistics for the dashboard and profile (private, read-only).

Every value is a projection over the stored solve / hint-unlock / submission
history and the shared scoring rule; nothing is trusted from the client and no
second scoring system is introduced. The two point figures are deliberately
distinct:

* ``points`` is the authoritative *spendable* balance (``User.points``), which
  is already net of hint spending and is kept in sync in the same transaction
  as every solve/unlock;
* ``earnedPoints`` is the gross sum of ``Solve.points`` before hint deductions,
  and ``spentOnHints`` is the sum of ``HintUnlock.cost``.

So ``points == earnedPoints - spentOnHints`` always holds, and a player can see
how much they have earned separately from what is left to spend.

``progression`` (level / tier / Elite) is derived from ``earnedPoints``, never
from the spendable balance, so spending points on a hint can never demote a
player. See ``progression.py``.

The function issues a constant number of queries (no per-row/N+1 lookups): a
handful of aggregates, one query per relation for the recent-activity feed, and
two grouped queries for the progress chart.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from challenges.models import Category, Challenge

from .leaderboard import rank_for
from .models import HintUnlock, Solve, Submission
from .progression import progression_for

DEFAULT_ACTIVITY_LIMIT = 10
WEEKLY_DAYS = 7
MONTHLY_DAYS = 30
MONTHLY_BUCKETS = 10
MONTHLY_BUCKET_DAYS = MONTHLY_DAYS // MONTHLY_BUCKETS  # 3


def _net_points_by_day(user, since):
    """Map each local date >= ``since`` to (solve points - hint spend) that day."""
    net = defaultdict(int)

    solve_rows = (
        Solve.objects.filter(user=user, created_at__gte=since)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('points'))
    )
    for row in solve_rows:
        net[row['day']] += row['total'] or 0

    hint_rows = (
        HintUnlock.objects.filter(user=user, created_at__gte=since)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('cost'))
    )
    for row in hint_rows:
        net[row['day']] -= row['total'] or 0

    return net


def _progress(user, now):
    """Progress buckets, net of hint spending, for the profile chart.

    * weekly  - the current calendar week, Monday through Sunday;
    * monthly - the trailing 30 days, grouped into ten 3-day periods.

    Sundays/buckets with no activity report 0, which is what the chart renders
    as a rest day rather than a gap.
    """
    today = timezone.localdate(now)
    week_start = today - timedelta(days=today.weekday())
    month_start = today - timedelta(days=MONTHLY_DAYS - 1)
    earliest = min(week_start, month_start)

    since = timezone.make_aware(
        datetime.combine(earliest, time.min), timezone.get_current_timezone()
    )
    net = _net_points_by_day(user, since)

    weekly = [
        {
            'label': (week_start + timedelta(days=offset)).strftime('%a'),
            'points': net.get(week_start + timedelta(days=offset), 0),
        }
        for offset in range(WEEKLY_DAYS)
    ]

    monthly = []
    for index in range(MONTHLY_BUCKETS):
        bucket_start = month_start + timedelta(days=index * MONTHLY_BUCKET_DAYS)
        total = sum(
            net.get(bucket_start + timedelta(days=step), 0)
            for step in range(MONTHLY_BUCKET_DAYS)
        )
        monthly.append({
            'label': f'{bucket_start:%b} {bucket_start.day}',
            'points': total,
        })

    return {'weekly': weekly, 'monthly': monthly}


def category_progress(user):
    """Per-category published total plus the caller's own solved/points.

    ``total`` counts *published* challenges only, so drafts never inflate the
    denominator. A category is included when it has published challenges or the
    caller has solved one in it, so an empty board stays empty.
    """
    categories = Category.objects.annotate(
        total=Count(
            'challenges',
            filter=Q(challenges__status=Challenge.STATUS_PUBLISHED),
        )
    ).order_by('order', 'name')

    mine = {
        row['challenge__category_id']: row
        for row in (
            Solve.objects.filter(user=user)
            .values('challenge__category_id')
            .annotate(solved=Count('id'), points=Sum('points'))
        )
    }

    progress = []
    for category in categories:
        row = mine.get(category.id)
        solved = row['solved'] if row else 0
        points = (row['points'] if row else 0) or 0
        if category.total == 0 and solved == 0:
            continue
        progress.append({
            'id': category.id,
            'name': category.name,
            'color': category.color,
            'solved': solved,
            'total': category.total,
            'points': points,
        })
    return progress


def recent_activity(user, limit=DEFAULT_ACTIVITY_LIMIT):
    """The caller's most recent solves and hint unlocks, newest first."""
    solves = (
        Solve.objects.filter(user=user)
        .select_related('challenge')
        .order_by('-created_at', '-id')[:limit]
    )
    hints = (
        HintUnlock.objects.filter(user=user)
        .select_related('challenge')
        .order_by('-created_at', '-id')[:limit]
    )

    entries = [
        {
            'type': 'solve',
            'challengeId': solve.challenge_id,
            'challengeName': solve.challenge.title,
            'points': solve.points,
            'cost': 0,
            'timestamp': solve.created_at,
        }
        for solve in solves
    ]
    entries += [
        {
            'type': 'hint',
            'challengeId': unlock.challenge_id,
            'challengeName': unlock.challenge.title,
            'points': 0,
            'cost': unlock.cost,
            'timestamp': unlock.created_at,
        }
        for unlock in hints
    ]
    entries.sort(key=lambda entry: entry['timestamp'], reverse=True)
    return entries[:limit]


def build_user_statistics(user, *, now=None, activity_limit=DEFAULT_ACTIVITY_LIMIT):
    """Return the caller's own private statistics as a plain dict.

    Only the given ``user``'s rows are ever read; the view additionally checks
    that the request is authenticated.
    """
    now = now or timezone.now()

    solve_totals = Solve.objects.filter(user=user).aggregate(
        earned=Sum('points'), solved=Count('id')
    )
    spent = (
        HintUnlock.objects.filter(user=user).aggregate(total=Sum('cost'))['total']
        or 0
    )
    submissions = Submission.objects.filter(user=user).aggregate(
        total=Count('id'),
        correct=Count('id', filter=Q(correct=True)),
    )
    solved_ids = list(
        Solve.objects.filter(user=user).values_list('challenge_id', flat=True)
    )

    earned = solve_totals['earned'] or 0
    solved = solve_totals['solved'] or 0
    submissions_total = submissions['total'] or 0
    submissions_correct = submissions['correct'] or 0
    accuracy = (
        round(submissions_correct * 100 / submissions_total)
        if submissions_total else 0
    )

    return {
        'points': user.points,
        'earnedPoints': earned,
        'spentOnHints': spent,
        'solved': solved,
        'accuracy': accuracy,
        'submissions': {'total': submissions_total, 'correct': submissions_correct},
        'rank': rank_for(user, now=now),
        'joinDate': user.date_joined,
        'solvedChallengeIds': solved_ids,
        'categoryProgress': category_progress(user),
        'recentActivity': recent_activity(user, limit=activity_limit),
        'progress': _progress(user, now),
        'progression': progression_for(earned),
    }
