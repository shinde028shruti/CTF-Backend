"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/auth/', include('users.urls')),
    # Public challenge + category endpoints: /api/v1/challenges/, /categories/.
    path('api/v1/', include('challenges.urls')),
    # Flag submission: POST /api/v1/challenges/{id}/submit/.
    path('api/v1/', include('submissions.urls')),
    # In-app notifications: /api/v1/notifications/.
    path('api/v1/notifications/', include('notifications.urls')),
    # Public announcements: /api/v1/announcements/ (active published only).
    path('api/v1/', include('announcements.urls')),
    # Player achievement board: /api/v1/achievements/.
    path('api/v1/', include('achievements.urls')),
    # Admin challenge endpoints share the /api/v1/admin/ prefix with users.
    path('api/v1/admin/', include('users.admin_urls')),
    path('api/v1/admin/', include('challenges.admin_urls')),
    # Admin announcement management: /api/v1/admin/announcements/.
    path('api/v1/admin/', include('announcements.admin_urls')),
    # Admin achievement management: /api/v1/admin/achievements/.
    path('api/v1/admin/', include('achievements.admin_urls')),
    # Admin dashboard/analytics aggregates: /api/v1/admin/analytics/.
    path('api/v1/admin/', include('submissions.admin_urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
