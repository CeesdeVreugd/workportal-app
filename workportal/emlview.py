"""E-mail (.eml) lezen binnen WorkPortal: kopregels, tekst (of HTML in een afgeschermd venster) en bijlagen.

Veiligheid: HTML uit een mail wordt nooit als deel van WorkPortal getoond, maar in een iframe met sandbox (geen scripts,
geen toegang tot WorkPortal) en een eigen Content-Security-Policy: geen externe afbeeldingen of verbindingen (ook geen
"leesbevestiging" via verborgen plaatjes). Ingesloten afbeeldingen (cid:) worden als data-URL meegegeven.
"""
import base64
import html
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

MAX_INLINE_IMG = 4 * 1048576
FRAME_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"


def _addr(v):
    out = []
    for name, addr in getaddresses([str(v or "")]):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr or name:
            out.append(addr or name)
    return ", ".join(out)


def _parse(data):
    if not data:
        raise ValueError("Leeg bestand.")
    try:
        return BytesParser(policy=policy.default).parsebytes(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Deze mail kan niet worden gelezen.") from exc


def _parts(msg):
    """Alle 'bladeren' van de mail in vaste volgorde; een meegestuurde mail (message/rfc822) is zelf één bijlage."""
    out = []

    def walk(p):
        if p.get_content_type() == "message/rfc822":
            out.append(p)
        elif p.is_multipart():
            for sub in p.iter_parts():
                walk(sub)
        else:
            out.append(p)
    walk(msg)
    return out


def _payload(part):
    if part.get_content_type() == "message/rfc822":
        try:
            inner = part.get_content()
            return inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(inner)
        except Exception:  # noqa: BLE001
            try:
                return part.get_payload(0).as_bytes()
            except Exception:  # noqa: BLE001
                return b""
    try:
        return part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001
        return b""


def _name(part):
    name = part.get_filename()
    if name:
        return name
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        try:
            subj = str(part.get_content().get("Subject") or "").strip()
        except Exception:  # noqa: BLE001
            subj = ""
        return (re.sub(r'[\\/:*?"<>|]+', "_", subj)[:80] or "doorgestuurde mail") + ".eml"
    return "bijlage." + (ctype.split("/")[-1] if ctype else "bin")


def _attachments(msg):
    """[(volgnummer, naam, grootte, mime, content-id)] van alle onderdelen die geen hoofdtekst zijn."""
    body_ids = set()
    for pref in (("html",), ("plain",)):
        b = msg.get_body(preferencelist=pref)
        if b is not None:
            body_ids.add(id(b))
    out = []
    for i, part in enumerate(_parts(msg)):
        if id(part) in body_ids:
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if not part.get_filename() and disp != "attachment" and not part.get("Content-ID") and ctype in ("text/plain", "text/html"):
            continue   # tekstdeel (bijv. de platte-tekstversie naast HTML)
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        out.append((i, _name(part), len(_payload(part)), ctype, cid))
    return out


def attachment(data, idx):
    """(naam, bytes, mime) van bijlage nummer idx, of ValueError."""
    msg = _parse(data)
    parts = _parts(msg)
    if not 0 <= idx < len(parts):
        raise ValueError("Bijlage niet gevonden.")
    part = parts[idx]
    ctype = part.get_content_type()
    return _name(part), _payload(part), ("message/rfc822" if ctype == "message/rfc822" else ctype)


def _clean_html(raw, cids):
    """HTML voor het afgeschermde venster: scripts/iframes/objecten eruit, cid:-plaatjes ingevuld, links in een nieuw tabblad."""
    h = re.sub(r"(?is)<(script|iframe|object|embed|applet|noscript)\b[^>]*>.*?</\1\s*>", "", raw)
    h = re.sub(r"(?is)</?(script|iframe|object|embed|applet|noscript|form|meta|base|link)\b[^>]*>", "", h)
    h = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*\2", r'\1="#"', h)

    def cid_sub(m):
        key = m.group(2).strip()
        return f'{m.group(1)}"{cids[key]}"' if key in cids else f'{m.group(1)}""'
    h = re.sub(r'(?i)(src\s*=\s*)["\']?cid:([^"\'>\s]+)["\']?', cid_sub, h)
    h = re.sub(r'(?i)(src|background)\s*=\s*["\']?https?:[^"\'>\s]*["\']?', r'\1=""', h)   # niets van internet laden
    head = (f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{FRAME_CSP}">'
            '<base target="_blank"><style>body{font-family:Calibri,Carlito,"Segoe UI",Arial,sans-serif;font-size:15px;'
            'color:#14163A;margin:16px;word-wrap:break-word}img{max-width:100%;height:auto}table{max-width:100%}</style>')
    return "<!doctype html><html><head>" + head + "</head><body>" + h + "</body></html>"


def render(data):
    """Kop, tekst/HTML en bijlagen van een .eml voor de viewer."""
    msg = _parse(data)
    date = msg.get("Date")
    try:
        when = parsedate_to_datetime(str(date)).astimezone().strftime("%d-%m-%Y %H:%M") if date else ""
    except Exception:  # noqa: BLE001
        when = str(date or "")
    atts = _attachments(msg)
    cids = {}
    budget = MAX_INLINE_IMG
    for i, name, size, ctype, cid in atts:
        if cid and ctype.startswith("image/") and size <= budget:
            try:
                _, payload, _ = attachment(data, i)
                cids[cid] = f"data:{ctype};base64," + base64.b64encode(payload).decode()
                budget -= size
            except ValueError:
                pass
    part_html = msg.get_body(preferencelist=("html",))
    part_text = msg.get_body(preferencelist=("plain",))
    html_doc, text, remote, raw_html = None, "", False, ""
    if part_html is not None:
        try:
            raw = raw_html = part_html.get_content()
            remote = bool(re.search(r'(?i)src\s*=\s*["\']?https?:', raw))
            html_doc = _clean_html(raw, cids)
        except Exception:  # noqa: BLE001
            html_doc = None
    if part_text is not None:
        try:
            text = part_text.get_content()
        except Exception:  # noqa: BLE001
            text = ""
    if html_doc is None and not text:
        text = "(Deze mail heeft geen leesbare tekst.)"
    # ingesloten plaatjes (die echt in de tekst staan) niet nog eens als bijlage; Outlook geeft ook gewone
    # bijlagen een Content-ID, die blijven dus gewoon in de lijst
    used = set(re.findall(r'(?i)cid:([^"\'>\s]+)', raw_html))
    shown = [a for a in atts if not (a[4] and a[4] in used and a[4] in cids)]
    return {
        "subject": str(msg.get("Subject") or "(geen onderwerp)"),
        "from": _addr(msg.get("From")), "to": _addr(msg.get("To")), "cc": _addr(msg.get("Cc")),
        "date": when, "html": html_doc, "text": text.replace("\r\n", "\n").strip(), "remote_images": remote,
        "attachments": [{"i": i, "name": name, "size": size, "mime": ctype} for i, name, size, ctype, _c in shown],
        "tnef": any(a[3] == "application/ms-tnef" for a in atts),
        "escape": html.escape,
    }
