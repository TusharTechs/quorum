"""Observability: request ids and internal-only Prometheus metrics."""

import pytest


@pytest.mark.django_db
def test_request_id_is_generated_or_kept_when_well_formed(client):
    r = client.get("/healthz")
    assert len(r["X-Request-ID"]) == 16
    assert client.get("/healthz", HTTP_X_REQUEST_ID="edge-abc12345")["X-Request-ID"] == "edge-abc12345"
    assert client.get("/healthz", HTTP_X_REQUEST_ID="bad id <script>")["X-Request-ID"] != "bad id <script>"


@pytest.mark.django_db
def test_metrics_are_internal_only_and_ignore_forwarded_headers(client):
    ok = client.get("/metrics")  # the test client's peer is 127.0.0.1
    body = ok.content.decode()
    assert ok.status_code == 200 and "quorum_outbox_messages" in body and "quorum_http_requests_total" in body
    assert client.get("/metrics", REMOTE_ADDR="203.0.113.5").status_code == 404
    assert client.get("/metrics", REMOTE_ADDR="203.0.113.5", HTTP_X_FORWARDED_FOR="10.0.0.1").status_code == 404
