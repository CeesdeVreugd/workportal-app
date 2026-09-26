from flask import Blueprint, render_template, request, g, send_file, abort, jsonify

from .db import query, execute, get_db
from .notify import vapid_keys, push_user
from .permissions import can
from .util import file_path, now_iso, now_utc, iso

bp = Blueprint("main", __name__)

ENTITY_MODULE = {
    "ticket": "service", "visit": "service",
    "pressure_test": "druktest", "pressure_reading": "druktest",
    "customer": "klanten", "installation": "klanten", "project": "klanten",
    "article": "kennis", "nacalc": "nacalculatie", "calc": "calculatie",
}


@bp.route("/")
def dashboard():
    data = {}
    if can("service"):
        data["open_tickets"] = query("SELECT COUNT(*) c FROM tickets WHERE status NOT IN ('afgerond','gefactureerd')", one=True)["c"]
        data["urgent"] = query("SELECT COUNT(*) c FROM tickets WHERE status NOT IN ('afgerond','gefactureerd') AND priority = 'hoog'", one=True)["c"]
        data["tickets"] = query(
            "SELECT t.*, c.name AS customer FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
            " ORDER BY CASE WHEN t.status IN ('afgerond','gefactureerd') THEN 1 ELSE 0 END, t.updated_at DESC LIMIT 6")
        data["my_tickets"] = query("SELECT COUNT(*) c FROM tickets WHERE assigned_to = ? AND status NOT IN ('afgerond','gefactureerd')",
                                   (g.user["id"],), one=True)["c"]
    if can("druktest"):
        data["tests"] = query(
            "SELECT t.*, p.number AS project_no FROM pressure_tests t LEFT JOIN projects p ON p.id = t.project_id"
            " WHERE t.status = 'lopend' ORDER BY COALESCE(t.next_check_at, '9999') LIMIT 6")
    if can("calculatie"):
        data["calcs_open"] = query("SELECT COUNT(*) c FROM calculations WHERE status IN ('concept','verstuurd')", one=True)["c"]
        data["calcs_sent"] = query("SELECT COUNT(*) c FROM calculations WHERE status = 'verstuurd'", one=True)["c"]
    if can("nacalculatie"):
        from .nacalc import latest_results
        res = latest_results()
        data["below_margin"] = [r for r in res if r["status"] in ("bad", "warn")]
        data["nacalc_count"] = len(res)
    return render_template("dashboard.html", d=data)


@bp.route("/menu")
def menu():
    return render_template("menu.html")


@bp.route("/zoeken")
def search():
    q = (request.args.get("q") or "").strip()
    res = {}
    if q:
        like = f"%{q}%"
        if can("klanten"):
            res["klanten"] = query("SELECT * FROM customers WHERE name LIKE ? OR IFNULL(short_name,'') LIKE ? OR debtor_no LIKE ? OR city LIKE ? ORDER BY name LIMIT 20",
                                   (like, like, like, like))
            res["projecten"] = query("SELECT p.*, c.name AS customer FROM projects p LEFT JOIN customers c ON c.id = p.customer_id"
                                     " WHERE p.number LIKE ? OR p.name LIKE ? ORDER BY p.created_at DESC LIMIT 20", (like, like))
            res["installaties"] = query("SELECT i.*, c.name AS customer FROM installations i JOIN customers c ON c.id = i.customer_id"
                                        " WHERE i.name LIKE ? OR i.serial LIKE ? LIMIT 20", (like, like))
        if can("service"):
            res["tickets"] = query("SELECT t.*, c.name AS customer FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
                                   " WHERE t.number LIKE ? OR t.title LIKE ? OR t.description LIKE ? ORDER BY t.updated_at DESC LIMIT 20",
                                   (like, like, like))
        if can("druktest"):
            res["druktesten"] = query("SELECT * FROM pressure_tests WHERE number LIKE ? OR drawing_no LIKE ? OR line_no LIKE ?"
                                      " ORDER BY created_at DESC LIMIT 20", (like, like, like))
        if can("calculatie"):
            res["calculaties"] = query("SELECT c.*, cu.name AS customer FROM calculations c LEFT JOIN customers cu ON cu.id = c.customer_id"
                                       " WHERE c.number LIKE ? OR c.title LIKE ? OR c.customer_text LIKE ? ORDER BY c.updated_at DESC LIMIT 20",
                                       (like, like, like))
        if can("kennis"):
            res["artikelen"] = query("SELECT * FROM articles WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? LIMIT 20", (like, like, like))
    total = sum(len(v) for v in res.values())
    return render_template("search.html", q=q, res=res, total=total)


@bp.route("/account")
def account():
    devices = query("SELECT * FROM devices WHERE user_id = ? ORDER BY last_used DESC", (g.user["id"],))
    subs = query("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id = ?", (g.user["id"],), one=True)["c"]
    return render_template("account.html", devices=devices, subs=subs)


@bp.route("/account/apparaat/<int:did>/verwijderen", methods=["POST"])
def remove_device(did):
    execute("DELETE FROM devices WHERE id = ? AND user_id = ?", (did, g.user["id"]))
    return render_template("redirect.html", url="/account")


@bp.route("/bestanden/<int:fid>")
@bp.route("/bestanden/<int:fid>/<path:name>")
def file(fid, name=None):
    row = query("SELECT * FROM files WHERE id = ?", (fid,), one=True)
    if not row:
        abort(404)
    if row["entity"] in ("project", "order_inbox"):
        if not (can("projecten") or can("orders") or can("service") or can("druktest")):
            abort(403)
    else:
        module = ENTITY_MODULE.get(row["entity"])
        if module and not can(module):
            abort(403)
    download = request.args.get("download") == "1"
    return send_file(file_path(row), mimetype=row["mime"] or None, download_name=row["filename"],
                     as_attachment=download, max_age=3600)


# ---------------------------------------------------------------- push

@bp.route("/api/push/key")
def push_key():
    _, pub = vapid_keys(get_db())
    return jsonify({"key": pub})


@bp.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    js = request.get_json(silent=True) or {}
    endpoint = js.get("endpoint")
    keys = js.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "Ongeldig abonnement"}), 400
    execute("INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth",
            (g.user["id"], endpoint, keys["p256dh"], keys["auth"], now_iso()))
    return jsonify({"ok": True})


@bp.route("/api/push/test", methods=["POST"])
def push_test():
    sent = push_user(get_db(), g.user["id"], "WorkPortal testmelding", "Pushmeldingen werken op dit apparaat.", "/account")
    return jsonify({"sent": sent})
