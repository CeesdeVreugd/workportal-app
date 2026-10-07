"""Aantekeningen (productie-opmerkingen) bij projecten en orders.

Vervangt het losse Word-bestand: een screenshot (uit de 3D-viewer, van een ander programma, een foto of geplakt)
met pijlen/tekst erop, een stukje tekst eronder, een status en een actiehouder. Alle aantekeningen van een
project gaan samen als één PDF naar SharePoint: <projectmap>/3 Documenten/2 .../Productie-opmerkingen <nummer>.pdf
"""
import base64
import io
import re

import requests
from flask import render_template, request, redirect, url_for, flash, abort, g, Response
from PIL import Image

from . import sharepoint as sp
from .db import query, execute, get_db
from .permissions import can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, to_int, save_bytes, delete_file, file_path
from .werk import bp, KINDS, BP_OF_KIND, _row, _wrong_bp, werk_url, sp_on

STATUSES = [("nieuw", "Nieuw"), ("in_behandeling", "In behandeling"), ("afgerond", "Afgerond")]
STATUS_LABEL = dict(STATUSES)
SP_DOCS_RE = re.compile(r"^\s*3(\D|$)")       # '3 Documenten'
SP_SUB_RE = re.compile(r"^\s*2(\D|$)")        # '2 Productie opmerkingen' (of hoe het mapje heet)
SP_DOCS_DEFAULT = "3 Documenten"
SP_SUB_DEFAULT = "2 Productie opmerkingen"


def _module(p):
    return KINDS[BP_OF_KIND[p["kind"]]]["module"]


def _need_read(p):
    if not can(_module(p), LEZEN):
        abort(403)


def _can_edit(p):
    return can(_module(p), BEWERKEN)


def _can_status(p, a):
    """Status wijzigen: wie mag bewerken, of degene bij wie de actie ligt (bijv. de werkplaats)."""
    return _can_edit(p) or (a["assigned_to"] and a["assigned_to"] == g.user["id"])


def _annot(pid, aid):
    return query("SELECT a.*, u.name AS who, au.name AS assignee, du.name AS done_name FROM project_annotations a"
                 " LEFT JOIN users u ON u.id = a.created_by LEFT JOIN users au ON au.id = a.assigned_to"
                 " LEFT JOIN users du ON du.id = a.done_by WHERE a.id = ? AND a.project_id = ?", (aid, pid), one=True) or abort(404)


def annotations_for(pid):
    return query("SELECT a.*, u.name AS who, au.name AS assignee, du.name AS done_name FROM project_annotations a"
                 " LEFT JOIN users u ON u.id = a.created_by LEFT JOIN users au ON au.id = a.assigned_to"
                 " LEFT JOIN users du ON du.id = a.done_by WHERE a.project_id = ?"
                 " ORDER BY CASE a.status WHEN 'afgerond' THEN 1 ELSE 0 END, a.number DESC", (pid,))


def open_for_user(user_id, limit=10):
    """Open aantekeningen waar deze gebruiker de actie van heeft (voor het dashboard)."""
    return query("SELECT a.*, p.number AS pnumber, p.name AS pname, p.kind AS pkind FROM project_annotations a"
                 " JOIN projects p ON p.id = a.project_id WHERE a.assigned_to = ? AND a.status <> 'afgerond'"
                 " ORDER BY IFNULL(a.due_date, '9999'), a.created_at LIMIT ?", (user_id, limit))


def _users():
    return query("SELECT id, name FROM users WHERE active = 1 ORDER BY name")


def _image_from_dataurl(data_url, max_side=2400):
    if not data_url or "," not in data_url or not data_url.startswith("data:image/"):
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    if im.mode != "RGB":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- SharePoint: één PDF per project

def _find_or_make(g_, drive, parent_id, pattern, default):
    for it in g_.children(drive, parent_id):
        if "folder" in it and pattern.match(it["name"]):
            return it
    try:
        return g_.create_folder(drive, parent_id, default)
    except sp.GraphError as exc:
        if exc.status != 409:
            raise
        return g_.item_by_path(drive, default, parent_id)


