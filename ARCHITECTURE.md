# Architecture

Quorum is one Django application, one Postgres database, one background worker, and a
pure-Python judging engine that everything else calls. It is deliberately boring: a
stranger should be able to run it, read it and change it.

```
                 ┌────────────────────────────── docker compose ────────────────────────────────┐
 browser ─HTML──►│ web  (gunicorn · Django 5.2 · templates + htmx · django-ninja /api/v1)        │
 API ──Bearer───►│   middleware: security headers/CSP → sessions → CSRF → auth → Bearer tokens   │
                 │   policy/   deny-by-default @policy, Actor (per-event roles), scoped repos     │
                 │   events · judging · results · voting · audit · integrations · accounts        │
                 │   engine/  pure Python: calibrate · uncertainty · allocate · pairwise · assign │
                 │        │                                                                     │
                 │        ▼                                                                     │
                 │ db   PostgreSQL 16: tables · constraints · 10 invariant triggers · job/outbox  │
                 │        ▲                                                                     │
                 │ worker  outbox → e-mail / signed webhooks · reminders · deadline sealing     │
                 │ mail    Mailpit: SMTP sink + web UI (offline demo; real SMTP in production)  │
                 └──────────────────────────────────────────────────────────────────────────────┘
```

## Why this shape

| Decision | Why | Trade-off accepted |
|---|---|---|
| **Django + server-rendered templates + htmx** | Auth, sessions, CSRF, migrations, forms and an admin in one mature package. The gallery the checker reads is real HTML. One language to maintain; no Node toolchain. | Less "app-like" than an SPA; we use htmx and a small vanilla-JS file where interactivity matters (autosave, keyboard judging). |
| **django-ninja for `/api/v1`** | Typed schemas generate OpenAPI 3; docs are served from bundled static files (offline). | A second request path next to the HTML views. Both call the same services, so the rules live once. |
| **PostgreSQL 16** | Triggers for invariants, advisory locks for the audit chain, `SKIP LOCKED` job queue, full-text search, `ON CONFLICT` rate counters. | Heavier than SQLite; the backup story is `pg_dump` (scripted). |
| **No Redis, no Celery, no Kubernetes** | Postgres already does queues, locks and counters at hackathon scale (hundreds of projects, thousands of votes). Four containers total. | A very large public vote would want a dedicated rate limiter; documented. |
| **Pure-Python engine** | The maths must be recomputable by anyone with Python and nothing else; it is unit- and property-tested in isolation; the CLI verifies exported results. | Slower than numpy; a full run with robustness takes ~0.5 s for the fixture. |
| **Transactional outbox** | E-mails and webhooks are queued in the same transaction as the change that causes them; a crash cannot send a mail for a change that was rolled back, or lose one that committed. | Delivery is asynchronous (seconds). |

## A request, end to end

