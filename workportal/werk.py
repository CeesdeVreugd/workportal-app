"""Projecten en Orders: twee modules op dezelfde tabel (projects.kind = 'project' | 'order').

De blueprint wordt twee keer geregistreerd: als 'projecten' (/projecten) en als 'orders' (/orders).
Een ordernummer dat geen project is, is een order. Nieuwe ordermappen uit Komdex komen binnen in
het actievenster (/orders/inbox) en worden daar als project of order goedgezet.
"""
import os
import re
from urllib.parse import quote

import requests
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, Response,
                   stream_with_context, g, jsonify)

from . import sharepoint as sp
from .db import query, execute, get_db
from .permissions import can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, to_int, get_setting

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
        # project: direct een projectmap; order: pas een map als iemand op 'Ordermap aanmaken' klikt
        make = on and k["kind"] == "project"
        err = None
        if not _f("number") or not _f("name"):
            err = "Vul ordernummer en omschrijving in."
        elif query("SELECT 1 FROM projects WHERE number = ?", (_f("number"),), one=True):
            err = "Dit ordernummer bestaat al (als project of order)."
        elif make and not sp.NUMBER_RE.match(_f("number")):
            err = "Het ordernummer moet uit 8 cijfers bestaan (bijv. 20260138), zodat de projectmap goed wordt aangemaakt."
        elif make and not to_int(request.form.get("customer_id")):
            err = "Kies een klant: de projectmap komt in de klantmap van die klant."
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
                           p={"customer_id": request.args.get("klant"), "status": "actief"},
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
    notes = query("SELECT n.*, u.name AS who FROM project_notes n LEFT JOIN users u ON u.id = n.created_by"
                  " WHERE n.project_id = ? ORDER BY n.date DESC, n.id DESC", (pid,))
    contacts = query("SELECT name FROM contacts WHERE customer_id = ? ORDER BY name", (p["customer_id"] or 0,))
    from .kennis import calcs_for
    from .aantekeningen import annotations_for, STATUS_LABEL
    return render_template("werk/detail.html", K=k, B=request.blueprint, p=p, tickets=tickets, tests=tests, calcs=calcs,
                           annots=annotations_for(pid), ANNOT_STATUS=STATUS_LABEL,
                           toolcalcs=calcs_for(project_id=p["id"]),
                           nacalcs=nacalcs, sp_on=on, folder=folder, notes=notes, contacts=contacts,
                           NOTE_KINDS=NOTE_KINDS, can_note=_can_note(p), today=now_iso()[:10],
                           folder_name=sp.folder_name(p["number"], p["name"], p["kind"]), can_print=_can_print(),
                           oprops=__import__("workportal.komdex", fromlist=["order_props"]).order_props(p))


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
            elif on and not p["sp_item_id"] and p["kind"] == "project" and sp.NUMBER_RE.match(_f("number") or "") \
                    and to_int(request.form.get("customer_id")):
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


NOTE_KINDS = [("telefoon", "Telefoon"), ("bezoek", "Bezoek"), ("mail", "E-mail"), ("overleg", "Overleg intern"),
              ("teams", "Teams / online"), ("anders", "Anders")]


def _can_note(p):
    return can(KINDS[BP_OF_KIND[p["kind"]]]["module"], BEWERKEN)


@bp.route("/<int:pid>/notitie", methods=["POST"])
@bp.route("/<int:pid>/notitie/<int:nid>", methods=["POST"])
def note_save(pid, nid=None):
    p = _row(pid)
    if not _can_note(p):
        abort(403)
    target = werk_url("detail", p, pid=pid) + "#notities"
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Schrijf eerst wat er besproken is.", "error")
        return redirect(target)
    kind = request.form.get("kind") if request.form.get("kind") in dict(NOTE_KINDS) else "anders"
    date = _f("date") or now_iso()[:10]
    vals = (date, kind, _f("contact"), body, _f("follow_up"))
    if nid:
        n = query("SELECT * FROM project_notes WHERE id = ? AND project_id = ?", (nid, pid), one=True) or abort(404)
        if n["created_by"] != g.user["id"] and not can(KINDS[BP_OF_KIND[p["kind"]]]["module"], BEHEER):
            abort(403)
        execute("UPDATE project_notes SET date = ?, kind = ?, contact = ?, body = ?, follow_up = ?, updated_at = ? WHERE id = ?",
                vals + (now_iso(), nid))
        flash("Gespreksnotitie bijgewerkt.", "ok")
    else:
        execute("INSERT INTO project_notes (date, kind, contact, body, follow_up, project_id, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)", vals + (pid, g.user["id"], now_iso()))
        audit("gespreksnotitie", "project", pid, (kind + ": " + body)[:120])
        flash("Gespreksnotitie opgeslagen.", "ok")
    return redirect(target)


