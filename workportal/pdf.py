"""PDF-rapporten (ReportLab) in de huisstijl van De Vreugd Productietechniek."""
import io
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
                                Image, KeepTogether)
from PIL import Image as PILImage

from .util import fmt_dt, fmt_date, fmt_num, fmt_eur

BASIC = colors.HexColor("#0A0A96")
ACCENT = colors.HexColor("#0080FF")
INK = colors.HexColor("#14163A")
MUTED = colors.HexColor("#5A5F80")
LINE = colors.HexColor("#E1E5F0")
LIGHT = colors.HexColor("#F3F5FA")
OK = colors.HexColor("#1A6B43")
BAD = colors.HexColor("#9B1C12")

STATIC = os.path.join(os.path.dirname(__file__), "static", "img")

S = {
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=BASIC, spaceAfter=2),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=BASIC, spaceBefore=10, spaceAfter=5),
    "eyebrow": ParagraphStyle("eb", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=ACCENT),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK, alignment=TA_LEFT),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.8, leading=11.5, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.8, leading=11.5, textColor=INK),
    "label": ParagraphStyle("label", fontName="Helvetica", fontSize=8.3, leading=11, textColor=MUTED),
    "big": ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK),
}


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>"))


def _doc(buf, title, doc_no):
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=30 * mm,
                          bottomMargin=18 * mm, title=title, author="De Vreugd Productietechniek")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")

    def deco(canvas, d):
        canvas.saveState()
        w, h = A4
        logo = os.path.join(STATIC, "logo.png")
        if os.path.exists(logo):
            canvas.drawImage(logo, 18 * mm, h - 22 * mm, width=58 * mm, height=58 * mm * 207 / 900, mask="auto")
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.setFillColor(ACCENT)
        canvas.drawRightString(w - 18 * mm, h - 14 * mm, "WORKPORTAL")
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(w - 18 * mm, h - 19 * mm, f"{title} · {doc_no}")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.8)
        canvas.line(18 * mm, h - 25 * mm, w - 18 * mm, h - 25 * mm)
        canvas.line(18 * mm, 13 * mm, w - 18 * mm, 13 * mm)
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(BASIC)
        canvas.drawString(18 * mm, 9 * mm, "DE VREUGD PRODUCTIETECHNIEK · ALS PERFORMANCE TELT")
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(w - 18 * mm, 9 * mm, f"Pagina {d.page}")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=deco)])
    return doc


def _kv_table(pairs, width, cols=2):
    """Label/waarde-blokken in kolommen."""
    cells, row = [], []
    for label, value in pairs:
        row.append([Paragraph(esc(label), S["label"]), Paragraph(esc(value) or "–", S["cellb"])])
        if len(row) == cols:
            cells.append(row)
            row = []
    if row:
        while len(row) < cols:
            row.append(["", ""])
        cells.append(row)
    data = [[item for pair in r for item in pair] for r in cells]
    cw = width / cols
    t = Table(data, colWidths=[cw * 0.38, cw * 0.62] * cols)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
    ]))
    return t


def _grid(data, widths, header=True, align_right=()):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("FONTSIZE", (0, 0), (-1, 0), 8)]
    for c in align_right:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _img(path, max_w, max_h):
    try:
        with PILImage.open(path) as im:
            w, h = im.size
    except Exception:
        return None
    ratio = min(max_w / w, max_h / h)
    src = path
    if path.lower().endswith((".jpg", ".jpeg")) and max(w, h) > 1100:
        # Foto's verkleinen, zodat de PDF klein genoeg blijft om te mailen
        with PILImage.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((1100, 1100))
            src = io.BytesIO()
            im.save(src, "JPEG", quality=78, optimize=True)
            src.seek(0)
    return Image(src, width=w * ratio, height=h * ratio)


def _photo_grid(paths_captions, width, per_row=3):
    if not paths_captions:
        return None
    cell_w = width / per_row
    rows, row = [], []
    for path, cap in paths_captions:
        img = _img(path, cell_w - 6, 58 * mm)
        if img is None:
            continue
        row.append([img, Paragraph(esc(cap or ""), S["small"])])
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        while len(row) < per_row:
            row.append("")
        rows.append(row)
    if not rows:
        return None
    t = Table(rows, colWidths=[cell_w] * per_row)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


# ---------------------------------------------------------------- druktest

