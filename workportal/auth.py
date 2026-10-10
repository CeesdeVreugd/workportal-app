import secrets
from datetime import timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, g, flash, make_response

from .db import query, execute
from .mail import send_mail, code_mail_html, smtp_configured
from .permissions import load_permissions
from .util import (now_iso, now_utc, parse_iso, iso, hash_secret, hash_pin, check_pin, get_setting,
                   to_int, audit)

bp = Blueprint("auth", __name__)

DEVICE_COOKIE = "wp_device"
CODE_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
MAX_PIN_ATTEMPTS = 5


def _verify_days():
    return to_int(get_setting("verify_days"), 14)


def _unlock_hours():
    return to_int(get_setting("unlock_hours"), 12)


def _pin_len():
    return max(4, to_int(get_setting("pin_min_length"), 4))


KNOWN_COOKIE = "wp_known"   # andere gebruikers die op dit apparaat zijn ingelogd (hun apparaat-tokens)
MAX_KNOWN = 8


def _cookie_opts():
    return dict(max_age=400 * 24 * 3600, httponly=True, samesite="Lax", secure=request.is_secure)


def known_tokens():
    return [t for t in (request.cookies.get(KNOWN_COOKIE) or "").split() if t][:MAX_KNOWN]


def known_devices():
    """Andere gebruikers op dit apparaat (met pincode), zonder de huidige."""
    cur = request.cookies.get(DEVICE_COOKIE)
    out, seen = [], set()
    for t in known_tokens():
        if t == cur:
            continue
        d = query("SELECT d.id, d.user_id, d.pin_hash, d.verified_at, u.name, u.email FROM devices d JOIN users u ON u.id = d.user_id"
                  " WHERE d.token_hash = ? AND u.active = 1", (hash_secret(t),), one=True)
        if d and d["user_id"] not in seen:
            seen.add(d["user_id"])
            out.append(d)
    return out


def _switch_cookies(resp, new_token, keep_current=True, drop=None):
    """Maak new_token het actieve apparaat-token; het huidige gaat naar de lijst met bekende gebruikers."""
    cur = request.cookies.get(DEVICE_COOKIE)
    known = known_tokens()
    if keep_current and cur and cur != new_token:
        known = [cur] + known
    known = [t for i, t in enumerate(known) if t and t != new_token and t != drop and t not in known[:i]][:MAX_KNOWN]
    if new_token:
        resp.set_cookie(DEVICE_COOKIE, new_token, **_cookie_opts())
    else:
        resp.delete_cookie(DEVICE_COOKIE)
    if known:
        resp.set_cookie(KNOWN_COOKIE, " ".join(known), **_cookie_opts())
    else:
        resp.delete_cookie(KNOWN_COOKIE)
    return resp


def current_device():
    tok = request.cookies.get(DEVICE_COOKIE)
    if not tok:
        return None
    return query("SELECT * FROM devices WHERE token_hash = ?", (hash_secret(tok),), one=True)


def device_verified(device):
    if not device or not device["verified_at"]:
        return False
    ver = parse_iso(device["verified_at"])
    return ver and now_utc() - ver < timedelta(days=_verify_days())


def load_logged_in_user():
    uid, did = session.get("uid"), session.get("did")
    if not uid or not did:
        return
    device = current_device()
    ok = device is not None and device["id"] == did and device["user_id"] == uid and device_verified(device)
    unlocked = parse_iso(session.get("unlocked_at"))
    if ok and unlocked and now_utc() - unlocked < timedelta(hours=_unlock_hours()):
        user = query("SELECT u.*, (SELECT group_concat(name, ', ') FROM (SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id ORDER BY r.sort)) AS role_name FROM users u"
                     " WHERE u.id = ? AND u.active = 1", (uid,), one=True)
        if user:
            g.user = user
            g.perms = load_permissions(user)
            g.device = device
            return
    for k in ("uid", "did", "unlocked_at"):
        session.pop(k, None)


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.dashboard")


