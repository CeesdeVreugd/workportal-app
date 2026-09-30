"""3D-modellen: STEP/IGES/STL/OBJ/3MF bekijken in de browser (Online3DViewer, MIT; STEP via occt-import-js).

Bronnen
- SharePoint: 3D-bestanden in de project-/ordermap, submap '1 Tekeningen' (instelbaar, ook submappen daarvan).
- Lokaal: uploads bij een project/order zonder SharePoint-map of bij een serviceticket (files.kind = 'model3d').

Rechten (module 'modellen3d'): Lezen = bekijken en meten, Bewerken = uploaden, Beheer = verwijderen.

Fase 2 (voorbereid, niet gebouwd): na een upload kan convert_to_gltf() op de server een .glb maken
(OpenCascade in de container) zodat grote samenstellingen sneller laden. De viewer toont dan de .glb.
"""
import os
import re
from urllib.parse import quote

import requests
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, Response, send_file,
                   stream_with_context, current_app, jsonify, g)

from . import sharepoint as sp
from . import convert3d
from .db import query, execute, get_db
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import audit, save_bytes, file_path, safe_filename

bp = Blueprint("modellen", __name__, url_prefix="/3d")

EXT = sp.MODEL_EXT
MAX_MB = 60
ENTITIES = {"project": "projects", "ticket": "tickets"}


def occt_installed():
    return os.path.exists(os.path.join(current_app.root_path, "static", "3d", "occt", "occt-import-js.wasm"))


def is_model(name):
    return (name or "").lower().endswith(EXT)


def _entity(entity, eid):
    table = ENTITIES.get(entity) or abort(404)
    return query(f"SELECT * FROM {table} WHERE id = ?", (eid,), one=True) or abort(404)


def _back(entity, eid):
    if entity == "ticket":
        return url_for("service.detail", tid=eid) + "#modellen"
    from .werk import werk_url
    p = query("SELECT kind FROM projects WHERE id = ?", (eid,), one=True)
    return werk_url("detail", p, pid=eid) + "#modellen"


def cache_mb():
    try:
        return int(sp.setting(get_db(), "sp_3d_cache_mb") or 10)
    except (TypeError, ValueError):
        return 10


def convert_to_gltf(file_row):
    """Na een upload: grote STEP/IGES alvast op de server omzetten naar GLB."""
    if file_row and convert3d.is_step(file_row["filename"]) and (file_row["size"] or 0) >= cache_mb() * 1048576:
        _conv_local(file_row)


def _server_convert_on():
    return convert3d.available(current_app._get_current_object())


def _conv_sp(pid, rel, version, size):
    """Zet een grote STEP uit SharePoint (op de achtergrond) om naar GLB. Geeft info voor de viewer of None."""
    if not (version and convert3d.is_step(rel) and (size or 0) >= cache_mb() * 1048576 and _server_convert_on()):
        return None
    app = current_app._get_current_object()
    db_path = app.config["DB_PATH"]

    def fetch(dest):
        from .db import raw_connection
        conn = raw_connection(db_path)
        try:
            _meta, r = sp.open_file(conn, pid, rel)
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1024 * 1024):
                    fh.write(chunk)
        finally:
            conn.close()

    key, st = convert3d.ensure(app, f"sp/{pid}/{rel}", version, rel.split("/")[-1], fetch)
    return _conv_info(key, st)


def _conv_local(f):
    if not (convert3d.is_step(f["filename"]) and (f["size"] or 0) >= cache_mb() * 1048576 and _server_convert_on()):
        return None
    src = file_path(f)
    key, st = convert3d.ensure(current_app._get_current_object(), f"lokaal/{f['id']}", str(f["size"]), f["filename"],
                               lambda dest: __import__("shutil").copyfile(src, dest))
    return _conv_info(key, st)


def _conv_info(key, st):
    return {"key": key, "state": (st or {}).get("state"), "status": url_for("modellen.conv_status", key=key),
            "glb": url_for("modellen.conv_glb", key=key)}


# ---------------------------------------------------------------- overzicht / losse viewer

@bp.route("/")
@require("modellen3d", LEZEN)
def index():
    recent = query("SELECT f.*, u.name AS who FROM files f LEFT JOIN users u ON u.id = f.created_by"
                   " WHERE f.kind = 'model3d' ORDER BY f.id DESC LIMIT 30")
    items = []
    for f in recent:
        d = dict(f)
        if f["entity"] == "project":
            p = query("SELECT number, name, kind FROM projects WHERE id = ?", (f["entity_id"],), one=True)
            d["where"] = f"{'Order' if p and p['kind'] == 'order' else 'Project'} {p['number']}" if p else "–"
        else:
            t = query("SELECT number, title FROM tickets WHERE id = ?", (f["entity_id"],), one=True)
            d["where"] = f"Ticket {t['number']}" if t else "–"
        items.append(d)
    return render_template("modellen/index.html", items=items, occt=occt_installed(), max_mb=MAX_MB, cache_mb=cache_mb())


