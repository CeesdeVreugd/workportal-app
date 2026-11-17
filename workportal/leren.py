"""Zelflerend uitlezen van inkoopfacturen.

Op de factuurkaart klikt iemand na het verbeteren van de gegevens op "Leren". Per leverancier en per veld wordt dan
onthouden wáár de waarde in de PDF staat: het kopje ervoor op dezelfde regel ("Totaal in Euro"), anders het kopje erboven,
anders de vaste plek op de pagina. Bij de volgende factuur van die leverancier gaan deze regels vóór het gewone uitlezen.
"""
import io
import re

from .util import now_iso

FIELDS = ("invoice_no", "invoice_date", "due_date", "amount_incl", "amount_excl", "erp_ref")
LABELS = {"invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum",
          "amount_incl": "bedrag incl.", "amount_excl": "bedrag excl.", "erp_ref": "inkoopnummer"}
KINDS = {"rechts": "staat achter", "onder": "staat onder", "plek": "vaste plek op de pagina"}

_ID = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9\-/.]*\d)[A-Za-z0-9][A-Za-z0-9\-/.]*[A-Za-z0-9]")


def _c():
    from . import crediteuren as c
    return c


def _pages(data):
    """[(sleutel, breedte, hoogte, regels)] van de eerste twee en laatste twee pagina's (sleutel 0, 1, -2, -1)."""
    import pdfplumber
    c = _c()
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        allp = pdf.pages[:40]
        n = len(allp)
        for i in sorted({0, 1, n - 2, n - 1} & set(range(n))):
            p = allp[i]
            try:
                p = p.dedupe_chars(tolerance=1)
            except Exception:  # noqa: BLE001
                pass
            ws = p.extract_words(keep_blank_chars=False, use_text_flow=False, y_tolerance=1)
            for w in ws:
                w["text"] = c._signs(c._clean(w["text"]))
            lines = []
            for top, lw in c._lines(ws):
                text, spans = "", []
                for w in lw:
                    if text:
                        text += " "
                    spans.append((len(text), len(text) + len(w["text"]), w))
                    text += w["text"]
                lines.append({"top": top, "text": text, "spans": spans})
            out.append((i if i < 2 else i - n, float(p.width), float(p.height), lines))
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _candidates(field, text):
    """Mogelijke waarden in een regel: [(start, eind, ruwe tekst, waarde)]."""
    c = _c()
    out = []
    if field.startswith("amount"):
        for m in c._AMT_TOKEN.finditer(text):
            v = c.parse_amount(m.group(1))
            if v is not None:
                out.append((m.start(1), m.end(1), m.group(1), v))
    elif field.endswith("date"):
        for m in re.finditer(c._DATE, text, re.I):
            v = c.parse_date(m.group(1))
            if v:
                out.append((m.start(1), m.end(1), m.group(1), v))
    else:
        for m in _ID.finditer(text):
            out.append((m.start(), m.end(), m.group(0), m.group(0).rstrip(".-/")))
    return out


def _same(field, found, want):
    if want in (None, ""):
        return False
    if field.startswith("amount"):
        return abs(abs(found) - abs(float(want))) < 0.006
    if field.endswith("date"):
        return found == want
    return _norm(found) == _norm(want)


def _label_left(text, start):
    """Het kopje links van de waarde: de laatste woorden zonder cijfers (max. 4), bijv. 'Totaal in Euro'."""
    words = text[:start].split()
    lab = []
    for w in reversed(words):
        if re.search(r"\d", w):
            # 'Van31/10/26': de letters vóór de waarde horen bij het kopje
            if not lab:
                pre = re.match(r"^([^\d]+)", w)
                if pre and re.search(r"[A-Za-z]", pre.group(1)) and text[:start].endswith(w):
                    lab.append(pre.group(1))
            break
        if not re.search(r"[A-Za-z]", w):
            if lab:
                break
            continue   # losse leestekens ('|', ':') tussen kopje en waarde
        lab.append(w)
        if len(lab) >= 4:
            break
    lab = " ".join(reversed(lab)).strip(" :.#-")
    return lab if len(_norm(lab)) >= 2 and re.search(r"[A-Za-z]", lab) else ""


def _words_at(line, a, b):
    return [w for s, e, w in line["spans"] if s < b and e > a]


def _label_above(lines, li, ws):
    x0, x1 = min(w["x0"] for w in ws), max(w["x1"] for w in ws)
    top = lines[li]["top"]
    for k in range(li - 1, max(-1, li - 4), -1):
        ln = lines[k]
        if top - ln["top"] > 45:
            break
        hit = [w for s, e, w in ln["spans"] if w["x1"] >= x0 - 12 and w["x0"] <= x1 + 12]
        lab = " ".join(w["text"] for w in hit).strip(" :.#-")
        if hit and re.search(r"[A-Za-z]", lab) and not re.search(r"\d", lab):
            return lab
    return ""