def pdf_name(p):
    return f"Productie-opmerkingen {p['number']}.pdf"


def build_pdf(p, rows):
    from .pdf import annotations_pdf
    items = []
    for a in sorted(rows, key=lambda x: x["number"]):
        img = None
        if a["image_file_id"]:
            f = query("SELECT * FROM files WHERE id = ?", (a["image_file_id"],), one=True)
            img = file_path(f) if f else None
        items.append((dict(a), img))
    return annotations_pdf(dict(p), items, STATUS_LABEL)


def sync_sharepoint(p):
    """Zet de PDF met alle aantekeningen in 3 Documenten/2 ... van de projectmap. Geeft (ok, melding) of None."""
    conn = get_db()
    if not p["sp_item_id"] or p["sp_missing"] or not sp.connected(conn):
        return None
    rows = annotations_for(p["id"])
    try:
        g_ = sp.client()
        drive, _ = sp._ctx(conn)
        docs = _find_or_make(g_, drive, p["sp_item_id"], SP_DOCS_RE, SP_DOCS_DEFAULT)
        sub = _find_or_make(g_, drive, docs["id"], SP_SUB_RE, SP_SUB_DEFAULT)
        g_.upload(drive, sub["id"], pdf_name(p), build_pdf(p, rows), conflict="replace")
        return True, f"Bijgewerkt in SharePoint: {docs['name']}/{sub['name']}/{pdf_name(p)}"
    except sp.GraphError as exc:
        return False, f"Opslaan in SharePoint mislukt: {exc.message}"
    except (requests.RequestException, RuntimeError):
        return False, "SharePoint is op dit moment niet bereikbaar; de PDF wordt bij de volgende wijziging opnieuw geplaatst."


def _after_change(p):
    res = sync_sharepoint(p)
    if res and not res[0]:
        flash(res[1], "error")


def _notify(user_id, p, a, title, body):
    if not user_id or user_id == g.user["id"]:
        return
    try:
        from .notify import notify_user
        notify_user(get_db(), user_id, title, body, url=werk_url("annot_view", p, pid=p["id"], aid=a["id"]))
    except Exception:
        pass


# ---------------------------------------------------------------- routes

@bp.route("/<int:pid>/aantekeningen")
def annot_index(pid):
    p = _row(pid)
    r = _wrong_bp(p)
    if r:
        return r
    _need_read(p)
    return render_template("werk/aantekeningen.html", K=KINDS[BP_OF_KIND[p["kind"]]], B=request.blueprint, p=p,
                           rows=annotations_for(pid), STATUS_LABEL=STATUS_LABEL, can_edit=_can_edit(p),
                           sp_ok=bool(sp_on() and p["sp_item_id"] and not p["sp_missing"]), pdf_name=pdf_name(p))


def _form_values():
    status = request.form.get("status") if request.form.get("status") in STATUS_LABEL else "nieuw"
    due = (request.form.get("due_date") or "").strip()
    due = due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else None
    return {"title": (request.form.get("title") or "").strip()[:200], "body": (request.form.get("body") or "").strip(),
            "status": status, "assigned_to": to_int(request.form.get("assigned_to")), "due_date": due,
            "done_note": (request.form.get("done_note") or "").strip()}


