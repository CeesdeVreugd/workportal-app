import json

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, Response

from .db import query, execute, get_db
from .engine import parse_erp_xlsx, compute_nacalc, load_calc
from .integrations import fetch_nacalc, nacalc_configured
from .pdf import nacalc_pdf
from .permissions import require, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, to_float, to_int, save_bytes, safe_filename

bp = Blueprint("nacalc", __name__, url_prefix="/nacalculatie")

LINE_FIELDS = ["item_id", "item_desc", "pos", "qty", "line_type", "unit", "article", "description", "cost_unit",
               "cost_factor", "cost_discount", "cost_subtotal", "margin", "sale_unit", "sale_factor", "sale_discount",
               "sale_subtotal"]


def _load(nid):
    n = query("SELECT n.*, p.number AS project_no, p.name AS project_name, c.number AS calc_no, c.title AS calc_title"
              " FROM nacalcs n LEFT JOIN projects p ON p.id = n.project_id LEFT JOIN calculations c ON c.id = n.calc_id"
              " WHERE n.id = ?", (nid,), one=True)
    if not n:
        abort(404)
    lines = [dict(r) for r in query("SELECT * FROM nacalc_lines WHERE nacalc_id = ? ORDER BY pos, id", (nid,))]
    return dict(n), lines


def evaluate(nid):
    n, lines = _load(nid)
    calc = load_calc(n["calc_id"]) if n["calc_id"] else None
    summary, items = compute_nacalc(n, lines, calc)
    return n, lines, calc, summary, items


def latest_results():
    """Laatste import per order, voor dashboard en overzicht."""
    rows = query("SELECT n.id FROM nacalcs n WHERE n.id IN (SELECT MAX(id) FROM nacalcs GROUP BY COALESCE(order_no, id))"
                 " ORDER BY n.imported_at DESC")
    out = []
    for r in rows:
        n, _, calc, s, _ = evaluate(r["id"])
        out.append({"id": n["id"], "order_no": n["order_no"], "order_desc": n["order_desc"], "project_id": n["project_id"],
                    "project_no": n["project_no"], "calc_no": n["calc_no"], "imported_at": n["imported_at"],
                    "order_total": s["order_total"], "cost": s["cost"], "sale": s["sale"], "result": s["result"],
                    "margin_pct": s["margin_pct"], "status": s["status"], "label": s["label"]})
    return out


