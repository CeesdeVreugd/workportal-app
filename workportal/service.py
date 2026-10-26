from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, Response

from .db import query, execute, get_db
from . import sharepoint as sp
from .integrations import upload_to_sharepoint, sharepoint_configured
from .mail import send_mail
from .pdf import visit_pdf
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .klanten import customer_options, ask_snelstart
from .util import fmt_date
from .util import (now_iso, audit, files_for, save_uploads, save_signature, file_path, to_float, to_int,
                   next_number, get_setting, local, now_utc, delete_file, safe_filename)

bp = Blueprint("service", __name__, url_prefix="/service")

TYPES = [("storing", "Storing"), ("onderhoud", "Service & onderhoud"), ("garantie", "Garantie"),
         ("inspectie", "Inspectie"), ("overig", "Overig")]
STATUSES = [("nieuw", "Nieuw"), ("ingepland", "Ingepland"), ("in_uitvoering", "In uitvoering"),
            ("wacht", "Wacht op onderdelen"), ("afgerond", "Afgerond")]
PRIOS = [("laag", "Laag"), ("normaal", "Normaal"), ("hoog", "Hoog")]
OPEN = ("nieuw", "ingepland", "in_uitvoering", "wacht")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


def _ticket(tid):
    t = query(
        "SELECT t.*, c.name AS customer, c.email AS customer_email, l.name AS location,"
        " TRIM(COALESCE(NULLIF(TRIM(IFNULL(l.address,'') || ' ' || IFNULL(l.postcode,'') || ' ' || IFNULL(l.city,'')), ''), TRIM(IFNULL(c.address,'') || ' ' || IFNULL(c.postcode,'') || ' ' || IFNULL(c.city,'')))) AS visit_address,"
        " i.name AS installation,"
        " i.serial AS serial, i.alert AS inst_alert, i.bring AS inst_bring, c.alert AS customer_alert, p.number AS project_no, p.name AS project_name, p.sharepoint_path, p.sp_item_id AS project_sp, p.sp_missing AS project_sp_missing, p.kind AS project_kind, p.work_description AS project_work, p.bon_file_id AS project_bon,"
        " ct.name AS contact, ct.phone AS contact_phone, ct.email AS contact_email, u.name AS assignee"
        " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id LEFT JOIN locations l ON l.id = t.location_id"
        " LEFT JOIN installations i ON i.id = t.installation_id LEFT JOIN projects p ON p.id = t.project_id"
        " LEFT JOIN contacts ct ON ct.id = t.contact_id LEFT JOIN users u ON u.id = t.assigned_to WHERE t.id = ?",
        (tid,), one=True)
    if not t:
        abort(404)
    return t


def _links():
    """Locatie, machine, contactpersoon en project alleen bewaren als ze bij de gekozen klant horen."""
    cid = to_int(request.form.get("customer_id"))
    out = {}
    for field, table in (("location_id", "locations"), ("installation_id", "installations"), ("contact_id", "contacts")):
        v = to_int(request.form.get(field))
        out[field] = v if v and cid and query(f"SELECT 1 FROM {table} WHERE id = ? AND customer_id = ?", (v, cid), one=True) else None
    pid = to_int(request.form.get("project_id"))
    if pid:
        p = query("SELECT customer_id FROM projects WHERE id = ?", (pid,), one=True)
        out["project_id"] = pid if p and (not p["customer_id"] or p["customer_id"] == cid) else None
    else:
        out["project_id"] = None
    return cid, out


def _lookups(customer_id=None):
    return dict(
        customers=customer_options(customer_id),
        locations=query("SELECT id, customer_id, name FROM locations ORDER BY name"),
        installations=query("SELECT id, customer_id, name, serial FROM installations ORDER BY name"),
        contacts=query("SELECT id, customer_id, name FROM contacts ORDER BY name"),
        projects=query("SELECT id, customer_id, number, name, kind FROM projects WHERE status = 'actief' OR customer_id = ? ORDER BY number DESC",
                       (customer_id or 0,)),
        users=query("SELECT id, name FROM users WHERE active = 1 ORDER BY name"),
        TYPES=TYPES, STATUSES=STATUSES, PRIOS=PRIOS,
    )


