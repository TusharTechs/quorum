#!/bin/sh
# Back up Quorum: a consistent Postgres dump + uploaded files + signing keys.
#   sh scripts/backup.sh            -> backups/<UTC timestamp>/
set -eu
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
DIR="backups/$STAMP"
mkdir -p "$DIR"
docker compose exec -T db pg_dump -U quorum -d quorum --format=custom > "$DIR/quorum.pgdump"
docker compose exec -T web tar -C /data -cf - media keys 2>/dev/null > "$DIR/data.tar" || true
docker compose exec -T web python manage.py shell -c "from quorum.audit.service import head; from quorum.events.models import Event; [print(e.ref, *head(e)) for e in Event.objects.all()]" > "$DIR/audit-heads.txt"
( cd "$DIR" && shasum -a 256 quorum.pgdump data.tar > SHA256SUMS )
echo "backup written to $DIR"
echo "keep it somewhere else too: it contains the Ed25519 signing key (keys/) and personal data."
