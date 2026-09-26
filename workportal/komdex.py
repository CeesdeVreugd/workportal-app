"""Nieuwe ordermappen uit Komdex.

Komdex maakt bij een nieuwe order een map aan op de server. Omdat de Docker-VM in een ander VLAN
zit, leest WorkPortal die map niet zelf uit: een PowerShell-script (scripts/komdex-orders.ps1) draait
als geplande taak op de server en stuurt elke paar minuten de lijst met ordermappen via HTTPS naar
POST /api/komdex/orders (header X-WorkPortal-Key = KOMDEX_KEY).

Mappen die nieuw zijn komen in het actievenster (/orders/inbox) en Verkoop-Inkoop-WVB en Directie
krijgen een melding. De eerste keer worden alle bestaande mappen alleen onthouden.
"""
import base64
import binascii
import hmac
import json
import os
import re
from datetime import timedelta

from flask import Blueprint, request, jsonify

from . import sharepoint as sp
from .db import get_db
from .notify import notify_user
from .util import now_iso, now_utc, parse_iso, iso

bp = Blueprint("komdex", __name__)

NUM_RE = re.compile(r"^\s*(\d{8})(?:\s*[-_–]+\s*|\s+|$)(.*?)\s*$")
MAX_FOLDERS = 50000


def configured():
    return bool(os.environ.get("KOMDEX_KEY"))


def status():
    conn = get_db()
    last = sp.setting(conn, "komdex_last_push")
    dt = parse_iso(last) if last else None
    return {"configured": configured(), "last_push": last,
            "stale": bool(dt and now_utc() - dt > timedelta(hours=1)),
            "count": sp.setting(conn, "komdex_last_count")}


NOTIFY_AFTER = timedelta(minutes=10)   # eerst de orderbon een kans geven (volgende run van het script)
BON_WINDOW = timedelta(days=30)        # zo lang na het verschijnen van de map vragen we om de orderbon


def _types(conn, key, default):
    return [t.strip().lower() for t in (sp.setting(conn, key) or default).split(",") if t.strip()]


def type_kind(conn, order_type):
    """'project' / 'order' voor een ordertype uit de orderbon, of None als het (nog) onbekend is."""
    t = (order_type or "").strip().lower()
    if not t:
        return None
    if t in _types(conn, "komdex_project_types", "project"):
        return "project"
    if t in _types(conn, "komdex_order_types", "order"):
        return "order"
    return None


def remember_type(conn, order_type, kind):
    t = (order_type or "").strip()
    if not t or kind not in ("project", "order"):
        return
    keys = {"project": ("komdex_project_types", "project"), "order": ("komdex_order_types", "order")}
    for k, (key, default) in keys.items():
        lst = [x for x in (sp.setting(conn, key) or default).split(",") if x.strip() and x.strip().lower() != t.lower()]
        if k == kind:
            lst.append(t)
        sp.set_setting(conn, key, ",".join(x.strip() for x in lst))


def bon_of(row):
    try:
        return json.loads(row["bon_json"]) if row and row["bon_json"] else None
    except (ValueError, TypeError):
        return None


def create_from_inbox(conn, it, kind, number, name, cid, user_id=None, status="verwerkt"):
    """Maakt het project/de order aan vanuit een regel in het actievenster, met de gegevens van de orderbon."""
    bon = bon_of(it) or {}
    cur = conn.execute(
        "INSERT INTO projects (number, name, customer_id, status, created_at, source, kind, order_type, executor, order_date,"
        " delivery_date, delivery_week, reference, contact_name, work_description, bon_file_id)"
        " VALUES (?,?,?,'actief',?,'komdex',?,?,?,?,?,?,?,?,?,?)",
        (number, name, cid, now_iso(), kind, bon.get("order_type"), bon.get("executor"), bon.get("order_date"),
         bon.get("delivery_date"), bon.get("delivery_week"), bon.get("reference"), bon.get("contact"), bon.get("work"),
         it["bon_file_id"]))
    pid = cur.lastrowid
    if it["bon_file_id"]:
        conn.execute("UPDATE files SET entity = 'project', entity_id = ? WHERE id = ?", (pid, it["bon_file_id"]))
    conn.execute("UPDATE order_inbox SET status = ?, project_id = ?, customer_id = ?, handled_by = ?, handled_at = ? WHERE id = ?",
                 (status, pid, cid, user_id, now_iso(), it["id"]))
    conn.commit()
    return pid