def learn_from(data, values):
    """Zoekt per veld waar de (verbeterde) waarde in de PDF staat. Geeft {veld: regel} terug."""
    pages = _pages(data)
    rules = {}
    for field in FIELDS:
        want = values.get(field)
        if want in (None, ""):
            continue
        best = None
        for key, pw, ph, lines in pages:
            for li, ln in enumerate(lines):
                for a, b, raw, v in _candidates(field, ln["text"]):
                    if not _same(field, v, want):
                        continue
                    ws = _words_at(ln, a, b)
                    if not ws:
                        continue
                    pos = {"page": key, "x": round(ws[0]["x0"] / pw, 4), "y": round(ln["top"] / ph, 4)}
                    lab = _label_left(ln["text"], a)
                    if lab:
                        # een kopje met een echt woord (3+ letters) gaat voor; 'N|NL' uit een streepjescoderegel niet
                        r = dict(pos, kind="rechts", label=lab, sample=raw, rank=0 if re.search(r"[A-Za-z]{3,}", lab) else 1.5)
                    else:
                        lab = _label_above(lines, li, ws)
                        r = dict(pos, kind="onder", label=lab, sample=raw, rank=1) if lab else dict(pos, kind="plek", label="", sample=raw, rank=2)
                    if best is None or r["rank"] < best["rank"]:
                        best = r
        if best:
            best.pop("rank")
            rules[field] = best
    return rules


def _find(field, rule, pages):
    """Past één geleerde regel toe. Geeft (waarde, ruwe tekst) of None."""
    lab = _norm(rule["label"])
    hits = []
    order = sorted(pages, key=lambda p: p[0] != rule["page"])   # eerst dezelfde pagina
    for key, pw, ph, lines in order:
        same_page = key == rule["page"]
        for li, ln in enumerate(lines):
            dist = abs(ln["top"] / ph - rule["y"]) + (0 if same_page else 1)
            if rule["kind"] == "rechts":
                n = _norm(ln["text"])
                if lab not in n:
                    continue
                # het deel van de regel ná het kopje (kopje zoeken in de tekst, tekens tussen de letters negeren)
                pat = r"[^a-z0-9]*".join(re.escape(ch) for ch in lab)
                m = None
                for m in re.finditer(pat, ln["text"], re.I):
                    pass
                if not m:
                    continue
                for a, b, raw, v in _candidates(field, ln["text"][m.end():]):
                    hits.append((dist, v, raw))
                    break
            elif rule["kind"] == "onder":
                pat = r"\s*".join(re.escape(w) for w in rule["label"].split())
                m = re.search(pat, ln["text"], re.I)
                if not m:
                    continue
                ws = _words_at(ln, m.start(), m.end())
                if not ws:
                    continue
                x0, x1 = min(w["x0"] for w in ws), max(w["x1"] for w in ws)
                for ln2 in lines[li + 1:li + 4]:
                    if ln2["top"] - ln["top"] > 45:
                        break
                    found = None
                    for a, b, raw, v in _candidates(field, ln2["text"]):
                        w2 = _words_at(ln2, a, b)
                        if w2 and w2[-1]["x1"] >= x0 - 12 and w2[0]["x0"] <= x1 + 12:
                            found = (dist, v, raw)
                            break
                    if found:
                        hits.append(found)
                        break
            else:
                if not same_page or abs(ln["top"] / ph - rule["y"]) > 0.015:
                    continue
                for a, b, raw, v in _candidates(field, ln["text"]):
                    w2 = _words_at(ln, a, b)
                    if w2 and abs(w2[0]["x0"] / pw - rule["x"]) < 0.03:
                        hits.append((dist, v, raw))
                        break
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    return hits[0][1], hits[0][2]


def rules_for(conn, supplier_id):
    if not supplier_id:
        return {}
    return {r["field"]: r for r in conn.execute("SELECT * FROM extract_rules WHERE supplier_id = ?", (supplier_id,))}


def apply(conn, supplier_id, data, x):
    """Geleerde regels van deze leverancier toepassen op het uitleesresultaat x (in plaats van het gewone uitlezen).
    Geeft de lijst velden die uit een geleerde regel komen."""
    rules = rules_for(conn, supplier_id)
    if not rules:
        return []
    try:
        pages = _pages(data)
    except Exception:  # noqa: BLE001
        return []
    used = []
    for field, r in rules.items():
        got = _find(field, dict(r), pages)
        if not got:
            continue
        v = got[0]
        if field.startswith("amount"):
            v = -abs(v) if x.get("credit") else abs(v)
        x[field] = v
        if field == "due_date":
            x.pop("due_assumed", None)
        used.append(field)
        conn.execute("UPDATE extract_rules SET used = used + 1, last_used_at = ? WHERE id = ?", (now_iso(), r["id"]))
    if used:
        x["learned"] = used
    return used


def save_rules(conn, supplier_id, invoice_id, rules, user_id=None):
    now = now_iso()
    for field, r in rules.items():
        conn.execute("DELETE FROM extract_rules WHERE supplier_id = ? AND field = ?", (supplier_id, field))
        conn.execute("INSERT INTO extract_rules (supplier_id, field, kind, label, page, x, y, sample, invoice_id, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (supplier_id, field, r["kind"], r["label"], r["page"], r["x"], r["y"], r["sample"], invoice_id, user_id, now))


def describe(r):
    if r["kind"] == "rechts":
        return f"achter “{r['label']}”"
    if r["kind"] == "onder":
        return f"onder “{r['label']}”"
    return "vaste plek op de " + ("eerste" if r["page"] == 0 else "laatste" if r["page"] == -1 else "") + " pagina"
