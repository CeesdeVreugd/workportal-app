"""Debiteuren: verkoopfacturen en wat er nog openstaat.

Bronnen:
  * Excel 'Openstaande verkoopfacturen' uit SnelStart (handmatig importeren; eerst een voorbeeld, dan toepassen).
  * SnelStart B2B-API (alleen lezen), automatisch zodra de sleutels in Portainer staan. De Excel-import blijft
    beschikbaar als reserve.
  * De factuur-PDF uit het DigiDossier op DC01 (facturen\\<jaar>\\<factuurmap>\\*.pdf), via het script op DC01.

Statussen: 'open' (staat in de laatste lijst met openstaande posten), 'betaald' (stond open en staat er niet meer in),
'onbekend' (alleen bekend uit het DigiDossier, nog niet in een overzicht uit SnelStart gezien)."""
import base64
import binascii
import io
import json
import re
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, jsonify

from .db import query, execute, get_db
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, now_utc, TZ, to_float, audit, save_bytes

bp = Blueprint("debiteuren", __name__, url_prefix="/debiteuren")

NUM_RE = re.compile(r"(?<!\d)(\d{6,10})(?!\d)")
KLANT_RE = re.compile(r"^\s*Klant:\s*(\S+)\s*-\s*(.*?)\s*-\s*[^-]*$")
STATUS_LABEL = {"open": "Open", "betaald": "Betaald", "onbekend": "Nog niet in overzicht"}


def _today():
    return now_utc().astimezone(TZ).date()


def _d(v):
    """datum/tekst -> 'YYYY-MM-DD' of None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip().split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amount(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip().replace("€", "").replace(" ", "")
    if re.search(r",\d{1,2}$", s):
        s = s.replace(".", "").replace(",", ".")
    return to_float(s, None)


def _match_customer(conn, debtor_no, name):
    if debtor_no:
        hits = [r["id"] for r in conn.execute("SELECT id FROM customers WHERE TRIM(IFNULL(debtor_no,'')) = ?", (str(debtor_no).strip(),))]
        if len(hits) == 1:
            return hits[0]
    from .sharepoint import norm
    key = norm(name or "")
    if not key:
        return None
    hits = {r["id"] for r in conn.execute("SELECT id, name, short_name FROM customers")
            if norm(r["name"]) == key or (r["short_name"] and norm(r["short_name"]) == key)}
    return hits.pop() if len(hits) == 1 else None


# ---------------------------------------------------------------- Excel 'Openstaande verkoopfacturen'

def parse_excel(data):
    """Rijen uit de SnelStart-export. Kolommen op naam (volgorde maakt niet uit); klantkop 'Klant: 13030 - Naam - tel'."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    header, cols, out = None, {}, []
    debtor, debtor_name = None, None
    want = {"naam": "name", "factuurnummer": "number", "factuurdatum": "invoice_date", "factuurbedrag": "amount",
            "betaald": "paid", "openstaand saldo": "open_amount", "openstaand": "open_amount", "vervaldatum": "due_date"}
    for row in ws.iter_rows(values_only=True):
        cells = list(row)
        if header is None:
            low = [str(c or "").strip().lower() for c in cells]
            if "factuurnummer" in low:
                header = low
                cols = {want[h]: i for i, h in enumerate(low) if h in want}
            continue
        first = str(cells[0] or "") if cells else ""
        m = KLANT_RE.match(first)
        if m and not any(cells[1:]):
            debtor, debtor_name = m.group(1).strip(), m.group(2).strip()
            continue
        num = cells[cols["number"]] if "number" in cols and cols["number"] < len(cells) else None
        if num is None or str(num).strip() == "":
            continue
        num = str(int(num)) if isinstance(num, float) and num.is_integer() else str(num).strip()

        def col(k):
            i = cols.get(k)
            return cells[i] if i is not None and i < len(cells) else None
        out.append({"number": num, "debtor_no": debtor, "customer_name": (str(col("name") or "").strip() or debtor_name),
                    "invoice_date": _d(col("invoice_date")), "due_date": _d(col("due_date")),
                    "amount": _amount(col("amount")), "paid": _amount(col("paid")), "open_amount": _amount(col("open_amount"))})
    if header is None:
        raise ValueError("Geen kolom 'Factuurnummer' gevonden. Is dit de export 'Openstaande verkoopfacturen' uit SnelStart?")
    return out


def _max_num(rows):
    nums = [int(r["number"]) for r in rows if str(r["number"]).isdigit()]
    return max(nums) if nums else None