@bp.route("/")
@require("service", LEZEN)
def index():
    view = request.args.get("view", "open")
    typ = request.args.get("type", "")
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    if view == "open":
        where.append("t.status IN ('nieuw','ingepland','in_uitvoering','wacht')")
    elif view == "mijn":
        where.append("t.assigned_to = ? AND t.status IN ('nieuw','ingepland','in_uitvoering','wacht')")
        params.append(g.user["id"])
    elif view in ("factureren", "afgerond"):
        where.append("t.status = 'afgerond'")
    if typ:
        where.append("t.type = ?")
        params.append(typ)
    if q:
        where.append("(t.number LIKE ? OR t.title LIKE ? OR IFNULL(c.name,'') LIKE ?)")
        params += [f"%{q}%"] * 3
    sql = ("SELECT t.*, c.name AS customer, i.name AS installation, u.name AS assignee,"
           " TRIM(COALESCE(NULLIF(TRIM(IFNULL(l.address,'') || ' ' || IFNULL(l.city,'')), ''), TRIM(IFNULL(c.address,'') || ' ' || IFNULL(c.city,'')))) AS visit_address,"
           " (IFNULL(i.alert,'') <> '' OR IFNULL(i.bring,'') <> '' OR IFNULL(c.alert,'') <> '') AS has_alert FROM tickets t"
           " LEFT JOIN customers c ON c.id = t.customer_id LEFT JOIN installations i ON i.id = t.installation_id"
           " LEFT JOIN users u ON u.id = t.assigned_to LEFT JOIN locations l ON l.id = t.location_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE t.priority WHEN 'hoog' THEN 0 WHEN 'normaal' THEN 1 ELSE 2 END, t.updated_at DESC LIMIT 300"
    tickets = query(sql, params)
    due = query("SELECT i.*, c.name AS customer FROM installations i JOIN customers c ON c.id = i.customer_id"
                " WHERE i.next_service IS NOT NULL AND i.next_service <= date('now', '+30 days') ORDER BY i.next_service LIMIT 20")
    return render_template("service/index.html", tickets=tickets, view=view, typ=typ, q=q, TYPES=TYPES, TYPE_LABELS=dict(TYPES), due=due)


@bp.route("/nieuw", methods=["GET", "POST"])
@require("service", BEWERKEN)
def new():
    if request.method == "POST":
        mail = _dropped_mail()   # gesleepte mail (.eml): onderwerp, afzender en tekst overnemen als die nog leeg zijn
        title = _f("title") or (mail or {}).get("subject")
        if not title:
            flash("Vul een korte omschrijving in.", "error")
            return render_template("service/form.html", t=request.form, **_lookups(to_int(request.form.get("customer_id"))))
        now = now_iso()
        cid, lk = _links()
        number = next_number("tickets", "T")
        status = "nieuw"   # plannen gebeurt bij de bezoeken
        tid = execute(
            "INSERT INTO tickets (number, type, priority, status, title, description, customer_id, location_id, installation_id,"
            " project_id, contact_id, reported_by, assigned_to, planned_date, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, _f("type") or "storing", _f("priority") or "normaal", status, title[:200],
             _f("description") or (mail or {}).get("body"),
             cid, lk["location_id"], lk["installation_id"], lk["project_id"],
             lk["contact_id"], _f("reported_by") or (mail or {}).get("from"), to_int(request.form.get("assigned_to")),
             None, g.user["id"], now, now))
        save_uploads("ticket", tid)
        if getattr(g, "upload_skipped", 0):
            flash(f"{g.upload_skipped} bestand(en) waren leeg of onleesbaar en zijn niet opgeslagen.", "error")
        audit("aangemaakt", "ticket", tid, number)
        flash(f"Ticket {number} aangemaakt.", "ok")
        target = url_for("service.detail", tid=tid)
        return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    t = {"customer_id": request.args.get("klant"), "installation_id": request.args.get("installatie"),
         "type": request.args.get("type", "storing"), "priority": "normaal", "assigned_to": g.user["id"]}
    return render_template("service/form.html", t=t, **_lookups(to_int(t["customer_id"])))


@bp.route("/<int:tid>")
@require("service", LEZEN)
def detail(tid):
    t = _ticket(tid)
    visits = query("SELECT v.*, u.name AS author FROM visits v LEFT JOIN users u ON u.id = v.created_by"
                   " WHERE v.ticket_id = ? ORDER BY v.date DESC, v.id DESC", (tid,))
    vfiles = {v["id"]: files_for("visit", v["id"]) for v in visits}
    notes = query("SELECT n.*, u.name AS author FROM ticket_notes n LEFT JOIN users u ON u.id = n.created_by"
                  " WHERE n.ticket_id = ? ORDER BY n.created_at DESC, n.id DESC", (tid,))
    nfiles = {n["id"]: files_for("ticket_note", n["id"]) for n in notes}
    history = query("SELECT a.*, u.name AS who FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
                    " WHERE a.entity = 'ticket' AND a.entity_id = ? ORDER BY a.id DESC LIMIT 30", (tid,))
    from .kennis import calcs_for
    import json
    metas = {n["id"]: json.loads(n["mail_meta"]) for n in notes if n["mail_meta"]}
    mail = _mail_defaults(t, notes, metas)
    return render_template("service/detail.html", t=t, visits=visits, vfiles=vfiles, notes=notes, nfiles=nfiles, files=files_for("ticket", tid),
                           toolcalcs=calcs_for(ticket_id=tid), metas=metas, mail=mail, svc_box=svc_mailbox(),
                           history=history, STATUSES=STATUSES, TYPES=dict(TYPES))


