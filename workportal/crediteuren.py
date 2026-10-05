"""Inkoopfacturen (crediteuren): één plek per factuur met PDF, status, vragen, opmerkingen en logboek.

Facturen komen binnen via de mailbox (Microsoft Graph, alleen lezen) of door uploaden.
Statussen: nieuw -> erp (verwerkt in ERP) -> snelstart (verwerkt in SnelStart); of afgewezen.
Een open vraag (question_to) staat los van de status.
"""
import base64
import hashlib
import io
import os
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, current_app

from .db import query, execute, get_db
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, now_utc, to_float, safe_filename, fmt_eur

bp = Blueprint("crediteuren", __name__, url_prefix="/inkoopfacturen")

MODULE = "crediteuren"
STATUS = {"nieuw": "Nieuw", "erp": "Verwerkt in ERP", "snelstart": "Verwerkt in SnelStart", "afgewezen": "Afgewezen"}
STATUS_BADGE = {"nieuw": "b-blue", "erp": "b-purple", "snelstart": "b-ok", "afgewezen": "b-bad", "vraag": "b-warn"}
DEFAULT_MAILBOX = "factuur@devreugd-pt.nl"
POLL_SECONDS = 300
_poll_lock = threading.Lock()


# ------------------------------------------------------------------ PDF uitlezen

_AMOUNT = r"(?:€|eur|euro)?\s*(-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|-?\d+(?:,\d{2})|-?\d{1,3}(?:,\d{3})*\.\d{2})"
_DATE = r"(\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:jan|feb|mrt|maa|apr|mei|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]*\.?\s+\d{4})"
_MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12}


def parse_amount(s):
    if s is None:
        return None
    s = str(s).strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    if re.search(r",\d{2}$", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{2}$", s):
        s = s.replace(",", "")
    else:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(s):
    if not s:
        return None
    s = s.strip().lower()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})$", s)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.match(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})$", s)
            if not m or m[2][:3] not in _MONTHS:
                return None
            d, mo, y = int(m[1]), _MONTHS[m[2][:3]], int(m[3])
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _find(patterns, text, group=1):
    for p in patterns:
        m = re.search(p, text, re.I | re.M)
        if m:
            return m.group(group).strip()
    return None


def extract(data):
    """Leest factuurnummer, datums en bedragen uit een (tekst-)PDF. Geeft altijd een dict; ontbrekend = None."""
    out = {"invoice_no": None, "invoice_date": None, "due_date": None, "amount_incl": None, "amount_excl": None,
           "supplier_guess": None}
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    except Exception:
        return out
    if not text.strip():
        return out
    sep = r"[\s.:#\-]*"
    out["invoice_no"] = _find([
        r"factuur\s*(?:nummer|nr\.?|no\.?)" + sep + r"([A-Z0-9][A-Z0-9\-/.]{2,24})",
        r"invoice\s*(?:number|no\.?|nr\.?|#)" + sep + r"([A-Z0-9][A-Z0-9\-/.]{2,24})",
        r"rechnungs?\s*(?:nummer|nr\.?)" + sep + r"([A-Z0-9][A-Z0-9\-/.]{2,24})",
        r"^\s*factuur" + sep + r"([A-Z]{0,4}[\-]?\d[\dA-Z\-/.]{2,20})",
    ], text)
    if out["invoice_no"]:
        out["invoice_no"] = out["invoice_no"].rstrip(".-/")
    out["invoice_date"] = parse_date(_find([
        r"factuur\s*datum" + sep + _DATE, r"invoice\s*date" + sep + _DATE, r"rechnungsdatum" + sep + _DATE,
        r"^\s*datum" + sep + _DATE, r"\bdatum" + sep + _DATE], text))
    out["due_date"] = parse_date(_find([
        r"verval\s*datum" + sep + _DATE, r"vervalt\s*op" + sep + _DATE, r"due\s*date" + sep + _DATE,
        r"betalen\s*(?:voor|vóór|uiterlijk)" + sep + _DATE, r"f[äa]llig(?:keit)?" + sep + _DATE], text))
    incl = _find([
        r"te\s*betalen[^\n\d€-]{0,25}" + _AMOUNT, r"totaal\s*(?:incl\.?|inclusief)[^\n\d€-]{0,25}" + _AMOUNT,
        r"total\s*(?:incl\.?|including|amount\s*due)[^\n\d€-]{0,25}" + _AMOUNT, r"amount\s*due[^\n\d€-]{0,25}" + _AMOUNT,
        r"gesamtbetrag[^\n\d€-]{0,25}" + _AMOUNT, r"factuurbedrag[^\n\d€-]{0,25}" + _AMOUNT,
        r"^\s*totaal[^\n\d€-]{0,15}" + _AMOUNT, r"^\s*total[^\n\d€-]{0,15}" + _AMOUNT], text)
    out["amount_incl"] = parse_amount(incl)
    excl = _find([
        r"totaal\s*(?:excl\.?|exclusief)[^\n\d€-]{0,25}" + _AMOUNT, r"subtotaal[^\n\d€-]{0,25}" + _AMOUNT,
        r"total\s*(?:excl\.?|excluding|net)[^\n\d€-]{0,25}" + _AMOUNT, r"netto(?:bedrag)?[^\n\d€-]{0,25}" + _AMOUNT,
        r"sub\s*total[^\n\d€-]{0,25}" + _AMOUNT], text)
    out["amount_excl"] = parse_amount(excl)
    if out["amount_incl"] is None:
        nums = [parse_amount(x) for x in re.findall(r"€\s*" + _AMOUNT, text)]
        nums = [n for n in nums if n]
        if nums:
            out["amount_incl"] = max(nums)
    first = next((l.strip() for l in text.splitlines() if len(l.strip()) > 2 and not re.search(r"factuur|invoice|rechnung", l, re.I)), None)
    out["supplier_guess"] = (first or "")[:80] or None
    return out


