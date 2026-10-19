"""Notification creation helper.

One entry point so every producer validates the same way. It is deliberately
import-light (it only touches :mod:`notifications.models`) so that
``submissions.services`` can call it from inside the solve transaction without
introducing an import cycle.

The recipient is always supplied by server-side code - never from request data -
so a client cannot create a notification for somebody else.
"""

from .models import Notification


def notify(recipient, *, type=Notification.TYPE_SYSTEM, title, message,
           destination_url=''):
    """Create and return a :class:`~notifications.models.Notification`.

    ``full_clean`` enforces the closed ``type`` vocabulary, the column length
    limits and the internal-path rule for ``destination_url`` before anything is
    written, so an invalid row can never be persisted. A ``ValidationError`` here
    means the *calling code* is wrong, not the request.
    """
    notification = Notification(
        recipient=recipient,
        type=type,
        title=title,
        message=message,
        destination_url=destination_url or '',
    )
    notification.full_clean()
    notification.save()
    return notification
