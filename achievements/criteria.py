"""The closed, validated set of achievement unlock rules.

Supported criteria types (and the exact ``criteria_config`` shape each accepts):

* ``first_solve``       - ``{}``                - solve the first distinct challenge.
* ``total_solves``      - ``{"target": n}``     - solve ``n`` distinct challenges.
* ``category_solves``   - ``{"category": id, "target": n}``
                                                - solve ``n`` in one category.
* ``challenge_solved``  - ``{"challenge": id}`` - solve one specific challenge.

Nothing else is accepted: there is no arbitrary expression, code path or
free-form rule. ``validate_config`` is the single source of truth, used by the
model's ``clean`` and the admin write serializer, so a definition can never be
persisted with an unsupported type, a non-positive target or a dangling
category/challenge reference.

The module also owns progress and the user-facing label so the evaluator and the
API report the same numbers and wording.
"""

from django.core.exceptions import ValidationError

from challenges.models import Category, Challenge

FIRST_SOLVE = 'first_solve'
TOTAL_SOLVES = 'total_solves'
CATEGORY_SOLVES = 'category_solves'
CHALLENGE_SOLVED = 'challenge_solved'

CRITERIA_TYPES = (FIRST_SOLVE, TOTAL_SOLVES, CATEGORY_SOLVES, CHALLENGE_SOLVED)


def _positive_int(value, field):
    """Coerce ``value`` to a whole number >= 1, or raise a field error."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ValidationError({field: 'Enter a whole number.'})
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: 'Enter a whole number.'})
    if number < 1:
        raise ValidationError({field: 'Enter a number of 1 or more.'})
    return number


def _existing_id(value, field, model):
    if isinstance(value, bool):
        raise ValidationError({field: 'Select a valid option.'})
    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: 'Select a valid option.'})
    if not model.objects.filter(pk=pk).exists():
        raise ValidationError({field: 'Select an existing option.'})
    return pk


def validate_config(criteria_type, config):
    """Return a normalised config dict, or raise ``ValidationError``.

    ``config`` is treated as untrusted: an unknown type, an invalid/missing
    value or a reference to a category/challenge that does not exist is
    rejected before anything is written.
    """
    config = config if isinstance(config, dict) else {}

    if criteria_type == FIRST_SOLVE:
        return {}
    if criteria_type == TOTAL_SOLVES:
        return {'target': _positive_int(config.get('target'), 'target')}
    if criteria_type == CATEGORY_SOLVES:
        return {
            'category': _existing_id(config.get('category'), 'category', Category),
            'target': _positive_int(config.get('target'), 'target'),
        }
    if criteria_type == CHALLENGE_SOLVED:
        return {
            'challenge': _existing_id(
                config.get('challenge'), 'challenge', Challenge
            ),
        }
    raise ValidationError({'criteria_type': 'Unsupported criteria type.'})


def progress(criteria_type, config, solved_ids, per_category):
    """Return ``(gained, target)`` for one rule against a solved-history view.

    ``solved_ids`` is the set of solved challenge ids and ``per_category`` maps
    ``category_id -> solved count``; both are computed once per request by the
    caller so this stays a pure function with no queries.
    """
    config = config if isinstance(config, dict) else {}

    if criteria_type == FIRST_SOLVE:
        return len(solved_ids), 1
    if criteria_type == TOTAL_SOLVES:
        return len(solved_ids), int(config.get('target') or 1)
    if criteria_type == CATEGORY_SOLVES:
        try:
            category_id = int(config.get('category'))
        except (TypeError, ValueError):
            return 0, 1
        return per_category.get(category_id, 0), int(config.get('target') or 1)
    if criteria_type == CHALLENGE_SOLVED:
        try:
            challenge_id = int(config.get('challenge'))
        except (TypeError, ValueError):
            return 0, 1
        return (1 if challenge_id in solved_ids else 0), 1
    return 0, 1


def describe(criteria_type, config, category_names=None, challenge_titles=None):
    """A short, safe human summary of a rule, for the API/UI.

    Optional lookup dicts let a caller batch category/challenge names once per
    response instead of issuing a query per achievement.
    """
    config = config if isinstance(config, dict) else {}
    category_names = category_names or {}
    challenge_titles = challenge_titles or {}

    if criteria_type == FIRST_SOLVE:
        return 'Solve your first challenge'
    if criteria_type == TOTAL_SOLVES:
        target = int(config.get('target') or 1)
        return f'Solve {target} challenge{"s" if target != 1 else ""}'
    if criteria_type == CATEGORY_SOLVES:
        target = int(config.get('target') or 1)
        name = category_names.get(_as_int(config.get('category')), 'a category')
        return f'Solve {target} in {name}'
    if criteria_type == CHALLENGE_SOLVED:
        title = challenge_titles.get(_as_int(config.get('challenge')))
        return f'Solve "{title}"' if title else 'Solve a specific challenge'
    return 'Unlock requirement'


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
