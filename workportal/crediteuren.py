"""Inkoopfacturen (crediteuren): één plek per factuur met PDF, status, vragen, opmerkingen en logboek.

Facturen komen binnen via de mailbox (Microsoft Graph, alleen lezen) of door uploaden.
Statussen: nieuw -> erp (verwerkt in Komdex) of buiten (verwerkt buiten Komdex om) -> snelstart (verwerkt in SnelStart); of afgewezen.
Elke status is direct te kiezen (ook terug naar nieuw).
Een open vraag (question_to) staat los van de status.
"""
import base64
import hashlib
import io
import os
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, current_app

from .db import query, execute, get_db
from .permissions import require, can, LEZEN, BEWERKEN, BEHEER
from .util import now_iso, now_utc, to_float, to_int, safe_filename, fmt_eur

bp = Blueprint("crediteuren", __name__, url_prefix="/inkoopfacturen")

MODULE = "crediteuren"
STATUS = {"nieuw": "Nieuw", "erp": "Verwerkt in Komdex", "buiten": "Verwerkt buiten Komdex om",
          "ss_eerst": "In SnelStart, nog Komdex", "snelstart": "Afgerond (Komdex en SnelStart)", "afgewezen": "Afgewezen"}
STATUS_BADGE = {"nieuw": "b-blue", "erp": "b-purple", "buiten": "b-purple", "ss_eerst": "b-purple", "snelstart": "b-ok",
                "afgewezen": "b-bad", "vraag": "b-warn"}
OPEN = ("nieuw", "erp", "buiten", "ss_eerst")   # nog niet afgerond
TO_SNELSTART = ("erp", "buiten")         # klaar voor SnelStart
DEFAULT_MAILBOX = "factuur@devreugd-pt.nl"
POLL_SECONDS = 300
_poll_lock = threading.Lock()


# ------------------------------------------------------------------ PDF uitlezen

_AMOUNT = r"(?:€|eur|euro)?\s*(-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|-?\d+(?:,\d{2})|-?\d{1,3}(?:,\d{3})*\.\d{2})"
_DATE = r"(\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:jan|feb|mrt|maa|apr|mei|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]*\.?\s+\d{4})"
_MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12}


def parse_amount(s):
    if s is None:
        return None
    s = str(s).strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    if re.search(r",\d{2}$", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{2}$", s):
        s = s.replace(",", "")
    else:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(s):
    if not s:
        return None
    s = s.strip().lower()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})$", s)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.match(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})$", s)
            if not m or m[2][:3] not in _MONTHS:
                return None
            d, mo, y = int(m[1]), _MONTHS[m[2][:3]], int(m[3])
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _find(patterns, text, group=1):
    for p in patterns:
        m = re.search(p, text, re.I | re.M)
        if m:
            return m.group(group).strip()
    return None


_INCL_LABEL = (r"(?:totaal|total|eindtotaal)\s*(?:€|eur)?\s*(?:bedrag\s*)?(?:incl\.?|inclusief|including|inkl\.?)"
               r"|(?<!sub)(?<!sub )totaal\s*(?:in\s*)?(?:eur|euro|€)(?!\w)(?!\s*(?:excl|exclusief|ex\.))"
               r"|te\s*betalen|te\s*voldoen|factuurbedrag|totaalbedrag|eindbedrag|eindtotaal|amount\s*due|amount\s*payable"
               r"|to\s*pay(?:\s*in\s*(?:eur|euro|€))?|total\s*due|grand\s*total|gesamtbetrag|rechnungsbetrag"
               r"|(?<![a-z])incl\.?\s*(?:btw|vat)|inclusief\s*btw")
_VAT_LABEL = r"(?:btw|b\.\s?t\.\s?w\.|vat|mwst|ust)\.?\s*(?:bedrag\s*)?(?:eur\s*)?(?:\d{1,2}(?:[,.]\d{1,2})?\s*%)?|\d{1,2}(?:[,.]\d)?\s*%\s*(?:btw|vat)"
_EXCL_LABEL = (r"(?:totaal|total)\s*(?:€|eur)?\s*(?:bedrag\s*)?(?:excl\.?|exclusief|excluding|exkl\.?|net)"
               r"|btw[\s-]*(?:basis|grondslag)|basis\s*btw|grondslag|taxable\s*amount|netto\s*bedrag|nettobetrag|totaal\s*netto"
               r"|(?<![a-z])excl\.?\s*(?:btw|vat)|exclusief\s*btw|^\s*netto(?:\s*(?:eur|€))?(?=\s)")
_SUBTOTAL_LABEL = r"subtotaal|sub\s*total"   # alleen als er niets beters is (vracht/toeslagen staan er vaak nog onder)
_PO_LABEL = (r"(?:uw|your|ihre?)\s*(?:referentie|ref(?:erence|erenz)?\.?|kenmerk|zeichen|order\s*(?:nummer|nr\.?|no\.?|number)?"
             r"|bestel(?:ling|nummer|nr\.?)?|bestellung|inkoop\s*(?:order)?\s*(?:nummer|nr\.?)?)"
             r"|inkoop\s*(?:order)?\s*(?:nummer|nr\.?)|purchase\s*order\s*(?:number|no\.?|nr\.?)?|p\.?o\.?\s*(?:number|nr\.?|no\.?|#)"
             r"|customer\s*(?:ref(?:erence)?|po)|bestellnummer|referentie|reference|kenmerk")
_AMOUNT_SL = _AMOUNT.replace(r"(?:€|eur|euro)?\s*", r"(?:€|eur|euro)?[ \t]*", 1)   # bedrag op dezelfde regel
_INVNO_VALUE = re.compile(r"^(?=[A-Z0-9\-/.]*\d)[A-Z0-9][A-Z0-9\-/.]{2,24}$", re.I)
_DATE_VALUE = re.compile(r"^" + _DATE + r"$", re.I)
_INVNO_LABEL = r"fact(?:uur)?\.?\s*(?:nr|nummer|no)\.?|invoice\s*(?:no|number|nr)\.?|rechnungs?\s*(?:nr|nummer)\.?"
_DATE_LABEL = r"fact(?:uur)?\.?\s*datum|invoice\s*date|rechnungsdatum"
_VATAMT_LABEL = r"btw-?bedrag|bedrag\s*btw|vat\s*amount|mwst[\s-]*betrag"
_AMT_TOKEN = re.compile(r"(?<![\d.,])(-?\d{1,3}(?:\.\d{3})+,\d{2}|-?\d+,\d{2}|-?\d{1,3}(?:,\d{3})+\.\d{2}|-?\d+\.\d{2})(?!\d)"
                        r"(?!\s*(?:%|kg\b|st\b|stuks|mtr|m\b|/))", re.I)


def _line_amount(text, label, stop=None):
    """Bedrag op dezelfde regel na het kopje: het laatste bedrag (gewicht, % en aantallen tellen niet mee).
    stop: tekst waar een ander totaal begint (dan daar afkappen)."""
    for m in re.finditer(r"(?:" + label + r")", text, re.I | re.M):
        end = text.find("\n", m.end())
        rest = text[m.end(): end if end >= 0 else len(text)]
        if stop:
            cut = re.search(stop, rest, re.I)
            if cut:
                rest = rest[:cut.start()]
        nums = [n for n in _AMT_TOKEN.findall(rest) if parse_amount(n)]   # 0,00 telt niet ("To pay: 0,00" na vooruitbetaling)
        if nums:
            return nums[-1]
    return None


_AMOUNT_ONLY = re.compile(r"^(?:€|eur)?\s*" + _AMOUNT + r"$", re.I)
_LEGAL = re.compile(r"([A-Z0-9][\w&'.\-]*(?:\s+[\w&'.\-]+){0,5}?\s+(?:B\.?V\.?|N\.?V\.?|GmbH|V\.?O\.?F\.?|BVBA|Ltd\.?|AG|S\.?A\.?))(?=\s|$|,)")


def _lines(words):
    """Woorden (pdfplumber) per regel, van boven naar onder."""
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(rows[-1][0] - w["top"]) <= 2.5:
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in rows]


