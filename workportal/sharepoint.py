"""Koppeling met de Projecten-map in SharePoint via Microsoft Graph.

Structuur in SharePoint:
    <Projectenmap>/<Klantmap>/<ordernummer>-<omschrijving>/(Tekeningen, Werkvoorbereiding, ...)

- Nieuwe ordermappen (8 cijfers, streepje, omschrijving) worden automatisch
  een project in WorkPortal; bestaande projecten met hetzelfde nummer worden
  gekoppeld in plaats van dubbel aangemaakt.
- Een project dat in WorkPortal wordt aangemaakt krijgt een map in de juiste
  klantmap. Power Automate herkent die map en kopieert het sjabloon, precies
  zoals bij een handmatig aangemaakte map.
- Monteurs kunnen de projectmap alleen-lezen bekijken in WorkPortal.

Nodig: dezelfde app-registratie als voor de mail (GRAPH_TENANT_ID, GRAPH_CLIENT_ID,
GRAPH_CLIENT_SECRET) met toepassingsmachtiging Sites.Selected en schrijfrecht op
de site van de Projecten-map.
"""
import difflib
import os
import re
import threading
import time
import traceback
import unicodedata
from urllib.parse import urlparse, parse_qs, unquote, quote

import requests

from .util import now_iso

GRAPH = "https://graph.microsoft.com/v1.0"
PROJECT_RE = re.compile(r"^\s*(\d{8})\s*-\s*(.+?)\s*$")
NUMBER_RE = re.compile(r"^\d{8}$")
SMALL_UPLOAD = 4 * 1024 * 1024
CHUNK = 320 * 1024 * 10  # veelvoud van 320 KiB, zoals Graph vereist

DEFAULTS = {
    "sp_auto_create": "1",
    "sp_existing_status": "actief",
    "sp_sub_werkbon": "Service",
    "sp_sub_druktest": "Druktesten",
}

_lock = threading.Lock()


class GraphError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------ Graph-client

