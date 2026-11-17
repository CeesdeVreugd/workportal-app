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
    """Relatie zoeken: eerst op klantnummer SnelStart, dan op naam (met en zonder rechtsvorm, ook de verkorte naam).
    Meerdere treffers: alleen kiezen als er precies één aan Komdex gekoppeld is."""
    if debtor_no:
        hits = [r["id"] for r in conn.execute("SELECT id FROM customers WHERE TRIM(IFNULL(debtor_no,'')) = ?", (str(debtor_no).strip(),))]
        if len(hits) == 1:
            return hits[0]
    if not (name or "").strip():
        return None
    from .sharepoint import norm
    from .relaties import name_key
    rows = conn.execute("SELECT id, name, short_name, komdex_id, active FROM customers").fetchall()
    for fn in (norm, name_key):
        key = fn(name)
        if not key:
            continue
        hits = [r for r in rows if fn(r["name"]) == key or (r["short_name"] and fn(r["short_name"]) == key)]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            kx = [r for r in hits if r["komdex_id"] and r["active"]]
            if len(kx) == 1:
                return kx[0]["id"]
            return None
    return None


def rematch(conn):
    """Facturen zonder relatie opnieuw koppelen (bijv. nadat de relaties uit Komdex zijn bijgewerkt). Geeft het aantal."""
    n = 0
    names = {}
    for r in conn.execute("SELECT id, debtor_no, customer_name, pdf_json, project_id FROM sales_invoices WHERE customer_id IS NULL").fetchall():
        try:
            pdfname = (json.loads(r["pdf_json"]) or {}).get("customer") if r["pdf_json"] else None
        except ValueError:
            pdfname = None
        cid = None
        for nm in [x for x in (r["customer_name"], pdfname) if x] or [None]:
            k = (r["debtor_no"], nm)
            if k not in names:
                names[k] = _match_customer(conn, r["debtor_no"], nm)
            cid = names[k]
            if cid:
                break
        if not cid and r["project_id"]:
            p = conn.execute("SELECT customer_id FROM projects WHERE id = ?", (r["project_id"],)).fetchone()
            cid = p["customer_id"] if p else None
        if cid:
            conn.execute("UPDATE sales_invoices SET customer_id = ? WHERE id = ?", (cid, r["id"]))
            n += 1
    # klantnummer SnelStart invullen bij de relatie als dat nog leeg is en eenduidig
    for r in conn.execute("SELECT customer_id, MIN(debtor_no) AS d, COUNT(DISTINCT debtor_no) AS n FROM sales_invoices"
                          " WHERE customer_id IS NOT NULL AND IFNULL(debtor_no, '') <> '' GROUP BY customer_id").fetchall():
        if r["n"] == 1:
            conn.execute("UPDATE customers SET debtor_no = ? WHERE id = ? AND IFNULL(debtor_no, '') = ''"
                         " AND NOT EXISTS (SELECT 1 FROM customers c2 WHERE c2.debtor_no = ? AND c2.id <> ?)", (r["d"], r["customer_id"], r["d"], r["customer_id"]))
    conn.commit()
    return n


def scheduled_rematch(conn, state):
    import time
    if time.time() - state.get("deb_rematch", 0) < 3600:
        return
    state["deb_rematch"] = time.time()
    rematch(conn)


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


def _due_txt(v, inv_date):
    """Vervaldatum; sommige (oudere) facturen hebben de Amerikaanse volgorde '11-16-2025'. Kies de lezing die
    0-180 dagen na de factuurdatum valt (eerst dag-maand-jaar)."""
    v = (v or "").strip()
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", v)
    if not m:
        return _date_txt(v)
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    cands = []
    for d_, m_ in ((a, b), (b, a)):
        try:
            cands.append(date(y, m_, d_))
        except ValueError:
            pass
    if inv_date:
        base = date.fromisoformat(inv_date)
        for c in cands:
            if 0 <= (c - base).days <= 180:
                return c.isoformat()
    return cands[0].isoformat() if cands else None


def _address_block(page):
    """Linkerkolom onder 'Factuuradres:' (rechts staat het werk-/afleveradres)."""
    words = page.extract_words()
    head = next((w for w in words if re.match(r"(factuuradres|invoice-?adres|invoice-?address|billing|rechnungsadresse|adresse)", w["text"], re.I)), None)
    if not head:   # 'Invoice address:' als twee woorden
        for i, w in enumerate(words[:-1]):
            if re.match(r"^(invoice|billing|rechnungs?)$", w["text"], re.I) and re.match(r"^(address|adres|adresse)\b", words[i + 1]["text"], re.I):
                head = dict(w, x1=words[i + 1]["x1"])
                break
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
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:40])
        block = _address_block(pdf.pages[0]) if pdf.pages else []
    out = {}
    head = "\n".join(text.splitlines()[:25])
    if re.search(r"^\s*(herinnering|aanmaning|betalingsherinnering|sommatie|payment reminder|reminder|zahlungserinnerung|mahnung|rappel)\b", head, re.I | re.M):
        out["reminder"] = True
    m = re.search(r"(?:factuur\s*(?:nummer|nr\.?)|invoice\s*(?:number|no\.?)|rechnungs?\s*(?:nummer|nr\.?)|num[ée]ro\s*de\s*facture)\s*[:.]?\s*(\d{6,10})", text, re.I)
    if m:
        out["number"] = m.group(1)
    m = re.search(r"(?:factuurdatum|invoice\s*date|rechnungsdatum|date\s*de\s*facture)\s*[:.]?\s*" + DATE_TXT, text, re.I)
    if m:
        out["invoice_date"] = _date_txt(m.group(1))
    m = (re.search(r"(?:vervaldatum|due\s*date|f[äa]llig(?:keitsdatum)?|[ée]ch[ée]ance)\s*[:.]?\s*" + DATE_TXT, text, re.I)
         or re.search(r"(?:betaling\s+v[oó][oó]r|payment\s+(?:before|by)|zahlung\s+bis(?:\s+zum)?|paiement\s+avant\s+le)\s+" + DATE_TXT, text, re.I))
    if m:
        out["due_date"] = _due_txt(m.group(1), out.get("invoice_date"))
    if not out.get("due_date") and out.get("invoice_date"):
        # geen vervaldatum op de factuur: betalingstermijn ('binnen 30 dagen', '30 dagen netto', 'within 14 days'), anders 30 dagen
        m = re.search(r"(?:binnen|within|innerhalb(?:\s+von)?|sous|dans\s+les)\s+(\d{1,3})\s*(?:dagen|days|tagen|jours)", text, re.I) \
            or re.search(r"(\d{1,3})\s*(?:dagen|days|tage|jours)\s*(?:netto|net|na\s+factuurdatum|after\s+invoice)", text, re.I) \
            or re.search(r"(?:betalingstermijn|betalingsconditie|payment\s+terms?|zahlungsziel|zahlungsbedingung)[^\n\d]{0,30}(\d{1,3})", text, re.I)
        days = int(m.group(1)) if m and 0 < int(m.group(1)) <= 180 else 30
        from datetime import timedelta
        out["due_date"] = (date.fromisoformat(out["invoice_date"]) + timedelta(days=days)).isoformat()
        out["due_from_terms"] = days if m else "standaard 30"
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
    pdf = js.get("pdf") if isinstance(js.get("pdf"), dict) else {}
    return f"{pdf.get('name')}|{pdf.get('modified') or ''}|{pdf.get('size') or ''}"[:400] if pdf.get("name") else None


def _number_of(folder):
    pdf = folder.get("pdf") if isinstance(folder.get("pdf"), dict) else {}
    for s in (folder.get("name"), pdf.get("name")):
        m = NUM_RE.search(str(s or ""))
        if m:
            return m.group(1)
    return None


PDF_PER_RUN = 50


@bp.route("/api/dossiers", methods=["POST"])
def api_dossiers():
    """Het script op DC01 stuurt de factuurmappen (met de nieuwste PDF); WorkPortal zegt welke PDF's het wil hebben."""
    from .komdex import _key_ok
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    folders = [f for f in (js.get("folders") or []) if isinstance(f, dict)][:20000]
    if js.get("root"):
        get_db().execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_dc01_root', ?)", (str(js["root"])[:400],))
    try:
        return jsonify(_dossiers(folders))
    except Exception as exc:  # noqa: BLE001 - reden terug naar het script (komt in het logboek op DC01)
        get_db().rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "fout": f"{type(exc).__name__}: {str(exc)[:200]}", "mappen": len(folders), "nieuw": 0, "want": []})