def preview(conn, rows):
    """Wat er gebeurt als deze lijst met openstaande posten wordt toegepast."""
    by_num = {r["number"]: r for r in rows}
    known = {r["number"]: r for r in conn.execute("SELECT * FROM sales_invoices")}
    new = [n for n in by_num if n not in known]
    changed = [n for n in by_num if n in known and (known[n]["status"] != "open"
                                                    or abs((known[n]["open_amount"] or 0) - (by_num[n]["open_amount"] or 0)) > 0.004)]
    top = _max_num(rows)
    closing = [r["number"] for r in known.values() if r["number"] not in by_num and r["status"] in ("open", "onbekend")
               and (r["status"] == "open" or (top and r["number"].isdigit() and int(r["number"]) <= top))]
    total = round(sum(r["open_amount"] or 0 for r in rows), 2)
    return {"rows": len(rows), "new": new, "changed": changed, "closing": sorted(closing), "total": total}


def apply_rows(conn, rows, source):
    """Openstaande posten toepassen: in de lijst = open; stond open (of onbekend en ouder) en niet in de lijst = betaald."""
    now = now_iso()
    pv = preview(conn, rows)
    for r in rows:
        cur = conn.execute("SELECT * FROM sales_invoices WHERE number = ?", (r["number"],)).fetchone()
        cid = (cur["customer_id"] if cur and cur["customer_id"] else None) or _match_customer(conn, r["debtor_no"], r["customer_name"])
        if cur:
            conn.execute("UPDATE sales_invoices SET customer_id = ?, debtor_no = COALESCE(?, debtor_no), customer_name = COALESCE(?, customer_name),"
                         " invoice_date = COALESCE(?, invoice_date), due_date = COALESCE(?, due_date), amount = COALESCE(?, amount),"
                         " paid = ?, open_amount = ?, status = 'open', status_source = ?, status_at = ?, paid_at = NULL, updated_at = ? WHERE id = ?",
                         (cid, r["debtor_no"], r["customer_name"], r["invoice_date"], r["due_date"], r["amount"], r["paid"],
                          r["open_amount"], source, now, now, cur["id"]))
        else:
            conn.execute("INSERT INTO sales_invoices (number, customer_id, debtor_no, customer_name, invoice_date, due_date, amount, paid,"
                         " open_amount, status, status_source, status_at, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?)",
                         (r["number"], cid, r["debtor_no"], r["customer_name"], r["invoice_date"], r["due_date"], r["amount"],
                          r["paid"], r["open_amount"], source, now, now))
    for n in pv["closing"]:
        conn.execute("UPDATE sales_invoices SET status = 'betaald', open_amount = 0, paid = COALESCE(amount, paid), status_source = ?,"
                     " status_at = ?, paid_at = ?, updated_at = ? WHERE number = ?", (source, now, now, now, n))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_last_update', ?)", (f"{now}|{source}",))
    conn.commit()
    return {"rows": pv["rows"], "new": len(pv["new"]), "changed": len(pv["changed"]), "closed": len(pv["closing"]), "total": pv["total"]}


# ---------------------------------------------------------------- SnelStart (alleen lezen)

def snelstart_rows():
    """Openstaande verkoopfacturen uit SnelStart, in hetzelfde formaat als de Excel-rijen."""
    from . import snelstart
    rows, skip, rel = [], 0, {}
    while True:
        page = snelstart.get("verkoopfacturen", {"$filter": "openstaandSaldo ne 0", "$top": 500, "$skip": skip}) or []
        for f in page:
            rid = (f.get("relatie") or {}).get("id")
            if rid and rid not in rel:
                try:
                    r = snelstart.get(f"relaties/{rid}") or {}
                    rel[rid] = (str(r.get("relatiecode") or "") or None, r.get("naam"))
                except Exception:  # noqa: BLE001 - naam is bijzaak
                    rel[rid] = (None, None)
            code, naam = rel.get(rid, (None, None))
            amount, open_amt = _amount(f.get("factuurBedrag")), _amount(f.get("openstaandSaldo"))
            rows.append({"number": str(f.get("factuurnummer") or "").strip(), "debtor_no": code, "customer_name": naam,
                         "invoice_date": _d(f.get("factuurDatum")), "due_date": _d(f.get("vervalDatum")), "amount": amount,
                         "paid": round((amount or 0) - (open_amt or 0), 2) if amount is not None else None, "open_amount": open_amt})
        if len(page) < 500:
            break
        skip += 500
    return [r for r in rows if r["number"]]