@bp.route("/<int:pid>/notitie/<int:nid>/actie", methods=["POST"])
def note_done(pid, nid):
    p = _row(pid)
    if not _can_note(p):
        abort(403)
    execute("UPDATE project_notes SET follow_done = 1 - follow_done WHERE id = ? AND project_id = ?", (nid, pid))
    return redirect(werk_url("detail", p, pid=pid) + "#notities")


@bp.route("/<int:pid>/notitie/<int:nid>/verwijderen", methods=["POST"])
def note_delete(pid, nid):
    p = _row(pid)
    n = query("SELECT * FROM project_notes WHERE id = ? AND project_id = ?", (nid, pid), one=True) or abort(404)
    if not (_can_note(p) and (n["created_by"] == g.user["id"] or can(KINDS[BP_OF_KIND[p["kind"]]]["module"], BEHEER))):
        abort(403)
    execute("DELETE FROM project_notes WHERE id = ?", (nid,))
    flash("Gespreksnotitie verwijderd.", "ok")
    return redirect(werk_url("detail", p, pid=pid) + "#notities")


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
    target = werk_url("detail", new_kind, pid=pid)
    if p["sp_item_id"] and not p["sp_missing"] and sp_on():
        _rename(pid)
    elif new_kind == "project" and sp_on() and p["customer_id"] and sp.NUMBER_RE.match(p["number"] or ""):
        r = make_folder(pid, target)
        if r:
            return r
    return redirect(target)


@bp.route("/omzetten", methods=["POST"])
def bulk_convert():
    """Meerdere projecten in één keer omzetten naar order (of orders naar project)."""
    k = K()
    new_kind = "order" if k["kind"] == "project" else "project"
    _need(BEWERKEN, "projecten")
    _need(BEWERKEN, "orders")
    ids = [to_int(x) for x in request.form.getlist("ids") if to_int(x)]
    back = url_for(f"{request.blueprint}.index", filter=request.form.get("filter"), q=request.form.get("q") or None)
    if not ids:
        flash("Vink eerst aan wat je wilt omzetten.", "error")
        return redirect(back)
    rows = query(f"SELECT * FROM projects WHERE kind = ? AND id IN ({','.join('?' * len(ids))})", [k["kind"]] + ids)
    conn, on = get_db(), sp_on()
    renamed = made = failed = 0
    for p in rows:
        execute("UPDATE projects SET kind = ? WHERE id = ?", (new_kind, p["id"]))
        audit("omgezet", "project", p["id"], f"naar {new_kind} (bulk)")
        if not on:
            continue
        try:
            if p["sp_item_id"] and not p["sp_missing"]:
                msg = sp.rename_project_folder(conn, p["id"])
                if msg and "niet" in msg:
                    failed += 1
                elif msg:
                    renamed += 1
            elif new_kind == "project" and p["customer_id"] and sp.NUMBER_RE.match(p["number"] or "") \
                    and sp.customer_folder(conn, p["customer_id"]):
                ok, _ = sp.create_project_folder(conn, p["id"])
                made += 1 if ok else 0
                failed += 0 if ok else 1
        except (sp.GraphError, requests.RequestException, RuntimeError):
            failed += 1
    msg = f"{len(rows)} omgezet naar {new_kind}."
    if renamed:
        msg += f" {renamed} map(pen) in SharePoint hernoemd."
    if made:
        msg += f" {made} projectmap(pen) aangemaakt."
    if failed:
        msg += f" Bij {failed} lukte de actie in SharePoint niet; open die om het opnieuw te proberen."
    flash(msg, "ok" if not failed else "error")
    return redirect(back)


