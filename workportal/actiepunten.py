"""Actiepunten bij projecten en orders: een afvinklijst met per punt een verantwoordelijke (met melding), een streefdatum
en foto's (bij het punt en bij het afvinken als bewijs dat het klaar is)."""
from flask import request, redirect, url_for, flash, abort, g

from .db import query, execute, get_db
from .permissions import can, BEWERKEN, BEHEER
from .util import now_iso, audit, to_int, files_for, save_uploads, delete_file, fmt_date
from .werk import bp, KINDS, BP_OF_KIND, _row, werk_url


def _module(p):
    return KINDS[BP_OF_KIND[p["kind"]]]["module"]


def _can_edit(p):
    return can(_module(p), BEWERKEN)


def _item(p, aid):
    return query("SELECT * FROM action_items WHERE id = ? AND project_id = ?", (aid, p["id"]), one=True) or abort(404)


def _back(p, aid=None):
    return redirect(werk_url("detail", p, pid=p["id"]) + (f"#ap{aid}" if aid else "#actiepunten"))


def items_for(pid):
    rows = query("SELECT a.*, u.name AS assignee, d.name AS done_name FROM action_items a"
                 " LEFT JOIN users u ON u.id = a.assigned_to LEFT JOIN users d ON d.id = a.done_by"
                 " WHERE a.project_id = ? ORDER BY (a.done_at IS NOT NULL), COALESCE(a.due_date, '9999'), a.id", (pid,))
    out = []
    for r in rows:
        d = dict(r)
        d["photos"] = files_for("action_item", r["id"])
        d["done_photos"] = files_for("action_done", r["id"])
        out.append(d)
    return out


def open_for_user(uid, limit=50):
    return query("SELECT a.*, p.number, p.name AS pname, p.kind FROM action_items a JOIN projects p ON p.id = a.project_id"
                 " WHERE a.assigned_to = ? AND a.done_at IS NULL ORDER BY COALESCE(a.due_date, '9999'), a.id LIMIT ?", (uid, limit))


def _notify(p, a, uid):
    if not uid or uid == g.user["id"]:
        return
    try:
        from .notify import notify_user
        notify_user(get_db(), uid, f"Actiepunt {p['number']}: {a['title']}"[:120],
                    f"{g.user['name']} heeft je een actiepunt gegeven bij {p['number']} {p['name'] or ''}"
                    + (f" (klaar vóór {fmt_date(a['due_date'])})" if a["due_date"] else "") + ".",
                    url=werk_url("detail", p, pid=p["id"]) + f"#ap{a['id']}", email=True)
    except Exception as exc:  # pragma: no cover
        print(f"[WorkPortal] Melding actiepunt mislukt: {exc}", flush=True)


