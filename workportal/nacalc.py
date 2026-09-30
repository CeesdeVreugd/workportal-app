import json

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, Response

from .db import query, execute, get_db
from .engine import parse_erp_xlsx, compute_nacalc, load_calc
from .integrations import fetch_nacalc, nacalc_configured
from .pdf import nacalc_pdf
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, audit, to_float, to_int, save_bytes, safe_filename

bp = Blueprint("nacalc", __name__, url_prefix="/nacalculatie")

LINE_FIELDS = ["item_id", "item_desc", "pos", "qty", "line_type", "unit", "article", "description", "cost_unit",
               "cost_factor", "cost_discount", "cost_subtotal", "margin", "sale_unit", "sale_factor", "sale_discount",
               "sale_subtotal"]


def _load(nid):
    n = query("SELECT n.*, p.number AS project_no, p.name AS project_name, p.order_type AS project_order_type, p.kind AS project_kind,"
              " c.number AS calc_no, c.title AS calc_title"
              " FROM nacalcs n LEFT JOIN projects p ON p.id = n.project_id LEFT JOIN calculations c ON c.id = n.calc_id"
              " WHERE n.id = ?", (nid,), one=True)
    if not n:
        abort(404)
    lines = [dict(r) for r in query("SELECT * FROM nacalc_lines WHERE nacalc_id = ? ORDER BY pos, id", (nid,))]
    return dict(n), lines


def excel_calc(n):
    """De uit Excel geïmporteerde calculatie (als die gebruikt wordt), in de vorm die engine verwacht."""
    if n.get("calc_id") or not n.get("xcalc_json"):
        return None
    try:
        x = json.loads(n["xcalc_json"])
    except ValueError:
        return None
    return {"id": None, "excel": True, "number": f"uit Excel ({n.get('xcalc_name') or 'calculatie'})", "title": n.get("xcalc_name"),
            "agreed_price": x.get("agreed_price"), "_excel": x}


def evaluate(nid):
    from .engine import nacalc_mode
    n, lines = _load(nid)
    if nacalc_mode(n) == "kostprijs":
        calc = None  # productieorder: niet vergelijken met een calculatie
    else:
        calc = load_calc(n["calc_id"]) if n["calc_id"] else excel_calc(n)
    summary, items = compute_nacalc(n, lines, calc)
    return n, lines, calc, summary, items


def latest_results():
    """Laatste import per order, voor dashboard en overzicht."""
    rows = query("SELECT n.id FROM nacalcs n WHERE n.id IN (SELECT MAX(id) FROM nacalcs GROUP BY COALESCE(order_no, id))"
                 " ORDER BY n.order_no IS NULL, n.order_no DESC, n.imported_at DESC")
    out = []
    for r in rows:
        n, _, calc, s, _ = evaluate(r["id"])
        out.append({"id": n["id"], "order_no": n["order_no"], "order_desc": n["order_desc"], "project_id": n["project_id"],
                    "project_no": n["project_no"], "calc_no": n["calc_no"], "imported_at": n["imported_at"],
                    "order_total": s["order_total"], "cost": s["cost"], "sale": s["sale"], "result": s["result"],
                    "margin_pct": s["margin_pct"], "status": s["status"], "label": s["label"],
                    "extra": s["above_min"] if s["mode"] == "normaal" and s["order_total"] > 0 and s["above_min"] > 0.005 else None,
                    "mode": s["mode"], "unit_cost": s.get("unit_cost"), "unit_sale": s.get("unit_sale"),
                    "excel": bool(calc and calc.get("excel")),
                    "cost_total": s.get("cost_total", s["cost"])})
    return out


