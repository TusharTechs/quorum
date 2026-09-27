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
| Window − 2 days | Organizer | **Results → Plan focus round**: spare capacity goes where a prize is still uncertain. |
| Window end | Organizer | **Results**: read the signal check. For ties at a paid position, **Open tie-break round** (3 conflict-free judges compare the contenders). |
| Deliberation | Organizer | **Feedback**: see which teams would receive no feedback; approve, edit (with a note) or hide items; pick featured quotes for winners. **Voting**: review integrity flags, keep or void with reasons. |
| Results day | Organizer | **Results → Lock** (immutable run, signed audit head) → **Publish**. Every team is e-mailed its scorecard; judges receive signed, numbered evaluation protocols; participants and winners receive certificates. |
| After | Anyone | Public results show the method hash, run hash and audit head. **Data → bundle.json** lets anyone recompute the ranking. |

## Production deployment

Quorum runs anywhere Docker Compose runs (a small VPS is plenty: 2 vCPU, 2 GB RAM).
Put it behind a TLS-terminating reverse proxy (Caddy, nginx, Traefik).

```bash
cp .env.example .env        # then edit it
docker compose --env-file .env up -d --build
docker compose exec web python manage.py rotate_demo_tokens --revoke   # if you ever ran in demo mode
docker compose exec web python manage.py createsuperuser
```

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
TRUSTED_PROXY_IPS=<your proxy's address>
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

- `GET /healthz`: process up (used by the container healthcheck).
- `GET /readyz`: database reachable.
- Logs are JSON lines on stdout (`docker compose logs web worker`).
- Failed e-mail and webhook deliveries are retried with backoff and dead-lettered after
  8 attempts. Webhook status is shown on the data page.

## Tokens and keys

- **API tokens.** Created under `/settings/tokens` and stored hashed. Revoke them there.
- **Demo tokens.** `manage.py rotate_demo_tokens --revoke` removes them.
- **Signing key.** Generated on first boot into `/data/keys` (0600). Its public half is at `/.well-known/quorum-keys.json`. Losing the private key does not invalidate issued records, but no new ones can be signed with it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `run.py` shows `no response` | The portal is still starting; wait for the "Quorum ready" banner (`docker compose logs web`). |
| Build fails with `CERTIFICATE_VERIFY_FAILED` | A TLS-intercepting proxy on your network. Run `make wheels` on the host, then build (installs from `vendor/wheels`). |
| Judges say they got no e-mail | In demo mode, mail is at http://localhost:8025. In production, check `SMTP_*` and the worker logs. |
| "Refusing to start in production" | Read the listed reasons; each has a one-line fix above. |
