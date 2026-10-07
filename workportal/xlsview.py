"""Excel bekijken in WorkPortal (alleen lezen): .xlsx/.xlsm/.xltx en .csv als tabel met tabbladen.

Het bestand wordt op de server met openpyxl gelezen (waarden zoals Excel ze het laatst berekend heeft),
met kolombreedtes, samengevoegde cellen, vet/cursief, tekst- en vulkleur en getalnotatie.
"""
import csv
import datetime as dt
import io
import re
from html import escape

MAX_ROWS = 2000
MAX_COLS = 80
EXCEL_EXT = (".xlsx", ".xlsm", ".xltx", ".xltm")
VIEW_EXT = EXCEL_EXT + (".csv",)


def is_viewable(name):
    return (name or "").lower().endswith(VIEW_EXT)


def _nl_num(v, decimals=None, thousands=False):
    if decimals is None:
        s = f"{v:.10g}"
        if "e" in s:
            return s.replace(".", ",")
        if "." in s:
            ip, fp = s.split(".")
        else:
            ip, fp = s, ""
        if thousands:
            ip = f"{int(ip):,}".replace(",", ".") if ip.lstrip("-").isdigit() else ip
        return ip + ("," + fp if fp else "")
    s = f"{v:,.{decimals}f}" if thousands else f"{v:.{decimals}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_value(v, number_format="General"):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "WAAR" if v else "ONWAAR"
    if isinstance(v, dt.datetime):
        if v.hour or v.minute or v.second:
            return v.strftime("%d-%m-%Y %H:%M")
        return v.strftime("%d-%m-%Y")
    if isinstance(v, dt.date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, (int, float)):
        f = (number_format or "General").split(";")[0]
        if f == "General" or f == "@":
            return _nl_num(v)
        clean = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", f)
        dec = len(re.match(r"[0#]*", clean.split(".", 1)[1]).group(0)) if "." in clean else 0
        thousands = "," in clean.split(".")[0]
        if "%" in clean:
            return _nl_num(v * 100, dec, thousands) + "%"
        out = _nl_num(v, dec, thousands)
        if "€" in f or "EUR" in f.upper():
            return "€ " + out
        return out
    return str(v)


def _color(c):
    """openpyxl-kleur naar #rrggbb (alleen echte RGB-kleuren; themakleuren laten we weg)."""
    try:
        if c is not None and c.type == "rgb" and isinstance(c.rgb, str) and len(c.rgb) in (6, 8):
            rgb = c.rgb[-6:]
            if c.rgb[:2] == "00" and len(c.rgb) == 8 and rgb == "000000":
                return None
            return "#" + rgb
        if c is not None and c.type == "indexed" and c.indexed not in (64, 65):
            from openpyxl.styles.colors import COLOR_INDEX
            if 0 <= c.indexed < len(COLOR_INDEX):
                return "#" + COLOR_INDEX[c.indexed][-6:]
    except Exception:
        pass
    return None


EMU_PX = 9525


def _pictures(ws, max_px=1400, budget=None):
    """Afbeeldingen op een tabblad (logo's, productfoto's) met hun ankerpunt. Grote foto's worden verkleind."""
    import base64
    from PIL import Image as PILImage
    out = []
    for img in getattr(ws, "_images", []) or []:
        try:
            raw = img._data()
            anchor = img.anchor
            fr = anchor._from
            item = {"r1": fr.row + 1, "c1": fr.col + 1, "dx": int(fr.colOff / EMU_PX), "dy": int(fr.rowOff / EMU_PX),
                    "w": None, "h": None, "r2": fr.row + 1, "c2": fr.col + 1, "dx2": 0, "dy2": 0}
            ext = getattr(anchor, "ext", None)
            to = getattr(anchor, "to", None)
            if to is not None:
                item.update(r2=to.row + 1, c2=to.col + 1, dx2=int(to.colOff / EMU_PX), dy2=int(to.rowOff / EMU_PX))
            elif ext is not None and getattr(ext, "width", None):
                item.update(w=int(ext.width / EMU_PX), h=int(ext.height / EMU_PX))
            else:
                item.update(w=int(img.width or 0), h=int(img.height or 0))
            if item["w"] is not None:  # ruwe schatting van de cel waar hij eindigt (voor het zichtbare bereik)
                item["c2"] = item["c1"] + max(0, int((item["dx"] + item["w"]) / 70))
                item["r2"] = item["r1"] + max(0, int((item["dy"] + item["h"]) / 22))
            mime = "image/png"
            try:
                with PILImage.open(io.BytesIO(raw)) as im:
                    fmt = (im.format or "PNG").upper()
                    if max(im.size) > max_px or fmt not in ("PNG", "JPEG", "GIF"):
                        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                        im.thumbnail((max_px, max_px))
                        b = io.BytesIO()
                        if im.mode == "RGBA":
                            im.save(b, "PNG", optimize=True)
                        else:
                            im.save(b, "JPEG", quality=82)
                            mime = "image/jpeg"
                        raw = b.getvalue()
                    else:
                        mime = "image/jpeg" if fmt == "JPEG" else "image/gif" if fmt == "GIF" else "image/png"
            except Exception:
                continue  # EMF/WMF e.d. kan een browser niet tonen
            item["src"] = f"data:{mime};base64," + base64.b64encode(raw).decode()
            out.append(item)
        except Exception:
            continue
    return out