@bp.route("/<int:tid>/bewerken", methods=["GET", "POST"])
@require("service", BEWERKEN)
def edit(tid):
    t = _ticket(tid)
    if request.method == "POST":
        if not _f("title"):
            flash("Vul een korte omschrijving in.", "error")
        else:
            cid, lk = _links()
            if to_int(request.form.get("assigned_to")) != t["assigned_to"]:
                execute("UPDATE tickets SET alert_sent_at = NULL WHERE id = ?", (tid,))
            execute("UPDATE tickets SET type=?, priority=?, status=?, title=?, description=?, customer_id=?, location_id=?,"
                    " installation_id=?, project_id=?, contact_id=?, reported_by=?, assigned_to=?, planned_date=?, updated_at=?"
                    " WHERE id=?",
                    (_f("type") or t["type"], _f("priority") or "normaal", _f("status") or t["status"], _f("title"),
                     _f("description"), cid, lk["location_id"], lk["installation_id"], lk["project_id"],
                     lk["contact_id"], _f("reported_by"), to_int(request.form.get("assigned_to")),
                     t["planned_date"], now_iso(), tid))
            save_uploads("ticket", tid)
            audit("gewijzigd", "ticket", tid)
            flash("Ticket opgeslagen.", "ok")
            target = url_for("service.detail", tid=tid)
            return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    return render_template("service/form.html", t=t, edit=True, **_lookups(t["customer_id"]))


@bp.route("/<int:tid>/notitie", methods=["POST"])
@bp.route("/<int:tid>/notitie/<int:nid>", methods=["POST"])
@require("service", BEWERKEN)
def note_save(tid, nid=None):
    t = _ticket(tid)
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Schrijf eerst een notitie.", "error")
        return redirect(url_for("service.detail", tid=tid) + "#notities")
    now = now_iso()
    if nid:
        n = query("SELECT * FROM ticket_notes WHERE id = ? AND ticket_id = ?", (nid, tid), one=True) or abort(404)
        if n["created_by"] != g.user["id"] and not can("service", BEHEER):
            abort(403)
        if n["kind"] != "intern":
            abort(400)   # verstuurde of ontvangen mails worden niet bewerkt
        execute("UPDATE ticket_notes SET body = ?, updated_at = ? WHERE id = ?", (body, now, nid))
    else:
        nid = execute("INSERT INTO ticket_notes (ticket_id, kind, body, created_by, created_at) VALUES (?,?,?,?,?)",
                      (tid, "intern", body, g.user["id"], now))
        execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, tid))
        audit("notitie toegevoegd", "ticket", tid, body[:80])
    save_uploads("ticket_note", nid)
    if getattr(g, "upload_skipped", 0):
        flash(f"{g.upload_skipped} foto('s) waren leeg of onleesbaar en zijn niet opgeslagen.", "error")
    flash("Notitie opgeslagen.", "ok")
    return redirect(url_for("service.detail", tid=tid) + f"#notitie{nid}")


@bp.route("/<int:tid>/notitie/<int:nid>/verwijderen", methods=["POST"])
@require("service", BEWERKEN)
def note_delete(tid, nid):
    n = query("SELECT * FROM ticket_notes WHERE id = ? AND ticket_id = ?", (nid, tid), one=True) or abort(404)
    if n["created_by"] != g.user["id"] and not can("service", BEHEER) and n["kind"] != "mail_in":
        abort(403)
    for f in files_for("ticket_note", nid):
        delete_file(f["id"])
    execute("DELETE FROM ticket_notes WHERE id = ?", (nid,))
    return redirect(url_for("service.detail", tid=tid) + ("#klant" if n["kind"] != "intern" else "#notities"))


@bp.route("/<int:tid>/status", methods=["POST"])
@require("service", BEWERKEN)
def set_status(tid):
    t = _ticket(tid)
    status = request.form.get("status")
    if status not in dict(STATUSES):
        abort(400)
    closed = now_iso() if status == "afgerond" and not t["closed_at"] else t["closed_at"]
    if status in OPEN:
        closed = None
    execute("UPDATE tickets SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?", (status, closed, now_iso(), tid))
    audit("status", "ticket", tid, dict(STATUSES)[status])
    flash(f"Status gewijzigd naar {dict(STATUSES)[status]}.", "ok")
    return redirect(url_for("service.detail", tid=tid))


@bp.route("/<int:tid>/vertrek", methods=["POST"])
@require("service", BEWERKEN)
def depart(tid):
    t = _ticket(tid)
    items = request.form.getlist("item")
    if t["status"] in ("nieuw", "ingepland"):
        execute("UPDATE tickets SET status = 'in_uitvoering', updated_at = ? WHERE id = ?", (now_iso(), tid))
    audit("vertrokken", "ticket", tid, ("meegenomen: " + ", ".join(items)) if items else "meldingen gelezen")
    flash("Goede reis! Vertrek vastgelegd" + (" met alles bij je." if items else "."), "ok")
    return redirect(url_for("service.detail", tid=tid))


@bp.route("/<int:tid>/fotos", methods=["POST"])
@require("service", BEWERKEN)
def add_photos(tid):
    _ticket(tid)
    n = len(save_uploads("ticket", tid, caption=_f("caption")))
    execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_iso(), tid))
    flash(f"{n} bestand(en) toegevoegd.", "ok")
    return redirect(url_for("service.detail", tid=tid))


@bp.route("/<int:tid>/verwijderen", methods=["POST"])
@require("service", BEHEER)
def delete(tid):
    t = _ticket(tid)
    for v in query("SELECT id FROM visits WHERE ticket_id = ?", (tid,)):
        for f in files_for("visit", v["id"]):
            delete_file(f["id"])
    for f in files_for("ticket", tid):
        delete_file(f["id"])
    execute("DELETE FROM tickets WHERE id = ?", (tid,))
    audit("verwijderd", "ticket", tid, t["number"])
    flash(f"Ticket {t['number']} verwijderd.", "ok")
    return redirect(url_for("service.index"))


