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

from flask import current_app, Blueprint, request, jsonify

from . import sharepoint as sp
from .db import get_db
from .notify import notify_user
from .util import now_iso, now_utc, parse_iso, iso

bp = Blueprint("komdex", __name__)

NUM_RE = re.compile(r"^\s*(\d{8})(?:\s*[-_–]+\s*|\s+|$)(.*?)\s*$")
MAX_FOLDERS = 50000


# Excel met na-calculatie in de ordermap: naam begint met "Nacalculatie" (ook de schrijfwijze "Nacacalculatie")
NACALC_RE = re.compile(r"^\s*na\s*-?\s*(ca)?calculatie.*\.xls[xm]?$", re.I)
NACALC_PER_RUN = 20   # max. aantal na-calculaties per ronde van het script (eerste keer niet alles tegelijk)


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


# Ordertypes uit Komdex. kind: 'project' (direct map + Power Automate), 'order' (map pas op verzoek)
# of 'vragen' (altijd in het actievenster). Aan te passen onder Orders -> Nieuwe orders.
DEFAULT_TYPES = [
    (0, "Leeg", "Leeg", "vragen"), (10, "Offerte", "Off", "vragen"), (20, "Particulier", "P", "order"),
    (30, "Handel", "H", "order"), (40, "Service / onderhoud", "SO", "order"), (50, "Engineering", "E", "project"),
    (60, "Speciaal Machinebouw", "SM", "project"), (70, "Constructie/Plaatwerk", "CP", "vragen"),
    (80, "Leidingwerk", "L", "vragen"), (90, "WBSO", "WBSO", "vragen"), (100, "Garantie", "G", "order"),
    (110, "Intern", "I", "order"), (120, "Standaard Machine - PalletRotator", "STM", "project"),
]
KINDS = ("project", "order", "vragen")


def order_types(conn):
    try:
        data = json.loads(sp.setting(conn, "komdex_types") or "null")
    except ValueError:
        data = None
    if not data:
        data = [{"id": i, "name": n, "abbr": a, "kind": k} for i, n, a, k in DEFAULT_TYPES]
    return data


def save_order_types(conn, data):
    sp.set_setting(conn, "komdex_types", json.dumps(data, ensure_ascii=False))