`POST /api/v1/events/evt_01/projects` with `Authorization: Bearer …` (the checker's late submission):

1. **SecurityHeadersMiddleware** attaches CSP (`script-src 'self'`, `frame-ancestors 'none'` except `/embed/`).
2. **BearerTokenMiddleware** hashes the token, finds the `ApiToken`, sets `request.user` (replacing any cookie session), marks the request exempt from CSRF (browsers never send this header on their own). An invalid token becomes an anonymous request flagged `invalid_token`.
3. **ninja auth guard** (`QuorumAuth`) runs *before* parameter parsing: anonymous → 401, never a 422 that would leak an endpoint's shape. It is installed automatically on every operation whose `@policy` is not `public`.
4. **`@policy("authenticated")`** builds the `Actor` (per-event roles are looked up lazily) and enforces CSRF for cookie sessions.
5. **Service** `events.services.create_project`: role check → **deadline guard** (server clock) → only then input validation. The late submission is refused with `403 {"error": "deadline_passed", "closed_at": "2026-03-01T18:00:00Z"}`.
6. Had it been on time: the project, its revision (content hash) and an **audit event** are written in one transaction; the audit writer takes a per-event advisory lock and links the new row's SHA-256 to the previous one.
7. Had a bug skipped step 5, the **`project_deadline` trigger** in Postgres would still refuse the INSERT.

## Where each rule is enforced

| Rule | Application | Database | Test |
|---|---|---|---|
| A judge never reads another judge's scores | `policy.repos.reviews_of_judge` (uniform 403) | — | matrix, canary crawl, isolation probe |
| A judge sees only their tracks / assignments | `judge_visible_projects`, `judge_assignment` | — | matrix, canary |
| No submission changes after the deadline | `guard_submissions_open` before validation | `project_deadline` trigger | `test_deadline.py` |
| One team per person per event | `create_team`/`join_team` | `UNIQUE(event, user)` | `test_one_team_per_event` |
| Team size cap | service | `team_size` trigger with row lock | — |
| A judge is not on a team in the same event | service | `member_not_judge`, `judge_not_member` triggers | — |
| Scores inside the criterion scale | `_parse_scores` | `score_in_scale` trigger | — |
| Method frozen after pre-registration | `organize._guard_rubric_editable` | `method_locked`, `criterion_frozen` triggers | lifecycle |
| Audit log cannot be edited | — | `audit_append_only` trigger + hash chain | `test_audit_log_is_append_only`, tamper test |
| Ranking runs are immutable | — | `run_immutable` trigger | — |
| Every route declares a policy | `@policy` decorator | — | system check `quorum.E001/E002` (boot fails) |
| Protected models only via scoped repos | `policy.repos` | — | AST lint test |
| Tallies hidden during voting | `tallies_visible_to` | — | extended checker, canary |
| Duplicate votes | service | `UNIQUE(voter, project, category)` | integrity tests |

A deliberate escape hatch (`SET LOCAL quorum.bypass = 'on'`) exists for the fixture
importer and the audited admin override; request handlers never set it.

## Modules

| Package | Responsibility |
|---|---|
| `engine/` | calibration, uncertainty, focus allocation, Bradley–Terry and tie-break fusion, assignment, canonical JSON + hashing, CLI |
| `quorum/policy/` | `Actor` (per-event roles), `@policy` (deny by default), `repos` (scoped queries), typed refusals |
| `quorum/events/` | events, roles, tracks, prizes, questions, teams, submissions, revisions, eligibility, comments; organizer operations |
| `quorum/judging/` | rubric, method pre-registration, conflicts, batches, assignments, reviews, pairwise, reminders; `ops` (Pillar 1), `feedback` (Pillar 4) and `pairwise` (comparative judging per track) |
| `quorum/results/` | immutable ranking runs, focus rounds, tie-breaks, lock/publish (Pillars 2–3) |
| `quorum/voting/` | voter identities, ballots, votes, integrity flags |
| `quorum/audit/` | hash-chained audit writer and verifier, signed checkpoints, certificates and judge protocols |
| `quorum/integrations/` | CSV exports, bundle export/import/verify, CSV import wizard, outbox worker (e-mail, webhooks) |
| `quorum/api/` | ninja routers (94 operations), per-audience serializers |
| `quorum/web/` | HTML views for public, participant, judge and organizer; server-rendered SVG charts |

## Background work

`manage.py run_worker` (its own container) loops every 2 seconds:
- delivers the **outbox** (e-mail via SMTP; webhooks with `X-Quorum-Signature: t=…,v1=HMAC-SHA256`,
  SSRF guard, exponential backoff, dead-lettering after 8 attempts);
- sends due **reminders** (midpoint, 24 h left, overdue), idempotent per judge;
- **seals** submissions at the deadline (content hashes into the audit chain).

Deadline *enforcement* never depends on the worker: a stopped worker cannot let a late
submission through.

## Performance

Fixture-sized events (41 projects, 30 judges, 126 reviews): a full ranking run with
REML, 4,000-draw prize simulation, 2,000-permutation signal check, leave-one-judge-out
and weight sensitivity takes about 0.5 s and is cached by input hash. Pages render
server-side in tens of milliseconds. Raptors' largest recent event (295 projects) is
well within the same design; the O(P³) Cholesky at P=300 is ~0.1 s.
