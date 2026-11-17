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
    known = {r["number"]: r for r in conn.execute("SELECT number, pdf_sig, dossier_path FROM sales_invoices")}
    want, new = [], 0
    for num, (_, f, path) in sorted(best.items(), reverse=True):   # nieuwste facturen eerst
        cur = known.get(num)
        if not cur:
            conn.execute("INSERT OR IGNORE INTO sales_invoices (number, dossier_path, status, created_at) VALUES (?,?, 'onbekend', ?)", (num, path, now))
            new += 1
        elif cur["dossier_path"] != path:
            conn.execute("UPDATE sales_invoices SET dossier_path = ? WHERE number = ?", (path, num))
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
    fname = f"Factuur_{inv['number']}.pdf"
    old = inv["pdf_file_id"]
    fid = save_bytes("sales_invoice", inv["id"], data, fname, kind="factuur", mime="application/pdf")
    try:
        info = parse_pdf(data)
    except Exception as exc:  # noqa: BLE001 - PDF bewaren, uitlezen is bijzaak
        info = {"fout": f"{type(exc).__name__}: {str(exc)[:150]}"}
    sig = f"{js.get('filename')}|{js.get('modified') or ''}|{js.get('size') or ''}"[:400]
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


PARSE_VERSION = "2"


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
    letters = query("SELECT l.*, u.name AS who FROM dunning_letters l LEFT JOIN users u ON u.id = COALESCE(l.sent_by, l.created_by)"
                    " WHERE l.invoice_id = ? AND l.status IN ('voorstel', 'verstuurd') ORDER BY l.id DESC", (sid,))
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
DEFAULT_DAYS = (7, 21, 35)
DEFAULT_SENDER = "administratie@devreugd-pt.nl"
IBAN = "NL70 RABO 0133 9900 36"

