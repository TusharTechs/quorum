#!/bin/sh
# Quorum container entrypoint: web | worker | <any manage.py command>
set -e
cd /app/src

case "$1" in
  web)
    python manage.py wait_for_db
    python manage.py boot    # migrate, check (deny-by-default), production guards, seed: one replica at a time
    python manage.py warm_intelligence || true   # local model; features fall back to keywords if unavailable
    # threaded workers: WEB_CONCURRENCY processes x WEB_THREADS threads, each thread with its own
    # persistent database connection (budget: replicas x workers x threads < Postgres max_connections)
    exec gunicorn quorum.wsgi:application --bind 0.0.0.0:8080 --worker-class gthread \
         --workers "${WEB_CONCURRENCY:-$(python -c "import os; print(min(os.cpu_count() or 2, 8))")}" \
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
