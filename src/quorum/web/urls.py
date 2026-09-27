from django.urls import path, re_path

from . import views_auth as auth
from . import views_judge as judge
from . import views_me as me
from . import views_org as org
from . import views_public as pub
from . import views_vote as vote

urlpatterns = [
    # public
    path("", pub.home),
    path("events", pub.events_list),
    path("e/<slug:slug>", pub.event_page),
    path("e/<slug:slug>/projects", pub.event_gallery),
    path("e/<slug:slug>/methodology", pub.event_methodology),
    path("e/<slug:slug>/vote", vote.ballot),
    path("e/<slug:slug>/results", vote.public_results),
    path("projects", pub.gallery),
    path("embed/events/<slug:slug>/gallery", pub.embed_gallery),
    path("p/<uuid:pid>", pub.project_page),
    path("p/<uuid:pid>/comment", me.post_comment),
    path("comments/<uuid:cid>/hide", me.hide_comment),
    path("methodology", pub.methodology),
    path("verify", vote.verify_page),
    path("certificates/<uuid:cid>", vote.certificate),
    re_path(r"^media/(?P<path>.+)$", pub.media),
    path(".well-known/quorum-keys.json", pub.keys),
    path(".well-known/quorum-revocations.json", pub.revocations),
    path("healthz", pub.healthz),
    path("readyz", pub.readyz),
    # auth
    path("login", auth.login_view),
    path("signup", auth.signup_view),
    path("logout", auth.logout_view),
    path("magic/<str:token>", auth.magic_view),
    path("settings/tokens", auth.tokens_view),
    # participant
    path("me", me.dashboard),
    path("me/e/<slug:slug>", me.team_hub),
    path("me/e/<slug:slug>/submit", me.new_submission),
    path("me/projects/<uuid:pid>/edit", me.edit_submission),
    path("me/scorecard/<slug:slug>", me.my_scorecard),
    path("invite/<str:code>", me.invite),
    # judge
    path("j", judge.judge_home),
    path("j/<slug:slug>", judge.inbox),
    path("j/<slug:slug>/review/<uuid:aid>", judge.review),
    path("j/<slug:slug>/recuse/<uuid:aid>", judge.recuse),
    path("j/<slug:slug>/tiebreak/<uuid:tid>", judge.tiebreak),
    path("j/<slug:slug>/compare/<str:track>", judge.compare),
    path("j/<slug:slug>/protocol", judge.protocol),
    # organizer
    path("o", org.org_home),
    path("o/new", org.org_new),
    path("o/<slug:slug>", org.overview),
    path("o/<slug:slug>/setup", org.setup),
    path("o/<slug:slug>/participants", org.participants),
    path("o/<slug:slug>/judges", org.judges),
    path("o/<slug:slug>/judges/stats", org.judge_stats),
    path("o/<slug:slug>/ops", org.ops_view),
    path("o/<slug:slug>/results", org.results),
    path("o/<slug:slug>/results/pairwise", org.pairwise_view),
    path("o/<slug:slug>/results/<str:ref>", org.explain),
    path("o/<slug:slug>/feedback", org.feedback_view),
    path("o/<slug:slug>/voting", org.voting_view),
    path("o/<slug:slug>/audit", org.audit_view),
    path("o/<slug:slug>/data", org.data_view),
    path("o/<slug:slug>/data/bundle.json", org.bundle_download),
]
