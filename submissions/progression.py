"""Player level / rank / Elite progression (read-only, derived).

Level is a pure function of a player's *lifetime earned* points, i.e. the gross
sum of ``Solve.points`` before any hint spending. It deliberately does not read
``User.points``: that counter also carries non-solve grants (demo seeding and
test fixtures write it directly - see ``users/management/commands/
create_demo_users.py``), so it is not reconstructable from authoritative rows
and can drift from what a player actually earned. ``earnedPoints`` is the one
truthful basis, and unlocking a hint never lowers it, so spending points on a
hint can never demote a player.

There is no stored level column and no migration: the ladder below is evaluated
on read inside ``build_user_statistics``, which already computes the
``earnedPoints`` aggregate. Re-tuning the ladder is therefore a code change that
re-tiers everyone at once and can never go stale or require a backfill.

The ladder is absolute, not a fraction of the published board, so a tier never
regresses when an admin publishes new challenges - which is what "consistent
progression" requires.
"""

# (tier name, minimum lifetime earned points). Strictly increasing, starts at 0.
PROGRESSION_TIERS = (
    ('Rookie', 0),
    ('Novice', 100),
    ('Intermediate', 250),
    ('Advanced', 500),
    ('Elite', 800),
)

# The top tier's entry threshold, reused as the denominator of the capped
# 0-100 "Elite progress" figure the UI shows.
ELITE_MIN = PROGRESSION_TIERS[-1][1]


def _tier_index(points):
    """Index of the highest tier whose minimum ``points`` has reached."""
    index = 0
    for position, (_name, minimum) in enumerate(PROGRESSION_TIERS):
        if points >= minimum:
            index = position
        else:
            break
    return index


def progression_for(earned_points):
    """Return the derived level/rank/Elite progression for ``earned_points``.

    ``earned_points`` is the caller's gross ``sum(Solve.points)``. The result is
    a plain, JSON-ready dict consumed by ``UserStatsSerializer`` and rendered
    identically by the Dashboard, Sidebar and Profile so all three agree:

    * ``level``         1-based tier index (1 = Rookie, 5 = Elite);
    * ``tier``          the tier name;
    * ``points``        the lifetime earned figure the tier was derived from;
    * ``levelMin``      points at which the current tier was entered;
    * ``nextLevelMin``  points for the next tier, or ``None`` at Elite;
    * ``toNext``        points still needed for the next tier (0 at Elite);
    * ``progressPct``   0-100 progress within the current tier (100 at Elite);
    * ``elitePct``      0-100 cumulative progress toward Elite, capped at 100;
    * ``isMax``         ``True`` once the top tier is reached.
    """
    points = max(0, int(earned_points or 0))
    index = _tier_index(points)
    name, level_min = PROGRESSION_TIERS[index]
    is_max = index == len(PROGRESSION_TIERS) - 1

    if is_max:
        next_min = None
        to_next = 0
        progress_pct = 100
    else:
        next_min = PROGRESSION_TIERS[index + 1][1]
        span = next_min - level_min
        # Integer floor: progress never overstates how close the next tier is
        # (and only reads 100 once the tier has actually been reached).
        progress_pct = (points - level_min) * 100 // span if span else 100
        to_next = next_min - points

    elite_pct = min(100, points * 100 // ELITE_MIN) if ELITE_MIN else 100

    return {
        'level': index + 1,
        'tier': name,
        'points': points,
        'levelMin': level_min,
        'nextLevelMin': next_min,
        'toNext': to_next,
        'progressPct': progress_pct,
        'elitePct': elite_pct,
        'isMax': is_max,
    }
