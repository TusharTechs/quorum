#!/bin/sh
# Quorum container entrypoint: web | worker | <any manage.py command>
set -e
cd /app/src

# default web workers: one per CPU up to 8, but never more than the container's memory holds
# (about 250 MB each with the local model loaded); cgroup limits count, not the host's CPUs
default_workers() {
  python - <<'PY'
import os
n = min(os.cpu_count() or 2, 8)
try:
    quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
    if quota != "max":
        n = min(n, max(1, round(int(quota) / int(period))))
except (OSError, ValueError):
    pass
for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
    try:
        limit = int(open(f).read())
    except (OSError, ValueError):
        continue
    if limit < 1 << 50:
        n = min(n, max(1, limit // (250 << 20)))
    break
print(max(1, n))
PY
}

case "$1" in
  web)
    python manage.py wait_for_db
    case "${QUORUM_PUBLIC_DEMO:-0}" in
      1|true|yes|on)
        # hosted public demo: a fresh secret per container, the seed restored now and again every
        # QUORUM_DEMO_RESET_MINUTES, so nothing a visitor does lasts (see OPERATIONS.md)
        case "${DJANGO_SECRET_KEY:-quorum-demo-secret-key}" in
          quorum-demo-secret-key*)
            DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"
            export DJANGO_SECRET_KEY ;;
        esac
        python manage.py demo_reset
        ( while sleep $(( ${QUORUM_DEMO_RESET_MINUTES:-60} * 60 )); do python manage.py demo_reset || true; done ) &
        ;;
      *)
        python manage.py boot    # migrate, check (deny-by-default), production guards, seed: one replica at a time
        python manage.py warm_intelligence || true   # local model; features fall back to keywords if unavailable
        ;;
    esac
    # threaded workers: WEB_CONCURRENCY processes x WEB_THREADS threads, each thread with its own
    # persistent database connection (budget: replicas x workers x threads < Postgres max_connections)
    exec gunicorn quorum.wsgi:application --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread \
         --workers "${WEB_CONCURRENCY:-$(default_workers)}" \
         --threads "${WEB_THREADS:-4}" --no-control-socket \
         --max-requests 2000 --max-requests-jitter 200 --timeout 120 --graceful-timeout 20 \
         --access-logfile - --forwarded-allow-ips="${TRUSTED_PROXY_IPS:-127.0.0.1}"
    ;;
  worker)
    python manage.py wait_for_db --migrated
    exec python manage.py run_worker
    ;;
  *)
    exec python manage.py "$@"
    ;;
esac
