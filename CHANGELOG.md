# Changelog

## 1.0.0 — DOGFOOD 2026

First release. Participant, judge, organizer and public surfaces; the four pillars (judge
operations, focus rounds, tie-breaks with pre-registered methods, feedback and scorecards);
focus rounds planned against every prize decision (overall places and track prizes); optional
comparative (pairwise) judging per track, shown beside the rubric order and never fused with it; community voting with integrity review; signed records with a signed revocation list and
key rotation; 96-operation REST API; bundle import/export with recompute; offline operation
verified on an internal network.

Design system and brand (the "quorum reached" mark, vendored type and icons, light and dark,
tested accessibility basics and contrast); a redesigned landing page, judge console, gallery,
organizer console and participant flow; Quorum Intelligence, local and assistive (Ask Quorum,
smart search, duplicate suggestions, feedback coach, scorecard themes); a load test with
invariants on one instance and on the production topology (Caddy, TLS, replicas);
observability (request ids, internal metrics); immutable, content-hashed static assets.

A hosted public demo mode (`QUORUM_PUBLIC_DEMO`): hourly reset to the seed in one transaction,
no outbound mail or webhooks, a banner with the reset countdown, and a Railway configuration.
Quorum now reads `DATABASE_URL`, `PORT` and the platform's public domain, sizes its web
processes to the container's CPU and memory limits, and refuses `/metrics` to any request that
came through a reverse proxy. The local model runs with ONNX Runtime telemetry switched off.