class Graph:
    def _headers(self):
        from .mail import _graph_token
        return {"Authorization": f"Bearer {_graph_token()}"}

    def _req(self, method, url, auth=True, **kw):
        if not url.startswith("http"):
            url = GRAPH + url
        kw.setdefault("timeout", 30)
        for attempt in range(3):
            headers = dict(kw.pop("headers", {}) or {})
            if auth:
                headers.update(self._headers())
            r = requests.request(method, url, headers=headers, **kw)
            if r.status_code in (429, 503, 504) and attempt < 2:
                time.sleep(min(int(r.headers.get("Retry-After", "3") or 3), 15))
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message") or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            raise GraphError(r.status_code, msg)
        return r

    def _json(self, method, url, **kw):
        r = self._req(method, url, **kw)
        return r.json() if r.content else {}

    def _all(self, url, **kw):
        out = []
        while url:
            js = self._json("GET", url, **kw)
            out.extend(js.get("value", []))
            url = js.get("@odata.nextLink")
            kw.pop("params", None)
        return out

    @staticmethod
    def _p(path):
        return "/".join(quote(s, safe="") for s in path.split("/") if s)

    def get_site(self, host, site_path):
        return self._json("GET", f"/sites/{host}:/{site_path}" if site_path else f"/sites/{host}")

    def drives(self, site_id):
        return self._all(f"/sites/{site_id}/drives")

    def item(self, drive, item_id):
        return self._json("GET", f"/drives/{drive}/items/{item_id}")

    def item_by_path(self, drive, path, base_item=None):
        if not path:
            return self.item(drive, base_item or "root")
        if base_item:
            return self._json("GET", f"/drives/{drive}/items/{base_item}:/{self._p(path)}")
        return self._json("GET", f"/drives/{drive}/root:/{self._p(path)}")

    def children(self, drive, item_id, relpath=""):
        url = (f"/drives/{drive}/items/{item_id}:/{self._p(relpath)}:/children" if relpath
               else f"/drives/{drive}/items/{item_id}/children")
        return self._all(url, params={"$top": "999"})

    def create_folder(self, drive, parent_id, name):
        return self._json("POST", f"/drives/{drive}/items/{parent_id}/children",
                          json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def rename(self, drive, item_id, name):
        return self._json("PATCH", f"/drives/{drive}/items/{item_id}", json={"name": name})

    def delta(self, drive, link=None):
        """Geeft (items, nieuwe deltaLink). Zonder link: alleen een startpunt ('nu')."""
        url = link or f"/drives/{drive}/root/delta?token=latest"
        items = []
        while True:
            js = self._json("GET", url)
            items.extend(js.get("value", []))
            if js.get("@odata.nextLink"):
                url = js["@odata.nextLink"]
                continue
            return items, js.get("@odata.deltaLink")

    def download(self, drive, base_item, relpath):
        meta = self.item_by_path(drive, relpath, base_item)
        url = meta.get("@microsoft.graph.downloadUrl")
        if not url:
            raise GraphError(404, "Geen downloadlink")
        r = requests.get(url, stream=True, timeout=60)
        if r.status_code >= 400:
            raise GraphError(r.status_code, "Download mislukt")
        return meta, r

    def upload(self, drive, parent_id, name, data):
        target = f"/drives/{drive}/items/{parent_id}:/{quote(name, safe='')}:"
        if len(data) <= SMALL_UPLOAD:
            return self._json("PUT", target + "/content", params={"@microsoft.graph.conflictBehavior": "replace"},
                              data=data, timeout=120)
        sess = self._json("POST", target + "/createUploadSession",
                          json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        url, total, pos, res = sess["uploadUrl"], len(data), 0, {}
        while pos < total:
            chunk = data[pos:pos + CHUNK]
            r = self._req("PUT", url, auth=False, data=chunk, timeout=120,
                          headers={"Content-Range": f"bytes {pos}-{pos + len(chunk) - 1}/{total}"})
            pos += len(chunk)
            if r.content:
                res = r.json()
        return res


def client():
    return Graph()


# ------------------------------------------------------------------ instellingen

def configured():
    return all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"))


def setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row["value"] is not None:
        return row["value"]
    return DEFAULTS.get(key)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def connected(conn):
    return configured() and bool(setting(conn, "sp_root_id"))


def _ctx(conn):
    return setting(conn, "sp_drive_id"), setting(conn, "sp_root_id")


# ------------------------------------------------------------------ hulpfuncties

LEGAL = {"bv", "nv", "vof", "cv", "gmbh", "ltd", "bvba", "sa", "holding"}


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()
    s = s.replace("&", " en ").replace("b.v.", "bv").replace("v.o.f.", "vof").replace("n.v.", "nv")
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in LEGAL]
    return "".join(words)


def folder_name(number, name):
    """Mapnaam zoals Power Automate hem herkent: <ordernummer>-<omschrijving>."""
    desc = re.sub(r'["*:<>?/\\|#%]+', " ", name or "")
    desc = re.sub(r"\s+", " ", desc).strip().rstrip(".").strip()
    return f"{number}-{desc}"[:200].rstrip(". ")


def safe_rel(relpath):
    parts = [p for p in (relpath or "").replace("\\", "/").split("/") if p]
    for p in parts:
        if p in (".", "..") or p.startswith("~"):
            raise ValueError("Ongeldig pad")
    return "/".join(parts)


def _is_folder(item):
    return "folder" in item and "deleted" not in item


def _parent(item):
    return (item.get("parentReference") or {}).get("id")


# ------------------------------------------------------------------ verbinden

def resolve_url(url, g=None):
    """Zoekt site, documentbibliotheek en map bij een geplakte SharePoint-link."""
    g = g or client()
    u = urlparse(url.strip())
    if not u.netloc.endswith("sharepoint.com"):
        raise ValueError("Plak de link van de Projecten-map in SharePoint (…sharepoint.com/…).")
    qs = parse_qs(u.query)
    path = unquote(qs["id"][0]) if qs.get("id") else unquote(u.path)
    segs = [s for s in path.split("/") if s]
    if segs and segs[-1].lower().endswith(".aspx"):
        segs = segs[:-1]
        if segs and segs[-1].lower() == "forms":
            segs = segs[:-1]
    if len(segs) >= 2 and segs[0].lower() in ("sites", "teams"):
        site_path, rest = "/".join(segs[:2]), segs[2:]
    else:
        site_path, rest = "", segs
    site = g.get_site(u.netloc, site_path)
    best, best_len = None, -1
    full = [s.lower() for s in (site_path.split("/") if site_path else []) + rest]
    for d in g.drives(site["id"]):
        dsegs = [s.lower() for s in unquote(urlparse(d.get("webUrl", "")).path).split("/") if s]
        n = len(dsegs)
        if n <= len(full) and dsegs == full[:n] and n > best_len:
            best, best_len = d, n
    if not best:
        raise ValueError("Documentbibliotheek niet gevonden in deze link.")
    site_n = len(site_path.split("/")) if site_path else 0
    inner = rest[best_len - site_n:]
    item = g.item_by_path(best["id"], "/".join(inner))
    if "folder" not in item:
        raise ValueError("De link verwijst niet naar een map.")
    return {"site_id": site["id"], "drive_id": best["id"], "root_id": item["id"],
            "name": item.get("name") or best.get("name"), "web_url": item.get("webUrl") or url,
            "site_name": site.get("displayName") or site.get("name")}


def connect(conn, url, existing_status="actief", g=None):
    """Controleert de link en slaat de map op. De eerste volledige synchronisatie start daarna op de achtergrond."""
    g = g or client()
    info = resolve_url(url, g)
    for k, v in (("sp_url", url.strip()), ("sp_site_id", info["site_id"]), ("sp_drive_id", info["drive_id"]),
                 ("sp_root_id", info["root_id"]), ("sp_root_name", info["name"]), ("sp_root_web", info["web_url"]),
                 ("sp_site_name", info["site_name"] or ""), ("sp_delta", ""), ("sp_existing_status", existing_status)):
        set_setting(conn, k, v)
    return info


def summary(st):
    if not st:
        return ""
    labels = (("klantmappen", "nieuwe klantmap(pen)"), ("nieuw", "nieuw(e) project(en)"),
              ("gekoppeld", "bestaand(e) project(en) gekoppeld"), ("bijgewerkt", "bijgewerkt"),
              ("weg", "map(pen) niet meer gevonden"))
    parts = [f"{st[k]} {label}" for k, label in labels if st.get(k)]
    return (", ".join(parts) + ".") if parts else "Geen wijzigingen."


def sync_background(db_path, full=True, initial=False):
    """Draait een (volledige) synchronisatie in een aparte thread; de voortgang staat in de instellingen."""
    from .db import raw_connection

    def run():
        conn = raw_connection(db_path)
        try:
            set_setting(conn, "sp_running", now_iso())
            try:
                st = sync(conn, full=full, initial=initial)
                set_setting(conn, "sp_last_result", f"{now_iso()}|{summary(st)}")
            except Exception as exc:
                traceback.print_exc()
                set_setting(conn, "sp_last_result", f"{now_iso()}|Mislukt: {getattr(exc, 'message', None) or exc}"[:500])
        finally:
            set_setting(conn, "sp_running", "")
            conn.close()

    t = threading.Thread(target=run, daemon=True, name="wp-sharepoint-sync")
    t.start()
    return t


def disconnect(conn):
    for k in ("sp_root_id", "sp_delta"):
        set_setting(conn, k, "")


# ------------------------------------------------------------------ synchroniseren

def _match_customer(conn, name):
    key = norm(name)
    if not key:
        return None
    hits = set()
    for r in conn.execute("SELECT id, name, short_name FROM customers"):
        if norm(r["name"]) == key or (r["short_name"] and norm(r["short_name"]) == key):
            hits.add(r["id"])
    return hits.pop() if len(hits) == 1 else None


def _upsert_folder(conn, item, stats):
    row = conn.execute("SELECT * FROM sp_folders WHERE item_id = ?", (item["id"],)).fetchone()
    if row:
        conn.execute("UPDATE sp_folders SET name = ?, web_url = ?, missing = 0, updated_at = ? WHERE item_id = ?",
                     (item["name"], item.get("webUrl"), now_iso(), item["id"]))
        return row["customer_id"]
    cid = _match_customer(conn, item["name"])
    conn.execute("INSERT INTO sp_folders (item_id, name, web_url, customer_id, auto, updated_at) VALUES (?,?,?,?,?,?)",
                 (item["id"], item["name"], item.get("webUrl"), cid, 1 if cid else 0, now_iso()))
    stats["klantmappen"] += 1
    return cid


def _upsert_project(conn, item, parent_id, stats, status):
    m = PROJECT_RE.match(item.get("name") or "")
    if not m:
        return
    number, desc = m.group(1), m.group(2)
    f = conn.execute("SELECT customer_id FROM sp_folders WHERE item_id = ?", (parent_id,)).fetchone()
    cust = f["customer_id"] if f else None
    row = conn.execute("SELECT * FROM projects WHERE sp_item_id = ?", (item["id"],)).fetchone()
    if row:
        new_number = row["number"]
        if number != row["number"] and not conn.execute(
                "SELECT 1 FROM projects WHERE number = ? AND id <> ?", (number, row["id"])).fetchone():
            new_number = number
        new_name = desc if (row["sp_name"] is None or row["name"] == row["sp_name"]) else row["name"]
        changed = (new_number, new_name, parent_id) != (row["number"], row["name"], row["sp_parent_id"]) or row["sp_missing"]
        conn.execute("UPDATE projects SET number = ?, name = ?, sp_name = ?, sp_parent_id = ?, sp_web_url = ?, sp_missing = 0,"
                     " customer_id = COALESCE(?, customer_id) WHERE id = ?",
                     (new_number, new_name, desc, parent_id, item.get("webUrl"), cust, row["id"]))
        if changed:
            stats["bijgewerkt"] += 1
        return
    row = conn.execute("SELECT * FROM projects WHERE number = ? AND sp_item_id IS NULL ORDER BY id LIMIT 1", (number,)).fetchone()
    if row:
        conn.execute("UPDATE projects SET sp_item_id = ?, sp_parent_id = ?, sp_name = ?, sp_web_url = ?, sp_missing = 0,"
                     " customer_id = COALESCE(customer_id, ?) WHERE id = ?",
                     (item["id"], parent_id, desc, item.get("webUrl"), cust, row["id"]))
        stats["gekoppeld"] += 1
        return
    if setting(conn, "sp_auto_create") == "0":
        return
    conn.execute("INSERT INTO projects (number, name, customer_id, status, created_at, source, sp_item_id, sp_parent_id,"
                 " sp_name, sp_web_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (number, desc, cust, status, now_iso(), "sharepoint", item["id"], parent_id, desc, item.get("webUrl")))
    stats["nieuw"] += 1


def _new_stats():
    return {"klantmappen": 0, "nieuw": 0, "gekoppeld": 0, "bijgewerkt": 0, "weg": 0}


def _full_scan(conn, g, status):
    drive, root = _ctx(conn)
    stats = _new_stats()
    # startpunt voor de wijzigingen vóór het scannen, zodat er niets tussendoor valt
    _, link = g.delta(drive)
    folders = [i for i in g.children(drive, root) if _is_folder(i)]
    seen_f = {f["id"] for f in folders}
    for f in folders:
        _upsert_folder(conn, f, stats)
    conn.execute("UPDATE sp_folders SET missing = 1 WHERE item_id NOT IN (%s)" % ",".join("?" * len(seen_f)) if seen_f
                 else "UPDATE sp_folders SET missing = 1", tuple(seen_f))
    seen_p = set()
    for f in folders:
        try:
            kids = g.children(drive, f["id"])
        except GraphError as exc:
            print(f"[WorkPortal] SharePoint: map {f['name']} overgeslagen ({exc.status})", flush=True)
            seen_p.update(r["sp_item_id"] for r in conn.execute("SELECT sp_item_id FROM projects WHERE sp_parent_id = ?", (f["id"],)))
            continue
        for k in kids:
            if _is_folder(k) and PROJECT_RE.match(k.get("name") or ""):
                seen_p.add(k["id"])
                _upsert_project(conn, k, f["id"], stats, status)
    for r in conn.execute("SELECT id, sp_item_id FROM projects WHERE sp_item_id IS NOT NULL AND sp_missing = 0").fetchall():
        if r["sp_item_id"] not in seen_p:
            conn.execute("UPDATE projects SET sp_missing = 1 WHERE id = ?", (r["id"],))
            stats["weg"] += 1
    conn.commit()
    set_setting(conn, "sp_delta", link or "")
    set_setting(conn, "sp_last_full", now_iso())
    return stats


def _delta(conn, g):
    drive, root = _ctx(conn)
    link = setting(conn, "sp_delta")
    if not link:
        return _full_scan(conn, g, "actief")
    try:
        items, new_link = g.delta(drive, link)
    except GraphError as exc:
        if exc.status in (410, 400, 404):
            return _full_scan(conn, g, "actief")
        raise
    stats = _new_stats()
    folder_ids = {r["item_id"] for r in conn.execute("SELECT item_id FROM sp_folders WHERE missing = 0")}
    for it in items:
        if it.get("id") == root:
            continue
        if "deleted" in it:
            if it["id"] in folder_ids:
                conn.execute("UPDATE sp_folders SET missing = 1 WHERE item_id = ?", (it["id"],))
                folder_ids.discard(it["id"])
        elif _is_folder(it) and _parent(it) == root:
            _upsert_folder(conn, it, stats)
            folder_ids.add(it["id"])
        elif _is_folder(it) and it["id"] in folder_ids:
            # klantmap verplaatst naar een andere plek
            conn.execute("UPDATE sp_folders SET missing = 1 WHERE item_id = ?", (it["id"],))
            folder_ids.discard(it["id"])
    for it in items:
        proj = conn.execute("SELECT id FROM projects WHERE sp_item_id = ?", (it.get("id"),)).fetchone()
        if "deleted" in it:
            if proj:
                conn.execute("UPDATE projects SET sp_missing = 1 WHERE id = ?", (proj["id"],))
                stats["weg"] += 1
            continue
        if not _is_folder(it):
            continue
        parent = _parent(it)
        if parent in folder_ids and PROJECT_RE.match(it.get("name") or ""):
            _upsert_project(conn, it, parent, stats, "actief")
        elif proj:
            conn.execute("UPDATE projects SET sp_missing = 1 WHERE id = ?", (proj["id"],))
            stats["weg"] += 1
    conn.commit()
    if new_link:
        set_setting(conn, "sp_delta", new_link)
    return stats


def sync(conn, full=False, g=None, initial=False):
    """Synchroniseert klantmappen en projectmappen. Geeft statistieken terug."""
    if not connected(conn):
        return None
    if not _lock.acquire(timeout=180):
        raise RuntimeError("Er loopt al een synchronisatie")
    try:
        g = g or client()
        status = (setting(conn, "sp_existing_status") or "actief") if initial else "actief"
        try:
            stats = _full_scan(conn, g, status) if full else _delta(conn, g)
        except Exception as exc:
            set_setting(conn, "sp_last_error", f"{now_iso()} {getattr(exc, 'message', None) or exc}"[:500])
            raise
        set_setting(conn, "sp_last_sync", now_iso())
        set_setting(conn, "sp_last_error", "")
        if stats and (stats["nieuw"] or stats["gekoppeld"] or stats["klantmappen"]):
            print(f"[WorkPortal] SharePoint-sync: {stats}", flush=True)
        return stats
    finally:
        _lock.release()


def scheduled_sync(conn, state):
    """Vanuit de planner: elke 3 minuten wijzigingen, elke nacht (03:00) een volledige controle."""
    if not connected(conn) or setting(conn, "sp_running"):
        return
    from .util import now_utc, TZ
    now = time.time()
    local = now_utc().astimezone(TZ)
    today = local.strftime("%Y-%m-%d")
    full = local.hour == 3 and state.get("sp_full") != today
    if not full and now - state.get("sp_last", 0) < 180:
        return
    state["sp_last"] = now
    if full:
        state["sp_full"] = today
    try:
        sync(conn, full=full)
    except Exception:
        traceback.print_exc()


# ------------------------------------------------------------------ klantmappen

def customer_folder(conn, customer_id):
    if not customer_id:
        return None
    return conn.execute("SELECT * FROM sp_folders WHERE customer_id = ? AND missing = 0 ORDER BY auto, updated_at DESC LIMIT 1",
                        (customer_id,)).fetchone()


def suggestions(conn, customer_id, limit=6):
    c = conn.execute("SELECT name, short_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not c:
        return []
    keys = [k for k in (norm(c["name"]), norm(c["short_name"])) if k]
    rows = conn.execute("SELECT * FROM sp_folders WHERE missing = 0 AND customer_id IS NULL").fetchall()
    scored = []
    for r in rows:
        n = norm(r["name"])
        score = max((difflib.SequenceMatcher(None, n, k).ratio() for k in keys), default=0)
        if any(k and (k in n or n in k) for k in keys):
            score = max(score, 0.8)
        if score >= 0.5:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def link_folder(conn, item_id, customer_id):
    conn.execute("UPDATE sp_folders SET customer_id = ?, auto = 0, updated_at = ? WHERE item_id = ?",
                 (customer_id, now_iso(), item_id))
    if customer_id:
        conn.execute("UPDATE projects SET customer_id = ? WHERE sp_parent_id = ? AND customer_id IS NULL",
                     (customer_id, item_id))
    conn.commit()


def create_customer_folder(conn, customer_id, g=None):
    g = g or client()
    drive, root = _ctx(conn)
    c = conn.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    name = re.sub(r'["*:<>?/\\|#%]+', " ", c["name"]).strip().rstrip(".")
    try:
        item = g.create_folder(drive, root, name)
    except GraphError as exc:
        if exc.status != 409:
            raise
        item = g.item_by_path(drive, name, root)
    stats = _new_stats()
    _upsert_folder(conn, item, stats)
    link_folder(conn, item["id"], customer_id)
    return item


# ------------------------------------------------------------------ projectmappen

def create_project_folder(conn, pid, g=None):
    """Maakt <ordernummer>-<omschrijving> aan in de klantmap. Geeft (ok, melding)."""
    g = g or client()
    drive, _ = _ctx(conn)
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if p["sp_item_id"] and not p["sp_missing"]:
        return True, "Projectmap was al gekoppeld."
    if not NUMBER_RE.match(p["number"] or ""):
        return False, "Geen map aangemaakt: het projectnummer moet het ordernummer van 8 cijfers zijn (bijv. 20260138)."
    folder = customer_folder(conn, p["customer_id"])
    if not folder:
        return False, "nofolder"
    name = folder_name(p["number"], p["name"])
    try:
        item = g.create_folder(drive, folder["item_id"], name)
        msg = f"Projectmap '{name}' aangemaakt in {folder['name']}. Power Automate zet de sjabloonmappen erin."
    except GraphError as exc:
        if exc.status != 409:
            return False, f"Projectmap aanmaken mislukt: {exc.message}"
        item = g.item_by_path(drive, name, folder["item_id"])
        msg = f"Bestaande projectmap '{name}' gekoppeld."
    conn.execute("UPDATE projects SET sp_item_id = ?, sp_parent_id = ?, sp_name = ?, sp_web_url = ?, sp_missing = 0 WHERE id = ?",
                 (item["id"], folder["item_id"], p["name"], item.get("webUrl"), pid))
    conn.commit()
    return True, msg


def rename_project_folder(conn, pid, g=None):
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if not p["sp_item_id"] or p["sp_missing"] or not NUMBER_RE.match(p["number"] or ""):
        return None
    g = g or client()
    drive, _ = _ctx(conn)
    name = folder_name(p["number"], p["name"])
    try:
        item = g.rename(drive, p["sp_item_id"], name)
    except GraphError as exc:
        return f"Map in SharePoint niet hernoemd: {exc.message}"
    conn.execute("UPDATE projects SET sp_name = ?, sp_web_url = COALESCE(?, sp_web_url) WHERE id = ?",
                 (p["name"], item.get("webUrl"), pid))
    conn.commit()
    return f"Map in SharePoint hernoemd naar '{name}'."


def list_folder(conn, pid, relpath="", g=None):
    g = g or client()
    drive, _ = _ctx(conn)
    p = conn.execute("SELECT sp_item_id FROM projects WHERE id = ?", (pid,)).fetchone()
    items = g.children(drive, p["sp_item_id"], safe_rel(relpath))
    out = []
    for i in items:
        out.append({"name": i["name"], "folder": "folder" in i, "size": i.get("size") or 0,
                    "count": (i.get("folder") or {}).get("childCount"),
                    "modified": i.get("lastModifiedDateTime"), "mime": (i.get("file") or {}).get("mimeType"),
                    "web_url": i.get("webUrl")})
    out.sort(key=lambda x: (not x["folder"], x["name"].lower()))
    return out


def open_file(conn, pid, relpath, g=None):
    g = g or client()
    drive, _ = _ctx(conn)
    p = conn.execute("SELECT sp_item_id FROM projects WHERE id = ?", (pid,)).fetchone()
    rel = safe_rel(relpath)
    if not rel:
        raise ValueError("Geen bestand")
    return g.download(drive, p["sp_item_id"], rel)


def upload_document(conn, project_id, sub, filename, data, g=None):
    """Zet een document (werkbon, druktestrapport) direct in <projectmap>/<submap>. Geeft (ok, melding) of None."""
    if not project_id or not connected(conn):
        return None
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not p or not p["sp_item_id"] or p["sp_missing"]:
        return None
    g = g or client()
    drive, _ = _ctx(conn)
    try:
        parent = p["sp_item_id"]
        if sub:
            try:
                parent = g.create_folder(drive, p["sp_item_id"], sub)["id"]
            except GraphError as exc:
                if exc.status != 409:
                    raise
                parent = g.item_by_path(drive, sub, p["sp_item_id"])["id"]
        g.upload(drive, parent, filename, data)
        return True, f"Opgeslagen in SharePoint ({folder_name(p['number'], p['sp_name'] or p['name'])}/{sub})"
    except GraphError as exc:
        return False, f"Opslaan in SharePoint mislukt: {exc.message}"
