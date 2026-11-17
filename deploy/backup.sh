#!/usr/bin/env bash
# Nightly database backup on the server (systemd timer jobfinder-backup.timer). Keeps 14 days in
# /opt/jobfinder/backups. These files live on the same disk as the database: copy them elsewhere (S3, another
# machine) to survive the loss of the server.
#
# Restore into an empty database:
#   gunzip -c backups/jobfinder-YYYYMMDD-HHMMSS.sql.gz | docker compose exec -T postgres psql -q -U jobfinder -d jobfinder
set -euo pipefail
cd /opt/jobfinder
umask 077
mkdir -p backups
target="backups/jobfinder-$(date -u +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T postgres pg_dump -U jobfinder -d jobfinder --no-owner | gzip > "$target.part"
gunzip -t "$target.part"
mv "$target.part" "$target"
find backups -name 'jobfinder-*.sql.gz' -mtime +14 -delete
echo "backup written: $target ($(du -h "$target" | cut -f1))"
