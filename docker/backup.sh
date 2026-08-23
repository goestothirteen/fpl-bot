#!/usr/bin/env bash
# Nightly pg_dump with 14-day retention. Add to the host crontab:
#   30 3 * * *  cd /srv/fpl-bot && ./docker/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date +%Y%m%d)
docker compose exec -T db pg_dump -U fpl fpl | gzip > "backups/fpl-${stamp}.sql.gz"
find backups -name 'fpl-*.sql.gz' -mtime +14 -delete
echo "backed up to backups/fpl-${stamp}.sql.gz"