@bp.route("/bekijk")
@require("modellen3d", LEZEN)
def view_local_file():
    """Viewer zonder upload: het bestand wordt alleen in de browser geopend."""
    return render_template("modellen/viewer.html", url=None, title="Bestand van deze computer", back=url_for("modellen.index"),
                           occt=occt_installed(), max_mb=MAX_MB)


# ---------------------------------------------------------------- lijst bij project/order/ticket

@bp.route("/lijst/<entity>/<int:eid>")
@require("modellen3d", LEZEN)
def listing(entity, eid):
    row = _entity(entity, eid)
    local = query("SELECT f.*, u.name AS who FROM files f LEFT JOIN users u ON u.id = f.created_by"
                  " WHERE f.entity = ? AND f.entity_id = ? AND f.kind = 'model3d' ORDER BY f.filename", (entity, eid))
    remote, sub, error = [], None, None
    pid = eid if entity == "project" else row["project_id"]
    prow = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) if pid else None
    if prow and prow["sp_item_id"] and not prow["sp_missing"] and sp.connected(get_db()):
        try:
            remote, sub = sp.list_models(get_db(), pid)
        except sp.GraphError as exc:
            error = f"SharePoint gaf een fout ({exc.status})."
        except (requests.RequestException, RuntimeError):
            error = "SharePoint is op dit moment niet bereikbaar."
    for m in remote:  # grote STEP-bestanden alvast op de server omzetten, dan openen ze straks direct
        try:
            _conv_sp(pid, m["path"], m.get("version"), m.get("size"))
        except Exception:  # noqa: BLE001 - de lijst mag hier nooit op stuk gaan
            pass
    project_local = []
    if entity == "ticket" and pid:
        project_local = query("SELECT * FROM files WHERE entity = 'project' AND entity_id = ? AND kind = 'model3d' ORDER BY filename",
                              (pid,))
    return render_template("modellen/_list.html", entity=entity, eid=eid, local=local, remote=remote, sub=sub, error=error,
                           pid=pid, prow=prow, project_local=project_local)


# ---------------------------------------------------------------- uploaden / verwijderen

@bp.route("/upload/<entity>/<int:eid>", methods=["POST"])
@require("modellen3d", BEWERKEN)
def upload(entity, eid):
    row = _entity(entity, eid)
    back = _back(entity, eid)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Kies een 3D-bestand (STEP, IGES, STL, OBJ of 3MF).", "error")
        return redirect(back)
    done, to_sp, errors = 0, 0, []
    conn = get_db()
    use_sp = (entity == "project" and row["sp_item_id"] and not row["sp_missing"] and sp.connected(conn))
    for f in files:
        name = safe_filename(os.path.basename(f.filename))
        if not is_model(name):
            errors.append(f"{name}: geen 3D-bestand (toegestaan: {', '.join(e.lstrip('.').upper() for e in EXT)})")
            continue
        data = f.read()
        if len(data) > MAX_MB * 1024 * 1024:
            errors.append(f"{name}: groter dan {MAX_MB} MB")
            continue
        if use_sp:
            try:
                res = sp.upload_model(conn, eid, name, data)
            except (requests.RequestException, RuntimeError) as exc:
                res = (False, f"SharePoint niet bereikbaar: {exc}")
            if res and res[0]:
                done += 1
                to_sp += 1
                audit("3D-model", "project", eid, f"{name} naar SharePoint")
                continue
            errors.append(f"{name}: {res[1] if res else 'SharePoint niet beschikbaar'}; lokaal opgeslagen")
        fid = save_bytes(entity, eid, data, name, kind="model3d", mime="application/octet-stream")
        audit("3D-model", entity, eid, name)
        convert_to_gltf(query("SELECT * FROM files WHERE id = ?", (fid,), one=True))
        done += 1
    if done:
        sub = (sp.setting(conn, "sp_sub_3d") or "1 Tekeningen")
        flash(f"{done} 3D-bestand(en) opgeslagen" + (f" in SharePoint ({sub})" if to_sp == done and to_sp else "") + ".", "ok")
    for e in errors:
        flash(e, "error")
    return redirect(back)


