import json
import os
import tempfile

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, send_file, current_app

import re

import requests

from . import sharepoint as sp
from .db import query, execute, backup, get_db
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
    rows = query("SELECT u.*, (SELECT group_concat(name, ', ') FROM (SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id ORDER BY r.sort)) AS role_name, (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id) AS n_devices"
                 " FROM users u ORDER BY u.active DESC, u.name")
    uroles = {}
    for r in query("SELECT ur.user_id, ro.key, ro.name FROM user_roles ur JOIN roles ro ON ro.id = ur.role_id ORDER BY ro.sort"):
        uroles.setdefault(r["user_id"], []).append(r)
    return render_template("beheer/users.html", users=rows, roles=query("SELECT * FROM roles ORDER BY sort"), uroles=uroles)


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
            return render_template("beheer/user.html", u=request.form, uid=uid, roles=roles, extra={}, devices=[],
                                   user_roles=_form_roles())
        dup = query("SELECT id FROM users WHERE email = ? AND id != ?", (email, uid or 0), one=True)
        if dup:
            flash("Er bestaat al een gebruiker met dit e-mailadres.", "error")
            return render_template("beheer/user.html", u=request.form, uid=uid, roles=roles, extra={}, devices=[],
                                   user_roles=_form_roles())
        if g.user["is_admin"]:
            is_admin = 1 if request.form.get("is_admin") else 0
        else:
            is_admin = u["is_admin"] if u else 0  # alleen een beheerder kan beheerders maken
        if uid == g.user["id"]:
            is_admin = u["is_admin"]  # jezelf geen beheerrechten afnemen
        active = 1 if request.form.get("active") or uid == g.user["id"] else 0
        role_ids = [r["id"] for r in roles if str(r["id"]) in request.form.getlist("role_ids")]
        role_id = role_ids[0] if role_ids else None
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
                          f"en stelt daarna een pincode in.")
        execute("DELETE FROM user_roles WHERE user_id = ?", (uid,))
        for rid in role_ids:
            execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, rid))
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
    user_roles = {r["role_id"] for r in query("SELECT role_id FROM user_roles WHERE user_id = ?", (uid,))} if uid else set()
    return render_template("beheer/user.html", u=u or {"active": 1}, uid=uid, roles=roles, extra=extra, devices=devices,
                           user_roles=user_roles)


def _form_roles():
    return {to_int(x) for x in request.form.getlist("role_ids")}


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
    keys = ["verify_days", "unlock_hours", "pin_min_length", "extra_margin_pct", "pressure_presets", "sharepoint_root", "cert_sender"]
    if request.method == "POST":
        for k in keys:
            v = _f(k)
            if v is not None:
                set_setting(k, v)
            elif k == "cert_sender" and k in request.form:
                set_setting(k, "")
        if request.form.get("mobile_form") == "1":
            on = set(request.form.getlist("mobile_on"))
            set_setting("mobile_off", json.dumps([m for m in PHONE_MODULES if m not in on]))
        if request.form.get("cred_form") == "1":
            set_setting("cred_mail_on", "1" if request.form.get("cred_mail_on") else "0")
            set_setting("cred_mailbox", (request.form.get("cred_mailbox") or "").strip().lower() or "factuur@devreugd-pt.nl")
            since = (request.form.get("cred_since") or "").strip()
            if since and since != (get_setting("cred_since") or "")[:10]:
                set_setting("cred_since", since + "T00:00:00Z")
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
    from .util import mobile_off
    from .crediteuren import mail_configured
    cred = {"on": get_setting("cred_mail_on") == "1", "box": get_setting("cred_mailbox") or "factuur@devreugd-pt.nl",
            "since": (get_setting("cred_since") or "")[:10], "last": get_setting("cred_last_poll"),
            "error": get_setting("cred_last_error"), "graph": mail_configured()}
    return render_template("beheer/settings.html", v=values, status=status, mobile_off=mobile_off(), cred=cred,
                           phone_modules=[(k, dict(MODULES)[k]) for k in PHONE_MODULES])


PHONE_MODULES = ["klanten", "projecten", "orders", "service", "druktest", "modellen3d", "calculatie", "nacalculatie", "crediteuren", "kennis", "beheer"]


