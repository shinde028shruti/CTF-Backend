"""Throttles for the submissions API.

Flag guessing is the classic offline/online brute-force target, so submissions
are limited per authenticated user. The rate is configured once in
``settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']``.

Hint reveals are idempotent (an already-unlocked hint is never charged again)
but still walk the hint/challenge tables, so a light per-user limit keeps a
client from hammering the endpoint while leaking nothing about hint ids.
"""

from rest_framework.throttling import UserRateThrottle


class FlagSubmitThrottle(UserRateThrottle):
    """Rate-limit flag submissions per authenticated user."""

    scope = 'flag_submit'


class HintRevealThrottle(UserRateThrottle):
    """Rate-limit hint reveals per authenticated user."""

    scope = 'hint_reveal'
