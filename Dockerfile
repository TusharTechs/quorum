# Quorum: one image for the web app and the worker.
#
# Stage 1 installs Python dependencies: from vendor/wheels when present (`make wheels`, for an
# air-gapped build with no access to PyPI), otherwise from PyPI. Stage 2 copies only the
# installed packages and the files the app needs at runtime, so no wheel ends up in the image.

FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
COPY vendor/ /vendor/
RUN if ls /vendor/wheels/*.whl >/dev/null 2>&1; then \
        pip install --prefix=/install --no-index --find-links=/vendor/wheels -r /tmp/requirements.txt; \
    else \
        pip install --prefix=/install -r /tmp/requirements.txt; \
    fi

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    QUORUM_STATIC_ROOT=/app/staticfiles \
    QUORUM_DATA_DIR=/data \
    ORT_DISABLE_TELEMETRY=1
COPY --from=deps /install /usr/local

WORKDIR /app
# runtime: the app, the local model and the fixture; the checkers too, so the offline proof
# (docker-compose.offline.yml) runs from inside the sealed network
COPY src/ src/
COPY docker/ docker/
COPY models/ models/
COPY tools/ tools/
COPY scripts/ scripts/
COPY run.py fixtures.json .dogfood.toml LICENSE THIRD_PARTY_NOTICES.md ./
RUN cd src && QUORUM_ENV=build DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput -v0 \
 && useradd --system --uid 10001 --home /app quorum \
 && mkdir -p /data && chown -R quorum /data \
 && chmod +x /app/docker/entrypoint.sh

# so that `docker compose exec web python manage.py <command>` works as documented
WORKDIR /app/src
USER quorum
EXPOSE 8080
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
