#!/bin/sh
set -e
echo "[WorkPortal] Opstarten..."
mkdir -p "${DATA_DIR:-/data}"
# Niet als root draaien: de app draait als gebruiker 'wp' (uid 10001). /data wordt eenmalig aan die gebruiker gegeven.
DATA="${DATA_DIR:-/data}"
RUN_AS=""
if [ "$(id -u)" = "0" ] && id wp >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1; then
  if [ "$(stat -c %u "$DATA")" != "10001" ] || [ -n "$(find "$DATA" ! -user 10001 -print -quit 2>/dev/null)" ]; then
    echo "[WorkPortal] Rechten van $DATA overzetten naar gebruiker wp..."
    chown -R 10001:10001 "$DATA"
  fi
  RUN_AS="setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs"
  export HOME=/tmp
else
  echo "[WorkPortal] Let op: draait als $(id -un) (setpriv of gebruiker wp niet gevonden)."
fi
# Eén worker met meerdere threads: SQLite + de achtergrondplanner draaien zo precies één keer.
exec $RUN_AS gunicorn --bind 0.0.0.0:3000 --workers 1 --threads 8 --timeout 180 \
     --access-logfile - --error-logfile - wsgi:app
