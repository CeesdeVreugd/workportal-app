import base64
import html as html_lib
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


SIGNERS = {
    "systeem": "Systeembeheer | De Vreugd Productietechniek",
    "werkvoorbereiding": "Werkvoorbereiding | De Vreugd Productietechniek",
}
GREETING = "Met vriendelijke groeten / Kind regards / Mit Freundlichen Grüßen,"


LINKEDIN_URL = os.environ.get("SIGNATURE_LINKEDIN", "")
YOUTUBE_URL = os.environ.get("SIGNATURE_YOUTUBE", "")

DISCLAIMER = [
    ["Deze e-mail is uitsluitend bestemd voor de geadresseerde(n). Het bericht kan vertrouwelijke informatie bevatten welke niet voor derden is bedoeld.",
     "Kopiëren of verstrekking aan en gebruik door anderen van de informatie in dit bericht is niet toegestaan.",
     "Meld het ons als dit bericht niet bij de juiste persoon terecht is gekomen en verwijder deze e-mail.",
     "De Vreugd Productietechniek is op geen enkele wijze aansprakelijk voor enige fout of gebrek in de inhoud van dit e-mailbericht."],
    ["Op al onze offertes, op alle opdrachten aan ons en op alle met ons gesloten overeenkomsten zijn toepasselijk de METAALUNIE voorwaarden, "
     "gedeponeerd ter Griffie van de Rechtbank te Rotterdam, zoals deze luiden volgens de laatstelijk neergelegde tekst.",
     "Alle prijzen zijn excl. B.T.W.. De leveringsvoorwaarden worden U op verzoek toegezonden",
     "Uw algemene voorwaarden worden voor nu en als voor dan uitdrukkelijk van de hand gewezen."],
    ["To all quotations, all orders placed with us and all contracts concluded with us are subject to the METAALUNIE CONDITIONS,",
     "filed at the District Court of Rotterdam, as stipulated in the latest text lodged. You can download these conditions below.",
     "Your general terms and conditions are expressly rejected for now and any time."],
]


def signature_html(signer="systeem"):
    """Handtekening zoals de bedrijfshandtekening: groet, afzender, logo, contactgegevens, social, banner en disclaimer."""
    base = os.environ.get("APP_URL", "").rstrip("/")
    img = lambda f, w, h, alt: (f'<img src="{base}/static/img/{f}" alt="{alt}" width="{w}" height="{h}" '
                                f'style="display:inline-block;border:0;vertical-align:middle">') if base else ""
    who = html_lib.escape(SIGNERS.get(signer, SIGNERS["systeem"]))
    link = "color:#0563C1;text-decoration:underline"
    li = img("mail-linkedin.png", 30, 30, "LinkedIn")
    yt = img("mail-youtube.png", 43, 30, "YouTube")
    if LINKEDIN_URL and li:
        li = f'<a href="{LINKEDIN_URL}">{li}</a>'
    if YOUTUBE_URL and yt:
        yt = f'<a href="{YOUTUBE_URL}">{yt}</a>'
    social = f'<p style="margin:14px 0 0">{li}&nbsp;&nbsp;{yt}</p>' if base else ""
    logo = f'<p style="margin:12px 0 0">{img("logo.png", 220, 51, "De Vreugd Productietechniek")}</p>' if base else ""
    banner = (f'<p style="margin:18px 0 0">{img("mail-banner.png", 560, 40, "De Vreugd Productietechniek – Als performance telt")}</p>'
              if base else "")
    disc = "".join('<p style="margin:0 0 9pt">' + "<br>".join(html_lib.escape(l) for l in block) + "</p>" for block in DISCLAIMER)
    return (f'<div style="font-family:{FONT};font-size:10pt;line-height:1.35;color:#000;margin-top:22px">'
            f'<p style="margin:0">{html_lib.escape(GREETING)}</p><p style="margin:0">&nbsp;</p>'
            f'<p style="margin:0">{who}</p>{logo}'
            f'<p style="margin:12px 0 0">Edisonring 11, 6669 NA Dodewaard<br>+31 (0)488 41 28 28<br>'
            f'<a href="mailto:werkvoorbereiding@devreugd-pt.nl" style="{link}">werkvoorbereiding@devreugd-pt.nl</a> | '
            f'<a href="https://www.devreugd-pt.nl" style="{link}">www.devreugd-pt.nl</a></p>'
            f'{social}{banner}'
            f'<div style="font-size:7.5pt;line-height:1.3;margin-top:14px">{disc}</div></div>')


def signature_text(signer="systeem"):
    disc = "\n\n".join("\n".join(b) for b in DISCLAIMER)
    return (f"\n\n{GREETING}\n\n{SIGNERS.get(signer, SIGNERS['systeem'])}\n\n"
            f"Edisonring 11, 6669 NA Dodewaard\n+31 (0)488 41 28 28\nwerkvoorbereiding@devreugd-pt.nl | www.devreugd-pt.nl\n\n{disc}")


def send_mail(to, subject, text, html=None, attachments=None, signer="systeem"):
    """Verstuurt een e-mail via de app-registratie (Microsoft Graph) of SMTP, met handtekening.
    signer: 'systeem' (Systeembeheer) of 'werkvoorbereiding' (werkbonnen e.d.).
    Zonder instellingen wordt het bericht in het containerlog gezet (testmodus)."""
    if html is None and text:
        html = text_to_html(text)
    html = card_html(html or "", signer)
    if signer:
        text = (text or "").rstrip() + signature_text(signer)
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


# Zelfde lettertype als de standaard e-mailhandtekening (Exclaimer): Calibri
FONT = "Calibri, Carlito, 'Segoe UI', Arial, sans-serif"


def code_mail_html(name, code, minutes):
    """Inhoud van de inlogmail (het kader en de handtekening komen er in send_mail omheen)."""
    name = html_lib.escape(name or "")
    return (f'<p style="margin:0 0 12px">Hallo {name},</p>'
            f'<p style="margin:0 0 12px">Je inlogcode voor WorkPortal is:</p>'
            f'<p style="font-size:26pt;font-weight:bold;letter-spacing:8px;color:#0A0A96;margin:14px 0">{code}</p>'
            f'<p style="margin:0">De code is {minutes} minuten geldig. Heb je niet geprobeerd in te loggen? Dan kun je deze mail negeren.</p>')


def card_html(inner, signer="systeem"):
    """Alle mails in hetzelfde kader: blauwe kop WORKPORTAL, inhoud en handtekening."""
    sig = signature_html(signer) if signer else ""
    return (f'<div style="font-family:{FONT};font-size:11pt;max-width:640px;color:#000000">'
            f'<div style="background:#0A0A96;color:#fff;padding:16px 22px;border-radius:10px 10px 0 0;font-weight:bold;'
            f'letter-spacing:.1em;font-size:12pt">WORKPORTAL</div>'
            f'<div style="border:1px solid #E1E5F0;border-top:0;padding:20px 22px;border-radius:0 0 10px 10px">'
            f'{inner}{sig}</div></div>')


def text_to_html(text):
    """Platte tekst als nette HTML in het huisstijl-lettertype, zodat alle mails er hetzelfde uitzien."""
    body = html_lib.escape(text or "").replace("\r\n", "\n")
    paras = "".join(f'<p style="margin:0 0 11pt">{p.replace(chr(10), "<br>")}</p>' for p in body.split("\n\n") if p.strip())
    return paras
