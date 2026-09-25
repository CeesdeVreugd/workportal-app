from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from .db import query, execute
from .permissions import require, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, files_for, save_uploads, to_int

bp = Blueprint("klanten", __name__, url_prefix="/klanten")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


@bp.route("/")
@require("klanten", LEZEN)
def index():
    q = (request.args.get("q") or "").strip()
    tab = request.args.get("tab", "klanten")
    like = f"%{q}%"
    customers = query(
        "SELECT c.*, (SELECT COUNT(*) FROM projects p WHERE p.customer_id = c.id) AS n_projects,"
        " (SELECT COUNT(*) FROM installations i WHERE i.customer_id = c.id) AS n_inst,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.customer_id = c.id AND t.status NOT IN ('afgerond','gefactureerd')) AS n_open"
        " FROM customers c WHERE c.name LIKE ? OR IFNULL(c.city,'') LIKE ? OR IFNULL(c.debtor_no,'') LIKE ? ORDER BY c.name",
        (like, like, like))
    projects = query(
        "SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id"
        " WHERE p.number LIKE ? OR p.name LIKE ? OR IFNULL(c.name,'') LIKE ?"
        " ORDER BY CASE p.status WHEN 'actief' THEN 0 ELSE 1 END, p.number DESC", (like, like, like))
    return render_template("klanten/index.html", customers=customers, projects=projects, q=q, tab=tab)


@bp.route("/nieuw", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def new():
    if request.method == "POST":
        if not _f("name"):
            flash("Vul een klantnaam in.", "error")
            return render_template("klanten/customer_form.html", c=request.form)
        cid = execute(
            "INSERT INTO customers (name, debtor_no, address, postcode, city, phone, email, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_f("name"), _f("debtor_no"), _f("address"), _f("postcode"), _f("city"), _f("phone"), _f("email"),
             _f("notes"), now_iso()))
        audit("aangemaakt", "customer", cid, _f("name"))
        flash("Klant aangemaakt.", "ok")
        return redirect(url_for("klanten.customer", cid=cid))
    return render_template("klanten/customer_form.html", c={})


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
                           installations=installations, projects=projects, tickets=tickets)


@bp.route("/<int:cid>/bewerken", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def edit(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    if request.method == "POST":
        if not _f("name"):
            flash("Vul een klantnaam in.", "error")
            return render_template("klanten/customer_form.html", c=request.form, edit=True, cid=cid)
        execute("UPDATE customers SET name=?, debtor_no=?, address=?, postcode=?, city=?, phone=?, email=?, notes=? WHERE id=?",
                (_f("name"), _f("debtor_no"), _f("address"), _f("postcode"), _f("city"), _f("phone"), _f("email"),
                 _f("notes"), cid))
        audit("gewijzigd", "customer", cid)
        flash("Klant opgeslagen.", "ok")
        return redirect(url_for("klanten.customer", cid=cid))
    return render_template("klanten/customer_form.html", c=c, edit=True, cid=cid)


@bp.route("/<int:cid>/verwijderen", methods=["POST"])
@require("klanten", BEHEER)
def delete(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    execute("DELETE FROM customers WHERE id = ?", (cid,))
    audit("verwijderd", "customer", cid, c["name"])
    flash(f"Klant {c['name']} verwijderd.", "ok")
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
    customers = query("SELECT id, name FROM customers ORDER BY name")
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
        if nxt and nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("klanten.project", pid=pid))
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
    customers = query("SELECT id, name FROM customers ORDER BY name")
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
    return redirect(url_for("klanten.index", tab="projecten"))
