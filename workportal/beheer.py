import json
import os
import tempfile

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, send_file, current_app

from .db import query, execute, backup
from .mail import smtp_configured, send_mail, mail_method
from .integrations import sharepoint_configured, nacalc_configured
from .permissions import require, MODULES, MODULE_KEYS, MODULE_GROUPS, BEHEER
from .seed import CATEGORIES, WORKTYPES
from .util import now_iso, audit, get_setting, set_setting, to_int, to_float, DEFAULT_SETTINGS

bp = Blueprint("beheer", __name__, url_prefix="/beheer")


def _f(name):
    v = request.form.get(name)
    return v.strip() if v and v.strip() else None


@bp.route("/")
@require("beheer", BEHEER)
def users():
    rows = query("SELECT u.*, r.name AS role_name, (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id) AS n_devices"
                 " FROM users u LEFT JOIN roles r ON r.id = u.role_id ORDER BY u.active DESC, u.name")
    return render_template("beheer/users.html", users=rows, roles=query("SELECT * FROM roles ORDER BY sort"))


@bp.route("/gebruiker/nieuw", methods=["GET", "POST"])
@bp.route("/gebruiker/<int:uid>", methods=["GET", "POST"])
@require("beheer", BEHEER)
def user(uid=None):
    u = query("SELECT * FROM users WHERE id = ?", (uid,), one=True) if uid else None
    if uid and not u:
        abort(404)
    roles = query("SELECT * FROM roles ORDER BY sort")
    if request.method == "POST":
        email = (_f("email") or "").lower()
        name = _f("name")
        if not email or "@" not in email or not name:
            flash("Vul naam en een geldig e-mailadres in.", "error")
            return render_template("beheer/user.html", u=request.form, uid=uid, roles=roles, extra={}, devices=[])
        dup = query("SELECT id FROM users WHERE email = ? AND id != ?", (email, uid or 0), one=True)
        if dup:
            flash("Er bestaat al een gebruiker met dit e-mailadres.", "error")
            return render_template("beheer/user.html", u=request.form, uid=uid, roles=roles, extra={}, devices=[])
        if g.user["is_admin"]:
            is_admin = 1 if request.form.get("is_admin") else 0
        else:
            is_admin = u["is_admin"] if u else 0  # alleen een beheerder kan beheerders maken
        if uid == g.user["id"]:
            is_admin = u["is_admin"]  # jezelf geen beheerrechten afnemen
        active = 1 if request.form.get("active") or uid == g.user["id"] else 0
        role_id = to_int(request.form.get("role_id"))
        if uid:
            execute("UPDATE users SET email=?, name=?, role_id=?, is_admin=?, active=? WHERE id=?",
                    (email, name, role_id, is_admin, active, uid))
            if not active:
                execute("DELETE FROM devices WHERE user_id = ?", (uid,))
        else:
            uid = execute("INSERT INTO users (email, name, role_id, is_admin, active, created_at) VALUES (?,?,?,?,?,?)",
                          (email, name, role_id, is_admin, active, now_iso()))
            if request.form.get("welcome"):
                app_url = os.environ.get("APP_URL", "")
                send_mail(email, "Je account voor WorkPortal",
                          f"Hallo {name},\n\nEr is een account voor je aangemaakt in WorkPortal van De Vreugd Productietechniek.\n\n"
                          f"Ga naar {app_url or 'WorkPortal'} en log in met dit e-mailadres. Je ontvangt dan een code per mail "
                          f"en stelt daarna een pincode in.\n\nGroet,\n{g.user['name']}")
        execute("DELETE FROM user_permissions WHERE user_id = ?", (uid,))
        for m in MODULE_KEYS:
            lvl = to_int(request.form.get(f"extra_{m}"), 0)
            if lvl:
                execute("INSERT INTO user_permissions (user_id, module, level) VALUES (?,?,?)", (uid, m, lvl))
        audit("gebruiker opgeslagen", "user", uid, email)
        flash("Gebruiker opgeslagen.", "ok")
        return redirect(url_for("beheer.users"))
    extra = {r["module"]: r["level"] for r in query("SELECT * FROM user_permissions WHERE user_id = ?", (uid or 0,))}
    devices = query("SELECT * FROM devices WHERE user_id = ? ORDER BY last_used DESC", (uid or 0,))
    return render_template("beheer/user.html", u=u or {"active": 1}, uid=uid, roles=roles, extra=extra, devices=devices)


@bp.route("/gebruiker/<int:uid>/apparaten-wissen", methods=["POST"])
@require("beheer", BEHEER)
def reset_devices(uid):
    execute("DELETE FROM devices WHERE user_id = ?", (uid,))
    audit("apparaten gewist", "user", uid)
    flash("Alle apparaten van deze gebruiker zijn afgemeld. Bij de volgende keer inloggen is een e-mailcode nodig.", "ok")
    return redirect(url_for("beheer.user", uid=uid))


