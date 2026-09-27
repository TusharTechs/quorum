#!/bin/sh
# Restore a backup made by scripts/backup.sh into a running stack.
#   sh scripts/restore.sh backups/2026-09-28T120000Z
set -eu
DIR=${1:?usage: restore.sh backups/<timestamp>}
( cd "$DIR" && shasum -a 256 -c SHA256SUMS )
docker compose stop web worker
docker compose exec -T db dropdb -U quorum --if-exists quorum
docker compose exec -T db createdb -U quorum quorum
docker compose exec -T db pg_restore -U quorum -d quorum --no-owner < "$DIR/quorum.pgdump"
docker compose start web worker
docker compose exec -T web sh -c "tar -C /data -xf -" < "$DIR/data.tar"
echo "restored from $DIR; verify each event's audit chain in Organize > Audit (or GET /api/v1/events/<e>/audit/verify)."