# ---------------------------------------------------------------- mailen met de klant (zoals bij inkoopfacturen)

import json as _json
import re as _re
import os as _os

MAIL_TAG = _re.compile(r"\[WP-T(\d+)\]")
_RE_PREFIX = _re.compile(r"^\s*((re|aw|antw|fw|fwd|wg|tr)\s*:\s*)+", _re.I)


def svc_mailbox(conn=None):
    """Mailbox waaruit klanten vanuit een ticket gemaild worden (Beheer > Instellingen)."""
    from .util import get_setting as _gs
    if conn is not None:
        r = conn.execute("SELECT value FROM settings WHERE key = 'svc_mailbox'").fetchone()
        v = r["value"] if r else ""
        if not v:
            r = conn.execute("SELECT value FROM settings WHERE key = 'cert_sender'").fetchone()
            v = r["value"] if r else ""
        return (v or _os.environ.get("MAIL_FROM", "")).strip().lower()
    return ((_gs("svc_mailbox") or _gs("cert_sender") or _os.environ.get("MAIL_FROM", "")) or "").strip().lower()


def _mail_defaults(t, notes, metas):
    """Aan/Cc/onderwerp voor het mailformulier. Was er al mail: 'Antwoord allen' op de laatste mail."""
    box = svc_mailbox()
    mails = [n for n in reversed(notes) if n["kind"] in ("mail_in", "mail_uit") and n["id"] in metas]   # oud -> nieuw
    out = {"to": t["contact_email"] or t["customer_email"] or "", "cc": "", "subject": f"Ticket {t['number']}",
           "reply_all": False, "last": None}
    if mails:
        last = mails[-1]
        m = metas[last["id"]]
        if last["kind"] == "mail_uit":
            to, cc = list(m.get("to") or []), list(m.get("cc") or [])
        else:
            to = [m.get("from")] if m.get("from") else []
            cc = list(m.get("to") or []) + list(m.get("cc") or [])
        seen, t2, c2 = {box}, [], []
        for lst, o in ((to, t2), (cc, c2)):
            for a in lst:
                if a and a.lower() not in seen:
                    seen.add(a.lower())
                    o.append(a)
        subj = _RE_PREFIX.sub("", MAIL_TAG.sub("", m.get("subject") or "").strip()) or out["subject"]
        out.update(to=", ".join(t2) or out["to"], cc=", ".join(c2), subject="RE: " + subj, reply_all=True, last=last)
    return out


@bp.route("/<int:tid>/mailen", methods=["POST"])
@require("service", BEWERKEN)
def mail_customer(tid):
    """Mail aan de klant vanuit het ticket. Onderwerp: het ticketnummer (met [WP-T..] zodat het antwoord hier terugkomt)."""
    from .mail import send_mail_with_files, reply_in_thread
    from .util import ALLOWED_EXT, IMAGE_EXT
    t = _ticket(tid)
    f = request.form
    split = lambda v: [a.strip() for a in _re.split(r"[;,\s]+", v or "") if a.strip()]   # noqa: E731
    to, cc = split(f.get("to")), split(f.get("cc"))
    if f.get("cc_me") and g.user["email"] and g.user["email"] not in cc:
        cc.append(g.user["email"])
    bad = [a for a in to + cc if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
    body = (f.get("body") or "").replace("\r\n", "\n").strip()
    back = url_for("service.detail", tid=tid) + "#klant"
    box = svc_mailbox()
    if not to or bad or not body:
        flash("Vul een geldig e-mailadres en een bericht in." + (f" Niet geldig: {', '.join(bad)}" if bad else ""), "error")
        return redirect(back)
    if not box:
        flash("Er is nog geen mailbox voor service ingesteld (Beheer > Instellingen).", "error")
        return redirect(back)
    atts = []
    for up in request.files.getlist("mailfiles"):
        if up and up.filename:
            ext = _os.path.splitext(up.filename)[1].lower()
            if ext in ALLOWED_EXT or ext in IMAGE_EXT:
                data = up.read()
                if data:
                    atts.append((safe_filename(up.filename), data, up.mimetype or "application/octet-stream"))
    notes = query("SELECT * FROM ticket_notes WHERE ticket_id = ? AND kind IN ('mail_in', 'mail_uit') ORDER BY id", (tid,))
    last_in = next((n for n in reversed(notes) if n["kind"] == "mail_in" and n["mail_meta"]
                    and _json.loads(n["mail_meta"]).get("msg_id")), None)
    signer = "werkvoorbereiding:" + (g.user["name"] or "")
    if f.get("reply_all") and last_in:
        lm = _json.loads(last_in["mail_meta"])
        subject = "RE: " + _RE_PREFIX.sub("", lm.get("subject") or "").strip()
        ok, err = reply_in_thread(box, lm["msg_id"], to, body, atts, cc=cc or None, signer=signer)
    else:
        subject = _RE_PREFIX.sub("", MAIL_TAG.sub("", (f.get("subject") or "").strip())).strip() or f"Ticket {t['number']}"
        if f.get("reply_all") and notes:
            subject = "RE: " + subject
        subject = f"{subject} [WP-T{tid}]"
        ok, err = send_mail_with_files(box, to, subject, body, atts, cc=cc or None, signer=signer)
    if not ok:
        flash(f"Mail versturen is niet gelukt: {err}", "error")
        return redirect(back)
    now = now_iso()
    nid = execute("INSERT INTO ticket_notes (ticket_id, kind, body, created_by, created_at, mail_meta) VALUES (?,?,?,?,?,?)",
                  (tid, "mail_uit", body[:8000], g.user["id"], now,
                   _json.dumps({"to": to, "cc": cc, "subject": subject, "from": box, "files": [a[0] for a in atts]},
                               ensure_ascii=False)))
    from .util import save_bytes
    for name, data, mime in atts:
        save_bytes("ticket_note", nid, data, name, kind="attachment", mime=mime)
    execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, tid))
    audit("mail aan klant", "ticket", tid, f"{', '.join(to)}: {subject[:80]}")
    flash(f"Mail verstuurd aan {', '.join(to)}. Het antwoord van de klant komt hier bij Notities klant.", "ok")
    return redirect(back)


