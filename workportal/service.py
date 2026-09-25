from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, Response

from .db import query, execute
from .integrations import upload_to_sharepoint, sharepoint_configured
from .mail import send_mail
from .pdf import visit_pdf
from .permissions import require, LEZEN, BEWERKEN, BEHEER
from .klanten import customer_options, ask_snelstart
from .util import (now_iso, audit, files_for, save_uploads, save_signature, file_path, to_float, to_int,
                   next_number, get_setting, local, now_utc, delete_file, safe_filename)

bp = Blueprint("service", __name__, url_prefix="/service")

TYPES = [("onderhoud", "Onderhoud"), ("storing", "Storing"), ("inspectie", "Inspectie"), ("overig", "Overig")]
STATUSES = [("nieuw", "Nieuw"), ("ingepland", "Ingepland"), ("in_uitvoering", "In uitvoering"),
            ("wacht", "Wacht op onderdelen"), ("afgerond", "Afgerond"), ("gefactureerd", "Gefactureerd")]
PRIOS = [("laag", "Laag"), ("normaal", "Normaal"), ("hoog", "Hoog")]
OPEN = ("nieuw", "ingepland", "in_uitvoering", "wacht")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


def _ticket(tid):
    t = query(
        "SELECT t.*, c.name AS customer, c.email AS customer_email, l.name AS location, i.name AS installation,"
        " i.serial AS serial, p.number AS project_no, p.name AS project_name, p.sharepoint_path,"
        " ct.name AS contact, ct.phone AS contact_phone, ct.email AS contact_email, u.name AS assignee"
        " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id LEFT JOIN locations l ON l.id = t.location_id"
        " LEFT JOIN installations i ON i.id = t.installation_id LEFT JOIN projects p ON p.id = t.project_id"
        " LEFT JOIN contacts ct ON ct.id = t.contact_id LEFT JOIN users u ON u.id = t.assigned_to WHERE t.id = ?",
        (tid,), one=True)
    if not t:
        abort(404)
    return t