@bp.route("/<int:pid>/actiepunt", methods=["POST"])
def action_save(pid):
    """Nieuw actiepunt, of een bestaand wijzigen (veld id)."""
    p = _row(pid)
    if not _can_edit(p):
        abort(403)
    f = request.form
    title = (f.get("title") or "").strip()[:200]
    if not title:
        flash("Vul een korte omschrijving van het actiepunt in.", "error")
        return _back(p)
    uid = to_int(f.get("assigned_to"))
    if uid and not query("SELECT 1 FROM users WHERE id = ? AND active = 1", (uid,), one=True):
        uid = None
    due = (f.get("due_date") or "")[:10] or None
    desc = (f.get("description") or "").strip()[:4000] or None
    now = now_iso()
    aid = to_int(f.get("id"))
    if aid:
        a = _item(p, aid)
        execute("UPDATE action_items SET title = ?, description = ?, assigned_to = ?, due_date = ?, updated_at = ? WHERE id = ?",
                (title, desc, uid, due, now, aid))
        if uid and uid != a["assigned_to"]:
            _notify(p, {"id": aid, "title": title, "due_date": due}, uid)
        audit("actiepunt gewijzigd", "project", pid, title)
        msg = "Actiepunt opgeslagen."
    else:
        aid = execute("INSERT INTO action_items (project_id, title, description, assigned_to, due_date, created_by, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (pid, title, desc, uid, due, g.user["id"], now, now))
        _notify(p, {"id": aid, "title": title, "due_date": due}, uid)
        audit("actiepunt toegevoegd", "project", pid, title)
        msg = "Actiepunt toegevoegd."
    save_uploads("action_item", aid)
    flash(msg, "ok")
    return _back(p, aid)


@bp.route("/<int:pid>/actiepunt/<int:aid>/afvinken", methods=["POST"])
def action_done(pid, aid):
    p = _row(pid)
    a = _item(p, aid)
    if not (_can_edit(p) or a["assigned_to"] == g.user["id"]):
        abort(403)
    if not a["done_at"]:
        execute("UPDATE action_items SET done_at = ?, done_by = ?, done_note = ? WHERE id = ?",
                (now_iso(), g.user["id"], (request.form.get("note") or "").strip()[:2000] or None, aid))
        save_uploads("action_done", aid)
        audit("actiepunt afgevinkt", "project", pid, a["title"])
        if a["created_by"] and a["created_by"] != g.user["id"]:
            try:
                from .notify import notify_user
                notify_user(get_db(), a["created_by"], f"Actiepunt klaar: {a['title']}"[:120],
                            f"{g.user['name']} heeft het actiepunt bij {p['number']} afgevinkt.",
                            url=werk_url("detail", p, pid=pid) + f"#ap{aid}", email=False)
            except Exception:  # noqa: BLE001
                pass
        flash("Actiepunt afgevinkt.", "ok")
    return _back(p, aid)


@bp.route("/<int:pid>/actiepunt/<int:aid>/heropenen", methods=["POST"])
def action_reopen(pid, aid):
    p = _row(pid)
    a = _item(p, aid)
    if not (_can_edit(p) or a["assigned_to"] == g.user["id"]):
        abort(403)
    execute("UPDATE action_items SET done_at = NULL, done_by = NULL WHERE id = ?", (aid,))
    audit("actiepunt heropend", "project", pid, a["title"])
    flash("Actiepunt staat weer open.", "ok")
    return _back(p, aid)


@bp.route("/<int:pid>/actiepunt/<int:aid>/fotos", methods=["POST"])
def action_photos(pid, aid):
    p = _row(pid)
    a = _item(p, aid)
    if not (_can_edit(p) or a["assigned_to"] == g.user["id"]):
        abort(403)
    n = len(save_uploads("action_done" if a["done_at"] else "action_item", aid))
    flash(f"{n} foto('s) toegevoegd.", "ok")
    return _back(p, aid)


@bp.route("/<int:pid>/actiepunt/<int:aid>/foto/<int:fid>/verwijderen", methods=["POST"])
def action_photo_delete(pid, aid, fid):
    p = _row(pid)
    a = _item(p, aid)
    f = query("SELECT * FROM files WHERE id = ? AND entity IN ('action_item', 'action_done') AND entity_id = ?", (fid, aid), one=True) \
        or abort(404)
    if not (_can_edit(p) or (a["assigned_to"] == g.user["id"] and f["created_by"] == g.user["id"])):
        abort(403)
    delete_file(fid)
    flash("Foto verwijderd.", "ok")
    return _back(p, aid)


@bp.route("/<int:pid>/actiepunt/<int:aid>/verwijderen", methods=["POST"])
def action_delete(pid, aid):
    p = _row(pid)
    a = _item(p, aid)
    if not (_can_edit(p) and (a["created_by"] == g.user["id"] or can(_module(p), BEHEER) or not a["done_at"])):
        abort(403)
    for f in files_for("action_item", aid) + files_for("action_done", aid):
        delete_file(f["id"])
    execute("DELETE FROM action_items WHERE id = ?", (aid,))
    audit("actiepunt verwijderd", "project", pid, a["title"])
    flash("Actiepunt verwijderd.", "ok")
    return _back(p)
