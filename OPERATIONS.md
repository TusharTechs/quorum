# Operations

## Running a Raptors-style event on Quorum, day by day

| When | Who | In Quorum |
|---|---|---|
| T−30 days | Organizer | **Organize → New event → "Hackathon Raptors style"** (or **Run it again**, which clones last event's tracks, prizes, questions and rubric with dates shifted). Set dates, tracks, prizes, custom questions, rubric weights and anchors. |
| T−30 days | Organizer | **Setup → Publish & lock method**. Registration opens; the rubric, weights, calibration, tie-break rule and focus budget are hashed and public. |
| T−7 days | Organizer | **Judges → Invite** (tracks, capacity). Invitations are 7-day sign-in links. |
| Kickoff → freeze | Participants | Create or join a team by invite link, draft (autosaves), submit, edit until the deadline. The countdown is on every page; the server refuses anything later. |
| Freeze | System | Submissions are sealed: every project's content hash goes into the audit chain. **Participants → Eligibility** lists duplicates and problems for a human decision. |
| Freeze + 1 day | Organizer | **Judging ops → Plan: fill coverage gaps** (overlap-aware, conflict-free batches of 10–12) → dry run → **Commit and e-mail judges**. Reminders are scheduled automatically: midpoint, 24 hours left, overdue. |
| Judging window (~10 days) | Organizer | Check **Overview → Decisions that need you** once a day. Stalled or behind judges → **Nudge** or **Plan rebalance**. Flat scorers and judges with no feedback show up early, while there is time to ask them. |
| Mid-window (optional) | Organizer | **Results → Comparative judging per track → Switch on** for tracks where judges can spare ten minutes. Judges get a "Comparisons" card once they have reviewed 3 projects there: 10 choices between projects they already scored. The Bradley–Terry order appears beside the rubric order; disagreements are flagged for a look. Advisory: it never changes the ranking. |
| Window − 2 days | Organizer | **Results → Plan focus round**: spare capacity goes where a prize is still uncertain. |
| Window end | Organizer | **Results**: read the signal check. For ties at a paid position, **Open tie-break round** (3 conflict-free judges compare the contenders). |
| Deliberation | Organizer | **Feedback**: see which teams would receive no feedback; approve, edit (with a note) or hide items; pick featured quotes for winners. **Voting**: review integrity flags, keep or void with reasons. |
| Results day | Organizer | **Results → Lock** (immutable run, signed audit head) → **Publish**. Every team is e-mailed its scorecard; judges receive signed, numbered evaluation protocols; participants and winners receive certificates. |
| After | Anyone | Public results show the method hash, run hash and audit head. **Data → bundle.json** lets anyone recompute the ranking. |

## Production deployment

Quorum runs anywhere Docker Compose runs. A small VPS is plenty for a typical event: 2 vCPU and
2 GB RAM, or 4 GB with the local AI in every worker. The production overlay puts Caddy in front for
automatic HTTPS, compression and load balancing, and you choose the number of web replicas.

```bash
cp .env.example .env        # then edit it (below)
QUORUM_DOMAIN=judging.example.org docker compose --env-file .env \
  -f docker-compose.yml -f docker-compose.proxy.yml up -d --build --scale web=2
docker compose exec web python manage.py createsuperuser
```

**Scaling.**
- Web replicas are stateless: sessions, rate limits, jobs, the outbox and the audit lock all live in
  Postgres. Add replicas with `--scale web=N`.
- They boot one at a time under a Postgres lock (`manage.py boot`), so they never race each other's
  migrations.
- Each replica runs `WEB_CONCURRENCY` processes × `WEB_THREADS` threads. The default is one process
  per core (at most 8) with 4 threads each.
- Keep replicas × processes × threads below Postgres `max_connections`. The overlay sets it to 250.
- The load test in [docs/proof/load-test.md](docs/proof/load-test.md) runs 300 voters, 20 judges,
  readers and organizers on three replicas. Nothing was lost or double-counted.

**Behind another proxy or load balancer.**
- Set `TRUSTED_PROXY_IPS` to its addresses or CIDR ranges.
- Quorum reads the client from the right-most untrusted `X-Forwarded-For` hop, so a forged header
  cannot dodge rate limits.
- The Caddyfile trusts private ranges only.

**Local AI.** The embedding model ships in the image, so there is nothing to download or configure.
- Each process loads it lazily on the first search, question or coach request. Budget about 100 MB
  per process, or set `QUORUM_INTELLIGENCE=0` on very small hosts to use keyword fallbacks everywhere.
- `warm_intelligence` runs at boot. `/metrics` reports whether the model is available.

`.env` for production (all variables in [`.env.example`](.env.example)):

```
QUORUM_ENV=production
QUORUM_DEMO=0
QUORUM_PUBLIC_ORIGIN=https://judging.example.org
DJANGO_SECRET_KEY=<50+ random characters>
DJANGO_ALLOWED_HOSTS=judging.example.org
POSTGRES_PASSWORD=<random>
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_TLS=1
TRUSTED_PROXY_IPS=<your proxy's address or CIDR>   # the proxy overlay sets private ranges by default
WEB_CONCURRENCY=4                                    # processes per replica (default: cores, max 8)
WEB_THREADS=4
METRICS_ALLOWED_NETS=10.0.0.0/8,172.16.0.0/12        # who may scrape /metrics (default: private + loopback)
```

**Production guards.** With `QUORUM_ENV=production` the web container refuses to start if:
- the secret key is the demo default;
- `DEBUG` is on;
- demo mode is on;
- demo tokens exist;
- e-mail would go to the console.

The checks are in `manage.py production_guards`. In production, remove the `mail` service or leave it unused.

**Secure cookies.** HTTPS origins turn on Secure cookies and HSTS automatically.

## Backup and restore

```bash
make backup                      # backups/<UTC timestamp>/: pg_dump + uploaded files + signing key + SHA256SUMS
make restore DIR=backups/<stamp> # verifies checksums, restores database and files
```

The backup contains the Ed25519 signing key (`keys/`) and personal data. Store it
encrypted, somewhere other than the server. After a restore, run **Audit → Verify chain**
for each event (or `GET /api/v1/events/<e>/audit/verify`).

## Upgrades

```bash
git pull
docker compose build
docker compose up -d        # migrations run on start; the app refuses to boot if a route lacks a policy
```

Migrations are forward-only and run inside the web container's entrypoint. Take a
backup first. The engine version is recorded in every ranking run, so results computed
before an upgrade stay reproducible with their own engine version.

## Air-gapped installation

```bash
make wheels                 # on a connected machine: fills vendor/wheels for this architecture
docker compose build        # installs from vendor/wheels with no network access
docker save quorum:local postgres:16-alpine axllent/mailpit:latest | gzip > quorum-images.tgz
```

Copy the repository and `quorum-images.tgz` across, `docker load < quorum-images.tgz`,
then `docker compose up`. At runtime Quorum makes no outbound connections except SMTP and
the webhooks you configure.

## Monitoring

- `GET /metrics` serves Prometheus text to `METRICS_ALLOWED_NETS` only. It includes:
  - outbox messages by status and pending jobs;
  - platform counts and model availability;
  - per-process request counters and a latency histogram.
  Alert on `quorum_outbox_messages{status="dead"} > 0` and a growing `quorum_jobs_pending`.
- Every response carries `X-Request-ID`, and every JSON log line carries the same id. Requests slower
  than `SLOW_REQUEST_SECONDS` (default 1 s) are logged as warnings.
- `GET /healthz`: process up (used by the container healthcheck).
- `GET /readyz`: database reachable.
- Logs are JSON lines on stdout (`docker compose logs web worker`).
- Failed e-mail and webhook deliveries are retried with backoff and dead-lettered after
  8 attempts. Webhook status is shown on the data page.

## Tokens and keys

- **API tokens.** Created under `/settings/tokens` and stored hashed. Revoke them there.
- **Demo tokens.** `manage.py rotate_demo_tokens --revoke` removes them.
- **Signing key.** Generated on first boot into `/data/keys` (0600). Its public half is at `/.well-known/quorum-keys.json`. Losing the private key does not invalidate issued records, but no new ones can be signed with it.
- **Rotating the signing key.** Run `docker compose exec web python manage.py rotate_signing_key`, yearly or at once if `/data/keys` may have leaked. The old key is marked retired but stays published, so every record signed before still verifies. Its private half is deleted. Verifiers refuse a record that claims the retired key with a later timestamp. The rotation is audited.
- **Revoking a record.** Use **Audit → Signed records → revoke** with a public reason, for example a prize withdrawn after a late disqualification. Revocation is final: the record page shows it, and it is added to the signed list at `/.well-known/quorum-revocations.json`, which the verifier checks.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `run.py` shows `no response` | The portal is still starting; wait for the "Quorum ready" banner (`docker compose logs web`). |
| Build fails with `CERTIFICATE_VERIFY_FAILED` | A TLS-intercepting proxy on your network. Run `make wheels` on the host, then build (installs from `vendor/wheels`). |
| Judges say they got no e-mail | In demo mode, mail is at http://localhost:8025. In production, check `SMTP_*` and the worker logs. |
| "Refusing to start in production" | Read the listed reasons; each has a one-line fix above. |
