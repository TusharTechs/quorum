# Load test: many people at once, nothing lost

`tools/loadtest.py` (standard library only, like `run.py`) drives four kinds of traffic at the
same time against a running stack. It then checks the invariants a judging platform must keep
under concurrency:

- **Readers:** 40 anonymous visitors on the gallery, project, event and methodology pages and the
  public API.
- **Voters:** 300 signed-in voters, each arriving from its own /24 network. Each opens a ballot,
  casts three votes, then tries one duplicate.
- **Judges:** 20 judges, each with a batch of 10, autosaving drafts and then submitting every review.
- **Organizers:** 2 organizers on the ops dashboard and the latest ranking, recomputing rankings
  as they go.

The machine is one laptop: 8 cores and 8 GB for Docker, shared by the app, Postgres *and* the load
generator. These numbers are a floor, not a ceiling.

## One instance

The default `docker compose up`: one web container running 8 processes × 4 threads, with the
load generator acting as the reverse proxy (`docker-compose.loadtest.yml`).

122 concurrent clients (40 readers, 60 voter workers for 300 voters, 20 judges, 2 organizers) against `http://localhost:8080`; 20159 requests in 60.1 s = **335 req/s**.

| Traffic | Requests | p50 ms | p95 ms | p99 ms | Status codes |
|---|---:|---:|---:|---:|---|
| read | 17651 | 71 | 533 | 966 | 200×17651 |
| ballot | 300 | 215 | 1288 | 1454 | 200×300 |
| vote | 900 | 201 | 1016 | 1140 | 201×900 |
| vote-duplicate | 300 | 177 | 1020 | 1069 | 409×300 |
| review-draft | 400 | 346 | 1079 | 1396 | 200×400 |
| review-submit | 200 | 403 | 983 | 1240 | 200×200 |
| organizer | 384 | 205 | 658 | 1017 | 200×384 |
| recompute | 24 | 416 | 2393 | 2680 | 200×24 |

| Invariant | Result | Detail |
|---|---|---|
| no 5xx responses | PASS | 0 |
| every accepted vote counted exactly once | PASS | 900 accepted, tally +900 |
| every duplicate vote refused | PASS | 300 refused |
| every submitted review recorded | PASS | 200 submitted, 200 recorded |
| audit hash chain intact | PASS | 415 entries |

## Production topology: Caddy with TLS, 3 web replicas

`docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --scale web=3`. Every
request goes over HTTPS through Caddy, which balances across three web replicas (4 × 4 each) on
the same 8 cores, so throughput per replica drops. The point of this run is the invariants.
Sessions, rate-limit counters, the job queue, the outbox and the audit-chain lock all live in
Postgres, so adding replicas adds capacity without changing results.

122 concurrent clients (40 readers, 60 voter workers for 300 voters, 20 judges, 2 organizers) against `https://localhost`; 17543 requests in 60.1 s = **292 req/s**.

| Traffic | Requests | p50 ms | p95 ms | p99 ms | Status codes |
|---|---:|---:|---:|---:|---|
| read | 15073 | 111 | 466 | 1003 | 200×15073 |
| ballot | 300 | 322 | 2414 | 3317 | 200×300 |
| vote | 900 | 137 | 1453 | 2178 | 201×900 |
| vote-duplicate | 300 | 96 | 1249 | 1622 | 409×300 |
| review-draft | 400 | 376 | 1722 | 3690 | 200×400 |
| review-submit | 200 | 449 | 1156 | 1717 | 200×200 |
| organizer | 348 | 259 | 674 | 1897 | 200×348 |
| recompute | 22 | 516 | 2043 | 2158 | 200×22 |

| Invariant | Result | Detail |
|---|---|---|
| no 5xx responses | PASS | 0 |
| every accepted vote counted exactly once | PASS | 900 accepted, tally +900 |
| every duplicate vote refused | PASS | 300 refused |
| every submitted review recorded | PASS | 200 submitted, 200 recorded |
| audit hash chain intact | PASS | 413 entries |

## What the invariants prove

- **Votes.** Every vote the API accepted (201) appears in the tally exactly once. Every duplicate
  is refused (409), because `UNIQUE(voter, project, category)` holds under concurrency and the
  voter row is locked while it votes.
- **Reviews.** Every review submission that returned 200 is recorded as submitted, with 20 judges
  autosaving and submitting at once.
- **Audit chain.** The hash chain verifies after the storm. The per-event advisory lock
  serialises appends even with three replicas writing at once.
- **Errors.** No request failed with a 5xx.

## Reproduce

```bash
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --wait
python3 tools/loadtest.py .dogfood.toml --voters 300 --judges 20 --readers 40 --duration 60

docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --wait --scale web=3
python3 tools/loadtest.py .dogfood.toml --base https://localhost --insecure \
  --setup "docker compose -f docker-compose.yml -f docker-compose.proxy.yml exec -T --index 1 web python manage.py loadtest_setup"
```

`loadtest_setup` refuses to run outside demo mode. Reset afterwards with `make reset`.