def _enrich(conn, pid, it):
    """Bestaand project/order aanvullen met de orderbon (alleen lege velden)."""
    bon = bon_of(it) or {}
    fields = {"order_type": "order_type", "executor": "executor", "order_date": "order_date", "delivery_date": "delivery_date",
              "delivery_week": "delivery_week", "reference": "reference", "contact_name": "contact", "work_description": "work"}
    for col, key in fields.items():
        if bon.get(key):
            conn.execute(f"UPDATE projects SET {col} = COALESCE(NULLIF({col}, ''), ?) WHERE id = ?", (bon[key], pid))
    if it["bon_file_id"]:
        conn.execute("UPDATE projects SET bon_file_id = COALESCE(bon_file_id, ?) WHERE id = ?", (it["bon_file_id"], pid))
        conn.execute("UPDATE files SET entity = 'project', entity_id = ? WHERE id = ?", (pid, it["bon_file_id"]))
    conn.commit()


def auto_process(conn, iid):
    """Na ontvangst van de orderbon: automatisch aanmaken als ordertype en klant bekend zijn. Geeft een melding terug."""
    it = conn.execute("SELECT * FROM order_inbox WHERE id = ?", (iid,)).fetchone()
    if it["project_id"]:
        _enrich(conn, it["project_id"], it)
        return "aangevuld"
    if it["status"] != "nieuw":
        return "overgeslagen"
    bon = bon_of(it) or {}
    kind = type_kind(conn, bon.get("order_type"))
    note = None
    if not kind:
        note = f"Ordertype '{bon.get('order_type') or '–'}' is nog niet bekend als project of order."
    elif not it["customer_id"]:
        note = f"Klant '{bon.get('customer') or it['parent'] or '–'}' niet herkend in de relaties."
    elif sp.connected(conn) and not sp.customer_folder(conn, it["customer_id"]):
        note = "Klant heeft nog geen klantmap in SharePoint."
    if note:
        conn.execute("UPDATE order_inbox SET note = ? WHERE id = ?", (note, iid))
        conn.commit()
        return "actie nodig"
    number = it["number"] or bon.get("number")
    existing = conn.execute("SELECT id FROM projects WHERE number = ?", (number,)).fetchone()
    if existing:
        conn.execute("UPDATE order_inbox SET status = 'gekoppeld', project_id = ?, handled_at = ? WHERE id = ?",
                     (existing["id"], now_iso(), iid))
        conn.commit()
        _enrich(conn, existing["id"], conn.execute("SELECT * FROM order_inbox WHERE id = ?", (iid,)).fetchone())
        return "gekoppeld"
    name = bon.get("description") or it["description"] or f"Order {number}"
    pid = create_from_inbox(conn, it, kind, number, name, it["customer_id"], None, status="automatisch")
    msg = f"automatisch aangemaakt als {kind}"
    if sp.connected(conn):
        try:
            ok, spmsg = sp.create_project_folder(conn, pid)
        except Exception as exc:  # pragma: no cover - netwerk
            ok, spmsg = False, f"SharePoint niet bereikbaar: {exc}"
        conn.execute("UPDATE order_inbox SET note = ? WHERE id = ?", (spmsg, iid))
        conn.commit()
        msg += "; " + spmsg
    return msg