def _dossiers(folders):
    conn = get_db()
    now = now_iso()
    # per factuurnummer één map: voorkeur voor de map die precies zo heet, anders de nieuwste PDF
    best = {}
    for f in folders:
        num, path = _number_of(f), str(f.get("path") or "")[:500]
        if not num or not path:
            continue
        exact = str(f.get("name") or "").strip() == num
        pdf = f.get("pdf") if isinstance(f.get("pdf"), dict) else {}
        key = (exact, str(pdf.get("modified") or ""))
        if num not in best or key > best[num][0]:
            best[num] = (key, f, path)
    known = {r["number"]: r for r in conn.execute("SELECT number, pdf_sig, dossier_path, dossier_reminders FROM sales_invoices")}
    want, new = [], 0
    for num, (_, f, path) in sorted(best.items(), reverse=True):   # nieuwste facturen eerst
        cur = known.get(num)
        if not cur:
            conn.execute("INSERT OR IGNORE INTO sales_invoices (number, dossier_path, status, created_at) VALUES (?,?, 'onbekend', ?)", (num, path, now))
            new += 1
        elif cur["dossier_path"] != path:
            conn.execute("UPDATE sales_invoices SET dossier_path = ? WHERE number = ?", (path, num))
        rem = [r for r in (f.get("reminders") or []) if isinstance(r, dict) and r.get("name")][:20]
        rem_js = json.dumps(sorted(({"name": str(r["name"])[:200], "modified": str(r.get("modified") or "")[:40]} for r in rem),
                                   key=lambda r: r["modified"]), ensure_ascii=False) if rem else None
        if rem_js != (cur["dossier_reminders"] if cur else None):
            conn.execute("UPDATE sales_invoices SET dossier_reminders = ? WHERE number = ?", (rem_js, num))
            if rem:   # al eerder herinnerd vanuit het ERP: niveau en datum meenemen, zodat WorkPortal niet opnieuw begint
                last = max(str(r.get("modified") or "") for r in rem)[:19].replace("T", " ")
                last_iso = (last[:10] + "T" + last[11:19] + "Z") if len(last) >= 19 else None
                conn.execute("UPDATE sales_invoices SET dun_level = MAX(dun_level, ?), dunned_at = CASE WHEN ? IS NOT NULL AND"
                             " (dunned_at IS NULL OR dunned_at < ?) THEN ? ELSE dunned_at END WHERE number = ?",
                             (min(len(rem), 3), last_iso, last_iso, last_iso, num))
        sig = _sig(f)
        if sig and (not cur or cur["pdf_sig"] != sig) and len(want) < PDF_PER_RUN:
            want.append(path)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_dc01_last', ?)", (f"{now}|{len(folders)}|{new}",))
    years = {}
    for f in folders:
        y = str(f.get("year") or "")[:4]
        if y.isdigit():
            years[y] = years.get(y, 0) + 1
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_dc01_years', ?)", (json.dumps(dict(sorted(years.items()))),))
    conn.commit()
    return {"ok": True, "mappen": len(folders), "factuurnummers": len(best), "nieuw": new, "want": want}


@bp.route("/api/factuur", methods=["POST"])
def api_pdf():
    from .komdex import _key_ok
    if not _key_ok():
        return jsonify({"error": "Ongeldige sleutel"}), 401
    js = request.get_json(silent=True) or {}
    try:
        return _pdf(js)
    except Exception as exc:  # noqa: BLE001 - reden terug naar het script
        get_db().rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "resultaat": f"FOUT in WorkPortal: {type(exc).__name__}: {str(exc)[:200]}"})


def _pdf(js):
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
    try:
        info = parse_pdf(data)
    except Exception as exc:  # noqa: BLE001 - PDF bewaren, uitlezen is bijzaak
        info = {"fout": f"{type(exc).__name__}: {str(exc)[:150]}"}
    sig = f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400]
    if info.get("reminder") and inv["pdf_file_id"]:
        # een herinnering is niet de factuur: de opgeslagen factuur houden, deze niet opnieuw vragen
        conn.execute("UPDATE sales_invoices SET pdf_sig = ? WHERE id = ?", (sig, inv["id"]))
        conn.commit()
        return jsonify({"ok": True, "resultaat": f"Factuur {inv['number']}: dit is een herinnering, factuur niet vervangen"})
    fname = f"Factuur_{inv['number']}.pdf"
    old = inv["pdf_file_id"]
    fid = save_bytes("sales_invoice", inv["id"], data, fname, kind="factuur", mime="application/pdf")
    conn.execute("UPDATE sales_invoices SET pdf_file_id = ?, pdf_sig = ? WHERE id = ?", (fid, sig, inv["id"]))
    apply_info(conn, inv, info)
    conn.commit()
    if old and old != fid:
        try:
            from .util import delete_file
            delete_file(old)
        except Exception:  # noqa: BLE001
            pass
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_dc01_lastpdf', ?)", (f"{now_iso()}|{inv['number']}",))
    conn.commit()
    return jsonify({"ok": True, "resultaat": f"Factuur {inv['number']} opgeslagen"})


def apply_info(conn, inv, info):
    """Gegevens uit de factuur-PDF opslaan. Klant: herkend op naam, anders de klant van de order uit 'Onze ref.'.
    Datums/bedrag alleen waar SnelStart/Excel nog niets gaf."""
    pid = inv["project_id"]
    if not pid and info.get("order_no"):
        pr = conn.execute("SELECT id FROM projects WHERE number = ?", (info["order_no"],)).fetchone()
        pid = pr["id"] if pr else None
    cid = inv["customer_id"] or (_match_customer(conn, inv["debtor_no"], info.get("customer")) if info.get("customer") or inv["debtor_no"] else None)
    if not cid and pid:
        pr = conn.execute("SELECT customer_id FROM projects WHERE id = ?", (pid,)).fetchone()
        cid = pr["customer_id"] if pr else None
    conn.execute("UPDATE sales_invoices SET customer_id = ?, customer_name = COALESCE(customer_name, ?), project_id = ?, order_no = COALESCE(order_no, ?),"
                 " attn = COALESCE(attn, ?), pdf_json = ?, invoice_date = COALESCE(invoice_date, ?), due_date = COALESCE(due_date, ?),"
                 " amount = COALESCE(amount, ?), mail_to = COALESCE(?, mail_to), updated_at = ? WHERE id = ?",
                 (cid, info.get("customer"), pid, info.get("order_no"), info.get("attn"), json.dumps(info, ensure_ascii=False),
                  info.get("invoice_date"), info.get("due_date"), info.get("amount"), info.get("mail_to"), now_iso(), inv["id"]))
    # betaald gemeld voordat het bedrag bekend was: betaald = factuurbedrag
    conn.execute("UPDATE sales_invoices SET paid = amount WHERE id = ? AND status = 'betaald' AND paid IS NULL AND amount IS NOT NULL", (inv["id"],))


PARSE_VERSION = "6"


def reparse(conn, ids=None, upload_dir=None):
    """Opgeslagen factuur-PDF's opnieuw uitlezen (na een betere herkenning). Geeft het aantal."""
    import os
    sql = "SELECT s.*, f.stored_name FROM sales_invoices s JOIN files f ON f.id = s.pdf_file_id"
    rows = conn.execute(sql + (f" WHERE s.id IN ({','.join('?' * len(ids))})" if ids else ""), ids or []).fetchall()
    n = 0
    for r in rows:
        try:
            with open(os.path.join(upload_dir, r["stored_name"]), "rb") as fh:
                info = parse_pdf(fh.read())
        except Exception as exc:  # noqa: BLE001
            info = {"fout": f"{type(exc).__name__}: {str(exc)[:150]}"}
        if info.get("reminder"):   # opgeslagen PDF blijkt een herinnering: de echte factuur opnieuw laten ophalen
            conn.execute("UPDATE sales_invoices SET pdf_sig = NULL WHERE id = ?", (r["id"],))
        apply_info(conn, r, info)
        n += 1
    conn.commit()
    return n


def scheduled_reparse(conn, app):
    """Eenmalig na een nieuwe versie van de herkenning: alle factuur-PDF's opnieuw uitlezen."""
    if _setting("deb_parse_version", conn) == PARSE_VERSION:
        return
    with app.app_context():
        from .util import upload_dir
        d = upload_dir()
    n = reparse(conn, upload_dir=d)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_parse_version', ?)", (PARSE_VERSION,))
    conn.commit()
    print(f"[WorkPortal] Verkoopfacturen: {n} PDF's opnieuw uitgelezen", flush=True)


# ---------------------------------------------------------------- pagina's

def _setting(key, conn=None):
    r = (conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone() if conn is not None
         else query("SELECT value FROM settings WHERE key = ?", (key,), one=True))
    return r["value"] if r else None