DUN = {
    "nl": {
        "subject": {1: "Herinnering factuur {nr}", 2: "Aanmaning factuur {nr}", 3: "Laatste aanmaning factuur {nr}"},
        "body": {
            1: "Geachte heer/mevrouw,\n\nVolgens onze administratie is factuur {nr} van {date} ter waarde van {amount} nog niet (volledig) betaald. "
               "De vervaldatum was {due}; het openstaande bedrag is {open}.\n\nWilt u dit bedrag binnen 7 dagen overmaken op {iban} "
               "onder vermelding van het factuurnummer? Heeft u inmiddels betaald, dan kunt u deze herinnering als niet verzonden beschouwen.\n\n"
               "De factuur is als bijlage meegestuurd.",
            2: "Geachte heer/mevrouw,\n\nOndanks onze eerdere herinnering hebben wij de betaling van factuur {nr} van {date} nog niet ontvangen. "
               "De vervaldatum was {due}, inmiddels {days} dagen geleden. Het openstaande bedrag is {open}.\n\n"
               "Wij verzoeken u dit bedrag binnen 7 dagen over te maken op {iban} onder vermelding van het factuurnummer. "
               "Is er een reden waarom de factuur nog niet betaald is, neem dan contact met ons op.\n\nDe factuur is als bijlage meegestuurd.",
            3: "Geachte heer/mevrouw,\n\nTot onze spijt hebben wij, ondanks herinnering en aanmaning, de betaling van factuur {nr} van {date} "
               "nog steeds niet ontvangen. Het openstaande bedrag is {open} (vervaldatum {due}).\n\n"
               "Wij verzoeken u dringend dit bedrag binnen 5 werkdagen over te maken op {iban} onder vermelding van het factuurnummer. "
               "Blijft betaling uit, dan zijn wij genoodzaakt verdere stappen te nemen; de kosten daarvan komen voor uw rekening.\n\n"
               "De factuur is als bijlage meegestuurd.",
        },
    },
    "en": {
        "subject": {1: "Payment reminder invoice {nr}", 2: "Second reminder invoice {nr}", 3: "Final notice invoice {nr}"},
        "body": {
            1: "Dear Sir/Madam,\n\nAccording to our records, invoice {nr} dated {date} for {amount} has not yet been (fully) paid. "
               "The due date was {due}; the outstanding amount is {open}.\n\nPlease transfer this amount within 7 days to {iban} (BIC RABONL2U), "
               "stating the invoice number. If you have already paid, please disregard this reminder.\n\nThe invoice is attached.",
            2: "Dear Sir/Madam,\n\nDespite our earlier reminder, we have not yet received payment of invoice {nr} dated {date}. "
               "The due date was {due}, {days} days ago. The outstanding amount is {open}.\n\n"
               "We kindly ask you to transfer this amount within 7 days to {iban} (BIC RABONL2U), stating the invoice number. "
               "If there is a reason why the invoice has not been paid, please contact us.\n\nThe invoice is attached.",
            3: "Dear Sir/Madam,\n\nWe regret to note that, despite our reminders, invoice {nr} dated {date} is still unpaid. "
               "The outstanding amount is {open} (due date {due}).\n\n"
               "We urgently request you to transfer this amount within 5 working days to {iban} (BIC RABONL2U), stating the invoice number. "
               "If payment is not received, we will have to take further action, at your expense.\n\nThe invoice is attached.",
        },
    },
    "de": {
        "subject": {1: "Zahlungserinnerung Rechnung {nr}", 2: "Mahnung Rechnung {nr}", 3: "Letzte Mahnung Rechnung {nr}"},
        "body": {
            1: "Sehr geehrte Damen und Herren,\n\nlaut unserer Buchhaltung ist die Rechnung {nr} vom {date} über {amount} noch nicht (vollständig) bezahlt. "
               "Fällig war sie am {due}; der offene Betrag beträgt {open}.\n\nBitte überweisen Sie diesen Betrag innerhalb von 7 Tagen auf {iban} "
               "(BIC RABONL2U) unter Angabe der Rechnungsnummer. Sollten Sie bereits bezahlt haben, betrachten Sie diese Erinnerung bitte als gegenstandslos.\n\n"
               "Die Rechnung finden Sie im Anhang.",
            2: "Sehr geehrte Damen und Herren,\n\ntrotz unserer Erinnerung haben wir die Zahlung der Rechnung {nr} vom {date} noch nicht erhalten. "
               "Sie war am {due} fällig, vor {days} Tagen. Der offene Betrag beträgt {open}.\n\n"
               "Bitte überweisen Sie diesen Betrag innerhalb von 7 Tagen auf {iban} (BIC RABONL2U) unter Angabe der Rechnungsnummer. "
               "Gibt es einen Grund für die ausstehende Zahlung, nehmen Sie bitte Kontakt mit uns auf.\n\nDie Rechnung finden Sie im Anhang.",
            3: "Sehr geehrte Damen und Herren,\n\nleider ist die Rechnung {nr} vom {date} trotz Erinnerung und Mahnung noch immer nicht bezahlt. "
               "Der offene Betrag beträgt {open} (fällig am {due}).\n\n"
               "Wir fordern Sie dringend auf, diesen Betrag innerhalb von 5 Werktagen auf {iban} (BIC RABONL2U) zu überweisen. "
               "Andernfalls sehen wir uns gezwungen, weitere Schritte einzuleiten; die Kosten gehen zu Ihren Lasten.\n\nDie Rechnung finden Sie im Anhang.",
        },
    },
    "fr": {
        "subject": {1: "Rappel facture {nr}", 2: "Relance facture {nr}", 3: "Dernière relance facture {nr}"},
        "body": {
            1: "Madame, Monsieur,\n\nSelon notre comptabilité, la facture {nr} du {date} d'un montant de {amount} n'a pas encore été (entièrement) réglée. "
               "L'échéance était le {due} ; le montant restant dû est de {open}.\n\nNous vous prions de virer ce montant sous 7 jours sur {iban} "
               "(BIC RABONL2U) en indiquant le numéro de facture. Si vous avez déjà payé, veuillez ne pas tenir compte de ce rappel.\n\nLa facture est jointe.",
            2: "Madame, Monsieur,\n\nMalgré notre rappel, nous n'avons pas encore reçu le paiement de la facture {nr} du {date}. "
               "L'échéance était le {due}, il y a {days} jours. Le montant restant dû est de {open}.\n\n"
               "Nous vous prions de virer ce montant sous 7 jours sur {iban} (BIC RABONL2U) en indiquant le numéro de facture. "
               "Si ce retard a une raison, merci de nous contacter.\n\nLa facture est jointe.",
            3: "Madame, Monsieur,\n\nMalgré nos rappels, la facture {nr} du {date} reste impayée. Le montant restant dû est de {open} (échéance le {due}).\n\n"
               "Nous vous demandons instamment de virer ce montant sous 5 jours ouvrables sur {iban} (BIC RABONL2U). "
               "À défaut, nous serons contraints d'engager d'autres démarches, à vos frais.\n\nLa facture est jointe.",
        },
    },
}


def _ddmm(v):
    d = _d(v)
    return date.fromisoformat(d).strftime("%d-%m-%Y") if d else "–"


def _eur(v):
    from .util import fmt_eur
    return fmt_eur(v or 0, 2)


