"""Submission, solve-tracking and hint-unlock models.

Three tables on purpose:

* ``Submission`` is the append-only history of *every* flag attempt, correct
  or not. It deliberately does **not** store the submitted plaintext - only
  whether the attempt was right - so a guess can never leak into the database
  or a backup.
* ``Solve`` records a *successful* solve exactly once per (user, challenge).
  A unique constraint enforces that at the database level, so repeated or
  concurrent correct submissions can never create a second solve or award
  points twice.
* ``HintUnlock`` records a player unlocking a hint exactly once per (user,
  hint), together with the point cost charged. A unique constraint prevents a
  duplicate charge from a repeated or concurrent reveal.

Scoring is historical: ``Solve.points`` snapshots ``Challenge.points`` at the
moment of the solve and ``HintUnlock.cost`` snapshots ``ChallengeHint.cost`` at
the moment of the unlock, so later edits never rewrite history. ``User.points``
and ``Challenge.solves`` are denormalised fast counters kept in sync in the same
transaction (see ``submissions.services``).
"""

from django.conf import settings
from django.db import models

from challenges.models import Challenge, ChallengeHint


class Submission(models.Model):
    """A single flag submission attempt (correct or incorrect)."""

    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name='submissions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='submissions',
    )
    correct = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['user', 'challenge'])]

    def __str__(self):
        outcome = 'correct' if self.correct else 'incorrect'
        return f'{outcome} submission by {self.user_id} for {self.challenge_id}'


class Solve(models.Model):
    """A successful solve: at most one per (user, challenge)."""

    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name='solve_records',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='solves',
    )
    # Snapshot of the challenge's points at solve time (historical scoring).
    points = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'challenge'],
                name='unique_solve_per_user_challenge',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} solved {self.challenge_id} (+{self.points})'


class HintUnlock(models.Model):
    """A player unlocking a challenge hint: at most one per (user, hint).

    ``cost`` is a *snapshot* of ``ChallengeHint.cost`` at unlock time, so a
    later edit to the hint's price never rewrites how many points were actually
    spent. ``challenge`` is denormalised from ``hint.challenge`` for convenient
    per-challenge queries; it is always written as ``hint.challenge``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='hint_unlocks',
    )
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name='hint_unlocks',
    )
    hint = models.ForeignKey(
        ChallengeHint,
        on_delete=models.CASCADE,
        related_name='unlocks',
    )
    # Snapshot of the hint's cost at unlock time (historical scoring).
    cost = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['user', 'challenge'])]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'hint'],
                name='unique_hint_unlock_per_user_hint',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} unlocked hint {self.hint_id} (-{self.cost})'
