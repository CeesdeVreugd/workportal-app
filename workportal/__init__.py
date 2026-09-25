import os
import secrets
from datetime import timedelta

from flask import Flask, g, session, request, redirect, url_for, render_template, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db as dbmod
from .util import (fmt_dt, fmt_date, fmt_eur, fmt_num, csrf_token, check_csrf, long_date, greeting,
                   get_setting, parse_iso, now_utc, hash_secret)
from .permissions import load_permissions, can, MODULES, LEVEL_NAMES
from .integrations import sharepoint_configured, nacalc_configured

VERSION = "1.7.2"

PUBLIC_ENDPOINTS = {"static", "sw", "manifest", "health", "favicon", "apple_icon"}


def _secret_key(data_dir):
    env = os.environ.get("SECRET_KEY") or os.environ.get("SESSION_SECRET")
    if env:
        return env
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    return key


def create_app(test_config=None, start_scheduler=True):
    app = Flask(__name__)
    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    app_url = os.environ.get("APP_URL", "")
    app.config.update(
        DATA_DIR=data_dir,
        DB_PATH=os.path.join(data_dir, "workportal.db"),
        UPLOAD_DIR=os.path.join(data_dir, "uploads"),
        BACKUP_DIR=os.path.join(data_dir, "backups"),
        SECRET_KEY=_secret_key(data_dir),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "60")) * 1024 * 1024,
        SESSION_COOKIE_NAME="wp_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app_url.startswith("https"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        APP_URL=app_url,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["BACKUP_DIR"], exist_ok=True)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    dbmod.migrate(app.config["DB_PATH"])
    from .seed import seed
    seed(app)

    app.teardown_appcontext(dbmod.close_db)

    app.jinja_env.filters.update(dt=fmt_dt, date=fmt_date, eur=fmt_eur, num=fmt_num)
    app.jinja_env.globals.update(csrf_token=csrf_token, can=can, MODULES=MODULES, LEVEL_NAMES=LEVEL_NAMES,
                                 long_date=long_date, greeting=greeting, VERSION=VERSION,
                                 sharepoint_configured=sharepoint_configured,
                                 nacalc_configured=nacalc_configured)

    from .auth import bp as auth_bp, load_logged_in_user
    from .main import bp as main_bp
    from .klanten import bp as klanten_bp
    from .service import bp as service_bp
    from .druktest import bp as druktest_bp
    from .calculatie import bp as calc_bp
    from .nacalc import bp as nacalc_bp
    from .kennis import bp as kennis_bp
    from .beheer import bp as beheer_bp
    for bp in (auth_bp, main_bp, klanten_bp, service_bp, druktest_bp, calc_bp, nacalc_bp, kennis_bp, beheer_bp):
        app.register_blueprint(bp)

    @app.before_request
    def _before():
        g.user = None
        g.perms = {}
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        check_csrf()
        load_logged_in_user()
        if request.endpoint and request.endpoint.startswith("auth."):
            return None
        if g.user is None:
            if request.path.startswith("/api/"):
                return {"error": "Niet ingelogd"}, 401
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    @app.route("/sw.js")
    def sw():
        resp = send_from_directory(os.path.join(app.root_path, "static", "js"), "sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.route("/manifest.webmanifest")
    def manifest():
        ua = request.headers.get("User-Agent", "")
        if any(k in ua for k in ("iPhone", "iPad", "iPod")):
            # iOS: effen vierkant icoon zonder doorzichtige delen (anders legt iOS er een glaseffect overheen)
            icons = [{"src": url_for("static", filename=f"img/wp-app-{n}.png"), "sizes": f"{n}x{n}",
                      "type": "image/png", "purpose": "any"} for n in (180, 192, 512)]
        else:
            # Bureaublad/Android: rond icoon
            icons = [{"src": url_for("static", filename=f"img/wp-round-{n}.png"), "sizes": f"{n}x{n}",
                      "type": "image/png", "purpose": "any"} for n in (96, 144, 192, 256, 384, 512)]
        return {
            "name": "WorkPortal - DVP",
            "short_name": "WorkPortal - DVP",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#FFFFFF",
            "theme_color": "#0A0A96",
            "id": "/",
            "scope": "/",
            "icons": icons,
        }, 200, {"Vary": "User-Agent", "Cache-Control": "no-cache"}

    @app.route("/apple-touch-icon.png")
    @app.route("/apple-touch-icon-precomposed.png")
    def apple_icon():
        return send_from_directory(os.path.join(app.root_path, "static", "img"), "apple-touch-icon.png")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(os.path.join(app.root_path, "static", "img"), "favicon.ico",
                                   mimetype="image/x-icon")

    @app.route("/health")
    def health():
        return {"status": "ok", "version": VERSION}

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, title="Geen toegang",
                               message="Je functierol heeft geen rechten voor dit onderdeel."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, title="Niet gevonden",
                               message="Deze pagina of dit item bestaat niet (meer)."), 404

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, title="Ongeldig verzoek",
                               message=getattr(e, "description", "") or "Probeer het opnieuw."), 400

    @app.errorhandler(413)
    def e413(e):
        return render_template("error.html", code=413, title="Bestand te groot",
                               message="De upload is te groot. Probeer minder of kleinere bestanden."), 413

    if start_scheduler and os.environ.get("WP_NO_SCHEDULER") != "1":
        from .scheduler import start
        start(app)

    return app
