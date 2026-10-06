import json

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, jsonify, Response

from .db import query, execute
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, files_for, save_uploads, delete_file, to_float

bp = Blueprint("kennis", __name__, url_prefix="/kennis")

TOOLS = [
    {"endpoint": "kennis.tools", "icon": "pipe", "title": "Leidingen, pompen & CIP",
     "text": "Leidinginhoud, snelheden in leidingen, pomp en drukverlies, CIP-reiniging en verpompen (DIN 11850 reeks 2)."},
    {"endpoint": "kennis.motor", "icon": "bolt", "title": "Draaistroommotor: ster of driehoek",
     "text": "Beantwoord een paar vragen en zie direct hoe je de bruggen op het klemmenbord legt."},
    {"endpoint": "kennis.reducer", "icon": "cone", "title": "Verloopstuk inkorten",
     "text": "Kies de grote of kleine kant, vul de maten en de gewenste nieuwe diameter in."},
    {"endpoint": "kennis.offset", "icon": "pipe", "title": "Verzet met twee bochten (45°)",
     "text": "Hoe lang moet de buis tussen twee 45°-bochten zijn bij een verzet hart-op-hart?"},
]


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


@bp.route("/")
@require("kennis", LEZEN)
def index():
    q = (request.args.get("q") or "").strip()
    cat = request.args.get("cat") or ""
    where, params = [], []
    if q:
        where.append("(title LIKE ? OR body LIKE ? OR IFNULL(tags,'') LIKE ?)")
        params += [f"%{q}%"] * 3
    if cat:
        where.append("category = ?")
        params.append(cat)
    sql = "SELECT * FROM articles" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY category, title"
    articles = query(sql, params)
    cats = [r["category"] for r in query("SELECT DISTINCT category FROM articles WHERE category IS NOT NULL ORDER BY category")]
    tools = [t for t in TOOLS if not q or q.lower() in (t["title"] + t["text"]).lower()]
    return render_template("kennis/index.html", articles=articles, cats=cats, cat=cat, q=q, tools=tools)


@bp.route("/motor")
@require("kennis", LEZEN)
def motor():
    return render_template("kennis/motor.html")


@bp.route("/verloopstuk")
@require("kennis", LEZEN)
def reducer():
    return render_template("kennis/reducer.html")


@bp.route("/verzet")
@require("kennis", LEZEN)
def offset():
    bends = query("SELECT * FROM bends ORDER BY diameter, radius")
    return render_template("kennis/offset.html", bends=[dict(b) for b in bends])


@bp.route("/bochten", methods=["POST"])
@require("kennis_schrijven", BEWERKEN)
def bends():
    if request.form.get("delete"):
        execute("DELETE FROM bends WHERE id = ?", (request.form.get("delete"),))
    else:
        radius = to_float(request.form.get("radius"))
        if not _f("name") or radius is None:
            flash("Vul naam en buigradius in.", "error")
        else:
            execute("INSERT INTO bends (name, diameter, radius, tangent, notes) VALUES (?,?,?,?,?)",
                    (_f("name"), to_float(request.form.get("diameter")), radius, to_float(request.form.get("tangent"), 0) or 0,
                     _f("notes")))
            flash("Bocht toegevoegd aan de bibliotheek.", "ok")
    return redirect(url_for("kennis.offset"))


@bp.route("/artikel/<int:aid>")
@require("kennis", LEZEN)
def article(aid):
    a = query("SELECT a.*, u.name AS author FROM articles a LEFT JOIN users u ON u.id = a.created_by WHERE a.id = ?",
              (aid,), one=True) or abort(404)
    return render_template("kennis/article.html", a=a, files=files_for("article", aid))


