"""Koppelingen via Power Automate (HTTP-triggers).

PA_SHAREPOINT_URL  -> flow 'Document naar SharePoint'
PA_NACALC_URL      -> flow 'Na-calculatie ophalen'
PA_SECRET          -> gedeelde sleutel, meegestuurd als header 'x-workportal-key'
"""
import base64
import os

import requests


def sharepoint_configured():
    return bool(os.environ.get("PA_SHAREPOINT_URL"))


def nacalc_configured():
    return bool(os.environ.get("PA_NACALC_URL"))


def _headers():
    return {"x-workportal-key": os.environ.get("PA_SECRET", ""), "Content-Type": "application/json"}


def upload_to_sharepoint(data: bytes, filename: str, folder: str, project_no: str = "", doc_type: str = ""):
    """Stuurt een document naar de flow. Geeft (ok, melding) terug."""
    if not sharepoint_configured():
        return False, "SharePoint-koppeling niet ingesteld"
    payload = {
        "bestandsnaam": filename,
        "map": folder,
        "projectnummer": project_no,
        "type": doc_type,
        "inhoudBase64": base64.b64encode(data).decode(),
    }
    try:
        r = requests.post(os.environ["PA_SHAREPOINT_URL"], json=payload, headers=_headers(), timeout=60)
        if 200 <= r.status_code < 300:
            return True, "Opgeslagen in SharePoint"
        return False, f"SharePoint-flow gaf fout {r.status_code}"
    except requests.RequestException as exc:
        return False, f"SharePoint-flow niet bereikbaar ({exc.__class__.__name__})"


def fetch_nacalc(order_no: str):
    """Vraagt de regie-Excel op bij de flow. Geeft (bytes|None, bestandsnaam|melding)."""
    if not nacalc_configured():
        return None, "Power Automate-koppeling niet ingesteld"
    try:
        r = requests.post(os.environ["PA_NACALC_URL"], json={"ordernummer": order_no}, headers=_headers(), timeout=120)
    except requests.RequestException as exc:
        return None, f"Flow niet bereikbaar ({exc.__class__.__name__})"
    if r.status_code >= 300:
        return None, f"Flow gaf fout {r.status_code}"
    ctype = r.headers.get("Content-Type", "")
    if "json" in ctype:
        try:
            js = r.json()
        except ValueError:
            return None, "Ongeldig antwoord van de flow"
        content = js.get("inhoudBase64") or js.get("$content") or js.get("contentBase64")
        if not content:
            return None, "Flow stuurde geen bestand terug"
        return base64.b64decode(content), js.get("bestandsnaam") or f"nacalculatie_{order_no}.xlsx"
    return r.content, f"nacalculatie_{order_no}.xlsx"
