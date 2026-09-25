from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import relaties
from .db import query, execute
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .relaties import RELATION_TYPES, TYPE_LABELS
from .util import now_iso, audit, files_for, save_uploads, to_int

bp = Blueprint("klanten", __name__, url_prefix="/klanten")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


CUSTOMER_FIELDS = ["name", "short_name", "debtor_no", "relation_type", "relation_group", "address", "postcode",
                   "city", "phone", "email", "website", "notes"]


def customer_options(include_id=None):
    """Relaties voor keuzelijsten: actieve klanten (geen pure leveranciers)."""
    return query("SELECT id, name, city FROM customers WHERE (active = 1 AND IFNULL(relation_type, '') != 'leverancier')"
                 " OR id = ? ORDER BY name COLLATE NOCASE", (include_id or 0,))


def ask_snelstart(customer_id, next_url):
    """Eerste keer dat een klant wordt gebruikt zonder klantnummer SnelStart: vraag het één keer."""
    if not customer_id or not can("klanten", BEWERKEN):
        return None
    c = query("SELECT debtor_no, snelstart_asked, relation_type FROM customers WHERE id = ?", (customer_id,), one=True)
    if not c or c["debtor_no"] or c["snelstart_asked"] or c["relation_type"] == "leverancier":
        return None
    return redirect(url_for("klanten.snelstart", cid=customer_id, next=next_url))


@bp.route("/")
@require("klanten", LEZEN)
def index():
    q = (request.args.get("q") or "").strip()
    tab = request.args.get("tab")
    view = request.args.get("view") or ("projecten" if tab == "projecten" else "relaties")
    flt = request.args.get("filter") or (tab if tab in ("klanten", "leveranciers", "alle", "vervallen") else None)
    like = f"%{q}%"
    customers, projects = [], []
    if view == "projecten":
        flt = flt if flt in ("actief", "afgerond", "gearchiveerd", "alle") else "actief"
        where, params = ["(p.number LIKE ? OR p.name LIKE ? OR IFNULL(c.name,'') LIKE ?)"], [like, like, like]
        if flt != "alle":
            where.append("p.status = ?")
            params.append(flt)
        projects = query("SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id"
                         " WHERE " + " AND ".join(where) + " ORDER BY p.number DESC", params)
    else:
        view = "relaties"
        flt = flt if flt in ("klanten", "leveranciers", "alle", "vervallen") else "klanten"
        where = ["(c.name LIKE ? OR IFNULL(c.short_name,'') LIKE ? OR IFNULL(c.city,'') LIKE ? OR IFNULL(c.debtor_no,'') LIKE ?"
                 " OR IFNULL(c.komdex_id,'') = ?)"]
        params = [like, like, like, like, q]
        if flt == "klanten":
            where.append("c.active = 1 AND IFNULL(c.relation_type,'') IN ('klant','beide','instelling','prospect','')")
        elif flt == "leveranciers":
            where.append("c.active = 1 AND c.relation_type IN ('leverancier','beide')")
        elif flt == "vervallen":
            where.append("c.active = 0")
        customers = query(
            "SELECT c.*, (SELECT COUNT(*) FROM projects p WHERE p.customer_id = c.id) AS n_projects,"
            " (SELECT COUNT(*) FROM tickets t WHERE t.customer_id = c.id AND t.status NOT IN ('afgerond','gefactureerd')) AS n_open"
            " FROM customers c WHERE " + " AND ".join(where) + " ORDER BY c.name COLLATE NOCASE", params)
    counts = query("SELECT COUNT(*) AS alle, SUM(active = 1 AND IFNULL(relation_type,'') IN ('klant','beide','instelling','prospect',''))"
                   " AS klanten, SUM(active = 1 AND relation_type IN ('leverancier','beide')) AS leveranciers,"
                   " SUM(active = 0) AS vervallen FROM customers", one=True)
    pcounts = query("SELECT COUNT(*) AS alle, SUM(status = 'actief') AS actief, SUM(status = 'afgerond') AS afgerond,"
                    " SUM(status = 'gearchiveerd') AS gearchiveerd FROM projects", one=True)
    return render_template("klanten/index.html", customers=customers, projects=projects, q=q, view=view, flt=flt,
                           counts=counts, pcounts=pcounts, TYPE_LABELS=TYPE_LABELS)


