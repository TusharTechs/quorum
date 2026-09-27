from typing import Any

from ninja import Body, File, Form, Router, Schema
from ninja.files import UploadedFile

from quorum.audit import service as audit
from quorum.integrations import importers
from quorum.integrations.bundle import export_bundle, verify_bundle
from quorum.integrations.models import ImportJob, WebhookEndpoint
from quorum.policy.decorators import policy
from quorum.policy.errors import NotFound

from .common import get_event

router = Router(tags=["import & export"])


class MappingIn(Schema):
    mapping: dict[str, Any]


class HookIn(Schema):
    url: str
    topics: list[str] = []


def _org(request, e):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return ev


@router.get("/events/{e}/exports/bundle.json", summary="The whole event as a recomputable quorum.bundle/v1")
@policy("authenticated")
def bundle(request, e: str, pseudonymize: bool = False):
    ev = _org(request, e)
    audit.record("EXPORT_DOWNLOADED", "Event bundle exported via API", event=ev, actor=request.actor,
                 actor_role="organizer", data={"kind": "bundle", "pseudonymized": pseudonymize})
    return export_bundle(ev, pseudonymize)


@router.post("/bundles/verify", summary="Recompute every ranking run inside a bundle (MATCH / MISMATCH)")
@policy("public")
def verify(request, payload: dict = Body(...)):
    return verify_bundle(payload)


@router.post("/events/{e}/imports", response={201: dict}, summary="Upload a CSV/bundle; returns an automatic dry run")
@policy("authenticated")
def upload(request, e: str, file: File[UploadedFile], source: Form[str] = "generic_csv"):
    job = importers.create_job(request.actor, _org(request, e), file, source)
    return 201, {"id": str(job.pk), "source": job.source, "mapping": job.mapping, "report": job.report}


@router.post("/imports/{jid}/mapping", summary="Re-run the dry run with a corrected column mapping")
@policy("authenticated")
def remap(request, jid: str, payload: MappingIn):
    job = ImportJob.objects.select_related("event").filter(pk=jid).first()
    if not job:
        raise NotFound("No such import.")
    request.actor.require_organizer(job.event)
    return importers.dry_run(job, payload.mapping)


@router.post("/imports/{jid}/commit", summary="Commit an import (audited)")
@policy("authenticated")
def commit(request, jid: str):
    job = ImportJob.objects.select_related("event").filter(pk=jid).first()
    if not job:
        raise NotFound("No such import.")
    return importers.commit(request.actor, job).report


@router.get("/events/{e}/webhooks", summary="Webhook endpoints")
@policy("authenticated")
def hooks(request, e: str):
    ev = _org(request, e)
    return [{"id": str(h.pk), "url": h.url, "topics": h.topics, "last_status": h.last_status}
            for h in WebhookEndpoint.objects.filter(event=ev, active=True)]


@router.post("/events/{e}/webhooks", response={201: dict}, summary="Add a webhook; the signing secret is returned once")
@policy("authenticated")
def add_hook(request, e: str, payload: HookIn):
    import secrets

    ev = _org(request, e)
    h = WebhookEndpoint.objects.create(event=ev, url=payload.url, secret=secrets.token_hex(24), topics=payload.topics,
                                       created_by=request.user)
    audit.record("WEBHOOK_ADDED", f"Webhook added: {payload.url}", event=ev, actor=request.actor, actor_role="organizer")
    return 201, {"id": str(h.pk), "secret": h.secret}


@router.delete("/events/{e}/webhooks/{hid}", summary="Remove a webhook")
@policy("authenticated")
def del_hook(request, e: str, hid: str):
    ev = _org(request, e)
    WebhookEndpoint.objects.filter(event=ev, pk=hid).update(active=False)
    return {"ok": True}