# ------------------------------------------------------------------ opslaan (ook buiten een request)

def _upload_dir(app=None):
    d = (app or current_app).config["UPLOAD_DIR"]
    os.makedirs(d, exist_ok=True)
    return d


def _store_file(conn, upload_dir, invoice_id, data, filename, user_id=None):
    sub = now_utc().strftime("%Y%m")
    os.makedirs(os.path.join(upload_dir, sub), exist_ok=True)
    rel = f"{sub}/{uuid.uuid4().hex}.pdf"
    with open(os.path.join(upload_dir, rel), "wb") as fh:
        fh.write(data)
    cur = conn.execute(
        "INSERT INTO files (entity, entity_id, kind, filename, stored_name, mime, size, caption, created_by, created_at)"
        " VALUES ('invoice',?,?,?,?,?,?,?,?,?)",
        (invoice_id, "invoice", filename, rel, "application/pdf", len(data), None, user_id, now_iso()))
    return cur.lastrowid


def log(conn, invoice_id, text, user_id=None):
    conn.execute("INSERT INTO invoice_log (invoice_id, user_id, action, created_at) VALUES (?,?,?,?)",
                 (invoice_id, user_id, text, now_iso()))


def create_invoice(conn, upload_dir, data, filename, source="upload", user_id=None, mail=None):
    """Maakt een factuurkaart van een PDF. Geeft (id, nieuw?) terug; dubbele PDF's worden herkend."""
    sha = hashlib.sha256(data).hexdigest()
    dup = conn.execute("SELECT id FROM invoices WHERE sha256 = ?", (sha,)).fetchone()
    if dup:
        return dup["id"], False
    x = extract(data)
    mail = mail or {}
    supplier = None
    if mail.get("from_addr"):
        prev = conn.execute("SELECT supplier FROM invoices WHERE mail_from = ? AND IFNULL(supplier,'') <> ''"
                            " ORDER BY id DESC LIMIT 1", (mail["from_addr"],)).fetchone()
        supplier = prev["supplier"] if prev else (mail.get("from_name") or None)
    supplier = supplier or x["supplier_guess"]
    if x["invoice_no"] and supplier:
        same = conn.execute("SELECT id FROM invoices WHERE invoice_no = ? AND supplier = ?", (x["invoice_no"], supplier)).fetchone()
        if same:
            return same["id"], False
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO invoices (status, supplier, invoice_no, invoice_date, due_date, amount_incl, amount_excl, source,"
        " mail_id, mail_from, mail_subject, received_at, sha256, created_by, created_at, updated_at)"
        " VALUES ('nieuw',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (supplier, x["invoice_no"], x["invoice_date"], x["due_date"], x["amount_incl"], x["amount_excl"], source,
         mail.get("key"), mail.get("from_addr"), mail.get("subject"), mail.get("received") or now, sha, user_id, now, now))
    iid = cur.lastrowid
    fid = _store_file(conn, upload_dir, iid, data, safe_filename(filename) or "factuur.pdf", user_id)
    conn.execute("UPDATE invoices SET file_id = ? WHERE id = ?", (fid, iid))
    if source == "mail":
        log(conn, iid, f"Binnengekomen per mail van {mail.get('from_name') or mail.get('from_addr') or 'onbekend'}"
                       f" ({mail.get('subject') or 'geen onderwerp'})")
    else:
        log(conn, iid, "Geüpload", user_id)
    found = [k for k in ("invoice_no", "invoice_date", "due_date", "amount_incl") if x[k]]
    if found:
        log(conn, iid, "Gegevens automatisch uitgelezen: " + ", ".join(
            {"invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum", "amount_incl": "bedrag"}[k] for k in found))
    conn.commit()
    return iid, True