def _customer_values():
    vals = {f: _f(f) for f in CUSTOMER_FIELDS}
    if vals["relation_type"] not in TYPE_LABELS:
        vals["relation_type"] = None
    return vals


@bp.route("/nieuw", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def new():
    if request.method == "POST":
        vals = _customer_values()
        if not vals["name"]:
            flash("Vul een naam in.", "error")
            return render_template("klanten/customer_form.html", c=request.form, TYPES=RELATION_TYPES)
        dup = query("SELECT id, name FROM customers WHERE lower(name) = lower(?)", (vals["name"],), one=True)
        if dup and not request.form.get("confirm_dup"):
            flash(f"Er bestaat al een relatie '{dup['name']}'. Klik nogmaals op Opslaan om toch een nieuwe aan te maken.", "error")
            return render_template("klanten/customer_form.html", c=request.form, TYPES=RELATION_TYPES, dup=dup)
        now = now_iso()
        cid = execute(f"INSERT INTO customers ({', '.join(CUSTOMER_FIELDS)}, created_at, updated_at)"
                      f" VALUES ({', '.join('?' * len(CUSTOMER_FIELDS))}, ?, ?)",
                      [vals[f] for f in CUSTOMER_FIELDS] + [now, now])
        audit("aangemaakt", "customer", cid, vals["name"])
        flash("Relatie aangemaakt.", "ok")
        return redirect(url_for("klanten.customer", cid=cid))
    return render_template("klanten/customer_form.html", c={"relation_type": "klant"}, TYPES=RELATION_TYPES)


@bp.route("/<int:cid>")
@require("klanten", LEZEN)
def customer(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    locations = query("SELECT * FROM locations WHERE customer_id = ? ORDER BY name", (cid,))
    contacts = query("SELECT ct.*, l.name AS location FROM contacts ct LEFT JOIN locations l ON l.id = ct.location_id"
                     " WHERE ct.customer_id = ? ORDER BY ct.name", (cid,))
    installations = query("SELECT i.*, l.name AS location, p.number AS project_no FROM installations i"
                          " LEFT JOIN locations l ON l.id = i.location_id LEFT JOIN projects p ON p.id = i.project_id"
                          " WHERE i.customer_id = ? ORDER BY i.name", (cid,))
    projects = query("SELECT * FROM projects WHERE customer_id = ? ORDER BY number DESC", (cid,))
    tickets = query("SELECT t.*, i.name AS installation FROM tickets t LEFT JOIN installations i ON i.id = t.installation_id"
                    " WHERE t.customer_id = ? ORDER BY t.created_at DESC LIMIT 50", (cid,))
    return render_template("klanten/customer.html", c=c, locations=locations, contacts=contacts,
                           installations=installations, projects=projects, tickets=tickets, TYPE_LABELS=TYPE_LABELS)


@bp.route("/<int:cid>/bewerken", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def edit(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    if request.method == "POST":
        vals = _customer_values()
        if not vals["name"]:
            flash("Vul een naam in.", "error")
            return render_template("klanten/customer_form.html", c=request.form, edit=True, cid=cid, TYPES=RELATION_TYPES,
                                   komdex_id=c["komdex_id"])
        active = 1 if request.form.get("active") else 0
        execute(f"UPDATE customers SET {', '.join(f + ' = ?' for f in CUSTOMER_FIELDS)}, active = ?, updated_at = ? WHERE id = ?",
                [vals[f] for f in CUSTOMER_FIELDS] + [active, now_iso(), cid])
        audit("gewijzigd", "customer", cid)
        flash("Relatie opgeslagen.", "ok")
        return redirect(url_for("klanten.customer", cid=cid))
    return render_template("klanten/customer_form.html", c=c, edit=True, cid=cid, TYPES=RELATION_TYPES,
                           komdex_id=c["komdex_id"])


@bp.route("/<int:cid>/verwijderen", methods=["POST"])
@require("klanten", BEHEER)
def delete(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    execute("DELETE FROM customers WHERE id = ?", (cid,))
    audit("verwijderd", "customer", cid, c["name"])
    flash(f"Relatie {c['name']} verwijderd.", "ok")
    return redirect(url_for("klanten.index"))


# ---------------------------------------------------------------- onderdelen van een klant

@bp.route("/<int:cid>/locatie", methods=["POST"])
@require("klanten", BEWERKEN)
def add_location(cid):
    lid = to_int(request.form.get("id"))
    if not _f("name"):
        flash("Vul een naam voor de locatie in.", "error")
    elif lid:
        execute("UPDATE locations SET name=?, address=?, postcode=?, city=? WHERE id=? AND customer_id=?",
                (_f("name"), _f("address"), _f("postcode"), _f("city"), lid, cid))
    else:
        execute("INSERT INTO locations (customer_id, name, address, postcode, city) VALUES (?,?,?,?,?)",
                (cid, _f("name"), _f("address"), _f("postcode"), _f("city")))
    return redirect(url_for("klanten.customer", cid=cid) + "#locaties")


@bp.route("/<int:cid>/contact", methods=["POST"])
@require("klanten", BEWERKEN)
def add_contact(cid):
    ctid = to_int(request.form.get("id"))
    loc = to_int(request.form.get("location_id"))
    if not _f("name"):
        flash("Vul een naam voor de contactpersoon in.", "error")
    elif ctid:
        execute("UPDATE contacts SET name=?, function=?, phone=?, email=?, location_id=? WHERE id=? AND customer_id=?",
                (_f("name"), _f("function"), _f("phone"), _f("email"), loc, ctid, cid))
    else:
        execute("INSERT INTO contacts (customer_id, location_id, name, function, phone, email) VALUES (?,?,?,?,?,?)",
                (cid, loc, _f("name"), _f("function"), _f("phone"), _f("email")))
    return redirect(url_for("klanten.customer", cid=cid) + "#contacten")


@bp.route("/<int:cid>/installatie", methods=["POST"])
@require("klanten", BEWERKEN)
def add_installation(cid):
    iid = to_int(request.form.get("id"))
    vals = (_f("name"), _f("serial"), _f("year"), to_int(request.form.get("location_id")),
            to_int(request.form.get("project_id")), to_int(request.form.get("service_interval_months")),
            _f("next_service"), _f("notes"))
    if not vals[0]:
        flash("Vul een naam voor de installatie in.", "error")
    elif iid:
        execute("UPDATE installations SET name=?, serial=?, year=?, location_id=?, project_id=?, service_interval_months=?,"
                " next_service=?, notes=? WHERE id=? AND customer_id=?", vals + (iid, cid))
    else:
        iid = execute("INSERT INTO installations (name, serial, year, location_id, project_id, service_interval_months,"
                      " next_service, notes, customer_id) VALUES (?,?,?,?,?,?,?,?,?)", vals + (cid,))
    if iid:
        save_uploads("installation", iid, field="files")
    return redirect(url_for("klanten.customer", cid=cid) + f"#inst{iid or ''}")


@bp.route("/<int:cid>/onderdeel/<kind>/<int:oid>/verwijderen", methods=["POST"])
@require("klanten", BEWERKEN)
def delete_sub(cid, kind, oid):
    table = {"locatie": "locations", "contact": "contacts", "installatie": "installations"}.get(kind) or abort(404)
    execute(f"DELETE FROM {table} WHERE id = ? AND customer_id = ?", (oid, cid))
    flash("Verwijderd.", "ok")
    return redirect(url_for("klanten.customer", cid=cid))


@bp.route("/installatie/<int:iid>")
@require("klanten", LEZEN)
def installation(iid):
    i = query("SELECT i.*, c.name AS customer, l.name AS location, p.number AS project_no, p.name AS project_name"
              " FROM installations i JOIN customers c ON c.id = i.customer_id LEFT JOIN locations l ON l.id = i.location_id"
              " LEFT JOIN projects p ON p.id = i.project_id WHERE i.id = ?", (iid,), one=True) or abort(404)
    tickets = query("SELECT * FROM tickets WHERE installation_id = ? ORDER BY created_at DESC", (iid,))
    visits = query("SELECT v.*, t.number, t.title FROM visits v JOIN tickets t ON t.id = v.ticket_id"
                   " WHERE t.installation_id = ? ORDER BY v.date DESC", (iid,))
    return render_template("klanten/installation.html", i=i, tickets=tickets, visits=visits,
                           files=files_for("installation", iid))


# ---------------------------------------------------------------- projecten

@bp.route("/projecten/nieuw", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def new_project():
    customers = customer_options(to_int(request.values.get("customer_id") or request.args.get("klant")))
    if request.method == "POST":
        if not _f("number") or not _f("name"):
            flash("Vul projectnummer en omschrijving in.", "error")
            return render_template("klanten/project_form.html", p=request.form, customers=customers)
        if query("SELECT 1 FROM projects WHERE number = ?", (_f("number"),), one=True):
            flash("Dit projectnummer bestaat al.", "error")
            return render_template("klanten/project_form.html", p=request.form, customers=customers)
        pid = execute("INSERT INTO projects (number, name, customer_id, status, sharepoint_path, notes, created_at)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                       _f("sharepoint_path"), _f("notes"), now_iso()))
        audit("aangemaakt", "project", pid, _f("number"))
        flash("Project aangemaakt.", "ok")
        nxt = request.form.get("next")
        target = nxt if nxt and nxt.startswith("/") else url_for("klanten.project", pid=pid)
        return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    return render_template("klanten/project_form.html", customers=customers,
                           p={"customer_id": request.args.get("klant"), "status": "actief"},
                           next=request.args.get("next", ""))


@bp.route("/projecten/<int:pid>")
@require("klanten", LEZEN)
def project(pid):
    p = query("SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id WHERE p.id = ?",
              (pid,), one=True) or abort(404)
    tickets = query("SELECT * FROM tickets WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    tests = query("SELECT * FROM pressure_tests WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    calcs = query("SELECT * FROM calculations WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    nacalcs = query("SELECT * FROM nacalcs WHERE project_id = ? ORDER BY imported_at DESC", (pid,))
    installations = query("SELECT * FROM installations WHERE project_id = ?", (pid,))
    return render_template("klanten/project.html", p=p, tickets=tickets, tests=tests, calcs=calcs, nacalcs=nacalcs,
                           installations=installations)


@bp.route("/projecten/<int:pid>/bewerken", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def edit_project(pid):
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    customers = customer_options(p["customer_id"])
    if request.method == "POST":
        if not _f("number") or not _f("name"):
            flash("Vul projectnummer en omschrijving in.", "error")
        else:
            execute("UPDATE projects SET number=?, name=?, customer_id=?, status=?, sharepoint_path=?, notes=? WHERE id=?",
                    (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                     _f("sharepoint_path"), _f("notes"), pid))
            audit("gewijzigd", "project", pid)
            flash("Project opgeslagen.", "ok")
            return redirect(url_for("klanten.project", pid=pid))
    return render_template("klanten/project_form.html", p=p, customers=customers, edit=True)


@bp.route("/projecten/<int:pid>/verwijderen", methods=["POST"])
@require("klanten", BEHEER)
def delete_project(pid):
    execute("DELETE FROM projects WHERE id = ?", (pid,))
    audit("verwijderd", "project", pid)
    flash("Project verwijderd.", "ok")
    return redirect(url_for("klanten.index", view="projecten"))


# ---------------------------------------------------------------- klantnummer SnelStart

@bp.route("/<int:cid>/klantnummer", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def snelstart(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    nxt = request.values.get("next") or url_for("klanten.customer", cid=cid)
    if not nxt.startswith("/"):
        nxt = url_for("klanten.customer", cid=cid)
    if request.method == "POST":
        nr = _f("debtor_no")
        if request.form.get("later") or not nr:
            execute("UPDATE customers SET snelstart_asked = 1 WHERE id = ?", (cid,))
        else:
            execute("UPDATE customers SET debtor_no = ?, snelstart_asked = 1, updated_at = ? WHERE id = ?", (nr, now_iso(), cid))
            audit("klantnummer SnelStart", "customer", cid, nr)
            flash(f"Klantnummer SnelStart {nr} opgeslagen bij {c['name']}.", "ok")
        return redirect(nxt)
    return render_template("klanten/snelstart.html", c=c, next=nxt)


# ---------------------------------------------------------------- import uit Komdex

@bp.route("/importeren", methods=["GET", "POST"])
@require("klanten", BEHEER)
def import_upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Kies de Excel-export uit Komdex.", "error")
            return redirect(url_for("klanten.import_upload"))
        try:
            records, warnings = relaties.parse_relations(f.read())
        except Exception as exc:
            flash(f"Inlezen mislukt: {exc}", "error")
            return redirect(url_for("klanten.import_upload"))
        token = relaties.store(records, warnings, f.filename)
        return redirect(url_for("klanten.import_review", token=token))
    total = query("SELECT COUNT(*) c, SUM(komdex_id IS NOT NULL) k FROM customers", one=True)
    return render_template("klanten/import.html", total=total)


@bp.route("/importeren/<token>", methods=["GET", "POST"])
@require("klanten", BEHEER)
def import_review(token):
    data = relaties.load(token)
    if not data:
        flash("Deze import is verlopen. Upload het bestand opnieuw.", "error")
        return redirect(url_for("klanten.import_upload"))
    if request.method == "POST":
        decisions = {k[4:]: v for k, v in request.form.items() if k.startswith("dec_")}
        p = relaties.plan(data["records"])
        missing = [c["rec"]["name"] for c in p["conflicts"] if not decisions.get(c["rec"]["komdex_id"])]
        if missing:
            flash(f"Maak eerst een keuze bij: {', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}", "error")
            return render_template("klanten/import_review.html", token=token, data=data, p=p, decisions=decisions,
                                   TYPE_LABELS=TYPE_LABELS, FIELD_LABELS=relaties.FIELD_LABELS)
        stats = relaties.apply(data["records"], decisions)
        relaties.discard(token)
        audit("relaties geïmporteerd", "customer", None,
              f"{data['filename']}: {stats['nieuw']} nieuw, {stats['bijgewerkt']} bijgewerkt, {stats['gekoppeld']} gekoppeld")
        msg = (f"Import klaar: {stats['nieuw']} nieuw, {stats['bijgewerkt']} bijgewerkt, "
               f"{stats['gekoppeld']} gekoppeld aan een bestaande relatie, {stats['ongewijzigd']} ongewijzigd.")
        if stats["namen"]:
            msg += f" {len(stats['namen'])} naam/namen gewijzigd."
        if stats["dubbel"]:
            msg += (" Let op: " + ", ".join(stats["dubbel"]) + " kon niet gekoppeld worden omdat die app-relatie al aan"
                    " een ander Komdex-ID is gekoppeld; toegevoegd als aparte relatie.")
        flash(msg, "ok")
        return redirect(url_for("klanten.index"))
    p = relaties.plan(data["records"])
    return render_template("klanten/import_review.html", token=token, data=data, p=p, decisions={},
                           TYPE_LABELS=TYPE_LABELS, FIELD_LABELS=relaties.FIELD_LABELS)