def import_file(data, filename, project_id=None, calc_id=None, source="upload"):
    header, lines = parse_erp_xlsx(data)
    order_no = header.get("order_no")
    if not project_id and order_no:
        p = query("SELECT id FROM projects WHERE number = ?", (order_no,), one=True)
        if p:
            project_id = p["id"]
        else:
            project_id = execute("INSERT INTO projects (number, name, status, created_at, kind, source) VALUES (?,?, 'actief', ?, 'order', 'nacalculatie')",
                                 (order_no, header.get("order_desc") or f"Order {order_no}", now_iso()))
    prev = None
    if order_no:
        prev = query("SELECT calc_id, divide_by, no_divide, xcalc_json, xcalc_name, xcalc_file_id FROM nacalcs WHERE order_no = ?"
                     " ORDER BY id DESC LIMIT 1", (order_no,), one=True)
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
         prev["divide_by"] if prev else 1, prev["no_divide"] if prev else None, source, filename,
         g.user["id"] if getattr(g, "user", None) else None, now_iso()))
    nid = cur.lastrowid
    if prev and prev["xcalc_json"]:
        db.execute("UPDATE nacalcs SET xcalc_json = ?, xcalc_name = ?, xcalc_file_id = ? WHERE id = ?",
                   (prev["xcalc_json"], prev["xcalc_name"], prev["xcalc_file_id"], nid))
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
    projects = query("SELECT id, number, name, kind FROM projects ORDER BY number DESC")
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
        hist.append({"id": h["id"], "imported_at": h["imported_at"], "cost": s.get("cost_total", s["cost"]) if s["mode"] == "kostprijs" else s["cost"],
                     "result": s["result"], "margin_pct": s["margin_pct"], "status": s["status"], "mode": s["mode"],
                     "unit_cost": s.get("unit_cost")})
    calcs = query("SELECT id, number, title FROM calculations ORDER BY updated_at DESC")
    return render_template("nacalc/detail.html", n=n, calc=calc, s=summary, items=items, hist=hist, calcs=calcs,
                           pa=nacalc_configured(), notes=notes_for(n), oprops=_oprops(n), opts=_opts(n),
                           xcalc=json.loads(n["xcalc_json"]) if n.get("xcalc_json") else None)


def _opts(n):
    try:
        o = json.loads(n.get("no_divide") or "{}")
    except ValueError:
        o = {}
    return o if isinstance(o, dict) else {}


def _oprops(n):
    from .komdex import order_props
    if not n.get("project_id"):
        return {}
    return order_props(query("SELECT order_props FROM projects WHERE id = ?", (n["project_id"],), one=True))


def notes_for(n):
    """Opmerkingen horen bij de order: ze blijven staan bij een nieuwe import van dezelfde order."""
    if n.get("order_no"):
        return query("SELECT x.*, u.name AS who FROM nacalc_notes x LEFT JOIN users u ON u.id = x.created_by"
                     " WHERE x.order_no = ? ORDER BY x.created_at DESC", (n["order_no"],))
    return query("SELECT x.*, u.name AS who FROM nacalc_notes x LEFT JOIN users u ON u.id = x.created_by"
                 " WHERE x.nacalc_id = ? ORDER BY x.created_at DESC", (n["id"],))