def dun_days(conn=None):
    raw = _setting("deb_days", conn) or ""
    try:
        vals = tuple(int(x) for x in raw.split(","))
        if len(vals) == 3 and all(0 <= v <= 365 for v in vals):
            return vals
    except ValueError:
        pass
    return DEFAULT_DAYS


def sender():
    return (_setting("deb_sender") or DEFAULT_SENDER).strip()


def _cust(conn, inv):
    return conn.execute("SELECT * FROM customers WHERE id = ?", (inv["customer_id"],)).fetchone() if inv["customer_id"] else None


def letter_text(inv, level, lang):
    L = DUN.get(lang) or DUN["nl"]
    dd = _d(inv["due_date"])
    days = (_today() - date.fromisoformat(dd)).days if dd else 0
    v = {"nr": inv["number"], "date": _ddmm(inv["invoice_date"]), "due": _ddmm(inv["due_date"]), "amount": _eur(inv["amount"]),
         "open": _eur(inv["open_amount"] if inv["open_amount"] is not None else inv["amount"]), "days": days, "iban": IBAN}
    return L["subject"][level].format(**v), L["body"][level].format(**v)


def new_letter(conn, inv, level, auto, uid=None):
    c = _cust(conn, inv)
    from .talen import norm_lang
    lang = norm_lang(c["language"] if c and c["language"] else "nl")
    lang = lang if lang in DUN else "en"
    subject, body = letter_text(inv, level, lang)
    to = (c["invoice_email"] if c and c["invoice_email"] else None) or inv["mail_to"] or ""
    cur = conn.execute("INSERT INTO dunning_letters (invoice_id, level, status, auto, language, to_addr, subject, body, created_by, created_at)"
                       " VALUES (?,?, 'voorstel', ?,?,?,?,?,?,?)", (inv["id"], level, 1 if auto else 0, lang, to, subject, body, uid, now_iso()))
    return cur.lastrowid


def _due_for_dunning(inv, days):
    """Welk niveau is aan de beurt (of None)."""
    if inv["status"] != "open" or inv["no_dunning"] or (inv["open_amount"] is not None and inv["open_amount"] <= 0.004):
        return None
    if inv["postpone_until"] and inv["postpone_until"] > _today().isoformat():
        return None
    dd = _d(inv["due_date"])
    if not dd or inv["dun_level"] >= 3:
        return None
    over = (_today() - date.fromisoformat(dd)).days
    level = inv["dun_level"] + 1
    if over < days[level - 1]:
        return None
    if inv["dunned_at"] and (now_utc().astimezone(TZ).date() - date.fromisoformat(inv["dunned_at"][:10])).days < 7:
        return None   # minstens een week tussen twee brieven
    return level


def cleanup(conn):
    """Voorstellen van facturen die betaald, uitgesteld of op 'niet aanmanen' staan, vervallen."""
    conn.execute("UPDATE dunning_letters SET status = 'vervallen', updated_at = ? WHERE status = 'voorstel' AND invoice_id IN"
                 " (SELECT id FROM sales_invoices WHERE status <> 'open' OR no_dunning = 1 OR IFNULL(open_amount, 1) <= 0.004"
                 " OR (postpone_until IS NOT NULL AND postpone_until > ?))", (now_iso(), _today().isoformat()))


def propose(conn):
    """Nieuwe voorstellen maken voor facturen die aan de beurt zijn. Geeft het aantal nieuwe voorstellen."""
    cleanup(conn)
    days = dun_days(conn)
    pending = {r[0] for r in conn.execute("SELECT invoice_id FROM dunning_letters WHERE status = 'voorstel'")}
    n = 0
    for inv in conn.execute("SELECT * FROM sales_invoices WHERE status = 'open' AND no_dunning = 0").fetchall():
        if inv["id"] in pending:
            continue
        level = _due_for_dunning(inv, days)
        if level:
            new_letter(conn, inv, level, auto=True)
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


