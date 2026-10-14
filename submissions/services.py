"""Solve/scoring logic for flag submissions and hint reveals.

Scoring rule (historical): a solve stores a *snapshot* of ``Challenge.points``
at the moment it is first accepted, and a hint unlock stores a *snapshot* of
``ChallengeHint.cost`` at the moment it is first unlocked. Later edits to a
challenge's points or a hint's cost never change the past, and a user's total
can always be reconstructed as ``sum(Solve.points) - sum(HintUnlock.cost)``.
The denormalised ``User.points`` and ``Challenge.solves`` counters are updated
with atomic ``F()`` expressions in the same transaction.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F

from challenges.models import Challenge

from .models import HintUnlock, Solve

User = get_user_model()

# ``unlock_hint`` outcomes.
UNLOCK_STATUS_UNLOCKED = 'unlocked'
UNLOCK_STATUS_ALREADY = 'already'
UNLOCK_STATUS_INSUFFICIENT = 'insufficient'


def award_solve(user, challenge):
    """Record ``user`` solving ``challenge`` exactly once, atomically.

    Returns ``(created, solve)``. ``created`` is ``True`` only for the call
    that actually awarded the points; every repeated or concurrent call gets
    ``(False, existing_solve)`` and awards nothing.

    Duplicate protection is layered:

    * ``select_for_update`` locks the challenge row so concurrent submissions
      serialise (a no-op on SQLite, effective on PostgreSQL/MySQL);
    * the ``unique_solve_per_user_challenge`` constraint is the ultimate guard,
      so even a true race cannot persist a second solve.
    """

    with transaction.atomic():
        locked = Challenge.objects.select_for_update().get(pk=challenge.pk)

        existing = Solve.objects.filter(user=user, challenge=locked).first()
        if existing is not None:
            return False, existing

        try:
            # A nested atomic() opens a savepoint: if the unique constraint
            # fires (a real race), only this insert rolls back and the outer
            # transaction stays usable for the follow-up read.
            with transaction.atomic():
                solve = Solve.objects.create(
                    user=user,
                    challenge=locked,
                    points=locked.points,
                )
        except IntegrityError:
            return False, Solve.objects.get(user=user, challenge=locked)

        User.objects.filter(pk=user.pk).update(
            points=F('points') + locked.points
        )
        Challenge.objects.filter(pk=locked.pk).update(
            solves=F('solves') + 1
        )
        return True, solve


def unlock_hint(user, hint):
    """Unlock ``hint`` for ``user`` exactly once, deducting its cost atomically.

    Returns ``(status, unlock, remaining_points)`` where ``status`` is one of
    ``UNLOCK_STATUS_UNLOCKED`` (points were charged now), ``UNLOCK_STATUS_ALREADY``
    (the hint was already unlocked; nothing is charged) or
    ``UNLOCK_STATUS_INSUFFICIENT`` (the user cannot afford it; nothing is
    charged and ``unlock`` is ``None``).

    Rules enforced here, never trusting the caller/client:

    * ``hint.cost`` is read from the database and snapshotted onto the unlock;
    * a balance is only deducted once per (user, hint), and never below zero;
    * the balance check, deduction and unlock insert happen in one transaction
      while the user's row is locked, so concurrent reveals cannot double-charge.

    Duplicate protection mirrors ``award_solve``: the ``select_for_update`` lock
    serialises a single user's reveals, the ``unique_hint_unlock_per_user_hint``
    constraint is the ultimate guard, and a nested ``atomic()`` savepoint keeps
    the outer transaction usable if that constraint fires in a true race.
    """

    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)

        existing = HintUnlock.objects.filter(
            user=locked_user, hint=hint
        ).first()
        if existing is not None:
            return UNLOCK_STATUS_ALREADY, existing, locked_user.points

        cost = hint.cost
        if locked_user.points < cost:
            return UNLOCK_STATUS_INSUFFICIENT, None, locked_user.points

        try:
            with transaction.atomic():
                unlock = HintUnlock.objects.create(
                    user=locked_user,
                    challenge_id=hint.challenge_id,
                    hint=hint,
                    cost=cost,
                )
        except IntegrityError:
            existing = HintUnlock.objects.get(user=locked_user, hint=hint)
            return UNLOCK_STATUS_ALREADY, existing, locked_user.points

        if cost:
            User.objects.filter(pk=locked_user.pk).update(
                points=F('points') - cost
            )

        return UNLOCK_STATUS_UNLOCKED, unlock, locked_user.points - cost
