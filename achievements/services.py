"""Server-side achievement evaluation.

The evaluator is the single place an award is created. It is triggered only from
the successful-solve path (``submissions.services.award_solve``), never on
unrelated requests and never from client-supplied data:

* only **active, non-deleted** definitions are considered;
* the user's solved history is read from the database (``Solve`` rows), not from
  anything the browser sends;
* dispatch is by validated ``criteria_type`` only - no achievement name is ever
  hardcoded - so a new definition with a supported type works without a code
  change;
* every award is idempotent: the ``unique_achievement_per_user`` constraint plus
  a nested savepoint mean a repeated or concurrent evaluation can never award
  the same achievement twice;
* each *new* award raises one in-app notification of type ``achievement``.
"""

from collections import defaultdict

from django.db import IntegrityError, transaction

from notifications.models import Notification
from notifications.services import notify
from submissions.models import Solve

from .criteria import progress, validate_config
from .models import Achievement, UserAchievement


def progress_context(user):
    """Return ``(solved_ids, per_category)`` for ``user`` in one query.

    ``solved_ids`` is the set of solved challenge ids (distinct by the
    ``unique_solve_per_user_challenge`` constraint) and ``per_category`` maps
    ``category_id -> solved count``. Building both once keeps a full evaluation
    (or a whole API listing) at a constant number of queries.
    """
    rows = Solve.objects.filter(user=user).values_list(
        'challenge_id', 'challenge__category_id'
    )
    solved_ids = set()
    per_category = defaultdict(int)
    for challenge_id, category_id in rows:
        solved_ids.add(challenge_id)
        if category_id is not None:
            per_category[category_id] += 1
    return solved_ids, per_category


def award(user, achievement):
    """Create the award exactly once; return the row, or ``None`` if it existed.

    A nested ``atomic()`` opens a savepoint so a true race that trips the unique
    constraint only rolls back this insert and leaves the outer transaction
    usable, mirroring ``submissions.services``.
    """
    try:
        with transaction.atomic():
            return UserAchievement.objects.create(user=user, achievement=achievement)
    except IntegrityError:
        return None


def pending_achievements(user, definitions=None):
    """Active achievements ``user`` already satisfies but has not yet earned.

    Pure read: creates nothing. ``definitions`` may be passed to avoid
    re-querying the catalogue when sweeping many users.
    """
    if definitions is None:
        definitions = list(Achievement.objects.active())
    if not definitions:
        return []

    earned_ids = set(
        UserAchievement.objects.filter(user=user).values_list(
            'achievement_id', flat=True
        )
    )
    solved_ids, per_category = progress_context(user)

    pending = []
    for achievement in definitions:
        if achievement.pk in earned_ids:
            continue
        # Re-validate defensively: a rule that somehow became invalid can never
        # award anything.
        try:
            config = validate_config(
                achievement.criteria_type, achievement.criteria_config
            )
        except Exception:
            continue
        gained, target = progress(
            achievement.criteria_type, config, solved_ids, per_category
        )
        if target <= 0 or gained < target:
            continue
        pending.append(achievement)
    return pending


def evaluate_achievements(user, definitions=None):
    """Award every active achievement ``user`` now satisfies.

    Returns the list of newly created :class:`UserAchievement` rows (empty when
    nothing new was earned, or when no definitions exist - the common case, and
    a single cheap query). Intended to run inside the solve transaction.
    """
    newly_earned = []
    for achievement in pending_achievements(user, definitions):
        record = award(user, achievement)
        if record is None:
            continue

        notify(
            recipient=user,
            type=Notification.TYPE_ACHIEVEMENT,
            title='Achievement unlocked',
            message=f'You earned "{achievement.name}".',
            destination_url='/achievements',
        )
        newly_earned.append(record)

    return newly_earned