def _below_amount(words, label, value_re=None):
    """Bedrag (of ander gegeven) dat ónder een kopje staat (totaalregels als tabel: 'Totaal excl. btw' met het bedrag
    op de regel eronder), of direct rechts ervan op dezelfde regel."""
    value_re = value_re or _AMOUNT_ONLY
    lines = _lines(words)
    pat = re.compile(label, re.I)
    for i, (top, ws) in enumerate(lines):
        text, spans = "", []
        for w in ws:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w["text"]), w))
            text += w["text"]
        for m in pat.finditer(text):
            hit = [w for a, b, w in spans if a < m.end() and b > m.start()]
            if not hit:
                continue
            x0, x1 = min(w["x0"] for w in hit), max(w["x1"] for w in hit)
            # bedrag rechts op dezelfde regel
            right = [w for w in ws if w["x0"] > x1 and value_re.match(w["text"])]
            if right and right[0]["x0"] - x1 < 200:
                return right[0]["text"]
            best = None
            for top2, ws2 in lines[i + 1:i + 4]:
                if top2 - top > 45:
                    break
                for w in ws2:
                    if not value_re.match(w["text"]):
                        continue
                    if w["x1"] < x0 - 12 or w["x0"] > x1 + 12:
                        continue   # niet onder het kopje
                    d = (top2 - top) + abs((w["x0"] + w["x1"]) / 2 - (x0 + x1) / 2) / 4
                    if best is None or d < best[0]:
                        best = (d, w["text"])
                if best:
                    return best[1]
    return None


def _po_ref(text):
    """Inkoopnummer van De Vreugd: begint altijd met 4 cijfers (4267, 4267-0, 4268-20260101)."""
    sep = r"[\s.:#\-]*"
    m = re.search(r"(?:" + _PO_LABEL + r")" + sep + r"(\d{4}(?:\s*-\s*\d{1,8})?)(?![\d,.])", text, re.I)
    if not m:
        m = re.search(r"(?<![\d.,/])(\d{4}\s*-\s*20\d{6})(?!\d)", text)   # 4267-20260131: inkoopnummer met ordernummer
    return re.sub(r"\s+", "", m.group(1)) if m else None


def _supplier(text):
    for line in text.splitlines()[:12]:
        m = _LEGAL.search(line)
        if m and not re.search(r"vreugd", m.group(1), re.I):
            words = m.group(1).split()
            for k in range(1, len(words) // 2 + 1):   # logo-tekst + naam: "LM Systems LM Systems BV" -> "LM Systems BV"
                if words[:k] == words[k:2 * k]:
                    words = words[k:]
                    break
            return " ".join(words)[:80]
    head = text.splitlines()[:15]
    # geen naam met B.V. e.d. in de kop (logo als plaatje): dan de website als aanwijzing, bijv. "bmn-bv.nl"
    for line in head:
        wm = re.search(r"(?:www\.|https?://)([\w-]+(?:\.[\w-]+)+)", line, re.I) or re.search(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", line)
        if wm and "vreugd" not in wm.group(1).lower():
            return wm.group(1).lower()[:80]
    addr = re.compile(r"\d{4}\s?[A-Z]{2}\b|@|www\.|^\s*[TFEM]\s|\btel\b|\bfax\b|iban|kvk|btw|^\W*\d|factuur|invoice|rechnung|vreugd|\|", re.I)
    first = next((l.strip() for l in head if len(re.sub(r"[^A-Za-z]", "", l)) >= 3 and not addr.search(l)), None)
    return (first or "")[:80] or None


def _close_vat(excl, vat):
    """Past het btw-bedrag bij een gangbaar tarief (21%, 9%, 0%) over dit excl.-bedrag?"""
    if not excl or excl <= 0:
        return False
    pct = vat / excl * 100
    return any(abs(pct - t) < 0.15 for t in (21, 9, 6, 19, 20, 7))


_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
                         "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u00ad": ""})


def _clean(v):
    return (v or "").translate(_DASHES)


