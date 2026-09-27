#!/bin/sh
# Quorum container entrypoint: web | worker | <any manage.py command>
set -e
cd /app/src

case "$1" in
  web)
    python manage.py wait_for_db
    python manage.py migrate --noinput -v0
    python manage.py check --fail-level ERROR     # deny-by-default: refuses to boot if a route lacks a policy
    python manage.py production_guards            # refuses demo credentials / default secret in production
    python manage.py seed_fixtures
    exec gunicorn quorum.wsgi:application --bind 0.0.0.0:8080 --workers "${WEB_CONCURRENCY:-3}" \
         --timeout 120 --access-logfile - --forwarded-allow-ips="${TRUSTED_PROXY_IPS:-127.0.0.1}"
    ;;
  worker)
    python manage.py wait_for_db --migrated
    exec python manage.py run_worker
    ;;
  *)
    exec python manage.py "$@"
    ;;
esac
