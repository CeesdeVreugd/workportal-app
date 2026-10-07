"""Printen via Printix Cloud Print API (https://printix.github.io/).

Instellen:
- Printix Administrator > Instellingen/API > Cloud Print API: maak een set API-gegevens (client ID + secret).
  Vereist Printix Premium; Secure Print mag niet op "Alle gebruikers moeten veilig printen" staan.
- Portainer (stack): PRINTIX_TENANT_ID, PRINTIX_CLIENT_ID, PRINTIX_CLIENT_SECRET.
- WorkPortal > Beheer > Printen: printer kiezen en automatisch printen van orderbonnen aanzetten.

Werking: job aanmaken (submit) -> PDF uploaden naar de opgegeven cloud-opslag -> completeUpload.
"""
import json
import os
import threading
import time

import requests

AUTH_URL = "https://auth.printix.net/oauth/token"
API = "https://api.printix.net/cloudprint"
_token = {"value": None, "exp": 0}
_lock = threading.Lock()

DEFAULTS = {"printix_auto_orderbon": "0", "printix_copies": "1", "printix_duplex": "NONE", "printix_color": "0"}


class PrintixError(Exception):
    pass


def configured():
    return all(os.environ.get(k) for k in ("PRINTIX_TENANT_ID", "PRINTIX_CLIENT_ID", "PRINTIX_CLIENT_SECRET"))


def setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else DEFAULTS.get(key)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def printer(conn):
    try:
        return json.loads(setting(conn, "printix_printer") or "null")
    except ValueError:
        return None


def options(conn):
    return {"copies": max(1, min(20, int(setting(conn, "printix_copies") or 1))),
            "duplex": setting(conn, "printix_duplex") or "NONE",
            "color": setting(conn, "printix_color") == "1"}


def _tenant():
    return os.environ.get("PRINTIX_TENANT_ID", "")


def token():
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 60:
            return _token["value"]
        r = requests.post(AUTH_URL, data={"grant_type": "client_credentials", "client_id": os.environ.get("PRINTIX_CLIENT_ID", ""),
                                          "client_secret": os.environ.get("PRINTIX_CLIENT_SECRET", "")}, timeout=30)
        if r.status_code >= 400:
            raise PrintixError(f"Aanmelden bij Printix mislukt ({r.status_code}). Controleer de API-gegevens.")
        js = r.json()
        _token["value"], _token["exp"] = js["access_token"], time.time() + int(js.get("expires_in") or 3600)
        return _token["value"]