def finance_role(user=None):
    """Alleen directie en administratie zien verkoopfacturen bij projecten en orders (ongeacht andere rechten)."""
    user = user or getattr(g, "user", None)
    if not user:
        return False
    if "_fin_role" not in g:
        g._fin_role = bool(query("SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = ?"
                                 " AND r.key IN ('directie', 'administratie')", (user["id"],), one=True))
    return g._fin_role and can("debiteuren")


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
    year = request.args.get("jaar") or ""
    if year.isdigit() and len(year) == 4:
        where.append("COALESCE(substr(s.invoice_date, 1, 4), substr(s.number, 1, 4)) = ?")
        params.append(year)
    else:
        year = ""
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
    years = [r["y"] for r in query("SELECT DISTINCT COALESCE(substr(invoice_date, 1, 4), substr(number, 1, 4)) AS y FROM sales_invoices"
                                   " WHERE y GLOB '[12][0-9][0-9][0-9]' ORDER BY y DESC")]
    return render_template("debiteuren/index.html", rows=out, view=view, q=q, year=year, years=years, kpi=kpi, counts=counts, STATUS_LABEL=STATUS_LABEL,
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
    letters = _letters_for_invoice(sid)
    cust = query("SELECT invoice_email FROM customers WHERE id = ?", (s["customer_id"],), one=True) if s["customer_id"] else None
    return render_template("debiteuren/detail.html", s=s, letters=letters, LEVELS=LEVELS, cust=cust, today=_today().isoformat(), proj=proj, others=others, days=days, STATUS_LABEL=STATUS_LABEL,
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


# ---------------------------------------------------------------- herinneren en aanmanen

LEVELS = {1: "Herinnering", 2: "Aanmaning", 3: "Laatste aanmaning"}
DUN_TAG = re.compile(r"\[WP-H(\d+)\]")
DEFAULT_DAYS = (7, 21, 35)
DEFAULT_SENDER = "administratie@devreugd-pt.nl"
IBAN = "NL70 RABO 0133 9900 36"

# Mailtekst (kort); de brief zelf zit als PDF in de bijlage, samen met de facturen.
DUN = {
    "nl": {
        "title": {1: "Herinnering", 2: "Aanmaning", 3: "Laatste aanmaning"},
        "subject": {1: "Herinnering openstaande facturen", 2: "Aanmaning openstaande facturen", 3: "Laatste aanmaning openstaande facturen"},
        "subject1": {1: "Herinnering factuur {nr}", 2: "Aanmaning factuur {nr}", 3: "Laatste aanmaning factuur {nr}"},
        "body": {
            1: "Geachte heer/mevrouw,\n\nVolgens onze administratie staat er nog een bedrag van {total} open ({what}). "
               "In de bijlage vindt u een overzicht en de {docs}.\n\nWilt u het openstaande bedrag binnen 7 dagen overmaken op {iban} "
               "onder vermelding van de factuurnummers? Heeft u inmiddels betaald, dan kunt u deze herinnering als niet verzonden beschouwen.",
            2: "Geachte heer/mevrouw,\n\nOndanks onze eerdere herinnering hebben wij de betaling van {what} (totaal {total}) nog niet ontvangen. "
               "In de bijlage vindt u een overzicht en de {docs}.\n\nWij verzoeken u het openstaande bedrag binnen 7 dagen over te maken op {iban} "
               "onder vermelding van de factuurnummers. Is er een reden waarom nog niet betaald is, neem dan contact met ons op.",
            3: "Geachte heer/mevrouw,\n\nTot onze spijt hebben wij, ondanks herinnering en aanmaning, de betaling van {what} (totaal {total}) "
               "nog steeds niet ontvangen. In de bijlage vindt u een overzicht en de {docs}.\n\nWij verzoeken u dringend het openstaande bedrag "
               "binnen 5 werkdagen over te maken op {iban}. Blijft betaling uit, dan zijn wij genoodzaakt verdere stappen te nemen; "
               "de kosten daarvan komen voor uw rekening.",
        },
        "what1": "factuur {nr}", "whatn": "{n} facturen", "doc1": "factuur", "docn": "facturen",
        "salute": "Geachte heer/mevrouw,", "address": "Factuuradres:", "attn": "T.a.v.",
        "intro": {1: "Volgens onze administratie zijn de onderstaande facturen nog niet (volledig) betaald.",
                  2: "Uit onze administratie is gebleken dat u geen gehoor heeft gegeven aan ons eerdere betalingsverzoek. "
                     "Hieronder geven we u een overzicht van de openstaande facturen.",
                  3: "Ondanks onze herinnering en aanmaning zijn de onderstaande facturen nog steeds niet betaald."},
        "cols": ["Factuurnr.", "Omschrijving", "Datum", "Vervaldatum", "Vervallen", "Openstaand"], "days": "{n} dagen", "notdue": "nog niet", "overdue": "Waarvan vervallen",
        "total": "Totaal openstaand",
        "close": {1: "Wilt u het openstaande bedrag binnen 7 dagen overmaken op {iban} onder vermelding van de factuurnummers? "
                     "Heeft u inmiddels betaald, dan kunt u deze herinnering als niet verzonden beschouwen.",
                  2: "Wij vragen u om per omgaande de openstaande factuur(en) te betalen op {iban} onder vermelding van de factuurnummers.",
                  3: "Wij verzoeken u dringend het openstaande bedrag binnen 5 werkdagen over te maken op {iban}. Blijft betaling uit, "
                     "dan zijn wij genoodzaakt verdere stappen te nemen; de kosten daarvan komen voor uw rekening."},
        "terms": "Op al onze offertes, op alle opdrachten aan ons en op alle met ons gesloten overeenkomsten zijn van toepassing de "
                 "METAALUNIEVOORWAARDEN, Gedeponeerd ter Griffie van de rechtbank te Rotterdam, zoals deze luiden volgens de laatstelijk "
                 "aldaar neergelegde tekst.",
        "page": "Pagina", "country": "Nederland",
    },
    "en": {
        "title": {1: "Payment reminder", 2: "Second reminder", 3: "Final notice"},
        "subject": {1: "Payment reminder outstanding invoices", 2: "Second reminder outstanding invoices", 3: "Final notice outstanding invoices"},
        "subject1": {1: "Payment reminder invoice {nr}", 2: "Second reminder invoice {nr}", 3: "Final notice invoice {nr}"},
        "body": {
            1: "Dear Sir/Madam,\n\nAccording to our records, an amount of {total} is still outstanding ({what}). "
               "Please find attached an overview and the {docs}.\n\nPlease transfer the outstanding amount within 7 days to {iban} "
               "(BIC RABONL2U), stating the invoice numbers. If you have already paid, please disregard this reminder.",
            2: "Dear Sir/Madam,\n\nDespite our earlier reminder, we have not yet received payment of {what} (total {total}). "
               "Please find attached an overview and the {docs}.\n\nWe kindly ask you to transfer the outstanding amount within 7 days to {iban} "
               "(BIC RABONL2U), stating the invoice numbers. If there is a reason why payment has not been made, please contact us.",
            3: "Dear Sir/Madam,\n\nWe regret to note that, despite our reminders, {what} (total {total}) is still unpaid. "
               "Please find attached an overview and the {docs}.\n\nWe urgently request you to transfer the outstanding amount within "
               "5 working days to {iban} (BIC RABONL2U). If payment is not received, we will have to take further action, at your expense.",
        },
        "what1": "invoice {nr}", "whatn": "{n} invoices", "doc1": "invoice", "docn": "invoices",
        "salute": "Dear Sir/Madam,", "address": "Invoice address:", "attn": "Attn:",
        "intro": {1: "According to our records, the invoices below have not yet been (fully) paid.",
                  2: "Our records indicate that you have not responded to our previous payment request. "
                     "Below is an overview of the outstanding invoices.",
                  3: "Despite our reminders, the invoices below are still unpaid."},
        "cols": ["Invoice no.", "Description", "Date", "Due date", "Overdue", "Outstanding"], "days": "{n} days", "notdue": "not yet due", "overdue": "Of which overdue",
        "total": "Total outstanding balance",
        "close": {1: "Please transfer the outstanding amount within 7 days to {iban} (BIC RABONL2U), stating the invoice numbers. "
                     "If you have already paid, please disregard this reminder.",
                  2: "We kindly request that you pay the outstanding invoice(s) immediately to {iban} (BIC RABONL2U), stating the invoice numbers.",
                  3: "We urgently request you to transfer the outstanding amount within 5 working days to {iban} (BIC RABONL2U). "
                     "If payment is not received, we will have to take further action, at your expense."},
        "terms": "All our quotations, all orders placed with us and all contracts concluded with us are subject to the METAALUNIE "
                 "CONDITIONS, filed with the Registry of the District Court of Rotterdam, as they read according to the latest text filed there.",
        "page": "Page", "country": "",
    },
    "de": {
        "title": {1: "Zahlungserinnerung", 2: "Mahnung", 3: "Letzte Mahnung"},
        "subject": {1: "Zahlungserinnerung offene Rechnungen", 2: "Mahnung offene Rechnungen", 3: "Letzte Mahnung offene Rechnungen"},
        "subject1": {1: "Zahlungserinnerung Rechnung {nr}", 2: "Mahnung Rechnung {nr}", 3: "Letzte Mahnung Rechnung {nr}"},
        "body": {
            1: "Sehr geehrte Damen und Herren,\n\nlaut unserer Buchhaltung ist noch ein Betrag von {total} offen ({what}). "
               "Im Anhang finden Sie eine Übersicht und die {docs}.\n\nBitte überweisen Sie den offenen Betrag innerhalb von 7 Tagen auf {iban} "
               "(BIC RABONL2U) unter Angabe der Rechnungsnummern. Sollten Sie bereits bezahlt haben, betrachten Sie diese Erinnerung bitte als gegenstandslos.",
            2: "Sehr geehrte Damen und Herren,\n\ntrotz unserer Erinnerung haben wir die Zahlung von {what} (insgesamt {total}) noch nicht erhalten. "
               "Im Anhang finden Sie eine Übersicht und die {docs}.\n\nBitte überweisen Sie den offenen Betrag innerhalb von 7 Tagen auf {iban} "
               "(BIC RABONL2U). Gibt es einen Grund für die ausstehende Zahlung, nehmen Sie bitte Kontakt mit uns auf.",
            3: "Sehr geehrte Damen und Herren,\n\nleider ist {what} (insgesamt {total}) trotz Erinnerung und Mahnung noch immer nicht bezahlt. "
               "Im Anhang finden Sie eine Übersicht und die {docs}.\n\nWir fordern Sie dringend auf, den offenen Betrag innerhalb von 5 Werktagen "
               "auf {iban} (BIC RABONL2U) zu überweisen. Andernfalls sehen wir uns gezwungen, weitere Schritte einzuleiten; die Kosten gehen zu Ihren Lasten.",
        },
        "what1": "die Rechnung {nr}", "whatn": "{n} Rechnungen", "doc1": "Rechnung", "docn": "Rechnungen",
        "salute": "Sehr geehrte Damen und Herren,", "address": "Rechnungsadresse:", "attn": "z. Hd.",
        "intro": {1: "laut unserer Buchhaltung sind die folgenden Rechnungen noch nicht (vollständig) bezahlt.",
                  2: "wir stellen fest, dass Sie auf unsere vorherige Zahlungserinnerung nicht reagiert haben. "
                     "Nachstehend eine Übersicht der offenen Rechnungen.",
                  3: "trotz Erinnerung und Mahnung sind die folgenden Rechnungen noch immer nicht bezahlt."},
        "cols": ["Rechnung", "Beschreibung", "Datum", "Fällig am", "Überfällig", "Offen"], "days": "{n} Tage", "notdue": "noch nicht fällig", "overdue": "Davon überfällig",
        "total": "Offener Gesamtbetrag",
        "close": {1: "Bitte überweisen Sie den offenen Betrag innerhalb von 7 Tagen auf {iban} (BIC RABONL2U) unter Angabe der Rechnungsnummern.",
                  2: "Wir bitten Sie, die offene(n) Rechnung(en) umgehend auf {iban} (BIC RABONL2U) zu bezahlen.",
                  3: "Wir fordern Sie dringend auf, den offenen Betrag innerhalb von 5 Werktagen auf {iban} (BIC RABONL2U) zu überweisen. "
                     "Andernfalls sehen wir uns gezwungen, weitere Schritte einzuleiten; die Kosten gehen zu Ihren Lasten."},
        "terms": "Für alle unsere Angebote, Aufträge und Verträge gelten die METAALUNIE-BEDINGUNGEN, hinterlegt bei der Geschäftsstelle "
                 "des Bezirksgerichts Rotterdam, in der zuletzt dort hinterlegten Fassung.",
        "page": "Seite", "country": "",
    },
    "fr": {
        "title": {1: "Rappel de paiement", 2: "Relance", 3: "Dernière relance"},
        "subject": {1: "Rappel factures impayées", 2: "Relance factures impayées", 3: "Dernière relance factures impayées"},
        "subject1": {1: "Rappel facture {nr}", 2: "Relance facture {nr}", 3: "Dernière relance facture {nr}"},
        "body": {
            1: "Madame, Monsieur,\n\nSelon notre comptabilité, un montant de {total} reste impayé ({what}). "
               "Vous trouverez en pièce jointe un récapitulatif et les {docs}.\n\nNous vous prions de virer le montant dû sous 7 jours sur {iban} "
               "(BIC RABONL2U) en indiquant les numéros de facture. Si vous avez déjà payé, veuillez ne pas tenir compte de ce rappel.",
            2: "Madame, Monsieur,\n\nMalgré notre rappel, nous n'avons pas encore reçu le paiement de {what} (total {total}). "
               "Vous trouverez en pièce jointe un récapitulatif et les {docs}.\n\nNous vous prions de virer le montant dû sous 7 jours sur {iban} "
               "(BIC RABONL2U). Si ce retard a une raison, merci de nous contacter.",
            3: "Madame, Monsieur,\n\nMalgré nos rappels, {what} (total {total}) reste impayé(e). Vous trouverez en pièce jointe un récapitulatif "
               "et les {docs}.\n\nNous vous demandons instamment de virer le montant dû sous 5 jours ouvrables sur {iban} (BIC RABONL2U). "
               "À défaut, nous serons contraints d'engager d'autres démarches, à vos frais.",
        },
        "what1": "la facture {nr}", "whatn": "{n} factures", "doc1": "facture", "docn": "factures",
        "salute": "Madame, Monsieur,", "address": "Adresse de facturation :", "attn": "À l'attention de",
        "intro": {1: "Selon notre comptabilité, les factures ci-dessous n'ont pas encore été (entièrement) réglées.",
                  2: "Nous constatons que vous n'avez pas donné suite à notre précédente demande de paiement. "
                     "Voici un récapitulatif des factures impayées.",
                  3: "Malgré nos rappels, les factures ci-dessous restent impayées."},
        "cols": ["N° facture", "Description", "Date", "Échéance", "Retard", "Restant dû"], "days": "{n} jours", "notdue": "non échue", "overdue": "Dont échu",
        "total": "Total restant dû",
        "close": {1: "Nous vous prions de virer le montant dû sous 7 jours sur {iban} (BIC RABONL2U) en indiquant les numéros de facture.",
                  2: "Nous vous prions de régler immédiatement la ou les facture(s) impayée(s) sur {iban} (BIC RABONL2U).",
                  3: "Nous vous demandons instamment de virer le montant dû sous 5 jours ouvrables sur {iban} (BIC RABONL2U). "
                     "À défaut, nous serons contraints d'engager d'autres démarches, à vos frais."},
        "terms": "Toutes nos offres, commandes et contrats sont soumis aux CONDITIONS METAALUNIE, déposées au greffe du tribunal "
                 "de Rotterdam, dans leur dernière version déposée.",
        "page": "Page", "country": "",
    },
}


def _ddmm(v):
    d = _d(v)
    return date.fromisoformat(d).strftime("%d-%m-%Y") if d else "–"


def _eur(v):
    from .util import fmt_eur
    return fmt_eur(v or 0, 2)


def _open(inv):
    return inv["open_amount"] if inv["open_amount"] is not None else (inv["amount"] or 0)


def dun_days(conn=None):
    raw = _setting("deb_days", conn) or ""
    try:
        vals = tuple(int(x) for x in raw.split(","))
        if len(vals) == 3 and all(0 <= v <= 365 for v in vals):
            return vals
    except ValueError:
        pass
    return DEFAULT_DAYS


def sender(conn=None):
    return (_setting("deb_sender", conn) or DEFAULT_SENDER).strip()


def group_key(inv):
    """Herinneringen gaan per klant: zelfde relatie (of debiteurnummer) = één brief."""
    if inv["customer_id"]:
        return f"c{inv['customer_id']}"
    if inv["debtor_no"]:
        return f"d{inv['debtor_no']}"
    return f"i{inv['id']}"


def _lang_of(conn, cid):
    from .talen import norm_lang
    c = conn.execute("SELECT language FROM customers WHERE id = ?", (cid,)).fetchone() if cid else None
    lang = norm_lang(c["language"] if c and c["language"] else "nl")
    return lang if lang in DUN else "en"


def letter_text(invs, level, lang):
    L = DUN.get(lang) or DUN["nl"]
    total = _eur(sum(_open(i) for i in invs))
    one = len(invs) == 1
    v = {"what": (L["what1"].format(nr=invs[0]["number"]) if one else L["whatn"].format(n=len(invs))), "total": total,
         "docs": L["doc1"] if one else L["docn"], "iban": IBAN}
    subject = L["subject1"][level].format(nr=invs[0]["number"]) if one else L["subject"][level]
    return subject, L["body"][level].format(**v)


def letter_ids(L):
    try:
        ids = json.loads(L["invoice_ids"]) if L["invoice_ids"] else []
    except (ValueError, TypeError):
        ids = []
    return [int(x) for x in ids if str(x).isdigit()] or [L["invoice_id"]]


def _dunnable(inv, today=None):
    """Mag deze factuur in een herinnering? (open, vervallen, niet uitgesteld, niet 'niet aanmanen')"""
    today = today or _today().isoformat()
    if inv["status"] != "open" or inv["no_dunning"] or (inv["open_amount"] is not None and inv["open_amount"] <= 0.004):
        return False
    if inv["postpone_until"] and inv["postpone_until"] > today:
        return False
    dd = _d(inv["due_date"])
    return bool(dd and dd < today)


def _group_invoices(conn, key, today=None):
    rows = conn.execute("SELECT * FROM sales_invoices WHERE status = 'open' ORDER BY due_date, number").fetchall()
    return [r for r in rows if group_key(r) == key and _dunnable(r, today)]


def new_letter(conn, invs, level, auto, uid=None):
    first = invs[0]
    c = conn.execute("SELECT * FROM customers WHERE id = ?", (first["customer_id"],)).fetchone() if first["customer_id"] else None
    lang = _lang_of(conn, first["customer_id"])
    subject, body = letter_text(invs, level, lang)
    to = (c["invoice_email"] if c and c["invoice_email"] else None) or next((i["mail_to"] for i in invs if i["mail_to"]), None) or ""
    cur = conn.execute("INSERT INTO dunning_letters (invoice_id, invoice_ids, group_key, customer_id, level, status, auto, language, to_addr,"
                       " subject, body, created_by, created_at) VALUES (?,?,?,?,?, 'voorstel', ?,?,?,?,?,?,?)",
                       (first["id"], json.dumps([i["id"] for i in invs]), group_key(first), first["customer_id"], level, 1 if auto else 0,
                        lang, to, subject, body, uid, now_iso()))
    return cur.lastrowid


def _due_level(inv, days):
    """Welk niveau is voor deze factuur aan de beurt (of None)."""
    if not _dunnable(inv) or inv["dun_level"] >= 3:
        return None
    over = (_today() - date.fromisoformat(_d(inv["due_date"]))).days
    level = inv["dun_level"] + 1
    if over < days[level - 1]:
        return None
    if inv["dunned_at"] and (_today() - date.fromisoformat(inv["dunned_at"][:10])).days < 7:
        return None   # minstens een week tussen twee brieven
    return level


def cleanup(conn):
    """Voorstellen bijwerken: betaalde/uitgestelde/'niet aanmanen'-facturen eruit; niets meer over = vervallen."""
    today = _today().isoformat()
    for L in conn.execute("SELECT * FROM dunning_letters WHERE status = 'voorstel'").fetchall():
        ids = letter_ids(L)
        rows = conn.execute(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        keep = [r["id"] for r in rows if _dunnable(r, today)]
        if not keep:
            conn.execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Geen openstaande facturen meer', updated_at = ? WHERE id = ?",
                         (now_iso(), L["id"]))
        elif keep != ids:
            conn.execute("UPDATE dunning_letters SET invoice_ids = ?, invoice_id = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(keep), keep[0], now_iso(), L["id"]))


def propose(conn):
    """Nieuwe voorstellen (per klant) voor wie aan de beurt is. Geeft het aantal nieuwe voorstellen."""
    cleanup(conn)
    days = dun_days(conn)
    pending = {r[0] for r in conn.execute("SELECT group_key FROM dunning_letters WHERE status = 'voorstel' AND group_key IS NOT NULL")}
    pending |= {f"i{r[0]}" for r in conn.execute("SELECT invoice_id FROM dunning_letters WHERE status = 'voorstel' AND group_key IS NULL")}
    groups = {}
    today = _today().isoformat()
    for inv in conn.execute("SELECT * FROM sales_invoices WHERE status = 'open' ORDER BY due_date, number").fetchall():
        if not _dunnable(inv, today):
            continue
        g_ = groups.setdefault(group_key(inv), {"invs": [], "level": 0})
        g_["invs"].append(inv)
        lv = _due_level(inv, days)
        if lv:
            g_["level"] = max(g_["level"], lv)
    n = 0
    for key, g_ in groups.items():
        if not g_["level"] or key in pending:
            continue
        new_letter(conn, g_["invs"], g_["level"], auto=True)
        n += 1
    conn.commit()
    return n


def scheduled_dunning(conn, state):
    """Elk uur: voorstellen maken (alleen als er openstaande posten zijn ingelezen); directie krijgt een melding."""
    import time
    if time.time() - state.get("deb_dun", 0) < 3600:
        return
    state["deb_dun"] = time.time()
    if not conn.execute("SELECT 1 FROM settings WHERE key = 'deb_last_update'").fetchone():
        return
    n = propose(conn)
    if n:
        from .notify import notify_user
        users = [r["id"] for r in conn.execute(
            "SELECT DISTINCT u.id FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id LEFT JOIN role_permissions rp"
            " ON rp.role_id = ur.role_id AND rp.module = 'debiteuren' LEFT JOIN user_permissions up ON up.user_id = u.id AND up.module = 'debiteuren'"
            " WHERE u.active = 1 AND (u.is_admin = 1 OR rp.level >= 3 OR up.level >= 3)")]
        for uid in users:
            try:
                notify_user(conn, uid, f"{n} nieuwe aanmaningsvoorstel{'len' if n != 1 else ''}",
                            "Er staan herinneringen/aanmaningen klaar om te controleren en te versturen.", url="/debiteuren/aanmaningen", email=False)
            except Exception:  # noqa: BLE001
                pass


def snelstart_open(number):
    """Actueel openstaand saldo uit SnelStart (None = niet bekend / niet gekoppeld)."""
    from . import snelstart
    if not snelstart.configured():
        return None
    rows = snelstart.get("verkoopfacturen", {"$filter": f"factuurnummer eq '{number}'"}) or []
    if not rows:
        return None
    return _amount(rows[0].get("openstaandSaldo"))


def _invoice_desc(conn, inv):
    """Omschrijving voor de brief: 'Onze ref.' (order) met projectnaam."""
    name = None
    if inv["project_id"]:
        p = conn.execute("SELECT name FROM projects WHERE id = ?", (inv["project_id"],)).fetchone()
        name = p["name"] if p else None
    parts = [x for x in (inv["order_no"], name) if x]
    return " - ".join(parts)


def letter_pdf(conn, L, invs):
    """De brief (PDF) zoals uit het ERP: factuuradres, tekst, tabel met openstaande facturen, totaal."""
    from .pdf import dunning_pdf
    c = conn.execute("SELECT * FROM customers WHERE id = ?", (invs[0]["customer_id"],)).fetchone() if invs[0]["customer_id"] else None
    lang = L["language"] if L["language"] in DUN else "nl"
    T = DUN[lang]
    addr = [c["name"] if c else (invs[0]["customer_name"] or "")]
    attn = next((i["attn"] for i in invs if i["attn"]), None)
    if attn:
        addr.append(f"{T['attn']} {attn}")
    if c:
        if c["address"]:
            addr.append(c["address"])
        pc = " ".join(x for x in (c["postcode"], c["city"]) if x)
        if pc:
            addr.append(pc)
    # alle openstaande facturen van de klant, met hoeveel dagen vervallen
    key = L["group_key"] or group_key(invs[0])
    alle = [r for r in conn.execute("SELECT * FROM sales_invoices WHERE status = 'open' ORDER BY due_date, number").fetchall()
            if group_key(r) == key and _open(r) > 0.004]
    ids = {i["id"] for i in invs}
    alle += [i for i in invs if i["id"] not in {r["id"] for r in alle}]
    alle.sort(key=lambda r: (r["due_date"] or "9999", r["number"]))
    today = _today()
    rows, over = [], 0.0
    for i in alle:
        dd = _d(i["due_date"])
        days = (today - date.fromisoformat(dd)).days if dd else None
        if days and days > 0:
            over += _open(i)
        rows.append({"number": i["number"], "desc": _invoice_desc(conn, i), "date": _ddmm(i["invoice_date"]), "due": _ddmm(i["due_date"]),
                     "days": T["days"].format(n=days) if days and days > 0 else T["notdue"], "open": _eur(_open(i)),
                     "late": bool(days and days > 0), "selected": i["id"] in ids})
    total = sum(_open(i) for i in alle)
    num = L["number"] if "number" in L.keys() else None
    return dunning_pdf(T, L["level"], addr, rows, _eur(total), IBAN, _today().strftime("%d-%m-%Y"),
                       overdue=_eur(over) if abs(over - total) > 0.004 else None, number=num)


def send_letter(conn, lid):
    """Versturen (na goedkeuring). Geeft (ok, melding)."""
    from .mail import send_mail_with_files
    from .util import file_path
    L = conn.execute("SELECT * FROM dunning_letters WHERE id = ?", (lid,)).fetchone()
    if not L or L["status"] != "voorstel":
        return False, "Deze brief is al verstuurd of vervallen."
    to = [a.strip() for a in re.split(r"[;,\s]+", L["to_addr"] or "") if a.strip()]
    if not to or any(not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a) for a in to):
        return False, "Vul een geldig e-mailadres in (bij voorkeur de factuur-e-mail bij de relatie)."
    ids = letter_ids(L)
    invs = [r for r in conn.execute(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))}) ORDER BY due_date, number", ids)
            if _dunnable(r)]
    # vlak voor versturen: in SnelStart kijken of er inmiddels betaald is
    paid, now = [], now_iso()
    for inv in list(invs):
        try:
            o = snelstart_open(inv["number"])
        except Exception:  # noqa: BLE001 - SnelStart niet bereikbaar: niet tegenhouden
            o = None
        if o is not None and o <= 0.004:
            conn.execute("UPDATE sales_invoices SET status = 'betaald', open_amount = 0, paid_at = ?, status_source = 'snelstart', status_at = ? WHERE id = ?",
                         (now, now, inv["id"]))
            paid.append(inv["number"])
            invs.remove(inv)
    if not invs:
        conn.execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Betaald volgens SnelStart vlak voor versturen', updated_at = ? WHERE id = ?", (now, lid))
        conn.commit()
        return False, f"Niet verstuurd: factuur {', '.join(paid)} is volgens SnelStart inmiddels betaald." if paid else "Niet verstuurd: geen openstaande facturen meer."
    if paid:   # tekst klopt niet meer (andere facturen/totaal): eerst opnieuw laten bekijken
        keep = [i["id"] for i in invs]
        subject, body = letter_text(invs, L["level"], L["language"] or "nl")
        conn.execute("UPDATE dunning_letters SET invoice_ids = ?, invoice_id = ?, subject = ?, body = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(keep), keep[0], subject, body, now, lid))
        conn.commit()
        return False, f"Niet verstuurd: factuur {', '.join(paid)} is volgens SnelStart inmiddels betaald. Het voorstel is aangepast; controleer het en verstuur opnieuw."
    number = L["number"] or next_number(conn)
    L = dict(L, number=number)
    pdf, atts = _attachments(conn, L, invs)
    fname = atts[0][0]
    subject = _subject_with(L["subject"], number, lid)
    ok, err = send_mail_with_files(sender(conn), to, subject, L["body"], atts, signer="administratie")
    if not ok:
        conn.execute("UPDATE dunning_letters SET error = ?, updated_at = ? WHERE id = ?", (str(err)[:400], now, lid))
        conn.commit()
        return False, f"Versturen mislukt: {err}"
    fid = save_bytes("dunning_letter", lid, pdf, fname, kind="brief", mime="application/pdf")
    conn = get_db()
    conn.execute("UPDATE dunning_letters SET status = 'verstuurd', number = ?, sent_by = ?, sent_at = ?, error = NULL, pdf_file_id = ?, subject = ?,"
                 " invoice_ids = ?, updated_at = ? WHERE id = ?", (number, g.user["id"], now, fid, subject, json.dumps([i["id"] for i in invs]), now, lid))
    conn.execute("INSERT INTO dunning_messages (letter_id, kind, body, mail_meta, created_by, created_at) VALUES (?, 'mail_uit', ?, ?, ?, ?)",
                 (lid, L["body"], json.dumps({"to": to, "subject": subject, "files": [a[0] for a in atts]}, ensure_ascii=False), g.user["id"], now))
    for inv in invs:
        conn.execute("UPDATE sales_invoices SET dun_level = MAX(dun_level, ?), dunned_at = ? WHERE id = ?", (L["level"], now, inv["id"]))
    conn.commit()
    nrs = ", ".join(i["number"] for i in invs)
    for inv in invs:
        audit(f"{LEVELS[L['level']].lower()} verstuurd", "sales_invoice", inv["id"], f"{inv['number']} aan {', '.join(to)}")
    return True, f"{LEVELS[L['level']]} {number} verstuurd aan {', '.join(to)} (factuur {nrs})."


def next_number(conn):
    """Volgend herinneringnummer: H-<jaar>-0001, H-<jaar>-0002, ..."""
    y = _today().year
    r = conn.execute("SELECT MAX(CAST(substr(number, 8) AS INTEGER)) AS m FROM dunning_letters WHERE number LIKE ?", (f"H-{y}-%",)).fetchone()
    return f"H-{y}-{(r['m'] or 0) + 1:04d}"


def _subject_with(subject, number, lid):
    s_ = DUN_TAG.sub("", subject or "").strip()
    if number and number not in s_:
        s_ = f"{s_} ({number})"
    return f"{s_} [WP-H{lid}]"


def _attachments(conn, L, invs):
    """Brief (PDF) + de factuur-PDF's."""
    from .util import file_path
    pdf = letter_pdf(conn, L, invs)
    title = DUN.get(L["language"] or "nl", DUN["nl"])["title"][L["level"]]
    cname = re.sub(r"[^\w .-]+", "", (invs[0]["customer_name"] or "")).strip()[:60]
    fname = f"{title} {L.get('number') or ''} - {cname} - {_today().strftime('%d-%m-%Y')}.pdf".replace("  ", " ")
    atts = [(fname, pdf, "application/pdf")]
    for inv in invs:
        if inv["pdf_file_id"]:
            f = conn.execute("SELECT * FROM files WHERE id = ?", (inv["pdf_file_id"],)).fetchone()
            if f:
                try:
                    with open(file_path(f), "rb") as fh:
                        atts.append((f"Factuur_{inv['number']}.pdf", fh.read(), "application/pdf"))
                except OSError:
                    pass
    return pdf, atts


def resend_letter(conn, lid, to):
    """Verstuurde herinnering opnieuw sturen (zelfde nummer), met de facturen die nu nog openstaan."""
    from .mail import send_mail_with_files
    L = conn.execute("SELECT * FROM dunning_letters WHERE id = ?", (lid,)).fetchone()
    if not L or L["status"] != "verstuurd":
        return False, "Alleen een verstuurde herinnering kan opnieuw worden verstuurd."
    if not to or any(not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a) for a in to):
        return False, "Vul een geldig e-mailadres in."
    ids = letter_ids(L)
    invs = [r for r in conn.execute(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))}) ORDER BY due_date, number", ids)
            if r["status"] == "open"]
    if not invs:
        return False, "Alle facturen van deze herinnering zijn inmiddels betaald."
    pdf, atts = _attachments(conn, dict(L), invs)
    ok, err = send_mail_with_files(sender(conn), to, L["subject"], L["body"], atts, signer="administratie")
    if not ok:
        return False, f"Versturen mislukt: {err}"
    now = now_iso()
    fid = save_bytes("dunning_letter", lid, pdf, atts[0][0], kind="brief", mime="application/pdf")
    conn = get_db()
    conn.execute("UPDATE dunning_letters SET pdf_file_id = ?, updated_at = ? WHERE id = ?", (fid, now, lid))
    conn.execute("INSERT INTO dunning_messages (letter_id, kind, body, mail_meta, created_by, created_at) VALUES (?, 'mail_uit', ?, ?, ?, ?)",
                 (lid, L["body"], json.dumps({"to": to, "subject": L["subject"], "files": [a[0] for a in atts], "resend": True}, ensure_ascii=False),
                  g.user["id"], now))
    for inv in invs:
        conn.execute("UPDATE sales_invoices SET dunned_at = ? WHERE id = ?", (now, inv["id"]))
    conn.commit()
    audit("herinnering opnieuw verstuurd", "dunning_letter", lid, f"{L['number']} aan {', '.join(to)}")
    return True, f"{L['number'] or LEVELS[L['level']]} opnieuw verstuurd aan {', '.join(to)}."