@bp.route("/<int:pid>/aantekeningen/nieuw", methods=["GET", "POST"])
@bp.route("/<int:pid>/aantekeningen/<int:aid>/bewerken", methods=["GET", "POST"])
def annot_edit(pid, aid=None):
    p = _row(pid)
    r = _wrong_bp(p)
    if r:
        return r
    if not _can_edit(p):
        abort(403)
    a = _annot(pid, aid) if aid else None
    k = KINDS[BP_OF_KIND[p["kind"]]]
    ctx = dict(K=k, B=request.blueprint, p=p, a=a, users=_users(), STATUSES=STATUSES, today=now_iso()[:10])
    # Vanuit de 3D-viewer: alleen een afbeelding meegestuurd, nog niets ingevuld
    if request.method == "POST" and "title" not in request.form:
        seed = request.form.get("seed") or ""
        return render_template("werk/aantekening_form.html", seed=seed if seed.startswith("data:image/") else "",
                               form={"title": request.form.get("seed_title") or ""}, **ctx)
    if request.method == "POST":
        v = _form_values()
        if not v["title"]:
            flash("Geef de aantekening een korte titel.", "error")
            return render_template("werk/aantekening_form.html", seed=request.form.get("image") or "", form=v, **ctx)
        if v["status"] == "afgerond" and not v["done_note"]:
            flash("Schrijf bij het afronden kort wat er gedaan is.", "error")
            return render_template("werk/aantekening_form.html", seed=request.form.get("image") or "", form=v, **ctx)
        data = _image_from_dataurl(request.form.get("image"))
        now = now_iso()
        if a:
            image_id = a["image_file_id"]
            if request.form.get("image_changed") == "1":
                if data:
                    image_id = save_bytes("project", pid, data, f"aantekening-{a['number']}.jpg", kind="annotation", mime="image/jpeg")
                else:
                    image_id = None
                if a["image_file_id"] and a["image_file_id"] != image_id:
                    delete_file(a["image_file_id"])
            done = (now, g.user["id"]) if v["status"] == "afgerond" and a["status"] != "afgerond" else \
                ((a["done_at"], a["done_by"]) if v["status"] == "afgerond" else (None, None))
            execute("UPDATE project_annotations SET title=?, body=?, image_file_id=?, status=?, assigned_to=?, due_date=?,"
                    " done_note=?, done_at=?, done_by=?, updated_at=? WHERE id=?",
                    (v["title"], v["body"], image_id, v["status"], v["assigned_to"], v["due_date"],
                     v["done_note"] if v["status"] == "afgerond" else a["done_note"], done[0], done[1], now, aid))
            new_id = aid
            if v["assigned_to"] and v["assigned_to"] != a["assigned_to"]:
                _notify(v["assigned_to"], p, a, f"Actie voor jou: {p['number']} – aantekening {a['number']}",
                        f"{g.user['name']} heeft aantekening {a['number']} ‘{v['title']}’ bij {p['number']} {p['name']} aan jou toegewezen.")
            flash("Aantekening bijgewerkt.", "ok")
        else:
            n = (query("SELECT MAX(number) AS m FROM project_annotations WHERE project_id = ?", (pid,), one=True)["m"] or 0) + 1
            image_id = save_bytes("project", pid, data, f"aantekening-{n}.jpg", kind="annotation", mime="image/jpeg") if data else None
            new_id = execute("INSERT INTO project_annotations (project_id, number, title, body, image_file_id, status, assigned_to,"
                             " due_date, done_note, done_at, done_by, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (pid, n, v["title"], v["body"], image_id, v["status"], v["assigned_to"], v["due_date"],
                              v["done_note"] if v["status"] == "afgerond" else None,
                              now if v["status"] == "afgerond" else None, g.user["id"] if v["status"] == "afgerond" else None,
                              g.user["id"], now))
            audit("aantekening", "project", pid, f"{n}: {v['title']}"[:120])
            if v["assigned_to"]:
                _notify(v["assigned_to"], p, {"id": new_id}, f"Actie voor jou: {p['number']} – aantekening {n}",
                        f"{g.user['name']} heeft een aantekening ‘{v['title']}’ bij {p['number']} {p['name']} aan jou toegewezen.")
            flash(f"Aantekening {n} opgeslagen.", "ok")
        _after_change(_row(pid))
        return redirect(werk_url("annot_view", p, pid=pid, aid=new_id))
    seed = url_for("main.file", fid=a["image_file_id"]) if a and a["image_file_id"] else ""
    form = dict(a) if a else {"status": "nieuw"}
    return render_template("werk/aantekening_form.html", seed=seed, form=form, **ctx)