def _norm_type(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def type_kind(conn, order_type):
    """'project' / 'order' voor een ordertype uit de orderbon, of None als het (nog) niet automatisch mag."""
    t = _norm_type(order_type)
    if not t:
        return None
    for ot in order_types(conn):
        if t in (_norm_type(ot["name"]), _norm_type(ot.get("abbr"))):
            return ot["kind"] if ot["kind"] in ("project", "order") else None
    return None


def remember_type(conn, order_type, kind):
    t = (order_type or "").strip()
    if not t or kind not in KINDS:
        return
    data = order_types(conn)
    for ot in data:
        if _norm_type(t) in (_norm_type(ot["name"]), _norm_type(ot.get("abbr"))):
            ot["kind"] = kind
            break
    else:
        data.append({"id": None, "name": t, "abbr": "", "kind": kind})
    save_order_types(conn, data)


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
    _set_props(conn, pid, bon)
    if it["bon_file_id"]:
        conn.execute("UPDATE files SET entity = 'project', entity_id = ? WHERE id = ?", (pid, it["bon_file_id"]))
    conn.execute("UPDATE order_inbox SET status = ?, project_id = ?, customer_id = ?, handled_by = ?, handled_at = ?, note = NULL WHERE id = ?",
                 (status, pid, cid, user_id, now_iso(), it["id"]))
    conn.commit()
    return pid


def _set_props(conn, pid, bon):
    """Vinkjes van de orderbon altijd bijwerken (die veranderen: geleverd, afgefactureerd, ...)."""
    if isinstance(bon.get("props"), dict) and bon["props"]:
        conn.execute("UPDATE projects SET order_props = ? WHERE id = ?", (json.dumps(bon["props"]), pid))


def order_props(row):
    try:
        return json.loads(row["order_props"] or "{}") if row and row["order_props"] else {}
    except (ValueError, KeyError, IndexError):
        return {}


def _enrich(conn, pid, it):
    """Bestaand project/order aanvullen met de orderbon (alleen lege velden)."""
    bon = bon_of(it) or {}
    if it["customer_id"]:
        conn.execute("UPDATE projects SET customer_id = COALESCE(customer_id, ?) WHERE id = ?", (it["customer_id"], pid))
    fields = {"order_type": "order_type", "executor": "executor", "order_date": "order_date", "delivery_date": "delivery_date",
              "delivery_week": "delivery_week", "reference": "reference", "contact_name": "contact", "work_description": "work"}
    for col, key in fields.items():
        if bon.get(key):
            conn.execute(f"UPDATE projects SET {col} = COALESCE(NULLIF({col}, ''), ?) WHERE id = ?", (bon[key], pid))
    _set_props(conn, pid, bon)
    if it["bon_file_id"]:  # altijd de nieuwste orderbon
        conn.execute("UPDATE projects SET bon_file_id = ? WHERE id = ?", (it["bon_file_id"], pid))
        conn.execute("UPDATE files SET entity = 'project', entity_id = ? WHERE id = ?", (pid, it["bon_file_id"]))
    conn.commit()


def auto_process(conn, iid):
    """Na ontvangst van de orderbon: automatisch aanmaken als ordertype en klant bekend zijn. Geeft een melding terug."""
    it = conn.execute("SELECT * FROM order_inbox WHERE id = ?", (iid,)).fetchone()
    if it["project_id"]:
        _enrich(conn, it["project_id"], it)
        return "aangevuld"
    if it["status"] == "bestaand":
        return _from_existing(conn, it)
    if it["status"] != "nieuw":
        return "overgeslagen"
    bon = bon_of(it) or {}
    kind = type_kind(conn, bon.get("order_type"))
    note = None
    if not kind:
        note = f"Ordertype '{bon.get('order_type') or '–'}' is nog niet bekend als project of order."
    elif not it["customer_id"]:
        note = f"Klant '{bon.get('customer') or it['parent'] or '–'}' niet herkend in de relaties."
    elif kind == "project" and sp.connected(conn) and not sp.customer_folder(conn, it["customer_id"]):
        note = "Klant heeft nog geen klantmap in SharePoint (nodig voor de projectmap)."
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
    # alleen een project krijgt direct een map; een order pas als iemand op 'Ordermap aanmaken' klikt
    if kind == "project" and sp.connected(conn):
        try:
            ok, spmsg = sp.create_project_folder(conn, pid)
        except Exception as exc:  # pragma: no cover - netwerk
            ok, spmsg = False, f"SharePoint niet bereikbaar: {exc}"
        conn.execute("UPDATE order_inbox SET note = ? WHERE id = ?", (spmsg, iid))
        conn.commit()
        msg += "; " + spmsg
    return msg


def _from_existing(conn, it):
    """Bestaande Komdex-map met orderbon: altijd in WorkPortal zetten, maar zonder mappen in SharePoint,
    zonder melding en zonder actievenster. Onbekend ordertype wordt een order (later om te zetten)."""
    bon = bon_of(it) or {}
    number = it["number"] or bon.get("number")
    existing = conn.execute("SELECT id FROM projects WHERE number = ?", (number,)).fetchone()
    if existing:
        conn.execute("UPDATE order_inbox SET project_id = ? WHERE id = ?", (existing["id"], it["id"]))
        conn.commit()
        _enrich(conn, existing["id"], conn.execute("SELECT * FROM order_inbox WHERE id = ?", (it["id"],)).fetchone())
        return "aangevuld"
    kind = type_kind(conn, bon.get("order_type")) or "order"
    name = bon.get("description") or it["description"] or f"Order {number}"
    create_from_inbox(conn, it, kind, number, name, it["customer_id"], None, status="bestaand")
    conn.execute("UPDATE order_inbox SET note = ? WHERE id = ?",
                 ("Bestaande map: aangemaakt als " + kind + " zonder map in SharePoint.", it["id"]))
    conn.commit()
    return f"bestaande map aangemaakt als {kind} (zonder SharePoint-map)"


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
    seen, new_items, nacalc_seen, bon_seen = set(), [], {}, {}
    for f in folders[:MAX_FOLDERS]:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip()[:1000]
        name = str(f.get("name") or re.split(r"[\\/]", path.rstrip("\\/"))[-1]).strip()[:300]
        m = NUM_RE.match(name)
        if not path or not m:
            continue
        seen.add(path)
        bi = f.get("bon")
        if isinstance(bi, dict) and bi.get("name"):
            bon_seen[path] = f"{bi.get('name')}|{bi.get('modified') or ''}|{bi.get('size') or ''}"[:400]
        nc = f.get("nacalc")
        if isinstance(nc, dict) and nc.get("name") and NACALC_RE.match(str(nc.get("name"))):
            nacalc_seen[path] = f"{nc.get('name')}|{nc.get('modified') or ''}|{nc.get('size') or ''}"[:400]
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
            # bestaande map: geen melding, niets aanmaken (alleen koppelen en later aanvullen met de orderbon)
            conn.execute("UPDATE order_inbox SET notified_at = ? WHERE path = ?", (now, path))
    # mappen die in Komdex zijn verdwenen verdwijnen ook uit het actievenster
    for path, r in known.items():
        if path not in seen and r["status"] == "nieuw":
            conn.execute("UPDATE order_inbox SET missing = 1 WHERE path = ? AND status = 'nieuw'", (path,))
    # bestaande mappen koppelen aan projecten/orders met hetzelfde nummer (ook als die later in WorkPortal komen)
    conn.execute("UPDATE order_inbox SET project_id = (SELECT p.id FROM projects p WHERE p.number = order_inbox.number"
                 " ORDER BY p.id LIMIT 1) WHERE project_id IS NULL AND status IN ('bestaand', 'gekoppeld', 'nieuw')"
                 " AND EXISTS (SELECT 1 FROM projects p WHERE p.number = order_inbox.number)")
    conn.execute("UPDATE order_inbox SET status = 'gekoppeld', handled_at = COALESCE(handled_at, ?) WHERE status = 'nieuw'"
                 " AND project_id IS NOT NULL", (now,))
    conn.commit()
    sp.set_setting(conn, "komdex_baseline", "1")
    sp.set_setting(conn, "komdex_last_push", now)
    sp.set_setting(conn, "komdex_last_count", str(len(seen)))
    if seen:
        sample = next(iter(seen))
        sp.set_setting(conn, "komdex_order_dir", re.split(r"[\\/]\d{4}[\\/]\d{8}|[\\/]\d{8}", sample)[0])
    # welke mappen mogen hun orderbon nog sturen?
    since = iso(now_utc() - BON_WINDOW)
    # nieuwe mappen (30 dagen) en bestaande mappen die aan een project/order hangen (alleen aanvullen, nooit aanmaken)
    # nieuwe mappen (30 dagen), alle bestaande mappen (een orderbon zet ze alsnog in WorkPortal, zonder SharePoint-map)
    # en mappen die aan een project/order hangen (aanvullen)
    want = [r["path"] for r in conn.execute(
        "SELECT path FROM order_inbox WHERE bon_received_at IS NULL AND missing = 0 AND status <> 'genegeerd'"
        " AND ((status NOT IN ('bestaand','gekoppeld') AND first_seen >= ?) OR status = 'bestaand' OR project_id IS NOT NULL)"
        " ORDER BY number DESC LIMIT 2000", (since,))]
    # orderbon gewijzigd (opnieuw opgeslagen in de map): opnieuw ophalen, zodat o.a. de vinkjes bijgewerkt worden
    if bon_seen:
        have = set(want)
        for r in conn.execute("SELECT path, bon_sig FROM order_inbox WHERE bon_received_at IS NOT NULL AND missing = 0"
                              " AND status <> 'genegeerd'"):
            sig = bon_seen.get(r["path"])
            if sig and sig != r["bon_sig"] and r["path"] not in have and len(want) < 2000:
                want.append(r["path"])
    # na-calculatie: nieuwste Excel "Nacalculatie..." per map; alleen opvragen als hij nieuw of gewijzigd is
    want_nacalc = []
    if nacalc_seen:
        stored = {r["path"]: r["nacalc_sig"] for r in conn.execute("SELECT path, nacalc_sig FROM order_inbox WHERE missing = 0")}
        for path, sig in sorted(nacalc_seen.items(), key=lambda kv: kv[0], reverse=True):
            if path in stored and stored[path] != sig:
                want_nacalc.append(path)
            if len(want_nacalc) >= NACALC_PER_RUN:
                break
    return {"ontvangen": len(folders), "ordermappen": len(seen), "nieuw": len(new_items), "eerste_keer": first,
            "want": want, "want_nacalc": want_nacalc, "nacalc_gezien": len(nacalc_seen)}


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
        conn.execute("UPDATE order_inbox SET bon_received_at = ?, note = ?, bon_sig = COALESCE(?, bon_sig) WHERE id = ?",
                     (now, f"Orderbon '{fname}' kon niet worden gelezen.",
                      f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400] if js.get("modified") else None, it["id"]))
        conn.commit()
        return jsonify({"ok": False, "melding": "Orderbon niet leesbaar"}), 200
    fid = save_bytes("order_inbox", it["id"], data, fname, kind="orderbon", mime="application/pdf")
    cid = it["customer_id"]
    if bon.get("customer"):
        cid = sp._match_customer(conn, bon["customer"]) or cid
    sig = f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400] if js.get("modified") else None
    conn.execute("UPDATE order_inbox SET bon_json = ?, bon_file_id = ?, bon_received_at = ?, customer_id = ?,"
                 " description = COALESCE(?, description), bon_sig = COALESCE(?, bon_sig) WHERE id = ?",
                 (json.dumps(bon, ensure_ascii=False), fid, now, cid, bon.get("description"), sig, it["id"]))
    conn.commit()
    result = auto_process(conn, it["id"])
    # nieuwe order (geen bestaande map van vóór de koppeling), eerste keer dat de orderbon binnenkomt: automatisch printen
    from . import printix
    if (it["status"] != "bestaand" and not it["printed_at"] and not it["bon_received_at"] and printix.configured()
            and printix.setting(conn, "printix_auto_orderbon") == "1" and printix.printer(conn)):
        printix.print_background(current_app.config["DB_PATH"], data, f"Orderbon {it['number']}", it["id"])
        result = (result or "") + " · wordt geprint"
        try:
            from .util import audit
            audit("afgedrukt", "order_inbox", it["id"], f"{it['number']} · Orderbon · automatisch bij binnenkomst")
        except Exception:
            pass
    return jsonify({"ok": True, "ordertype": bon.get("order_type"), "resultaat": result})