def pressure_test_pdf(t, project, readings, reading_photos, general_photos, signature_path, users):
    """t: dict druktest; readings: lijst dicts; reading_photos: {reading_id: [(pad, bijschrift)]}"""
    buf = io.BytesIO()
    doc = _doc(buf, "Druktestrapport", t["number"])
    W = doc.width
    story = [Paragraph("DRUKTESTRAPPORT DUBBELWANDIG LEIDINGWERK", S["eyebrow"]),
             Paragraph(f"Druktest {esc(t['number'])}", S["h1"])]
    result = (t.get("result") or "").lower()
    rcol = OK if result == "goedgekeurd" else BAD
    res_tbl = Table([[Paragraph("Resultaat", S["label"]),
                      Paragraph(f"<font color='#{rcol.hexval()[2:]}'>{esc((t.get('result') or 'onbekend').upper())}</font>", S["big"])]],
                    colWidths=[30 * mm, W - 30 * mm])
    res_tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [Spacer(1, 4), res_tbl, Spacer(1, 6)]

    proj = f"{project['number']} – {project['name']}" if project else "–"
    customer = project.get("customer") if project else None
    pairs = [
        ("Project", proj), ("Klant", customer or "–"),
        ("Tekeningnummer", f"{t['drawing_no']}" + (f" rev {t['drawing_rev']}" if t.get("drawing_rev") else "")),
        ("Leiding-/lijnnummer", t.get("line_no") or "–"),
        ("Getest deel", t.get("part")), ("Medium", t.get("medium")),
        ("Testdruk", f"{fmt_num(t['test_pressure'], 2)} bar"),
        ("Toegestaan drukverlies", f"{fmt_num(t['max_drop'], 2)} bar" if t.get("max_drop") is not None else "–"),
        ("Manometer", t.get("gauge_id") or "–"), ("Kalibratiedatum", fmt_date(t.get("gauge_cal_date")) or "–"),
        ("Uitvoerder(s)", t.get("executor") or "–"), ("Getuige klant", t.get("witness") or "–"),
        ("Gestart", fmt_dt(t["created_at"])), ("Afgerond", fmt_dt(t.get("finished_at")) or "–"),
    ]
    story += [Paragraph("Testgegevens", S["h2"]), _kv_table(pairs, W)]

    story.append(Paragraph("Tijdlijn metingen", S["h2"]))
    start_p = readings[0]["pressure"] if readings else None
    data = [["Moment", "Datum / tijd", "Druk (bar)", "Verlies t.o.v. start", "Temp. (°C)", "Opmerking"]]
    for r in readings:
        drop = (start_p - r["pressure"]) if start_p is not None else None
        data.append([{"start": "Start", "controle": "Controle", "eind": "Eindmeting"}.get(r["kind"], r["kind"]),
                     fmt_dt(r["created_at"]), fmt_num(r["pressure"], 2),
                     fmt_num(drop, 2) if drop is not None else "–",
                     fmt_num(r["temperature"], 1) if r.get("temperature") is not None else "–",
                     Paragraph(esc(r.get("note") or ""), S["cell"])])
    story.append(_grid(data, [22 * mm, 30 * mm, 20 * mm, 30 * mm, 18 * mm, W - 120 * mm], align_right=(2, 3, 4)))
    if readings and len(readings) > 1:
        total_drop = start_p - readings[-1]["pressure"]
        ok = t.get("max_drop") is None or total_drop <= (t.get("max_drop") or 0) + 1e-9
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"Totaal drukverlies: <b>{fmt_num(total_drop, 2)} bar</b>"
                               + (f" (toegestaan {fmt_num(t['max_drop'], 2)} bar: {'binnen norm' if ok else 'BUITEN NORM'})"
                                  if t.get("max_drop") is not None else ""), S["body"]))

    if t.get("remarks"):
        story += [Paragraph("Opmerkingen", S["h2"]), Paragraph(esc(t["remarks"]), S["body"])]

    for r in readings:
        pics = reading_photos.get(r["id"]) or []
        if pics:
            label = {"start": "Start", "controle": "Controle", "eind": "Eindmeting"}.get(r["kind"], r["kind"])
            grid = _photo_grid([(p, f"{label} · {fmt_dt(r['created_at'])} · {fmt_num(r['pressure'], 2)} bar") for p, _ in pics], W)
            if grid:
                story += [KeepTogether([Paragraph(f"Foto's {label.lower()} ({fmt_dt(r['created_at'])})", S["h2"]), grid])]
    if general_photos:
        grid = _photo_grid(general_photos, W)
        if grid:
            story += [KeepTogether([Paragraph("Overige foto's", S["h2"]), grid])]

    sig = [Paragraph("Ondertekening", S["h2"]),
           Paragraph(f"Uitgevoerd en afgerond door: <b>{esc(t.get('signed_name') or users.get(t.get('finished_by')) or '')}</b>"
                     f" op {fmt_dt(t.get('finished_at'))}", S["body"])]
    if signature_path:
        img = _img(signature_path, 70 * mm, 28 * mm)
        if img:
            sig.append(img)
    story.append(KeepTogether(sig))
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------- werkbon

