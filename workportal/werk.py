"""Projecten en Orders: twee modules op dezelfde tabel (projects.kind = 'project' | 'order').

De blueprint wordt twee keer geregistreerd: als 'projecten' (/projecten) en als 'orders' (/orders).
Een ordernummer dat geen project is, is een order. Nieuwe ordermappen uit Komdex komen binnen in
het actievenster (/orders/inbox) en worden daar als project of order goedgezet.
"""
from urllib.parse import quote

import requests
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, Response,
                   stream_with_context, g)

from . import sharepoint as sp
from .db import query, execute, get_db
from .permissions import can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, to_int

bp = Blueprint("werk", __name__)

KINDS = {
    "projecten": {"kind": "project", "module": "projecten", "label": "Project", "plural": "Projecten",
                  "lower": "project", "icon": "klanten", "other": "orders"},
    "orders": {"kind": "order", "module": "orders", "label": "Order", "plural": "Orders",
               "lower": "order", "icon": "calculatie", "other": "projecten"},
}
BP_OF_KIND = {"project": "projecten", "order": "orders"}


def K():
    return KINDS.get(request.blueprint) or abort(404)


def _need(level=LEZEN, module=None):
    if not can(module or K()["module"], level):
        abort(403)


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


def werk_url(endpoint, row_or_kind, **kw):
    """url_for naar de juiste module (projecten/orders) voor een rij of soort."""
    kind = row_or_kind if isinstance(row_or_kind, str) else (row_or_kind["kind"] if row_or_kind else "project")
    return url_for(f"{BP_OF_KIND.get(kind, 'projecten')}.{endpoint}", **kw)


def _row(pid):
    p = query("SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id WHERE p.id = ?",
              (pid,), one=True) or abort(404)
    return p


def _wrong_bp(p):
    """Juiste module voor dit nummer? Anders doorsturen (oude links blijven zo werken)."""
    want = BP_OF_KIND.get(p["kind"], "projecten")
    if request.blueprint != want:
        return redirect(url_for(request.endpoint.replace(request.blueprint + ".", want + ".", 1),
                                **(request.view_args or {}), **request.args.to_dict()))
    return None


# ---------------------------------------------------------------- SharePoint-hulp

def sp_on():
    return sp.connected(get_db())


def _sp_try(fn, *args):
    try:
        return fn(get_db(), *args)
    except sp.GraphError as exc:
        return False, f"SharePoint gaf een fout ({exc.status}): {exc.message}"
    except (requests.RequestException, RuntimeError) as exc:
        return False, f"SharePoint niet bereikbaar: {exc}"


def make_folder(pid, next_url):
    """Maakt de project- of ordermap aan; stuurt door naar 'klantmap kiezen' als de klant nog geen map heeft."""
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True)
    if not p["customer_id"]:
        flash("Geen klant gekozen, dus geen map in SharePoint aangemaakt.", "error")
        return None
    ok, msg = _sp_try(sp.create_project_folder, pid)
    if msg == "nofolder":
        return redirect(url_for("klanten.klantmap", cid=p["customer_id"], project=pid, next=next_url))
    flash(msg, "ok" if ok else "error")
    if ok:
        audit("map", "project", pid, msg)
    return None


def _customers(include_id=None):
    from .klanten import customer_options
    return customer_options(include_id)


# ---------------------------------------------------------------- lijst

@bp.route("/")
def index():
    k = K()
    _need()
    q = (request.args.get("q") or "").strip()
    flt = request.args.get("filter")
    flt = flt if flt in ("actief", "afgerond", "gearchiveerd", "alle") else "actief"
    like = f"%{q}%"
    where, params = ["p.kind = ?", "(p.number LIKE ? OR p.name LIKE ? OR IFNULL(c.name,'') LIKE ?)"], [k["kind"], like, like, like]
    if flt != "alle":
        where.append("p.status = ?")
        params.append(flt)
    rows = query("SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id"
                 " WHERE " + " AND ".join(where) + " ORDER BY p.number DESC LIMIT 1000", params)
    counts = query("SELECT COUNT(*) AS alle, SUM(status = 'actief') AS actief, SUM(status = 'afgerond') AS afgerond,"
                   " SUM(status = 'gearchiveerd') AS gearchiveerd FROM projects WHERE kind = ?", (k["kind"],), one=True)
    return render_template("werk/index.html", K=k, B=request.blueprint, rows=rows, q=q, flt=flt, counts=counts,
                           inbox=inbox_count() if k["kind"] == "order" else 0)


