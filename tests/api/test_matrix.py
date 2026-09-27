"""Route x role authorization matrix for EVERY API operation.

Each operation is classified; the test fails if an operation exists that is not in the
table (so a new endpoint cannot ship without an explicit authorization decision), if a
role that must be refused gets anything but 401/403, or if an allowed role is refused
for an authorization reason (business-state refusals such as deadline_passed are fine).
"""

import io
import json
import re
import uuid

import pytest

ROLES = ["anon", "participant", "judge_a", "judge_b", "organizer", "admin"]
STATE_CODES = {"deadline_passed", "window_closed", "results_not_published", "voter_unverified", "judging_closed",
               "results_locked", "not_released", "tally_hidden", "wrong_voting_mode", "method_locked",
               "submissions_not_open", "own_team", "ballot_token_invalid", "ballot_token_required", "voting_closed"}

P = "public"
A = "auth"
O = "organizer"
J = "judge"
C = "creator"      # organizers anywhere + admins may create events
S = "self_judge"   # judge_a's own scores: judge_a + organizer + admin

MATRIX = {
    ("GET", "/api/v1/events"): P, ("POST", "/api/v1/events"): C, ("GET", "/api/v1/events/{e}"): P,
    ("PATCH", "/api/v1/events/{e}"): O, ("POST", "/api/v1/events/{e}/clone"): O, ("PUT", "/api/v1/events/{e}/rubric"): O,
    ("GET", "/api/v1/events/{e}/method"): P, ("POST", "/api/v1/events/{e}/publish"): O, ("POST", "/api/v1/events/{e}/phase"): O,
    ("POST", "/api/v1/events/{e}/tracks"): O, ("POST", "/api/v1/events/{e}/prizes"): O,
    ("POST", "/api/v1/events/{e}/questions"): O, ("POST", "/api/v1/events/{e}/teams"): A,
    ("GET", "/api/v1/events/{e}/my-team"): A, ("GET", "/api/v1/teams/{tid}"): "team",
    ("POST", "/api/v1/teams/{tid}/invite"): "team", ("POST", "/api/v1/invites/accept"): A,
    ("POST", "/api/v1/teams/{tid}/leave"): "team_member",
    ("GET", "/api/v1/events/{e}/projects"): P, ("POST", "/api/v1/events/{e}/projects"): "participant",
    ("GET", "/api/v1/projects/{pid}"): P, ("PATCH", "/api/v1/projects/{pid}"): "team_member",
    ("POST", "/api/v1/projects/{pid}/submit"): "team_member", ("POST", "/api/v1/projects/{pid}/withdraw"): "team_member",
    ("GET", "/api/v1/projects/{pid}/revisions"): "team", ("POST", "/api/v1/projects/{pid}/images"): "team_member",
    ("GET", "/api/v1/me/scores"): J, ("GET", "/api/v1/events/{e}/judges/{j}/scores"): S,
    ("GET", "/api/v1/events/{e}/judges"): O, ("POST", "/api/v1/events/{e}/judges"): O,
    ("PATCH", "/api/v1/events/{e}/judges/{j}"): O, ("GET", "/api/v1/events/{e}/conflicts"): O,
    ("POST", "/api/v1/events/{e}/conflicts"): O, ("GET", "/api/v1/me/assignments"): J,
    ("GET", "/api/v1/assignments/{aid}/review"): "assignee", ("PUT", "/api/v1/assignments/{aid}/review"): "assignee",
    ("POST", "/api/v1/assignments/{aid}/recuse"): "assignee",
    ("GET", "/api/v1/events/{e}/ops"): O, ("POST", "/api/v1/events/{e}/assignments/plan"): O,
    ("POST", "/api/v1/events/{e}/rebalance/plan"): O, ("POST", "/api/v1/events/{e}/assignments/commit"): O,
    ("POST", "/api/v1/events/{e}/batches/send"): O, ("POST", "/api/v1/events/{e}/nudge"): O,
    ("POST", "/api/v1/events/{e}/rankings"): O, ("GET", "/api/v1/events/{e}/rankings/latest"): O,
    ("GET", "/api/v1/events/{e}/rankings/latest/explain/{ref}"): O, ("GET", "/api/v1/events/{e}/ties"): O,
    ("POST", "/api/v1/events/{e}/focus/plan"): O, ("POST", "/api/v1/events/{e}/focus/commit"): O,
    ("POST", "/api/v1/events/{e}/tiebreaks"): O, ("GET", "/api/v1/tiebreaks/{tid}"): O,
    ("POST", "/api/v1/tiebreaks/{tid}/resolve"): O, ("GET", "/api/v1/me/tiebreaks/{tid}/next"): A,
    ("POST", "/api/v1/me/tiebreaks/{tid}/compare"): A,
    ("POST", "/api/v1/events/{e}/results/lock"): O, ("POST", "/api/v1/events/{e}/results/publish"): O,
    ("POST", "/api/v1/events/{e}/results/reopen"): O, ("GET", "/api/v1/events/{e}/results"): "published",
    ("GET", "/api/v1/events/{e}/feedback"): O, ("POST", "/api/v1/reviews/{rid}/moderate"): O,
    ("POST", "/api/v1/events/{e}/feedback/approve-all"): O, ("POST", "/api/v1/events/{e}/feedback/release"): O,
    ("GET", "/api/v1/me/scorecard"): "participant", ("GET", "/api/v1/events/{e}/ballot"): A,  # the fixture event uses signed-in voting
    ("POST", "/api/v1/events/{e}/voters/email"): P, ("POST", "/api/v1/events/{e}/votes"): A,
    ("GET", "/api/v1/events/{e}/votes/tally"): "tally", ("POST", "/api/v1/events/{e}/vote-codes"): O,
    ("GET", "/api/v1/events/{e}/integrity-flags"): O, ("POST", "/api/v1/events/{e}/integrity-flags/resolve"): O,
    ("GET", "/api/v1/projects/{pid}/comments"): P, ("POST", "/api/v1/projects/{pid}/comments"): A,
    ("POST", "/api/v1/comments/{cid}/hide"): "comment_org",
    ("GET", "/api/v1/events/{e}/audit"): O, ("GET", "/api/v1/events/{e}/audit/verify"): O,
    ("GET", "/api/v1/events/{e}/audit/checkpoints"): P, ("GET", "/api/v1/me/certificates"): A,
    ("GET", "/api/v1/certificates/{cid}"): P, ("POST", "/api/v1/verify"): P,
    ("GET", "/api/v1/events/{e}/exports/{kind}.csv"): O, ("GET", "/api/v1/events/{e}/exports/bundle.json"): O,
    ("POST", "/api/v1/bundles/verify"): P, ("POST", "/api/v1/events/{e}/imports"): O,
    ("POST", "/api/v1/imports/{jid}/mapping"): "import_org", ("POST", "/api/v1/imports/{jid}/commit"): "import_org",
    ("GET", "/api/v1/events/{e}/webhooks"): O, ("POST", "/api/v1/events/{e}/webhooks"): O,
    ("DELETE", "/api/v1/events/{e}/webhooks/{hid}"): O,
    ("GET", "/api/v1/events/{e}/pairwise"): O, ("POST", "/api/v1/events/{e}/tracks/{track}/pairwise"): O,
    ("GET", "/api/v1/me/events/{e}/pairwise"): J,
    ("POST", "/api/v1/me/events/{e}/pairwise"): "assignee",  # only projects the caller reviewed
    ("GET", "/api/v1/events/{e}/certificates"): O, ("POST", "/api/v1/certificates/{cid}/revoke"): O,
    ("GET", "/api/v1/search"): P,  # results are filtered by role; see test_search.py
    ("GET", "/api/v1/ask"): P,  # answers are filtered by role; see test_ask.py
}

