# Architecture

Quorum is one Django application, one Postgres database, one background worker, and a
pure-Python judging engine that everything else calls. It is deliberately boring: a
stranger should be able to run it, read it and change it.

```
                 ┌────────────────────────────── docker compose ────────────────────────────────┐
 browser ─HTML──►│ web  (gunicorn · Django 5.2 · templates + htmx · django-ninja /api/v1)        │
 API ──Bearer───►│   middleware: request id → CSP → sessions → CSRF → auth → Bearer tokens        │
                 │   policy/   deny-by-default @policy, Actor (per-event roles), scoped repos     │
                 │   events · judging · results · voting · audit · integrations · accounts        │
                 │   intelligence/  local embeddings (ONNX, CPU): Ask · search · coach · themes   │
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
| **Local embeddings, not a generative model** | The rules require it to run with the network off. A judging platform must not invent facts, so a 23 MB CPU model does retrieval and intent matching, and named skills compute the answers. The answers are verifiable and role-scoped. | The assistant cannot answer questions outside its 15 skills. It says so instead of guessing, and the ⌘K search still helps. |
| **Isolation in the application, invariants in the database; no row-level security** | Every read of scores goes through scoped repositories (lint-enforced) and every route declares a policy (boot-enforced), tested per operation and role, by canary crawl and by a live probe. The database enforces what must hold for *everyone* (append-only audit, immutable runs and records, deadlines, scale bounds). | Postgres RLS on reviews was specified as an optional backstop and deliberately not built: it filters rows *silently*, so any aggregate that ran under a judge's session (a preview ranking, pair selection over other judges' comparisons) would quietly compute a wrong answer instead of failing. A loud 403 in the application is the safer failure. |

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
| `quorum/intelligence/` | local embeddings (`embed`), Ask Quorum skills (`ask`), search/similar/duplicates (`semantic`), feedback coach and scorecard themes, the organizer briefing, the ⌘K palette search |
| `quorum/api/` | ninja routers (96 operations), per-audience serializers |
| `quorum/web/` | HTML views for public, participant, judge and organizer; server-rendered SVG charts (burn-down, whiskers, waterfall, certainty chart); template tags for icons and the plain-language glossary |
| `quorum/core/` | middleware (security headers, bearer tokens, request ids), rate limits, crypto, outbox, jobs, `observe` (metrics), boot and demo commands |

## Background work

`manage.py run_worker` (its own container) loops every 2 seconds:
- delivers the **outbox** (e-mail via SMTP; webhooks with `X-Quorum-Signature: t=…,v1=HMAC-SHA256`,
  SSRF guard, exponential backoff, dead-lettering after 8 attempts);
- sends due **reminders** (midpoint, 24 h left, overdue), idempotent per judge;
- **seals** submissions at the deadline (content hashes into the audit chain).

Deadline *enforcement* never depends on the worker: a stopped worker cannot let a late
submission through.

## Quorum Intelligence: how the AI is built, and what it is not allowed to do

**Rules, enforced by the design rather than by promises:**

1. **It runs inside the stack.** It uses a 23 MB int8 ONNX sentence-embedding model (all-MiniLM-L6-v2) with
   `onnxruntime` on the CPU. There is no network and no API key, so it satisfies the "network off" rule.
   - The files are SHA-256 checked at load. One intra-op thread per process keeps its CPU predictable
     next to the gunicorn threads.
   - `QUORUM_INTELLIGENCE=0` switches it off, and every feature then falls back to keywords.
2. **It never scores, ranks or decides.** The judging engine (`engine/`) has no dependency on it. The
   official ranking is byte-for-byte reproducible without the model.
3. **It cannot make things up.** There is no text generator.
   - **Ask Quorum** matches a question to one of 15 named skills by embedding similarity to example
     phrasings. Names in the question are found and masked first, so "why is Small Meadow 15th?" reads
     as "why is this project 15th?". The skill then calls the same service functions as the pages, under
     the caller's permissions.
   - Every answer carries "how I know": the skill, the data it read, and the match confidence.
   - **Scorecard themes** only quote sentences judges wrote.
4. **It can only tell you what you could already see.** Skills are role-scoped (organizer, judge,
   anyone). A judge asking "who is winning?" gets no answer.

**How it performs.**
- Embeddings are cached in `intelligence_embedding`, keyed by a hash of the model and the text, so an
  edit re-embeds only what changed.
- A boot-time `warm_intelligence` fills the cache for every listed event.
- A query costs about 5 ms, and a cold event of 40 projects about 0.5 s.

## Scale and production topology

```text
          ┌──────────────────────────── docker-compose.proxy.yml ────────────────────────────┐
 client ──► Caddy (HTTPS, zstd/gzip, least-conn LB, health checks, HSTS)                      │
          │   ├─► web replica 1  (gunicorn gthread: workers × threads, persistent DB conns)   │
          │   ├─► web replica 2                                                              │
          │   └─► web replica N  ──►  PostgreSQL 16  ◄── worker (outbox, reminders, sealing)  │
          └───────────────────────────────────────────────────────────────────────────────────┘
```

- **Stateless web.** Sessions, rate-limit counters, the job queue, the outbox and the audit-chain
  advisory lock all live in Postgres. Adding replicas adds capacity without changing any result.
  [docs/proof/load-test.md](docs/proof/load-test.md) runs 300 voters, 20 judges, readers and
  organizers against three replicas and checks that nothing was lost or double-counted.
- **Safe boot.** `manage.py boot` runs migrate, the deny-by-default check, the production guards and the
  idempotent seed under a session-level Postgres advisory lock. Replicas starting together never race
  migrations.
- **Client addresses.** They are read from the right-most untrusted `X-Forwarded-For` hop, and trusted
  proxies may be CIDR ranges. A client that forges the header still appears as itself.
- **Observability.**
  - Every request gets an `X-Request-ID`, kept from the proxy when well formed, and every JSON log line
    carries it.
  - Slow requests are logged as warnings.
  - `/metrics` (Prometheus) is served only to private networks. It reports queue depths and counts from
    Postgres, which are the same on every replica, plus per-process request counters and a latency
    histogram.
- **Static assets.** The image build writes a manifest, so CSS, JS, fonts and icons are served under
  content hashes with `Cache-Control: immutable`.

## Performance

Fixture-sized events (41 projects, 30 judges, 126 reviews): a full ranking run with
REML, 4,000-draw prize simulation, 2,000-permutation signal check, leave-one-judge-out
and weight sensitivity takes about 0.5 s and is cached by input hash. Pages render
server-side in 10–25 ms. Raptors' largest recent event (295 projects) is well within the
same design; the O(P³) Cholesky at P=300 is ~0.1 s.

Under load, on one 8-core laptop that also runs Postgres and the load generator, a single
instance serves about 335 requests per second from 122 concurrent clients, with a p50 of
about 70 ms for reads and zero errors. Per-request CPU is the limit, so adding cores or
replicas adds capacity ([docs/proof/load-test.md](docs/proof/load-test.md)).