def _dropped_mail():
    """Eerste gesleepte/gekozen .eml-bestand uitlezen: {'subject', 'from', 'body'} of None. Het bestand zelf wordt
    gewoon als bijlage bewaard (save_uploads); de stroom wordt teruggezet."""
    from email import policy
    from email.parser import BytesParser
    for up in request.files.getlist("photos"):
        if up and (up.filename or "").lower().endswith(".eml"):
            raw = up.read()
            up.stream.seek(0)
            try:
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                part = msg.get_body(preferencelist=("plain", "html"))
                text = part.get_content() if part else ""
                if part is not None and part.get_content_type() == "text/html":
                    text = _re.sub(r"<(br|/p|/div)[^>]*>", "\n", text, flags=_re.I)
                    text = _re.sub(r"<[^>]+>", "", text)
                    import html as _h
                    text = _h.unescape(text)
                text = _re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
                from email.utils import parseaddr
                nm, ad = parseaddr(str(msg.get("From") or ""))
                frm = (f"{nm} ({ad})" if nm and ad else (nm or ad or "")).strip()
                return {"subject": _RE_PREFIX.sub("", str(msg.get("Subject") or "")).strip()[:200] or None,
                        "from": frm[:150] or None, "body": text[:8000] or None}
            except Exception:  # noqa: BLE001 - onleesbare mail: alleen als bijlage bewaren
                return None
    return None