@bp.route("/sharepoint", methods=["GET", "POST"])
@require("beheer", BEHEER)
def sharepoint():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "verbinden":
                info = sp.connect(conn, request.form.get("url") or "", request.form.get("existing_status") or "actief")
                audit("sharepoint verbonden", "settings", None, info["web_url"])
                sp.sync_background(current_app.config["DB_PATH"], full=True, initial=True)
                flash(f"Verbonden met '{info['name']}'. De klantmappen en ordermappen worden nu ingelezen; dat kan een paar minuten duren.", "ok")
            elif action == "sync":
                if sp.setting(conn, "sp_running"):
                    flash("Er loopt al een synchronisatie.", "info")
                else:
                    sp.sync_background(current_app.config["DB_PATH"], full=True)
                    flash("Volledige synchronisatie gestart.", "ok")
            elif action == "instellingen":
                sp.set_setting(conn, "sp_auto_create", "1" if request.form.get("sp_auto_create") else "0")
                for k in ("sp_sub_werkbon", "sp_sub_druktest", "sp_sub_3d"):
                    sp.set_setting(conn, k, re.sub(r'["*:<>?\\|#%]+', "", request.form.get(k) or "").strip("/ "))
                mb = re.sub(r"[^0-9]", "", request.form.get("sp_3d_cache_mb") or "")
                sp.set_setting(conn, "sp_3d_cache_mb", str(min(int(mb), 2000)) if mb else "10")
                flash("Instellingen opgeslagen.", "ok")
            elif action == "koppel":
                m = re.search(r"#(\d+)\s*$", request.form.get("customer") or "")
                cid = int(m.group(1)) if m else None
                if request.form.get("customer") and not cid:
                    flash("Kies een relatie uit de lijst.", "error")
                else:
                    sp.link_folder(conn, request.form.get("item_id"), cid)
                    flash("Klantmap gekoppeld." if cid else "Klantmap ontkoppeld.", "ok")
                return redirect(url_for("beheer.sharepoint", alle=request.form.get("alle")) + "#klantmappen")
            elif action == "ontkoppelen":
                sp.disconnect(conn)
                audit("sharepoint ontkoppeld", "settings")
                flash("SharePoint ontkoppeld. Projecten en koppelingen blijven bewaard.", "ok")
        except ValueError as exc:
            flash(str(exc), "error")
        except sp.GraphError as exc:
            hint = " Controleer of IT de app toegang heeft gegeven tot deze site (Sites.Selected)." if exc.status in (401, 403) else ""
            flash(f"SharePoint gaf een fout ({exc.status}): {exc.message}.{hint}", "error")
        except (requests.RequestException, RuntimeError) as exc:
            flash(f"SharePoint niet bereikbaar: {exc}", "error")
        return redirect(url_for("beheer.sharepoint"))
    show_all = request.args.get("alle") == "1"
    folders = query("SELECT f.*, c.name AS customer, (SELECT COUNT(*) FROM projects p WHERE p.sp_parent_id = f.item_id) AS n"
                    " FROM sp_folders f LEFT JOIN customers c ON c.id = f.customer_id WHERE f.missing = 0"
                    + ("" if show_all else " AND f.customer_id IS NULL") + " ORDER BY f.name COLLATE NOCASE")
    counts = query("SELECT (SELECT COUNT(*) FROM sp_folders WHERE missing = 0) AS folders,"
                   " (SELECT COUNT(*) FROM sp_folders WHERE missing = 0 AND customer_id IS NULL) AS unlinked,"
                   " (SELECT COUNT(*) FROM projects WHERE sp_item_id IS NOT NULL AND sp_missing = 0) AS projects,"
                   " (SELECT COUNT(*) FROM projects WHERE sp_missing = 1) AS missing,"
                   " (SELECT COUNT(*) FROM projects WHERE sp_item_id IS NOT NULL AND customer_id IS NULL) AS nocust", one=True)
    customers = query("SELECT id, name FROM customers WHERE active = 1 ORDER BY name COLLATE NOCASE")
    v = {k: sp.setting(conn, k) for k in ("sp_url", "sp_root_name", "sp_root_web", "sp_site_name", "sp_last_sync", "sp_last_full",
                                           "sp_last_error", "sp_auto_create", "sp_sub_werkbon", "sp_sub_druktest", "sp_sub_3d", "sp_3d_cache_mb", "sp_root_id",
                                           "sp_running", "sp_last_result")}
    if v["sp_last_result"] and "|" in v["sp_last_result"]:
        v["result_at"], v["result"] = v["sp_last_result"].split("|", 1)
    return render_template("beheer/sharepoint.html", v=v, configured=sp.configured(), connected=sp.connected(conn),
                           folders=folders, counts=counts, customers=customers, show_all=show_all,
                           client_id=os.environ.get("GRAPH_CLIENT_ID", ""))