def extract(data):
    """Leest factuurnummer, datums, bedragen en ons inkoopnummer uit een (tekst-)PDF. Geeft altijd een dict; ontbrekend = None."""
    out = {"invoice_no": None, "invoice_date": None, "due_date": None, "amount_incl": None, "amount_excl": None,
           "erp_ref": None, "supplier_guess": None, "text": ""}
    words = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            allp = pdf.pages[:40]
            # kop staat op de eerste pagina('s), de totalen op de laatste: bij lange facturen beide lezen
            idx = sorted({0, 1, len(allp) - 2, len(allp) - 1} & set(range(len(allp))))
            pages = [allp[i] for i in idx]
            text = "\n".join((p.extract_text() or "") for p in pages)
            # woorden met positie van de laatste pagina (daar staan de totalen) en de eerste (kopgegevens)
            for p in ([pages[-1]] + ([pages[0]] if len(pages) > 1 else [])):
                words.append(p.extract_words(keep_blank_chars=False, use_text_flow=False))
    except Exception:
        return out
    if not text.strip():
        return out
    # andere streepjes (‐ ‑ – — −) en harde spaties gelijk maken, anders worden 15‑10‑2026 en 4288‑20260000 niet herkend
    text = _clean(text)
    for ws in words:
        for w in ws:
            w["text"] = _clean(w["text"])
    sep = r"[\s.:#\-]*"
    num = r"((?=[A-Z0-9\-/.]*\d)[A-Z0-9][A-Z0-9\-/.]{2,24})(?![A-Za-z])"   # met minstens één cijfer (niet "Klantnummer")
    out["invoice_no"] = _find([
        r"(?:factuur|invoice|facture)\s*n[°º]\.?\s*((?:[A-Z]{1,5}\s)?(?=[A-Z0-9\-/.]*\d)[A-Z0-9][A-Z0-9\-/.]{2,24})(?![A-Za-z])",
        r"factuur\s*(?:nummer|nr\.?|no\.?)" + sep + num,
        r"invoice\s*(?:number|no\.?|nr\.?|#)" + sep + num,
        r"rechnungs?\s*(?:nummer|nr\.?)" + sep + num,
        r"^\s*factuur" + sep + r"([A-Z]{0,4}[\-]?\d[\dA-Z\-/.]{2,20})",
    ], text)
    combo = re.search(r"factuur(?:nummer|nr\.?)?\s*/\s*(?:factuur)?datum[\s:]*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{2,24})\s*/\s*" + _DATE, text, re.I)
    if combo:
        out["invoice_no"] = combo.group(1)
    if out["invoice_no"]:
        out["invoice_no"] = out["invoice_no"].rstrip(".-/")
    out["invoice_date"] = parse_date(combo.group(2)) if combo else None
    out["invoice_date"] = out["invoice_date"] or parse_date(_find([
        r"factuur\s*datum" + sep + _DATE, r"invoice\s*date" + sep + _DATE, r"rechnungsdatum" + sep + _DATE,
        r"document\s*datum" + sep + _DATE, r"^\s*datum" + sep + _DATE, r"\bdatum" + sep + _DATE, r"\bdate" + sep + _DATE,
        r"^[A-Za-z'’.\-\s]{2,40},\s*" + _DATE], text))   # "'s-Hertogenbosch, 02 oktober 2026"
    out["due_date"] = parse_date(_find([
        r"verval\s*(?:datum|dag)" + sep + _DATE, r"betal\w*\s*termijn[^\n]{0,20}?(?:tot|voor|vóór|uiterlijk)\s*" + _DATE, r"vervalt\s*op" + sep + _DATE, r"due\s*date" + sep + _DATE,
        r"betalen\s*(?:voor|vóór|uiterlijk)" + sep + _DATE, r"f[äa]llig(?:keit)?" + sep + _DATE], text))
    gap = r"[^\n\d€-]{0,25}"
    incl = _line_amount(text, _INCL_LABEL, r"\bexcl|exclusief|\bnetto")
    excl = _line_amount(text, _EXCL_LABEL, r"\bincl|inclusief|te\s*betalen|eindbedrag|(?<!sub)totaal\s*(?:in\s*)?(?:eur|€)")
    if not excl:   # "btw 21,00 % over 1.421,11  298,43": het bedrag na "over" is de grondslag (excl.)
        om = re.search(r"\b(?:btw|vat|b\.t\.w\.)\b[^\n]*?\b(?:over|van|on|auf)\s*(?:€|eur)?\s*" + _AMOUNT_SL, text, re.I)
        excl = om.group(1) if om else None
    for ws in words:   # totalen als tabel: bedrag onder het kopje
        incl = incl or _below_amount(ws, _INCL_LABEL)
        excl = excl or _below_amount(ws, _EXCL_LABEL)
    for ws in words:   # factuurnummer/-datum in een tabel onderaan (bijv. "Fact.nr." met het nummer eronder)
        if not out["invoice_no"]:
            v = _below_amount(ws, _INVNO_LABEL, _INVNO_VALUE)
            out["invoice_no"] = v.rstrip(".-/") if v else None
        if not out["invoice_date"]:
            out["invoice_date"] = parse_date(_below_amount(ws, _DATE_LABEL, _DATE_VALUE))
        if not out["due_date"]:   # "Vervaldatum" als kopje met de datum eronder
            out["due_date"] = parse_date(_below_amount(ws, r"verval\s*(?:datum|dag)|due\s*date|f[äa]llig(?:keit)?", _DATE_VALUE))
    if not out["invoice_date"]:
        # losse datum zonder kopje (bijv. rechts naast het debiteurnummer): de eerste datum die geen
        # verval-, lever- of orderdatum is
        for line in text.splitlines()[:40]:
            if re.search(r"verval|due|f[äa]llig|lever|order|bestel|pakbon|periode|geldig|\btot\b|\bvan\b|termijn", line, re.I):
                continue
            dm = re.search(r"(?<![\d/.-])" + _DATE + r"(?![\d/.-])", line, re.I)
            if dm and parse_date(dm.group(1)):
                out["invoice_date"] = parse_date(dm.group(1))
                break
    excl = excl or _find([r"(?:" + _SUBTOTAL_LABEL + r")" + gap + _AMOUNT_SL], text)
    for ws in words:
        excl = excl or _below_amount(ws, _SUBTOTAL_LABEL)
    if not incl:
        incl = _find([r"^\s*totaal(?!\s*(?:btw|vat|excl|netto))[^\n\d€-]{0,15}" + _AMOUNT_SL,
                      r"^\s*total(?!\s*(?:vat|excl|net))[^\n\d€-]{0,15}" + _AMOUNT_SL], text)
    if not incl:
        for ws in words:   # kolom "Totaal" naast "Excl. BTW" en "Bedrag BTW" (wordt hieronder met de btw gecontroleerd)
            incl = incl or _below_amount(ws, r"(?<![\w.])(?:totaal|total)(?![\w ]*(?:btw|vat|excl|netto))")
    out["amount_incl"] = parse_amount(incl)
    out["amount_excl"] = parse_amount(excl)
    if out["amount_incl"] is None:
        nums = [parse_amount(x) for x in re.findall(r"€\s*" + _AMOUNT, text)]
        nums = [n for n in nums if n]
        if nums:
            out["amount_incl"] = max(nums)
    if out["amount_incl"] is not None and out["amount_excl"] is not None and out["amount_excl"] > out["amount_incl"] > 0:
        out["amount_incl"], out["amount_excl"] = out["amount_excl"], out["amount_incl"]
    # btw-bedrag: om een ontbrekend bedrag uit te rekenen, of een fout gevonden incl.-bedrag te herkennen
    vat = None
    for line in text.splitlines():
        if not re.search(r"(?<![a-z])(?:btw|b\.\s?t\.\s?w\.|vat|mwst)(?![a-z])", line, re.I):
            continue
        if re.search(r"basis|grondslag|nummer|\bnr\b|identific|excl|incl|totaal|total|eori|kvk|iban", line, re.I):
            continue
        nums = _AMT_TOKEN.findall(line)
        v = parse_amount(nums[-1]) if nums else None
        if v and v > 0:
            vat = v
            break
    if vat is None:
        for ws in words:
            vat = vat or parse_amount(_below_amount(ws, _VATAMT_LABEL))
    ex, inc = out["amount_excl"], out["amount_incl"]
    if vat and ex and inc is None and _close_vat(ex, vat):
        out["amount_incl"] = round(ex + vat, 2)
    elif vat and inc and ex is None and _close_vat(inc - vat, vat):
        out["amount_excl"] = round(inc - vat, 2)
    elif vat and ex and inc and abs(ex + vat - inc) > 0.02:
        if _close_vat(inc - vat, vat):
            # excl. zonder toeslagen gelezen (bijv. "Nettobedrag" vóór milieu/tol): excl. = totaal - btw
            out["amount_excl"] = round(inc - vat, 2)
        elif _close_vat(ex, vat):
            out["amount_incl"] = round(ex + vat, 2)   # incl. verkeerd gelezen (bijv. een regelbedrag): excl. + btw klopt wel
    # vervaldatum uit de betalingstermijn ("betaling binnen 30 dagen", "30 dagen netto")
    if not out["due_date"] and out["invoice_date"]:
        dm = re.search(r"(?:binnen|within|innerhalb)\s*(\d{1,3})\s*(?:dagen|days|tagen)|(\d{1,3})\s*(?:dagen|days|tage)\s*(?:netto|net|na\s*factuurdatum)", text, re.I)
        if dm:
            days = int(dm.group(1) or dm.group(2))
            if 0 < days <= 120:
                out["due_date"] = (datetime.strptime(out["invoice_date"], "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    # geen vervaldatum of betalingstermijn gevonden: factuurdatum + 30 dagen
    if not out["due_date"] and out["invoice_date"]:
        out["due_date"] = (datetime.strptime(out["invoice_date"], "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
        out["due_assumed"] = True
    out["erp_ref"] = _po_ref(text)
    out["supplier_guess"] = _supplier(text)
    out["text"] = text
    return out


# ------------------------------------------------------------------ leverancier herkennen uit de relaties

_GENERIC_DOMAINS = {"gmail.com", "hotmail.com", "hotmail.nl", "outlook.com", "outlook.nl", "live.nl", "live.com", "icloud.com",
                    "me.com", "yahoo.com", "ziggo.nl", "kpnmail.nl", "planet.nl", "home.nl", "xs4all.nl", "upcmail.nl",
                    "onmicrosoft.com", "devreugd-pt.nl", "exactonline.nl", "snelstart.nl", "twinfield.com", "basecone.com"}
_OWN_POSTCODES = {"6669NA"}


def _domain(v):
    v = str(v or "").strip().lower()
    if not v:
        return None
    v = v.split("@")[-1]
    v = re.sub(r"^[a-z]+://", "", v).split("/")[0].split("?")[0]
    v = re.sub(r"^www\d?\.", "", v)
    parts = [p for p in v.split(".") if p]
    if len(parts) < 2:
        return None
    d = ".".join(parts[-3:]) if parts[-2] in ("co", "com") and len(parts) > 2 else ".".join(parts[-2:])
    return None if d in _GENERIC_DOMAINS else d


def _digits_phone(v):
    d = re.sub(r"\D", "", str(v or ""))
    if d.startswith("0031"):
        d = "0" + d[4:]
    elif d.startswith("31") and len(d) == 11:
        d = "0" + d[2:]
    return d if len(d) >= 9 else None


def _dom_core(d):
    """Kern van een domein: bmn-bv.nl -> 'bmn', fabory.com -> 'fabory', lm-systems.nl -> 'lmsystems'."""
    if not d:
        return None
    lab = d.split(".")[0].lower()
    lab = re.sub(r"[-_.]?(bv|b-v|nv|nl|groep|group|holding|benelux|nederland|online|shop|webshop)$", "", lab)
    core = re.sub(r"[^a-z0-9]", "", lab)
    return core if len(core) >= 3 else None


def _name_has_core(name, core):
    """Begint de naam (of de afkorting van de naam) met de kern van het domein?"""
    from .relaties import _norm
    if not core:
        return False
    full = _yk(_norm(name))
    words = re.findall(r"[a-z0-9]+", _yk((name or "").lower()))
    initials = "".join(w[0] for w in words if w not in ("de", "het", "van", "en", "bv", "b", "v", "nv"))
    if full.startswith(core) or (words and words[0] == core):
        return 2
    return 1 if initials.startswith(core) else 0   # afkorting telt minder zwaar


def _yk(v):
    """ij en y gelijk behandelen (Waaijenberg = Waayenberg)."""
    return (v or "").replace("ij", "y")


def match_supplier(conn, text, mail_from=None, hint=None):
    """Zoekt de leverancier in de relaties op meerdere kenmerken: naam op de factuur, e-maildomein van de afzender,
    website/e-mail op de factuur, postcode, telefoonnummer en eerdere facturen van hetzelfde adres.
    Geeft (id, naam, redenen) als het overtuigend is, anders None."""
    from .relaties import name_key as _nk, _norm as _nm
    name_key = lambda v: _yk(_nk(v))   # noqa: E731
    _norm = lambda v: _yk(_nm(v))      # noqa: E731
    text = text or ""
    tkey = name_key(text)
    tnorm = _norm(text)
    hkey = name_key(hint) if hint else ""   # naam van de afzender in de mail, bijv. "Digitale factuur van Waaijenberg Coatings BV"
    head_key = name_key("\n".join(text.splitlines()[:15]))   # kop van de factuur: daar staat de afzender
    tdom = {d for d in (_domain(x) for x in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|(?:https?://|www\.)[\w.-]+\.[a-z]{2,}"
                                                       r"|\b[\w-]+\.(?:nl|com|de|be|eu|net|org|co\.uk)\b", text, re.I)) if d}
    tpc = {re.sub(r"\s", "", m).upper() for m in re.findall(r"\b\d{4}\s?[A-Za-z]{2}\b", text)} - _OWN_POSTCODES
    tph = {d for d in (_digits_phone(m) for m in re.findall(r"(?:\+|00)?\d[\d\s\-().]{8,16}\d", text)) if d}
    mdom = _domain(mail_from)
    mcore = _yk(_dom_core(mdom) or "") or None
    tcores = {_yk(cc) for cc in (_dom_core(d) for d in tdom) if cc}
    prev = None
    if mail_from:
        prev = conn.execute("SELECT supplier_id FROM invoices WHERE mail_from = ? AND supplier_id IS NOT NULL"
                            " ORDER BY id DESC LIMIT 1", (mail_from.lower(),)).fetchone()
    best = []
    for c in conn.execute("SELECT id, name, short_name, email, website, postcode, phone, relation_type FROM customers"
                          " WHERE IFNULL(active, 1) = 1"):
        if re.search(r"vreugd", c["name"] or "", re.I):
            continue
        score, why = 0.0, []
        nk = name_key(c["name"])
        full = _norm(c["name"])
        if len(full) >= 8 and full in tnorm and full != nk:
            score += 4
            why.append("volledige naam op de factuur")   # bijv. "Fabory Nederland BV" letterlijk
        elif len(nk) >= 4 and nk in tkey:
            score += 3
            why.append("naam op de factuur" + (" (kop)" if nk in head_key else ""))
        elif c["short_name"] and len(name_key(c["short_name"])) >= 5 and name_key(c["short_name"]) in tkey:
            score += 2
            why.append("verkorte naam op de factuur")
        doms = {d for d in (_domain(c["email"]), _domain(c["website"])) if d}
        if mdom and mdom in doms:
            score += 3
            why.append("e-maildomein afzender")
        elif mcore and _name_has_core(c["name"], mcore):
            lvl = _name_has_core(c["name"], mcore)
            score += 3 if lvl == 2 else 2
            why.append("e-maildomein afzender past bij de naam" if lvl == 2 else "e-maildomein afzender past bij de afkorting")
        tl = max([_name_has_core(c["name"], tc) for tc in tcores] or [0])
        if not (doms & tdom) and tl:
            score += 1.5 if tl == 2 else 1
            long_core = tl == 2 and any(len(tc) >= 8 and _name_has_core(c["name"], tc) == 2 for tc in tcores)
            why.append("website op de factuur is de naam" if long_core else "website op de factuur past bij de naam")
        if hkey and len(nk) >= 5 and nk in hkey:
            score += 3
            why.append("naam afzender van de mail")
        if doms & tdom:
            score += 2
            why.append("website/e-mail op de factuur")
        pc = re.sub(r"\s", "", c["postcode"] or "").upper()
        if len(pc) == 6 and pc in tpc:
            score += 2
            why.append("postcode")
        ph = _digits_phone(c["phone"])
        if ph and ph in tph:
            score += 2
            why.append("telefoonnummer")
        if prev and prev["supplier_id"] == c["id"]:
            score += 4
            why.append("eerdere factuur van dit e-mailadres")
        if not score:
            continue
        if c["relation_type"] in ("leverancier", "beide"):
            score += 0.5
        if any(_name_has_core(c["name"], cc) == 2 for cc in ({mcore} | tcores) if cc):
            score += 0.7   # naam begint met het domein (bmn-bv.nl -> "BMN ..."): voorrang bij dubbele relaties
        strong = len([w for w in why if not w.startswith("verkorte")]) >= 2 or "e-maildomein afzender" in why \
            or "e-maildomein afzender past bij de naam" in why or "website op de factuur is de naam" in why \
            or ("naam afzender van de mail" in why and len(nk) >= 8) \
            or "eerdere factuur van dit e-mailadres" in why or "volledige naam op de factuur" in why \
            or (any(w.startswith("naam op de factuur") for w in why) and len(nk) >= 8)
        best.append((score, strong, c["id"], c["name"], why))
    if not best:
        return None
    if len(best) > 1:
        # bij gelijke stand (bijv. dubbele relatie uit Komdex): de relatie die al eerder facturen had iets voorrang
        counts = {r["supplier_id"]: r["n"] for r in conn.execute(
            f"SELECT supplier_id, COUNT(*) AS n FROM invoices WHERE supplier_id IN ({','.join('?' * len(best))}) GROUP BY supplier_id",
            [b[2] for b in best])}
        best = [(b[0] + (0.6 if counts.get(b[2]) else 0), b[1], b[2], b[3], b[4]) for b in best]
    best.sort(key=lambda b: -b[0])
    top = best[0]
    if not top[1]:
        return None
    if len(best) > 1 and best[1][0] >= top[0] - 0.5 and top[0] < 5:
        return None   # twijfel tussen twee relaties: niet gokken
    return top[2], top[3], top[4]


# ------------------------------------------------------------------ opslaan (ook buiten een request)

def _upload_dir(app=None):
    d = (app or current_app).config["UPLOAD_DIR"]
    os.makedirs(d, exist_ok=True)
    return d


def _store_file(conn, upload_dir, invoice_id, data, filename, user_id=None, kind="invoice", mime="application/pdf"):
    sub = now_utc().strftime("%Y%m")
    os.makedirs(os.path.join(upload_dir, sub), exist_ok=True)
    ext = (os.path.splitext(filename or "")[1].lower() or ".pdf")[:10]
    if not re.match(r"^\.[a-z0-9]{1,9}$", ext):
        ext = ".bin"
    rel = f"{sub}/{uuid.uuid4().hex}{ext}"
    with open(os.path.join(upload_dir, rel), "wb") as fh:
        fh.write(data)
    cur = conn.execute(
        "INSERT INTO files (entity, entity_id, kind, filename, stored_name, mime, size, caption, created_by, created_at)"
        " VALUES ('invoice',?,?,?,?,?,?,?,?,?)",
        (invoice_id, kind, filename, rel, mime or "application/octet-stream", len(data), None, user_id, now_iso()))
    return cur.lastrowid


def invoice_score(x, filename=""):
    """Hoe erg lijkt een PDF op een factuur (en niet op algemene voorwaarden, een pakbon of een brochure)?"""
    n = (filename or "").lower()
    sc = 0
    sc += 3 if x.get("invoice_no") else 0
    sc += 3 if x.get("amount_incl") is not None else 0
    sc += 1 if x.get("amount_excl") is not None else 0
    sc += 1 if x.get("invoice_date") else 0
    sc += 1 if x.get("erp_ref") else 0
    if re.search(r"factuur|invoice|rechnung|faktura|nota", n):
        sc += 3
    if re.search(r"voorwaarden|conditions|terms|agb|bedingungen|leveringscond|privacy|brochure|catalog|pakbon|packing|delivery\s*note|offerte|quotation", n):
        sc -= 6
    txt = (x.get("text") or "")[:3000].lower()
    if re.search(r"algemene\s+(verkoop|leverings)?\s*voorwaarden|general\s+terms|allgemeine\s+geschäftsbedingungen", txt) and not x.get("invoice_no"):
        sc -= 4
    if re.search(r"\b(factuur|invoice|rechnung)\b", txt):
        sc += 1
    return sc


def log(conn, invoice_id, text, user_id=None):
    conn.execute("INSERT INTO invoice_log (invoice_id, user_id, action, created_at) VALUES (?,?,?,?)",
                 (invoice_id, user_id, text, now_iso()))


def create_invoice(conn, upload_dir, data, filename, source="upload", user_id=None, mail=None, extra=None, x=None):
    """Maakt een factuurkaart van een PDF (bij mail: één kaart per mail, de overige bestanden als bijlage).
    extra = [(naam, bytes, mime), ...]. Geeft (id, nieuw?) terug; dubbele PDF's worden herkend."""
    sha = hashlib.sha256(data).hexdigest()
    dup = conn.execute("SELECT id FROM invoices WHERE sha256 = ?", (sha,)).fetchone()
    if dup:
        return dup["id"], False
    x = x or extract(data)
    mail = mail or {}
    supplier = None
    if mail.get("from_addr"):
        prev = conn.execute("SELECT supplier FROM invoices WHERE mail_from = ? AND IFNULL(supplier,'') <> ''"
                            " ORDER BY id DESC LIMIT 1", (mail["from_addr"],)).fetchone()
        supplier = prev["supplier"] if prev else (mail.get("from_name") or None)
    if _bad_supplier(supplier):
        supplier = None
    supplier = supplier or x["supplier_guess"]
    m = match_supplier(conn, x["text"], mail.get("from_addr"), hint=mail.get("from_name"))
    supplier_id = supplier_match = None
    if m:
        supplier_id, supplier, why = m
        supplier_match = ", ".join(why)
    if x["invoice_no"] and supplier:
        same = conn.execute("SELECT id FROM invoices WHERE invoice_no = ? AND supplier = ?", (x["invoice_no"], supplier)).fetchone()
        if same:
            return same["id"], False
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO invoices (status, supplier, invoice_no, invoice_date, due_date, amount_incl, amount_excl, erp_ref, source,"
        " mail_id, mail_from, mail_subject, received_at, sha256, created_by, created_at, updated_at)"
        " VALUES ('nieuw',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (supplier, x["invoice_no"], x["invoice_date"], x["due_date"], x["amount_incl"], x["amount_excl"], x["erp_ref"], source,
         mail.get("key"), mail.get("from_addr"), mail.get("subject"), mail.get("received") or now, sha, user_id, now, now))
    iid = cur.lastrowid
    if supplier_id:
        conn.execute("UPDATE invoices SET supplier_id = ?, supplier_match = ? WHERE id = ?", (supplier_id, supplier_match, iid))
    fid = _store_file(conn, upload_dir, iid, data, safe_filename(filename) or "factuur.pdf", user_id)
    conn.execute("UPDATE invoices SET file_id = ? WHERE id = ?", (fid, iid))
    for ename, edata, emime in (extra or []):
        _store_file(conn, upload_dir, iid, edata, safe_filename(ename) or "bijlage", user_id, kind="attachment", mime=emime)
    if source == "mail":
        log(conn, iid, f"Binnengekomen per mail van {mail.get('from_name') or mail.get('from_addr') or 'onbekend'}"
                       f" ({mail.get('subject') or 'geen onderwerp'})"
                       + (f"; {len(extra)} andere bijlage(n) bij deze kaart: " + ", ".join(e[0] for e in extra) if extra else ""))
    else:
        log(conn, iid, "Geüpload", user_id)
    labels = {"invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum",
              "amount_excl": "bedrag excl.", "amount_incl": "bedrag incl.", "erp_ref": "inkoopnummer"}
    found = [k for k in labels if x.get(k)]
    if found:
        log(conn, iid, "Gegevens automatisch uitgelezen: " + ", ".join(
            labels[k] + (" (niet op de factuur: factuurdatum + 30 dagen)" if k == "due_date" and x.get("due_assumed") else "") for k in found))
    if supplier_id:
        log(conn, iid, f"Leverancier herkend uit de relaties: {supplier} (op {supplier_match})")
    conn.commit()
    return iid, True


def merge_mail_cards(conn, user_id=None):
    """Kaarten die uit dezelfde mail komen (van vóór 'één kaart per mail') samenvoegen: de kaart die het meest op een
    factuur lijkt blijft, de andere bestanden (bijv. algemene voorwaarden) worden bijlagen. Alleen kaarten die nog
    'nieuw' zijn en waar niemand iets bij heeft gezet."""
    rows = conn.execute("SELECT i.*, f.filename AS fname FROM invoices i LEFT JOIN files f ON f.id = i.file_id"
                        " WHERE i.source = 'mail' AND i.mail_id LIKE '%:%'").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["mail_id"].split(":")[0], []).append(r)
    merged = 0
    for mid, cards in groups.items():
        if len(cards) < 2:
            continue
        cards.sort(key=lambda r: (-invoice_score(dict(r), r["fname"] or ""), r["id"]))
        best = cards[0]
        for r in cards[1:]:
            busy = r["status"] != "nieuw" or r["question_to"] or conn.execute(
                "SELECT 1 FROM invoice_notes WHERE invoice_id = ? LIMIT 1", (r["id"],)).fetchone()
            if busy:
                continue
            conn.execute("UPDATE files SET entity_id = ?, kind = 'attachment' WHERE entity = 'invoice' AND entity_id = ?",
                         (best["id"], r["id"]))
            conn.execute("DELETE FROM invoices WHERE id = ?", (r["id"],))
            log(conn, best["id"], f"Samengevoegd: '{r['fname'] or 'bestand'}' uit dezelfde mail is nu een bijlage van deze kaart", user_id)
            merged += 1
        conn.execute("UPDATE invoices SET mail_id = ? WHERE id = ?", (mid, best["id"]))
    if merged:
        conn.commit()
    return merged


def _bad_supplier(v):
    v = (v or "").strip()
    return len(re.sub(r"[^A-Za-z]", "", v)) < 3 or bool(re.match(r"^(invoice|factuur|rechnung|faktura|creditnota|credit\s*note)\b", v, re.I))


def reread(conn, upload_dir, iid, overwrite=False, user_id=None):
    """Factuur opnieuw uitlezen (na verbeteringen in het uitlezen). overwrite=False: alleen lege velden aanvullen.
    De leverancier wordt gekoppeld aan de relaties als die herkend wordt en er nog geen koppeling is (of bij overwrite)."""
    r = conn.execute("SELECT i.*, f.stored_name FROM invoices i LEFT JOIN files f ON f.id = i.file_id WHERE i.id = ?", (iid,)).fetchone()
    if not r or not r["stored_name"]:
        return []
    from .util import file_path
    try:
        with open(file_path(r), "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    x = extract(data)
    # wat iemand zelf heeft aangepast blijft staan; de rest (automatisch uitgelezen) mag vervangen worden
    labels = {"invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum",
              "amount_incl": "bedrag incl.", "amount_excl": "bedrag excl.", "erp_ref": ("inkoopnummer", "inkooporder ERP")}
    manual = " ".join(l["action"] for l in conn.execute(
        "SELECT action FROM invoice_log WHERE invoice_id = ? AND action LIKE 'Gegevens aangepast:%'", (iid,)))
    upd = {}
    for k in ("invoice_no", "invoice_date", "due_date", "amount_incl", "amount_excl", "erp_ref"):
        lab = labels[k] if isinstance(labels[k], tuple) else (labels[k],)
        by_hand = any(l in manual for l in lab)
        if x.get(k) is not None and (overwrite or r[k] in (None, "") or not by_hand) and x[k] != r[k]:
            upd[k] = x[k]
    if overwrite or not r["supplier_id"]:
        m = match_supplier(conn, x["text"], r["mail_from"], hint=r["supplier"])
        if m and m[0] != r["supplier_id"]:
            upd.update(supplier_id=m[0], supplier=m[1], supplier_match=", ".join(m[2]))
        elif not m and not r["supplier_id"] and x["supplier_guess"] and _bad_supplier(r["supplier"]):
            upd["supplier"] = x["supplier_guess"]   # bijv. "Invoice ." vervangen door de naam uit de kop
    if upd:
        conn.execute("UPDATE invoices SET " + ", ".join(f"{k} = ?" for k in upd) + ", updated_at = ? WHERE id = ?",
                     list(upd.values()) + [now_iso(), iid])
        names = {"invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum", "amount_incl": "bedrag incl.",
                 "amount_excl": "bedrag excl.", "erp_ref": "inkoopnummer", "supplier": "leverancier"}
        log(conn, iid, "Opnieuw uitgelezen: " + ", ".join(names[k] for k in upd if k in names)
            + (f" (leverancier uit de relaties, op {upd['supplier_match']})" if "supplier_match" in upd else ""), user_id)
        conn.commit()
    return [k for k in upd if k != "supplier_match"]


# ------------------------------------------------------------------ mailbox (Microsoft Graph, alleen lezen)

def _setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r and r["value"] not in (None, "") else default


def _set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def mail_configured():
    return all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"))


def poll_mailbox(conn, upload_dir):
    """Haalt nieuwe PDF-bijlagen op uit de factuurmailbox. Wijzigt niets in de mailbox. Geeft (nieuw, melding)."""
    if not _poll_lock.acquire(blocking=False):
        return 0, "Ophalen loopt al."
    try:
        return _poll(conn, upload_dir)
    finally:
        _poll_lock.release()


def _poll(conn, upload_dir):
    import requests
    try:
        merge_mail_cards(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"[WorkPortal] Samenvoegen kaarten mislukt: {exc}", flush=True)
    from .mail import _graph_token
    box = _setting(conn, "cred_mailbox", DEFAULT_MAILBOX)
    since = _setting(conn, "cred_since")
    if not since:
        since = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _set(conn, "cred_since", since)
    headers = {"Authorization": f"Bearer {_graph_token()}"}
    refreshed = False
    url = (f"https://graph.microsoft.com/v1.0/users/{box}/mailFolders/inbox/messages"
           f"?$filter=receivedDateTime ge {since} and hasAttachments eq true"
           f"&$select=id,subject,from,receivedDateTime&$orderby=receivedDateTime asc&$top=50")
    new = 0
    pages = 0
    while url and pages < 10:
        pages += 1
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code in (401, 403) and not refreshed:
            # net een recht toegevoegd in Entra? Het bewaarde token kent dat nog niet: één keer een nieuw token halen
            refreshed = True
            headers = {"Authorization": f"Bearer {_graph_token(force=True)}"}
            pages -= 1
            continue
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            raise RuntimeError(f"Mailbox {box} lezen mislukt ({r.status_code}): {msg}" + _mail_hint(r.status_code, box))
        js = r.json()
        for m in js.get("value", []):
            mid = m["id"]
            if conn.execute("SELECT 1 FROM invoice_mail_seen WHERE message_id = ?", (mid,)).fetchone():
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            ar = requests.get(f"https://graph.microsoft.com/v1.0/users/{box}/messages/{mid}/attachments",
                              headers=headers, timeout=60)
            if ar.status_code >= 400:
                raise RuntimeError(f"Bijlagen lezen mislukt ({ar.status_code})")
            # één kaart per mail: de PDF die het meest op een factuur lijkt is de factuur, de rest zijn bijlagen
            files = []
            for a in ar.json().get("value", []):
                if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                    continue
                data = base64.b64decode(a.get("contentBytes") or "")
                if not data:
                    continue
                name = a.get("name") or "bijlage"
                files.append((name, data, (a.get("contentType") or "application/octet-stream")))
            pdfs = [f for f in files if f[1].startswith(b"%PDF")]
            if pdfs:
                scored = []
                for f in pdfs:
                    fx = extract(f[1])
                    scored.append((invoice_score(fx, f[0]), f, fx))
                scored.sort(key=lambda t: -t[0])
                main, mx = scored[0][1], scored[0][2]
                extra = [f for f in files if f is not main]
                received = (m.get("receivedDateTime") or "").replace("Z", "+00:00")
                _, created = create_invoice(conn, upload_dir, main[1], main[0], source="mail", x=mx, extra=extra, mail={
                    "key": mid, "from_addr": (frm.get("address") or "").lower(),
                    "from_name": frm.get("name"), "subject": m.get("subject"), "received": received or None})
                new += 1 if created else 0
            conn.execute("INSERT OR IGNORE INTO invoice_mail_seen (message_id, received_at) VALUES (?,?)",
                         (mid, m.get("receivedDateTime")))
            conn.commit()
        url = js.get("@odata.nextLink")
    return new, None


def _mail_hint(status, box):
    """Uitleg bij een geweigerde mailbox: ontbreekt het recht, of houdt Exchange de mailbox tegen?"""
    if status not in (401, 403):
        return ""
    from .mail import graph_roles
    roles = graph_roles()
    have = ", ".join(roles) or "geen"
    if not ({"Mail.Read", "Mail.ReadWrite", "Mail.ReadBasic.All"} & set(roles)):
        return (f" — Oorzaak: de app-registratie heeft het toepassingsrecht Mail.Read nog niet (of zonder beheerderstoestemming)."
                f" Nu aanwezig: {have}. Voeg in Entra bij de app-registratie onder API-machtigingen > Microsoft Graph >"
                f" Toepassingsmachtigingen 'Mail.Read' toe en klik op 'Beheerderstoestemming verlenen'.")
    return (f" — Mail.Read is aanwezig, maar Exchange weigert de mailbox {box}. Waarschijnlijk staat er een ApplicationAccessPolicy"
            f" waarin {box} niet is opgenomen: voeg de mailbox toe aan de toegestane groep (zie README, Inkoopfacturen)."
            f" Wijzigingen in Exchange kunnen tot een uur duren.")


def scheduled_poll(conn, app, state):
    """Vanuit de planner: elke 5 minuten de mailbox, als dat aanstaat."""
    if _setting(conn, "cred_mail_on", "0") != "1" or not mail_configured():
        return
    t = now_utc().timestamp()
    if t - state.get("cred_poll", 0) < POLL_SECONDS:
        return
    state["cred_poll"] = t
    try:
        n, _ = poll_mailbox(conn, _upload_dir(app))
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", "")
        if n:
            print(f"[WorkPortal] Inkoopfacturen: {n} nieuwe factuur/facturen uit de mailbox", flush=True)
    except Exception as exc:
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", str(exc)[:400])
        traceback.print_exc()


# ------------------------------------------------------------------ helpers voor de schermen

def _status_key(r):
    if r["status"] in OPEN and r["question_to"]:
        return "vraag"
    return r["status"]


def _label(r):
    if _status_key(r) == "vraag":
        return "Vraag open bij " + (r["question_name"] or "collega")
    if r["status"] == "afgewezen":
        return "Afgewezen door " + (r["rejected_name"] or "onbekend")
    if r["status"] == "snelstart":
        return "Afgerond · " + ("buiten Komdex om" if r["route"] == "buiten" else "Komdex") + " + SnelStart"
    return STATUS.get(r["status"], r["status"])


def _users():
    return query("SELECT u.id, u.name FROM users u WHERE u.active = 1 ORDER BY u.name")


def _load(iid):
    r = query("SELECT i.*, q.name AS question_name, qb.name AS question_by_name, rb.name AS rejected_name FROM invoices i"
              " LEFT JOIN users q ON q.id = i.question_to LEFT JOIN users qb ON qb.id = i.question_by"
              " LEFT JOIN users rb ON rb.id = i.rejected_by"
              " WHERE i.id = ?", (iid,), one=True)
    if not r:
        abort(404)
    return r


def _notify(user_id, title, body, iid):
    try:
        from .notify import notify_user
        notify_user(get_db(), user_id, title, body, url=url_for("crediteuren.detail", iid=iid))
    except Exception:
        traceback.print_exc()


def open_counts():
    """Voor menu en dashboard."""
    if not getattr(g, "user", None) or not can(MODULE):
        return {}
    r = query("SELECT SUM(status='nieuw' AND question_to IS NULL) AS nieuw,"
              " SUM(status IN ('nieuw','erp','buiten','ss_eerst') AND question_to IS NOT NULL) AS vraag,"
              " SUM(status IN ('erp','buiten') AND question_to IS NULL) AS erp,"
              " SUM(status IN ('nieuw','erp','buiten','ss_eerst') AND question_to = ?) AS mij FROM invoices", (g.user["id"],), one=True)
    return {k: (r[k] or 0) for k in ("nieuw", "vraag", "erp", "mij")}


# ------------------------------------------------------------------ routes

FILTERS = [
    ("open", "Alles open"),
    ("nieuw", "Nieuw"),
    ("erp", "Nog naar SnelStart"),
    ("komdex", "Nog in Komdex"),
    ("vraag", "Vraag open"),
    ("mij", "Wacht op mij"),
    ("boekhouding", "Voor de boekhouding"),
    ("afgewezen", "Afgewezen"),
    ("klaar", "Afgerond"),
    ("alle", "Alle"),
]


@bp.route("/")
@require(MODULE)
def index():
    flt = request.args.get("filter") or "open"
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    uid = g.user["id"]
    cond = {
        "open": "i.status IN ('nieuw','erp','buiten','ss_eerst')",
        "nieuw": "i.status = 'nieuw' AND i.question_to IS NULL",
        "erp": "i.status IN ('erp','buiten') AND i.question_to IS NULL",
        "komdex": "i.status = 'ss_eerst' AND i.question_to IS NULL",
        "vraag": "i.status IN ('nieuw','erp','buiten','ss_eerst') AND i.question_to IS NOT NULL",
        "mij": "i.status IN ('nieuw','erp','buiten','ss_eerst') AND i.question_to = ?",
        "boekhouding": "i.status <> 'snelstart' AND EXISTS (SELECT 1 FROM invoice_notes n WHERE n.invoice_id = i.id AND n.for_accounting = 1)",
        "afgewezen": "i.status = 'afgewezen'",
        "klaar": "i.status = 'snelstart'",
        "alle": "1=1",
    }
    if flt not in cond:
        flt = "open"
    where.append(cond[flt])
    if flt == "mij":
        params.append(uid)
    if q:
        where.append("(i.supplier LIKE ? OR i.invoice_no LIKE ? OR i.erp_ref LIKE ? OR i.description LIKE ? OR i.mail_subject LIKE ?)")
        params += [f"%{q}%"] * 5
    rows = query(
        "SELECT i.*, q.name AS question_name, (SELECT name FROM users WHERE id = i.rejected_by) AS rejected_name,"
        " (SELECT COUNT(*) FROM invoice_notes n WHERE n.invoice_id = i.id AND n.for_accounting = 1) AS acc_notes,"
        " (SELECT l.action || ' · ' || IFNULL(u.name, 'systeem') FROM invoice_log l LEFT JOIN users u ON u.id = l.user_id"
        "   WHERE l.invoice_id = i.id ORDER BY l.id DESC LIMIT 1) AS last_action"
        " FROM invoices i LEFT JOIN users q ON q.id = i.question_to WHERE " + " AND ".join(where) +
        " ORDER BY CASE WHEN i.status IN ('nieuw','erp','buiten','ss_eerst') THEN 0 ELSE 1 END, i.received_at DESC, i.id DESC LIMIT 500", params)
    c = query("SELECT SUM(status IN ('nieuw','erp','buiten','ss_eerst')) AS open, SUM(status='nieuw' AND question_to IS NULL) AS nieuw,"
              " SUM(status IN ('erp','buiten') AND question_to IS NULL) AS erp, SUM(status IN ('nieuw','erp','buiten','ss_eerst') AND question_to IS NOT NULL) AS vraag,"
              " SUM(status IN ('nieuw','erp','buiten','ss_eerst') AND question_to = ?) AS mij, SUM(status='afgewezen') AS afgewezen,"
              " SUM(status='snelstart') AS klaar, COUNT(*) AS alle,"
              " SUM(status IN ('nieuw','erp','buiten','ss_eerst') AND due_date IS NOT NULL AND due_date <= date('now','+7 day')) AS verval,"
              " SUM(status = 'afgewezen' AND rejected_at >= date('now','start of month')) AS af_maand"
              " FROM invoices", (uid,), one=True)
    counts = {k: (c[k] or 0) for k in c.keys()}
    counts["boekhouding"] = query("SELECT COUNT(DISTINCT n.invoice_id) AS n FROM invoice_notes n JOIN invoices i ON i.id = n.invoice_id"
                                  " WHERE n.for_accounting = 1 AND i.status <> 'snelstart'", one=True)["n"] or 0
    items = []
    for r in rows:
        d = dict(r)
        d["skey"] = _status_key(r)
        d["label"] = _label(r)
        items.append(d)
    conn = get_db()
    mail = {"on": _setting(conn, "cred_mail_on", "0") == "1", "box": _setting(conn, "cred_mailbox", DEFAULT_MAILBOX),
            "last": _setting(conn, "cred_last_poll"), "error": _setting(conn, "cred_last_error"), "configured": mail_configured()}
    return render_template("crediteuren/index.html", items=items, flt=flt, q=q, counts=counts, FILTERS=FILTERS,
                           STATUS_BADGE=STATUS_BADGE, mail=mail, soon=(now_utc() + timedelta(days=7)).strftime("%Y-%m-%d"))


@bp.route("/uploaden", methods=["POST"])
@require(MODULE, BEWERKEN)
def upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Kies een of meer PDF-bestanden.", "error")
        return redirect(url_for("crediteuren.index"))
    conn = get_db()
    made, dup, skipped, last = 0, 0, [], None
    for f in files:
        data = f.read()
        if not data.startswith(b"%PDF"):
            skipped.append(f.filename)
            continue
        iid, created = create_invoice(conn, _upload_dir(), data, f.filename, source="upload", user_id=g.user["id"])
        last = iid
        made += created
        dup += not created
    if skipped:
        flash("Geen PDF, overgeslagen: " + ", ".join(skipped), "error")
    if dup:
        flash(f"{dup} factuur/facturen stond(en) er al in.", "info")
    if made == 1 and len(files) == 1:
        return redirect(url_for("crediteuren.detail", iid=last))
    if made:
        flash(f"{made} factuur/facturen toegevoegd.", "success")
    return redirect(url_for("crediteuren.index"))


@bp.route("/ophalen", methods=["POST"])
@require(MODULE, BEWERKEN)
def fetch():
    conn = get_db()
    if not mail_configured():
        flash("De Microsoft-koppeling is niet ingesteld (GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET).", "error")
        return redirect(url_for("crediteuren.index"))
    try:
        n, msg = poll_mailbox(conn, _upload_dir())
        _set(conn, "cred_last_poll", now_iso())
        _set(conn, "cred_last_error", "")
        flash(msg or (f"{n} nieuwe factuur/facturen opgehaald." if n else "Geen nieuwe facturen in de mailbox."),
              "success" if n else "info")
    except Exception as exc:
        _set(conn, "cred_last_error", str(exc)[:400])
        flash(f"Ophalen mislukt: {exc}", "error")
    return redirect(url_for("crediteuren.index"))


@bp.route("/<int:iid>")
@require(MODULE)
def detail(iid):
    r = _load(iid)
    conn = get_db()
    if r["status"] == "nieuw" and not r["seen_at"] and r["created_by"] != g.user["id"]:
        conn.execute("UPDATE invoices SET seen_at = ?, seen_by = ? WHERE id = ?", (now_iso(), g.user["id"], iid))
        log(conn, iid, "Bekeken", g.user["id"])
        conn.commit()
        r = _load(iid)
    notes = query("SELECT n.*, u.name AS who, t.name AS to_name FROM invoice_notes n LEFT JOIN users u ON u.id = n.created_by"
                  " LEFT JOIN users t ON t.id = n.to_user WHERE n.invoice_id = ? ORDER BY n.id", (iid,))
    logs = query("SELECT l.*, u.name AS who FROM invoice_log l LEFT JOIN users u ON u.id = l.user_id"
                 " WHERE l.invoice_id = ? ORDER BY l.id DESC", (iid,))
    d = dict(r)
    d["skey"] = _status_key(r)
    d["label"] = _label(r)
    back = request.args.get("terug") or url_for("crediteuren.index")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("crediteuren.index")
    attachments = query("SELECT * FROM files WHERE entity = 'invoice' AND entity_id = ? AND kind = 'attachment' ORDER BY id", (iid,))
    suppliers = query("SELECT id, name, city, email, relation_type FROM customers WHERE IFNULL(active, 1) = 1 OR id = ?"
                      " ORDER BY CASE WHEN relation_type IN ('leverancier', 'beide') THEN 0 WHEN relation_type IS NULL THEN 1 ELSE 2 END,"
                      " name", (r["supplier_id"] or 0,))
    return render_template("crediteuren/detail.html", r=d, notes=notes, logs=logs, users=_users(), STATUS_BADGE=STATUS_BADGE,
                           back=back, suppliers=suppliers, attachments=attachments)


@bp.route("/<int:iid>/gegevens", methods=["POST"])
@require(MODULE, BEWERKEN)
def save(iid):
    r = _load(iid)
    f = request.form
    sup_id = to_int(f.get("supplier_id")) if "supplier_id" in f else r["supplier_id"]
    rel = query("SELECT id, name FROM customers WHERE id = ?", (sup_id,), one=True) if sup_id else None
    vals = {
        "supplier": rel["name"] if rel else ((f.get("supplier") or "").strip()[:150] or (None if "supplier_id" in f and r["supplier_id"] else r["supplier"])),
        "invoice_no": (f.get("invoice_no") or "").strip()[:60] or None,
        "invoice_date": f.get("invoice_date") or None,
        "due_date": f.get("due_date") or None,
        "amount_incl": parse_amount(f.get("amount_incl")),
        "amount_excl": parse_amount(f.get("amount_excl")),
        "erp_ref": (f.get("erp_ref") or "").strip()[:60] or None,
    }
    changed = [k for k, v in vals.items() if (r[k] if not isinstance(v, float) else (r[k] if r[k] is None else round(r[k], 2))) != v]
    if (rel["id"] if rel else None) != r["supplier_id"]:
        execute("UPDATE invoices SET supplier_id = ?, supplier_match = ? WHERE id = ?",
                (rel["id"] if rel else None, "handmatig gekozen" if rel else None, iid))
        if "supplier" not in changed:
            changed.append("supplier")
    if changed:
        execute("UPDATE invoices SET " + ", ".join(f"{k} = ?" for k in vals) + ", updated_at = ? WHERE id = ?",
                list(vals.values()) + [now_iso(), iid])
        names = {"supplier": "leverancier", "invoice_no": "factuurnummer", "invoice_date": "factuurdatum", "due_date": "vervaldatum",
                 "amount_incl": "bedrag incl.", "amount_excl": "bedrag excl.", "erp_ref": "inkoopnummer", "description": "omschrijving"}
        conn = get_db()
        log(conn, iid, "Gegevens aangepast: " + ", ".join(names[k] for k in changed), g.user["id"])
        conn.commit()
        flash("Gegevens opgeslagen.", "success")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/opnieuw-uitlezen", methods=["POST"])
def reread_one(iid):
    _load(iid)
    if not can("crediteuren", 2):
        abort(403)
    done = reread(get_db(), _upload_dir(), iid, overwrite=True, user_id=g.user["id"])
    flash("Opnieuw uitgelezen; bijgewerkt: " + ", ".join(done) if done else "Opnieuw uitgelezen; er is niets veranderd.", "ok")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/opnieuw-uitlezen", methods=["POST"])
def reread_all():
    if not can("crediteuren", 3):
        abort(403)
    conn = get_db()
    merged = merge_mail_cards(conn, g.user["id"])
    n = 0
    for r in conn.execute("SELECT id FROM invoices WHERE status NOT IN ('snelstart', 'afgewezen') ORDER BY id").fetchall():
        if reread(conn, _upload_dir(), r["id"], overwrite=False, user_id=g.user["id"]):
            n += 1
    flash((f"{merged} losse kaart(en) uit dezelfde mail samengevoegd (nu bijlage). " if merged else "")
          + f"Openstaande facturen opnieuw uitgelezen: {n} bijgewerkt. Wat iemand zelf had aangepast is blijven staan.", "ok")
    return redirect(url_for("crediteuren.index"))


@bp.route("/<int:iid>/status", methods=["POST"])
@require(MODULE, BEWERKEN)
def set_status(iid):
    """Twee losse stappen, in willekeurige volgorde:
    - Komdex: 'Verwerkt in Komdex' óf 'Verwerkt buiten Komdex om' (nooit allebei; nog eens klikken = uit);
    - SnelStart: aan/uit.
    De status volgt daaruit: allebei gedaan = afgerond. 'Nieuw' zet alles terug."""
    r = _load(iid)
    if not can("crediteuren", 2):
        abort(403)
    act = request.form.get("status") or {"terug": "nieuw"}.get(request.form.get("action"), request.form.get("action"))
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    route = r["route"] if r["status"] != "afgewezen" else None
    ss = bool(r["snelstart_at"]) and r["status"] != "afgewezen"
    msg = None
    if act in ("erp", "buiten"):
        want = "komdex" if act == "erp" else "buiten"
        if route == want:
            route, msg = None, f"{STATUS[act]} uitgezet"
        else:
            route, msg = want, STATUS[act]
            conn.execute("UPDATE invoices SET erp_at=?, erp_by=? WHERE id=?", (now, uid, iid))
    elif act == "snelstart":
        ss = not ss
        msg = "Verwerkt in SnelStart" + ("" if ss else " uitgezet")
        if ss:
            conn.execute("UPDATE invoices SET snelstart_at=?, snelstart_by=? WHERE id=?", (now, uid, iid))
    elif act == "nieuw":
        route, ss, msg = None, False, "Terug naar Nieuw"
    else:
        return redirect(url_for("crediteuren.detail", iid=iid))
    new = ("snelstart" if ss else ("erp" if route == "komdex" else "buiten")) if route else ("ss_eerst" if ss else "nieuw")
    conn.execute("UPDATE invoices SET status=?, route=?, snelstart_at=CASE WHEN ? THEN snelstart_at ELSE NULL END, updated_at=? WHERE id=?",
                 (new, route, 1 if ss else 0, now, iid))
    log(conn, iid, f"Status: {msg}" + (f" → {STATUS[new]}" if STATUS[new] != msg else ""), uid)
    conn.commit()
    flash(f"Status: {STATUS[new]}.", "ok")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/afwijzen", methods=["POST"])
@require(MODULE, BEWERKEN)
def reject(iid):
    r = _load(iid)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Geef een reden voor het afwijzen.", "error")
        return redirect(request.form.get("terug") or url_for("crediteuren.detail", iid=iid))
    if r["status"] == "snelstart":
        abort(400)
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    conn.execute("UPDATE invoices SET status='afgewezen', rejected_at=?, rejected_by=?, reject_reason=?, question_to=NULL,"
                 " updated_at=? WHERE id=?", (now, uid, reason[:500], now, iid))
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, created_by, created_at) VALUES (?,?,?,?,?)",
                 (iid, "afwijzing", reason[:2000], uid, now))
    log(conn, iid, "Afgewezen: " + reason[:120], uid)
    conn.commit()
    flash("Factuur afgewezen.", "success")
    back = request.form.get("terug") or ""
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/vraag", methods=["POST"])
@require(MODULE, BEWERKEN)
def ask(iid):
    r = _load(iid)
    to = request.form.get("to_user", type=int)
    body = (request.form.get("body") or "").strip()
    target = query("SELECT id, name FROM users WHERE id = ? AND active = 1", (to,), one=True) if to else None
    if not target or not body:
        flash("Kies een collega en schrijf je vraag.", "error")
        return redirect(url_for("crediteuren.detail", iid=iid))
    conn = get_db()
    now, uid = now_iso(), g.user["id"]
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, to_user, created_by, created_at) VALUES (?,?,?,?,?,?)",
                 (iid, "vraag", body[:4000], target["id"], uid, now))
    conn.execute("UPDATE invoices SET question_to=?, question_by=?, question_at=?, updated_at=? WHERE id=?",
                 (target["id"], uid, now, now, iid))
    log(conn, iid, f"Vraag gesteld aan {target['name']}", uid)
    conn.commit()
    if target["id"] != uid:
        _notify(target["id"], f"Vraag over factuur {r['supplier'] or ''} {r['invoice_no'] or ''}".strip(),
                f"{g.user['name']} vraagt: {body[:400]}", iid)
    flash(f"Vraag gesteld aan {target['name']}.", "success")
    return redirect(url_for("crediteuren.detail", iid=iid))