@bp.route("/bestand/<int:fid>/verwijderen", methods=["POST"])
@require("modellen3d", BEHEER)
def delete(fid):
    f = query("SELECT * FROM files WHERE id = ? AND kind = 'model3d'", (fid,), one=True) or abort(404)
    try:
        os.remove(file_path(f))
    except OSError:
        pass
    execute("DELETE FROM files WHERE id = ?", (fid,))
    audit("3D-model verwijderd", f["entity"], f["entity_id"], f["filename"])
    flash(f"{f['filename']} verwijderd.", "ok")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else _back(f["entity"], f["entity_id"]))


# ---------------------------------------------------------------- viewer + bestanden leveren

@bp.route("/bekijk/lokaal/<int:fid>")
@require("modellen3d", LEZEN)
def view_local(fid):
    f = query("SELECT * FROM files WHERE id = ? AND kind = 'model3d'", (fid,), one=True) or abort(404)
    return render_template("modellen/viewer.html", url=url_for("modellen.local_file", fid=fid, name=f["filename"]),
                           title=f["filename"], size=f["size"], back=_back(f["entity"], f["entity_id"]),
                           occt=occt_installed(), max_mb=MAX_MB,
                           cache_key=f"lokaal/{fid}/{f['size']}", cache_mb=cache_mb(), conv=_conv_local(f))


@bp.route("/bekijk/sharepoint/<int:pid>")
@require("modellen3d", LEZEN)
def view_sp(pid):
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    try:
        rel = sp.safe_rel(request.args.get("pad", ""))
    except ValueError:
        abort(400)
    if not rel or not is_model(rel) or not p["sp_item_id"]:
        abort(404)
    back = request.args.get("terug") if (request.args.get("terug") or "").startswith("/") else _back("project", pid)
    size, cache_key, version = None, None, None
    try:  # versie van het bestand, zodat een gewijzigde STEP opnieuw wordt gedownload
        meta = sp.file_meta(get_db(), pid, rel)
        size = meta.get("size")
        version = sp.item_version(meta)
        if version:
            cache_key = f"sp/{pid}/{rel}?v={version}"
    except (sp.GraphError, ValueError, requests.RequestException, RuntimeError):
        pass
    return render_template("modellen/viewer.html", url=url_for("modellen.sp_file", pid=pid, rel=rel),
                           title=rel.split("/")[-1], subtitle=f"{p['number']} · {rel}", back=back,
                           occt=occt_installed(), max_mb=MAX_MB, size=size, cache_key=cache_key, cache_mb=cache_mb(),
                           conv=_conv_sp(pid, rel, version, size) if cache_key else None)


@bp.route("/bestand/<int:fid>/<path:name>")
@require("modellen3d", LEZEN)
def local_file(fid, name):
    f = query("SELECT * FROM files WHERE id = ? AND kind = 'model3d'", (fid,), one=True) or abort(404)
    return send_file(file_path(f), mimetype="application/octet-stream", download_name=f["filename"],
                     as_attachment=request.args.get("download") == "1", max_age=3600)


@bp.route("/sp/<int:pid>/<path:rel>")
@require("modellen3d", LEZEN)
def sp_file(pid, rel):
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    if not p["sp_item_id"] or not is_model(rel) or not sp.connected(get_db()):
        abort(404)
    try:
        meta, r = sp.open_file(get_db(), pid, rel)
    except ValueError:
        abort(400)
    except sp.GraphError as exc:
        abort(404 if exc.status == 404 else 502)
    headers = {"Cache-Control": "private, max-age=600"}
    if meta.get("size") and not r.headers.get("Content-Encoding"):
        headers["Content-Length"] = str(meta["size"])
    if request.args.get("download") == "1":
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(meta.get('name') or 'model')}"
    return Response(stream_with_context(r.iter_content(256 * 1024)), mimetype="application/octet-stream", headers=headers)


# ---------------------------------------------------------------- omgezette modellen (GLB)

@bp.route("/omzet/<key>/status")
@require("modellen3d", LEZEN)
def conv_status(key):
    if not convert3d.valid_key(key):
        abort(404)
    st = convert3d.status(current_app._get_current_object(), key) or {"state": "onbekend"}
    now = __import__("time").time()
    out = {"state": st.get("state"), "step": st.get("step"), "error": st.get("error"), "size": st.get("size"),
           "src_size": st.get("src_size"), "wait": round(now - (st.get("started") or st.get("queued") or now))}
    return jsonify(out)


@bp.route("/omzet/<key>.glb")
@require("modellen3d", LEZEN)
def conv_glb(key):
    app = current_app._get_current_object()
    if not convert3d.valid_key(key) or not os.path.exists(convert3d.glb_path(app, key)):
        abort(404)
    convert3d.touch(app, key)
    return send_file(convert3d.glb_path(app, key), mimetype="model/gltf-binary", max_age=86400)
