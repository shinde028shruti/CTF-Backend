"""
Authentication backend that accepts either a username or an email address.

SimpleJWT's ``TokenObtainPairSerializer`` calls
``authenticate(username=<value from the "username" field>, password=...)``.
Django's default ``ModelBackend`` looks that value up with an exact match on
``USERNAME_FIELD`` (which is ``username``), so signing in with an email always
failed with "No active account found with the given credentials" - even though
the login form was labelled "Username or Email".

This backend resolves the identifier against both fields instead.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

User = get_user_model()


class UsernameOrEmailBackend(ModelBackend):
    """Authenticate against ``username`` OR ``email``, case-insensitively."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)

        if not username or not password:
            return None

        try:
            user = User.objects.get(
                Q(username__iexact=username) | Q(email__iexact=username)
            )
        except User.DoesNotExist:
            # Run the default password hasher anyway so that a missing user and
            # a wrong password take a comparable amount of time.
            User().set_password(password)
            return None
        except User.MultipleObjectsReturned:
            # An email shared by several accounts (or a username that collides
            # with another account's email) cannot identify a single user.
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None