def _lookups(customer_id=None):
    return dict(
        customers=customer_options(customer_id),
        locations=query("SELECT id, customer_id, name FROM locations ORDER BY name"),
        installations=query("SELECT id, customer_id, name, serial FROM installations ORDER BY name"),
        contacts=query("SELECT id, customer_id, name FROM contacts ORDER BY name"),
        projects=query("SELECT id, customer_id, number, name FROM projects WHERE status = 'actief' OR customer_id = ? ORDER BY number DESC",
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
    elif view == "factureren":
        where.append("t.status = 'afgerond'")
    if typ:
        where.append("t.type = ?")
        params.append(typ)
    if q:
        where.append("(t.number LIKE ? OR t.title LIKE ? OR IFNULL(c.name,'') LIKE ?)")
        params += [f"%{q}%"] * 3
    sql = ("SELECT t.*, c.name AS customer, i.name AS installation, u.name AS assignee FROM tickets t"
           " LEFT JOIN customers c ON c.id = t.customer_id LEFT JOIN installations i ON i.id = t.installation_id"
           " LEFT JOIN users u ON u.id = t.assigned_to")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE t.priority WHEN 'hoog' THEN 0 WHEN 'normaal' THEN 1 ELSE 2 END, t.updated_at DESC LIMIT 300"
    tickets = query(sql, params)
    due = query("SELECT i.*, c.name AS customer FROM installations i JOIN customers c ON c.id = i.customer_id"
                " WHERE i.next_service IS NOT NULL AND i.next_service <= date('now', '+30 days') ORDER BY i.next_service LIMIT 20")
    return render_template("service/index.html", tickets=tickets, view=view, typ=typ, q=q, TYPES=TYPES, due=due)


@bp.route("/nieuw", methods=["GET", "POST"])
@require("service", BEWERKEN)
def new():
    if request.method == "POST":
        if not _f("title"):
            flash("Vul een korte omschrijving in.", "error")
            return render_template("service/form.html", t=request.form, **_lookups(to_int(request.form.get("customer_id"))))
        now = now_iso()
        number = next_number("tickets", "T")
        status = _f("status") or ("ingepland" if _f("planned_date") else "nieuw")
        tid = execute(
            "INSERT INTO tickets (number, type, priority, status, title, description, customer_id, location_id, installation_id,"
            " project_id, contact_id, reported_by, assigned_to, planned_date, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, _f("type") or "storing", _f("priority") or "normaal", status, _f("title"), _f("description"),
             to_int(request.form.get("customer_id")), to_int(request.form.get("location_id")),
             to_int(request.form.get("installation_id")), to_int(request.form.get("project_id")),
             to_int(request.form.get("contact_id")), _f("reported_by"), to_int(request.form.get("assigned_to")),
             _f("planned_date"), g.user["id"], now, now))
        save_uploads("ticket", tid)
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
    history = query("SELECT a.*, u.name AS who FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
                    " WHERE a.entity = 'ticket' AND a.entity_id = ? ORDER BY a.id DESC LIMIT 30", (tid,))
    return render_template("service/detail.html", t=t, visits=visits, vfiles=vfiles, files=files_for("ticket", tid),
                           history=history, STATUSES=STATUSES, TYPES=dict(TYPES))


@bp.route("/<int:tid>/bewerken", methods=["GET", "POST"])
@require("service", BEWERKEN)
def edit(tid):
    t = _ticket(tid)
    if request.method == "POST":
        if not _f("title"):
            flash("Vul een korte omschrijving in.", "error")
        else:
            execute("UPDATE tickets SET type=?, priority=?, status=?, title=?, description=?, customer_id=?, location_id=?,"
                    " installation_id=?, project_id=?, contact_id=?, reported_by=?, assigned_to=?, planned_date=?, updated_at=?"
                    " WHERE id=?",
                    (_f("type") or t["type"], _f("priority") or "normaal", _f("status") or t["status"], _f("title"),
                     _f("description"), to_int(request.form.get("customer_id")), to_int(request.form.get("location_id")),
                     to_int(request.form.get("installation_id")), to_int(request.form.get("project_id")),
                     to_int(request.form.get("contact_id")), _f("reported_by"), to_int(request.form.get("assigned_to")),
                     _f("planned_date"), now_iso(), tid))
            save_uploads("ticket", tid)
            audit("gewijzigd", "ticket", tid)
            flash("Ticket opgeslagen.", "ok")
            target = url_for("service.detail", tid=tid)
            return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    return render_template("service/form.html", t=t, edit=True, **_lookups(t["customer_id"]))


@bp.route("/<int:tid>/status", methods=["POST"])
@require("service", BEWERKEN)
def set_status(tid):
    t = _ticket(tid)
    status = request.form.get("status")
    if status not in dict(STATUSES):
        abort(400)
    closed = now_iso() if status in ("afgerond", "gefactureerd") and not t["closed_at"] else t["closed_at"]
    if status in OPEN:
        closed = None
    execute("UPDATE tickets SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?", (status, closed, now_iso(), tid))
    audit("status", "ticket", tid, dict(STATUSES)[status])
    flash(f"Status gewijzigd naar {dict(STATUSES)[status]}.", "ok")
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


# ---------------------------------------------------------------- bezoeken

@bp.route("/<int:tid>/bezoek", methods=["GET", "POST"])
@bp.route("/<int:tid>/bezoek/<int:vid>", methods=["GET", "POST"])
@require("service", BEWERKEN)
def visit(tid, vid=None):
    t = _ticket(tid)
    v = query("SELECT * FROM visits WHERE id = ? AND ticket_id = ?", (vid, tid), one=True) if vid else None
    if vid and not v:
        abort(404)
    if v and v["signed_at"]:
        flash("Dit bezoek is ondertekend en kan niet meer worden gewijzigd.", "info")
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
        execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_iso(), tid))
        audit("bezoek opgeslagen", "ticket", tid, vals[0])
        if request.form.get("next") == "tekenen":
            return redirect(url_for("service.sign", tid=tid, vid=vid))
        flash("Bezoek opgeslagen.", "ok")
        return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")
    v = v or {"date": local(now_utc()).strftime("%Y-%m-%d"), "technicians": g.user["name"]}
    return render_template("service/visit.html", t=t, v=v, vid=vid, files=files_for("visit", vid) if vid else [])


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
                           f"Beste {name},\n\nIn de bijlage vindt u de werkbon van ons bezoek.\n\n"
                           f"Met vriendelijke groet,\n{g.user['name']}\nDe Vreugd Productietechniek",
                           attachments=[(fname, pdf, "application/pdf")])
            msgs.append("Werkbon gemaild naar " + to + "." if ok else "Mailen is mislukt.")
        if request.form.get("sharepoint") and sharepoint_configured():
            folder = _sp_folder(t, "Service")
            ok, msg = upload_to_sharepoint(pdf, fname, folder, t["project_no"] or "", "werkbon")
            execute("UPDATE visits SET sharepoint_status = ? WHERE id = ?", (msg, vid))
            msgs.append(msg + ".")
        flash(" ".join(msgs), "ok")
        return redirect(url_for("service.detail", tid=tid) + f"#bezoek{vid}")
    return render_template("service/sign.html", t=t, v=v, mail_to=t["contact_email"] or t["customer_email"] or "",
                           sharepoint=sharepoint_configured())


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
