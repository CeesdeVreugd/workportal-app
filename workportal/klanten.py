from urllib.parse import quote

import requests
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response, stream_with_context

from . import relaties
from . import sharepoint as sp
from .db import query, execute, get_db
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .relaties import RELATION_TYPES, TYPE_LABELS
from .util import now_iso, audit, files_for, save_uploads, to_int

bp = Blueprint("klanten", __name__, url_prefix="/klanten")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


CUSTOMER_FIELDS = ["name", "short_name", "debtor_no", "relation_type", "relation_group", "address", "postcode",
                   "city", "phone", "email", "website", "notes", "alert"]


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
    sp_on = _sp_on()
    return render_template("klanten/customer.html", c=c, locations=locations, contacts=contacts,
                           installations=installations, projects=projects, tickets=tickets, TYPE_LABELS=TYPE_LABELS,
                           sp_on=sp_on, sp_folder=sp.customer_folder(get_db(), cid) if sp_on else None)


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
    bring = "\n".join(x.strip() for x in (request.form.get("bring") or "").replace(",", "\n").splitlines() if x.strip()) or None
    vals = (_f("name"), _f("serial"), _f("year"), to_int(request.form.get("location_id")),
            to_int(request.form.get("project_id")), to_int(request.form.get("service_interval_months")),
            _f("next_service"), _f("notes"), _f("alert"), bring)
    if not vals[0]:
        flash("Vul een naam voor de installatie in.", "error")
    elif iid:
        execute("UPDATE installations SET name=?, serial=?, year=?, location_id=?, project_id=?, service_interval_months=?,"
                " next_service=?, notes=?, alert=?, bring=? WHERE id=? AND customer_id=?", vals + (iid, cid))
        flash("Installatie opgeslagen.", "ok")
    else:
        iid = execute("INSERT INTO installations (name, serial, year, location_id, project_id, service_interval_months,"
                      " next_service, notes, alert, bring, customer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals + (cid,))
    if iid:
        save_uploads("installation", iid, field="files")
    if request.form.get("back") == "installatie" and iid:
        return redirect(url_for("klanten.installation", iid=iid))
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
    i = query("SELECT i.*, c.name AS customer, c.alert AS customer_alert, l.name AS location, p.number AS project_no, p.name AS project_name"
              " FROM installations i JOIN customers c ON c.id = i.customer_id LEFT JOIN locations l ON l.id = i.location_id"
              " LEFT JOIN projects p ON p.id = i.project_id WHERE i.id = ?", (iid,), one=True) or abort(404)
    tickets = query("SELECT * FROM tickets WHERE installation_id = ? ORDER BY created_at DESC", (iid,))
    visits = query("SELECT v.*, t.number, t.title FROM visits v JOIN tickets t ON t.id = v.ticket_id"
                   " WHERE t.installation_id = ? ORDER BY v.date DESC", (iid,))
    locations = query("SELECT * FROM locations WHERE customer_id = ? ORDER BY name", (i["customer_id"],))
    projects = query("SELECT id, number, name FROM projects WHERE customer_id = ? OR id = ? ORDER BY number DESC",
                     (i["customer_id"], i["project_id"] or 0))
    return render_template("klanten/installation.html", i=i, tickets=tickets, visits=visits, locations=locations,
                           projects=projects, files=files_for("installation", iid), edit=request.args.get("bewerken"))


# ---------------------------------------------------------------- projecten

def _sp_on():
    return sp.connected(get_db())


def _sp_try(fn, *args):
    """Voert een SharePoint-actie uit en vertaalt fouten naar een nette melding."""
    try:
        return fn(get_db(), *args)
    except sp.GraphError as exc:
        return False, f"SharePoint gaf een fout ({exc.status}): {exc.message}"
    except (requests.RequestException, RuntimeError) as exc:
        return False, f"SharePoint niet bereikbaar: {exc}"


def _make_folder(pid, next_url):
    """Maakt de projectmap aan; stuurt door naar 'klantmap kiezen' als de klant nog geen map heeft."""
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True)
    if not p["customer_id"]:
        flash("Geen klant gekozen, dus geen projectmap in SharePoint aangemaakt.", "error")
        return None
    ok, msg = _sp_try(sp.create_project_folder, pid)
    if msg == "nofolder":
        return redirect(url_for("klanten.klantmap", cid=p["customer_id"], project=pid, next=next_url))
    flash(msg, "ok" if ok else "error")
    if ok:
        audit("projectmap", "project", pid, msg)
    return None