def _save_file_conn(conn, upload_dir, entity, eid, data, name, mime):
    """Bestand opslaan buiten een request (planner)."""
    import uuid
    ext = _os.path.splitext(name)[1].lower() or ".bin"
    sub = now_utc().strftime("%Y%m")
    _os.makedirs(_os.path.join(upload_dir, sub), exist_ok=True)
    rel = f"{sub}/{uuid.uuid4().hex}{ext}"
    with open(_os.path.join(upload_dir, rel), "wb") as fh:
        fh.write(data)
    conn.execute("INSERT INTO files (entity, entity_id, kind, filename, stored_name, mime, size, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (entity, eid, "attachment", name, rel, mime, len(data), now_iso()))


def poll_replies(conn, upload_dir):
    """Antwoorden van klanten uit de service-mailbox ophalen: alleen mails met [WP-T..] in het onderwerp of uit dezelfde
    mailwisseling als een ticket. Er wordt niets in de mailbox veranderd. Geeft het aantal opgeslagen antwoorden."""
    import base64
    import requests
    from datetime import timedelta
    from .crediteuren import _reply_text
    from .mail import _graph_token
    box = svc_mailbox(conn)
    if not box:
        return 0
    row = conn.execute("SELECT value FROM settings WHERE key = 'svc_since'").fetchone()
    since = (row["value"] if row else "") or (now_utc() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {_graph_token()}"}
    url = (f"https://graph.microsoft.com/v1.0/users/{box}/mailFolders/inbox/messages?$filter=receivedDateTime ge {since}"
           f"&$select=id,subject,from,receivedDateTime,hasAttachments,conversationId&$orderby=receivedDateTime asc&$top=50")
    n, newest, pages = 0, since, 0
    while url and pages < 10:
        pages += 1
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Service-mailbox {box} lezen mislukt ({r.status_code})")
        js = r.json()
        for m in js.get("value", []):
            newest = max(newest, m.get("receivedDateTime") or newest)
            mid = m["id"]
            if conn.execute("SELECT 1 FROM ticket_notes WHERE kind = 'mail_in' AND mail_meta LIKE ?", (f'%"{mid}"%',)).fetchone():
                continue
            tag = MAIL_TAG.search(m.get("subject") or "")
            tid = int(tag.group(1)) if tag else None
            if tid and not conn.execute("SELECT 1 FROM tickets WHERE id = ?", (tid,)).fetchone():
                tid = None
            if not tid and m.get("conversationId"):
                hit = conn.execute("SELECT id FROM tickets WHERE mail_conv = ? ORDER BY id DESC LIMIT 1", (m["conversationId"],)).fetchone()
                tid = hit["id"] if hit else None
            if not tid:
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            if (frm.get("address") or "").lower() == box:
                continue   # eigen verzonden mail
            rb = requests.get(f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}?$select=uniqueBody,body,toRecipients,ccRecipients",
                              headers=dict(headers, Prefer='outlook.body-content-type="text"'), timeout=30)
            js2 = rb.json() if rb.status_code < 400 else {}
            body = _reply_text((js2.get("uniqueBody") or {}).get("content") or (js2.get("body") or {}).get("content") or "")
            rc = lambda k: [((x.get("emailAddress") or {}).get("address") or "").lower() for x in (js2.get(k) or [])]   # noqa: E731
            names = []
            cur = conn.execute("INSERT INTO ticket_notes (ticket_id, kind, body, created_at, mail_meta) VALUES (?,?,?,?,?)",
                               (tid, "mail_in", body or "(geen tekst)", (m.get("receivedDateTime") or now_iso()),
                                _json.dumps({"from": (frm.get("address") or "").lower(), "from_name": frm.get("name") or "",
                                             "subject": m.get("subject") or "", "msg_id": mid, "to": [a for a in rc("toRecipients") if a],
                                             "cc": [a for a in rc("ccRecipients") if a]}, ensure_ascii=False)))
            nid = cur.lastrowid
            if m.get("hasAttachments"):
                ar = requests.get(f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}/attachments", headers=headers, timeout=60)
                for a in (ar.json().get("value", []) if ar.status_code < 400 else []):
                    if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                        continue
                    data = base64.b64decode(a.get("contentBytes") or "")
                    if data:
                        nm = safe_filename(a.get("name") or "bijlage")
                        _save_file_conn(conn, upload_dir, "ticket_note", nid, data, nm, a.get("contentType") or "application/octet-stream")
                        names.append(nm)
                if names:
                    meta = _json.loads(conn.execute("SELECT mail_meta FROM ticket_notes WHERE id = ?", (nid,)).fetchone()["mail_meta"])
                    meta["files"] = names
                    conn.execute("UPDATE ticket_notes SET mail_meta = ? WHERE id = ?", (_json.dumps(meta, ensure_ascii=False), nid))
            conn.execute("UPDATE tickets SET mail_conv = COALESCE(mail_conv, ?), updated_at = ? WHERE id = ?",
                         (m.get("conversationId"), now_iso(), tid))
            t = conn.execute("SELECT number, assigned_to FROM tickets WHERE id = ?", (tid,)).fetchone()
            who = frm.get("name") or frm.get("address") or "klant"
            conn.execute("INSERT INTO audit_log (user_id, action, entity, entity_id, details, created_at) VALUES (?,?,?,?,?,?)",
                         (None, "reactie klant per mail", "ticket", tid, who, now_iso()))
            conn.commit()
            n += 1
            try:
                from .notify import notify_user
                last = conn.execute("SELECT created_by FROM ticket_notes WHERE ticket_id = ? AND kind = 'mail_uit' AND created_by IS NOT NULL"
                                    " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                for uid in {x for x in (t["assigned_to"], last["created_by"] if last else None) if x}:
                    notify_user(conn, uid, f"Reactie op ticket {t['number']}", f"{who}: " + (body[:140] or "(bijlage)"),
                                url=f"/service/{tid}#klant", email=False)
            except Exception as exc:  # pragma: no cover
                print(f"[WorkPortal] Melding reactie ticket mislukt: {exc}", flush=True)
        url = js.get("@odata.nextLink")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('svc_since', ?)", (newest,))
    conn.commit()
    return n


def scheduled_poll(conn, app, state):
    """Vanuit de planner: elke 5 minuten de service-mailbox op antwoorden van klanten controleren."""
    from .mail import graph_configured
    r = conn.execute("SELECT value FROM settings WHERE key = 'svc_mail_on'").fetchone()
    if not r or r["value"] != "1" or not graph_configured():
        return
    t = now_utc().timestamp()
    if t - state.get("svc_poll", 0) < 300:
        return
    state["svc_poll"] = t
    try:
        poll_replies(conn, app.config["UPLOAD_DIR"])
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('svc_last_error', '')")
    except Exception as exc:  # noqa: BLE001
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('svc_last_error', ?)", (str(exc)[:300],))
    conn.commit()


# ---------------------------------------------------------------- bezoeken

@bp.route("/<int:tid>/bezoek", methods=["GET", "POST"])
@bp.route("/<int:tid>/bezoek/<int:vid>", methods=["GET", "POST"])
@require("service", BEWERKEN)
def visit(tid, vid=None):
    t = _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) if vid else None
    if vid and not v:
        abort(404)
    if v and (v["signed_at"] or v["closed_at"]):
        flash("Dit bezoek is afgerond en kan niet meer worden gewijzigd.", "info")
        return redirect(url_for("service.detail", tid=tid))
    if request.method == "POST":
        vals = (_f("date") or local(now_utc()).strftime("%Y-%m-%d"), _f("technicians"),
                to_float(request.form.get("hours"), 0), to_float(request.form.get("travel_hours"), 0),
                to_float(request.form.get("km"), 0), _f("findings"), _f("work_done"), _f("materials"))
        if v:
            execute("UPDATE visits SET date=?, technicians=?, hours=?, travel_hours=?, km=?, findings=?, work_done=?, materials=?"
                    " WHERE id=?", vals + (vid,))
        else:
            vid = execute("INSERT INTO visits (date, technicians, hours, travel_hours, km, findings, work_done, materials,"
                          " ticket_id, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          vals + (tid, g.user["id"], now_iso()))
            if t["status"] in ("nieuw", "ingepland"):
                execute("UPDATE tickets SET status = 'in_uitvoering' WHERE id = ?", (tid,))
        save_uploads("visit", vid)
        if getattr(g, "upload_skipped", 0):
            flash(f"{g.upload_skipped} foto('s) waren leeg of onleesbaar en zijn niet opgeslagen. Voeg ze opnieuw toe.", "error")
        execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_iso(), tid))
        audit("bezoek opgeslagen", "ticket", tid, vals[0])
        if request.form.get("next") == "tekenen":
            return redirect(url_for("service.sign", tid=tid, vid=vid))
        if request.form.get("next") == "afronden":
            _close_visit(tid, vid, None, True)
            flash("Bezoek afgerond zonder handtekening en ticket afgesloten. Je kunt de werkbon nu versturen.", "ok")
            return redirect(url_for("service.visit_send", tid=tid, vid=vid))
        flash("Bezoek opgeslagen.", "ok")
        return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")
    v = v or {"date": local(now_utc()).strftime("%Y-%m-%d"), "technicians": g.user["name"]}
    return render_template("service/visit.html", t=t, v=v, vid=vid, files=files_for("visit", vid) if vid else [])