def _nacalc_reden(fname, exc):
    msg = str(exc)
    if fname.lower().endswith(".xls") or "zip file" in msg.lower():
        return "geen .xlsx-bestand (oud .xls-formaat of beschadigd). Sla het op als Excel-werkmap (.xlsx)."
    if "keyerror" in msg.lower() or "worksheet" in msg.lower() or "kolom" in msg.lower():
        return f"andere opbouw dan de ERP-export ({msg})."
    return msg


@bp.route("/api/komdex/nacalculatie", methods=["POST"])
def nacalculatie():
    """Het script stuurt de nieuwste Excel 'Nacalculatie...' uit een ordermap; WorkPortal importeert hem als na-calculatie."""
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    from .nacalc import import_file
    js = request.get_json(silent=True) or {}
    conn = get_db()
    it = conn.execute("SELECT * FROM order_inbox WHERE path = ?", (str(js.get("path") or ""),)).fetchone()
    if not it:
        return jsonify({"error": "Onbekende map"}), 404
    fname = re.sub(r'[\\/:*?"<>|]+', "_", str(js.get("filename") or "nacalculatie.xlsx"))[:150]
    sig = f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400]
    try:
        data = base64.b64decode(js.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "Ongeldige inhoud"}), 400
    now = now_iso()
    if not NACALC_RE.match(fname) or not data:
        return jsonify({"error": "Geen na-calculatie"}), 400
    pid = it["project_id"]
    if not pid:
        p = conn.execute("SELECT id FROM projects WHERE number = ? ORDER BY id LIMIT 1", (it["number"],)).fetchone()
        pid = p["id"] if p else None
    try:
        nid = import_file(data, fname, project_id=pid, source="komdex")
    except Exception as exc:  # noqa: BLE001 - onleesbaar bestand: onthouden, niet elke 2 minuten opnieuw proberen
        conn.execute("UPDATE order_inbox SET nacalc_sig = ?, nacalc_at = ?, nacalc_note = ? WHERE id = ?",
                     (sig, now, f"'{fname}' kon niet worden ingelezen: {_nacalc_reden(fname, exc)}"[:500], it["id"]))
        conn.commit()
        return jsonify({"ok": False, "resultaat": "niet leesbaar"}), 200
    n = conn.execute("SELECT project_id FROM nacalcs WHERE id = ?", (nid,)).fetchone()
    conn.execute("UPDATE order_inbox SET nacalc_sig = ?, nacalc_at = ?, nacalc_id = ?, nacalc_note = NULL,"
                 " project_id = COALESCE(project_id, ?) WHERE id = ?", (sig, now, nid, n["project_id"] if n else None, it["id"]))
    conn.commit()
    return jsonify({"ok": True, "resultaat": f"geïmporteerd (na-calculatie {nid})"})


@bp.route("/api/komdex/orders", methods=["POST"])
def push():
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    folders = js.get("folders")
    if not isinstance(folders, list):
        return jsonify({"error": "Verwacht {\"folders\": [...]}"}), 400
    return jsonify(receive(get_db(), folders))