# ------------------------------------------------------------------ mailbox (Microsoft Graph, alleen lezen)

def _setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r and r["value"] not in (None, "") else default


def _set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def mail_configured():
    return all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"))


def poll_mailbox(conn, upload_dir):
    """Haalt nieuwe PDF-bijlagen op uit de factuurmailbox. Wijzigt niets in de mailbox. Geeft (nieuw, melding)."""
    if not _poll_lock.acquire(blocking=False):
        return 0, "Ophalen loopt al."
    try:
        return _poll(conn, upload_dir)
    finally:
        _poll_lock.release()


def _poll(conn, upload_dir):
    import requests
    from .mail import _graph_token
    box = _setting(conn, "cred_mailbox", DEFAULT_MAILBOX)
    since = _setting(conn, "cred_since")
    if not since:
        since = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _set(conn, "cred_since", since)
    headers = {"Authorization": f"Bearer {_graph_token()}"}
    url = (f"https://graph.microsoft.com/v1.0/users/{box}/mailFolders/inbox/messages"
           f"?$filter=receivedDateTime ge {since} and hasAttachments eq true"
           f"&$select=id,subject,from,receivedDateTime&$orderby=receivedDateTime asc&$top=50")
    new = 0
    pages = 0
    while url and pages < 10:
        pages += 1
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            raise RuntimeError(f"Mailbox {box} lezen mislukt ({r.status_code}): {msg}")
        js = r.json()
        for m in js.get("value", []):
            mid = m["id"]
            if conn.execute("SELECT 1 FROM invoice_mail_seen WHERE message_id = ?", (mid,)).fetchone():
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            ar = requests.get(f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}/attachments",
                              headers=headers, timeout=60)
            if ar.status_code >= 400:
                raise RuntimeError(f"Bijlagen lezen mislukt ({ar.status_code})")
            for a in ar.json().get("value", []):
                name = a.get("name") or "factuur.pdf"
                if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                    continue
                if not (name.lower().endswith(".pdf") or (a.get("contentType") or "").lower() == "application/pdf"):
                    continue
                data = base64.b64decode(a.get("contentBytes") or "")
                if not data.startswith(b"%PDF"):
                    continue
                received = (m.get("receivedDateTime") or "").replace("Z", "+00:00")
                _, created = create_invoice(conn, upload_dir, data, name, source="mail", mail={
                    "key": f"{mid}:{a.get('id')}", "from_addr": (frm.get("address") or "").lower(),
                    "from_name": frm.get("name"), "subject": m.get("subject"), "received": received or None})
                new += 1 if created else 0
            conn.execute("INSERT OR IGNORE INTO invoice_mail_seen (message_id, received_at) VALUES (?,?)",
                         (mid, m.get("receivedDateTime")))
            conn.commit()
        url = js.get("@odata.nextLink")
    return new, None


def scheduled_poll(conn, app, state):
    """Vanuit de planner: elke 5 minuten de mailbox, als dat aanstaat."""
    if _setting(conn, "cred_mail_on", "0") != "1" or not mail_configured():
        return
    t = now_utc().timestamp()
    if t - state.get("cred_poll", 0) < POLL_SECONDS:
        return
    state["cred_poll"] = t
    try:
        n, _ = poll_mailbox(conn, _upload_dir(app))
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", "")
        if n:
            print(f"[WorkPortal] Inkoopfacturen: {n} nieuwe factuur/facturen uit de mailbox", flush=True)
    except Exception as exc:
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", str(exc)[:400])
        traceback.print_exc()