@bp.route("/rechten", methods=["GET", "POST"])
@require("beheer", BEHEER)
def rights():
    roles = query("SELECT * FROM roles ORDER BY sort")
    if request.method == "POST":
        for r in roles:
            for m in MODULE_KEYS:
                vals = [to_int(v, 0) for v in request.form.getlist(f"{r['id']}_{m}")] or [0]
                lvl = max(0, min(3, max(vals)))
                execute("INSERT INTO role_permissions (role_id, module, level) VALUES (?,?,?)"
                        " ON CONFLICT(role_id, module) DO UPDATE SET level = excluded.level", (r["id"], m, lvl))
        audit("rechten gewijzigd", "roles")
        flash("Rechten per functierol opgeslagen.", "ok")
        return redirect(url_for("beheer.rights", rol=request.form.get("active_role")))
    matrix = {(r["role_id"], r["module"]): r["level"] for r in query("SELECT * FROM role_permissions")}
    return render_template("beheer/rights.html", roles=roles, matrix=matrix, groups=MODULE_GROUPS,
                           labels=dict(MODULES), active=to_int(request.args.get("rol")) or roles[0]["id"])


@bp.route("/instellingen", methods=["GET", "POST"])
@require("beheer", BEHEER)
def settings():
    keys = ["verify_days", "unlock_hours", "pin_min_length", "extra_margin_pct", "pressure_presets", "sharepoint_root"]
    if request.method == "POST":
        for k in keys:
            v = _f(k)
            if v is not None:
                set_setting(k, v)
        audit("instellingen gewijzigd", "settings")
        flash("Instellingen opgeslagen.", "ok")
        return redirect(url_for("beheer.settings"))
    values = {k: get_setting(k) for k in keys}
    status = {
        "smtp": smtp_configured(), "mail_method": mail_method(), "sharepoint": sharepoint_configured(), "nacalc": nacalc_configured(),
        "app_url": os.environ.get("APP_URL", ""), "admin": os.environ.get("ADMIN_EMAIL", ""),
        "data_dir": current_app.config["DATA_DIR"],
        "backups": sorted(os.listdir(current_app.config["BACKUP_DIR"]))[-5:] if os.path.isdir(current_app.config["BACKUP_DIR"]) else [],
        "db_size": os.path.getsize(current_app.config["DB_PATH"]) if os.path.exists(current_app.config["DB_PATH"]) else 0,
    }
    return render_template("beheer/settings.html", v=values, status=status)


@bp.route("/testmail", methods=["POST"])
@require("beheer", BEHEER)
def testmail():
    ok = send_mail(g.user["email"], "WorkPortal testmail", "Deze testmail laat zien dat e-mail vanuit WorkPortal werkt.")
    flash("Testmail verstuurd naar " + g.user["email"] + "." if ok else "Versturen mislukt; zie het containerlog.",
          "ok" if ok else "error")
    return redirect(url_for("beheer.settings"))


@bp.route("/backup")
@require("beheer", BEHEER)
def download_backup():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    backup(current_app.config["DB_PATH"], tmp.name)
    audit("back-up gedownload", "settings")
    return send_file(tmp.name, as_attachment=True, download_name=f"workportal-{now_iso()[:10]}.db")


@bp.route("/sjablonen", methods=["GET", "POST"])
@require("beheer", BEHEER)
def templates():
    wt = request.args.get("type") or WORKTYPES[0][0]
    row = query("SELECT * FROM calc_templates WHERE worktype = ?", (wt,), one=True) or abort(404)
    data = json.loads(row["data"] or "{}")
    if request.method == "POST":
        new = {}
        for cat, _, _ in CATEGORIES:
            lines = []
            for raw in (request.form.get(cat) or "").splitlines():
                parts = [p.strip() for p in raw.split("|")]
                if not parts or not parts[0]:
                    continue
                while len(parts) < 4:
                    parts.append("")
                lines.append({"d": parts[0], "u": parts[1] or "uur", "p": to_float(parts[2], 0) or 0,
                              "f": to_float(parts[3], 1) or 1})
            if lines:
                new[cat] = lines
        execute("UPDATE calc_templates SET data = ? WHERE worktype = ?", (json.dumps(new, ensure_ascii=False), wt))
        audit("sjabloon gewijzigd", "calc_template", row["id"], wt)
        flash(f"Sjabloon {row['name']} opgeslagen. Nieuwe calculaties gebruiken dit sjabloon.", "ok")
        return redirect(url_for("beheer.templates", type=wt))

    def fmt(v):
        s = f"{v:g}" if isinstance(v, (int, float)) else str(v)
        return s.replace(".", ",")
    text = {cat: "\n".join(f"{l['d']} | {l.get('u', '')} | {fmt(l.get('p', 0))} | {fmt(l.get('f', 1))}"
                           for l in data.get(cat, [])) for cat, _, _ in CATEGORIES}
    return render_template("beheer/templates.html", wt=wt, row=row, text=text, WORKTYPES=WORKTYPES, CATEGORIES=CATEGORIES)


@bp.route("/logboek")
@require("beheer", BEHEER)
def log():
    rows = query("SELECT a.*, u.name AS who FROM audit_log a LEFT JOIN users u ON u.id = a.user_id ORDER BY a.id DESC LIMIT 500")
    return render_template("beheer/log.html", rows=rows)