@bp.route("/<int:nid>/calculatie-excel", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def excel_upload(nid):
    from . import xcalc
    n, _ = _load(nid)
    f = request.files.get("file")
    back = url_for("nacalc.detail", nid=nid) + "#calculatie"
    if not f or not f.filename:
        flash("Kies het Excel-bestand met de calculatie.", "error")
        return redirect(back)
    data = f.read()
    try:
        x = xcalc.parse(data)
    except Exception as exc:  # noqa: BLE001
        flash(f"Deze Excel kon niet als calculatie worden gelezen: {exc}", "error")
        return redirect(back)
    items = [it for it in evaluate(nid)[4] if it.get("lines")]
    x["map"] = xcalc.auto_map(x["sections"], items)
    fid = save_bytes("nacalc", nid, data, safe_filename(f.filename), kind="attachment",
                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    name = safe_filename(f.filename).rsplit(".", 1)[0][:120]
    # geldt voor alle imports van deze order
    where, params = ("order_no = ?", (n["order_no"],)) if n["order_no"] else ("id = ?", (nid,))
    execute(f"UPDATE nacalcs SET xcalc_json = ?, xcalc_name = ?, xcalc_file_id = ?, calc_id = NULL WHERE {where}",
            (json.dumps(x), name, fid) + params)
    audit("calculatie uit Excel", "nacalc", nid, f"{name}: {len(x['sections'])} secties, € {x['total']:.2f}")
    missing = [s for s, k in x["map"].items() if not k]
    flash(f"Calculatie uit Excel ingelezen: {len(x['sections'])} onderdelen, totaal € {x['total']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
          + (f". Nog niet gekoppeld: {', '.join(missing)}." if missing else "."), "ok")
    return redirect(back)


@bp.route("/<int:nid>/calculatie-excel/koppeling", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def excel_map(nid):
    n, _ = _load(nid)
    if not n.get("xcalc_json"):
        abort(404)
    x = json.loads(n["xcalc_json"])
    x["map"] = {s["name"]: (request.form.get(f"map_{i}") or None) for i, s in enumerate(x.get("sections", []))}
    where, params = ("order_no = ?", (n["order_no"],)) if n["order_no"] else ("id = ?", (nid,))
    execute(f"UPDATE nacalcs SET xcalc_json = ? WHERE {where}", (json.dumps(x),) + params)
    flash("Koppeling opgeslagen.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid) + "#calculatie")


@bp.route("/<int:nid>/calculatie-excel/verwijderen", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def excel_delete(nid):
    n, _ = _load(nid)
    where, params = ("order_no = ?", (n["order_no"],)) if n["order_no"] else ("id = ?", (nid,))
    execute(f"UPDATE nacalcs SET xcalc_json = NULL, xcalc_name = NULL, xcalc_file_id = NULL WHERE {where}", params)
    flash("Excel-calculatie losgekoppeld.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid) + "#calculatie")


@bp.route("/<int:nid>/opmerking", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def note_add(nid):
    n, _ = _load(nid)
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Vul een opmerking in.", "error")
    else:
        execute("INSERT INTO nacalc_notes (order_no, nacalc_id, body, created_by, created_at) VALUES (?,?,?,?,?)",
                (n["order_no"], nid, body[:4000], g.user["id"], now_iso()))
        audit("opmerking", "nacalc", nid, body[:200])
        flash("Opmerking opgeslagen.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid) + "#opmerkingen")


@bp.route("/<int:nid>/opmerking/<int:xid>/verwijderen", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def note_delete(nid, xid):
    x = query("SELECT * FROM nacalc_notes WHERE id = ?", (xid,), one=True) or abort(404)
    if x["created_by"] != g.user["id"] and not can("nacalculatie", BEHEER):
        abort(403)
    execute("DELETE FROM nacalc_notes WHERE id = ?", (xid,))
    flash("Opmerking verwijderd.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid) + "#opmerkingen")


@bp.route("/<int:nid>/instellingen", methods=["POST"])
@require("nacalculatie", BEWERKEN)
def settings(nid):
    n, _ = _load(nid)
    divide_by = to_float(request.form.get("divide_by"), 1) or 1
    per_item = {}
    for key, val in zip(request.form.getlist("item_key"), request.form.getlist("item_div")):
        d = to_float(val, None)
        if key and d and d > 0:
            per_item[key] = d
    old = _opts(n)
    if request.form.get("part") == "top":
        # alleen soort en aantal machines (bovenaan de pagina); de rest blijft staan
        mode = request.form.get("mode") if request.form.get("mode") in ("auto", "normaal", "kostprijs") else "auto"
        old["mode"] = mode
        execute("UPDATE nacalcs SET divide_by = ?, no_divide = ? WHERE id = ?", (max(divide_by, 1), json.dumps(old), nid))
        flash("Opgeslagen.", "ok")
        return redirect(url_for("nacalc.detail", nid=nid))
    mode = request.form.get("mode") if request.form.get("mode") in ("auto", "normaal", "kostprijs") else (old.get("mode") or "auto")
    item_kind = {}
    for key, kind in zip(request.form.getlist("item_key"), request.form.getlist("item_kind")):
        if key and kind in ("basis", "versie", "optie"):
            item_kind[key] = kind
    versions = [k for k, v in item_kind.items() if v == "versie"]
    variants = []
    for i in range(20):
        name = (request.form.get(f"var_{i}_name") or "").strip()
        if name:
            variants.append({"name": name[:120], "versions": [k for k in request.form.getlist(f"var_{i}_ver") if k in versions]})
    if request.form.get("part") == "kostprijs":
        per_item = {}
        for key in request.form.getlist("item_key"):
            d = to_float(request.form.get(f"cnt_{key}"), None)
            if d and d > 0:
                per_item[key] = d
        opts = {**old, "per_item": per_item, "item_kind": item_kind, "variants": variants, "options": [], "mode": mode}
    else:
        opts = {**old, "items": [], "per_item": per_item, "order": request.form.get("divide_order") == "1", "mode": mode}
    calc_id = n["calc_id"] if "calc_id" not in request.form else (None if request.form.get("calc_id") == "excel" else to_int(request.form.get("calc_id")))
    if "divide_by" not in request.form:
        divide_by = n["divide_by"] or 1
    execute("UPDATE nacalcs SET divide_by = ?, no_divide = ?, calc_id = ? WHERE id = ?",
            (max(divide_by, 1), json.dumps(opts), calc_id, nid))
    audit("instellingen", "nacalc", nid, f"aantal {divide_by}, per item {per_item}")
    flash("Instellingen opgeslagen.", "ok")
    return redirect(url_for("nacalc.detail", nid=nid))


@bp.route("/<int:nid>/rapport.pdf")
@require("nacalculatie", LEZEN)
def report(nid):
    n, lines, calc, summary, items = evaluate(nid)
    pdf = nacalc_pdf(n, summary, items, calc, notes=notes_for(n))
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
