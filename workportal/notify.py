import os

from . import webpush
from .mail import send_mail


def vapid_keys(conn):
    """Haalt VAPID-sleutels op, of maakt ze aan bij de eerste start."""
    rows = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('vapid_private','vapid_public')")}
    if rows.get("vapid_private") and rows.get("vapid_public"):
        return rows["vapid_private"], rows["vapid_public"]
    pem, pub = webpush.generate_vapid()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('vapid_private', ?)", (pem,))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('vapid_public', ?)", (pub,))
    conn.commit()
    return pem, pub


def push_user(conn, user_id, title, body, url="/"):
    subs = conn.execute("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    if not subs:
        return 0
    pem, pub = vapid_keys(conn)
    subject = "mailto:" + (os.environ.get("MAIL_FROM") or os.environ.get("ADMIN_EMAIL") or "noreply@example.com")
    sent = 0
    for s in subs:
        status = webpush.send(s, {"title": title, "body": body, "url": url}, pem, pub, subject)
        if status in (404, 410):
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
            conn.commit()
        elif 200 <= status < 300:
            sent += 1
    return sent


def notify_user(conn, user_id, title, body, url="/", email=True):
    user = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
    if not user:
        return
    push_user(conn, user_id, title, body, url)
    if email:
        app_url = os.environ.get("APP_URL", "").rstrip("/")
        link = f"{app_url}{url}" if app_url else url
        send_mail(user["email"], title, f"Hallo {user['name']},\n\n{body}\n\nOpenen: {link}")
