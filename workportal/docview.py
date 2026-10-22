"""Word-bestanden (.docx) omzetten naar HTML om te lezen in WorkPortal, zonder extra pakketten.

Ondersteunt: koppen en stijlen, vet/cursief/onderstreept/kleur/grootte, uitlijning en inspringen,
opsommingen en nummering, tabellen (met samengevoegde cellen en arcering), afbeeldingen (ook in
tekstvakken), hyperlinks, kop- en voettekst, voetnoten en pagina-einden. Alles wordt ge-escaped;
er komt geen script of externe inhoud in de uitvoer.
"""
import base64
import html
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
try:   # veilig XML inlezen (geen entity-bommen of externe verwijzingen uit een kwaadaardig .docx)
    from defusedxml.ElementTree import fromstring as _safe_fromstring
except ImportError:  # pragma: no cover
    _safe_fromstring = ET.fromstring

MAX_BYTES = 20 * 1048576
MAX_UNZIPPED = 250 * 1048576

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "v": "urn:schemas-microsoft-com:vml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def q(tag):
    p, _, t = tag.partition(":")
    return "{%s}%s" % (NS[p], t)


W = lambda t: "{%s}%s" % (NS["w"], t)  # noqa: E731
R_ID = q("r:id")
R_EMBED = q("r:embed")
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
HIGHLIGHT = {"yellow": "#FFFF00", "green": "#00FF00", "cyan": "#00FFFF", "magenta": "#FF00FF", "blue": "#0000FF",
             "red": "#FF0000", "darkBlue": "#000080", "darkCyan": "#008080", "darkGreen": "#008000",
             "darkMagenta": "#800080", "darkRed": "#800000", "darkYellow": "#808000", "darkGray": "#808080",
             "lightGray": "#C0C0C0", "black": "#000000", "white": "#FFFFFF"}
BULLET_MAP = {"": "•", "": "▪", "": "➢", "": "✓", "": "❖", "": "➔", "o": "◦", "": "•"}
IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".bmp": "image/bmp", ".webp": "image/webp", ".svg": None, ".tif": "tiff", ".tiff": "tiff"}


def wval(el, name="val"):
    if el is None:
        return None
    return el.get(W(name))


def on(el):
    """Aan/uit-eigenschap (<w:b/>, <w:b w:val="0"/>)."""
    if el is None:
        return None
    v = el.get(W("val"))
    return v not in ("0", "false", "off", "none")


