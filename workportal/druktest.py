from datetime import timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, Response

from .db import query, execute, get_db
from . import sharepoint as sp
from .integrations import upload_to_sharepoint, sharepoint_configured
from .pdf import pressure_test_pdf
from .permissions import require, LEZEN, BEWERKEN, BEHEER
from .util import (now_iso, now_utc, iso, audit, files_for, save_uploads, save_signature, save_bytes, file_path,
                   to_float, to_int, next_number, get_setting, delete_file, safe_filename, fmt_num)

bp = Blueprint("druktest", __name__, url_prefix="/druktesten")

PARTS = ["binnenbuis", "mantel", "binnenbuis en mantel"]
MEDIA = ["lucht", "stikstof", "water"]


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


def _presets():
    out = []
    for p in (get_setting("pressure_presets") or "").split(","):
        m = to_int(p.strip())
        if m:
            out.append(m)
    return out or [15, 30, 60, 120, 240, 1440]


def _label(minutes):
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} dag" if d == 1 else f"{d} dagen"
    if minutes % 60 == 0:
        return f"{minutes // 60} uur"
    return f"{minutes} min"


def _test(tid):
    t = query("SELECT t.*, p.number AS project_no, p.name AS project_name, p.sharepoint_path, p.sp_item_id AS project_sp, p.sp_missing AS project_sp_missing, c.name AS customer,"
              " u.name AS creator FROM pressure_tests t LEFT JOIN projects p ON p.id = t.project_id"
              " LEFT JOIN customers c ON c.id = p.customer_id LEFT JOIN users u ON u.id = t.created_by WHERE t.id = ?",
              (tid,), one=True)
    if not t:
        abort(404)
    return t


def _readings(tid):
    return query("SELECT r.*, u.name AS who FROM pressure_readings r LEFT JOIN users u ON u.id = r.created_by"
                 " WHERE r.test_id = ? ORDER BY r.created_at, r.id", (tid,))


def _set_timer(tid, minutes):
    start = now_utc()
    execute("UPDATE pressure_tests SET timer_minutes = ?, timer_started_at = ?, next_check_at = ?, notified_at = NULL"
            " WHERE id = ?", (minutes, iso(start), iso(start + timedelta(minutes=minutes)), tid))