def scheduled_snelstart(conn, state):
    """Elk uur (als de sleutels er zijn) de openstaande posten uit SnelStart ophalen."""
    from . import snelstart
    if not snelstart.configured():
        return
    import time
    if time.time() - state.get("deb_snelstart", 0) < 3600:
        return
    state["deb_snelstart"] = time.time()
    try:
        rows = snelstart_rows()
        res = apply_rows(conn, rows, "snelstart")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_snelstart_status', ?)",
                     (f"{now_iso()}|ok|{res['rows']} open, {res['closed']} betaald gemeld",))
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_snelstart_status', ?)", (f"{now_iso()}|fout|{str(exc)[:300]}",))
    conn.commit()


# ---------------------------------------------------------------- factuur-PDF uit het DigiDossier

MONTHS = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7, "augustus": 8, "september": 9,
          "oktober": 10, "november": 11, "december": 12, "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
          "august": 8, "october": 10, "januar": 1, "februar": 2, "märz": 3, "mai": 5, "dezember": 12,
          "janvier": 1, "février": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7, "août": 8, "septembre": 9, "octobre": 10,
          "novembre": 11, "décembre": 12}
DATE_TXT = r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}\.?\s+[a-zäéû]+\s+\d{4})"


def _date_txt(v):
    """'20 maart 2026' / '20-03-2026' -> '2026-03-20'."""
    v = (v or "").strip()
    m = re.match(r"(\d{1,2})\.?\s+([a-zäéû]+)\s+(\d{4})", v, re.I)
    if m:
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
            except ValueError:
                return None
        return None
    return _d(v.replace("/", "-").replace(".", "-"))


def _address_block(page):
    """Linkerkolom onder 'Factuuradres:' (rechts staat het werk-/afleveradres)."""
    words = page.extract_words()
    head = next((w for w in words if re.match(r"(factuuradres|invoice\s*address|rechnungsadresse|adresse)", w["text"], re.I)), None)
    if not head:
        return []
    right = min([w["x0"] for w in words if abs(w["top"] - head["top"]) < 3 and w["x0"] > head["x1"] + 20] or [page.width / 2])
    lines = {}
    for w in words:
        if head["top"] + 5 < w["top"] < head["top"] + 120 and w["x0"] < right - 5:
            lines.setdefault(round(w["top"] / 3), []).append(w)
    out = []
    for k in sorted(lines):
        txt = " ".join(w["text"] for w in sorted(lines[k], key=lambda w: w["x0"]))
        if re.match(r"(factuurnummer|invoice|rechnung|facture)", txt, re.I):
            break
        out.append(txt)
    return out