def twips(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def px(tw):
    return round(tw / 15, 1)  # 1 px = 15 twips (96 dpi)


def esc(s):
    return html.escape(s, quote=True)


class Doc:
    def __init__(self, data):
        if not data:
            raise ValueError("Het bestand is leeg.")
        if len(data) > MAX_BYTES:
            raise ValueError("Dit bestand is te groot om hier te bekijken (meer dan 20 MB). Download het bestand.")
        if data[:4] == b"\xd0\xcf\x11\xe0":
            raise ValueError("Dit is een oud Word-bestand (.doc). Dat kan hier niet getoond worden. "
                             "Download het bestand, of sla het in Word op als .docx.")
        try:
            self.z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("Dit bestand is geen geldig Word-document (.docx).")
        if sum(i.file_size for i in self.z.infolist()) > MAX_UNZIPPED:
            raise ValueError("Dit document is te groot om hier te bekijken. Download het bestand.")
        names = set(self.z.namelist())
        self.main = "word/document.xml"
        if self.main not in names:
            ct = self._read("[Content_Types].xml") or b""
            m = re.search(rb'PartName="/([^"]+)"[^>]*document\.main', ct)
            if m and m.group(1).decode() in names:
                self.main = m.group(1).decode()
            else:
                raise ValueError("Dit bestand is geen geldig Word-document (.docx).")
        self.styles = {}
        self._last_style = None
        self.default_ppr = None
        self.default_sz = 22
        self.numbering = {}
        self.counters = {}
        self.footnotes = []
        self.fn_html = {}
        self.img_budget = 12 * 1048576
        self.field_depth = 0
        self.field_hidden = []
        self._load_styles()
        self._load_numbering()
        self._load_notes()

    # ------------------------------------------------------------------ inlezen
    def _read(self, name):
        try:
            return self.z.read(name)
        except KeyError:
            return None

    def _xml(self, name):
        raw = self._read(name)
        if not raw:
            return None
        if b"<!DOCTYPE" in raw[:2000] or b"<!ENTITY" in raw[:5000]:
            raise ValueError("Dit document kan niet veilig gelezen worden.")
        try:
            return _safe_fromstring(raw)
        except Exception:
            return None

    def _rels(self, part):
        d, b = posixpath.split(part)
        root = self._xml(posixpath.join(d, "_rels", b + ".rels"))
        out = {}
        if root is None:
            return out
        for r in root.findall(q("rel:Relationship")):
            tgt = r.get("Target") or ""
            if r.get("TargetMode") == "External":
                out[r.get("Id")] = ("ext", tgt)
            else:
                out[r.get("Id")] = ("int", posixpath.normpath(posixpath.join(d, tgt)) if not tgt.startswith("/") else tgt.lstrip("/"))
        return out

    def _load_styles(self):
        root = self._xml("word/styles.xml")
        if root is None:
            return
        dd = root.find(W("docDefaults"))
        if dd is not None:
            self.default_ppr = dd.find(W("pPrDefault") + "/" + W("pPr"))
            sz = dd.find(".//" + W("rPrDefault") + "/" + W("rPr") + "/" + W("sz"))
            if sz is not None:
                self.default_sz = twips(wval(sz), 22) or 22
        for s in root.findall(W("style")):
            sid = s.get(W("styleId"))
            name = wval(s.find(W("name"))) or sid or ""
            self.styles[sid] = {
                "name": name.lower(), "type": s.get(W("type")),
                "based": wval(s.find(W("basedOn"))),
                "pPr": s.find(W("pPr")), "rPr": s.find(W("rPr")), "tblPr": s.find(W("tblPr")),
            }

    def _style_chain(self, sid):
        out, seen = [], set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            out.append(self.styles[sid])
            sid = self.styles[sid]["based"]
        return out  # eigen stijl eerst

    def _load_numbering(self):
        root = self._xml("word/numbering.xml")
        if root is None:
            return
        abstract = {}
        for an in root.findall(W("abstractNum")):
            lv = {}
            for l in an.findall(W("lvl")):
                ilvl = twips(l.get(W("ilvl")))
                ind = l.find(W("pPr") + "/" + W("ind"))
                lv[ilvl] = {"fmt": wval(l.find(W("numFmt"))) or "decimal", "text": wval(l.find(W("lvlText"))) or "",
                            "start": twips(wval(l.find(W("start"))), 1),
                            "left": twips(wval(ind, "left") or wval(ind, "start")) if ind is not None else 720 * (ilvl + 1),
                            "hanging": twips(wval(ind, "hanging")) if ind is not None else 360}
            abstract[an.get(W("abstractNumId"))] = lv
        for n in root.findall(W("num")):
            a = abstract.get(wval(n.find(W("abstractNumId"))), {})
            lv = {k: dict(v) for k, v in a.items()}
            for o in n.findall(W("lvlOverride")):
                so = o.find(W("startOverride"))
                il = twips(o.get(W("ilvl")))
                if so is not None and il in lv:
                    lv[il]["start"] = twips(wval(so), 1)
            self.numbering[n.get(W("numId"))] = lv

    def _load_notes(self):
        root = self._xml("word/footnotes.xml")
        if root is None:
            return
        self._fn_rels = self._rels("word/footnotes.xml")
        for fn in root.findall(W("footnote")):
            if fn.get(W("type")) in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            self.fn_html[fn.get(W("id"))] = fn

    # ------------------------------------------------------------------ nummering
    @staticmethod
    def _fmt_num(n, fmt):
        if fmt in ("lowerLetter", "upperLetter"):
            s = ""
            while n > 0:
                n, r = divmod(n - 1, 26)
                s = chr(97 + r) + s
            return s.upper() if fmt == "upperLetter" else s
        if fmt in ("lowerRoman", "upperRoman"):
            vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                    (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
            s = ""
            for v, t in vals:
                while n >= v:
                    s += t
                    n -= v
            return s.upper() if fmt == "upperRoman" else s
        if fmt == "decimalZero":
            return "%02d" % n
        return str(n)

    def _list_label(self, num_id, ilvl):
        lv = self.numbering.get(num_id)
        if not lv or num_id == "0":
            return None, 0, 0
        lvl = lv.get(ilvl) or lv.get(0) or {"fmt": "bullet", "text": "•", "start": 1, "left": 720, "hanging": 360}
        cnt = self.counters.setdefault(num_id, {})
        cnt[ilvl] = cnt.get(ilvl, lvl["start"] - 1) + 1
        for k in list(cnt):
            if k > ilvl:
                del cnt[k]
        if lvl["fmt"] == "bullet":
            t = lvl["text"]
            label = BULLET_MAP.get(t, t if t and ord(t[0]) < 0xF000 else "•")
        elif lvl["fmt"] == "none":
            label = ""
        else:
            def sub(m):
                k = int(m.group(1)) - 1
                l2 = lv.get(k, lvl)
                return self._fmt_num(cnt.get(k, l2["start"]), l2["fmt"] if k != ilvl else lvl["fmt"])
            label = re.sub(r"%(\d)", sub, lvl["text"])
        return label, lvl["left"], lvl["hanging"]

    # ------------------------------------------------------------------ opmaak
    def _run_css(self, rpr, base=False):
        css = []
        if rpr is None:
            return css
        b = on(rpr.find(W("b")))
        if b is not None:
            css.append("font-weight:%s" % ("bold" if b else "normal"))
        i = on(rpr.find(W("i")))
        if i is not None:
            css.append("font-style:%s" % ("italic" if i else "normal"))
        deco = []
        u = rpr.find(W("u"))
        if u is not None and wval(u) != "none":
            deco.append("underline")
        if on(rpr.find(W("strike"))) or on(rpr.find(W("dstrike"))):
            deco.append("line-through")
        if deco:
            css.append("text-decoration:" + " ".join(deco))
        c = wval(rpr.find(W("color")))
        if c and HEX.match(c) and c.upper() != "000000":
            css.append("color:#" + c)
        sz = wval(rpr.find(W("sz")))
        if sz and sz.isdigit() and 6 <= int(sz) <= 400:
            css.append("font-size:%.2fem" % (int(sz) / self.default_sz))
        hl = wval(rpr.find(W("highlight")))
        if hl in HIGHLIGHT and hl != "none":
            css.append("background:" + HIGHLIGHT[hl])
        shd = rpr.find(W("shd"))
        fill = wval(shd, "fill") if shd is not None else None
        if fill and HEX.match(fill) and fill.upper() != "FFFFFF":
            css.append("background:#" + fill)
        va = wval(rpr.find(W("vertAlign")))
        if va == "superscript":
            css.append("vertical-align:super;font-size:.75em")
        elif va == "subscript":
            css.append("vertical-align:sub;font-size:.75em")
        if on(rpr.find(W("caps"))):
            css.append("text-transform:uppercase")
        if on(rpr.find(W("smallCaps"))):
            css.append("font-variant:small-caps")
        return css

    def _para_props(self, ppr):
        """Stijl-eigenschappen van een alinea (eigen + stijlketen)."""
        sid = wval(ppr.find(W("pStyle"))) if ppr is not None else None
        chain = self._style_chain(sid or "Normal") or self._style_chain(self._default_para_style())
        return sid, chain

    def _default_para_style(self):
        for k, s in self.styles.items():
            if s["type"] == "paragraph" and s["name"] == "normal":
                return k
        return None

    def _find_ppr(self, ppr, chain, tag):
        if ppr is not None and ppr.find(W(tag)) is not None:
            return ppr.find(W(tag))
        for s in chain:
            if s["pPr"] is not None and s["pPr"].find(W(tag)) is not None:
                return s["pPr"].find(W(tag))
        if tag == "spacing" and self.default_ppr is not None:
            return self.default_ppr.find(W(tag))
        return None

    # ------------------------------------------------------------------ onderdelen
    def render(self):
        root = self._xml(self.main)
        if root is None:
            raise ValueError("Dit Word-document kon niet gelezen worden.")
        self.rels = self._rels(self.main)
        body = root.find(W("body"))
        if body is None:
            raise ValueError("Dit Word-document is leeg.")
        sect = body.find(W("sectPr"))
        page = {"w": 11906, "ml": 1440, "mr": 1440, "mt": 1440, "mb": 1440}
        hdr = ftr = ""
        if sect is not None:
            pg, mar = sect.find(W("pgSz")), sect.find(W("pgMar"))
            if pg is not None:
                page["w"] = twips(wval(pg, "w"), 11906) or 11906
            if mar is not None:
                for k, a in (("ml", "left"), ("mr", "right"), ("mt", "top"), ("mb", "bottom")):
                    page[k] = max(0, min(4000, abs(twips(wval(mar, a), page[k]))))
            hdr = self._hf(sect, "headerReference")
            ftr = self._hf(sect, "footerReference")
        content = self._blocks(body, self.rels)
        notes = ""
        if self.footnotes:
            items = []
            for n, fid in self.footnotes:
                fn = self.fn_html.get(fid)
                if fn is not None:
                    items.append('<li id="dv-fn%s" value="%d">%s</li>' % (esc(fid), n, self._blocks(fn, self._fn_rels)))
            if items:
                notes = '<ol class="dv-notes">%s</ol>' % "".join(items)
        w = max(300, page["w"] - page["ml"] - page["mr"])
        return {
            "html": ('%s<div class="dv-body">%s</div>%s%s' % (
                '<div class="dv-hdr">%s</div>' % hdr if hdr.strip() else "", content, notes,
                '<div class="dv-ftr">%s</div>' % ftr if ftr.strip() else "")),
            "width": px(w + page["ml"] + page["mr"]), "pad_l": px(page["ml"]), "pad_r": px(page["mr"]),
            "pad_t": px(min(page["mt"], 1440)), "pad_b": px(min(page["mb"], 1440)),
            "font": round(self.default_sz / 2 * 96 / 72, 2),
        }

    def _hf(self, sect, tag):
        refs = {r.get(W("type")): r.get(R_ID) for r in sect.findall(W(tag))}
        rid = refs.get("default") or refs.get("first")
        if not rid or rid not in self.rels or self.rels[rid][0] != "int":
            return ""
        part = self.rels[rid][1]
        root = self._xml(part)
        if root is None:
            return ""
        saved = self.counters
        self.counters = {}
        out = self._blocks(root, self._rels(part))
        self.counters = saved
        return out

    def _blocks(self, parent, rels):
        out = []
        for el in parent:
            t = el.tag
            if t == W("p"):
                out.append(self._para(el, rels))
            elif t == W("tbl"):
                out.append(self._table(el, rels))
            elif t in (W("sdt"),):
                c = el.find(W("sdtContent"))
                if c is not None:
                    out.append(self._blocks(c, rels))
            elif t in (W("customXml"), W("ins"), W("smartTag")):
                out.append(self._blocks(el, rels))
            elif t == q("mc:AlternateContent"):
                ch = el.find(q("mc:Choice"))
                if ch is None:
                    ch = el.find(q("mc:Fallback"))
                if ch is not None:
                    out.append(self._blocks(ch, rels))
        return "".join(out)

    def _para(self, p, rels):
        ppr = p.find(W("pPr"))
        sid, chain = self._para_props(ppr)
        names = [s["name"] for s in chain]
        tag = "p"
        for nm in names:
            m = re.match(r"(heading|kop)\s*(\d)", nm)
            if m:
                tag = "h%d" % min(6, int(m.group(2)) + 1)
                break
            if nm in ("title", "titel"):
                tag = "h1"
                break
        if tag == "p":
            ol = self._find_ppr(ppr, chain, "outlineLvl")
            if ol is not None and (wval(ol) or "9").isdigit() and int(wval(ol)) < 6:
                tag = "h%d" % min(6, int(wval(ol)) + 2)
        css = []
        # stijl-letteropmaak (van basis naar eigen)
        for s in reversed(chain):
            css += self._run_css(s["rPr"], base=True)
        jc = wval(self._find_ppr(ppr, chain, "jc"))
        if jc in ("center", "right", "both", "end", "distribute"):
            css.append("text-align:%s" % {"both": "justify", "distribute": "justify", "end": "right"}.get(jc, jc))
        ind = self._find_ppr(ppr, chain, "ind")
        left = twips(wval(ind, "left") or wval(ind, "start")) if ind is not None else 0
        first = (twips(wval(ind, "firstLine")) - twips(wval(ind, "hanging"))) if ind is not None else 0
        sp = self._find_ppr(ppr, chain, "spacing")
        before = twips(wval(sp, "before")) if sp is not None and wval(sp, "before") else None
        after = twips(wval(sp, "after")) if sp is not None and wval(sp, "after") else None
        cs = self._find_ppr(ppr, chain, "contextualSpacing")
        if cs is not None and on(cs):   # Word: geen witruimte tussen alinea's met dezelfde stijl (lijsten)
            if self._last_style == sid:
                before = 0
            after = min(after or 0, 40)
        self._last_style = sid
        if before is not None:
            css.append("margin-top:%spx" % min(px(before), 80))
        if after is not None:
            css.append("margin-bottom:%spx" % min(px(after), 80))
        shd = self._find_ppr(ppr, chain, "shd")
        fill = wval(shd, "fill") if shd is not None else None
        if fill and HEX.match(fill) and fill.upper() != "FFFFFF":
            css.append("background:#%s;padding:2px 4px" % fill)
        label = None
        num = self._find_ppr(ppr, chain, "numPr")
        if num is not None:
            nid = wval(num.find(W("numId")))
            il = twips(wval(num.find(W("ilvl"))))
            if nid:
                label, nl, hang = self._list_label(nid, il)
                if label is not None:
                    if ind is None or not (wval(ind, "left") or wval(ind, "start")):
                        left = nl
                    first = -(hang or 360)
        if left:
            css.append("margin-left:%spx" % max(-200, min(px(left), 400)))
        if first and label is None:
            css.append("text-indent:%spx" % max(-200, min(px(first), 200)))
        inner = self._runs(p, rels)
        if inner == '<hr class="dv-page">':
            return inner
        if label is not None:
            hang_px = max(18, px(-first)) if first < 0 else 18
            inner = '<span class="dv-num" style="width:%spx;margin-left:-%spx">%s</span>%s' % (hang_px, hang_px, esc(label), inner)
        style = ' style="%s"' % ";".join(css) if css else ""
        if not inner.strip() or inner in ("<br>",):
            return '<%s class="dv-empty"%s>&nbsp;</%s>' % (tag, style, tag)
        pb = ""
        pbb = self._find_ppr(ppr, chain, "pageBreakBefore")
        if pbb is not None and on(pbb):
            pb = '<hr class="dv-page">'
        return '%s<%s%s>%s</%s>' % (pb, tag, style, inner, tag)

    def _runs(self, parent, rels, link=None):
        out = []
        for el in parent:
            t = el.tag
            if t == W("r"):
                out.append(self._run(el, rels))
            elif t == W("hyperlink"):
                href = None
                rid = el.get(R_ID)
                if rid and rid in rels and rels[rid][0] == "ext":
                    u = rels[rid][1].strip()
                    if re.match(r"^(https?:|mailto:|tel:)", u, re.I):
                        href = u
                elif el.get(W("anchor")):
                    href = "#" + el.get(W("anchor"))
                inner = self._runs(el, rels)
                if href and href.startswith("#"):
                    out.append('<a href="%s">%s</a>' % (esc(href), inner))
                elif href:
                    out.append('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (esc(href), inner))
                else:
                    out.append(inner)
            elif t == W("fldSimple"):
                out.append(self._runs(el, rels))
            elif t in (W("ins"), W("smartTag"), W("customXml"), W("dir"), W("bdo")):
                out.append(self._runs(el, rels))
            elif t == W("sdt"):
                c = el.find(W("sdtContent"))
                if c is not None:
                    out.append(self._runs(c, rels))
            elif t == W("bookmarkStart") and el.get(W("name")) and not el.get(W("name")).startswith("_"):
                out.append('<a id="%s"></a>' % esc(el.get(W("name"))))
            elif t == q("mc:AlternateContent"):
                ch = el.find(q("mc:Choice"))
                if ch is None:
                    ch = el.find(q("mc:Fallback"))
                if ch is not None:
                    out.append(self._runs(ch, rels))
        return "".join(out)

    def _run(self, r, rels):
        rpr = r.find(W("rPr"))
        if rpr is not None and on(rpr.find(W("vanish"))):
            return ""
        parts = []
        for el in r:
            t = el.tag
            if t == W("fldChar"):
                ft = el.get(W("fldCharType"))
                if ft == "begin":
                    self.field_hidden.append(True)
                elif ft == "separate" and self.field_hidden:
                    self.field_hidden[-1] = False
                elif ft == "end" and self.field_hidden:
                    self.field_hidden.pop()
                continue
            if self.field_hidden and self.field_hidden[-1]:
                continue   # veldcode (bijv. PAGE, HYPERLINK …) niet tonen, alleen het resultaat
            if t == W("t"):
                parts.append(esc(el.text or ""))
            elif t == W("tab") or t == W("ptab"):
                parts.append('<span class="dv-tab"></span>')
            elif t == W("br"):
                parts.append('<hr class="dv-page">' if el.get(W("type")) == "page" else "<br>")
            elif t == W("cr"):
                parts.append("<br>")
            elif t == W("noBreakHyphen"):
                parts.append("‑")
            elif t == W("softHyphen"):
                parts.append("&shy;")
            elif t == W("sym"):
                ch = el.get(W("char")) or ""
                try:
                    c = chr(int(ch, 16))
                    parts.append(esc(BULLET_MAP.get(c, c if ord(c) < 0xF000 else "•")))
                except ValueError:
                    pass
            elif t == W("drawing"):
                parts.append(self._drawing(el, rels))
            elif t == W("pict") or t == W("object"):
                parts.append(self._vml(el, rels))
            elif t == q("mc:AlternateContent"):
                ch = el.find(q("mc:Choice"))
                if ch is None:
                    ch = el.find(q("mc:Fallback"))
                if ch is not None:
                    for d in ch:
                        if d.tag == W("drawing"):
                            parts.append(self._drawing(d, rels))
                        elif d.tag == W("pict"):
                            parts.append(self._vml(d, rels))
            elif t == W("footnoteReference"):
                fid = el.get(W("id"))
                n = len(self.footnotes) + 1
                self.footnotes.append((n, fid))
                parts.append('<sup class="dv-fnref"><a href="#dv-fn%s">%d</a></sup>' % (esc(fid or ""), n))
        s = "".join(parts)
        if not s:
            return ""
        css = self._run_css(rpr)
        rs = wval(rpr.find(W("rStyle"))) if rpr is not None else None
        if rs:
            for st in reversed(self._style_chain(rs)):
                css = self._run_css(st["rPr"]) + css
        return '<span style="%s">%s</span>' % (";".join(css), s) if css else s

    # ------------------------------------------------------------------ afbeeldingen
    def _image(self, rid, rels, w_px=None, h_px=None, alt=""):
        if not rid or rid not in rels:
            return ""
        kind, tgt = rels[rid]
        if kind != "int":
            return '<span class="dv-noimg">[gekoppelde afbeelding]</span>'
        data = self._read(tgt)
        if not data:
            return ""
        ext = posixpath.splitext(tgt)[1].lower()
        mime = IMG_MIME.get(ext, "")
        if mime is None or ext in (".emf", ".wmf") or not mime:
            # EMF/WMF/onbekend: proberen om te zetten, anders melding
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                im.load()
                data, mime = self._to_png(im), "image/png"
            except Exception:
                return '<span class="dv-noimg">[afbeelding (%s) kan hier niet getoond worden]</span>' % esc(ext.lstrip(".").upper() or "?")
        elif mime == "tiff" or len(data) > 450 * 1024:
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                im.load()
                if mime == "tiff" or max(im.size) > 1800 or len(data) > 450 * 1024:
                    im.thumbnail((1800, 1800))
                    if im.mode in ("RGBA", "LA", "P"):
                        data, mime = self._to_png(im), "image/png"
                    else:
                        b = io.BytesIO()
                        im.convert("RGB").save(b, "JPEG", quality=82)
                        data, mime = b.getvalue(), "image/jpeg"
            except Exception:
                if mime == "tiff":
                    return '<span class="dv-noimg">[afbeelding (TIFF) kan hier niet getoond worden]</span>'
        if len(data) > self.img_budget:
            return '<span class="dv-noimg">[afbeelding weggelaten: document te groot – download het bestand]</span>'
        self.img_budget -= len(data)
        style = ""
        if w_px and w_px > 2:
            style = ' style="width:%spx"' % round(w_px)
        return '<img src="data:%s;base64,%s" alt="%s"%s>' % (mime, base64.b64encode(data).decode(), esc(alt or "Afbeelding"), style)

    @staticmethod
    def _to_png(im):
        b = io.BytesIO()
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA")
        im.save(b, "PNG", optimize=True)
        return b.getvalue()

    def _drawing(self, d, rels):
        out = []
        for cont in d:
            ext = cont.find(q("wp:extent"))
            w = twips(ext.get("cx")) / 9525 if ext is not None else None
            h = twips(ext.get("cy")) / 9525 if ext is not None else None
            doc_pr = cont.find(q("wp:docPr"))
            alt = (doc_pr.get("descr") or doc_pr.get("title") or "") if doc_pr is not None else ""
            blips = cont.findall(".//" + q("a:blip"))
            for b in blips:
                out.append(self._image(b.get(R_EMBED), rels, w if len(blips) == 1 else None, h, alt))
            for tx in cont.iter(W("txbxContent")):
                out.append('<div class="dv-box">%s</div>' % self._blocks(tx, rels))
            if cont.tag == q("wp:anchor") and out:
                return '<span class="dv-float">%s</span>' % "".join(out)
        return "".join(out)

    def _vml(self, el, rels):
        out = []
        for im in el.iter(q("v:imagedata")):
            rid = im.get(R_ID)
            style = ""
            for sh in el.iter():
                if sh.get("style"):
                    style = sh.get("style")
                    break
            m = re.search(r"width:\s*([\d.]+)(pt|px|in)", style)
            w = None
            if m:
                w = float(m.group(1)) * {"pt": 96 / 72, "px": 1, "in": 96}[m.group(2)]
            out.append(self._image(rid, rels, w))
        for tx in el.iter(W("txbxContent")):
            out.append('<div class="dv-box">%s</div>' % self._blocks(tx, rels))
        return "".join(out)

    # ------------------------------------------------------------------ tabellen
    def _table(self, tbl, rels):
        tpr = tbl.find(W("tblPr"))
        sid = wval(tpr.find(W("tblStyle"))) if tpr is not None else None
        chain = self._style_chain(sid)
        bordered = False
        for src in [tpr] + [s["tblPr"] for s in chain]:
            if src is None:
                continue
            tb = src.find(W("tblBorders"))
            if tb is not None:
                bordered = any(wval(b) not in ("nil", "none", None) for b in tb)
                break
        if not bordered and any(("grid" in s["name"] or "raster" in s["name"]) and "light" not in s["name"] for s in chain):
            bordered = True
        grid = [twips(wval(g, "w")) for g in tbl.findall(W("tblGrid") + "/" + W("gridCol"))]
        total = sum(grid) or 0
        rows = []
        origin = {}   # kolom -> cel (voor verticaal samengevoegd)
        for tr in tbl.findall(W("tr")):
            trpr = tr.find(W("trPr"))
            is_head = trpr is not None and on(trpr.find(W("tblHeader")))
            cells = []
            col = 0
            gb = trpr.find(W("gridBefore")) if trpr is not None else None
            if gb is not None:
                col += twips(wval(gb))
            for tc in tr.findall(W("tc")) + [x for s in tr.findall(W("sdt")) for c in s.findall(W("sdtContent")) for x in c.findall(W("tc"))]:
                tcpr = tc.find(W("tcPr"))
                span = max(1, twips(wval(tcpr.find(W("gridSpan"))) if tcpr is not None else 1, 1))
                vm = tcpr.find(W("vMerge")) if tcpr is not None else None
                if vm is not None and wval(vm) != "restart":
                    o = origin.get(col)
                    if o is not None:
                        o["rowspan"] += 1
                        col += span
                        continue
                css = []
                if tcpr is not None:
                    shd = tcpr.find(W("shd"))
                    fill = wval(shd, "fill") if shd is not None else None
                    if fill and HEX.match(fill) and fill.upper() != "FFFFFF":
                        css.append("background:#" + fill)
                    va = wval(tcpr.find(W("vAlign")))
                    if va in ("center", "bottom"):
                        css.append("vertical-align:" + ("middle" if va == "center" else "bottom"))
                    tcb = tcpr.find(W("tcBorders"))
                    if tcb is not None and not bordered:
                        for side in ("top", "bottom", "left", "right"):
                            b = tcb.find(W(side))
                            if b is not None and wval(b) not in ("nil", "none"):
                                css.append("border-%s:1px solid #9AA0B4" % side)
                cell = {"html": self._blocks(tc, rels), "span": span, "rowspan": 1, "css": css, "head": is_head}
                for k in range(col, col + span):
                    origin[k] = cell
                cells.append(cell)
                col += span
            rows.append(cells)
        cols = "".join('<col style="width:%s%%">' % round(g / total * 100, 2) for g in grid) if total else ""
        width = ' style="width:%spx" data-w="%spx"' % (min(px(total), 2400), min(px(total), 2400)) if total else ""
        body = []
        for cells in rows:
            tds = []
            for c in cells:
                tagname = "th" if c["head"] else "td"
                attrs = ""
                if c["span"] > 1:
                    attrs += ' colspan="%d"' % c["span"]
                if c["rowspan"] > 1:
                    attrs += ' rowspan="%d"' % c["rowspan"]
                if c["css"]:
                    attrs += ' style="%s"' % ";".join(c["css"])
                tds.append("<%s%s>%s</%s>" % (tagname, attrs, c["html"], tagname))
            body.append("<tr>%s</tr>" % "".join(tds))
        return '<div class="dv-tw"><table class="dv-t%s"%s><colgroup>%s</colgroup>%s</table></div>' % (
            " dv-grid" if bordered else "", width, cols, "".join(body))


def render(data):
    """Geeft dict met html en paginamaten; ValueError met een Nederlandse melding als het niet lukt."""
    try:
        return Doc(data).render()
    except ValueError:
        raise
    except Exception:
        raise ValueError("Dit Word-document kon niet getoond worden. Download het bestand om het te openen.")