def _api(method, url, **kw):
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {token()}"
    r = requests.request(method, url if url.startswith("http") else API + url, headers=headers, timeout=kw.pop("timeout", 60), **kw)
    if r.status_code == 429:
        raise PrintixError("Printix: te veel verzoeken, probeer het over een minuut opnieuw.")
    if r.status_code >= 400:
        raise PrintixError(f"Printix gaf een fout ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content else {}


def _ids_from(item):
    """printer- en wachtrij-id uit de HAL-link .../printers/{printerId}/queues/{queueId}"""
    import re
    href = ((item.get("_links") or {}).get("self") or {}).get("href") or ""
    m = re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
    if m:
        return m.group(1), m.group(2)
    return item.get("printerId") or item.get("id"), item.get("queueId")


def list_printers():
    out, page = [], 0
    while page < 20:
        js = _api("GET", f"/tenants/{_tenant()}/printers", params={"page": page, "pageSize": 50})
        items = js.get("printers")
        if items is None:  # onbekende vorm: eerste lijst in het antwoord
            items = next((v for v in js.values() if isinstance(v, list)), [])
        for it in items:
            pid, qid = _ids_from(it)
            if not pid or not qid:
                continue
            out.append({"printer_id": pid, "queue_id": qid, "name": it.get("name") or it.get("queueName") or pid,
                        "location": it.get("location") or "", "model": it.get("model") or "",
                        "status": it.get("connectionStatus") or ""})
        pg = js.get("page") or {}
        if not items or (pg.get("totalPages") is not None and page + 1 >= int(pg.get("totalPages"))) or len(items) < 50:
            break
        page += 1
    return out


_CACHE = {"at": 0, "list": None}


def cached_printers(max_age=600):
    """Printerlijst uit Printix, 10 minuten bewaard (zodat het afdrukvenster snel opent)."""
    import time as _time
    if _CACHE["list"] is None or _time.time() - _CACHE["at"] > max_age:
        _CACHE["list"] = list_printers()
        _CACHE["at"] = _time.time()
    return _CACHE["list"]


def allowed_printers(conn):
    """Printers die gebruikers bij Afdrukken kunnen kiezen (Beheer > Printen); niets aangevinkt = alle.
    De standaardprinter staat altijd in de lijst, ook als Printix even niet bereikbaar is."""
    try:
        keep = set(json.loads(setting(conn, "printix_allowed") or "[]"))
    except ValueError:
        keep = set()
    try:
        items = cached_printers()
    except (PrintixError, requests.RequestException, KeyError, ValueError):
        items = []
    out = [p for p in items if not keep or f"{p['printer_id']}|{p['queue_id']}" in keep]
    cur = printer(conn)
    if cur and cur.get("printer_id") and not any(p["printer_id"] == cur["printer_id"] and p["queue_id"] == cur["queue_id"] for p in out):
        out.insert(0, {"printer_id": cur["printer_id"], "queue_id": cur["queue_id"], "name": cur.get("name") or "Standaardprinter",
                       "location": "", "model": "", "status": ""})
    return out


def pick(conn, key):
    """Printer uit de keuze van de gebruiker ('printer_id|queue_id'), alleen als die is toegestaan; anders de standaard."""
    if key:
        for p in allowed_printers(conn):
            if f"{p['printer_id']}|{p['queue_id']}" == key:
                return p
    return printer(conn)


def print_pdf(pr, data, title, opts=None):
    """Stuurt een PDF naar de printer. pr = {"printer_id", "queue_id"}. Geeft het job-id terug."""
    if not configured():
        raise PrintixError("Printix is niet ingesteld (PRINTIX_* in de stack).")
    if not pr or not pr.get("printer_id") or not pr.get("queue_id"):
        raise PrintixError("Er is nog geen printer gekozen (Beheer > Printen).")
    opts = opts or {}
    body = {"color": bool(opts.get("color")), "duplex": opts.get("duplex") or "NONE", "page_orientation": "AUTO",
            "copies": int(opts.get("copies") or 1), "media_size": "A4", "scaling": "SHRINK", "releaseImmediately": True}
    js = _api("POST", f"/tenants/{_tenant()}/printers/{pr['printer_id']}/queues/{pr['queue_id']}/submit",
              params={"title": title[:200]}, headers={"Content-Type": "application/json", "version": "1.1"},
              data=json.dumps(body))
    job = js.get("job") or {}
    links = js.get("uploadLinks") or []
    if not links:
        raise PrintixError("Printix gaf geen uploadadres terug.")
    up = links[0]
    hdrs = dict(up.get("headers") or {})
    hdrs.setdefault("Content-Type", "application/pdf")
    r = requests.put(up["url"], data=data, headers=hdrs, timeout=120)
    if r.status_code not in (200, 201):
        raise PrintixError(f"Uploaden naar Printix mislukt ({r.status_code}).")
    done = ((js.get("_links") or {}).get("uploadCompleted") or {}).get("href") \
        or f"{API}/tenants/{_tenant()}/jobs/{job.get('id')}/completeUpload"
    _api("POST", done)
    return job.get("id")


def print_background(db_path, data, title, inbox_id=None):
    """Printen op de achtergrond (bijv. na ontvangst van een orderbon); resultaat komt in order_inbox."""
    from .db import raw_connection

    def run():
        conn = raw_connection(db_path)
        try:
            try:
                jid = print_pdf(printer(conn), data, title, options(conn))
                note, ok = f"Geprint (Printix-job {jid})", True
            except (PrintixError, requests.RequestException, KeyError, ValueError) as exc:
                note, ok = f"Printen mislukt: {exc}", False
            if inbox_id:
                from .util import now_iso
                conn.execute("UPDATE order_inbox SET printed_at = COALESCE(?, printed_at), print_note = ? WHERE id = ?",
                             (now_iso() if ok else None, note[:300], inbox_id))
                conn.commit()
        finally:
            conn.close()

    t = threading.Thread(target=run, daemon=True, name="wp-printix")
    t.start()
    return t
