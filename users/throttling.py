"""Throttles for the authentication/account-security API.

Password change is the natural target for a logged-in attacker who has stolen
an access token and wants to lock the real owner out (or to brute-force the
current password from inside an authenticated session). It is limited per
authenticated user. The rate is configured once in
``settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']``.
"""

from rest_framework.throttling import UserRateThrottle


class PasswordChangeThrottle(UserRateThrottle):
    """Rate-limit password changes per authenticated user."""

    scope = 'password_change'
