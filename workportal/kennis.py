from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from .db import query, execute
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, files_for, save_uploads, delete_file, to_float

bp = Blueprint("kennis", __name__, url_prefix="/kennis")

TOOLS = [
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