# ------------------------------------------------------------------ helpers voor de schermen

def _status_key(r):
    if r["status"] in ("nieuw", "erp") and r["question_to"]:
        return "vraag"
    return r["status"]


def _users():
    return query("SELECT u.id, u.name FROM users u WHERE u.active = 1 ORDER BY u.name")


def _load(iid):
    r = query("SELECT i.*, q.name AS question_name, qb.name AS question_by_name, rb.name AS rejected_name FROM invoices i"
              " LEFT JOIN users q ON q.id = i.question_to LEFT JOIN users qb ON qb.id = i.question_by"
              " LEFT JOIN users rb ON rb.id = i.rejected_by"
              " WHERE i.id = ?", (iid,), one=True)
    if not r:
        abort(404)
    return r


def _notify(user_id, title, body, iid):
    try:
        from .notify import notify_user
        notify_user(get_db(), user_id, title, body, url=url_for("crediteuren.detail", iid=iid))
    except Exception:
        traceback.print_exc()


def open_counts():
    """Voor menu en dashboard."""
    if not getattr(g, "user", None) or not can(MODULE):
        return {}
    r = query("SELECT SUM(status='nieuw' AND question_to IS NULL) AS nieuw,"
              " SUM(status IN ('nieuw','erp') AND question_to IS NOT NULL) AS vraag,"
              " SUM(status='erp' AND question_to IS NULL) AS erp,"
              " SUM(status IN ('nieuw','erp') AND question_to = ?) AS mij FROM invoices", (g.user["id"],), one=True)
    return {k: (r[k] or 0) for k in ("nieuw", "vraag", "erp", "mij")}


# ------------------------------------------------------------------ routes

FILTERS = [
    ("open", "Alles open"),
    ("nieuw", "Nieuw"),
    ("erp", "Naar SnelStart"),
    ("vraag", "Vraag open"),
    ("mij", "Wacht op mij"),
    ("boekhouding", "Voor de boekhouding"),
    ("afgewezen", "Afgewezen"),
    ("klaar", "Afgerond"),
    ("alle", "Alle"),
]


@bp.route("/")
@require(MODULE)
def index():
    flt = request.args.get("filter") or "open"
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    uid = g.user["id"]
    cond = {
        "open": "i.status IN ('nieuw','erp')",
        "nieuw": "i.status = 'nieuw' AND i.question_to IS NULL",
        "erp": "i.status = 'erp' AND i.question_to IS NULL",
        "vraag": "i.status IN ('nieuw','erp') AND i.question_to IS NOT NULL",
        "mij": "i.status IN ('nieuw','erp') AND i.question_to = ?",
        "boekhouding": "i.status <> 'snelstart' AND EXISTS (SELECT 1 FROM invoice_notes n WHERE n.invoice_id = i.id AND n.for_accounting = 1)",
        "afgewezen": "i.status = 'afgewezen'",
        "klaar": "i.status = 'snelstart'",
        "alle": "1=1",
    }
    if flt not in cond:
        flt = "open"
    where.append(cond[flt])
    if flt == "mij":
        params.append(uid)
    if q:
        where.append("(i.supplier LIKE ? OR i.invoice_no LIKE ? OR i.erp_ref LIKE ? OR i.description LIKE ? OR i.mail_subject LIKE ?)")
        params += [f"%{q}%"] * 5
    rows = query(
        "SELECT i.*, q.name AS question_name,"
        " (SELECT COUNT(*) FROM invoice_notes n WHERE n.invoice_id = i.id AND n.for_accounting = 1) AS acc_notes,"
        " (SELECT l.action || ' · ' || IFNULL(u.name, 'systeem') FROM invoice_log l LEFT JOIN users u ON u.id = l.user_id"
        "   WHERE l.invoice_id = i.id ORDER BY l.id DESC LIMIT 1) AS last_action"
        " FROM invoices i LEFT JOIN users q ON q.id = i.question_to WHERE " + " AND ".join(where) +
        " ORDER BY CASE WHEN i.status IN ('nieuw','erp') THEN 0 ELSE 1 END, i.received_at DESC, i.id DESC LIMIT 500", params)
    c = query("SELECT SUM(status IN ('nieuw','erp')) AS open, SUM(status='nieuw' AND question_to IS NULL) AS nieuw,"
              " SUM(status='erp' AND question_to IS NULL) AS erp, SUM(status IN ('nieuw','erp') AND question_to IS NOT NULL) AS vraag,"
              " SUM(status IN ('nieuw','erp') AND question_to = ?) AS mij, SUM(status='afgewezen') AS afgewezen,"
              " SUM(status='snelstart') AS klaar, COUNT(*) AS alle,"
              " SUM(status IN ('nieuw','erp') AND due_date IS NOT NULL AND due_date <= date('now','+7 day')) AS verval,"
              " SUM(status = 'afgewezen' AND rejected_at >= date('now','start of month')) AS af_maand"
              " FROM invoices", (uid,), one=True)
    counts = {k: (c[k] or 0) for k in c.keys()}
    counts["boekhouding"] = query("SELECT COUNT(DISTINCT n.invoice_id) AS n FROM invoice_notes n JOIN invoices i ON i.id = n.invoice_id"
                                  " WHERE n.for_accounting = 1 AND i.status <> 'snelstart'", one=True)["n"] or 0
    items = []
    for r in rows:
        d = dict(r)
        d["skey"] = _status_key(r)
        d["label"] = ("Vraag open bij " + (r["question_name"] or "collega")) if d["skey"] == "vraag" else STATUS.get(r["status"], r["status"])
        items.append(d)
    conn = get_db()
    mail = {"on": _setting(conn, "cred_mail_on", "0") == "1", "box": _setting(conn, "cred_mailbox", DEFAULT_MAILBOX),
            "last": _setting(conn, "cred_last_poll"), "error": _setting(conn, "cred_last_error"), "configured": mail_configured()}
    return render_template("crediteuren/index.html", items=items, flt=flt, q=q, counts=counts, FILTERS=FILTERS,
                           STATUS_BADGE=STATUS_BADGE, mail=mail, soon=(now_utc() + timedelta(days=7)).strftime("%Y-%m-%d"))


