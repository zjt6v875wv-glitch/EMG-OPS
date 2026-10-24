#!/usr/bin/env bash
# Nightly backup: database dump + object storage sync. Keep at least 30 days.
#   DATABASE_URL=... BACKUP_DIR=/backups ./scripts/backup.sh
set -euo pipefail
: "${DATABASE_URL:?}" "${BACKUP_DIR:?}"
ts=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
pg_dump --format=custom --no-owner "$DATABASE_URL" > "$BACKUP_DIR/emg-$ts.dump"
if [ "${STORAGE_BACKEND:-local}" = "local" ]; then
  tar -C "${STORAGE_DIR:-./storage}" -czf "$BACKUP_DIR/storage-$ts.tgz" .
fi
find "$BACKUP_DIR" -type f -mtime +${RETENTION_DAYS:-30} -delete
echo "backup written: $BACKUP_DIR/emg-$ts.dump"
# Restore: pg_restore --clean --no-owner -d "$DATABASE_URL" emg-<ts>.dump