def pending_count():
    if "_dun_count" not in g:
        g._dun_count = query("SELECT COUNT(*) c FROM dunning_letters WHERE status = 'voorstel'", one=True)["c"] if can("debiteuren") else 0
    return g._dun_count


def _letters_for_invoice(sid, statuses=("voorstel", "verstuurd")):
    rows = query("SELECT l.*, u.name AS who FROM dunning_letters l LEFT JOIN users u ON u.id = COALESCE(l.sent_by, l.created_by)"
                 f" WHERE l.status IN ({','.join('?' * len(statuses))}) ORDER BY l.id DESC", list(statuses))
    out = []
    for r in rows:
        if sid in letter_ids(r):
            d = dict(r)
            d["replies"] = query("SELECT COUNT(*) c FROM dunning_messages WHERE letter_id = ? AND kind = 'mail_in'", (r["id"],), one=True)["c"]
            out.append(d)
    return out


@bp.route("/aanmaningen", methods=["GET", "POST"])
@require("debiteuren")
def dunning():
    conn = get_db()
    if request.method == "POST" and request.form.get("action") == "instellingen":
        if not can("debiteuren", BEHEER):
            abort(403)
        try:
            days = [int(request.form.get(f"d{i}") or 0) for i in (1, 2, 3)]
        except ValueError:
            days = list(DEFAULT_DAYS)
        if not (0 <= days[0] < days[1] < days[2] <= 365):
            flash("De dagen moeten oplopen (bijv. 7, 21, 35).", "error")
        else:
            snd = (request.form.get("sender") or "").strip()
            if snd and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", snd):
                flash("Ongeldig afzenderadres.", "error")
                return redirect(url_for("debiteuren.dunning"))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_days', ?)", (",".join(map(str, days)),))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_sender', ?)", (snd or DEFAULT_SENDER,))
            conn.commit()
            audit("instellingen", "debiteuren", None, f"aanmaningen na {days} dagen, afzender {snd or DEFAULT_SENDER}")
            flash("Instellingen opgeslagen.", "ok")
        return redirect(url_for("debiteuren.dunning"))
    if request.method == "POST" and request.form.get("action") == "voorstellen":
        n = propose(conn)
        flash(f"{n} nieuw{'e' if n != 1 else ''} voorstel{'len' if n != 1 else ''} gemaakt." if n else "Er is nu niets aan de beurt.", "ok")
        return redirect(url_for("debiteuren.dunning"))
    cleanup(conn)
    conn.commit()
    letters = query("SELECT l.*, c.name AS cname, c.invoice_email, u.name AS creator FROM dunning_letters l"
                    " LEFT JOIN customers c ON c.id = l.customer_id LEFT JOIN users u ON u.id = l.created_by"
                    " WHERE l.status = 'voorstel' ORDER BY l.level DESC, l.id")
    items = []
    today = _today()
    for L in letters:
        d = dict(L)
        ids = letter_ids(L)
        invs = query(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))}) ORDER BY due_date, number", ids)
        d["invs"] = []
        for i in invs:
            x = dict(i)
            dd = _d(i["due_date"])
            x["days_over"] = (today - date.fromisoformat(dd)).days if dd else None
            d["invs"].append(x)
        # andere vervallen facturen van deze klant die (nog) niet in de brief zitten
        d["extra"] = [dict(r) for r in _group_invoices(conn, L["group_key"] or f"i{L['invoice_id']}") if r["id"] not in ids]
        d["total"] = sum(_open(i) for i in invs)
        d["cname"] = d["cname"] or (invs[0]["customer_name"] if invs else "")
        items.append(d)
    sent = query("SELECT l.*, c.name AS cname, u.name AS who FROM dunning_letters l LEFT JOIN customers c ON c.id = l.customer_id"
                 " LEFT JOIN users u ON u.id = l.sent_by WHERE l.status = 'verstuurd' ORDER BY l.sent_at DESC LIMIT 50")
    sent = [dict(s, n=len(letter_ids(s)), first=query("SELECT number, customer_name FROM sales_invoices WHERE id = ?", (letter_ids(s)[0],), one=True),
                 replies=query("SELECT COUNT(*) c FROM dunning_messages WHERE letter_id = ? AND kind = 'mail_in'", (s["id"],), one=True)["c"]) for s in sent]
    return render_template("debiteuren/aanmaningen.html", items=items, sent=sent, LEVELS=LEVELS, days=dun_days(), sender=sender(),
                           has_data=bool(_setting("deb_last_update")))