@bp.route("/")
@require("druktest", LEZEN)
def index():
    view = request.args.get("view", "lopend")
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    if view in ("lopend", "afgerond"):
        where.append("t.status = ?")
        params.append(view)
    if q:
        where.append("(t.number LIKE ? OR t.drawing_no LIKE ? OR IFNULL(t.line_no,'') LIKE ? OR IFNULL(p.number,'') LIKE ?)")
        params += [f"%{q}%"] * 4
    sql = ("SELECT t.*, p.number AS project_no, (SELECT pressure FROM pressure_readings r WHERE r.test_id = t.id"
           " ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS last_pressure FROM pressure_tests t"
           " LEFT JOIN projects p ON p.id = t.project_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE t.status WHEN 'lopend' THEN 0 ELSE 1 END, COALESCE(t.next_check_at, t.created_at) DESC LIMIT 300"
    return render_template("druktest/index.html", tests=query(sql, params), view=view, q=q)


@bp.route("/nieuw", methods=["GET", "POST"])
@require("druktest", BEWERKEN)
def new():
    projects = query("SELECT p.id, p.number, p.name, c.name AS customer FROM projects p LEFT JOIN customers c"
                     " ON c.id = p.customer_id WHERE p.status = 'actief' ORDER BY p.number DESC")
    last = query("SELECT gauge_id, gauge_cal_date FROM pressure_tests WHERE created_by = ? ORDER BY id DESC LIMIT 1",
                 (g.user["id"],), one=True)
    if request.method == "POST":
        pressure = to_float(request.form.get("test_pressure"))
        start_p = to_float(request.form.get("start_pressure"), pressure)
        if not _f("drawing_no") or pressure is None:
            flash("Tekeningnummer en testdruk zijn verplicht.", "error")
            return render_template("druktest/new.html", t=request.form, projects=projects, PARTS=PARTS, MEDIA=MEDIA,
                                   presets=[(m, _label(m)) for m in _presets()])
        photos = [f for f in request.files.getlist("photos") if f and f.filename]
        if not photos:
            flash("Maak minimaal één startfoto van de manometer als bewijs.", "error")
            return render_template("druktest/new.html", t=request.form, projects=projects, PARTS=PARTS, MEDIA=MEDIA,
                                   presets=[(m, _label(m)) for m in _presets()])
        number = next_number("pressure_tests", "DT")
        now = now_iso()
        tid = execute(
            "INSERT INTO pressure_tests (number, project_id, drawing_no, drawing_rev, line_no, part, medium, test_pressure,"
            " max_drop, gauge_id, gauge_cal_date, executor, witness, remarks, status, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'lopend', ?, ?)",
            (number, to_int(request.form.get("project_id")), _f("drawing_no"), _f("drawing_rev"), _f("line_no"),
             _f("part") or PARTS[1], _f("medium") or MEDIA[0], pressure, to_float(request.form.get("max_drop")),
             _f("gauge_id"), _f("gauge_cal_date"), _f("executor"), _f("witness"), _f("remarks"), g.user["id"], now))
        rid = execute("INSERT INTO pressure_readings (test_id, kind, pressure, temperature, note, created_by, created_at)"
                      " VALUES (?, 'start', ?, ?, ?, ?, ?)",
                      (tid, start_p, to_float(request.form.get("temperature")), _f("start_note"), g.user["id"], now))
        save_uploads("pressure_reading", rid)
        minutes = to_int(request.form.get("timer"))
        if minutes:
            _set_timer(tid, minutes)
        audit("gestart", "pressure_test", tid, f"{number} {fmt_num(start_p, 2)} bar")
        flash(f"Druktest {number} gestart." + (f" Timer loopt: {_label(minutes)}." if minutes else ""), "ok")
        return redirect(url_for("druktest.detail", tid=tid))
    t = {"project_id": request.args.get("project"), "executor": g.user["name"], "part": "mantel", "medium": "lucht",
         "gauge_id": last["gauge_id"] if last else "", "gauge_cal_date": last["gauge_cal_date"] if last else ""}
    return render_template("druktest/new.html", t=t, projects=projects, PARTS=PARTS, MEDIA=MEDIA,
                           presets=[(m, _label(m)) for m in _presets()])


@bp.route("/<int:tid>")
@require("druktest", LEZEN)
def detail(tid):
    t = _test(tid)
    readings = _readings(tid)
    rfiles = {r["id"]: files_for("pressure_reading", r["id"]) for r in readings}
    start = readings[0]["pressure"] if readings else None
    last = readings[-1]["pressure"] if readings else None
    drop = (start - last) if (start is not None and last is not None) else None
    within = drop is None or t["max_drop"] is None or drop <= t["max_drop"] + 1e-9
    history = query("SELECT a.*, u.name AS who FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
                    " WHERE a.entity = 'pressure_test' AND a.entity_id = ? ORDER BY a.id DESC", (tid,))
    pdfs = files_for("pressure_test", tid, "pdf")
    return render_template("druktest/detail.html", t=t, readings=readings, rfiles=rfiles, drop=drop, within=within,
                           presets=[(m, _label(m)) for m in _presets()], label=_label, history=history,
                           files=files_for("pressure_test", tid), pdf=pdfs[-1] if pdfs else None,
                           sharepoint=_sp_available(t))


def _sp_available(t):
    return sharepoint_configured() or bool(t["project_sp"] and not t["project_sp_missing"] and sp.connected(get_db()))


def _require_open(t):
    if t["status"] != "lopend":
        flash("Deze druktest is afgerond en vergrendeld.", "error")
        return False
    return True


@bp.route("/<int:tid>/timer", methods=["POST"])
@require("druktest", BEWERKEN)
def timer(tid):
    t = _test(tid)
    if not _require_open(t):
        return redirect(url_for("druktest.detail", tid=tid))
    minutes = to_int(request.form.get("minutes")) or to_int(request.form.get("custom"))
    if not minutes or minutes < 1:
        flash("Kies een tijd.", "error")
    else:
        _set_timer(tid, minutes)
        audit("timer gestart", "pressure_test", tid, _label(minutes))
        flash(f"Timer gestart: {_label(minutes)}. Je krijgt een melding als je terug moet naar de leiding.", "ok")
    return redirect(url_for("druktest.detail", tid=tid))


@bp.route("/<int:tid>/controle", methods=["GET", "POST"])
@require("druktest", BEWERKEN)
def reading(tid):
    t = _test(tid)
    if not _require_open(t):
        return redirect(url_for("druktest.detail", tid=tid))
    readings = _readings(tid)
    if request.method == "POST":
        p = to_float(request.form.get("pressure"))
        photos = [f for f in request.files.getlist("photos") if f and f.filename]
        if p is None:
            flash("Vul de afgelezen druk in.", "error")
            return render_template("druktest/reading.html", t=t, readings=readings, presets=[(m, _label(m)) for m in _presets()])
        if not photos:
            flash("Maak een foto van de manometer als bewijs.", "error")
            return render_template("druktest/reading.html", t=t, readings=readings, presets=[(m, _label(m)) for m in _presets()])
        rid = execute("INSERT INTO pressure_readings (test_id, kind, pressure, temperature, note, created_by, created_at)"
                      " VALUES (?, 'controle', ?, ?, ?, ?, ?)",
                      (tid, p, to_float(request.form.get("temperature")), _f("note"), g.user["id"], now_iso()))
        save_uploads("pressure_reading", rid)
        minutes = to_int(request.form.get("timer"))
        if minutes:
            _set_timer(tid, minutes)
        else:
            execute("UPDATE pressure_tests SET next_check_at = NULL, timer_started_at = NULL WHERE id = ?", (tid,))
        audit("controle", "pressure_test", tid, f"{fmt_num(p, 2)} bar")
        flash("Controle vastgelegd." + (f" Nieuwe timer: {_label(minutes)}." if minutes else ""), "ok")
        return redirect(url_for("druktest.detail", tid=tid))
    return render_template("druktest/reading.html", t=t, readings=readings, presets=[(m, _label(m)) for m in _presets()])


@bp.route("/<int:tid>/afronden", methods=["GET", "POST"])
@require("druktest", BEWERKEN)
def finish(tid):
    t = _test(tid)
    if not _require_open(t):
        return redirect(url_for("druktest.detail", tid=tid))
    readings = _readings(tid)
    start = readings[0]["pressure"] if readings else None
    if request.method == "POST":
        p = to_float(request.form.get("pressure"))
        result = request.form.get("result")
        sig = request.form.get("signature")
        name = _f("signed_name")
        photos = [f for f in request.files.getlist("photos") if f and f.filename]
        err = None
        if p is None:
            err = "Vul de einddruk in."
        elif result not in ("goedgekeurd", "afgekeurd"):
            err = "Kies het oordeel: goedgekeurd of afgekeurd."
        elif not photos:
            err = "Maak een eindfoto van de manometer als bewijs."
        elif not name or not sig:
            err = "Vul je naam in en zet je handtekening."
        if err:
            flash(err, "error")
            return render_template("druktest/finish.html", t=t, readings=readings, start=start)
        now = now_iso()
        rid = execute("INSERT INTO pressure_readings (test_id, kind, pressure, temperature, note, created_by, created_at)"
                      " VALUES (?, 'eind', ?, ?, ?, ?, ?)",
                      (tid, p, to_float(request.form.get("temperature")), _f("note"), g.user["id"], now))
        save_uploads("pressure_reading", rid)
        save_signature("pressure_test", tid, sig)
        remarks = _f("remarks")
        execute("UPDATE pressure_tests SET status = 'afgerond', result = ?, remarks = COALESCE(?, remarks), finished_at = ?,"
                " finished_by = ?, signed_name = ?, next_check_at = NULL WHERE id = ?",
                (result, remarks, now, g.user["id"], name, tid))
        audit("afgerond", "pressure_test", tid, result)
        pdf, fname = _make_pdf(tid)
        save_bytes("pressure_test", tid, pdf, fname, kind="pdf", mime="application/pdf")
        msg = f"Druktest afgerond: {result}. PDF-rapport gemaakt."
        if _sp_available(t):
            ok, spmsg = _to_sharepoint(tid, pdf, fname)
            msg += " " + spmsg + "."
        flash(msg, "ok")
        return redirect(url_for("druktest.detail", tid=tid))
    return render_template("druktest/finish.html", t=t, readings=readings, start=start)


def _make_pdf(tid):
    t = dict(_test(tid))
    readings = [dict(r) for r in _readings(tid)]
    project = None
    if t["project_id"]:
        project = {"number": t["project_no"], "name": t["project_name"], "customer": t["customer"]}
    rphotos = {r["id"]: [(file_path(f), f["caption"]) for f in files_for("pressure_reading", r["id"], "photo")]
               for r in readings}
    general = [(file_path(f), f["caption"]) for f in files_for("pressure_test", tid, "photo")]
    sigs = files_for("pressure_test", tid, "signature")
    users = {u["id"]: u["name"] for u in query("SELECT id, name FROM users")}
    pdf = pressure_test_pdf(t, project, readings, rphotos, general, file_path(sigs[-1]) if sigs else None, users)
    date = (t.get("finished_at") or t["created_at"])[:10]
    fname = safe_filename(f"Druktest {t['number']} {t['drawing_no']} {date}.pdf")
    return pdf, fname


def _to_sharepoint(tid, pdf, fname):
    t = _test(tid)
    res = sp.upload_document(get_db(), t["project_id"], sp.setting(get_db(), "sp_sub_druktest"), fname, pdf)
    if res is not None:
        ok, msg = res
    elif not sharepoint_configured():
        ok, msg = False, "Geen gekoppelde projectmap in SharePoint"
    else:
        root = get_setting("sharepoint_root") or "Projecten"
        base = t["sharepoint_path"] or (f"{root}/{t['project_no']}" if t["project_no"] else f"{root}/Druktesten zonder project")
        ok, msg = upload_to_sharepoint(pdf, fname, f"{base}/Druktesten", t["project_no"] or "", "druktest")
    execute("UPDATE pressure_tests SET sharepoint_status = ? WHERE id = ?", (msg, tid))
    audit("sharepoint", "pressure_test", tid, msg)
    return ok, msg


@bp.route("/<int:tid>/rapport.pdf")
@require("druktest", LEZEN)
def report(tid):
    t = _test(tid)
    pdfs = files_for("pressure_test", tid, "pdf")
    if t["status"] == "afgerond" and pdfs:
        f = pdfs[-1]
        with open(file_path(f), "rb") as fh:
            data = fh.read()
        return Response(data, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{f["filename"]}"'})
    pdf, fname = _make_pdf(tid)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="CONCEPT {fname}"'})


@bp.route("/<int:tid>/sharepoint", methods=["POST"])
@require("druktest", BEWERKEN)
def resend(tid):
    t = _test(tid)
    pdfs = files_for("pressure_test", tid, "pdf")
    if t["status"] != "afgerond" or not pdfs:
        abort(400)
    with open(file_path(pdfs[-1]), "rb") as fh:
        ok, msg = _to_sharepoint(tid, fh.read(), pdfs[-1]["filename"])
    flash(msg + ".", "ok" if ok else "error")
    return redirect(url_for("druktest.detail", tid=tid))


@bp.route("/<int:tid>/fotos", methods=["POST"])
@require("druktest", BEWERKEN)
def add_photos(tid):
    t = _test(tid)
    if not _require_open(t):
        return redirect(url_for("druktest.detail", tid=tid))
    n = len(save_uploads("pressure_test", tid, caption=_f("caption")))
    audit("foto's toegevoegd", "pressure_test", tid, str(n))
    flash(f"{n} foto('s) toegevoegd.", "ok")
    return redirect(url_for("druktest.detail", tid=tid))


@bp.route("/<int:tid>/heropenen", methods=["POST"])
@require("druktest", BEHEER)
def reopen(tid):
    t = _test(tid)
    reason = _f("reason")
    if not reason:
        flash("Geef een reden op voor het heropenen.", "error")
        return redirect(url_for("druktest.detail", tid=tid))
    execute("UPDATE pressure_tests SET status = 'lopend', result = NULL, finished_at = NULL WHERE id = ?", (tid,))
    audit("heropend", "pressure_test", tid, reason)
    flash("Druktest heropend. Het vorige rapport blijft bewaard; bij opnieuw afronden komt er een nieuwe versie.", "info")
    return redirect(url_for("druktest.detail", tid=tid))


@bp.route("/<int:tid>/verwijderen", methods=["POST"])
@require("druktest", BEHEER)
def delete(tid):
    t = _test(tid)
    for r in _readings(tid):
        for f in files_for("pressure_reading", r["id"]):
            delete_file(f["id"])
    for f in files_for("pressure_test", tid):
        delete_file(f["id"])
    execute("DELETE FROM pressure_tests WHERE id = ?", (tid,))
    audit("verwijderd", "pressure_test", tid, t["number"])
    flash(f"Druktest {t['number']} verwijderd.", "ok")
    return redirect(url_for("druktest.index"))