@bp.route("/uploaden", methods=["POST"])
@require(MODULE, BEWERKEN)
def upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Kies een of meer PDF-bestanden.", "error")
        return redirect(url_for("crediteuren.index"))
    conn = get_db()
    made, dup, skipped, last = 0, 0, [], None
    for f in files:
        data = f.read()
        if not data.startswith(b"%PDF"):
            skipped.append(f.filename)
            continue
        iid, created = create_invoice(conn, _upload_dir(), data, f.filename, source="upload", user_id=g.user["id"])
        last = iid
        made += created
        dup += not created
    if skipped:
        flash("Geen PDF, overgeslagen: " + ", ".join(skipped), "error")
    if dup:
        flash(f"{dup} factuur/facturen stond(en) er al in.", "info")
    if made == 1 and len(files) == 1:
        return redirect(url_for("crediteuren.detail", iid=last))
    if made:
        flash(f"{made} factuur/facturen toegevoegd.", "success")
    return redirect(url_for("crediteuren.index"))


@bp.route("/ophalen", methods=["POST"])
@require(MODULE, BEWERKEN)
def fetch():
    conn = get_db()
    if not mail_configured():
        flash("De Microsoft-koppeling is niet ingesteld (GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET).", "error")
        return redirect(url_for("crediteuren.index"))
    try:
        n, msg = poll_mailbox(conn, _upload_dir())
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", "")
        flash(msg or (f"{n} nieuwe factuur/facturen opgehaald." if n else "Geen nieuwe facturen in de mailbox."),
              "success" if n else "info")
    except Exception as exc:
        _set(conn, "cred_last_error", str(exc)[:400])
        flash(f"Ophalen mislukt: {exc}", "error")
    return redirect(url_for("crediteuren.index"))


