"""Calculatie uit Excel (ons eigen calculatieblad) inlezen, om een na-calculatie mee te vergelijken.

Opbouw van het blad (per tabblad 'Tabblad 01', 'Tabblad 02', ...):
    kop:     Aantal | Prijs per stuk | marge+ risico: | Totaal | Inclusief marge | totaal
    sectie:  'Engineering / Tekenwerk/Besprekingen:'  (alleen tekst in kolom A)
    regels:  omschrijving | aantal | prijs | factor | totaal | incl. marge
    totaal:  (optioneel uren in 'Aantal') ... | sectietotaal in 'totaal'
Blad 'Totalen' bevat eventueel de 'Prijsafspraak'.
"""
import io
import re

# sectie in Excel -> trefwoorden om het item in de ERP-export te vinden
KEYWORDS = [
    ("engineering", ["engineering", "tekenwerk", "bespreking"]),
    ("inkopen", ["inkopen", "inkoop", "materiaal", "onderdelen"]),
    ("werkplaats", ["werkplaats", "wvb", "manuren", "arbeid"]),
    ("besturing", ["besturing", "bekabeling", "software", "elektr"]),
    ("transport", ["transport", "reis", "verblijf"]),
    ("montage", ["montage", "locatie"]),
    ("uitbesteding", ["uitbesteding", "uitbesteed"]),
]


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("€", "").replace(".", "").replace(",", ".").strip())
    except ValueError:
        return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse(data):
    """Geeft {"sections": [{"name", "total", "base", "hours", "lines": [...]}], "total", "hours", "agreed_price"}."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sections = {}
    order = []
    agreed = None
    found = False
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if ws.title.lower().startswith("totalen"):
            for r in rows:
                cells = [c for c in r if c not in (None, "")]
                if cells and isinstance(cells[0], str) and cells[0].strip().lower().startswith("prijsafspraak"):
                    agreed = next((_num(c) for c in cells[1:] if _num(c)), None)
            continue
        head = None
        for i, r in enumerate(rows[:30]):
            texts = [str(c).strip().lower() for c in r if isinstance(c, str)]
            if "aantal" in texts and any(t.startswith("inclusief marge") for t in texts):
                head = i
                cols = {str(c).strip().lower().rstrip(":"): j for j, c in enumerate(r) if isinstance(c, str)}
                break
        if head is None:
            continue
        found = True
        c_qty = cols.get("aantal", 1)
        c_price = cols.get("prijs per stuk", 2)
        c_base = cols.get("totaal", 4)
        c_incl = next((j for k, j in cols.items() if k.startswith("inclusief marge")), 5)
        c_sum = max(j for k, j in cols.items() if k == "totaal") if list(cols).count("totaal") else c_incl + 1
        # tweede kolom 'totaal' (kleine letter) = sectietotaal
        tot_cols = [j for j, c in enumerate(rows[head]) if isinstance(c, str) and c.strip().lower() == "totaal"]
        if len(tot_cols) >= 2:
            c_base, c_sum = tot_cols[0], tot_cols[-1]
        cur = None
        for r in rows[head + 1:]:
            r = r + [None] * (max(c_sum, c_incl) + 2 - len(r))
            a = r[0].strip() if isinstance(r[0], str) else None
            nums = {j: _num(r[j]) for j in (c_qty, c_price, c_base, c_incl, c_sum)}
            if a and all(v is None for v in nums.values()):
                name = a.rstrip(":").strip()
                if name.lower() in ("totalen", "totaal", "totaalprijs"):
                    break  # samenvatting onderaan het blad: niet dubbel tellen
                if cur and sections[cur]["lines"]:
                    continue  # tussenkop binnen een sectie (bijv. 'Koopdelen' onder 'Inkopen')
                if name:
                    cur = name
                    if cur not in sections:
                        sections[cur] = {"name": cur, "total": 0.0, "base": 0.0, "hours": 0.0, "lines": [], "has_total": False}
                        order.append(cur)
                continue
            if cur is None:
                continue
            sec = sections[cur]
            if nums[c_sum] is not None:  # sectietotaal
                sec["total"] += nums[c_sum]
                sec["has_total"] = True
                low = (a or "").lower()
                if nums[c_qty] is not None and ("uren" in low or not a):
                    sec["hours"] += nums[c_qty]
                cur = None
                continue
            if a and nums[c_incl] is not None:
                q, p = nums[c_qty] or 0.0, nums[c_price] or 0.0
                sec["lines"].append({"desc": a, "qty": q, "price": p, "base": nums[c_base] or 0.0, "total": nums[c_incl]})
                sec["base"] += nums[c_base] or 0.0
    if not found:
        raise ValueError("Geen calculatieblad gevonden (kolommen 'Aantal' en 'Inclusief marge' ontbreken).")
    out = []
    for name in order:
        s = sections[name]
        if not s["has_total"]:
            s["total"] = sum(l["total"] for l in s["lines"])
        s.pop("has_total", None)
        if abs(s["total"]) < 0.005 and not s["hours"]:
            continue
        s["lines"] = [l for l in s["lines"] if l["total"] or l["qty"]]
        out.append(s)
    return {"sections": out, "total": sum(s["total"] for s in out), "hours": sum(s["hours"] for s in out),
            "agreed_price": agreed if agreed and agreed > 0 else None}


def auto_map(sections, items):
    """Koppelt elke Excel-sectie aan het best passende item uit de na-calculatie. Geeft {sectienaam: itemsleutel}."""
    out = {}
    for s in sections:
        sn = _norm(s["name"])
        best, score = None, 0
        for it in items:
            dn = _norm(it["desc"])
            sc = 0
            for _, words in KEYWORDS:
                if any(w in sn for w in words) and any(w in dn for w in words):
                    sc += 3
            sc += len(set(sn.split()) & set(dn.split()))
            if sc > score:
                best, score = it["key"], sc
        out[s["name"]] = best if score >= 2 else None
    return out


def as_calc(x, items):
    """Vorm zoals engine.compute_calc (by_pos met total/hours) zodat de na-calculatie ermee vergelijkt."""
    mapping = x.get("map") or {}
    pos_of = {str(it["key"]): it["pos"] for it in items if it.get("pos") is not None}
    by_pos, extra_pos = {}, 9000
    for s in x.get("sections", []):
        key = mapping.get(s["name"])
        pos = pos_of.get(str(key)) if key else None
        if pos is None:
            extra_pos += 10
            pos = extra_pos
        agg = by_pos.setdefault(pos, {"total": 0.0, "hours": 0.0, "title": s["name"]})
        agg["total"] += s["total"]
        agg["hours"] += s["hours"]
    return {"by_pos": by_pos, "total": sum(v["total"] for v in by_pos.values()),
            "hours": sum(v["hours"] for v in by_pos.values())}