def _sheet_html(ws):
    from openpyxl.utils import get_column_letter
    max_r, max_c = ws.max_row or 0, ws.max_column or 0
    # werkelijk gebruikt bereik (lege opgemaakte rijen/kolommen aan het eind weglaten)
    last_r = last_c = 0
    for row in ws.iter_rows(min_row=1, max_row=min(max_r, MAX_ROWS * 3), max_col=min(max_c, MAX_COLS * 2)):
        for cell in row:
            if cell.value is not None and cell.value != "":
                last_r = max(last_r, cell.row)
                last_c = max(last_c, cell.column)
    for mr in ws.merged_cells.ranges:
        if mr.min_row <= last_r and mr.min_col <= last_c:
            last_r, last_c = max(last_r, mr.max_row), max(last_c, mr.max_col)
    pics = _pictures(ws)
    for pic in pics:
        last_r, last_c = max(last_r, pic["r2"]), max(last_c, pic["c2"])
    truncated = last_r > MAX_ROWS or last_c > MAX_COLS
    last_r, last_c = min(last_r, MAX_ROWS), min(last_c, MAX_COLS)
    if not last_r:
        return '<div class="xv-empty">Dit tabblad is leeg.</div>', False

    span, skip = {}, set()
    for mr in ws.merged_cells.ranges:
        if mr.min_row > last_r or mr.min_col > last_c:
            continue
        span[(mr.min_row, mr.min_col)] = (min(mr.max_row, last_r) - mr.min_row + 1, min(mr.max_col, last_c) - mr.min_col + 1)
        for r in range(mr.min_row, min(mr.max_row, last_r) + 1):
            for c in range(mr.min_col, min(mr.max_col, last_c) + 1):
                if (r, c) != (mr.min_row, mr.min_col):
                    skip.add((r, c))
    cols = []
    for c in range(1, last_c + 1):
        letter = get_column_letter(c)
        d = ws.column_dimensions.get(letter)
        if d is not None and d.hidden:
            continue
        w = d.width if d is not None and d.width else 9.5
        cols.append((c, letter, max(28, min(int(w * 7.2 + 6), 600))))
    visible = {c for c, _, _ in cols}
    colw = {c: w for c, _, w in cols}

    def row_px(r):
        rd = ws.row_dimensions.get(r)
        if rd is not None and rd.hidden:
            return 0
        return max(22, int(rd.height * 4 / 3)) if rd is not None and rd.height else 22

    host = {}  # (r, c) -> html van afbeeldingen die in deze cel beginnen
    for pic in pics:
        r, c, dx, dy = pic["r1"], pic["c1"], pic["dx"], pic["dy"]
        if r > last_r or c > last_c:
            continue
        while c not in visible and c <= last_c:  # verborgen kolom: naar de volgende
            c += 1; dx = 0
        while (r, c) in skip:  # in een samengevoegde cel: naar de cel linksboven
            for (mr, mc), (rs, cs) in span.items():
                if mr <= r < mr + rs and mc <= c < mc + cs:
                    dx += sum(colw.get(x, 0) for x in range(mc, c)); dy += sum(row_px(y) for y in range(mr, r))
                    r, c = mr, mc
                    break
            else:
                break
        w, h = pic["w"], pic["h"]
        if w is None:  # tot-cel bekend: breedte/hoogte uit de kolommen en rijen
            w = sum(colw.get(x, 0) for x in range(pic["c1"], pic["c2"])) + pic["dx2"] - pic["dx"]
            h = sum(row_px(y) for y in range(pic["r1"], pic["r2"])) + pic["dy2"] - pic["dy"]
        if w < 4 or h < 4:
            continue
        host.setdefault((r, c), []).append(
            f'<img class="xv-pic" src="{pic["src"]}" alt="" style="left:{dx}px;top:{dy}px;width:{w}px;height:{h}px">')
    out = ['<table class="xv-t"><colgroup><col style="width:44px">']
    out += [f'<col style="width:{w}px">' for _, _, w in cols]
    out.append('</colgroup><thead><tr><th class="xv-corner"></th>')
    out += [f"<th>{letter}</th>" for _, letter, _ in cols]
    out.append("</tr></thead><tbody>")
    for r in range(1, last_r + 1):
        rd = ws.row_dimensions.get(r)
        if rd is not None and rd.hidden:
            continue
        h = f' style="height:{row_px(r)}px"'
        out.append(f'<tr{h}><th>{r}</th>')
        for c, _, _ in cols:
            if (r, c) in skip:
                continue
            cell = ws.cell(row=r, column=c)
            attrs, style = "", []
            if (r, c) in span:
                rs, cs = span[(r, c)]
                cs = sum(1 for x in range(c, c + cs) if x in visible)
                attrs += (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
            v = cell.value
            text = fmt_value(v, cell.number_format)
            font = cell.font
            if font is not None:
                if font.b:
                    style.append("font-weight:700")
                if font.i:
                    style.append("font-style:italic")
                if font.u:
                    style.append("text-decoration:underline")
                col = _color(font.color)
                if col and col.upper() != "#000000":
                    style.append(f"color:{col}")
                if font.sz and abs(float(font.sz) - 11) > 1.5:
                    style.append(f"font-size:{max(9, min(float(font.sz), 28)) * 1.2:.0f}px")
            fill = cell.fill
            if fill is not None and fill.fill_type == "solid":
                col = _color(fill.fgColor)
                if col and col.upper() not in ("#FFFFFF",):
                    style.append(f"background:{col}")
            al = cell.alignment
            ha = al.horizontal if al is not None else None
            if ha in ("center", "centerContinuous"):
                style.append("text-align:center")
            elif ha == "right" or (ha is None and isinstance(v, (int, float, dt.date)) and not isinstance(v, bool)):
                style.append("text-align:right")
            if al is not None and al.wrap_text:
                style.append("white-space:pre-wrap")
            pics_here = host.get((r, c))
            if pics_here:
                attrs += ' class="xv-host"'
            st = f' style="{";".join(style)}"' if style else ""
            out.append(f"<td{attrs}{st}>{''.join(pics_here or [])}{escape(text)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out), truncated


def render(data, name):
    """Geeft een lijst tabbladen: {name, html, truncated}. Gooit ValueError bij een onleesbaar bestand."""
    low = (name or "").lower()
    if low.endswith(".csv"):
        return [_csv(data)]
    if low.endswith(".xls"):
        raise ValueError("Dit is het oude Excel-formaat (.xls). Dat kan de viewer niet openen; download het bestand.")
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ValueError("Dit Excel-bestand kan niet worden gelezen (beveiligd of beschadigd?).") from exc
    sheets = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        html, trunc = _sheet_html(ws)
        sheets.append({"name": ws.title, "html": html, "truncated": trunc})
    if not sheets:
        raise ValueError("Er staan geen zichtbare tabbladen in dit bestand.")
    return sheets


def _csv(data):
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    rows = list(csv.reader(io.StringIO(text), dialect))[:MAX_ROWS]
    n = min(max((len(r) for r in rows), default=0), MAX_COLS)
    from openpyxl.utils import get_column_letter
    out = ['<table class="xv-t"><thead><tr><th class="xv-corner"></th>']
    out += [f"<th>{get_column_letter(c + 1)}</th>" for c in range(n)]
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(rows, 1):
        out.append(f"<tr><th>{i}</th>" + "".join(f"<td>{escape(x)}</td>" for x in (r + [""] * n)[:n]) + "</tr>")
    out.append("</tbody></table>")
    return {"name": "CSV", "html": "".join(out) if rows else '<div class="xv-empty">Leeg bestand.</div>', "truncated": False}