def notify_pending(conn):
    """Vanuit de planner: melding voor ordermappen die na 10 minuten nog steeds actie nodig hebben."""
    limit = iso(now_utc() - NOTIFY_AFTER)
    rows = conn.execute("SELECT id, number, folder FROM order_inbox WHERE status = 'nieuw' AND missing = 0"
                        " AND notified_at IS NULL AND first_seen <= ?", (limit,)).fetchall()
    if not rows:
        return
    conn.execute(f"UPDATE order_inbox SET notified_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                 [now_iso()] + [r["id"] for r in rows])
    conn.commit()
    _notify(conn, [dict(r) for r in rows])


def _notify(conn, items):
    roles = [r.strip() for r in (sp.setting(conn, "komdex_notify_roles") or "verkoop,directie").split(",") if r.strip()]
    if not roles or not items:
        return
    users = conn.execute(
        "SELECT DISTINCT u.id FROM users u JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id"
        f" WHERE u.active = 1 AND r.key IN ({','.join('?' * len(roles))})", roles).fetchall()
    if len(items) == 1:
        it = items[0]
        title = f"Nieuwe order uit Komdex: {it['number']}"
        body = f"{it['folder']}. Zet hem goed als project of order in WorkPortal."
    else:
        title = f"{len(items)} nieuwe orders uit Komdex"
        body = ", ".join(i["number"] for i in items[:10]) + (" …" if len(items) > 10 else "") + ". Zet ze goed in WorkPortal."
    for u in users:
        try:
            notify_user(conn, u["id"], title, body, url="/orders/inbox")
        except Exception as exc:  # pragma: no cover - netwerk
            print(f"[WorkPortal] Melding nieuwe order mislukt: {exc}", flush=True)


def receive(conn, folders):
    """Verwerkt de volledige lijst met mappen van het script. Geeft een samenvatting terug."""
    now = now_iso()
    first = sp.setting(conn, "komdex_baseline") != "1"
    known = {r["path"]: r for r in conn.execute("SELECT id, path, status FROM order_inbox")}
    seen, new_items = set(), []
    for f in folders[:MAX_FOLDERS]:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip()[:1000]
        name = str(f.get("name") or re.split(r"[\\/]", path.rstrip("\\/"))[-1]).strip()[:300]
        m = NUM_RE.match(name)
        if not path or not m:
            continue
        seen.add(path)
        if path in known:
            conn.execute("UPDATE order_inbox SET last_seen = ?, missing = 0 WHERE id = ?", (now, known[path]["id"]))
            continue
        number, desc = m.group(1), (m.group(2) or "").strip()
        parent = str(f.get("parent") or "").strip()[:300] or None
        proj = conn.execute("SELECT id FROM projects WHERE number = ?", (number,)).fetchone()
        st = "bestaand" if first else ("gekoppeld" if proj else "nieuw")
        cid = sp._match_customer(conn, parent) if parent else None
        conn.execute("INSERT INTO order_inbox (source, path, folder, parent, number, description, customer_id, status, project_id,"
                     " first_seen, last_seen) VALUES ('komdex',?,?,?,?,?,?,?,?,?,?)",
                     (path, name, parent, number, desc, cid, st, proj["id"] if proj else None, now, now))
        known[path] = {"id": None, "path": path, "status": st}
        if st == "nieuw":
            new_items.append({"number": number, "folder": name})
        elif st == "bestaand":
            conn.execute("UPDATE order_inbox SET notified_at = ?, bon_received_at = ? WHERE path = ?", (now, now, path))
    # mappen die in Komdex zijn verdwenen verdwijnen ook uit het actievenster
    for path, r in known.items():
        if path not in seen and r["status"] == "nieuw":
            conn.execute("UPDATE order_inbox SET missing = 1 WHERE path = ? AND status = 'nieuw'", (path,))
    conn.commit()
    sp.set_setting(conn, "komdex_baseline", "1")
    sp.set_setting(conn, "komdex_last_push", now)
    sp.set_setting(conn, "komdex_last_count", str(len(seen)))
    # welke mappen mogen hun orderbon nog sturen?
    since = iso(now_utc() - BON_WINDOW)
    want = [r["path"] for r in conn.execute(
        "SELECT path FROM order_inbox WHERE bon_received_at IS NULL AND missing = 0 AND status NOT IN ('bestaand','genegeerd')"
        " AND first_seen >= ?", (since,))]
    return {"ontvangen": len(folders), "ordermappen": len(seen), "nieuw": len(new_items), "eerste_keer": first,
            "want": want}


def _key_ok():
    key = os.environ.get("KOMDEX_KEY", "")
    given = request.headers.get("X-WorkPortal-Key", "")
    return bool(key) and hmac.compare_digest(key.encode(), given.encode())


@bp.route("/api/komdex/orderbon", methods=["POST"])
def orderbon():
    """Het script stuurt de orderbon (PDF) uit de ordermap. WorkPortal leest hem uit en verwerkt de order zo mogelijk zelf."""
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    from .orderbon import parse
    from .util import save_bytes
    js = request.get_json(silent=True) or {}
    conn = get_db()
    it = conn.execute("SELECT * FROM order_inbox WHERE path = ?", (str(js.get("path") or ""),)).fetchone()
    if not it:
        return jsonify({"error": "Onbekende map"}), 404
    try:
        data = base64.b64decode(js.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "Ongeldige inhoud"}), 400
    fname = re.sub(r'[\\/:*?"<>|]+', "_", str(js.get("filename") or "orderbon.pdf"))[:150]
    bon = parse(data) if data[:4] == b"%PDF" else None
    now = now_iso()
    if not bon:
        conn.execute("UPDATE order_inbox SET bon_received_at = ?, note = ? WHERE id = ?",
                     (now, f"Orderbon '{fname}' kon niet worden gelezen.", it["id"]))
        conn.commit()
        return jsonify({"ok": False, "melding": "Orderbon niet leesbaar"}), 200
    fid = save_bytes("order_inbox", it["id"], data, fname, kind="orderbon", mime="application/pdf")
    cid = it["customer_id"]
    if bon.get("customer"):
        cid = sp._match_customer(conn, bon["customer"]) or cid
    conn.execute("UPDATE order_inbox SET bon_json = ?, bon_file_id = ?, bon_received_at = ?, customer_id = ?,"
                 " description = COALESCE(?, description) WHERE id = ?",
                 (json.dumps(bon, ensure_ascii=False), fid, now, cid, bon.get("description"), it["id"]))
    conn.commit()
    result = auto_process(conn, it["id"])
    return jsonify({"ok": True, "ordertype": bon.get("order_type"), "resultaat": result})


@bp.route("/api/komdex/orders", methods=["POST"])
def push():
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    folders = js.get("folders")
    if not isinstance(folders, list):
        return jsonify({"error": "Verwacht {\"folders\": [...]}"}), 400
    return jsonify(receive(get_db(), folders))
