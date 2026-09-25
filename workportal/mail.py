import base64
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr

import requests

GRAPH_MAX_ATTACH = 3 * 1024 * 1024  # Graph sendMail: bijlagen samen max ~3 MB
_token = {"value": None, "exp": 0}
_token_lock = threading.Lock()


def graph_configured():
    return all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "MAIL_FROM"))


def smtp_configured():
    """True als er een manier is om echt te mailen (app-registratie of SMTP)."""
    return graph_configured() or bool(os.environ.get("SMTP_HOST"))


def mail_method():
    if graph_configured():
        return "Microsoft 365 (app-registratie)"
    if os.environ.get("SMTP_HOST"):
        return "SMTP"
    return None


def _graph_token():
    with _token_lock:
        if _token["value"] and _token["exp"] > time.time() + 60:
            return _token["value"]
        r = requests.post(
            f"https://login.microsoftonline.com/{os.environ['GRAPH_TENANT_ID']}/oauth2/v2.0/token",
            data={"client_id": os.environ["GRAPH_CLIENT_ID"], "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
                  "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
            timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"token aanvragen mislukt ({r.status_code}): {r.text[:300]}")
        js = r.json()
        _token["value"] = js["access_token"]
        _token["exp"] = time.time() + int(js.get("expires_in", 3600))
        return _token["value"]


def _send_graph(to, subject, text, html=None, attachments=None):
    sender = os.environ["MAIL_FROM"]
    recipients = [to] if isinstance(to, str) else list(to)
    atts, total, skipped = [], 0, []
    for name, data, mime in attachments or []:
        if total + len(data) > GRAPH_MAX_ATTACH:
            skipped.append(name)
            continue
        total += len(data)
        atts.append({"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                     "contentType": mime or "application/octet-stream",
                     "contentBytes": base64.b64encode(data).decode()})
    if skipped:
        note = "\n\n(Bijlage te groot om te mailen: " + ", ".join(skipped) + ". Te downloaden in WorkPortal.)"
        text += note
        if html:
            html += "<p>" + note.strip() + "</p>"
    message = {
        "subject": subject,
        "body": {"contentType": "HTML" if html else "Text", "content": html or text},
        "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
    }
    if atts:
        message["attachments"] = atts
    r = requests.post(f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
                      json={"message": message, "saveToSentItems": True},
                      headers={"Authorization": f"Bearer {_graph_token()}"}, timeout=60)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"sendMail gaf {r.status_code}: {r.text[:300]}")


def send_mail(to, subject, text, html=None, attachments=None):
    """Verstuurt een e-mail via de app-registratie (Microsoft Graph) of SMTP.
    Zonder instellingen wordt het bericht in het containerlog gezet (testmodus)."""
    if graph_configured():
        try:
            _send_graph(to, subject, text, html, attachments)
            return True
        except Exception as exc:  # pragma: no cover - netwerk
            print(f"[WorkPortal] E-mail via Microsoft 365 mislukt naar {to}: {exc}", flush=True)
            return False
    if not smtp_configured():
        print("=" * 60, flush=True)
        print(f"[WorkPortal] E-MAIL (mail niet ingesteld) aan: {to}", flush=True)
        print(f"Onderwerp: {subject}", flush=True)
        print(text, flush=True)
        print("=" * 60, flush=True)
        return True
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM", user)
    sender_name = os.environ.get("MAIL_FROM_NAME", "WorkPortal De Vreugd Productietechniek")
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to if isinstance(to, str) else ", ".join(to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        name, data, mime = att
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                if security == "starttls":
                    s.starttls(context=ctx)
                    s.ehlo()
                if user:
                    s.login(user, password)
                s.send_message(msg)
        return True
    except Exception as exc:  # pragma: no cover - netwerk
        print(f"[WorkPortal] E-mail versturen mislukt naar {to}: {exc}", flush=True)
        return False


def code_mail_html(name, code, minutes):
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    payoff = (f'<img src="{app_url}/static/img/payoff.png" alt="Als performance telt" width="180" style="display:block;margin-top:6px">'
              if app_url else "")
    return f"""<div style="font-family:Arial,sans-serif;max-width:480px;color:#14163A">
<div style="background:#0A0A96;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0;font-weight:bold;letter-spacing:.1em">WORKPORTAL</div>
<div style="border:1px solid #E1E5F0;border-top:0;padding:22px;border-radius:0 0 10px 10px">
<p>Hallo {name},</p>
<p>Je inlogcode voor WorkPortal is:</p>
<p style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#0A0A96;margin:18px 0">{code}</p>
<p>De code is {minutes} minuten geldig. Heb je niet geprobeerd in te loggen? Dan kun je deze mail negeren.</p>
<p style="color:#5A5F80;font-size:12px;margin-top:24px">De Vreugd Productietechniek</p>
{payoff}
</div></div>"""