def _letter(lid):
    return query("SELECT * FROM dunning_letters WHERE id = ?", (lid,), one=True) or abort(404)


def _save_letter(lid):
    f = request.form
    L = _letter(lid)
    # welke facturen meegaan (aanvinken); alleen facturen van dezelfde klant die mogen
    if f.get("has_invoices"):
        allowed = {r["id"] for r in _group_invoices(get_db(), L["group_key"] or f"i{L['invoice_id']}")}
        chosen = [int(x) for x in f.getlist("inv") if str(x).isdigit() and int(x) in allowed]
        if chosen:
            execute("UPDATE dunning_letters SET invoice_ids = ?, invoice_id = ? WHERE id = ?", (json.dumps(chosen), chosen[0], lid))
    execute("UPDATE dunning_letters SET to_addr = ?, subject = ?, body = ?, level = ?, updated_at = ? WHERE id = ? AND status = 'voorstel'",
            ((f.get("to_addr") or "").strip()[:500], (f.get("subject") or "").strip()[:300], (f.get("body") or "").strip()[:10000],
             f.get("level", type=int) if f.get("level", type=int) in LEVELS else L["level"], now_iso(), lid))
    to = (f.get("to_addr") or "").strip()
    if f.get("save_to_customer") and re.match(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$", to) and L["customer_id"]:
        execute("UPDATE customers SET invoice_email = ?, updated_at = ? WHERE id = ? AND IFNULL(invoice_email, '') = ''", (to, now_iso(), L["customer_id"]))
        audit("factuur-e-mail ingevuld", "customer", L["customer_id"], to)


@bp.route("/aanmaningen/<int:lid>", methods=["POST"])
@require("debiteuren", BEWERKEN)
def letter_action(lid):
    L = _letter(lid)
    act = request.form.get("action")
    if L["status"] != "voorstel":
        flash("Deze brief is al verstuurd of vervallen.", "error")
        return redirect(url_for("debiteuren.dunning"))
    if act == "versturen":
        if not can("debiteuren", BEHEER):
            abort(403)
        _save_letter(lid)
        ok, msg = send_letter(get_db(), lid)
        flash(msg, "ok" if ok else "error")
    elif act == "opslaan":
        _save_letter(lid)
        flash("Opgeslagen.", "ok")
    elif act == "tekst":
        # tekst opnieuw opbouwen voor de aangevinkte facturen (na wijzigen van facturen of soort)
        _save_letter(lid)
        L = _letter(lid)
        invs = query(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(letter_ids(L)))}) ORDER BY due_date, number", letter_ids(L))
        subject, body = letter_text(invs, L["level"], L["language"] or "nl")
        execute("UPDATE dunning_letters SET subject = ?, body = ?, updated_at = ? WHERE id = ?", (subject, body, now_iso(), lid))
        flash("Onderwerp en tekst opnieuw opgesteld.", "ok")
    elif act == "verwijderen":
        execute("UPDATE dunning_letters SET status = 'vervallen', note = ?, updated_at = ? WHERE id = ?",
                (f"Weggegooid door {g.user['name']}", now_iso(), lid))
        flash("Voorstel weggegooid. Bij de volgende ronde komt het terug als de facturen dan nog openstaan; gebruik 'Uitstellen' om dat te voorkomen.", "ok")
    nxt = _safe_next(request.form.get("next"))
    return redirect(nxt or (url_for("debiteuren.dunning") + f"#l{lid}"))


