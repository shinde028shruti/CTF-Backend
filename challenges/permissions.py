"""Permission helpers for the challenge API.

The main CRUD views reuse ``users.permissions.DjangoModelPermissionsWithView``,
which maps HTTP methods onto the model's Django permissions
(``challenges.view/add/change/delete_challenge``). Some sub-actions (uploading
or removing an attachment) are state changes on the parent challenge, so they
require ``challenges.change_challenge`` explicitly.
"""

from rest_framework.permissions import BasePermission

CHANGE_CHALLENGE_PERMISSION = 'challenges.change_challenge'
VIEW_CHALLENGE_PERMISSION = 'challenges.view_challenge'


class HasChallengeChangePermission(BasePermission):
    """Allow only users holding ``challenges.change_challenge``."""

    message = (
        'You need the "challenges.change_challenge" permission to manage '
        'challenge content.'
    )

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.has_perm(CHANGE_CHALLENGE_PERMISSION)
        )