def _unlock(user_id, device_id):
    session.permanent = True
    session["uid"] = user_id
    session["did"] = device_id
    session["unlocked_at"] = now_iso()
    execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), user_id))
    execute("UPDATE devices SET last_used = ?, pin_attempts = 0 WHERE id = ?", (now_iso(), device_id))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user and request.method == "GET" and not request.args.get("nieuw"):
        return redirect(_safe_next(request.args.get("next")))
    device = current_device()
    if request.method == "GET" and device and device["pin_hash"] and device_verified(device) \
            and not request.args.get("ander"):
        return redirect(url_for("auth.pin", next=request.args.get("next")))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        session["login_email"] = email
        session["login_next"] = request.form.get("next") or ""
        user = query("SELECT * FROM users WHERE email = ? AND active = 1", (email,), one=True)
        if user:
            recent = query("SELECT COUNT(*) c FROM login_codes WHERE user_id = ? AND created_at > ?",
                           (user["id"], iso(now_utc() - timedelta(minutes=15))), one=True)["c"]
            if recent >= 5:
                flash("Te veel codes aangevraagd. Wacht een kwartier en probeer het opnieuw.", "error")
                return render_template("auth/login.html", email=email)
            code = f"{secrets.randbelow(1_000_000):06d}"
            execute("UPDATE login_codes SET used = 1 WHERE user_id = ? AND used = 0", (user["id"],))
            execute("INSERT INTO login_codes (user_id, code_hash, expires_at, created_at) VALUES (?,?,?,?)",
                    (user["id"], hash_secret(code), iso(now_utc() + timedelta(minutes=CODE_MINUTES)), now_iso()))
            send_mail(user["email"], f"Je inlogcode voor WorkPortal: {code}",
                      f"Hallo {user['name']},\n\nJe inlogcode voor WorkPortal is: {code}\n\n"
                      f"De code is {CODE_MINUTES} minuten geldig.",
                      code_mail_html(user["name"], code, CODE_MINUTES))
        # Altijd dezelfde melding, zodat niet te zien is welke adressen bestaan
        return redirect(url_for("auth.code"))
    return render_template("auth/login.html", email="" if request.args.get("nieuw") else session.get("login_email", ""),
                           next=request.args.get("next", ""), can_back=bool(device or known_tokens()))


def _mask(email):
    if "@" not in email:
        return email
    name, dom = email.split("@", 1)
    return f"{name[:1]}{'•' * max(2, len(name) - 1)}@{dom}"