@bp.route("/<int:pid>/map-aanmaken", methods=["POST"])
def make_project_folder(pid):
    p = _row(pid)
    _need(BEWERKEN, KINDS[BP_OF_KIND[p["kind"]]]["module"])
    target = werk_url("detail", p, pid=pid)
    if not sp_on():
        flash("SharePoint is niet gekoppeld.", "error")
        return redirect(target)
    return make_folder(pid, target) or redirect(target)


def _print_opts():
    """Standaardinstellingen voor de afdrukknop (aantal, dubbelzijdig, kleur, printernaam)."""
    from . import printix
    if not _can_print():
        return None
    o = printix.options(get_db())
    o["printer"] = (printix.printer(get_db()) or {}).get("name") or ""
    return o


def _can_print():
    from . import printix
    return printix.configured() and bool(printix.printer(get_db()))


@bp.route("/<int:pid>/orderbon-printen", methods=["POST"])
def print_bon(pid):
    from flask import current_app
    from . import printix
    from .util import file_path
    p = _row(pid)
    _need(LEZEN, KINDS[BP_OF_KIND[p["kind"]]]["module"])
    target = werk_url("detail", p, pid=pid)
    f = query("SELECT * FROM files WHERE id = ?", (p["bon_file_id"],), one=True) if p["bon_file_id"] else None
    if not f:
        flash("Er is geen orderbon bij deze order.", "error")
    elif not (printix.configured() and printix.printer(get_db())):
        flash("Printen is nog niet ingesteld (Beheer > Printen).", "error")
    else:
        inbox = query("SELECT id FROM order_inbox WHERE bon_file_id = ?", (f["id"],), one=True)
        with open(file_path(f), "rb") as fh:
            printix.print_background(current_app.config["DB_PATH"], fh.read(), f"Orderbon {p['number']}",
                                     inbox["id"] if inbox else None)
        audit("afgedrukt", "project", pid, f"{p['number']} · Orderbon · via Printix")
        flash("Orderbon naar de printer gestuurd.", "ok")
    return redirect(target)


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
    photo_mode = bool(parts and PHOTO_RE.match(parts[0]))
    for i in items:
        i["is_image"] = not i["folder"] and ((i["mime"] or "").startswith("image/") or i["name"].lower().endswith(IMAGE_EXT))
    return render_template(tpl, K=k, B=BP_OF_KIND[p["kind"]], p=p, items=items, rel=rel, crumbs=crumbs, error=error,
                           photo_mode=photo_mode, can_print=_can_print(), print_opts=_print_opts(),
                           folder_name=sp.folder_name(p["number"], p["sp_name"] or p["name"], p["kind"]),
                           can_upload=_can_upload(p), cert_root=_cert_root(rel))


# Fotomappen: '4 ...' t/m '8 ...' in de projectmap. Foto's als raster met groot beeld, en direct een foto maken.
PHOTO_RE = re.compile(r"^\s*[4-8](\D|$)")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")


@bp.route("/<int:pid>/map/miniatuur")
def project_thumb(pid):
    _project_sp(pid)
    size = "c1600x1600" if request.args.get("groot") else "c400x400"
    try:
        data, mime = sp.thumbnail(get_db(), pid, request.args.get("pad", ""), size)
    except ValueError:
        abort(400)
    except sp.GraphError as exc:
        abort(404 if exc.status == 404 else 502)
    except (requests.RequestException, RuntimeError):
        abort(502)
    return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=86400"})


PRINTABLE_EXT = (".pdf", ".jpg", ".jpeg", ".png")


