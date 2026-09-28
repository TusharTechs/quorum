"""Observability: a request id on every request and log line, slow-request warnings, and
Prometheus metrics.

Request ids: an incoming X-Request-ID (from Caddy/nginx) is kept if it is well formed,
otherwise one is generated; it is returned in the response and attached to every log record
emitted while the request runs.

/metrics serves Prometheus text to internal networks only (METRICS_ALLOWED_NETS, private
ranges and loopback by default); anyone else gets a 404. Gauges that matter across replicas
(queue depths, counts) are read from Postgres, so every replica reports the same truth;
request counters are per process and labelled with the pid.
"""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import os
import re
import threading
import time
import uuid

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound

request_id = contextvars.ContextVar("request_id", default="")
log = logging.getLogger("quorum.request")
_OK_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_lock = threading.Lock()
_started = time.time()
_counts: dict[str, int] = {}
_hist = [0] * (len(_BUCKETS) + 1)
_sum = [0.0]


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.META.get("HTTP_X_REQUEST_ID", "")
        rid = rid if _OK_ID.match(rid or "") else uuid.uuid4().hex[:16]
        request.request_id = rid
        token = request_id.set(rid)
        t = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            request_id.reset(token)
        dt = time.perf_counter() - t
        response["X-Request-ID"] = rid
        cls = f"{response.status_code // 100}xx"
        with _lock:
            _counts[cls] = _counts.get(cls, 0) + 1
            i = next((k for k, b in enumerate(_BUCKETS) if dt <= b), len(_BUCKETS))
            _hist[i] += 1
            _sum[0] += dt
        if dt > getattr(settings, "SLOW_REQUEST_SECONDS", 1.0):
            log.warning("slow request", extra={"path": request.path, "status": response.status_code,
                                               "ms": round(dt * 1000), "request_id": rid})
        return response


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not getattr(record, "request_id", ""):
            record.request_id = request_id.get()
        return True


def _allowed(ip: str) -> bool:
    nets = getattr(settings, "METRICS_ALLOWED_NETS", ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
                                                       "192.168.0.0/16"])
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in ipaddress.ip_network(n, strict=False) for n in nets)


def metrics(request):
    """Prometheus text format. Internal networks only (the direct peer, never X-Forwarded-For)."""
    if not _allowed(request.META.get("REMOTE_ADDR", "")):
        return HttpResponseNotFound()
    from quorum.core.models import Job, Outbox
    from quorum.events.models import Event, Project
    from quorum.intelligence import embed
    from quorum.judging.ops import submitted_review_count
    from quorum.voting.services import vote_count

    pid = os.getpid()
    lines = [
        "# HELP quorum_build_info Build and engine versions.", "# TYPE quorum_build_info gauge",
        f'quorum_build_info{{build="{getattr(settings, "QUORUM_BUILD", "local")}",engine="quorum-engine/1.0",'
        f'model="{embed.MODEL_NAME}"}} 1',
        "# TYPE quorum_intelligence_available gauge", f"quorum_intelligence_available {int(embed.available())}",
        "# HELP quorum_outbox_messages Outbox messages (e-mail, webhooks) by status.", "# TYPE quorum_outbox_messages gauge",
    ]
    for st in ("pending", "sent", "dead"):
        lines.append(f'quorum_outbox_messages{{status="{st}"}} {Outbox.objects.filter(status=st).count()}')
    lines += ["# TYPE quorum_jobs_pending gauge", f"quorum_jobs_pending {Job.objects.filter(status='pending').count()}",
              "# TYPE quorum_events gauge", f"quorum_events {Event.objects.count()}",
              "# TYPE quorum_projects_submitted gauge",
              f"quorum_projects_submitted {Project.objects.filter(status='submitted').count()}",
              "# TYPE quorum_reviews_submitted gauge",
              f"quorum_reviews_submitted {submitted_review_count()}",
              "# TYPE quorum_votes gauge", f"quorum_votes {vote_count()}",
              "# HELP quorum_process_uptime_seconds Seconds since this worker process started.",
              "# TYPE quorum_process_uptime_seconds gauge", f'quorum_process_uptime_seconds{{pid="{pid}"}} {time.time() - _started:.0f}',
              "# HELP quorum_http_requests_total Requests served by this worker process.",
              "# TYPE quorum_http_requests_total counter"]
    with _lock:
        counts, hist, total = dict(_counts), list(_hist), _sum[0]
    lines += [f'quorum_http_requests_total{{pid="{pid}",class="{c}"}} {n}' for c, n in sorted(counts.items())]
    lines += ["# HELP quorum_http_request_seconds Request latency in this worker process.",
              "# TYPE quorum_http_request_seconds histogram"]
    acc = 0
    for b, n in zip(_BUCKETS, hist):
        acc += n
        lines.append(f'quorum_http_request_seconds_bucket{{pid="{pid}",le="{b}"}} {acc}')
    acc += hist[-1]
    lines += [f'quorum_http_request_seconds_bucket{{pid="{pid}",le="+Inf"}} {acc}',
              f'quorum_http_request_seconds_sum{{pid="{pid}"}} {total:.3f}',
              f'quorum_http_request_seconds_count{{pid="{pid}"}} {acc}']
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")