@bp.route("/sharepoint/snelstart", methods=["GET", "POST"])
@require("beheer", BEHEER)
def sharepoint_snelstart():
    conn = get_db()
    if not sp.connected(conn):
        flash("SharePoint is niet gekoppeld.", "error")
        return redirect(url_for("beheer.sharepoint"))
    if request.method == "POST":
        linked = sp.rematch_folders(conn)
        n = sp.snelstart_apply(conn)
        audit("klantnummers SnelStart uit klantmappen", "customer", None, f"{n} ingevuld")
        msg = f"{n} klantnummer(s) SnelStart ingevuld."
        if linked:
            msg += f" Daarnaast {linked} klantmap(pen) automatisch aan een relatie gekoppeld."
        flash(msg, "ok")
        return redirect(url_for("beheer.sharepoint_snelstart"))
    extra = 0
    for f in query("SELECT name FROM sp_folders WHERE missing = 0 AND customer_id IS NULL"):
        if sp._match_customer(conn, f["name"]):
            extra += 1
    plan = sp.snelstart_plan(conn)
    counts = {}
    for p in plan:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return render_template("beheer/sharepoint_snelstart.html", plan=plan, counts=counts, extra=extra)


@bp.route("/dc01", methods=["GET", "POST"])
@require("beheer", BEHEER)
def dc01():
    """Sync met de Komdex-ordermappen op de server DC01 (via het script komdex-orders.ps1)."""
    from . import komdex
    if request.method == "POST":
        ids = [to_int(x) for x in request.form.getlist("ids") if to_int(x)]
        if request.form.get("action") == "opnieuw" and ids:
            execute(f"UPDATE order_inbox SET bon_received_at = NULL, note = NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
            flash(f"Orderbon wordt bij de volgende run van het script opnieuw opgehaald ({len(ids)} map(pen)).", "ok")
        elif request.form.get("action") == "printen" and ids:
            from . import printix
            from .util import file_path
            if not (printix.configured() and printix.printer(get_db())):
                flash("Printix is nog niet ingesteld (Beheer > Printen).", "error")
            else:
                n = 0
                for it in query(f"SELECT i.id, i.number, f.stored_name FROM order_inbox i JOIN files f ON f.id = i.bon_file_id"
                                f" WHERE i.id IN ({','.join('?' * len(ids))})", ids):
                    with open(file_path(it), "rb") as fh:
                        printix.print_background(current_app.config["DB_PATH"], fh.read(), f"Orderbon {it['number']}", it["id"])
                    audit("afgedrukt", "order_inbox", it["id"], f"{it['number']} · Orderbon · via Beheer > DC01")
                    n += 1
                flash(f"{n} orderbon(nen) naar de printer gestuurd." if n else "Geen orderbon gevonden bij de selectie.", "ok" if n else "error")
        elif request.form.get("action") == "nacalc" and ids:
            execute(f"UPDATE order_inbox SET nacalc_sig = NULL, nacalc_note = NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
            flash(f"Na-calculatie wordt bij de volgende run van het script opnieuw opgehaald ({len(ids)} map(pen)).", "ok")
        return redirect(url_for("beheer.dc01", filter=request.form.get("filter"), q=request.form.get("q") or None))
    flt = request.args.get("filter") or "alle"
    q = (request.args.get("q") or "").strip()
    where, params = ["1=1"], []
    if flt == "bon":
        where.append("i.bon_json IS NOT NULL")
    elif flt == "zonderbon":
        where.append("i.bon_json IS NULL AND i.missing = 0")
    elif flt == "gekoppeld":
        where.append("i.project_id IS NOT NULL")
    elif flt == "niet":
        where.append("i.project_id IS NULL AND i.missing = 0 AND i.status <> 'genegeerd'")
    elif flt == "actie":
        where.append("i.status = 'nieuw' AND i.missing = 0")
    elif flt == "weg":
        where.append("i.missing = 1")
    elif flt == "nacalc":
        where.append("i.nacalc_sig IS NOT NULL")
    else:
        flt = "alle"
    if q:
        where.append("(i.number LIKE ? OR i.path LIKE ? OR IFNULL(c.name,'') LIKE ?)")
        params += [f"%{q}%"] * 3
    rows = query("SELECT i.*, c.name AS customer, p.kind, p.name AS pname, p.number AS pnumber FROM order_inbox i"
                 " LEFT JOIN customers c ON c.id = i.customer_id LEFT JOIN projects p ON p.id = i.project_id"
                 " WHERE " + " AND ".join(where) + " ORDER BY i.number DESC LIMIT 600", params)
    items = []
    for r in rows:
        d = dict(r)
        d["bon"] = komdex.bon_of(r)
        items.append(d)
    counts = query("SELECT COUNT(*) AS alle, SUM(bon_json IS NOT NULL) AS bon, SUM(bon_json IS NULL AND missing = 0) AS zonderbon,"
                   " SUM(project_id IS NOT NULL) AS gekoppeld,"
                   " SUM(project_id IS NULL AND missing = 0 AND status <> 'genegeerd') AS niet,"
                   " SUM(status = 'nieuw' AND missing = 0) AS actie, SUM(missing = 1) AS weg,"
                   " SUM(nacalc_sig IS NOT NULL) AS nacalc, SUM(nacalc_note IS NOT NULL) AS nacalc_fout FROM order_inbox", one=True)
    return render_template("beheer/dc01.html", items=items, counts=counts, flt=flt, q=q, status=komdex.status(),
                           order_dir=sp.setting(get_db(), "komdex_order_dir"))


@bp.route("/printen", methods=["GET", "POST"])
@require("beheer", BEHEER)
def printen():
    """Printix: printer kiezen, orderbonnen automatisch printen, testpagina."""
    from . import printix
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "opslaan":
            pr = request.form.get("printer") or ""
            if pr:
                pid, qid, name = (pr.split("|", 2) + ["", ""])[:3]
                printix.set_setting(conn, "printix_printer", json.dumps({"printer_id": pid, "queue_id": qid, "name": name}))
            printix.set_setting(conn, "printix_auto_orderbon", "1" if request.form.get("auto") else "0")
            printix.set_setting(conn, "printix_copies", str(max(1, min(20, to_int(request.form.get("copies")) or 1))))
            printix.set_setting(conn, "printix_duplex", request.form.get("duplex") if request.form.get("duplex") in ("NONE", "LONG_EDGE", "SHORT_EDGE") else "NONE")
            printix.set_setting(conn, "printix_color", "1" if request.form.get("color") else "0")
            printix.set_setting(conn, "printix_allowed", json.dumps([x for x in request.form.getlist("allowed") if "|" in x]))
            printix._CACHE["list"] = None
            audit("printinstellingen", None, None, request.form.get("printer"))
            flash("Printinstellingen opgeslagen.", "ok")
        elif action == "test":
            from .pdf import test_page_pdf
            try:
                jid = printix.print_pdf(printix.printer(conn), test_page_pdf(), "WorkPortal testpagina", printix.options(conn))
                flash(f"Testpagina verstuurd naar Printix (job {jid}).", "ok")
            except (printix.PrintixError, requests.RequestException, KeyError, ValueError) as exc:
                flash(f"Testpagina printen mislukt: {exc}", "error")
        return redirect(url_for("beheer.printen"))
    printers, error = [], None
    if printix.configured():
        try:
            printers = printix.list_printers()
        except (printix.PrintixError, requests.RequestException, KeyError, ValueError) as exc:
            error = str(exc)
    recent = query("SELECT id, number, printed_at, print_note FROM order_inbox WHERE print_note IS NOT NULL"
                   " ORDER BY COALESCE(printed_at, last_seen) DESC LIMIT 15")
    return render_template("beheer/printen.html", configured=printix.configured(), printers=printers, error=error,
                           current=printix.printer(conn), auto=printix.setting(conn, "printix_auto_orderbon") == "1",
                           opts=printix.options(conn), recent=recent, allowed=set(json.loads(printix.setting(conn, "printix_allowed") or "[]")),
                           printlog=query("SELECT a.*, u.name AS who, p.kind AS pkind FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
                                          " LEFT JOIN projects p ON a.entity = 'project' AND p.id = a.entity_id"
                                          " WHERE a.action IN ('afgedrukt', 'afdrukken mislukt', 'orderbon geprint')"
                                          " ORDER BY a.id DESC LIMIT 100"))


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