@bp.route("/projecten/nieuw", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def new_project():
    customers = customer_options(to_int(request.values.get("customer_id") or request.args.get("klant")))
    sp_on = _sp_on()
    if request.method == "POST":
        make = sp_on and request.form.get("sp_create")
        err = None
        if not _f("number") or not _f("name"):
            err = "Vul projectnummer en omschrijving in."
        elif query("SELECT 1 FROM projects WHERE number = ?", (_f("number"),), one=True):
            err = "Dit projectnummer bestaat al."
        elif make and not sp.NUMBER_RE.match(_f("number")):
            err = "Voor een projectmap in SharePoint moet het projectnummer het ordernummer van 8 cijfers zijn (bijv. 20260138)."
        elif make and not to_int(request.form.get("customer_id")):
            err = "Kies een klant: de projectmap komt in de map van die klant."
        if err:
            flash(err, "error")
            return render_template("klanten/project_form.html", p=request.form, customers=customers, sp_on=sp_on,
                                   next=request.form.get("next", ""))
        pid = execute("INSERT INTO projects (number, name, customer_id, status, sharepoint_path, notes, created_at, source)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                       None if make else _f("sharepoint_path"), _f("notes"), now_iso(), "workportal"))
        audit("aangemaakt", "project", pid, _f("number"))
        flash("Project aangemaakt.", "ok")
        nxt = request.form.get("next")
        target = nxt if nxt and nxt.startswith("/") else url_for("klanten.project", pid=pid)
        if make:
            r = _make_folder(pid, target)
            if r:
                return r
        return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    return render_template("klanten/project_form.html", customers=customers, sp_on=sp_on,
                           p={"customer_id": request.args.get("klant"), "status": "actief", "sp_create": "1"},
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
    sp_on = _sp_on()
    folder = sp.customer_folder(get_db(), p["customer_id"]) if sp_on else None
    return render_template("klanten/project.html", p=p, tickets=tickets, tests=tests, calcs=calcs, nacalcs=nacalcs,
                           installations=installations, sp_on=sp_on, folder=folder,
                           folder_name=sp.folder_name(p["number"], p["name"]))


@bp.route("/projecten/<int:pid>/bewerken", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def edit_project(pid):
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    customers = customer_options(p["customer_id"])
    sp_on = _sp_on()
    if request.method == "POST":
        if not _f("number") or not _f("name"):
            flash("Vul projectnummer en omschrijving in.", "error")
        elif query("SELECT 1 FROM projects WHERE number = ? AND id <> ?", (_f("number"), pid), one=True):
            flash("Dit projectnummer bestaat al.", "error")
        else:
            execute("UPDATE projects SET number=?, name=?, customer_id=?, status=?, sharepoint_path=?, notes=? WHERE id=?",
                    (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                     _f("sharepoint_path") if not p["sp_item_id"] else p["sharepoint_path"], _f("notes"), pid))
            audit("gewijzigd", "project", pid)
            flash("Project opgeslagen.", "ok")
            target = url_for("klanten.project", pid=pid)
            if sp_on and p["sp_item_id"] and not p["sp_missing"] and (_f("number"), _f("name")) != (p["number"], p["name"]):
                try:
                    msg = sp.rename_project_folder(get_db(), pid)
                except (requests.RequestException, RuntimeError) as exc:
                    msg = f"Map in SharePoint niet hernoemd: {exc}"
                if msg:
                    flash(msg, "error" if "niet" in msg else "ok")
            elif sp_on and not p["sp_item_id"] and request.form.get("sp_create"):
                r = _make_folder(pid, target)
                if r:
                    return r
            return redirect(target)
    return render_template("klanten/project_form.html", p=p, customers=customers, edit=True, sp_on=sp_on)


@bp.route("/projecten/<int:pid>/map-aanmaken", methods=["POST"])
@require("klanten", BEWERKEN)
def make_project_folder(pid):
    query("SELECT id FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    target = url_for("klanten.project", pid=pid)
    if not _sp_on():
        flash("SharePoint is niet gekoppeld.", "error")
        return redirect(target)
    return _make_folder(pid, target) or redirect(target)


def _can_see_folder():
    if not (can("klanten") or can("service") or can("druktest")):
        abort(403)


def _project_sp(pid):
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    if not p["sp_item_id"] or not _sp_on():
        abort(404)
    return p


@bp.route("/projecten/<int:pid>/map")
def project_folder(pid):
    _can_see_folder()
    p = _project_sp(pid)
    try:
        rel = sp.safe_rel(request.args.get("pad", ""))
    except ValueError:
        abort(400)
    error, items = None, []
    try:
        items = sp.list_folder(get_db(), pid, rel)
    except sp.GraphError as exc:
        error = "Deze map bestaat niet (meer) in SharePoint." if exc.status == 404 else f"SharePoint gaf een fout ({exc.status})."
    except (requests.RequestException, RuntimeError):
        error = "SharePoint is op dit moment niet bereikbaar."
    parts = rel.split("/") if rel else []
    crumbs = [("/".join(parts[:i + 1]), parts[i]) for i in range(len(parts))]
    tpl = "klanten/_folder_list.html" if request.args.get("partial") else "klanten/project_folder.html"
    return render_template(tpl, p=p, items=items, rel=rel, crumbs=crumbs, error=error,
                           folder_name=sp.folder_name(p["number"], p["sp_name"] or p["name"]))


@bp.route("/projecten/<int:pid>/bestand")
def project_file(pid):
    _can_see_folder()
    _project_sp(pid)
    rel = request.args.get("pad", "")
    try:
        meta, r = sp.open_file(get_db(), pid, rel)
    except ValueError:
        abort(400)
    except sp.GraphError as exc:
        abort(404 if exc.status == 404 else 502)
    name = meta.get("name") or "bestand"
    mime = (meta.get("file") or {}).get("mimeType") or "application/octet-stream"
    inline = request.args.get("download") != "1" and (mime.startswith("image/") or mime in ("application/pdf", "text/plain"))
    disp = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(name)}", "Cache-Control": "private, max-age=300"}
    if meta.get("size"):
        headers["Content-Length"] = str(meta["size"])
    return Response(stream_with_context(r.iter_content(64 * 1024)), mimetype=mime, headers=headers)


@bp.route("/projecten/<int:pid>/verwijderen", methods=["POST"])
@require("klanten", BEHEER)
def delete_project(pid):
    execute("DELETE FROM projects WHERE id = ?", (pid,))
    audit("verwijderd", "project", pid)
    flash("Project verwijderd. De map in SharePoint is niet aangeraakt.", "ok")
    return redirect(url_for("klanten.index", view="projecten"))


# ---------------------------------------------------------------- klantmap in SharePoint

@bp.route("/<int:cid>/klantmap", methods=["GET", "POST"])
@require("klanten", BEWERKEN)
def klantmap(cid):
    c = query("SELECT * FROM customers WHERE id = ?", (cid,), one=True) or abort(404)
    if not _sp_on():
        flash("SharePoint is niet gekoppeld.", "error")
        return redirect(url_for("klanten.customer", cid=cid))
    pid = to_int(request.values.get("project"))
    nxt = request.values.get("next") or (url_for("klanten.project", pid=pid) if pid else url_for("klanten.customer", cid=cid))
    if not nxt.startswith("/"):
        nxt = url_for("klanten.customer", cid=cid)
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "overslaan":
            if pid:
                flash("Projectmap niet aangemaakt. Dat kan later alsnog via de projectpagina.", "info")
            return ask_snelstart(cid, nxt) or redirect(nxt)
        try:
            if action == "nieuw":
                item = sp.create_customer_folder(conn, cid)
                msg = f"Klantmap '{item['name']}' aangemaakt en gekoppeld."
            else:
                fid = request.form.get("folder_id")
                f = query("SELECT * FROM sp_folders WHERE item_id = ?", (fid,), one=True)
                if not f:
                    flash("Kies een klantmap.", "error")
                    return redirect(request.full_path)
                sp.link_folder(conn, fid, cid)
                msg = f"Klantmap '{f['name']}' gekoppeld aan {c['name']}."
            audit("klantmap", "customer", cid, msg)
            flash(msg, "ok")
        except sp.GraphError as exc:
            flash(f"SharePoint gaf een fout ({exc.status}): {exc.message}", "error")
            return redirect(request.full_path)
        except (requests.RequestException, RuntimeError) as exc:
            flash(f"SharePoint niet bereikbaar: {exc}", "error")
            return redirect(request.full_path)
        if pid:
            r = _make_folder(pid, nxt)
            if r:
                return r
            return ask_snelstart(cid, nxt) or redirect(nxt)
        return redirect(nxt)
    current = sp.customer_folder(conn, cid)
    return render_template("klanten/klantmap.html", c=c, pid=pid, next=nxt, current=current,
                           suggestions=sp.suggestions(conn, cid),
                           folders=query("SELECT * FROM sp_folders WHERE missing = 0 AND customer_id IS NULL ORDER BY name COLLATE NOCASE"),
                           project=query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) if pid else None)


@bp.route("/<int:cid>/klantmap/ontkoppelen", methods=["POST"])
@require("klanten", BEHEER)
def klantmap_unlink(cid):
    execute("UPDATE sp_folders SET customer_id = NULL, auto = 0 WHERE customer_id = ?", (cid,))
    flash("Klantmap ontkoppeld.", "ok")
    return redirect(url_for("klanten.customer", cid=cid))


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
