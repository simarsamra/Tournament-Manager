"""Custom context processors for the tournament manager."""


def notification_count(request):
    """Inject unread notification count into every template context."""
    if not request.user.is_authenticated:
        return {"unread_notification_count": 0}
    from .models import Notification
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return {"unread_notification_count": count}
