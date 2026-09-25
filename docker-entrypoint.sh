#!/bin/sh
set -e
echo "[WorkPortal] Opstarten..."
mkdir -p "${DATA_DIR:-/data}"
# Eén worker met meerdere threads: SQLite + de achtergrondplanner draaien zo precies één keer.
exec gunicorn --bind 0.0.0.0:3000 --workers 1 --threads 8 --timeout 180 \
     --access-logfile - --error-logfile - wsgi:app