@bp.route("/artikel/nieuw", methods=["GET", "POST"])
@bp.route("/artikel/<int:aid>/bewerken", methods=["GET", "POST"])
@require("kennis_schrijven", BEWERKEN)
def edit(aid=None):
    a = query("SELECT * FROM articles WHERE id = ?", (aid,), one=True) if aid else None
    if aid and not a:
        abort(404)
    if request.method == "POST":
        if not _f("title"):
            flash("Vul een titel in.", "error")
            return render_template("kennis/edit.html", a=request.form, aid=aid, files=files_for("article", aid) if aid else [])
        now = now_iso()
        if aid:
            execute("UPDATE articles SET title=?, category=?, tags=?, body=?, updated_at=? WHERE id=?",
                    (_f("title"), _f("category"), _f("tags"), request.form.get("body") or "", now, aid))
        else:
            aid = execute("INSERT INTO articles (title, category, tags, body, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                          (_f("title"), _f("category"), _f("tags"), request.form.get("body") or "", g.user["id"], now, now))
        save_uploads("article", aid)
        for fid in request.form.getlist("remove_file"):
            delete_file(int(fid))
        audit("opgeslagen", "article", aid, _f("title"))
        flash("Artikel opgeslagen.", "ok")
        return redirect(url_for("kennis.article", aid=aid))
    cats = [r["category"] for r in query("SELECT DISTINCT category FROM articles WHERE category IS NOT NULL ORDER BY category")]
    return render_template("kennis/edit.html", a=a or {}, aid=aid, files=files_for("article", aid) if aid else [], cats=cats)


@bp.route("/artikel/<int:aid>/verwijderen", methods=["POST"])
@require("kennis_schrijven", BEHEER)
def delete(aid):
    for f in files_for("article", aid):
        delete_file(f["id"])
    execute("DELETE FROM articles WHERE id = ?", (aid,))
    audit("verwijderd", "article", aid)
    flash("Artikel verwijderd.", "ok")
    return redirect(url_for("kennis.index"))


# ---------------------------------------------------------------- rekentools: leidingen, pompen & CIP

CALC_KINDS = {"i": "Leidinginhoud", "s": "Snelheid & debiet", "p": "Pomp & drukverlies", "c": "CIP / reinigen", "v": "Verpompen"}


def _link_label(r):
    if r["project_id"]:
        p = query("SELECT number, name, kind FROM projects WHERE id = ?", (r["project_id"],), one=True)
        if p:
            return ("Order " if p["kind"] == "order" else "Project ") + p["number"], p
    if r["ticket_id"]:
        t = query("SELECT number, title FROM tickets WHERE id = ?", (r["ticket_id"],), one=True)
        if t:
            return "Ticket " + t["number"], t
    return None, None


@bp.route("/rekentools")
@require("kennis", LEZEN)
def tools():
    tab = request.args.get("tab") if request.args.get("tab") in CALC_KINDS else "i"
    saved = None
    if request.args.get("laad"):
        r = query("SELECT * FROM calc_saves WHERE id = ?", (request.args.get("laad", type=int),), one=True) or abort(404)
        saved = {"id": r["id"], "kind": r["kind"], "title": r["title"], "inputs": json.loads(r["inputs"] or "{}")}
        tab = r["kind"]
    projects = query("SELECT id, number, name, kind FROM projects WHERE status = 'actief' ORDER BY number DESC LIMIT 800") \
        if (can("projecten") or can("orders")) else []
    tickets = query("SELECT id, number, title FROM tickets WHERE status <> 'afgerond' ORDER BY id DESC LIMIT 300") if can("service") else []
    recent = []
    for r in query("SELECT c.*, u.name AS who FROM calc_saves c LEFT JOIN users u ON u.id = c.created_by ORDER BY c.id DESC LIMIT 12"):
        d = dict(r)
        d["link_label"] = _link_label(r)[0]
        recent.append(d)
    return render_template("kennis/rekentools.html", tab=tab, saved=saved, projects=projects, tickets=tickets, recent=recent,
                           KINDS=CALC_KINDS)


@bp.route("/rekentools/opslaan", methods=["POST"])
@require("kennis", LEZEN)
def calc_save():
    js = request.get_json(silent=True) or {}
    kind = js.get("kind")
    if kind not in CALC_KINDS:
        return jsonify({"error": "Onbekende berekening"}), 400
    link = str(js.get("link") or "")
    pid = tid = None
    if link.startswith("p") and link[1:].isdigit() and (can("projecten") or can("orders")):
        pid = int(link[1:]) if query("SELECT 1 FROM projects WHERE id = ?", (int(link[1:]),), one=True) else None
    elif link.startswith("t") and link[1:].isdigit() and can("service"):
        tid = int(link[1:]) if query("SELECT 1 FROM tickets WHERE id = ?", (int(link[1:]),), one=True) else None
    title = (str(js.get("title") or "").strip() or CALC_KINDS[kind])[:150]
    results = {"results": js.get("results") or [], "lines": js.get("lines") or [], "inputs_text": js.get("inputs_text") or []}
    now = now_iso()
    cid = js.get("id")
    row = query("SELECT * FROM calc_saves WHERE id = ?", (cid,), one=True) if cid else None
    if row and (row["created_by"] == g.user["id"] or can("kennis_schrijven", BEHEER) or g.user["is_admin"]):
        execute("UPDATE calc_saves SET kind=?, title=?, inputs=?, results=?, project_id=?, ticket_id=?, updated_at=? WHERE id=?",
                (kind, title, json.dumps(js.get("inputs") or {}), json.dumps(results), pid, tid, now, row["id"]))
        cid = row["id"]
    else:
        cid = execute("INSERT INTO calc_saves (kind, title, inputs, results, project_id, ticket_id, created_by, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (kind, title, json.dumps(js.get("inputs") or {}), json.dumps(results), pid, tid, g.user["id"], now))
    r = query("SELECT * FROM calc_saves WHERE id = ?", (cid,), one=True)
    label, obj = _link_label(r)
    link_url = None
    if pid:
        from .werk import werk_url
        link_url = werk_url("detail", obj["kind"] or "project", pid=pid)
    elif tid:
        link_url = url_for("service.detail", tid=tid)
    return jsonify({"ok": True, "id": cid, "pdf": url_for("kennis.calc_pdf", cid=cid), "link_url": link_url, "link_label": label})


@bp.route("/rekentools/<int:cid>.pdf")
@require("kennis", LEZEN)
def calc_pdf(cid):
    r = query("SELECT c.*, u.name AS who FROM calc_saves c LEFT JOIN users u ON u.id = c.created_by WHERE c.id = ?", (cid,), one=True) or abort(404)
    from .pdf import calc_pdf as build
    data = json.loads(r["results"] or "{}")
    label = _link_label(r)[0]
    pdf = build(dict(r), CALC_KINDS.get(r["kind"], "Berekening"), data, label)
    fname = f"Berekening {r['title'] or CALC_KINDS.get(r['kind'])}.pdf".replace("/", "-")
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}"'})


@bp.route("/rekentools/<int:cid>/verwijderen", methods=["POST"])
@require("kennis", LEZEN)
def calc_delete(cid):
    r = query("SELECT * FROM calc_saves WHERE id = ?", (cid,), one=True) or abort(404)
    if r["created_by"] != g.user["id"] and not g.user["is_admin"]:
        abort(403)
    execute("DELETE FROM calc_saves WHERE id = ?", (cid,))
    flash("Berekening verwijderd.", "ok")
    return redirect(request.form.get("terug") or url_for("kennis.tools"))


def calcs_for(project_id=None, ticket_id=None):
    if project_id:
        return query("SELECT * FROM calc_saves WHERE project_id = ? ORDER BY id DESC", (project_id,))
    return query("SELECT * FROM calc_saves WHERE ticket_id = ? ORDER BY id DESC", (ticket_id,))