@bp.route("/<int:pid>/map/afdrukken", methods=["POST"])
def project_folder_print(pid):
    """PDF (of foto) uit de projectmap afdrukken via Printix."""
    from . import printix
    p = _project_sp(pid)
    if not (printix.configured() and printix.printer(get_db())):
        return jsonify({"error": "Printen is nog niet ingesteld (Beheer > Printen)."}), 400
    try:
        rel = sp.safe_rel(request.form.get("pad", ""))
    except ValueError:
        return jsonify({"error": "Ongeldig pad"}), 400
    if not rel.lower().endswith(PRINTABLE_EXT):
        return jsonify({"error": "Alleen PDF's en foto's kunnen worden afgedrukt."}), 400
    opts = printix.options(get_db())
    opts["copies"] = max(1, min(50, to_int(request.form.get("copies"), opts["copies"]) or 1))
    if request.form.get("duplex") in ("NONE", "LONG_EDGE", "SHORT_EDGE"):
        opts["duplex"] = request.form.get("duplex")
    if "color" in request.form:
        opts["color"] = request.form.get("color") == "1"
    try:
        meta, r = sp.open_file(get_db(), pid, rel)
        data = b"".join(r.iter_content(256 * 1024))
    except sp.GraphError as exc:
        return jsonify({"error": "Dit bestand bestaat niet meer." if exc.status == 404 else f"SharePoint gaf een fout: {exc.message}"}), 502
    except (requests.RequestException, RuntimeError):
        return jsonify({"error": "SharePoint is op dit moment niet bereikbaar."}), 502
    name = meta.get("name") or rel.rpartition("/")[2]
    if not name.lower().endswith(".pdf"):  # foto -> PDF van één pagina
        import io
        from PIL import Image, ImageOps
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PDF", resolution=150.0)
            data = buf.getvalue()
        except Exception:
            return jsonify({"error": "Deze foto kan niet worden omgezet om af te drukken."}), 400
    try:
        jid = printix.print_pdf(printix.printer(get_db()), data, f"{p['number']} {name}", opts)
    except (printix.PrintixError, requests.RequestException, KeyError, ValueError) as exc:
        audit("afdrukken mislukt", "project", pid, f"{p['number']} · {rel} · {exc}"[:500])
        return jsonify({"error": f"Afdrukken mislukt: {exc}"}), 502
    pr = printix.printer(get_db()) or {}
    audit("afgedrukt", "project", pid, f"{p['number']} · {rel} · {opts['copies']}× · "
          f"{ {'NONE': 'enkelzijdig', 'LONG_EDGE': 'dubbelzijdig', 'SHORT_EDGE': 'dubbelzijdig (korte zijde)'}[opts['duplex']] } · "
          f"{'kleur' if opts['color'] else 'zwart-wit'} · {pr.get('name') or 'printer'} · job {jid}"[:500])
    return jsonify({"ok": True, "job": jid, "copies": opts["copies"]})


@bp.route("/<int:pid>/map/verwijderen", methods=["POST"])
def project_folder_delete(pid):
    """Foto verwijderen uit een fotomap (4 t/m 8). Gaat in SharePoint naar de prullenbak."""
    p = _project_sp(pid)
    if not _can_upload(p):
        return jsonify({"error": "Je hebt geen rechten om hier iets te verwijderen."}), 403
    try:
        rel = sp.safe_rel(request.form.get("pad", ""))
    except ValueError:
        return jsonify({"error": "Ongeldig pad"}), 400
    if not rel or not PHOTO_RE.match(rel.split("/")[0]) or "/" not in rel:
        return jsonify({"error": "Verwijderen kan alleen in de fotomappen (4 t/m 8)."}), 400
    try:
        name = sp.delete_file(get_db(), pid, rel)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sp.GraphError as exc:
        return jsonify({"error": "Dit bestand bestaat niet meer." if exc.status == 404 else f"SharePoint gaf een fout: {exc.message}"}), 502
    except (requests.RequestException, RuntimeError):
        return jsonify({"error": "SharePoint is op dit moment niet bereikbaar."}), 502
    audit("bestand verwijderd uit map", "project", pid, rel[:300])
    return jsonify({"ok": True, "deleted": name})


def _can_upload(p):
    return can(KINDS[BP_OF_KIND[p["kind"]]]["module"], BEWERKEN) or can("service", BEWERKEN) or can("druktest", BEWERKEN)


