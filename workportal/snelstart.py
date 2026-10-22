"""Koppeling met SnelStart (B2B-API) – alleen lezen.

Sleutels alleen in Portainer (omgevingsvariabelen):
  SNELSTART_SUBSCRIPTION_KEY  abonnementssleutel uit het SnelStart Developer Portal (Primaire sleutel)
  SNELSTART_CLIENT_KEY        koppelingssleutel uit SnelStart Web (Koppelingen > Maatwerk)
  SNELSTART_API_BASE          optioneel, standaard https://b2bapi.snelstart.nl/v2
"""
import os
import threading
import time

import requests

AUTH_URL = "https://auth.snelstart.nl/b2b/token"
_tok = {"value": None, "exp": 0}
_lock = threading.Lock()


class SnelStartError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f"{status}: {text[:300]}")
        self.status = status


def configured():
    return bool(os.environ.get("SNELSTART_SUBSCRIPTION_KEY") and os.environ.get("SNELSTART_CLIENT_KEY"))


def _base():
    return (os.environ.get("SNELSTART_API_BASE") or "https://b2bapi.snelstart.nl/v2").rstrip("/")


def _token(force=False):
    with _lock:
        if not force and _tok["value"] and _tok["exp"] > time.time() + 60:
            return _tok["value"]
        r = requests.post(AUTH_URL, data={"grant_type": "clientkey", "clientkey": os.environ["SNELSTART_CLIENT_KEY"]},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        if r.status_code != 200:
            raise SnelStartError(r.status_code, "token aanvragen mislukt (koppelingssleutel?): " + r.text)
        js = r.json()
        _tok["value"] = js["access_token"]
        _tok["exp"] = time.time() + int(js.get("expires_in", 3600))
        return _tok["value"]


def get(path, params=None):
    """GET op de SnelStart-API; bij een verlopen token één keer opnieuw."""
    for attempt in (0, 1):
        r = requests.get(_base() + "/" + path.lstrip("/"), params=params, timeout=30,
                         headers={"Authorization": f"Bearer {_token(force=attempt == 1)}",
                                  "Ocp-Apim-Subscription-Key": os.environ["SNELSTART_SUBSCRIPTION_KEY"]})
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code >= 400:
            raise SnelStartError(r.status_code, r.text)
        return r.json() if r.content else None
    return None


def test():
    """Verbinding testen: (ok, melding). Leest alleen de administratiegegevens en één relatie."""
    if not configured():
        return False, "Niet ingesteld: zet SNELSTART_SUBSCRIPTION_KEY en SNELSTART_CLIENT_KEY in Portainer."
    try:
        _token(force=True)
    except SnelStartError as e:
        return False, f"Inloggen bij SnelStart mislukt ({e.status}). Controleer de koppelingssleutel (SnelStart Web > Koppelingen > Maatwerk)."
    except Exception as e:  # noqa: BLE001 - netwerk
        return False, f"SnelStart niet bereikbaar: {str(e)[:200]}"
    name = None
    try:
        info = get("companyinfo")
        if isinstance(info, dict):
            name = info.get("bedrijfsnaam") or info.get("naam") or info.get("administratieNaam")
    except SnelStartError:
        pass
    try:
        rel = get("relaties", {"$top": 1})
    except SnelStartError as e:
        if e.status in (401, 403):
            return False, (f"Ingelogd, maar de API weigert ({e.status}). Controleer de abonnementssleutel (Developer Portal, "
                           f"Primaire sleutel) en of het pakket inZicht/inControle is.")
        return False, f"Ingelogd, maar relaties lezen mislukt ({e.status}): {str(e)[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, f"SnelStart niet bereikbaar: {str(e)[:200]}"
    n = len(rel) if isinstance(rel, list) else "?"
    return True, "Verbinding met SnelStart werkt" + (f" – administratie: {name}" if name else "") + f" (testlezing: {n} relatie)."