def parse_pdf(data):
    """Gegevens van de factuur zelf: nummer, datums, bedrag, klant (factuuradres), T.a.v., onze referentie (order),
    factuur-e-mailadres als dat op de factuur staat."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
        block = _address_block(pdf.pages[0]) if pdf.pages else []
    out = {}
    m = re.search(r"(?:factuur\s*(?:nummer|nr\.?)|invoice\s*(?:number|no\.?)|rechnungs?\s*(?:nummer|nr\.?)|num[ée]ro\s*de\s*facture)\s*[:.]?\s*(\d{6,10})", text, re.I)
    if m:
        out["number"] = m.group(1)
    m = re.search(r"(?:factuurdatum|invoice\s*date|rechnungsdatum|date\s*de\s*facture)\s*[:.]?\s*" + DATE_TXT, text, re.I)
    if m:
        out["invoice_date"] = _date_txt(m.group(1))
    m = (re.search(r"(?:vervaldatum|due\s*date|f[äa]llig(?:keitsdatum)?|[ée]ch[ée]ance)\s*[:.]?\s*" + DATE_TXT, text, re.I)
         or re.search(r"(?:betaling\s+v[oó][oó]r|payment\s+(?:before|by)|zahlung\s+bis(?:\s+zum)?|paiement\s+avant\s+le)\s+" + DATE_TXT, text, re.I))
    if m:
        out["due_date"] = _date_txt(m.group(1))
    m = re.search(r"^\s*(?:totaal|total|gesamt(?:betrag)?|te betalen)\b[^\n\d€-]*€?\s*(-?\s*[\d.]+,\d{2})", text, re.I | re.M)
    if m:
        out["amount"] = _amount(m.group(1).replace(" ", ""))
    m = re.search(r"(?:onze\s*ref|our\s*ref|unsere\s*ref|notre\s*r[ée]f)[^:\n]*:\s*(\d{8})\b", text, re.I)
    if m:
        out["order_no"] = m.group(1)
    m = re.search(r"factuur[\s-]*e-?mail(?:adres)?\s*[:.]?\s*([^\s@]+@[^\s@]+\.[a-z]{2,})", text, re.I)
    if m:
        out["mail_to"] = m.group(1).strip(".,;")
    if block:
        out["customer"] = block[0].strip()
        attn = next((l for l in block[1:] if re.match(r"(t\.?a\.?v\.?|attn\.?|z\.?\s*hd\.?|[àa]\s*l'attention)", l, re.I)), None)
        if attn:
            out["attn"] = re.sub(r"^(t\.?a\.?v\.?|attn\.?|z\.?\s*hd\.?)\s*:?\s*", "", attn, flags=re.I).strip()
    return out


def _sig(js):
    pdf = js.get("pdf") or {}
    return f"{pdf.get('name')}|{pdf.get('modified') or ''}|{pdf.get('size') or ''}"[:400] if pdf.get("name") else None


def _number_of(folder):
    for s in (folder.get("name"), (folder.get("pdf") or {}).get("name")):
        m = NUM_RE.search(str(s or ""))
        if m:
            return m.group(1)
    return None


PDF_PER_RUN = 25


@bp.route("/api/dossiers", methods=["POST"])
def api_dossiers():
    """Het script op DC01 stuurt de factuurmappen (met de nieuwste PDF); WorkPortal zegt welke PDF's het wil hebben."""
    from .komdex import _key_ok
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    conn = get_db()
    now = now_iso()
    known = {r["number"]: r for r in conn.execute("SELECT number, pdf_sig, dossier_path FROM sales_invoices")}
    want, new = [], 0
    for f in (js.get("folders") or [])[:20000]:
        num, path = _number_of(f), str(f.get("path") or "")[:500]
        if not num or not path:
            continue
        cur = known.get(num)
        if not cur:
            conn.execute("INSERT INTO sales_invoices (number, dossier_path, status, created_at) VALUES (?,?, 'onbekend', ?)", (num, path, now))
            new += 1
        elif cur["dossier_path"] != path:
            conn.execute("UPDATE sales_invoices SET dossier_path = ? WHERE number = ?", (path, num))
        sig = _sig(f)
        if sig and (not cur or cur["pdf_sig"] != sig) and len(want) < PDF_PER_RUN:
            want.append(path)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_dc01_last', ?)", (f"{now}|{len(js.get('folders') or [])}|{new}",))
    conn.commit()
    return jsonify({"ok": True, "mappen": len(js.get("folders") or []), "nieuw": new, "want": want})


@bp.route("/api/factuur", methods=["POST"])
def api_pdf():
    from .komdex import _key_ok
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    conn = get_db()
    inv = conn.execute("SELECT * FROM sales_invoices WHERE dossier_path = ?", (str(js.get("path") or ""),)).fetchone()
    if not inv:
        return jsonify({"error": "Onbekende map"}), 404
    try:
        data = base64.b64decode(js.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "Ongeldige inhoud"}), 400
    if data[:4] != b"%PDF":
        return jsonify({"error": "Geen PDF"}), 400
    fname = f"Factuur_{inv['number']}.pdf"
    old = inv["pdf_file_id"]
    fid = save_bytes("sales_invoice", inv["id"], data, fname, kind="factuur", mime="application/pdf")
    try:
        info = parse_pdf(data)
    except Exception as exc:  # noqa: BLE001 - PDF bewaren, uitlezen is bijzaak
        info = {"fout": f"{type(exc).__name__}: {str(exc)[:150]}"}
    sig = f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400]
    cid = inv["customer_id"] or (_match_customer(conn, None, info.get("customer")) if info.get("customer") else None)
    pid = inv["project_id"]
    if not pid and info.get("order_no"):
        pr = conn.execute("SELECT id FROM projects WHERE number = ?", (info["order_no"],)).fetchone()
        pid = pr["id"] if pr else None
    conn.execute("UPDATE sales_invoices SET customer_id = ?, customer_name = COALESCE(customer_name, ?), project_id = ?,"
                 " order_no = COALESCE(order_no, ?), attn = COALESCE(attn, ?) WHERE id = ?",
                 (cid, info.get("customer"), pid, info.get("order_no"), info.get("attn"), inv["id"]))
    # gegevens uit de PDF alleen invullen waar SnelStart/Excel nog niets gaf
    conn.execute("UPDATE sales_invoices SET pdf_file_id = ?, pdf_sig = ?, pdf_json = ?, invoice_date = COALESCE(invoice_date, ?),"
                 " due_date = COALESCE(due_date, ?), amount = COALESCE(amount, ?), mail_to = COALESCE(?, mail_to), updated_at = ? WHERE id = ?",
                 (fid, sig, json.dumps(info, ensure_ascii=False), info.get("invoice_date"), info.get("due_date"), info.get("amount"),
                  info.get("mail_to"), now_iso(), inv["id"]))
    conn.commit()
    if old and old != fid:
        try:
            from .util import delete_file
            delete_file(old)
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, "resultaat": f"Factuur {inv['number']} opgeslagen"})


