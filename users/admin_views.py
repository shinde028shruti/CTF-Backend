"""Admin user-management API.

Authorization is enforced server-side with Django permissions
(``DjangoModelPermissions`` -> ``users.add/change/delete/view_user`` for the
user endpoints, and ``auth.view_group`` for the group list). The frontend role
is irrelevant here: holding ``role='admin'`` without the Django permissions
grants nothing.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.filters import SearchFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_serializers import (
    AdminGroupSerializer,
    AdminUserCreateSerializer,
    AdminUserGroupsSerializer,
    AdminUserSerializer,
    AdminUserStatusSerializer,
    AdminUserUpdateSerializer,
)
from .groups import ADMIN_GROUP_NAME
from .permissions import (
    DjangoModelPermissionsRestore,
    DjangoModelPermissionsWithView,
    HasGroupChangePermission,
)

User = get_user_model()


class AdminUserPagination(PageNumberPagination):
    """Paginate the user list and attach status counts to every page."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data):
        queryset = self.page.paginator.object_list
        summary = {
            'total': self.page.paginator.count,
            'active': queryset.filter(status=User.STATUS_ACTIVE).count(),
            'suspended': queryset.filter(status=User.STATUS_SUSPENDED).count(),
            'banned': queryset.filter(status=User.STATUS_BANNED).count(),
            # Global count of soft-deleted accounts, regardless of the current
            # page or its `deleted` filter - the Active/Deleted tab badge.
            'deleted': User.all_objects.filter(deleted_at__isnull=False).count(),
        }
        return Response(
            {
                'count': self.page.paginator.count,
                'next': self.get_next_link(),
                'previous': self.get_previous_link(),
                'summary': summary,
                'results': data,
            }
        )


class AdminUserListCreateView(generics.ListCreateAPIView):
    """GET /admin/users/ - list (with optional ?deleted=true). POST - create."""

    queryset = User.objects.all().order_by('id')
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    pagination_class = AdminUserPagination
    filter_backends = [SearchFilter]
    search_fields = ['username', 'email', 'first_name', 'last_name']

    def get_queryset(self):
        if self.request.query_params.get('deleted') == 'true':
            return (
                User.all_objects
                .filter(deleted_at__isnull=False)
                .order_by('-deleted_at')
            )
        return User.objects.all().order_by('id')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return AdminUserCreateSerializer
        return AdminUserSerializer


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /admin/users/{id}/."""

    queryset = User.objects.all()
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return AdminUserUpdateSerializer
        return AdminUserSerializer

    def get_object(self):
        if self.request.query_params.get('deleted') == 'true':
            queryset = User.all_objects.all()
        else:
            queryset = User.objects.all()
        target = get_object_or_404(queryset, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, target)
        return target

    @staticmethod
    def _guard_superuser(request, target):
        if target.is_superuser and not request.user.is_superuser:
            raise PermissionDenied(
                'Only a superuser can modify another superuser account.'
            )

    def update(self, request, *args, **kwargs):
        self._guard_superuser(request, self.get_object())
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        target = self.get_object()
        if target.pk == request.user.pk:
            raise ValidationError(
                {'detail': 'You cannot delete your own account.'}
            )
        self._guard_superuser(request, target)
        if (
            target.is_superuser
            and User.objects.filter(is_superuser=True).count() <= 1
        ):
            raise ValidationError(
                {'detail': 'You cannot delete the last superuser.'}
            )
        return super().destroy(request, *args, **kwargs)


class AdminUserStatusView(APIView):
    """PATCH /admin/users/{id}/status/ - set active/suspended/banned."""

    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]
    queryset = User.objects.all()

    def patch(self, request, pk):
        target = get_object_or_404(User, pk=pk)

        serializer = AdminUserStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data['status']
        note = serializer.validated_data.get('status_note', '')

        if target.pk == request.user.pk and new_status != User.STATUS_ACTIVE:
            raise ValidationError(
                {'detail': 'You cannot deactivate your own account.'}
            )

        if target.is_superuser and not request.user.is_superuser:
            raise PermissionDenied(
                'Only a superuser can change another superuser account.'
            )

        if target.is_superuser and new_status != User.STATUS_ACTIVE:
            others = (
                User.objects.filter(is_superuser=True, status=User.STATUS_ACTIVE)
                .exclude(pk=target.pk)
                .count()
            )
            if others == 0:
                raise ValidationError(
                    {'detail': 'You cannot deactivate the last superuser.'}
                )

        target.status = new_status
        target.status_note = note
        target.is_active = new_status == User.STATUS_ACTIVE
        target.save(update_fields=['status', 'status_note', 'is_active'])
        return Response(AdminUserSerializer(target).data)


class AdminUserGroupsView(APIView):
    """PATCH /admin/users/{id}/groups/ - replace group membership."""

    permission_classes = [IsAuthenticated, HasGroupChangePermission]

    def patch(self, request, pk):
        target = get_object_or_404(User, pk=pk)

        serializer = AdminUserGroupsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_groups = serializer.validated_data['groups']

        if target.is_superuser and not request.user.is_superuser:
            raise PermissionDenied(
                'Only a superuser can change another superuser account.'
            )

        admin_group = Group.objects.filter(name=ADMIN_GROUP_NAME).first()
        if (
            admin_group is not None
            and admin_group in target.groups.all()
            and admin_group not in new_groups
        ):
            remaining = admin_group.user_set.exclude(pk=target.pk).count()
            if remaining == 0:
                raise ValidationError(
                    {
                        'detail': (
                            f'You cannot remove the last member of the '
                            f'"{ADMIN_GROUP_NAME}" group.'
                        )
                    }
                )

        target.groups.set(new_groups)
        return Response(AdminUserSerializer(target).data)


class AdminGroupListView(generics.ListAPIView):
    """GET /admin/groups/ - groups an admin can assign."""

    queryset = Group.objects.all().order_by('name')
    serializer_class = AdminGroupSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissionsWithView]


class AdminUserRestoreView(APIView):
    """POST /admin/users/{id}/restore/ - undo a soft delete."""

    permission_classes = [IsAuthenticated, DjangoModelPermissionsRestore]
    # Required by DjangoModelPermissions (it reads view.queryset.model).
    queryset = User.all_objects.all()

    def post(self, request, pk):
        target = get_object_or_404(User.all_objects, pk=pk)

        if target.deleted_at is None:
            raise ValidationError(
                {'detail': 'This account is not deleted.'}
            )

        if target.is_superuser and not request.user.is_superuser:
            raise PermissionDenied(
                'Only a superuser can restore another superuser account.'
            )

        # Full restore to a usable state, keeping the is_active <-> status
        # lockstep that the status endpoint maintains.
        target.deleted_at = None
        target.is_active = True
        target.status = User.STATUS_ACTIVE
        target.status_note = ''
        target.save(
            update_fields=['deleted_at', 'is_active', 'status', 'status_note']
        )
        return Response(AdminUserSerializer(target).data)