# ---------------------------------------------------------------- nieuw / detail / bewerken

@bp.route("/nieuw", methods=["GET", "POST"])
def new():
    k = K()
    _need(BEWERKEN)
    customers = _customers(to_int(request.values.get("customer_id") or request.args.get("klant")))
    on = sp_on()
    if request.method == "POST":
        make = on and request.form.get("sp_create")
        err = None
        if not _f("number") or not _f("name"):
            err = "Vul ordernummer en omschrijving in."
        elif query("SELECT 1 FROM projects WHERE number = ?", (_f("number"),), one=True):
            err = "Dit ordernummer bestaat al (als project of order)."
        elif make and not sp.NUMBER_RE.match(_f("number")):
            err = "Voor een map in SharePoint moet het ordernummer uit 8 cijfers bestaan (bijv. 20260138)."
        elif make and not to_int(request.form.get("customer_id")):
            err = "Kies een klant: de map komt in de klantmap van die klant."
        if err:
            flash(err, "error")
            return render_template("werk/form.html", K=k, B=request.blueprint, p=request.form, customers=customers,
                                   sp_on=on, next=request.form.get("next", ""))
        pid = execute("INSERT INTO projects (number, name, customer_id, status, sharepoint_path, notes, created_at, source, kind)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                       None if make else _f("sharepoint_path"), _f("notes"), now_iso(), "workportal", k["kind"]))
        audit("aangemaakt", "project", pid, f"{k['lower']} {_f('number')}")
        flash(f"{k['label']} aangemaakt.", "ok")
        nxt = request.form.get("next")
        target = nxt if nxt and nxt.startswith("/") else url_for(f"{request.blueprint}.detail", pid=pid)
        if make:
            r = make_folder(pid, target)
            if r:
                return r
        from .klanten import ask_snelstart
        return ask_snelstart(to_int(request.form.get("customer_id")), target) or redirect(target)
    return render_template("werk/form.html", K=k, B=request.blueprint, customers=customers, sp_on=on,
                           p={"customer_id": request.args.get("klant"), "status": "actief", "sp_create": "1"},
                           next=request.args.get("next", ""))


@bp.route("/<int:pid>")
def detail(pid):
    p = _row(pid)
    r = _wrong_bp(p)
    if r:
        return r
    k = K()
    _need()
    tickets = query("SELECT * FROM tickets WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    tests = query("SELECT * FROM pressure_tests WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    calcs = query("SELECT * FROM calculations WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    nacalcs = query("SELECT * FROM nacalcs WHERE project_id = ? ORDER BY imported_at DESC", (pid,))
    on = sp_on()
    folder = sp.customer_folder(get_db(), p["customer_id"]) if on else None
    return render_template("werk/detail.html", K=k, B=request.blueprint, p=p, tickets=tickets, tests=tests, calcs=calcs,
                           nacalcs=nacalcs, sp_on=on, folder=folder,
                           folder_name=sp.folder_name(p["number"], p["name"], p["kind"]))


@bp.route("/<int:pid>/bewerken", methods=["GET", "POST"])
def edit(pid):
    p = _row(pid)
    r = _wrong_bp(p)
    if r:
        return r
    k = K()
    _need(BEWERKEN)
    customers = _customers(p["customer_id"])
    on = sp_on()
    if request.method == "POST":
        if not _f("number") or not _f("name"):
            flash("Vul ordernummer en omschrijving in.", "error")
        elif query("SELECT 1 FROM projects WHERE number = ? AND id <> ?", (_f("number"), pid), one=True):
            flash("Dit ordernummer bestaat al.", "error")
        else:
            execute("UPDATE projects SET number=?, name=?, customer_id=?, status=?, sharepoint_path=?, notes=? WHERE id=?",
                    (_f("number"), _f("name"), to_int(request.form.get("customer_id")), _f("status") or "actief",
                     _f("sharepoint_path") if not p["sp_item_id"] else p["sharepoint_path"], _f("notes"), pid))
            audit("gewijzigd", "project", pid)
            flash(f"{k['label']} opgeslagen.", "ok")
            target = url_for(f"{request.blueprint}.detail", pid=pid)
            if on and p["sp_item_id"] and not p["sp_missing"] and (_f("number"), _f("name")) != (p["number"], p["name"]):
                _rename(pid)
            elif on and not p["sp_item_id"] and request.form.get("sp_create"):
                r = make_folder(pid, target)
                if r:
                    return r
            return redirect(target)
    return render_template("werk/form.html", K=k, B=request.blueprint, p=p, customers=customers, edit=True, sp_on=on)


def _rename(pid):
    try:
        msg = sp.rename_project_folder(get_db(), pid)
    except (requests.RequestException, RuntimeError) as exc:
        msg = f"Map in SharePoint niet hernoemd: {exc}"
    if msg:
        flash(msg, "error" if "niet" in msg else "ok")


@bp.route("/<int:pid>/omzetten", methods=["POST"])
def convert(pid):
    """Project <-> order. De map in SharePoint krijgt de bijbehorende naam."""
    p = _row(pid)
    new_kind = "order" if p["kind"] == "project" else "project"
    _need(BEWERKEN, KINDS[BP_OF_KIND[p["kind"]]]["module"])
    _need(BEWERKEN, KINDS[BP_OF_KIND[new_kind]]["module"])
    execute("UPDATE projects SET kind = ? WHERE id = ?", (new_kind, pid))
    audit("omgezet", "project", pid, f"naar {new_kind}")
    flash(f"{p['number']} is nu een {new_kind}.", "ok")
    if p["sp_item_id"] and not p["sp_missing"] and sp_on():
        _rename(pid)
    return redirect(werk_url("detail", new_kind, pid=pid))


@bp.route("/<int:pid>/map-aanmaken", methods=["POST"])
def make_project_folder(pid):
    p = _row(pid)
    _need(BEWERKEN, KINDS[BP_OF_KIND[p["kind"]]]["module"])
    target = werk_url("detail", p, pid=pid)
    if not sp_on():
        flash("SharePoint is niet gekoppeld.", "error")
        return redirect(target)
    return make_folder(pid, target) or redirect(target)


@bp.route("/<int:pid>/verwijderen", methods=["POST"])
def delete(pid):
    p = _row(pid)
    _need(BEHEER, KINDS[BP_OF_KIND[p["kind"]]]["module"])
    execute("DELETE FROM projects WHERE id = ?", (pid,))
    execute("UPDATE order_inbox SET project_id = NULL, status = 'nieuw' WHERE project_id = ?", (pid,))
    audit("verwijderd", "project", pid)
    flash(f"{p['number']} verwijderd. De map in SharePoint is niet aangeraakt.", "ok")
    return redirect(werk_url("index", p))


# ---------------------------------------------------------------- map in SharePoint (alleen lezen)

def _can_see_folder(p):
    if not (can(KINDS[BP_OF_KIND[p["kind"]]]["module"]) or can("service") or can("druktest")):
        abort(403)


def _project_sp(pid):
    p = _row(pid)
    if not p["sp_item_id"] or not sp_on():
        abort(404)
    _can_see_folder(p)
    return p


@bp.route("/<int:pid>/map")
def project_folder(pid):
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
    k = KINDS[BP_OF_KIND[p["kind"]]]
    tpl = "werk/_folder_list.html" if request.args.get("partial") else "werk/folder.html"
    return render_template(tpl, K=k, B=BP_OF_KIND[p["kind"]], p=p, items=items, rel=rel, crumbs=crumbs, error=error,
                           folder_name=sp.folder_name(p["number"], p["sp_name"] or p["name"], p["kind"]))


@bp.route("/<int:pid>/bestand")
def project_file(pid):
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
    headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
               "Cache-Control": "private, max-age=300"}
    if meta.get("size"):
        headers["Content-Length"] = str(meta["size"])
    return Response(stream_with_context(r.iter_content(64 * 1024)), mimetype=mime, headers=headers)


# ---------------------------------------------------------------- actievenster: nieuwe ordermappen uit Komdex

def inbox_count():
    if not can("orders", BEWERKEN):
        return 0
    if "inbox_n" not in g:
        g.inbox_n = query("SELECT COUNT(*) c FROM order_inbox WHERE status = 'nieuw' AND missing = 0", one=True)["c"]
    return g.inbox_n


@bp.route("/inbox")
def inbox():
    if request.blueprint != "orders":
        return redirect(url_for("orders.inbox"))
    _need(BEWERKEN, "orders")
    from .komdex import bon_of, type_kind
    rows = query("SELECT i.*, c.name AS customer FROM order_inbox i LEFT JOIN customers c ON c.id = i.customer_id"
                 " WHERE i.status = 'nieuw' AND i.missing = 0 ORDER BY i.number DESC, i.first_seen DESC")
    items = []
    for r in rows:
        d = dict(r)
        d["bon"] = bon_of(r)
        d["suggest"] = type_kind(get_db(), (d["bon"] or {}).get("order_type")) or "order"
        items.append(d)
    done = query("SELECT i.*, p.kind, p.name AS pname, u.name AS who FROM order_inbox i LEFT JOIN projects p ON p.id = i.project_id"
                 " LEFT JOIN users u ON u.id = i.handled_by WHERE i.status IN ('verwerkt','genegeerd','gekoppeld','automatisch')"
                 " ORDER BY COALESCE(i.handled_at, i.first_seen) DESC LIMIT 15")
    from . import komdex
    conn = get_db()
    types = {"project": sp.setting(conn, "komdex_project_types") or "project",
             "order": sp.setting(conn, "komdex_order_types") or "order"}
    return render_template("werk/inbox.html", K=KINDS["orders"], B="orders", items=items, done=done,
                           customers=_customers(), sp_on=sp_on(), status=komdex.status(), types=types)


@bp.route("/inbox/ordertypes", methods=["POST"])
def inbox_types():
    if request.blueprint != "orders":
        abort(404)
    _need(BEHEER, "orders")
    conn = get_db()
    clean = lambda v: ",".join(t.strip() for t in (v or "").split(",") if t.strip())
    sp.set_setting(conn, "komdex_project_types", clean(request.form.get("project_types")))
    sp.set_setting(conn, "komdex_order_types", clean(request.form.get("order_types")))
    flash("Ordertypes opgeslagen.", "ok")
    return redirect(url_for("orders.inbox") + "#ordertypes")


@bp.route("/inbox/<int:iid>", methods=["POST"])
def inbox_handle(iid):
    if request.blueprint != "orders":
        abort(404)
    _need(BEWERKEN, "orders")
    it = query("SELECT * FROM order_inbox WHERE id = ?", (iid,), one=True) or abort(404)
    back = url_for("orders.inbox")
    if it["status"] != "nieuw":
        flash("Deze ordermap is al verwerkt.", "info")
        return redirect(back)
    if request.form.get("action") == "negeren":
        execute("UPDATE order_inbox SET status = 'genegeerd', handled_by = ?, handled_at = ? WHERE id = ?",
                (g.user["id"], now_iso(), iid))
        flash(f"{it['folder']} genegeerd.", "ok")
        return redirect(back)
    kind = "project" if request.form.get("kind") == "project" else "order"
    _need(BEWERKEN, KINDS[BP_OF_KIND[kind]]["module"])
    number, name, cid = _f("number"), _f("name"), to_int(request.form.get("customer_id"))
    if not number or not name:
        flash("Vul ordernummer en omschrijving in.", "error")
        return redirect(back + f"#i{iid}")
    existing = query("SELECT * FROM projects WHERE number = ?", (number,), one=True)
    if existing:
        execute("UPDATE order_inbox SET status = 'gekoppeld', project_id = ?, handled_by = ?, handled_at = ? WHERE id = ?",
                (existing["id"], g.user["id"], now_iso(), iid))
        flash(f"{number} bestond al als {existing['kind']} en is daaraan gekoppeld.", "info")
        return redirect(back)
    from .komdex import create_from_inbox, remember_type, bon_of
    pid = create_from_inbox(get_db(), it, kind, number, name, cid, g.user["id"])
    audit("aangemaakt", "project", pid, f"{kind} {number} uit Komdex")
    msg = f"{number} aangemaakt als {kind}."
    bon = bon_of(it) or {}
    if request.form.get("remember") and bon.get("order_type") and can("orders", BEHEER):
        remember_type(get_db(), bon["order_type"], kind)
        msg += f" Ordertype '{bon['order_type']}' wordt voortaan automatisch een {kind}."
    flash(msg, "ok")
    if request.form.get("sp_create") and sp_on() and cid:
        r = make_folder(pid, back)
        if r:
            return r
    return redirect(back)
