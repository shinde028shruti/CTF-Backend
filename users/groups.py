"""Helpers for the platform's default user group.

The single source of truth for group *configuration* is the ``setup_groups``
management command. Nothing in this module creates a group or a permission; it
only reads the ``User`` group that ``setup_groups`` owns, and fails loudly when
that group has not been created yet.
"""

from django.contrib.auth.models import Group
from rest_framework.exceptions import APIException

DEFAULT_GROUP_NAME = 'User'
ADMIN_GROUP_NAME = 'Admin'


class UserGroupNotConfigured(APIException):
    """The default group is missing, so a new account cannot be provisioned."""

    status_code = 503
    default_detail = (
        f'The default "{DEFAULT_GROUP_NAME}" group is not configured. '
        'Run: python manage.py setup_groups'
    )
    default_code = 'default_group_not_configured'


def get_default_group():
    """Return the configured default group, or ``None`` if it does not exist."""
    return Group.objects.filter(name=DEFAULT_GROUP_NAME).first()


def assign_default_group(user):
    """Add ``user`` to the default group.

    Reads - never creates - the group owned by ``setup_groups``, so it cannot
    become a second permission configuration. Raises
    ``UserGroupNotConfigured`` when setup has not been run, which lets the
    caller's transaction roll back instead of committing a half-provisioned
    account.
    """
    group = get_default_group()
    if group is None:
        raise UserGroupNotConfigured()
    user.groups.add(group)
    return group
