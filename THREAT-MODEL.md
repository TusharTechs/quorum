# Threat model

Who attacks a hackathon, what they want, and what Quorum does about it. Every row says
what is **stopped**, what is **detected**, and, honestly, what is **not** stopped. The
list of what we do not prevent is as important as the list of what we do.

Assets: judges' scores and notes, unpublished rankings and tallies, submissions (drafts
especially), the integrity of the deadline, the vote, and the published results.

| # | Attack | Stopped by | Detected by | Not stopped |
|---|---|---|---|---|
| 1 | **Reading another judge's scores** (IDOR/BOLA: change an ID in a URL) | Scoped repositories are the only query path; `reviews_of_judge` allows self or organizer; uniform 403 whether or not the judge exists | Route × role matrix (88 operations × 6 roles), canary crawl, 320-request live probe | Organizers and admins see everything, by design; a database superuser |
| 2 | **Track or aggregate leakage** (search, counts, CSV, embed, results, webhooks) | Aggregates are computed from scoped data; exports and rankings are organizer-only; the embed renders as an anonymous visitor | Canary crawl over API, HTML, CSV, embed | Per-track project counts are public once the gallery is (a deliberate choice) |
| 3 | **Results leak during voting** (tally fields, popularity sort, timing) | Tallies stripped for non-organizers until close and publication; per-voter random ballot order; no public popularity sort | Extended checker, canary | A team can estimate its own count from its own outreach |
| 4 | **Participant sees scores before release** | Scorecards gated on `feedback_released_at`; judges pseudonymised as Judge A/B/C | Matrix | With very few judges a team may guess who wrote what |
| 5 | **Deadline gaming**: edit, upload, change track or team after close; spoof `submitted_at` | Server clock only; guard runs before validation; **Postgres trigger** refuses content changes after close even from a shell; `status` and `submitted_at` are server-set; content hashes sealed into the audit chain at close | `SUBMISSION_REJECTED_DEADLINE`, `DEADLINE_CHANGED` audit events | Commits pushed to a repository after the deadline (offline: we store the declared commit SHA for judges to check); an operator changing the server clock |
| 6 | **Submission scraping / idea theft** (drafts visible early) | `public_projects()` shows only submitted, canonical projects; draft images are served through an authorization check | — | Scraping the gallery once it is public; public GitHub repositories |
| 7 | **Judge conflict of interest / collusion** | A team member cannot be a judge in the same event (triggers); declared conflicts exclude the whole team from assignment; shared-domain conflicts are suggested | Leave-one-judge-out (pivotal judges), low-agreement and strong-offset signals, time-on-task | Undisclosed friendships; judges who collude consistently |
| 8 | **Method shopping** (organizer changes weights after seeing scores) | Method locked and hashed at registration; database trigger; changes only by an admin with a written reason, shown on the results page forever | `METHOD_OVERRIDDEN` audit event | A malicious administrator (visible, not preventable) |
| 9 | **Sybil votes** (many fake voters) | Identity depends on mode: one-time codes (strongest offline), signed-in accounts, verified e-mail (HMAC, not stored), open link (labelled a popularity signal) | F2 same network + browser, F3 new-identity burst, F4 disposable domains (vendored CC0 list), F5 one-domain clusters | Open-link mode cannot stop repeat voting; an attacker with many real inboxes or accounts |
| 10 | **Ballot stuffing by script** | Signed, expiring ballot token required; UNIQUE(voter, project); per-voter cap; rate limits per voter and per network (Postgres counters, correct across workers) | F1 velocity (median/MAD z-score on 5-minute buckets), F6 bot-speed votes | Slow, human-paced stuffing through many identities |
| 11 | **Vote buying / brigading** | Rules text; tallies hidden | F1 bursts | Not preventable; only detectable |
| 12 | **Voting for your own team** | Refused for signed-in and e-mail voters who are team members | Extended checker | Voters using an address we cannot link to the team |
| 13 | **CSRF** on votes, scores, submissions | Django CSRF for cookie sessions (also enforced for session calls to the API); Bearer tokens are not ambient credentials | `csrf_failed` refusals | A same-origin XSS (see 14) |
| 14 | **Stored XSS** in descriptions, comments, feedback | Markdown rendered with raw HTML disabled, then sanitised (nh3); CSP `script-src 'self'` with no inline scripts anywhere | Tests | — |
| 15 | **Malicious uploads** (SVG script, polyglots, decompression bombs) | Only PNG/JPEG/WebP/GIF by content; decoded, size- and pixel-capped, re-encoded to WebP (metadata stripped); served with `nosniff` and a sandbox CSP | Tests | Undisclosed Pillow vulnerabilities (version pinned) |
| 16 | **CSV injection** in exports | String cells starting with `= + - @ \t \r` (and full-width forms) are prefixed with `'`; numbers untouched | Tests | Exotic spreadsheet parsers |
| 17 | **Credential stuffing / brute force** | Rate limits per e-mail hash and per network; argon2 | 429s | Distributed attacks from many networks |
| 18 | **Token theft** | Tokens stored as SHA-256; expiry; revocation in the UI; demo tokens refused in production mode | `last_used_at` | A stolen, unexpired token until revoked |
| 19 | **Webhook SSRF** into the internal network | Targets resolving to private, loopback or link-local addresses are refused (demo mode excepted); signed payloads | Delivery status on the data page | DNS rebinding between check and connect |
| 20 | **Rewriting history** (edit a score, delete an audit row) | Audit table append-only (trigger); every row hash-chained; ranking runs immutable; checkpoints signed with Ed25519 at every phase change; the head hash is printed on the public results page and in exports | "Verify chain" recomputes every hash and names the first broken row (tested with a superuser forging a row) | An operator who rewrites the entire chain **before** any head hash has left the server. Signatures prove what the server's key-holder attested, not that the organizer is honest |
| 21 | **Leaked signing key / withdrawn record** | Private key only in `/data/keys` (0600), never in the database or a dump; `rotate_signing_key` retires it (public half stays for old records, private half deleted); revocation is final, public and on a signed list the browser verifier checks | Verifiers refuse a record that claims a retired key with a later timestamp; revocations and rotations are audited | A leaked key used to back-date a forged record to before its retirement: the signature alone cannot tell. The audit checkpoint inside every record is what contradicts it |

## Voting integrity, in one paragraph

Hard-block only what is unambiguous: a duplicate, over the cap, outside the window,
no valid ballot token, your own team, over a rate limit. Everything statistical is
flagged with its evidence for an organizer, who keeps or voids the flagged votes with a
written reason. Nothing is deleted; results show raw and reviewed tallies side by side.
Quadratic voting is available only where identity is strong (signed-in or one-time
codes), because with cheap identities it collapses to one-person-one-vote with extra
friction. Community prizes are always separate from judged prizes.

## Privacy

No raw IP addresses are stored. Network and browser fingerprints are keyed HMACs (the
key rotates per event) used only for abuse detection. Voters' e-mails are stored as HMACs.
The legal basis for the fingerprints is fraud prevention (legitimate interest).
