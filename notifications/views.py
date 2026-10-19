"""Notification API.

    GET    /api/v1/notifications/                 (authenticated, paginated)
    GET    /api/v1/notifications/unread-count/    (authenticated)
    PATCH  /api/v1/notifications/{id}/read/       (authenticated, owner only)
    POST   /api/v1/notifications/mark-all-read/   (authenticated, owner only)
    DELETE /api/v1/notifications/{id}/            (authenticated, owner only)

Every query is scoped to ``request.user``, so a notification id belonging to
another account behaves exactly like a missing id: 404. No endpoint reads a
recipient, type, title, message or destination from the request body - the only
writable state is ``is_read``/``read_at``, and only on the caller's own rows.
"""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer


class NotificationPagination(PageNumberPagination):
    """Same envelope and ``?limit=`` convention as the leaderboard list."""

    page_size = 20
    page_size_query_param = 'limit'
    max_page_size = 100


class NotificationListView(APIView):
    """GET / - the caller's notifications, newest first."""

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        queryset = Notification.objects.filter(recipient=request.user)
        paginator = NotificationPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = NotificationSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class UnreadCountView(APIView):
    """GET /unread-count/ - the caller's unread total."""

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        count = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).count()
        return Response({'unread_count': count})


class NotificationReadView(APIView):
    """PATCH /{id}/read/ - idempotent, owner-only mark-as-read.

    The body is ignored entirely, so a caller cannot alter the recipient, title,
    message, type or destination; only ``is_read``/``read_at`` change. A second
    call is a no-op and returns the same row.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ['patch', 'head', 'options']

    def patch(self, request, pk):
        notification = get_object_or_404(
            Notification, pk=pk, recipient=request.user
        )
        notification.mark_read()
        return Response(NotificationSerializer(notification).data)


class MarkAllReadView(APIView):
    """POST /mark-all-read/ - mark every unread row of the caller as read."""

    permission_classes = [IsAuthenticated]
    http_method_names = ['post', 'head', 'options']

    def post(self, request):
        # Scoped to the caller, so another user's rows are never touched.
        updated = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True, read_at=timezone.now())
        return Response({'updated': updated, 'unread_count': 0})


class NotificationDetailView(APIView):
    """DELETE /{id}/ - owner-only removal."""

    permission_classes = [IsAuthenticated]
    http_method_names = ['delete', 'head', 'options']

    def delete(self, request, pk):
        notification = get_object_or_404(
            Notification, pk=pk, recipient=request.user
        )
        notification.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
