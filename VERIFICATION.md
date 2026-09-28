# Verification

Every claim in this repository is checkable with one command. Numbers below are from the
committed run; rerun them yourself.

| What | Result | Rerun |
|---|---|---|
| Official DOGFOOD checker | **7/7 PASS**, `claimed T1 T2, verified T1 T2` | `python3 run.py .dogfood.toml` → [acceptance-report.txt](acceptance-report.txt) |
| Extended T3/T4 checker (same style, stdlib only) | **21/21 PASS** (T2+ 3/3 · T3 10/10 · T4 8/8) | `python3 tools/acceptance_ext.py .dogfood.toml` → [acceptance-report-extended.txt](acceptance-report-extended.txt) |
| **Network off** | Stack on an `internal` Docker network, no route to the internet (asserted), then run.py 7/7, extended 21/21, probe 0 leaks, all from inside that network | `docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d --wait && docker compose -f docker-compose.yml -f docker-compose.offline.yml run --rm offline-check` |
| **Fresh clone** | `git clone` of the committed repository into an empty directory, built as its own Compose project: run.py 7/7, extended 21/21, probe 336/0; its exported bundle recomputes to `MATCH` with the operating system's stock Python 3.9 and no packages | `git clone … && docker compose up`, then the three checkers; `cd src && python3 -m engine recompute bundle.json` |
| **Live public demo** | [quorum-production-646e.up.railway.app](https://quorum-production-646e.up.railway.app) on Railway (one container, 2 vCPU / 1 GB limit, managed Postgres): every page 200, each role signs in with one click, Ask Quorum answers in ≈0.6 s, `/metrics` is 404 from the internet. Locally, the same mode under a 1 CPU / 1 GB limit browsed continuously through a reset with no errors (slowest request 3.3 s) | open it; `OPERATIONS.md#hosted-public-demo` to run your own |
| Live isolation probe | **344** cross-role GET requests against the running stack, **0** unexpected successes | `python3 scripts/isolation_probe.py .dogfood.toml` |
| Automated tests (real Postgres, triggers included) | **186 passed** | `make test` |
| ↳ Authorization matrix | every one of the **96** API operations × 6 roles (anonymous, participant, judge A, judge B, organizer, admin) = 576 requests; the test fails if an operation is not classified | `tests/api/test_matrix.py` |
| ↳ Canary leak crawl | private draft title, judge's private note, unreleased feedback and a hidden comment never appear in any API, HTML, CSV, bundle or embed response for anonymous, participant or another judge | `tests/api/test_isolation.py` |
| ↳ Deadline | refused *because of the deadline* (`deadline_passed`), not CSRF or validation; a direct database UPDATE after close is refused by the trigger; grace window honoured; server-set fields ignored | `tests/api/test_deadline.py` |
| ↳ Audit | UPDATE/DELETE refused by trigger; a superuser who disables the trigger and forges a row is caught by chain verification at that exact row; signed checkpoints detect tampering | `tests/api/test_integrity.py` |
| ↳ Engine | ALS = direct GLS solve to 1e-10; explanations sum exactly for all 40 projects; leniency shifts change no ranking; flat judge weight 0; deterministic hashes; Bradley–Terry edge cases; assignment properties over 60 generated events (no conflicts, tracks respected, capacity respected, every slot filled or reported) | `tests/engine/` |
| ↳ Full lifecycle | fixture → stalled batches detected → rebalance (40/40 at target, 1 judge-graph component, 0 conflicts) → review → focus round → tie-break (3-judge panel, 9 comparisons) → lock → publish → pseudonymous scorecard → signed protocol verifies → audit chain intact → bundle recompute MATCH | `tests/api/test_lifecycle.py` |
| ↳ Comparative judging | off by default (409 `pairwise_disabled`); switching on is audited and e-mails the track's judges; a judge is offered only pairs of projects they reviewed, never the same pair twice (API and a `NULLS NOT DISTINCT` constraint); a project another judge reviewed or one from another track is a 403; a full round robin leaves the ranking run's output hash **unchanged**; the Bradley–Terry board reproduces a consistent round robin exactly; CSV and bundle round-trip | `tests/api/test_pairwise.py` |
| ↳ Signed records | revocation is organizer-only, needs a public reason, is final (409 on a second attempt, and the database trigger refuses clearing or rewording it), and is audited; the record page and the server check show it; the revocation list is itself a valid signed record; after `rotate_signing_key` the old record still verifies, new ones use the new key, the retired private key file is gone, and a record claiming the retired key after retirement is refused | `tests/api/test_certificates.py` |
| ↳ Focus planning | a project nearly out overall but a coin flip in its track earns a review only when track prizes are paid; with one track place, P(track prize) equals P(#1 in track) draw for draw, and with k places every track hands out exactly k per draw; the fixture's "Best in track" makes the plan target track-open projects; an event without track prizes gets an unchanged engine input | `tests/engine/test_proof.py`, `tests/api/test_focus.py` |
| ↳ Accessibility basics | 34 pages across all four roles: every form control has a programmatic label, images have alt text, buttons and links have an accessible name, ids are unique, `lang` is set, exactly one h1. A static check, not a screen-reader audit. The colour tokens meet WCAG AA in both themes: 4.5:1 for text, 3:1 for form borders | `tests/api/test_accessibility.py` |
| ↳ Ask Quorum | organizer questions run the named skill ("who hasn't started?" → stalled judges, names found and masked: "why is Small Meadow…" → its explanation); judges, participants and visitors get **no answer** to organizer questions (e.g. "who is winning?"); off-topic questions get none; with the model switched off, keyword matching still works; the ⌘K palette carries the answer | `tests/api/test_ask.py` |
| ↳ Local AI | the model is present and checksum-verified, and a tampered file is refused with every feature falling back; on the fixture, boilerplate is detected and **exactly** the real duplicate (prj_07/prj_41) is suggested; smart and exact gallery search; similar projects; scorecard themes quote judges verbatim and place sentences under the right criterion | `tests/api/test_semantic.py` |
| ↳ Feedback coach and demo sign-in | the coach is for judges only, flags harsh wording, and sees which criteria are covered; demo sign-in is POST-only, for three showcase roles, and 404 outside demo mode | `tests/api/test_ux.py` |
| ↳ Proxies and observability | a forged left-most `X-Forwarded-For` never becomes the client; CIDR trusted proxies; request ids generated or kept; `/metrics` is internal-only and ignores forwarded headers | `tests/api/test_proxy.py`, `tests/api/test_observe.py` |
| ↳ Operations | `boot` is idempotent under its lock; `loadtest_setup` refuses production | `tests/api/test_ops_commands.py` |
| **Load test** | 122 concurrent clients (readers, 300 voters from distinct networks, 20 judges, organizers): 0 5xx, every accepted vote counted exactly once, every duplicate refused, every submitted review recorded, audit chain intact. This holds on one instance (≈335 req/s) and behind Caddy with TLS and 3 web replicas | `python3 tools/loadtest.py .dogfood.toml` → [docs/proof/load-test.md](docs/proof/load-test.md) |
| Normalization proof | on the fixture's exact design with known truth: calibration beats the raw mean when judges differ (+0.018 τ at leniency sd 0.4, +0.070 at 0.8), costs nothing when they do not (+0.003), beats per-judge z-scores everywhere; disjoint panels give exactly 0 gain vs +0.036 with overlap | `python3 tools/generate_proof.py` → [docs/proof/](docs/proof/README.md) |
| Result reproducibility | every ranking in an exported bundle recomputes byte-for-byte: `MATCH` | `cd src && python3 -m engine recompute <bundle.json>` or `POST /api/v1/bundles/verify` |
| Deny-by-default | the app refuses to boot if any route lacks a `@policy` (system check `quorum.E001/E002`, run by the entrypoint) | `cd src && python manage.py check` |
| Offline assets | no external URL in any template, stylesheet or script (CI guard) | see `.github/workflows/ci.yml` |
| Lint | ruff clean | `ruff check src tests tools scripts` |

## What the fixture shows (and what that means)

`python3 -m engine fixture ../fixtures.json` (from `src/`):

- 41 submissions → 40 after merging the duplicate (prj_07 → prj_41, latest on-time copy);
  3 judges scored both copies and disagreed with themselves by 1.0–1.33 points.
- jdg_07 is a flat judge (4 on all nine cells): weight 0, visible in every explanation.
- Two unfinished batches (jdg_23, jdg_12) are reconstructed and shown as stalled; eight
  projects are below 3 reviews.
- **ICC(1) = −0.006, permutation p ≈ 0.5**: the judges agree no better than chance. Every
  adjacent position is a statistical tie. Quorum says so on the results page instead of
  printing a confident podium, and shows how focus rounds and a tie-break would settle it.

## Found and fixed by this verification

Worth recording, because they are exactly the bugs a judge would look for:

1. **422 before 401.** Anonymous requests with missing parameters got a validation error
   (leaking the shape of protected endpoints) because the API framework parsed input before
   the policy ran. Fixed by installing authentication as the framework's first check, derived
   from the same `@policy` marker.
2. **Rebalance lost work.** Identifier types differed (UUID vs string), so a stalled judge's
   pending reviews were retired but not replaced, and "fill gaps" then gave them back to the
   stalled judge. Fixed; the lifecycle test now asserts no stalled judge receives work.
3. **A duplicate label crashed** (unique constraint, 500). Refs are now generated uniquely
   and any integrity error maps to 409, never 500.
4. **Team names are not unique** in the real fixture (three pairs share a name); an early
   schema constraint would have rejected real data.
5. **A proof artefact.** A first simulation showed calibration *losing* to the raw mean; the
   cause was Kendall's τ-b rewarding the exact ties of raw integer means. The proof now breaks
   ties identically for every method, and says so.
