# Data model

PostgreSQL 16. Primary keys are time-ordered UUIDs (v7). Event-scoped entities also carry
a `ref` (a stable, human key such as `prj_07` or `jdg_24`) unique within the event, so
imports and exports round-trip the organizer's own identifiers. All times are UTC.

## Shape

```
user ─< event_role >─ event ─< track ─< prize            judging_method (locked spec + sha256, versioned)
         │   (organizer│judge│participant, per event)    criterion (weight in basis points, scale, anchors)
         │             ├─< team ─< team_member >─ user    UNIQUE(event, user)  → one team per person
         │             ├─< project ─< project_revision / project_image / project_answer
         │             │      └─ duplicate_of → project   eligibility_item (duplicate, late attempt, …)
         │             ├─< judge_batch ─< assignment ─1 review ─< review_score
         │             │                    └─ reassigned_from → assignment
         │             ├─< conflict (judge_role ↔ team, declared / suggested / confirmed)
         │             ├─< pairwise_comparison (optionally part of a tiebreak_round)
         │             ├─< ranking_run (immutable; input_hash + output_hash)
         │             │      ├─ focus_round   ├─ tiebreak_round   └─ result_publication (final order, signed audit head)
         │             ├─< voter ─< vote     vote_code     integrity_flag     comment
         │             ├─< reminder ─< reminder_delivery    featured_quote    certificate (serial, Ed25519)
         │             └─< import_job    webhook_endpoint
audit_event (seq, prev_hash, hash; append-only) ─< audit_checkpoint (signed head)
outbox · job · rate_bucket · signing_key (public half only) · api_token (sha256) · magic_link (sha256)
```

## Tables that matter

| Table | Key columns | Constraints / notes |
|---|---|---|
| `accounts_user` | email, name, is_superuser (= platform admin) | case-insensitive unique email; argon2 passwords |
| `accounts_apitoken` | token_hash (SHA-256), prefix, expires_at, revoked_at, is_demo | the secret is never stored |
| `accounts_magiclink` | token_hash, purpose, expires_at, used_at | single use, 15 min default |
| `events_event` | all dates, grace, phase, team size, reviews per project, batch size, feedback minimum, focus budget, voting mode | CHECK: windows close after they open |
| `events_eventrole` | event, user, role, ref, capacity, available, last_activity_at | UNIQUE(event, user, role); roles are per event |
| `events_track` | ref, name, position, pairwise | `pairwise` switches on comparative judging for the track (audited; advisory, never changes the ranking) |
| `events_judgetrack` | event_role, track | track isolation is read from here |
| `events_team` / `events_teammember` | ref, name, invite_code_hash / role | UNIQUE(event, user) on members; **team names are not unique** (the fixture has three pairs of same-named teams) |
| `events_project` | ref, team, track, content fields, status, duplicate_of, submitted_at, content_hash, version | duplicates are modelled, never deleted |
| `events_projectrevision` | version, snapshot (JSON), content_hash, author | every save; proves what judges saw |
| `judging_criterion` | key, weight_bp, scale_min/max, anchors | CHECK weight > 0, min < max; frozen once scored |
| `judging_judgingmethod` | version, spec (JSON), spec_hash, locked_at, override_reason | immutable once locked |
| `judging_assignment` | judge_role, project, batch, status, source, strategy, seed, reassigned_from | partial UNIQUE(judge_role, project) for live assignments |
| `judging_review` | status, feedback_to_team, note_to_organizers, quotable, moderation, active_seconds, source_scores | `source_scores` keeps the raw rows merged into a duplicate |
| `judging_reviewscore` | review, criterion, value (decimal) | UNIQUE(review, criterion); value within scale (trigger) |
| `judging_pairwisecomparison` | judge_role, project_a, project_b, outcome, tiebreak, reason, active_seconds | CHECK a ≠ b; UNIQUE per judge per pair per round, `NULLS NOT DISTINCT` so a comparative choice (no round) is also made once |
| `results_rankingrun` | kind, input (exact engine input), input_hash, output, output_hash, method_hash | immutable (trigger); content-addressed |
| `results_resultpublication` | run, final_order, tiebreak_results, audit_seq, audit_head, checkpoint_signature | what the public results page shows |
| `voting_voter` | kind, voter_key (HMAC of the identity), email_domain | UNIQUE(event, voter_key); raw e-mail never stored |
| `voting_vote` | voter, project, weight, ballot_position, net_key, ua_key, status, void_reason | UNIQUE(voter, project, category); voided, never deleted |
| `voting_integrityflag` | rule, subject, evidence (JSON), vote_ids, fingerprint, status, resolution | UNIQUE(event, fingerprint): scans are idempotent |
| `audit_auditevent` | seq, ts, actor, action, target, summary, data, prev_hash, hash | append-only (trigger); SHA-256 chain per event |
| `audit_certificate` | kind, serial, payload (exact signed bytes), signature, key_id | UNIQUE(event, kind, serial); never contains scores |