DENY = {  # role -> refused? per category (True = must be 401/403)
    P: dict.fromkeys(ROLES, False),
    A: {"anon": True, **dict.fromkeys(ROLES[1:], False)},
    O: {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    C: {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    J: {"anon": True, "participant": True, "judge_a": False, "judge_b": False, "organizer": True, "admin": True},
    S: {"anon": True, "participant": True, "judge_a": False, "judge_b": True, "organizer": False, "admin": False},
    "team": {"anon": True, "participant": False, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    "team_member": {"anon": True, "participant": False, "judge_a": True, "judge_b": True, "organizer": True, "admin": True},
    "participant": {"anon": True, "participant": False, "judge_a": True, "judge_b": True, "organizer": True, "admin": True},
    "assignee": {"anon": True, "participant": True, "judge_a": False, "judge_b": True, "organizer": True, "admin": True},
    "published": {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    "tally": {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    "comment_org": {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
    "import_org": {"anon": True, "participant": True, "judge_a": True, "judge_b": True, "organizer": False, "admin": False},
}


def api_operations():
    from quorum.api.router import api

    ops = set()
    schema = api.get_openapi_schema()
    for path, item in schema["paths"].items():
        for method in item:
            ops.add((method.upper(), path))
    return ops


def test_every_operation_is_classified(db):
    ops = api_operations()
    missing = sorted(ops - set(MATRIX))
    stale = sorted(set(MATRIX) - ops)
    assert not missing, f"new API operations need an authorization decision in MATRIX: {missing}"
    assert not stale, f"MATRIX lists operations that no longer exist: {stale}"


@pytest.fixture
def params(db):
    from quorum.events.models import Comment, Project, Team
    from quorum.judging.models import Assignment, Review

    pid = Project.objects.get(ref="prj_01").pk  # priya's team project
    comment = Comment.objects.create(project_id=pid, author_id=Project.objects.get(pk=pid).team.members.first().user_id,
                                     body="matrix", body_html="<p>matrix</p>")
    a = Assignment.objects.filter(judge_role__ref="jdg_24", event__ref="evt_01").first()
    from quorum.events.models import EventRole, Track
    from quorum.judging.pairwise import pool

    trk = Track.objects.get(event__ref="evt_01", ref="trk_01")
    mine = pool(EventRole.objects.get(event__ref="evt_01", ref="jdg_24"), trk)
    theirs = set(pool(EventRole.objects.get(event__ref="evt_01", ref="jdg_26"), trk))
    pair = [next(p for p in mine if p not in theirs), next(p for p in mine if p in theirs)]
    return {
        "track": "trk_01", "pair": pair,
        "e": "evt_01", "j": "jdg_24", "tid": str(Team.objects.get(ref="tm_01", event__ref="evt_01").pk), "pid": str(pid),
        "aid": str(a.pk), "rid": str(Review.objects.filter(event__ref="evt_01").first().pk), "cid": str(comment.pk),
        "jid": str(uuid.uuid4()), "hid": str(uuid.uuid4()), "ref": "prj_02", "kind": "results",
    }


BODIES = {
    "/api/v1/events": {"name": "Matrix event"}, "/api/v1/events/{e}/clone": {"name": "Matrix clone", "shift_days": 30},
    "/api/v1/events/{e}/rubric": {"criteria": [{"name": "X", "weight_pct": 100}]},
    "/api/v1/events/{e}/phase": {"phase": "deliberation"}, "/api/v1/events/{e}/tracks": {"name": "T"},
    "/api/v1/events/{e}/prizes": {"name": "P"}, "/api/v1/events/{e}/questions": {"label": "Q"},
    "/api/v1/events/{e}/teams": {"name": "Matrix team"}, "/api/v1/invites/accept": {"code": "nope"},
    "/api/v1/events/{e}/projects": {"title": "late", "summary": "probe"}, "/api/v1/projects/{pid}": {"title": "x"},
    "/api/v1/events/{e}/judges": {"email": "matrix-judge@example.test"}, "/api/v1/events/{e}/judges/{j}": {"capacity": 12},
    "/api/v1/events/{e}/conflicts": {"judge": "jdg_24", "team": "tm_02"},
    "/api/v1/assignments/{aid}/review": {"scores": {"functionality": 3}},
    "/api/v1/events/{e}/assignments/commit": {"plan": {"new": [], "metrics": {"projects_at_target": 0, "projects": 0, "components": 0}}},
    "/api/v1/events/{e}/focus/commit": {"plan": {"rows": [], "run": str(uuid.uuid4()), "budget": 0, "seed": 0}},
    "/api/v1/events/{e}/tiebreaks": {"projects": ["prj_02", "prj_03"], "boundary": 1},
    "/api/v1/tiebreaks/{tid}/resolve": {"order": ["prj_02"], "note": "x"},
    "/api/v1/me/tiebreaks/{tid}/compare": {"a": "prj_02", "b": "prj_03", "outcome": "a"},
    "/api/v1/events/{e}/results/reopen": {"reason": "matrix reopen reason"},
    "/api/v1/reviews/{rid}/moderate": {"action": "approve"}, "/api/v1/events/{e}/voters/email": {"email": "x@example.test"},
    "/api/v1/events/{e}/votes": {"project": "prj_02", "ballot": "x.0.y"}, "/api/v1/events/{e}/vote-codes": {"n": 2},
    "/api/v1/events/{e}/integrity-flags/resolve": {"flags": [], "action": "keep", "note": "matrix"},
    "/api/v1/projects/{pid}/comments": {"body": "hello from the matrix"}, "/api/v1/comments/{cid}/hide": {"reason": "x"},
    "/api/v1/verify": {"payload": "{}", "signature": "x", "key_id": "x"}, "/api/v1/bundles/verify": {"format": "x"},
    "/api/v1/imports/{jid}/mapping": {"mapping": {}}, "/api/v1/events/{e}/webhooks": {"url": "https://example.org/h"},
    "/api/v1/events/{e}/nudge": {}, "/api/v1/events/{e}/rebalance/plan": {"judges": []},
    "/api/v1/events/{e}/tracks/{track}/pairwise": {"enabled": True},
    "/api/v1/certificates/{cid}/revoke": {"reason": "matrix revocation reason"},
    "/api/v1/me/events/{e}/pairwise": lambda p: {"a": p["pair"][0], "b": p["pair"][1], "outcome": "a"},
}
MULTIPART = {"/api/v1/projects/{pid}/images", "/api/v1/events/{e}/imports"}
QUERY = {"/api/v1/me/scorecard": "?event=evt_01", "/api/v1/ask": "?q=who+is+winning&event=evt_01"}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def _call(c, method, path, params):
    url = re.sub(r"\{(\w+)\}", lambda m: params[m.group(1)], path)
    if path in QUERY:
        url += QUERY[path]
    if path in MULTIPART:
        f = io.BytesIO(PNG if path.endswith("images") else b"title,email\nA,a@example.test\n")
        f.name = "x.png" if path.endswith("images") else "x.csv"
        return c.post(url, {"file": f})
    body = BODIES.get(path, {})
    body = json.dumps(body(params) if callable(body) else body) if method in ("POST", "PUT", "PATCH") else ""
    return c.generic(method, url, data=body, content_type="application/json")


@pytest.mark.parametrize("op", sorted(MATRIX), ids=lambda op: f"{op[0]} {op[1]}")
def test_authorization_matrix(op, client_as, params):
    method, path = op
    category = MATRIX[op]
    for role in ROLES:
        c = client_as(None if role == "anon" else role)
        r = _call(c, method, path, params)
        body = r.content.decode("utf-8", "replace")
        code = ""
        try:
            code = json.loads(body).get("error", "") if body.startswith("{") else ""
        except ValueError:
            pass
        assert r.status_code != 500, f"{role} {method} {path}: server error"
        if DENY[category][role]:
            assert r.status_code in (401, 403, 404), f"{role} must be refused {method} {path}, got {r.status_code} {body[:120]}"
            if role == "anon" and r.status_code != 404:
                assert r.status_code == 401 or code in STATE_CODES, f"anonymous should get 401 on {path}, got {r.status_code}"
        else:
            assert not (r.status_code in (401, 403) and code not in STATE_CODES), \
                f"{role} was refused {method} {path} for an authorization reason: {r.status_code} {body[:160]}"