@bp.route("/<int:iid>/reactie", methods=["POST"])
@require(MODULE)
def comment(iid):
    r = _load(iid)
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("crediteuren.detail", iid=iid))
    acc = 1 if request.form.get("for_accounting") else 0
    uid = g.user["id"]
    answers = bool(r["question_to"]) and (r["question_to"] == uid or request.form.get("close_question"))
    if not can(MODULE, BEWERKEN) and r["question_to"] != uid:
        abort(403)
    conn = get_db()
    now = now_iso()
    kind = "antwoord" if answers else ("boekhouding" if acc else "opmerking")
    conn.execute("INSERT INTO invoice_notes (invoice_id, kind, body, for_accounting, created_by, created_at) VALUES (?,?,?,?,?,?)",
                 (iid, kind, body[:4000], acc, uid, now))
    if answers:
        asker = r["question_by"]
        conn.execute("UPDATE invoices SET question_to=NULL, updated_at=? WHERE id=?", (now, iid))
        log(conn, iid, "Vraag beantwoord", uid)
        conn.commit()
        if asker and asker != uid:
            _notify(asker, f"Antwoord op je vraag over factuur {r['supplier'] or ''} {r['invoice_no'] or ''}".strip(),
                    f"{g.user['name']}: {body[:400]}", iid)
    else:
        log(conn, iid, "Opmerking voor de boekhouding" if acc else "Opmerking geplaatst", uid)
        conn.commit()
    return redirect(url_for("crediteuren.detail", iid=iid) + "#gesprek")


@bp.route("/<int:iid>/opmerking/<int:nid>/afgehandeld", methods=["POST"])
@require(MODULE, BEWERKEN)
def note_done(iid, nid):
    n = query("SELECT * FROM invoice_notes WHERE id = ? AND invoice_id = ?", (nid, iid), one=True) or abort(404)
    execute("UPDATE invoice_notes SET handled = ? WHERE id = ?", (0 if n["handled"] else 1, nid))
    return redirect(url_for("crediteuren.detail", iid=iid) + "#gesprek")


@bp.route("/<int:iid>/verwijderen", methods=["POST"])
@require(MODULE, BEHEER)
def delete(iid):
    r = _load(iid)
    from .util import delete_file
    for fr in query("SELECT id FROM files WHERE entity = 'invoice' AND entity_id = ?", (iid,)):
        delete_file(fr["id"])
    execute("DELETE FROM invoices WHERE id = ?", (iid,))
    flash(f"Factuur {r['supplier'] or ''} {r['invoice_no'] or ''} verwijderd.".replace("  ", " "), "success")
    return redirect(url_for("crediteuren.index"))