def send_letter(conn, lid):
    """Versturen (na goedkeuring). Geeft (ok, melding)."""
    from .mail import send_mail_with_files
    from .util import file_path
    L = conn.execute("SELECT * FROM dunning_letters WHERE id = ?", (lid,)).fetchone()
    if not L or L["status"] != "voorstel":
        return False, "Deze brief is al verstuurd of vervallen."
    inv = conn.execute("SELECT * FROM sales_invoices WHERE id = ?", (L["invoice_id"],)).fetchone()
    to = [a.strip() for a in re.split(r"[;,\s]+", L["to_addr"] or "") if a.strip()]
    if not to or any(not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a) for a in to):
        return False, "Vul een geldig e-mailadres in (bij voorkeur de factuur-e-mail bij de relatie)."
    try:
        open_now = snelstart_open(inv["number"])
    except Exception:  # noqa: BLE001 - SnelStart niet bereikbaar: niet tegenhouden, wel melden
        open_now = None
    if open_now is not None and open_now <= 0.004:
        now = now_iso()
        conn.execute("UPDATE sales_invoices SET status = 'betaald', open_amount = 0, paid_at = ?, status_source = 'snelstart', status_at = ? WHERE id = ?",
                     (now, now, inv["id"]))
        conn.execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Betaald volgens SnelStart vlak voor versturen', updated_at = ? WHERE id = ?", (now, lid))
        conn.commit()
        return False, f"Niet verstuurd: factuur {inv['number']} is volgens SnelStart inmiddels betaald."
    atts = []
    if inv["pdf_file_id"]:
        f = conn.execute("SELECT * FROM files WHERE id = ?", (inv["pdf_file_id"],)).fetchone()
        if f:
            with open(file_path(f), "rb") as fh:
                atts.append((f"Factuur_{inv['number']}.pdf", fh.read(), "application/pdf"))
    ok, err = send_mail_with_files(sender(), to, L["subject"], L["body"], atts, signer="administratie")
    now = now_iso()
    if not ok:
        conn.execute("UPDATE dunning_letters SET error = ?, updated_at = ? WHERE id = ?", (str(err)[:400], now, lid))
        conn.commit()
        return False, f"Versturen mislukt: {err}"
    conn.execute("UPDATE dunning_letters SET status = 'verstuurd', sent_by = ?, sent_at = ?, error = NULL, updated_at = ? WHERE id = ?",
                 (g.user["id"], now, now, lid))
    conn.execute("UPDATE sales_invoices SET dun_level = MAX(dun_level, ?), dunned_at = ? WHERE id = ?", (L["level"], now, inv["id"]))
    conn.commit()
    audit(f"{LEVELS[L['level']].lower()} verstuurd", "sales_invoice", inv["id"], f"{inv['number']} aan {', '.join(to)}")
    return True, f"{LEVELS[L['level']]} voor factuur {inv['number']} verstuurd aan {', '.join(to)}."


def pending_count():
    if "_dun_count" not in g:
        g._dun_count = query("SELECT COUNT(*) c FROM dunning_letters WHERE status = 'voorstel'", one=True)["c"] if can("debiteuren") else 0
    return g._dun_count


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
    items = query("SELECT l.*, s.number, s.customer_id, s.customer_name, s.invoice_date, s.due_date, s.amount, s.open_amount, s.pdf_file_id,"
                  " s.dun_level, c.name AS cname, c.invoice_email, u.name AS creator FROM dunning_letters l JOIN sales_invoices s ON s.id = l.invoice_id"
                  " LEFT JOIN customers c ON c.id = s.customer_id LEFT JOIN users u ON u.id = l.created_by"
                  " WHERE l.status = 'voorstel' ORDER BY l.level DESC, s.due_date")
    sent = query("SELECT l.*, s.number, s.customer_name, c.name AS cname, u.name AS who FROM dunning_letters l JOIN sales_invoices s ON s.id = l.invoice_id"
                 " LEFT JOIN customers c ON c.id = s.customer_id LEFT JOIN users u ON u.id = l.sent_by"
                 " WHERE l.status = 'verstuurd' ORDER BY l.sent_at DESC LIMIT 50")
    out = []
    for r in items:
        d = dict(r)
        dd = _d(r["due_date"])
        d["days_over"] = (_today() - date.fromisoformat(dd)).days if dd else None
        out.append(d)
    return render_template("debiteuren/aanmaningen.html", items=out, sent=sent, LEVELS=LEVELS, days=dun_days(), sender=sender(),
                           has_data=bool(_setting("deb_last_update")))


def _letter(lid):
    return query("SELECT * FROM dunning_letters WHERE id = ?", (lid,), one=True) or abort(404)


