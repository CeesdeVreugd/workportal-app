"""Achtergrondtaken: druktest-meldingen en nachtelijke back-up.

Gunicorn draait met één worker (meerdere threads), dus deze thread draait
precies één keer. Een bestandsvergrendeling voorkomt dubbel draaien als er
toch meerdere processen zijn.
"""
import fcntl
import glob
import os
import threading
import time
import traceback

from . import sharepoint
from .db import raw_connection, backup
from .notify import notify_user, vapid_keys
from .util import now_utc, now_iso, parse_iso, TZ

_started = False


def _check_pressure_tests(conn):
    now = now_utc()
    rows = conn.execute(
        "SELECT * FROM pressure_tests WHERE status = 'lopend' AND next_check_at IS NOT NULL"
        " AND (notified_at IS NULL OR notified_at < next_check_at)").fetchall()
    for t in rows:
        due = parse_iso(t["next_check_at"])
        if not due or due > now:
            continue
        title = f"Terug naar leiding {t['line_no'] or t['drawing_no']}"
        body = (f"Druktest {t['number']} (tekening {t['drawing_no']}): tijd voor de controle. "
                f"Lees de manometer af en maak een foto.")
        users = {t["created_by"]} if t["created_by"] else set()
        for u in users:
            try:
                notify_user(conn, u, title, body, url=f"/druktesten/{t['id']}")
            except Exception:
                traceback.print_exc()
        conn.execute("UPDATE pressure_tests SET notified_at = ? WHERE id = ?", (now_iso(), t["id"]))
        conn.commit()


def _ticket_reminders(conn):
    """Op de geplande dag (vanaf 07:00) de monteur herinneren aan meldingen en mee te nemen spullen."""
    local = now_utc().astimezone(TZ)
    if local.hour < 7:
        return
    today = local.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT t.id, t.number, t.title, t.assigned_to, c.name AS customer, c.alert AS customer_alert,"
        " i.name AS installation, i.alert AS inst_alert, i.bring AS inst_bring"
        " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id LEFT JOIN installations i ON i.id = t.installation_id"
        " WHERE t.status IN ('nieuw','ingepland') AND substr(t.planned_date, 1, 10) = ? AND t.assigned_to IS NOT NULL"
        " AND t.alert_sent_at IS NULL AND (IFNULL(i.alert,'') <> '' OR IFNULL(i.bring,'') <> '' OR IFNULL(c.alert,'') <> '')",
        (today,)).fetchall()
    for t in rows:
        parts = []
        where = " – ".join(x for x in (t["customer"], t["installation"]) if x)
        if where:
            parts.append(f"Bij {where}.")
        if t["inst_bring"]:
            parts.append("Neem mee: " + ", ".join(x for x in t["inst_bring"].splitlines() if x.strip()) + ".")
        for a in (t["inst_alert"], t["customer_alert"]):
            if a:
                parts.append("Let op: " + a.strip())
        try:
            notify_user(conn, t["assigned_to"], f"Vandaag: {t['title']}", " ".join(parts), url=f"/service/{t['id']}")
        except Exception:
            traceback.print_exc()
        conn.execute("UPDATE tickets SET alert_sent_at = ? WHERE id = ?", (now_iso(), t["id"]))
        conn.commit()


def _nightly_backup(app, state):
    local = now_utc().astimezone(TZ)
    today = local.strftime("%Y-%m-%d")
    if local.hour != 2 or state.get("last_backup") == today:
        return
    state["last_backup"] = today
    dest_dir = app.config["BACKUP_DIR"]
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"workportal-{today}.db")
    backup(app.config["DB_PATH"], dest)
    files = sorted(glob.glob(os.path.join(dest_dir, "workportal-*.db")))
    for old in files[:-14]:
        try:
            os.remove(old)
        except OSError:
            pass
    print(f"[WorkPortal] Back-up gemaakt: {dest}", flush=True)


def _loop(app):
    lock_path = os.path.join(app.config["DATA_DIR"], ".scheduler.lock")
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # ander proces draait de planner al
    state = {}
    conn = raw_connection(app.config["DB_PATH"])
    try:
        vapid_keys(conn)
    except Exception:
        traceback.print_exc()
    while True:
        try:
            _check_pressure_tests(conn)
            _ticket_reminders(conn)
            sharepoint.scheduled_sync(conn, state)
            _nightly_backup(app, state)
            conn.execute("DELETE FROM login_codes WHERE created_at < datetime('now', '-2 days')")
            conn.commit()
        except Exception:
            traceback.print_exc()
            try:
                conn.close()
            except Exception:
                pass
            conn = raw_connection(app.config["DB_PATH"])
        time.sleep(30)


def start(app):
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_loop, args=(app,), daemon=True, name="wp-scheduler")
    t.start()