@bp.route("/login/code", methods=["GET", "POST"])
def code():
    email = session.get("login_email")
    if not email:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        entered = "".join(ch for ch in (request.form.get("code") or "") if ch.isdigit())
        user = query("SELECT * FROM users WHERE email = ? AND active = 1", (email,), one=True)
        row = None
        if user:
            row = query("SELECT * FROM login_codes WHERE user_id = ? AND used = 0 ORDER BY id DESC LIMIT 1",
                        (user["id"],), one=True)
        if not row or parse_iso(row["expires_at"]) < now_utc() or row["attempts"] >= MAX_CODE_ATTEMPTS:
            flash("Deze code is verlopen of ongeldig. Vraag een nieuwe code aan.", "error")
            return render_template("auth/code.html", masked=_mask(email))
        if hash_secret(entered) != row["code_hash"]:
            execute("UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            left = MAX_CODE_ATTEMPTS - row["attempts"] - 1
            flash(f"Onjuiste code. Nog {left} poging(en)." if left > 0 else "Te veel pogingen. Vraag een nieuwe code aan.", "error")
            return render_template("auth/code.html", masked=_mask(email))
        execute("UPDATE login_codes SET used = 1 WHERE id = ?", (row["id"],))

        device = current_device()
        token = None
        if not (device and device["user_id"] == user["id"]):
            for t in known_tokens():  # deze gebruiker was al eens ingelogd op dit apparaat
                d = query("SELECT * FROM devices WHERE token_hash = ?", (hash_secret(t),), one=True)
                if d and d["user_id"] == user["id"]:
                    device, token = d, t
                    break
        if device and device["user_id"] == user["id"]:
            execute("UPDATE devices SET verified_at = ?, pin_attempts = 0 WHERE id = ?", (now_iso(), device["id"]))
            did = device["id"]
            has_pin = bool(device["pin_hash"])
        else:
            token = secrets.token_urlsafe(40)
            did = execute("INSERT INTO devices (user_id, token_hash, verified_at, user_agent, created_at) VALUES (?,?,?,?,?)",
                          (user["id"], hash_secret(token), now_iso(), (request.user_agent.string or "")[:250], now_iso()))
            has_pin = False
        session.pop("login_email", None)
        nxt = session.pop("login_next", "")
        g.user = user
        audit("login", "user", user["id"], "e-mailcode")
        if has_pin:
            _unlock(user["id"], did)
            resp = make_response(redirect(_safe_next(nxt)))
        else:
            session["pending_did"] = did
            session["pending_uid"] = user["id"]
            session["login_next"] = nxt
            resp = make_response(redirect(url_for("auth.pin_set")))
        if token:
            _switch_cookies(resp, token)
        return resp
    return render_template("auth/code.html", masked=_mask(email), dev_mode=not smtp_configured(),
                           can_back=bool(current_device() or known_tokens()))


@bp.route("/pin/instellen", methods=["GET", "POST"])
def pin_set():
    did, uid = session.get("pending_did"), session.get("pending_uid")
    if not did:
        return redirect(url_for("auth.login"))
    min_len = _pin_len()
    if request.method == "POST":
        p1 = (request.form.get("pin") or "").strip()
        p2 = (request.form.get("pin2") or "").strip()
        if not p1.isdigit() or len(p1) < min_len or len(p1) > 8:
            flash(f"Kies een pincode van {min_len} tot 8 cijfers.", "error")
        elif p1 != p2:
            flash("De pincodes zijn niet gelijk.", "error")
        elif len(set(p1)) == 1 or p1 in "0123456789" or p1 in "9876543210":
            flash("Kies een minder voorspelbare pincode (niet 1111 of 1234).", "error")
        else:
            execute("UPDATE devices SET pin_hash = ?, pin_attempts = 0 WHERE id = ?", (hash_pin(p1), did))
            session.pop("pending_did", None)
            session.pop("pending_uid", None)
            _unlock(uid, did)
            flash("Pincode ingesteld. Op dit apparaat log je voortaan in met je pincode.", "ok")
            return redirect(_safe_next(session.pop("login_next", "")))
    return render_template("auth/pin_set.html", min_len=min_len)


@bp.route("/pin", methods=["GET", "POST"])
def pin():
    device = current_device()
    if not device or not device["pin_hash"] or not device_verified(device):
        if device and device["pin_hash"] and not device_verified(device):
            flash(f"Je moet je elke {_verify_days()} dagen opnieuw verifiëren met een e-mailcode.", "info")
        return redirect(url_for("auth.login", ander=1, next=request.args.get("next")))
    user = query("SELECT * FROM users WHERE id = ? AND active = 1", (device["user_id"],), one=True)
    if not user:
        return redirect(url_for("auth.login", ander=1))
    if request.method == "POST":
        entered = (request.form.get("pin") or "").strip()
        if check_pin(entered, device["pin_hash"]):
            _unlock(user["id"], device["id"])
            return redirect(_safe_next(request.form.get("next")))
        attempts = device["pin_attempts"] + 1
        if attempts >= MAX_PIN_ATTEMPTS:
            execute("UPDATE devices SET pin_hash = NULL, verified_at = NULL, pin_attempts = 0 WHERE id = ?", (device["id"],))
            flash("Te vaak een onjuiste pincode. Log opnieuw in met een e-mailcode.", "error")
            session["login_email"] = user["email"]
            return redirect(url_for("auth.login", ander=1))
        execute("UPDATE devices SET pin_attempts = ? WHERE id = ?", (attempts, device["id"]))
        flash(f"Onjuiste pincode. Nog {MAX_PIN_ATTEMPTS - attempts} poging(en).", "error")
    return render_template("auth/pin.html", user=user, next=request.args.get("next", ""), others=known_devices())


@bp.route("/wissel", methods=["GET", "POST"])
def switch_user():
    """Andere gebruiker op dit apparaat: kies wie, daarna diens pincode."""
    if request.method == "POST":
        did = to_int(request.form.get("device"))
        for t in known_tokens():
            d = query("SELECT * FROM devices WHERE token_hash = ?", (hash_secret(t),), one=True)
            if d and d["id"] == did:
                for k in ("uid", "did", "unlocked_at"):
                    session.pop(k, None)
                return _switch_cookies(make_response(redirect(url_for("auth.pin", next=request.form.get("next") or None))), t)
        flash("Deze gebruiker is niet (meer) bekend op dit apparaat.", "error")
        return redirect(url_for("auth.switch_user"))
    device = current_device()
    me = query("SELECT d.id, d.user_id, u.name, u.email FROM devices d JOIN users u ON u.id = d.user_id WHERE d.id = ?",
               (device["id"],), one=True) if device else None
    return render_template("auth/switch.html", me=me, others=known_devices(), next=request.args.get("next", ""),
                           logged_in=bool(g.user), back=_safe_next(request.args.get("next")))


@bp.route("/wissel/nieuw")
def switch_new():
    """Andere gebruiker toevoegen: naar het e-mailscherm (de huidige gebruiker blijft ingelogd tot de nieuwe binnen is)."""
    return redirect(url_for("auth.login", ander=1, nieuw=1))


@bp.route("/uitloggen", methods=["POST"])
def logout():
    for k in ("uid", "did", "unlocked_at"):
        session.pop(k, None)
    return redirect(url_for("auth.pin"))


@bp.route("/apparaat-vergeten", methods=["POST"])
def forget_device():
    """Deze gebruiker van dit apparaat halen; zijn er andere gebruikers op dit apparaat, dan naar de keuzelijst."""
    device = current_device()
    cur = request.cookies.get(DEVICE_COOKIE)
    if device:
        execute("DELETE FROM devices WHERE id = ?", (device["id"],))
    session.clear()
    others = known_devices()
    if others:
        nxt = next(t for t in known_tokens() if query("SELECT 1 FROM devices WHERE token_hash = ?", (hash_secret(t),), one=True))
        return _switch_cookies(make_response(redirect(url_for("auth.switch_user"))), nxt, keep_current=False, drop=cur)
    resp = make_response(redirect(url_for("auth.login", ander=1)))
    return _switch_cookies(resp, None, keep_current=False, drop=cur)
