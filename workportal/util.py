import hashlib
import hmac
import io
import json
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import current_app, g, session, request, abort
from PIL import Image, ImageOps

from .db import execute, query, get_db

TZ = ZoneInfo("Europe/Amsterdam")

DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
          "augustus", "september", "oktober", "november", "december"]


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(dt):
    dt = parse_iso(dt)
    return dt.astimezone(TZ) if dt else None


def fmt_dt(value, fmt="%d-%m-%Y %H:%M"):
    dt = local(value)
    return dt.strftime(fmt) if dt else ""


def fmt_date(value):
    if not value:
        return ""
    if len(value) == 10:  # YYYY-MM-DD
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            return value
    return fmt_dt(value, "%d-%m-%Y")


def long_date(dt=None):
    dt = local(dt) if dt else now_utc().astimezone(TZ)
    return f"{DAYS[dt.weekday()].capitalize()} {dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def fmt_num(value, decimals=2):
    if value is None or value == "":
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_eur(value, decimals=0):
    if value is None or value == "":
        return "–"
    v = float(value)
    sign = "− " if v < 0 else ""
    return f"{sign}€ {fmt_num(abs(v), decimals)}"


def to_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("€", "").replace(" ", "")
    if not s:
        return default
    # Nederlandse notatie: 1.234,56
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def to_int(value, default=None):
    f = to_float(value, None)
    return int(f) if f is not None else default


# ---------------------------------------------------------------- beveiliging

def hash_secret(value):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def check_pin(pin, stored):
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pin(pin, salt), stored)


def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "Formulier verlopen. Ververs de pagina en probeer opnieuw.")


# ---------------------------------------------------------------- audit & settings

def audit(action, entity=None, entity_id=None, details=None):
    uid = g.user["id"] if getattr(g, "user", None) else None
    if isinstance(details, (dict, list)):
        details = json.dumps(details, ensure_ascii=False)
    execute("INSERT INTO audit_log (user_id, action, entity, entity_id, details, created_at) VALUES (?,?,?,?,?,?)",
            (uid, action, entity, entity_id, details, now_iso()))


DEFAULT_SETTINGS = {
    "verify_days": "14",
    "unlock_hours": "12",
    "pin_min_length": "4",
    "extra_margin_pct": "5",
    "pressure_presets": "15,30,60,120,240,1440",
    "company_name": "De Vreugd Productietechniek",
    "sharepoint_root": "Projecten",
}


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row and row["value"] is not None:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def next_number(table, prefix, width=4, yearly=True):
    year = now_utc().astimezone(TZ).year
    pre = f"{prefix}-{year}-" if yearly else f"{prefix}-"
    row = query(f"SELECT number FROM {table} WHERE number LIKE ? ORDER BY id DESC LIMIT 1", (pre + "%",), one=True)
    n = 1
    if row:
        try:
            n = int(row["number"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = query(f"SELECT COUNT(*) c FROM {table}", one=True)["c"] + 1
    return f"{pre}{n:0{width}d}"


# ---------------------------------------------------------------- bestanden

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"}
ALLOWED_EXT = IMAGE_EXT | {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".txt", ".csv", ".dwg", ".dxf", ".step", ".stp", ".zip"}


def upload_dir():
    d = current_app.config["UPLOAD_DIR"]
    os.makedirs(d, exist_ok=True)
    return d


def save_bytes(entity, entity_id, data, filename, kind="attachment", mime=None, caption=None):
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    stored = f"{uuid.uuid4().hex}{ext}"
    sub = now_utc().strftime("%Y%m")
    os.makedirs(os.path.join(upload_dir(), sub), exist_ok=True)
    rel = f"{sub}/{stored}"
    with open(os.path.join(upload_dir(), rel), "wb") as fh:
        fh.write(data)
    uid = g.user["id"] if getattr(g, "user", None) else None
    return execute(
        "INSERT INTO files (entity, entity_id, kind, filename, stored_name, mime, size, caption, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (entity, entity_id, kind, filename, rel, mime, len(data), caption, uid, now_iso()))


def save_image(entity, entity_id, storage, kind="photo", caption=None, max_side=2000):
    """Verkleint een foto (max ~2000px, JPEG) en slaat hem op."""
    raw = storage.read()
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
            else:
                bg.paste(im.convert("RGB"))
            im = bg
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
        data = out.getvalue()
        name = os.path.splitext(storage.filename or "foto")[0] + ".jpg"
        return save_bytes(entity, entity_id, data, name, kind=kind, mime="image/jpeg", caption=caption)
    except Exception:
        # geen herkenbare afbeelding: als bijlage bewaren
        return save_bytes(entity, entity_id, raw, storage.filename or "bestand", kind="attachment",
                          mime=storage.mimetype, caption=caption)


def save_uploads(entity, entity_id, field="photos", kind="photo", caption=None):
    ids = []
    for f in request.files.getlist(field):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in IMAGE_EXT or (f.mimetype or "").startswith("image/"):
            fid = save_image(entity, entity_id, f, kind=kind, caption=caption)
        else:
            if ext not in ALLOWED_EXT:
                continue
            fid = save_bytes(entity, entity_id, f.read(), f.filename, kind="attachment", mime=f.mimetype, caption=caption)
        if fid:
            ids.append(fid)
    return ids


def save_signature(entity, entity_id, data_url):
    """Handtekening uit een canvas (data:image/png;base64,...)."""
    import base64
    if not data_url or "," not in data_url:
        return None
    try:
        data = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None
    if len(data) < 200:
        return None
    return save_bytes(entity, entity_id, data, "handtekening.png", kind="signature", mime="image/png")


def files_for(entity, entity_id, kind=None):
    if kind:
        return query("SELECT * FROM files WHERE entity=? AND entity_id=? AND kind=? ORDER BY id", (entity, entity_id, kind))
    return query("SELECT * FROM files WHERE entity=? AND entity_id=? ORDER BY id", (entity, entity_id))


def file_path(row):
    return os.path.join(upload_dir(), row["stored_name"])


def delete_file(fid):
    row = query("SELECT * FROM files WHERE id=?", (fid,), one=True)
    if not row:
        return
    try:
        os.remove(file_path(row))
    except OSError:
        pass
    execute("DELETE FROM files WHERE id=?", (fid,))


def safe_filename(name):
    keep = "-_.() "
    s = "".join(c if c.isalnum() or c in keep else "_" for c in name).strip()
    return s[:150] or "bestand"


def row_dict(row):
    return dict(row) if row is not None else None


def rows_dicts(rows):
    return [dict(r) for r in rows]


def db_tx():
    return get_db()


def in_hours(hours):
    return iso(now_utc() + timedelta(hours=hours))