def visit_pdf(ticket, visit, photos, signature_path):
    buf = io.BytesIO()
    doc = _doc(buf, "Werkbon", ticket["number"])
    W = doc.width
    story = [Paragraph("WERKBON SERVICE & ONDERHOUD", S["eyebrow"]),
             Paragraph(esc(ticket["title"]), S["h1"])]
    pairs = [
        ("Ticket", ticket["number"]), ("Type", (ticket.get("type") or "").capitalize()),
        ("Klant", ticket.get("customer") or "–"), ("Locatie", ticket.get("location") or "–"),
        ("Installatie", ticket.get("installation") or "–"), ("Serienummer", ticket.get("serial") or "–"),
        ("Datum bezoek", fmt_date(visit["date"])), ("Monteur(s)", visit.get("technicians") or "–"),
        ("Gewerkte uren", fmt_num(visit.get("hours"), 2)), ("Reistijd (uur)", fmt_num(visit.get("travel_hours"), 2)),
        ("Kilometers", fmt_num(visit.get("km"), 0)), ("Project", ticket.get("project_no") or "–"),
    ]
    story += [Spacer(1, 4), _kv_table(pairs, W)]
    if ticket.get("description"):
        story += [Paragraph("Melding", S["h2"]), Paragraph(esc(ticket["description"]), S["body"])]
    story += [Paragraph("Bevindingen", S["h2"]), Paragraph(esc(visit.get("findings") or "–"), S["body"]),
              Paragraph("Uitgevoerde werkzaamheden", S["h2"]), Paragraph(esc(visit.get("work_done") or "–"), S["body"]),
              Paragraph("Gebruikte materialen", S["h2"]), Paragraph(esc(visit.get("materials") or "–"), S["body"])]
    grid = _photo_grid(photos, W)
    if grid:
        story += [KeepTogether([Paragraph("Foto's", S["h2"]), grid])]
    sig = [Paragraph("Voor akkoord klant", S["h2"]),
           Paragraph(f"Naam: <b>{esc(visit.get('signed_name') or '')}</b> · {fmt_dt(visit.get('signed_at'))}", S["body"])]
    if signature_path:
        img = _img(signature_path, 70 * mm, 28 * mm)
        if img:
            sig.append(img)
    story.append(KeepTogether(sig))
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------- na-calculatie

def nacalc_pdf(n, summary, items, calc):
    buf = io.BytesIO()
    doc = _doc(buf, "Na-calculatie", n.get("order_no") or "")
    W = doc.width
    story = [Paragraph("NA-CALCULATIE", S["eyebrow"]),
             Paragraph(f"Order {esc(n.get('order_no'))} · {esc(n.get('order_desc'))}", S["h1"]),
             Paragraph(f"ERP-export van {fmt_dt(n['imported_at'])}"
                       + (f" · vergeleken met calculatie {esc(calc['number'])}" if calc else " · zonder calculatie")
                       + (f" · gedeeld door {fmt_num(n['divide_by'], 0)}" if (n.get('divide_by') or 1) != 1 else ""), S["small"]),
             Spacer(1, 8)]
    pairs = [("Orderbedrag", fmt_eur(summary["order_total"], 2)), ("Werkelijke kostprijs", fmt_eur(summary["cost"], 2)),
             ("Verkoopwaarde (incl. marges)", fmt_eur(summary["sale"], 2)), ("Resultaat", fmt_eur(summary["result"], 2)),
             ("Marge op orderbedrag", f"{fmt_num(summary['margin_pct'], 1)}%"), ("Beoordeling", summary["label"])]
    if calc:
        pairs.append(("Gecalculeerd (incl. marge)", fmt_eur(summary.get("calc_total"), 2)))
    story.append(_kv_table(pairs, W))
    story.append(Paragraph("Per item", S["h2"]))
    data = [["Pos", "Item", "Uren calc.", "Uren werk.", "Gecalculeerd", "Kostprijs", "Verkoopwaarde", "Verschil"]]
    for it in items:
        data.append([str(it["pos"] or ""), Paragraph(esc(it["desc"]), S["cell"]),
                     fmt_num(it.get("calc_hours"), 1) if it.get("calc_hours") is not None else "–",
                     fmt_num(it["hours"], 1),
                     fmt_eur(it["calc"], 0) if it.get("calc") is not None else "–",
                     fmt_eur(it["cost"], 0), fmt_eur(it["sale"], 0),
                     fmt_eur(it["diff"], 0) if it.get("diff") is not None else "–"])
    data.append(["", Paragraph("<b>Totaal</b>", S["cell"]), fmt_num(summary.get("calc_hours"), 1) if calc else "–",
                 fmt_num(summary["hours"], 1), fmt_eur(summary.get("calc_total"), 0) if calc else "–",
                 fmt_eur(summary["cost"], 0), fmt_eur(summary["sale"], 0),
                 fmt_eur(summary.get("diff"), 0) if calc else "–"])
    t = _grid(data, [12 * mm, W - 136 * mm, 17 * mm, 17 * mm, 22 * mm, 22 * mm, 24 * mm, 22 * mm], align_right=(2, 3, 4, 5, 6, 7))
    t.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("BACKGROUND", (0, -1), (-1, -1), LIGHT)]))
    story.append(t)
    story.append(Spacer(1, 6))
    story.append(Paragraph("Verschil = gecalculeerd − werkelijke verkoopwaarde. Negatief betekent meer besteed dan gecalculeerd.", S["small"]))
    doc.build(story)
    return buf.getvalue()