@bp.route("/<int:iid>")
@require(MODULE)
def detail(iid):
    r = _load(iid)
    conn = get_db()
    if r["status"] == "nieuw" and not r["seen_at"] and r["created_by"] != g.user["id"]:
        conn.execute("UPDATE invoices SET seen_at = ?, seen_by = ? WHERE id = ?", (now_iso(), g.user["id"], iid))
        log(conn, iid, "Bekeken", g.user["id"])
        conn.commit()
        r = _load(iid)
    notes = query("SELECT n.*, u.name AS who, t.name AS to_name FROM invoice_notes n LEFT JOIN users u ON u.id = n.created_by"
                  " LEFT JOIN users t ON t.id = n.to_user WHERE n.invoice_id = ? ORDER BY n.id", (iid,))
    logs = query("SELECT l.*, u.name AS who FROM invoice_log l LEFT JOIN users u ON u.id = l.user_id"
                 " WHERE l.invoice_id = ? ORDER BY l.id DESC", (iid,))
    d = dict(r)
    d["skey"] = _status_key(r)
    d["label"] = ("Vraag open bij " + (r["question_name"] or "collega")) if d["skey"] == "vraag" else STATUS.get(r["status"], r["status"])
    back = request.args.get("terug") or url_for("crediteuren.index")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("crediteuren.index")
    return render_template("crediteuren/detail.html", r=d, notes=notes, logs=logs, users=_users(), STATUS_BADGE=STATUS_BADGE,
                           back=back)


@bp.route("/<int:iid>/gegevens", methods=["POST"])
@require(MODULE, BEWERKEN)
def save(iid):
    r = _load(iid)
    f = request.form
    vals = {
        "supplier": (f.get("supplier") or "").strip()[:150] or None,
        "invoice_no": (f.get("invoice_no") or "").strip()[:60] or None,
        "invoice_date": f.get("invoice_date") or None,
        "due_date": f.get("due_date") or None,
        "amount_incl": parse_amount(f.get("amount_incl")),
        "amount_excl": parse_amount(f.get("amount_excl")),
        "erp_ref": (f.get("erp_ref") or "").strip()[:60] or None,
        "description": (f.get("description") or "").strip()[:300] or None,
    }
    changed = [k for k, v in vals.items() if (r[k] if not isinstance(v, float) else (r[k] if r[k] is None else round(r[k], 2))) != v]
    if changed:
        execute("UPDATE invoices SET " + ", ".join(f"{k} = ?" for k in vals) + ", updated_at = ? WHERE id = ?",
                list(vals.values()) + [now_iso(), iid])
        names = {"supplier": "leverancier", "invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum",
                 "amount_incl": "bedrag incl.", "amount_excl": "bedrag excl.", "erp_ref": "inkooporder ERP", "description": "omschrijving"}
        conn = get_db()
        log(conn, iid, "Gegevens aangepast: " + ", ".join(names[k] for k in changed), g.user["id"])
        conn.commit()
        flash("Gegevens opgeslagen.", "success")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/status", methods=["POST"])
@require(MODULE, BEWERKEN)
def set_status(iid):
    r = _load(iid)
    action = request.form.get("action")
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    if action == "erp" and r["status"] == "nieuw":
        conn.execute("UPDATE invoices SET status='erp', erp_at=?, erp_by=?, updated_at=? WHERE id=?", (now, uid, now, iid))
        log(conn, iid, "Verwerkt in ERP", uid)
    elif action == "snelstart" and r["status"] == "erp":
        conn.execute("UPDATE invoices SET status='snelstart', snelstart_at=?, snelstart_by=?, updated_at=? WHERE id=?", (now, uid, now, iid))
        log(conn, iid, "Verwerkt in SnelStart", uid)
    elif action == "terug" and r["status"] in ("erp", "snelstart", "afgewezen"):
        prev = {"erp": "nieuw", "snelstart": "erp", "afgewezen": "nieuw"}[r["status"]]
        conn.execute("UPDATE invoices SET status=?, updated_at=? WHERE id=?", (prev, now, iid))
        log(conn, iid, f"Teruggezet van '{STATUS[r['status']]}' naar '{STATUS[prev]}'", uid)
    else:
        abort(400)
    conn.commit()
    if action == "snelstart" or (action == "erp" and request.form.get("next") == "1"):
        nxt = query("SELECT id FROM invoices WHERE status = ? AND question_to IS NULL AND id <> ? ORDER BY received_at, id LIMIT 1",
                    ("nieuw" if action == "erp" else "erp", iid), one=True)
        if nxt and request.form.get("next") == "1":
            return redirect(url_for("crediteuren.detail", iid=nxt["id"]))
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/afwijzen", methods=["POST"])
@require(MODULE, BEWERKEN)
def reject(iid):
    r = _load(iid)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Geef een reden voor het afwijzen.", "error")
        return redirect(request.form.get("terug") or url_for("crediteuren.detail", iid=iid))
    if r["status"] == "snelstart":
        abort(400)
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    conn.execute("UPDATE invoices SET status='afgewezen', rejected_at=?, rejected_by=?, reject_reason=?, question_to=NULL,"
                 " updated_at=? WHERE id=?", (now, uid, reason[:500], now, iid))
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, created_by, created_at) VALUES (?,?,?,?,?)",
                 (iid, "afwijzing", reason[:2000], uid, now))
    log(conn, iid, "Afgewezen: " + reason[:120], uid)
    conn.commit()
    flash("Factuur afgewezen.", "success")
    back = request.form.get("terug") or ""
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/vraag", methods=["POST"])
@require(MODULE, BEWERKEN)
def ask(iid):
    r = _load(iid)
    to = request.form.get("to_user", type=int)
    body = (request.form.get("body") or "").strip()
    target = query("SELECT id, name FROM users WHERE id = ? AND active = 1", (to,), one=True) if to else None
    if not target or not body:
        flash("Kies een collega en schrijf je vraag.", "error")
        return redirect(url_for("crediteuren.detail", iid=iid))
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, to_user, created_by, created_at) VALUES (?,?,?,?,?,?)",
                 (iid, "vraag", body[:4000], target["id"], uid, now))
    conn.execute("UPDATE invoices SET question_to=?, question_by=?, question_at=?, updated_at=? WHERE id=?",
                 (target["id"], uid, now, now, iid))
    log(conn, iid, f"Vraag gesteld aan {target['name']}", uid)
    conn.commit()
    if target["id"] != uid:
        _notify(target["id"], f"Vraag over factuur {r['supplier'] or ''} {r['invoice_no'] or ''}".strip(),
                f"{g.user['name']} vraagt: {body[:400]}", iid)
    flash(f"Vraag gesteld aan {target['name']}.", "success")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/reactie", methods=["POST"])
