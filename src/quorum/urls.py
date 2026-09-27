from django.contrib import admin
from django.urls import include, path

from quorum.api.router import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("", include("quorum.web.urls")),
]
