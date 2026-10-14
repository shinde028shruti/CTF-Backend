"""Permission classes for the admin user-management API.

Group membership is the platform's authorization boundary: ordinary users hold
no Django permissions at all, and only the ``Admin`` group carries
``auth.change_group``. Requiring that permission here keeps privileged group
assignment out of the ``role`` field and off any frontend check.
"""

from rest_framework.permissions import BasePermission, DjangoModelPermissions

CHANGE_GROUP_PERMISSION = 'auth.change_group'


class DjangoModelPermissionsWithView(DjangoModelPermissions):
    """``DjangoModelPermissions`` that also guards read methods.

    Stock DRF maps ``GET`` to no permission, so any authenticated user could
    read the admin lists. Here reads require the model's ``view_*`` permission
    (``users.view_user`` / ``auth.view_group``), matching how the Admin group is
    configured.
    """

    perms_map = {
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'OPTIONS': [],
        'HEAD': [],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }


class DjangoModelPermissionsRestore(DjangoModelPermissionsWithView):
    """POST /users/{id}/restore/ is a state change, not a creation.

    Stock ``POST`` maps to ``add_*``; restoring an account is ``change_user``.
    """

    perms_map = dict(
        DjangoModelPermissionsWithView.perms_map,
        POST=['%(app_label)s.change_%(model_name)s'],
    )


class HasGroupChangePermission(BasePermission):
    """Allow only users holding Django's ``auth.change_group`` permission."""

    message = (
        'You need the "auth.change_group" permission to manage group membership.'
    )

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.has_perm(CHANGE_GROUP_PERMISSION)
        )