@bp.route("/<int:pid>/map/uploaden", methods=["POST"])
def project_folder_upload(pid):
    """Bestanden in de SharePoint-map zetten (slepen of kiezen). Eén of meer bestanden per verzoek."""
    p = _project_sp(pid)
    if not _can_upload(p):
        return jsonify({"error": "Je hebt geen rechten om hier bestanden toe te voegen."}), 403
    try:
        rel = sp.safe_rel(request.form.get("pad", ""))
    except ValueError:
        return jsonify({"error": "Ongeldige map"}), 400
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"error": "Geen bestand ontvangen"}), 400
    saved = []
    try:
        for f in files:
            saved.append(sp.upload_to_folder(get_db(), pid, rel, f.filename, f.read()))
    except sp.GraphError as exc:
        return jsonify({"error": f"SharePoint gaf een fout: {exc.message}", "saved": saved}), 502
    except (requests.RequestException, RuntimeError):
        return jsonify({"error": "SharePoint is op dit moment niet bereikbaar.", "saved": saved}), 502
    audit("bestand in map gezet", "project", pid, ", ".join(saved)[:300])
    return jsonify({"ok": True, "saved": saved})


CERT_RE = re.compile(r"certifica", re.I)


def _cert_root(rel):
    """De map met certificaten (bijv. '7 Certificaten') als rel daarin ligt, anders None."""
    parts = [x for x in (rel or "").split("/") if x]
    for i, part in enumerate(parts):
        if CERT_RE.search(part):
            return "/".join(parts[:i + 1])
    return None


def _contact_email(p):
    """E-mailadres van de contactpersoon van het project/de order (T.a.v.), anders van de klant."""
    if p["customer_id"]:
        if p["contact_name"]:
            r = query("SELECT email FROM contacts WHERE customer_id = ? AND IFNULL(email,'') <> '' AND lower(name) = lower(?)",
                      (p["customer_id"], p["contact_name"].strip()), one=True)
            if not r:
                r = query("SELECT email FROM contacts WHERE customer_id = ? AND IFNULL(email,'') <> '' AND"
                          " (lower(name) LIKE lower(?) OR lower(?) LIKE '%' || lower(name) || '%') LIMIT 1",
                          (p["customer_id"], f"%{p['contact_name'].strip()}%", p["contact_name"].strip()), one=True)
            if r:
                return r["email"]
        c = query("SELECT email FROM customers WHERE id = ?", (p["customer_id"],), one=True)
        if c and c["email"]:
            return c["email"]
    return ""