def import_file(data, filename, project_id=None, calc_id=None, source="upload"):
    header, lines = parse_erp_xlsx(data)
    order_no = header.get("order_no")
    if not project_id and order_no:
        p = query("SELECT id FROM projects WHERE number = ?", (order_no,), one=True)
        if p:
            project_id = p["id"]
        else:
            project_id = execute("INSERT INTO projects (number, name, status, created_at) VALUES (?,?, 'actief', ?)",
                                 (order_no, header.get("order_desc") or f"Order {order_no}", now_iso()))
    prev = None
    if order_no:
        prev = query("SELECT calc_id, divide_by, no_divide FROM nacalcs WHERE order_no = ? ORDER BY id DESC LIMIT 1",
                     (order_no,), one=True)
    if not calc_id and prev:
        calc_id = prev["calc_id"]
    if not calc_id and project_id:
        c = query("SELECT id FROM calculations WHERE project_id = ? ORDER BY (status = 'akkoord') DESC, updated_at DESC LIMIT 1",
                  (project_id,), one=True)
        calc_id = c["id"] if c else None
    db = get_db()
    cur = db.execute(
        "INSERT INTO nacalcs (project_id, calc_id, order_no, order_desc, order_total, invoiced_total, divide_by, no_divide,"
        " source, filename, imported_by, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, calc_id, order_no, header.get("order_desc"), header.get("order_total"), header.get("invoiced_total"),
         prev["divide_by"] if prev else 1, prev["no_divide"] if prev else None, source, filename, g.user["id"], now_iso()))
    nid = cur.lastrowid
    for l in lines:
        db.execute(f"INSERT INTO nacalc_lines (nacalc_id, {', '.join(LINE_FIELDS)}) VALUES (?{', ?' * len(LINE_FIELDS)})",
                   [nid] + [l.get(f) for f in LINE_FIELDS])
    db.commit()
    save_bytes("nacalc", nid, data, filename or f"nacalculatie_{order_no}.xlsx", kind="attachment",
               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    audit("geïmporteerd", "nacalc", nid, f"order {order_no}, {len(lines)} regels")
    return nid


@bp.route("/")
@require("nacalculatie", LEZEN)
def index():
    results = latest_results()
    projects = query("SELECT id, number, name FROM projects ORDER BY number DESC")
    calcs = query("SELECT id, number, title FROM calculations ORDER BY updated_at DESC")
    return render_template("nacalc/index.html", results=results, projects=projects, calcs=calcs,
                           pre_project=to_int(request.args.get("project")), pre_calc=to_int(request.args.get("calc")),
                           pa=nacalc_configured())


@bp.route("/importeren", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Kies de Excel-export uit het ERP.", "error")
        return redirect(url_for("nacalc.index"))
    try:
        nid = import_file(f.read(), f.filename, to_int(request.form.get("project_id")), to_int(request.form.get("calc_id")))
    except Exception as exc:
        flash(f"Importeren mislukt: {exc}", "error")
        return redirect(url_for("nacalc.index"))
    flash("Na-calculatie geïmporteerd.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid))


@bp.route("/ophalen", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def fetch():
    order_no = (request.form.get("order_no") or "").strip()
    if not order_no:
        flash("Vul het ordernummer in.", "error")
        return redirect(url_for("nacalc.index"))
    data, name = fetch_nacalc(order_no)
    if data is None:
        flash(f"Ophalen mislukt: {name}", "error")
        return redirect(request.referrer or url_for("nacalc.index"))
    try:
        nid = import_file(data, name, to_int(request.form.get("project_id")), to_int(request.form.get("calc_id")),
                          source="powerautomate")
    except Exception as exc:
        flash(f"Bestand ontvangen maar importeren mislukt: {exc}", "error")
        return redirect(url_for("nacalc.index"))
    flash("Na-calculatie opgehaald via Power Automate.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid))


@bp.route("/<int:nid>")
@require("nacalculatie", LEZEN)
def detail(nid):
    n, lines, calc, summary, items = evaluate(nid)
    history = query("SELECT id, imported_at, order_total FROM nacalcs WHERE order_no = ? ORDER BY imported_at DESC",
                    (n["order_no"],)) if n["order_no"] else []
    hist = []
    for h in history:
        _, _, _, s, _ = evaluate(h["id"])
        hist.append({"id": h["id"], "imported_at": h["imported_at"], "cost": s["cost"], "result": s["result"],
                     "margin_pct": s["margin_pct"], "status": s["status"]})
    calcs = query("SELECT id, number, title FROM calculations ORDER BY updated_at DESC")
    return render_template("nacalc/detail.html", n=n, calc=calc, s=summary, items=items, hist=hist, calcs=calcs,
                           pa=nacalc_configured())


@bp.route("/<int:nid>/instellingen", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def settings(nid):
    _load(nid)
    divide_by = to_float(request.form.get("divide_by"), 1) or 1
    shared = [to_int(x) for x in request.form.getlist("divide_item")]
    all_pos = [to_int(x) for x in request.form.getlist("all_pos")]
    no_div = [p for p in all_pos if p not in shared]
    opts = {"items": no_div, "order": request.form.get("divide_order") == "1"}
    execute("UPDATE nacalcs SET divide_by = ?, no_divide = ?, calc_id = ? WHERE id = ?",
            (max(divide_by, 1), json.dumps(opts), to_int(request.form.get("calc_id")), nid))
    audit("instellingen", "nacalc", nid, f"delen door {divide_by}")
    flash("Instellingen opgeslagen.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid))


@bp.route("/<int:nid>/rapport.pdf")
@require("nacalculatie", LEZEN)
def report(nid):
    n, lines, calc, summary, items = evaluate(nid)
    pdf = nacalc_pdf(n, summary, items, calc)
    fname = safe_filename(f"Na-calculatie {n['order_no']}.pdf")
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}"'})


@bp.route("/<int:nid>/verwijderen", methods=["POST"])
@require("nacalculatie", BEHEER)
def delete(nid):
    n, _ = _load(nid)
    execute("DELETE FROM nacalcs WHERE id = ?", (nid,))
    audit("verwijderd", "nacalc", nid, n["order_no"])
    flash("Import verwijderd.", "ok")
    return redirect(url_for("nacalc.index"))