@bp.route("/<int:pid>/aantekeningen/<int:aid>")
def annot_view(pid, aid):
    p = _row(pid)
    r = _wrong_bp(p)
    if r:
        return r
    _need_read(p)
    a = _annot(pid, aid)
    nav = query("SELECT id, number FROM project_annotations WHERE project_id = ? ORDER BY number", (pid,))
    ids = [x["id"] for x in nav]
    i = ids.index(aid)
    return render_template("werk/aantekening.html", K=KINDS[BP_OF_KIND[p["kind"]]], B=request.blueprint, p=p, a=a,
                           STATUS_LABEL=STATUS_LABEL, can_edit=_can_edit(p), can_status=_can_status(p, a),
                           prev_id=ids[i - 1] if i > 0 else None, next_id=ids[i + 1] if i + 1 < len(ids) else None,
                           count=len(ids))


@bp.route("/<int:pid>/aantekeningen/<int:aid>/status", methods=["POST"])
def annot_status(pid, aid):
    p = _row(pid)
    a = _annot(pid, aid)
    if not _can_status(p, a):
        abort(403)
    st = request.form.get("status")
    if st not in STATUS_LABEL:
        abort(400)
    note = (request.form.get("done_note") or "").strip()
    back = werk_url("annot_view", p, pid=pid, aid=aid)
    now = now_iso()
    if st == "afgerond":
        if not note:
            flash("Schrijf bij het afronden kort wat er gedaan is.", "error")
            return redirect(back + "#afronden")
        execute("UPDATE project_annotations SET status='afgerond', done_note=?, done_by=?, done_at=?, updated_at=? WHERE id=?",
                (note, g.user["id"], now, now, aid))
        _notify(a["created_by"], p, a, f"Afgerond: {p['number']} – aantekening {a['number']}",
                f"{g.user['name']} heeft aantekening {a['number']} ‘{a['title']}’ afgerond.\n\n{note}")
        flash(f"Aantekening {a['number']} afgerond.", "ok")
    else:
        execute("UPDATE project_annotations SET status=?, updated_at=?, done_at=CASE WHEN ?='afgerond' THEN done_at END WHERE id=?",
                (st, now, st, aid))
        flash(f"Status: {STATUS_LABEL[st]}.", "ok")
    audit("aantekening status", "project", pid, f"{a['number']}: {STATUS_LABEL[st]}")
    _after_change(p)
    return redirect(back)


@bp.route("/<int:pid>/aantekeningen/<int:aid>/verwijderen", methods=["POST"])
def annot_delete(pid, aid):
    p = _row(pid)
    a = _annot(pid, aid)
    if not (_can_edit(p) and (a["created_by"] == g.user["id"] or can(_module(p), BEHEER))):
        abort(403)
    if a["image_file_id"]:
        delete_file(a["image_file_id"])
    execute("DELETE FROM project_annotations WHERE id = ?", (aid,))
    audit("aantekening verwijderd", "project", pid, f"{a['number']}: {a['title']}"[:120])
    flash(f"Aantekening {a['number']} verwijderd.", "ok")
    _after_change(p)
    return redirect(werk_url("annot_index", p, pid=pid))


@bp.route("/<int:pid>/aantekeningen/pdf")
def annot_pdf(pid):
    p = _row(pid)
    _need_read(p)
    rows = annotations_for(pid)
    aid = to_int(request.args.get("id"))
    if aid:
        rows = [x for x in rows if x["id"] == aid]
    else:
        rows = sorted(rows, key=lambda x: x["number"])
    data = build_pdf(p, rows)
    name = pdf_name(p) if not aid else f"Aantekening {rows[0]['number'] if rows else ''} {p['number']}.pdf"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "private, no-store"})


@bp.route("/<int:pid>/aantekeningen/sharepoint", methods=["POST"])
def annot_sp(pid):
    p = _row(pid)
    if not _can_edit(p):
        abort(403)
    res = sync_sharepoint(p)
    flash(res[1] if res else "Dit project heeft geen gekoppelde SharePoint-map.", "ok" if res and res[0] else "error")
    return redirect(werk_url("annot_index", p, pid=pid))