@bp.route("/<int:pid>/map/certificaten", methods=["GET", "POST"])
def certificates_mail(pid):
    """Certificaten uit de projectmap mailen: aanvinken, tekst aanpassen, naar de contactpersoon."""
    p = _project_sp(pid)
    if not _can_upload(p):
        abort(403)
    k = KINDS[BP_OF_KIND[p["kind"]]]
    B = BP_OF_KIND[p["kind"]]
    try:
        rel = sp.safe_rel(request.values.get("pad", ""))
    except ValueError:
        abort(400)
    root = _cert_root(rel) or rel
    conn = get_db()
    try:
        files = sp.list_files_deep(conn, pid, root)
    except sp.GraphError as exc:
        flash("Deze map bestaat niet (meer) in SharePoint." if exc.status == 404 else f"SharePoint gaf een fout ({exc.status}).", "error")
        return redirect(url_for(B + ".project_folder", pid=pid, pad=rel or None))
    except (requests.RequestException, RuntimeError):
        flash("SharePoint is op dit moment niet bereikbaar.", "error")
        return redirect(url_for(B + ".project_folder", pid=pid, pad=rel or None))
    sender = get_setting("cert_sender") or os.environ.get("CERT_MAIL_FROM") or os.environ.get("MAIL_FROM") or ""
    label = "order" if p["kind"] == "order" else "project"
    cust = query("SELECT name FROM customers WHERE id = ?", (p["customer_id"],), one=True) if p["customer_id"] else None
    contacts = query("SELECT name, email FROM contacts WHERE customer_id = ? AND IFNULL(email,'') <> '' ORDER BY name",
                     (p["customer_id"] or 0,))
    if request.method == "POST":
        chosen = [x for x in request.form.getlist("files") if any(f["path"] == x for f in files)]
        to = [a.strip() for a in re.split(r"[;,\s]+", request.form.get("to") or "") if a.strip()]
        cc = [a.strip() for a in re.split(r"[;,\s]+", request.form.get("cc") or "") if a.strip()]
        if request.form.get("cc_me") and g.user["email"] and g.user["email"] not in cc:
            cc.append(g.user["email"])
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        bad = [a for a in to + cc if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
        if not chosen or not to or bad or not subject:
            flash("Vink minstens één certificaat aan en vul een geldig e-mailadres en onderwerp in."
                  + (f" Ongeldig: {', '.join(bad)}" if bad else ""), "error")
            return render_template("werk/certificaten.html", K=k, B=B, p=p, files=files, root=root, rel=rel, form=request.form,
                                   chosen=set(chosen), sender=sender, contacts=contacts)
        atts, total = [], 0
        for path in chosen:
            meta, r = sp.open_file(conn, pid, path)
            data = b"".join(r.iter_content(256 * 1024))
            total += len(data)
            atts.append((meta.get("name") or path.rpartition("/")[2], data, (meta.get("file") or {}).get("mimeType") or "application/octet-stream"))
        if total > 30 * 1024 * 1024:
            flash(f"De bijlagen zijn samen {total / 1048576:.0f} MB; dat is te groot voor één mail (max. ±30 MB). Verstuur ze in delen.", "error")
            return render_template("werk/certificaten.html", K=k, B=B, p=p, files=files, root=root, rel=rel, form=request.form,
                                   chosen=set(chosen), sender=sender, contacts=contacts)
        from .mail import send_mail_with_files
        ok, err = send_mail_with_files(sender, to, subject, body, atts, cc=cc or None, signer="werkvoorbereiding")
        if not ok:
            flash(f"Versturen mislukt: {err}", "error")
            return render_template("werk/certificaten.html", K=k, B=B, p=p, files=files, root=root, rel=rel, form=request.form,
                                   chosen=set(chosen), sender=sender, contacts=contacts)
        names = [a[0] for a in atts]
        execute("INSERT INTO project_notes (date, kind, contact, body, project_id, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (now_iso()[:10], "mail", ", ".join(to), f"Certificaten gemaild: {', '.join(names)}" + (f"\n\n{body}" if body else ""),
                 pid, g.user["id"], now_iso()))
        audit("certificaten gemaild", "project", pid, f"aan {', '.join(to)}: {', '.join(names)}"[:300])
        flash(f"{len(names)} certificaat/certificaten gemaild aan {', '.join(to)}.", "ok")
        return redirect(url_for(B + ".project_folder", pid=pid, pad=rel or None))
    form = {"to": _contact_email(p), "cc_me": "1",
            "subject": f"Certificaten {label} {p['number']} – {p['name']}",
            "body": (f"Beste {p['contact_name'] or 'relatie'},\n\nBijgaand ontvangt u de certificaten voor {label} {p['number']}"
                     f" ({p['name']}).\n\nHeeft u vragen, neem dan gerust contact met ons op.")}
    return render_template("werk/certificaten.html", K=k, B=B, p=p, files=files, root=root, rel=rel, form=form,
                           chosen={f["path"] for f in files}, sender=sender, contacts=contacts)


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
    return render_template("werk/inbox.html", K=KINDS["orders"], B="orders", items=items, done=done,
                           customers=_customers(), sp_on=sp_on(), status=komdex.status(), types=komdex.order_types(conn))


@bp.route("/inbox/ordertypes", methods=["POST"])
def inbox_types():
    if request.blueprint != "orders":
        abort(404)
    _need(BEHEER, "orders")
    from .komdex import order_types, save_order_types, KINDS as TK
    conn = get_db()
    data = order_types(conn)
    for i, ot in enumerate(data):
        v = request.form.get(f"kind_{i}")
        if v in TK:
            ot["kind"] = v
    nn = _f("new_name")
    if nn and request.form.get("new_kind") in TK:
        data.append({"id": None, "name": nn, "abbr": _f("new_abbr") or "", "kind": request.form.get("new_kind")})
    save_order_types(conn, data)
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
    if kind == "project" and sp_on() and cid:
        r = make_folder(pid, back)
        if r:
            return r
    return redirect(back)


# Aantekeningen (productie-opmerkingen) hangen aan dezelfde blueprint
from . import aantekeningen  # noqa: E402,F401
