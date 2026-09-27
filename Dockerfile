# Quorum: one image for the web app and the worker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    QUORUM_STATIC_ROOT=/app/staticfiles \
    QUORUM_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN cd src && QUORUM_ENV=build DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput -v0 \
 && useradd --system --uid 10001 --home /app quorum \
 && mkdir -p /data && chown -R quorum /data \
 && chmod +x /app/docker/entrypoint.sh

USER quorum
EXPOSE 8080
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