@bp.route("/aanmaningen/<int:lid>/brief.pdf")
@require("debiteuren")
def letter_preview(lid):
    from flask import Response
    L = _letter(lid)
    if L["status"] == "verstuurd" and L["pdf_file_id"]:
        return redirect(url_for("main.file", fid=L["pdf_file_id"]))
    ids = letter_ids(L)
    invs = query(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))}) ORDER BY due_date, number", ids)
    if not invs:
        abort(404)
    pdf = letter_pdf(get_db(), L, invs)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="brief-{lid}.pdf"'})


@bp.route("/aanmaningen/<int:lid>/uitstellen", methods=["POST"])
@require("debiteuren", BEWERKEN)
def letter_postpone(lid):
    L = _letter(lid)
    for sid in letter_ids(L):
        _postpone_one(sid)
    flash("Uitgesteld." if request.form.get("when") != "geen" else "Uitstel opgeheven.", "ok")
    return redirect(url_for("debiteuren.dunning"))


def _safe_next(url):
    url = (url or "").strip()
    return url if url.startswith("/debiteuren/") and "//" not in url and "\\" not in url else None


@bp.route("/<int:sid>/aanmanen", methods=["POST"])
@require("debiteuren", BEWERKEN)
def make_letter(sid):
    conn = get_db()
    inv = conn.execute("SELECT * FROM sales_invoices WHERE id = ?", (sid,)).fetchone() or abort(404)
    if inv["status"] != "open":
        flash("Deze factuur staat niet open.", "error")
        return redirect(url_for("debiteuren.detail", sid=sid))
    level = request.form.get("level", type=int) or min(inv["dun_level"] + 1, 3)
    level = level if level in LEVELS else 1
    key = group_key(inv)
    invs = _group_invoices(conn, key)
    if not any(i["id"] == sid for i in invs):   # nog niet vervallen of uitgesteld: dan alleen deze
        invs = [inv] + invs
    conn.execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Vervangen door een nieuw voorstel', updated_at = ?"
                 " WHERE status = 'voorstel' AND (group_key = ? OR invoice_id = ?)", (now_iso(), key, sid))
    lid = new_letter(conn, invs, level, auto=False, uid=g.user["id"])
    conn.commit()
    return redirect(url_for("debiteuren.dunning") + f"#l{lid}")