@require(MODULE)
def comment(iid):
    r = _load(iid)
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("crediteuren.detail", iid=iid))
    acc = 1 if request.form.get("for_accounting") else 0
    uid = g.user["id"]
    answers = bool(r["question_to"]) and (r["question_to"] == uid or request.form.get("close_question"))
    if not can(MODULE, BEWERKEN) and r["question_to"] != uid:
        abort(403)
    conn = get_db()
    now = now_iso()
    kind = "antwoord" if answers else ("boekhouding" if acc else "opmerking")
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, for_accounting, created_by, created_at) VALUES (?,?,?,?,?,?)",
                 (iid, kind, body[:4000], acc, uid, now))
    if answers:
        asker = r["question_by"]
        conn.execute("UPDATE invoices SET question_to=NULL, updated_at=? WHERE id=?", (now, iid))
        log(conn, iid, "Vraag beantwoord", uid)
        conn.commit()
        if asker and asker != uid:
            _notify(asker, f"Antwoord op je vraag over factuur {r['supplier'] or ''} {r['invoice_no'] or ''}".strip(),
                    f"{g.user['name']}: {body[:400]}", iid)
    else:
        log(conn, iid, "Opmerking voor de boekhouding" if acc else "Opmerking geplaatst", uid)
        conn.commit()
    return redirect(url_for("crediteuren.detail", iid=iid) + "#gesprek")


@bp.route("/<int:iid>/opmerking/<int:nid>/afgehandeld", methods=["POST"])
@require(MODULE, BEWERKEN)
def note_done(iid, nid):
    n = query("SELECT * FROM invoice_notes WHERE id = ? AND invoice_id = ?", (nid, iid), one=True) or abort(404)
    execute("UPDATE invoice_notes SET handled = ? WHERE id = ?", (0 if n["handled"] else 1, nid))
    return redirect(url_for("crediteuren.detail", iid=iid) + "#gesprek")


@bp.route("/<int:iid>/verwijderen", methods=["POST"])
@require(MODULE, BEHEER)
def delete(iid):
    r = _load(iid)
    from .util import delete_file
    for fr in query("SELECT id FROM files WHERE entity = 'invoice' AND entity_id = ?", (iid,)):
        delete_file(fr["id"])
    execute("DELETE FROM invoices WHERE id = ?", (iid,))
    flash(f"Factuur {r['supplier'] or ''} {r['invoice_no'] or ''} verwijderd.".replace("  ", " "), "success")
    return redirect(url_for("crediteuren.index"))