def _close_visit(tid, vid, note, close_ticket):
    now = now_iso()
    execute("UPDATE visits SET closed_at = ?, closed_by = ?, closed_note = ? WHERE id = ? AND ticket_id = ?",
            (now, g.user["id"], note, vid, tid))
    if close_ticket:
        execute("UPDATE tickets SET status = 'afgerond', closed_at = COALESCE(closed_at, ?), updated_at = ? WHERE id = ?", (now, now, tid))
    audit("bezoek afgerond zonder handtekening", "ticket", tid, note)


@bp.route("/<int:tid>/bezoek/<int:vid>/afronden", methods=["POST"])
@require("service", BEWERKEN)
def visit_close(tid, vid):
    _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) or abort(404)
    if v["signed_at"] or v["closed_at"]:
        flash("Dit bezoek is al afgerond.", "info")
    else:
        close = bool(request.form.get("close_ticket"))
        _close_visit(tid, vid, _f("note"), close)
        flash("Bezoek afgerond zonder handtekening" + (" en ticket afgesloten." if close else ".") + " Je kunt de werkbon nu versturen.", "ok")
        return redirect(url_for("service.visit_send", tid=tid, vid=vid))
    return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")


@bp.route("/<int:tid>/bezoek/<int:vid>/verwijderen", methods=["POST"])
@require("service", BEWERKEN)
def visit_delete(tid, vid):
    t = _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) or abort(404)
    if (v["signed_at"] or v["closed_at"]) and not can("service", BEHEER):
        flash("Een afgerond bezoek kan alleen iemand met Beheer-rechten op Service verwijderen.", "error")
        return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")
    for f in files_for("visit", vid):
        delete_file(f["id"])
    execute("DELETE FROM visits WHERE id = ?", (vid,))
    execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_iso(), tid))
    audit("bezoek verwijderd", "ticket", tid, f"{v['date']}{' (was ondertekend door ' + v['signed_name'] + ')' if v['signed_at'] else ''}")
    flash("Bezoek verwijderd.", "ok")
    return redirect(url_for("service.detail", tid=tid) + "#bezoeken")


@bp.route("/<int:tid>/bezoek/<int:vid>/tekenen", methods=["GET", "POST"])
@require("service", BEWERKEN)
def sign(tid, vid):
    t = _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) or abort(404)
    if request.method == "POST":
        name = _f("signed_name")
        sig = request.form.get("signature")
        if not name or not sig:
            flash("Vul de naam in en laat de klant tekenen.", "error")
            return render_template("service/sign.html", t=t, v=v)
        save_signature("visit", vid, sig)
        execute("UPDATE visits SET signed_name = ?, signed_at = ? WHERE id = ?", (name, now_iso(), vid))
        if request.form.get("close"):
            execute("UPDATE tickets SET status = 'afgerond', closed_at = ?, updated_at = ? WHERE id = ?", (now_iso(), now_iso(), tid))
        audit("werkbon ondertekend", "ticket", tid, name)
        pdf, fname = _werkbon(tid, vid)
        msgs = ["Werkbon ondertekend."]
        to = _f("mail_to")
        if request.form.get("mail") and to:
            ok = send_mail(to, f"Werkbon {t['number']} – De Vreugd Productietechniek",
                           f"Beste {name},\n\nIn de bijlage vindt u de werkbon van ons bezoek.",
                           attachments=[(fname, pdf, "application/pdf")], signer="werkvoorbereiding")
            msgs.append("Werkbon gemaild naar " + to + "." if ok else "Mailen is mislukt.")
        if request.form.get("sharepoint") and _sp_available(t):
            res = sp.upload_document(get_db(), t["project_id"], sp.setting(get_db(), "sp_sub_werkbon"), fname, pdf)
            if res is None:
                ok, msg = upload_to_sharepoint(pdf, fname, _sp_folder(t, "Service"), t["project_no"] or "", "werkbon")
            else:
                ok, msg = res
            execute("UPDATE visits SET sharepoint_status = ? WHERE id = ?", (msg, vid))
            msgs.append(msg + ".")
        flash(" ".join(msgs), "ok")
        return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")
    return render_template("service/sign.html", t=t, v=v, mail_to=t["contact_email"] or t["customer_email"] or "",
                           sharepoint=_sp_available(t))