# ---------------------------------------------------------------- pagina's

def _setting(key):
    r = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return r["value"] if r else None


def open_counts():
    """Voor het menu: aantal vervallen facturen."""
    if "_deb_counts" not in g:
        g._deb_counts = query("SELECT COUNT(*) c FROM sales_invoices WHERE status = 'open' AND due_date < ?",
                              (_today().isoformat(),), one=True)["c"] if can("debiteuren") else 0
    return g._deb_counts


@bp.route("/")
@require("debiteuren")
def index():
    view = request.args.get("v", "open")
    q = (request.args.get("q") or "").strip()
    today = _today()
    where, params = [], []
    if view == "open":
        where.append("s.status = 'open'")
    elif view == "vervallen":
        where.append("s.status = 'open' AND s.due_date < ?")
        params.append(today.isoformat())
    elif view == "betaald":
        where.append("s.status = 'betaald'")
    elif view == "onbekend":
        where.append("s.status = 'onbekend'")
    else:
        view = "alle"
    if q:
        where.append("(s.number LIKE ? OR s.customer_name LIKE ? OR c.name LIKE ? OR s.debtor_no = ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%", q]
    order = "s.due_date, s.number" if view in ("open", "vervallen") else "s.number DESC"
    rows = query("SELECT s.*, c.name AS cname FROM sales_invoices s LEFT JOIN customers c ON c.id = s.customer_id"
                 + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT 1000", params)
    out = []
    for r in rows:
        d = dict(r)
        dd = _d(r["due_date"])
        d["days_over"] = (today - date.fromisoformat(dd)).days if dd and r["status"] == "open" else None
        out.append(d)
    op = query("SELECT due_date, open_amount FROM sales_invoices WHERE status = 'open'")
    kpi = {"open": 0.0, "over": 0.0, "n_open": len(op), "n_over": 0, "b30": 0.0, "b60": 0.0, "b90": 0.0, "b90p": 0.0}
    for r in op:
        amt = r["open_amount"] or 0
        kpi["open"] += amt
        dd = _d(r["due_date"])
        days = (today - date.fromisoformat(dd)).days if dd else 0
        if days > 0:
            kpi["over"] += amt
            kpi["n_over"] += 1
            kpi["b30" if days <= 30 else "b60" if days <= 60 else "b90" if days <= 90 else "b90p"] += amt
    counts = {k: query(f"SELECT COUNT(*) c FROM sales_invoices WHERE status = '{k}'", one=True)["c"] for k in ("open", "betaald", "onbekend")}
    last = (_setting("deb_last_update") or "").split("|")
    from . import snelstart
    ss = (_setting("deb_snelstart_status") or "").split("|")
    dc = (_setting("deb_dc01_last") or "").split("|")
    return render_template("debiteuren/index.html", rows=out, view=view, q=q, kpi=kpi, counts=counts, STATUS_LABEL=STATUS_LABEL,
                           last=last if last[0] else None, ss_on=snelstart.configured(), ss=ss if ss[0] else None, dc=dc if dc[0] else None)


@bp.route("/<int:sid>")
@require("debiteuren")
def detail(sid):
    s = query("SELECT s.*, c.name AS cname FROM sales_invoices s LEFT JOIN customers c ON c.id = s.customer_id WHERE s.id = ?",
              (sid,), one=True) or abort(404)
    others = query("SELECT * FROM sales_invoices WHERE status = 'open' AND id <> ? AND ((customer_id IS NOT NULL AND customer_id = ?)"
                   " OR (debtor_no IS NOT NULL AND debtor_no = ?)) ORDER BY due_date", (sid, s["customer_id"] or -1, s["debtor_no"] or "-"))
    dd = _d(s["due_date"])
    days = (_today() - date.fromisoformat(dd)).days if dd and s["status"] == "open" else None
    proj = query("SELECT id, kind, number, name FROM projects WHERE id = ?", (s["project_id"],), one=True) if s["project_id"] else None
    if proj and not (can("projecten") if proj["kind"] == "project" else can("orders")):
        proj = None
    return render_template("debiteuren/detail.html", s=s, proj=proj, others=others, days=days, STATUS_LABEL=STATUS_LABEL,
                           info=json.loads(s["pdf_json"]) if s["pdf_json"] else {})


@bp.route("/<int:sid>/notitie", methods=["POST"])
@require("debiteuren", BEWERKEN)
def note(sid):
    query("SELECT id FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    execute("UPDATE sales_invoices SET note = ?, updated_at = ? WHERE id = ?", ((request.form.get("note") or "").strip()[:4000] or None, now_iso(), sid))
    flash("Notitie opgeslagen.", "ok")
    return redirect(url_for("debiteuren.detail", sid=sid))


@bp.route("/importeren", methods=["GET", "POST"])
@require("debiteuren", BEWERKEN)
def import_excel():
    conn = get_db()
    if request.method == "POST" and request.form.get("import_id"):
        imp = query("SELECT * FROM sales_imports WHERE id = ? AND status = 'voorbeeld'", (request.form.get("import_id"),), one=True)
        if not imp:
            flash("Dit voorbeeld is al toegepast of verlopen. Upload de Excel opnieuw.", "error")
            return redirect(url_for("debiteuren.import_excel"))
        res = apply_rows(conn, json.loads(imp["rows_json"]), "excel")
        execute("UPDATE sales_imports SET status = 'toegepast', applied_at = ?, summary_json = ?, rows_json = NULL WHERE id = ?",
                (now_iso(), json.dumps(res), imp["id"]))
        audit("openstaande posten geïmporteerd", "debiteuren", imp["id"], f"{imp['filename']}: {res['rows']} open, {res['closed']} betaald")
        flash(f"Geïmporteerd: {res['rows']} openstaande facturen ({res['new']} nieuw), {res['closed']} op betaald gezet.", "ok")
        return redirect(url_for("debiteuren.index"))
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Kies de Excel met openstaande verkoopfacturen.", "error")
            return redirect(url_for("debiteuren.import_excel"))
        data = f.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024 or data[:2] != b"PK":
            flash("Dit is geen Excel-bestand (.xlsx).", "error")
            return redirect(url_for("debiteuren.import_excel"))
        try:
            rows = parse_excel(data)
        except Exception as exc:  # noqa: BLE001
            flash(f"Kon de Excel niet lezen: {exc}", "error")
            return redirect(url_for("debiteuren.import_excel"))
        pv = preview(conn, rows)
        execute("DELETE FROM sales_imports WHERE status = 'voorbeeld' AND created_at < datetime('now', '-1 day')")
        iid = execute("INSERT INTO sales_imports (source, filename, status, rows_json, summary_json, created_by, created_at) VALUES ('excel',?,'voorbeeld',?,?,?,?)",
                      (f.filename[:200], json.dumps(rows), json.dumps(pv), g.user["id"], now_iso()))
        closing = query("SELECT number, customer_name, open_amount, status FROM sales_invoices WHERE number IN (%s) ORDER BY number"
                        % ",".join("?" * len(pv["closing"])), pv["closing"]) if pv["closing"] else []
        return render_template("debiteuren/import.html", pv=pv, rows=rows, iid=iid, filename=f.filename, closing=closing)
    hist = query("SELECT i.*, u.name AS who FROM sales_imports i LEFT JOIN users u ON u.id = i.created_by WHERE i.status = 'toegepast'"
                 " ORDER BY i.id DESC LIMIT 10")
    from . import snelstart
    return render_template("debiteuren/import.html", pv=None, hist=[dict(h, s=json.loads(h["summary_json"] or "{}")) for h in hist],
                           ss_on=snelstart.configured())


@bp.route("/snelstart", methods=["POST"])
@require("debiteuren", BEWERKEN)
def snelstart_now():
    from . import snelstart
    if not snelstart.configured():
        flash("SnelStart is nog niet gekoppeld (sleutels in Portainer).", "error")
        return redirect(url_for("debiteuren.index"))
    try:
        res = apply_rows(get_db(), snelstart_rows(), "snelstart")
        flash(f"Uit SnelStart: {res['rows']} openstaande facturen, {res['closed']} op betaald gezet.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Ophalen uit SnelStart mislukt: {str(exc)[:300]}", "error")
    return redirect(url_for("debiteuren.index"))