## Invariants enforced by Postgres triggers

`audit_append_only`, `run_immutable`, `score_in_scale`, `project_deadline`,
`member_not_judge`, `judge_not_member`, `team_size` (with a row lock, safe under
concurrency), `criterion_frozen`, `method_locked`. See
[`audit/migrations/0002_invariant_triggers.py`](src/quorum/audit/migrations/0002_invariant_triggers.py).

## Getting data in

| Path | What it does |
|---|---|
| `manage.py seed_fixtures` | Loads the official DOGFOOD fixture **exactly**: close date from the file, duplicate merged under the published policy, unfinished batches reconstructed (labelled inference), fixture SHA-256 recorded in the audit log. Idempotent. |
| Import wizard (`/o/<event>/data`, `POST /api/v1/events/<e>/imports`) | Devpost / Unstop / any CSV: columns mapped automatically from synonyms, dry-run report (rows importable, invalid lines, new tracks, possible duplicates), organizer confirms, audited commit. |
| Bundle import | A `quorum.bundle/v1` document becomes a new event; every stored ranking is recomputed on import and reported `MATCH`/`MISMATCH`. Comparative choices and the per-track switch come across; tie-break rounds stay with the source event, whose panels they belong to. |
| API | Every create operation is available at `/api/v1` (OpenAPI at `/api/v1/docs`). |

## Getting data out

| Path | What you get |
|---|---|
| CSV (`/api/v1/events/<e>/exports/<kind>.csv`) | `projects`, `teams`, `judges`, `assignments`, `reviews`, `scores` (long format: one row per review × criterion), `results`, `votes`, `feedback`, `comparisons` (tie-break and comparative choices), `audit`. String cells that start with `= + - @` are neutralised. Private notes to organizers are never exported. |
| `bundle.json` | The whole event: config, method versions and hashes, tracks, criteria, prizes, questions, teams and members, projects (with answers), judges, assignments, reviews and scores, pairwise comparisons, every ranking run's exact input and output hash, publication, audit head and signed checkpoints. `?pseudonymize=true` replaces e-mails. |
| `pg_dump` | `scripts/backup.sh`: database + uploaded files + signing key, with SHA-256 sums. |

`python -m engine recompute bundle.json` re-runs every ranking in a bundle with only
the Python standard library; `POST /api/v1/bundles/verify` does the same over HTTP.

## Personal data

Stored: names, e-mails, team membership, submissions, reviews. Not stored: raw IP
addresses (votes keep keyed HMACs of the /24 network and user agent, rotated per event),
voters' e-mails (an HMAC), API secrets, invite codes or magic-link tokens (SHA-256 only).
The audit log stores identifiers and summaries, not e-mail addresses. Erasure of a person
is deleting the user row; their audit entries keep an opaque identifier.