@bp.route("/<int:tid>/bezoek/<int:vid>/versturen", methods=["GET", "POST"])
@require("service", BEWERKEN)
def visit_send(tid, vid):
    """Werkbon (achteraf) mailen naar de klant en/of in SharePoint zetten, ook als hij niet getekend is."""
    import re as _re
    t = _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) or abort(404)
    back = url_for("service.detail", tid=tid) + f"#bezoek{vid}"
    default_to = t["contact_email"] or t["customer_email"] or ""
    greet = v["signed_name"] or t["contact"] or "relatie"
    if request.method == "POST":
        to = [a.strip() for a in _re.split(r"[;,\s]+", request.form.get("to") or "") if a.strip()]
        cc = [a.strip() for a in _re.split(r"[;,\s]+", request.form.get("cc") or "") if a.strip()]
        if request.form.get("cc_me") and g.user["email"] and g.user["email"] not in cc:
            cc.append(g.user["email"])
        bad = [a for a in to + cc if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
        want_sp = bool(request.form.get("sharepoint")) and _sp_available(t)
        if (not to and not want_sp) or bad:
            flash("Vul een geldig e-mailadres in" + (f" (ongeldig: {', '.join(bad)})" if bad else "") + ", of kies SharePoint.", "error")
            return render_template("service/send.html", t=t, v=v, form=request.form, sharepoint=_sp_available(t))
        pdf, fname = _werkbon(tid, vid)
        msgs = []
        if to:
            from .mail import send_mail_with_files
            import os as _os
            sender = get_setting("cert_sender") or _os.environ.get("CERT_MAIL_FROM") or _os.environ.get("MAIL_FROM") or ""
            ok, err = send_mail_with_files(sender, to, (request.form.get("subject") or "").strip() or f"Werkbon {t['number']}",
                                           (request.form.get("body") or "").strip(), [(fname, pdf, "application/pdf")],
                                           cc=cc or None, signer="werkvoorbereiding")
            if ok:
                execute("UPDATE visits SET sent_at = ?, sent_to = ? WHERE id = ?", (now_iso(), ", ".join(to)[:300], vid))
                audit("werkbon verstuurd", "ticket", tid, f"{v['date']} aan {', '.join(to)}"[:300])
                msgs.append("Werkbon gemaild naar " + ", ".join(to) + ".")
            else:
                flash(f"Mailen is mislukt: {err}", "error")
                return render_template("service/send.html", t=t, v=v, form=request.form, sharepoint=_sp_available(t))
        if want_sp:
            res = sp.upload_document(get_db(), t["project_id"], sp.setting(get_db(), "sp_sub_werkbon"), fname, pdf)
            if res is None:
                ok, msg = upload_to_sharepoint(pdf, fname, _sp_folder(t, "Service"), t["project_no"] or "", "werkbon")
            else:
                ok, msg = res
            execute("UPDATE visits SET sharepoint_status = ? WHERE id = ?", (msg, vid))
            msgs.append(msg + ".")
        flash(" ".join(msgs), "ok")
        return redirect(back)
    form = {"to": default_to, "cc_me": "1",
            "subject": f"Werkbon {t['number']} – {t['title']}",
            "body": f"Beste {greet},\n\nIn de bijlage vindt u de werkbon van ons bezoek op {fmt_date(v['date'])}."
                    + ("" if v["signed_at"] else "\nDe werkbon is niet ter plaatse ondertekend.")
                    + "\n\nHeeft u vragen, neem dan gerust contact met ons op."}
    return render_template("service/send.html", t=t, v=v, form=form, sharepoint=_sp_available(t))


def _sp_available(t):
    """Werkbon kan naar SharePoint: direct in de gekoppelde projectmap, of via de Power Automate-flow."""
    return sharepoint_configured() or bool(t["project_sp"] and not t["project_sp_missing"] and sp.connected(get_db()))


def _sp_folder(t, sub):
    root = get_setting("sharepoint_root") or "Projecten"
    base = t["sharepoint_path"] or (f"{root}/{t['project_no']}" if t["project_no"] else f"{root}/Service zonder project")
    return f"{base}/{sub}"


def _werkbon(tid, vid):
    t = dict(_ticket(tid))
    v = dict(query("SELECT * FROM visits WHERE id = ?", (vid,), one=True))
    photos = [(file_path(f), f["caption"]) for f in files_for("visit", vid, "photo")]
    sigs = files_for("visit", vid, "signature")
    pdf = visit_pdf(t, v, photos, file_path(sigs[-1]) if sigs else None)
    fname = safe_filename(f"Werkbon {t['number']} {v['date']}.pdf")
    return pdf, fname


@bp.route("/<int:tid>/bezoek/<int:vid>/werkbon.pdf")
@require("service", LEZEN)
def werkbon(tid, vid):
    _ticket(tid)
    pdf, fname = _werkbon(tid, vid)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})