def _save_letter(lid):
    f = request.form
    execute("UPDATE dunning_letters SET to_addr = ?, subject = ?, body = ?, updated_at = ? WHERE id = ? AND status = 'voorstel'",
            ((f.get("to_addr") or "").strip()[:500], (f.get("subject") or "").strip()[:300], (f.get("body") or "").strip()[:10000], now_iso(), lid))
    to = (f.get("to_addr") or "").strip()
    if f.get("save_to_customer") and re.match(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$", to):
        r = query("SELECT s.customer_id FROM dunning_letters l JOIN sales_invoices s ON s.id = l.invoice_id WHERE l.id = ?", (lid,), one=True)
        if r and r["customer_id"]:
            execute("UPDATE customers SET invoice_email = ?, updated_at = ? WHERE id = ? AND IFNULL(invoice_email, '') = ''", (to, now_iso(), r["customer_id"]))
            execute("UPDATE dunning_letters SET to_addr = ? WHERE status = 'voorstel' AND IFNULL(to_addr, '') = '' AND invoice_id IN"
                    " (SELECT id FROM sales_invoices WHERE customer_id = ?)", (to, r["customer_id"]))
            audit("factuur-e-mail ingevuld", "customer", r["customer_id"], to)


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
    elif act == "verwijderen":
        execute("UPDATE dunning_letters SET status = 'vervallen', note = ?, updated_at = ? WHERE id = ?",
                (f"Weggegooid door {g.user['name']}", now_iso(), lid))
        flash("Voorstel weggegooid. Bij de volgende ronde komt het terug als de factuur dan nog openstaat; gebruik 'Uitstellen' of 'Niet aanmanen' om dat te voorkomen.", "ok")
    nxt = _safe_next(request.form.get("next"))
    return redirect(nxt or (url_for("debiteuren.dunning") + f"#l{lid}"))


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
    conn.execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Vervangen door een nieuw voorstel', updated_at = ? WHERE invoice_id = ? AND status = 'voorstel'",
                 (now_iso(), sid))
    lid = new_letter(conn, inv, level, auto=False, uid=g.user["id"])
    conn.commit()
    return redirect(url_for("debiteuren.dunning") + f"#l{lid}")


@bp.route("/<int:sid>/uitstellen", methods=["POST"])
@require("debiteuren", BEWERKEN)
def postpone(sid):
    from datetime import timedelta
    inv = query("SELECT * FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    w = request.form.get("when")
    t = _today()
    until = {"1w": t + timedelta(days=7), "2w": t + timedelta(days=14), "1m": t + timedelta(days=30)}.get(w)
    if w == "datum":
        until = _parse_iso(request.form.get("until"))
    if w == "geen":
        execute("UPDATE sales_invoices SET postpone_until = NULL, postpone_reason = NULL WHERE id = ?", (sid,))
        flash("Uitstel opgeheven.", "ok")
    elif not until or until <= t:
        flash("Kies een datum in de toekomst.", "error")
    else:
        reason = (request.form.get("reason") or "").strip()[:500] or None
        execute("UPDATE sales_invoices SET postpone_until = ?, postpone_reason = ? WHERE id = ?", (until.isoformat(), reason, sid))
        execute("UPDATE dunning_letters SET status = 'vervallen', note = ?, updated_at = ? WHERE invoice_id = ? AND status = 'voorstel'",
                (f"Uitgesteld tot {until.strftime('%d-%m-%Y')}", now_iso(), sid))
        audit("aanmanen uitgesteld", "sales_invoice", sid, f"{inv['number']} tot {until.isoformat()}" + (f": {reason}" if reason else ""))
        flash(f"Geen herinneringen voor factuur {inv['number']} tot {until.strftime('%d-%m-%Y')}.", "ok")
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
    if new:
        execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Niet aanmanen', updated_at = ? WHERE invoice_id = ? AND status = 'voorstel'", (now_iso(), sid))
    audit("niet aanmanen " + ("aan" if new else "uit"), "sales_invoice", sid, inv["number"])
    flash(f"Factuur {inv['number']}: " + ("wordt niet aangemaand." if new else "wordt weer meegenomen bij herinneringen."), "ok")
    return redirect(_safe_next(request.form.get("next")) or url_for("debiteuren.detail", sid=sid))


@bp.route("/<int:sid>/betaald", methods=["POST"])
@require("debiteuren", BEWERKEN)
def mark_paid(sid):
    inv = query("SELECT * FROM sales_invoices WHERE id = ?", (sid,), one=True) or abort(404)
    now = now_iso()
    execute("UPDATE sales_invoices SET status = 'betaald', open_amount = 0, paid_at = ?, status_source = 'handmatig', status_at = ? WHERE id = ?", (now, now, sid))
    execute("UPDATE dunning_letters SET status = 'vervallen', note = 'Handmatig op betaald', updated_at = ? WHERE invoice_id = ? AND status = 'voorstel'", (now, sid))
    audit("handmatig op betaald", "sales_invoice", sid, inv["number"])
    flash(f"Factuur {inv['number']} staat op betaald. Staat hij bij de volgende import nog open in SnelStart, dan gaat hij weer op open.", "ok")
    return redirect(url_for("debiteuren.detail", sid=sid))
