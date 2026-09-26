"""Orderbon uit Komdex (PDF, FastReport) uitlezen.

Voorbeeld van de kop:
    Order        : 20260102   Leidingwerk            Klantgegevens
    Omschrijving : Aanpassing leidingdeel t.b.v.     Bedrijf : Van Dijk Bakery
                   Foodjet                           Adres   : Het Oude Diep 9
    Uitvoerder   : Wim de Vreugd                     Plaats  : 8064 PN Zwartsluis
    Orderdatum   : 6-7-2026                          Tel     : 038 386 68 33
    Leverdatum   : 24-8-2026                         Ref. nr.:
    Leverweek    : 35                                T.a.v.  : Gijsbert van Dijk
Het woord achter het ordernummer is het ordertype.
"""
import io
import re
from datetime import date

LEFT = {"order": "number", "omschrijving": "description", "uitvoerder": "executor", "orderdatum": "order_date",
        "leverdatum": "delivery_date", "leverweek": "delivery_week"}
RIGHT = {"bedrijf": "customer", "adres": "address", "plaats": "city", "tel": "phone", "ref. nr.": "reference",
         "ref.nr.": "reference", "t.a.v.": "contact"}
STOP = ("ordereigenschappen", "productie memo", "pos aantal")


def _lines(words, tol=3):
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(rows[-1][0] - w["top"]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in rows]


def _date(s):
    m = re.match(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _column(lines, x_min, x_max, labels):
    """Label : waarde per kolom; regels zonder label horen bij het vorige label (bijv. lange omschrijving)."""
    out, cur = {}, None
    for _, ws in lines:
        col = [w for w in ws if x_min <= w["x0"] < x_max]
        if not col:
            continue
        text = " ".join(w["text"] for w in col)
        low = text.lower()
        if any(low.startswith(s) for s in STOP):
            break
        if ":" in [w["text"] for w in col] or re.match(r"^[^:]{1,20}\s*:", text):
            label, _, value = text.partition(":")
            key = labels.get(label.strip().lower())
            cur = key
            if key:
                out[key] = value.strip()
            continue
        if cur and cur in out:
            out[cur] = (out[cur] + " " + text).strip()
    return out


def parse(data):
    """Geeft een dict met de gegevens van de orderbon, of None als het geen (leesbare) orderbon is."""
    import pdfplumber
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception:
        return None
    with pdf:
        if not pdf.pages:
            return None
        page = pdf.pages[0]
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
        if not any(w["text"].lower() == "orderbon" for w in words[:10]):
            return None
        cut = next((w["top"] for w in words if w["text"] in ("Ordereigenschappen", "Pos")), page.height)
        lines = [ln for ln in _lines(words) if ln[0] < cut - 2]
        split = next((w["x0"] for w in words if w["text"] == "Klantgegevens"), page.width * 0.5) - 5
        left = _column(lines, 0, split, LEFT)
        right = _column(lines, split, page.width + 1, RIGHT)
        # werkomschrijving: kolom 'Omschrijving' onder de kop 'Pos Aantal ...', over alle pagina's
        work = []
        for pg in pdf.pages:
            pw = pg.extract_words()
            pl = _lines(pw)
            head = next((i for i, (_, ws) in enumerate(pl) if [w["text"] for w in ws[:2]] == ["Pos", "Aantal"]), None)
            if head is None:
                continue
            xdesc = next((w["x0"] for w in pl[head][1] if w["text"] == "Omschrijving"), None)
            for _, ws in pl[head + 1:]:
                t = " ".join(w["text"] for w in ws)
                if t.upper().startswith("DE VREUGD PRODUCTIETECHNIEK") or t.lower().startswith(("gebruikte materialen", "gebruite materialen")):
                    break
                d = " ".join(w["text"] for w in ws if xdesc is None or w["x0"] >= xdesc - 2)
                if d:
                    work.append(d)
    num = left.get("number", "")
    m = re.match(r"^\s*(\d{8})\s*(.*)$", num)
    if not m:
        return None
    return {
        "number": m.group(1),
        "order_type": m.group(2).strip() or None,
        "description": left.get("description") or None,
        "executor": left.get("executor") or None,
        "order_date": _date(left.get("order_date")),
        "delivery_date": _date(left.get("delivery_date")),
        "delivery_week": left.get("delivery_week") or None,
        "customer": right.get("customer") or None,
        "address": right.get("address") or None,
        "city": right.get("city") or None,
        "phone": right.get("phone") or None,
        "reference": right.get("reference") or None,
        "contact": right.get("contact") or None,
        "work": "\n".join(work).strip() or None,
    }