def _postpone_one(sid):
    from datetime import timedelta
    inv = query("SELECT * FROM sales_invoices WHERE id = ?", (sid,), one=True)
    if not inv:
        return False
    w = request.form.get("when")
    t = _today()
    until = {"1w": t + timedelta(days=7), "2w": t + timedelta(days=14), "1m": t + timedelta(days=30)}.get(w)
    if w == "datum":
        until = _parse_iso(request.form.get("until"))
    if w == "geen":
        execute("UPDATE sales_invoices SET postpone_until = NULL, postpone_reason = NULL WHERE id = ?", (sid,))
        return True
    if not until or until <= t:
        return False
    reason = (request.form.get("reason") or "").strip()[:500] or None
    execute("UPDATE sales_invoices SET postpone_until = ?, postpone_reason = ? WHERE id = ?", (until.isoformat(), reason, sid))
    audit("aanmanen uitgesteld", "sales_invoice", sid, f"{inv['number']} tot {until.isoformat()}" + (f": {reason}" if reason else ""))
    conn = get_db()
    cleanup(conn)
    conn.commit()
    return True


@bp.route("/<int:sid>/uitstellen", methods=["POST"])
@require("debiteuren", BEWERKEN)
def postpone(sid):
    query("SELECT id FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    if _postpone_one(sid):
        flash("Uitstel opgeheven." if request.form.get("when") == "geen" else "Uitgesteld: geen herinneringen voor deze factuur tot de gekozen datum.", "ok")
    else:
        flash("Kies een datum in de toekomst.", "error")
    return redirect(_safe_next(request.form.get("next")) or url_for("debiteuren.detail", sid=sid))


def _parse_iso(v):
    try:
        return date.fromisoformat((v or "")[:10])
    except ValueError:
        return None


@bp.route("/<int:sid>/niet-aanmanen", methods=["POST"])
@require("debiteuren", BEWERKEN)
def toggle_no_dunning(sid):
    inv = query("SELECT * FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    new = 0 if inv["no_dunning"] else 1
    execute("UPDATE sales_invoices SET no_dunning = ? WHERE id = ?", (new, sid))
    conn = get_db()
    cleanup(conn)
    conn.commit()
    audit("niet aanmanen " + ("aan" if new else "uit"), "sales_invoice", sid, inv["number"])
    flash(f"Factuur {inv['number']}: " + ("wordt niet aangemaand." if new else "wordt weer meegenomen bij herinneringen."), "ok")
    return redirect(_safe_next(request.form.get("next")) or url_for("debiteuren.detail", sid=sid))


@bp.route("/<int:sid>/betaald", methods=["POST"])
@require("debiteuren", BEWERKEN)
def mark_paid(sid):
    inv = query("SELECT * FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    now = now_iso()
    execute("UPDATE sales_invoices SET status = 'betaald', open_amount = 0, paid = COALESCE(amount, paid), paid_at = ?,"
            " status_source = 'handmatig', status_at = ? WHERE id = ?", (now, now, sid))
    conn = get_db()
    cleanup(conn)
    conn.commit()
    audit("handmatig op betaald", "sales_invoice", sid, inv["number"])
    flash(f"Factuur {inv['number']} staat op betaald. Staat hij bij de volgende import nog open in SnelStart, dan gaat hij weer op open.", "ok")
    return redirect(url_for("debiteuren.detail", sid=sid))


# ---------------------------------------------------------------- mailgesprek bij een herinnering

def poll_replies(conn, upload_dir, lookback_days=None):
    """Antwoorden op herinneringen uit de mailbox (afzender, bijv. administratie@) ophalen: mails met [WP-H..] in het
    onderwerp of uit dezelfde mailwisseling. Alleen lezen (en naar de WorkPortal-map verplaatsen, zoals bij service)."""
    import base64 as _b64
    from datetime import timedelta
    from .crediteuren import _reply_text
    from .mail import _graph_token, graph_retry, graph_error_text
    from .service import svc_folder, _move_to_folder, _save_file_conn
    from .util import safe_filename
    box = sender(conn)
    since = _setting("deb_mail_since", conn) or (now_utc() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if lookback_days:
        since = min(since, (now_utc() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    headers = {"Authorization": f"Bearer {_graph_token()}"}
    skip = set()
    for wk in ("sentitems", "drafts", "outbox"):
        try:
            rf = graph_retry("GET", f"https://graph.microsoft.com/v1.0/users/{box}/mailFolders/{wk}?$select=id", headers=headers, timeout=30)
            if rf.status_code < 400 and rf.json().get("id"):
                skip.add(rf.json()["id"])
        except Exception:  # noqa: BLE001
            pass
    own = box.rpartition("@")[2]
    url = (f"https://graph.microsoft.com/v1.0/users/{box}/messages?$filter=receivedDateTime ge {since}"
           f"&$select=id,subject,from,receivedDateTime,hasAttachments,conversationId,parentFolderId,internetMessageId&$orderby=receivedDateTime asc&$top=50")
    folder, mcache = svc_folder(conn), {}
    n, newest, pages = 0, since, 0
    while url and pages < 10:
        pages += 1
        r = graph_retry("GET", url, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Mailbox {box} lezen: " + graph_error_text(r.status_code, "mislukt"))
        js = r.json()
        for m in js.get("value", []):
            newest = max(newest, m.get("receivedDateTime") or newest)
            mid, imid = m["id"], (m.get("internetMessageId") or "")[:300]
            if conn.execute("SELECT 1 FROM dunning_messages WHERE kind = 'mail_in' AND (mail_meta LIKE ? OR (? <> '' AND mail_meta LIKE ?))",
                            (f'%"{mid}"%', imid, f'%"{imid}"%')).fetchone():
                continue
            tag = DUN_TAG.search(m.get("subject") or "")
            lid = int(tag.group(1)) if tag else None
            if lid and not conn.execute("SELECT 1 FROM dunning_letters WHERE id = ? AND status = 'verstuurd'", (lid,)).fetchone():
                lid = None
            if not lid and m.get("conversationId"):
                hit = conn.execute("SELECT id FROM dunning_letters WHERE mail_conv = ? ORDER BY id DESC LIMIT 1", (m["conversationId"],)).fetchone()
                lid = hit["id"] if hit else None
            if not lid:
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            fa = (frm.get("address") or "").lower()
            if m.get("parentFolderId") in skip or fa == box.lower() or fa.startswith("/o=") or (own and fa.endswith("@" + own)):
                continue
            rb = graph_retry("GET", f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}?$select=uniqueBody,body,toRecipients,ccRecipients",
                             headers=dict(headers, Prefer='outlook.body-content-type="text"'), timeout=30)
            js2 = rb.json() if rb.status_code < 400 else {}
            body = _reply_text((js2.get("uniqueBody") or {}).get("content") or (js2.get("body") or {}).get("content") or "")
            rc = lambda k: [((x.get("emailAddress") or {}).get("address") or "").lower() for x in (js2.get(k) or [])]   # noqa: E731
            meta = {"from": fa, "from_name": frm.get("name") or "", "subject": m.get("subject") or "", "msg_id": mid, "imid": imid,
                    "to": [a for a in rc("toRecipients") if a], "cc": [a for a in rc("ccRecipients") if a]}
            cur = conn.execute("INSERT INTO dunning_messages (letter_id, kind, body, mail_meta, created_at) VALUES (?, 'mail_in', ?, ?, ?)",
                               (lid, body or "(geen tekst)", json.dumps(meta, ensure_ascii=False), m.get("receivedDateTime") or now_iso()))
            msg_id = cur.lastrowid
            if m.get("hasAttachments"):
                ar = graph_retry("GET", f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}/attachments", headers=headers, timeout=60)
                names = []
                for a in (ar.json().get("value", []) if ar.status_code < 400 else []):
                    if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                        continue
                    data = _b64.b64decode(a.get("contentBytes") or "")
                    if data:
                        nm = safe_filename(a.get("name") or "bijlage")
                        _save_file_conn(conn, upload_dir, "dunning_message", msg_id, data, nm, a.get("contentType") or "application/octet-stream")
                        names.append(nm)
                if names:
                    meta["files"] = names
            conn.execute("UPDATE dunning_letters SET mail_conv = COALESCE(mail_conv, ?) WHERE id = ?", (m.get("conversationId"), lid))
            conn.commit()
            n += 1
            if folder:
                newid = _move_to_folder(box, mid, folder, mcache)
                if newid:
                    meta.update(msg_id=newid, folder=folder)
            conn.execute("UPDATE dunning_messages SET mail_meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), msg_id))
            conn.commit()
            try:
                from .notify import notify_user
                L = conn.execute("SELECT * FROM dunning_letters WHERE id = ?", (lid,)).fetchone()
                who = frm.get("name") or fa or "klant"
                if L["sent_by"]:
                    notify_user(conn, L["sent_by"], f"Reactie op {LEVELS[L['level']].lower()}", f"{who}: " + (body[:140] or "(bijlage)"),
                                url=f"/debiteuren/aanmaningen/{lid}/gesprek", email=False)
            except Exception as exc:  # pragma: no cover
                print(f"[WorkPortal] Melding reactie herinnering mislukt: {exc}", flush=True)
        url = js.get("@odata.nextLink")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_mail_since', ?)", (newest,))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_mail_last', ?)", (now_iso(),))
    conn.commit()
    return n


def scheduled_replies(conn, app, state):
    """Elke 5 minuten (als er de laatste 6 maanden herinneringen zijn verstuurd) de mailbox op antwoorden controleren."""
    from .mail import graph_configured
    import time
    if not graph_configured() or time.time() - state.get("deb_mail", 0) < 300:
        return
    state["deb_mail"] = time.time()
    if not conn.execute("SELECT 1 FROM dunning_letters WHERE status = 'verstuurd' AND sent_at > datetime('now', '-180 days')").fetchone():
        return
    try:
        poll_replies(conn, app.config["UPLOAD_DIR"])
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_mail_error', '')")
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_mail_error', ?)", (str(exc)[:300],))
    conn.commit()


def _thread(lid):
    from .util import files_for
    msgs = query("SELECT m.*, u.name AS author FROM dunning_messages m LEFT JOIN users u ON u.id = m.created_by WHERE m.letter_id = ? ORDER BY m.created_at, m.id", (lid,))
    out = []
    for m in msgs:
        d = dict(m)
        try:
            d["meta"] = json.loads(m["mail_meta"] or "{}")
        except ValueError:
            d["meta"] = {}
        d["files"] = files_for("dunning_message", m["id"])
        out.append(d)
    return out


@bp.route("/aanmaningen/<int:lid>/gesprek", methods=["GET", "POST"])
@require("debiteuren")
def conversation(lid):
    L = _letter(lid)
    if L["status"] != "verstuurd":
        return redirect(url_for("debiteuren.dunning") + f"#l{lid}")
    conn = get_db()
    if request.method == "POST":
        if not can("debiteuren", BEWERKEN):
            abort(403)
        act = request.form.get("action")
        text = (request.form.get("body") or "").strip()[:10000]
        if act == "ophalen":
            from flask import current_app
            from .mail import graph_configured
            if not graph_configured():
                flash("De Microsoft-koppeling is niet ingesteld; antwoorden kunnen niet worden opgehaald.", "error")
            else:
                try:
                    n = poll_replies(conn, current_app.config["UPLOAD_DIR"], lookback_days=30)
                    flash(f"{n} nieuw{'e' if n != 1 else ''} antwoord{'en' if n != 1 else ''} opgehaald." if n else "Geen nieuwe antwoorden.", "ok")
                except Exception as exc:  # noqa: BLE001
                    flash(f"Ophalen mislukt: {str(exc)[:300]}", "error")
        elif act == "opnieuw":
            if not can("debiteuren", BEHEER):
                abort(403)
            to = [a.strip() for a in re.split(r"[;,\s]+", request.form.get("to") or "") if a.strip()]
            ok, msg = resend_letter(conn, lid, to)
            flash(msg, "ok" if ok else "error")
        elif act == "notitie" and text:
            execute("INSERT INTO dunning_messages (letter_id, kind, body, created_by, created_at) VALUES (?, 'notitie', ?, ?, ?)",
                    (lid, text, g.user["id"], now_iso()))
            flash("Notitie toegevoegd.", "ok")
        elif act == "mail" and text:
            from .mail import reply_in_thread, send_mail_with_files
            to = [a.strip() for a in re.split(r"[;,\s]+", request.form.get("to") or "") if a.strip()]
            if not to or any(not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a) for a in to):
                flash("Vul een geldig e-mailadres in.", "error")
                return redirect(url_for("debiteuren.conversation", lid=lid) + "#antwoord")
            atts = [(f.filename, f.read(), f.mimetype) for f in request.files.getlist("files") if f and f.filename][:10]
            last_in = next((m for m in reversed(_thread(lid)) if m["kind"] == "mail_in" and m["meta"].get("msg_id")), None)
            if last_in:
                ok, err = reply_in_thread(sender(conn), last_in["meta"]["msg_id"], to, text, atts, signer="administratie")
                subject = "RE: " + (last_in["meta"].get("subject") or L["subject"])
            else:
                subject = "RE: " + L["subject"]
                ok, err = send_mail_with_files(sender(conn), to, subject, text, atts, signer="administratie")
            if not ok:
                flash(f"Versturen mislukt: {err}", "error")
                return redirect(url_for("debiteuren.conversation", lid=lid) + "#antwoord")
            cur = conn.execute("INSERT INTO dunning_messages (letter_id, kind, body, mail_meta, created_by, created_at) VALUES (?, 'mail_uit', ?, ?, ?, ?)",
                               (lid, text, json.dumps({"to": to, "subject": subject, "files": [a[0] for a in atts]}, ensure_ascii=False), g.user["id"], now_iso()))
            for name, data, mime in atts:
                save_bytes("dunning_message", cur.lastrowid, data, name, mime=mime)
            conn.commit()
            audit("mail over herinnering", "dunning_letter", lid, ", ".join(to))
            flash(f"Verstuurd aan {', '.join(to)}.", "ok")
        return redirect(url_for("debiteuren.conversation", lid=lid))
    ids = letter_ids(L)
    invs = query(f"SELECT * FROM sales_invoices WHERE id IN ({','.join('?' * len(ids))}) ORDER BY due_date, number", ids)
    cust = query("SELECT id, name, invoice_email FROM customers WHERE id = ?", (L["customer_id"],), one=True) if L["customer_id"] else None
    msgs = _thread(lid)
    last_in = next((m for m in reversed(msgs) if m["kind"] == "mail_in"), None)
    reply_to = (last_in["meta"].get("from") if last_in else None) or L["to_addr"]
    who = query("SELECT name FROM users WHERE id = ?", (L["sent_by"],), one=True) if L["sent_by"] else None
    from .mail import graph_configured
    state = {"last": _setting("deb_mail_last"), "error": _setting("deb_mail_error"), "on": graph_configured()}
    return render_template("debiteuren/gesprek.html", L=L, invs=invs, cust=cust, msgs=msgs, reply_to=reply_to, LEVELS=LEVELS,
                           who=who, box=sender(), state=state)


@bp.route("/herinneringen")
@require("debiteuren")
def letters():
    """Overzicht van alle verstuurde herinneringen/aanmaningen (met nummer), gekoppeld aan de facturen."""
    q = (request.args.get("q") or "").strip()
    flt = request.args.get("f") or "alle"
    rows = query("SELECT l.*, c.name AS cname, u.name AS who, (SELECT COUNT(*) FROM dunning_messages m WHERE m.letter_id = l.id AND m.kind = 'mail_in') AS replies,"
                 " (SELECT MAX(created_at) FROM dunning_messages m WHERE m.letter_id = l.id AND m.kind = 'mail_in') AS last_reply"
                 " FROM dunning_letters l LEFT JOIN customers c ON c.id = l.customer_id LEFT JOIN users u ON u.id = l.sent_by"
                 " WHERE l.status = 'verstuurd' ORDER BY l.sent_at DESC LIMIT 1000")
    inv = {r["id"]: r for r in query("SELECT id, number, status, open_amount, amount, customer_name FROM sales_invoices")}
    out = []
    for r in rows:
        d = dict(r)
        d["invs"] = [inv[i] for i in letter_ids(r) if i in inv]
        d["open"] = sum((i["open_amount"] if i["open_amount"] is not None else (i["amount"] or 0)) for i in d["invs"] if i["status"] == "open")
        d["all_paid"] = bool(d["invs"]) and all(i["status"] == "betaald" for i in d["invs"])
        d["cname"] = d["cname"] or (d["invs"][0]["customer_name"] if d["invs"] else "")
        if flt == "open" and d["all_paid"]:
            continue
        if flt == "antwoord" and not d["replies"]:
            continue
        if q and q.lower() not in " ".join([d["number"] or "", d["cname"] or "", d["to_addr"] or ""] + [i["number"] for i in d["invs"]]).lower():
            continue
        out.append(d)
    return render_template("debiteuren/herinneringen.html", rows=out, q=q, flt=flt, LEVELS=LEVELS)
