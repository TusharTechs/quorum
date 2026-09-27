from django.urls import path, re_path

from . import views_auth as auth
from . import views_public as pub

urlpatterns = [
    path("", pub.home),
    path("events", pub.events_list),
    path("e/<slug:slug>", pub.event_page),
    path("e/<slug:slug>/projects", pub.event_gallery),
    path("e/<slug:slug>/methodology", pub.event_methodology),
    path("projects", pub.gallery),
    path("p/<uuid:pid>", pub.project_page),
    path("methodology", pub.methodology),
    re_path(r"^media/(?P<path>.+)$", pub.media),
    path(".well-known/quorum-keys.json", pub.keys),
    path("healthz", pub.healthz),
    path("readyz", pub.readyz),
    path("login", auth.login_view),
    path("signup", auth.signup_view),
    path("logout", auth.logout_view),
    path("magic/<str:token>", auth.magic_view),
    path("settings/tokens", auth.tokens_view),
]
