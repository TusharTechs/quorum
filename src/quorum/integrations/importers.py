"""Migration in: bring an event over from another platform's CSV export (or a Quorum
bundle). Always a two-step flow: upload -> automatic column mapping -> dry-run report
(what would be created, what is invalid, what looks duplicated) -> commit.

Column mapping works from synonyms rather than hard-coded formats, so it copes with
Devpost-style ("Project Title", "Submitter Email", "Team Member 1 Email"...), Unstop-style
("Team Name", "Team Leader Email"...) and hand-made spreadsheets alike. The organizer can
correct any mapping before committing. Presets are best-effort: verify against a real
export before relying on one."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re

from django.db import connection, transaction
from django.utils.dateparse import parse_datetime

from quorum.accounts.services import user_for_email
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.text import clean_line, render_markdown
from quorum.events.models import EventRole, Project, Team, TeamMember, Track
from quorum.policy.errors import Invalid

from .models import ImportJob

TARGET_FIELDS = {
    "title": ["project title", "project name", "submission title", "title", "name of project", "idea title"],
    "tagline": ["tagline", "elevator pitch", "summary", "short description", "one liner"],
    "description": ["about the project", "description", "project description", "details", "idea description"],
    "repo_url": ["repository", "repo url", "github", "source code", "code link", "github link", "repo_url"],
    "video_url": ["video demo link", "video", "demo video", "youtube", "video link"],
    "live_url": ["try it out links", "live url", "website", "demo link", "project link", "submission url", "live link"],
    "track": ["track", "category", "theme", "opt-in prizes", "challenge"],
    "team": ["team name", "team", "team id"],
    "emails": ["submitter email", "team leader email", "leader email", "email", "member emails", "team member emails",
               "team member 1 email", "team member 2 email", "team member 3 email", "team member 4 email",
               "member 1 email", "member 2 email", "member 3 email", "member 4 email"],
    "tags": ["built with", "tech stack", "technologies", "tags", "tech tags"],
    "submitted_at": ["project created at", "submitted at", "submission date", "created at", "timestamp"],
}
PRESET_HINTS = {
    "devpost_csv": ["project title", "submission url", "built with"],
    "unstop_csv": ["team name", "team leader email"],
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (h or "").strip().lower()).strip()


def auto_map(headers: list[str]) -> dict:
    mapping = {}
    normed = {h: _norm(h) for h in headers}
    for target, syns in TARGET_FIELDS.items():
        cols = [h for h, n in normed.items() if n in syns or any(n == s for s in syns)]
        if not cols:
            cols = [h for h, n in normed.items() if any(s in n for s in syns[:2])]
        if cols:
            mapping[target] = cols if target == "emails" else cols[0]
    return mapping


def detect_source(headers) -> str:
    normed = {_norm(h) for h in headers}
    for src, hints in PRESET_HINTS.items():
        if all(h in normed for h in hints):
            return src
    return "generic_csv"


def create_job(actor, ev, upload, source: str) -> ImportJob:
    raw = upload.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise Invalid("Import files must be 8 MB or smaller.")
    text = raw.decode("utf-8-sig", errors="replace")
    name = getattr(upload, "name", "upload")
    if name.endswith(".json") or source == "bundle":
        source = "bundle"
    job = ImportJob.objects.create(event=ev, source=source, filename=name[:200],
                                   file_sha256=hashlib.sha256(raw).hexdigest(), content=text,
                                   created_by=getattr(actor, "user", None))
    if source == "bundle":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Invalid("That is not valid JSON.")
        from .bundle import verify_bundle

        v = verify_bundle(data)
        job.report = {"kind": "bundle", "projects": len(data.get("projects", [])), "reviews": len(data.get("reviews", [])),
                      "judges": len(data.get("judges", [])), "verify": v,
                      "summary": f"bundle with {len(data.get('projects', []))} projects; recompute "
                                 f"{'MATCH' if v['all_match'] else 'no runs' if v['all_match'] is None else 'MISMATCH'}"}
        job.save(update_fields=["report"])
        return job
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise Invalid("The file is empty.")
    headers = rows[0]
    if job.source == "generic_csv":
        job.source = detect_source(headers)
    dry_run(job, auto_map(headers))
    return job


def _rows(job):
    return list(csv.DictReader(io.StringIO(job.content)))


def _emails(row, cols) -> list[str]:
    out = []
    for c in cols if isinstance(cols, list) else [cols]:
        for e in re.split(r"[;,\s]+", row.get(c) or ""):
            e = e.strip().lower()
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) and e not in out:
                out.append(e)
    return out


def dry_run(job: ImportJob, mapping: dict) -> dict:
    """Validate every row against the mapping; nothing is written except the report."""
    ev = job.event
    if isinstance(mapping.get("emails"), str):
        mapping["emails"] = [mapping["emails"]]
    rows = _rows(job)
    track_names = {t.name.lower(): t for t in ev.tracks.all()}
    existing_titles = {p.title.strip().lower() for p in ev.projects.all()}
    seen_titles, errors, plan = set(), [], []
    for i, row in enumerate(rows, start=2):
        title = clean_line(row.get(mapping.get("title", ""), ""), 120)
        emails = _emails(row, mapping.get("emails", []))
        problems = []
        if not title:
            problems.append("no title")
        if not emails:
            problems.append("no valid member e-mail")
        tkey = title.lower()
        dup = tkey in seen_titles or tkey in existing_titles
        seen_titles.add(tkey)
        track = (row.get(mapping.get("track", ""), "") or "").strip()
        plan.append({"line": i, "title": title, "team": clean_line(row.get(mapping.get("team", ""), "") or title, 80),
                     "emails": emails, "track": track, "track_known": track.lower() in track_names or not track,
                     "duplicate": dup, "problems": problems})
        if problems:
            errors.append({"line": i, "problems": problems})
    new_tracks = sorted({p["track"] for p in plan if p["track"] and not p["track_known"]})
    job.mapping = mapping
    job.report = {
        "kind": "csv", "headers": list(rows[0].keys()) if rows else [], "rows": len(rows),
        "valid": sum(1 for p in plan if not p["problems"]), "errors": errors[:50],
        "duplicates": [p["title"] for p in plan if p["duplicate"]][:50], "new_tracks": new_tracks,
        "people": len({e for p in plan for e in p["emails"]}), "preview": plan[:15],
        "summary": f"{sum(1 for p in plan if not p['problems'])} of {len(rows)} rows importable, "
                   f"{len(new_tracks)} new track(s), {len([p for p in plan if p['duplicate']])} possible duplicate(s)",
    }
    job.status = ImportJob.Status.DRY_RUN
    job.save(update_fields=["mapping", "report", "status", "source"])
    return job.report


@transaction.atomic
def commit(actor, job: ImportJob):
    ev = job.event
    actor.require_organizer(ev)
    if job.status == ImportJob.Status.COMMITTED:
        raise Invalid("This import was already committed.")
    with connection.cursor() as cur:
        cur.execute("SET LOCAL quorum.bypass = 'on'")  # an organizer import may add submissions after the deadline
    if job.source == "bundle":
        from .bundle import import_bundle

        new_ev, report = import_bundle(actor, json.loads(job.content))
        job.report = {**job.report, "imported_event": new_ev.slug, "verify": report,
                      "summary": f"imported as /e/{new_ev.slug}; recompute {'MATCH' if report['all_match'] else 'MISMATCH' if report['all_match'] is False else 'n/a'}"}
    else:
        mapping = job.mapping
        created = 0
        for row in _rows(job):
            title = clean_line(row.get(mapping.get("title", ""), ""), 120)
            emails = _emails(row, mapping.get("emails", []))
            if not title or not emails:
                continue
            tname = (row.get(mapping.get("track", ""), "") or "").strip()
            track = None
            if tname:
                track = ev.tracks.filter(name__iexact=tname).first() or Track.objects.create(
                    event=ev, ref=f"trk_{ev.tracks.count() + 1:02d}", name=tname[:100], position=ev.tracks.count() + 1)
            team_name = clean_line(row.get(mapping.get("team", ""), "") or title, 80)
            team = Team.objects.create(event=ev, ref=f"tm_imp_{Team.objects.filter(event=ev).count() + 1:03d}", name=team_name)
            for j, e in enumerate(emails[: ev.team_size_max]):
                u = user_for_email(e)
                if TeamMember.objects.filter(event=ev, user=u).exists():
                    continue
                TeamMember.objects.create(team=team, event=ev, user=u, role="owner" if j == 0 else "member")
                EventRole.objects.get_or_create(event=ev, user=u, role="participant")
            desc = row.get(mapping.get("description", ""), "") or ""
            tags = [t for t in re.split(r"[,;]", row.get(mapping.get("tags", ""), "") or "") if t.strip()]
            sub = parse_datetime(row.get(mapping.get("submitted_at", ""), "") or "") if mapping.get("submitted_at") else None
            Project.objects.create(
                event=ev, ref=f"prj_imp_{Project.objects.filter(event=ev).count() + 1:03d}", team=team, track=track,
                title=title, tagline=clean_line(row.get(mapping.get("tagline", ""), "") or "", 200), description_md=desc,
                description_html=render_markdown(desc), repo_url=(row.get(mapping.get("repo_url", ""), "") or "")[:400],
                demo_video_url=(row.get(mapping.get("video_url", ""), "") or "")[:400],
                live_url=(row.get(mapping.get("live_url", ""), "") or "").split()[0][:400] if row.get(mapping.get("live_url", "")) else "",
                tech_tags=[t.strip().lower()[:30] for t in tags][:12], status="submitted", submitted_at=sub or now())
            created += 1
        job.report = {**job.report, "created": created, "summary": f"{created} project(s) imported"}
    job.status = ImportJob.Status.COMMITTED
    job.committed_at = now()
    job.save(update_fields=["report", "status", "committed_at"])
    audit.record("IMPORT_COMMITTED", f"Import from {job.get_source_display()} committed: {job.report.get('summary')}",
                 event=ev, actor=actor, actor_role="organizer", data={"file_sha256": job.file_sha256, "source": job.source})
    return job
