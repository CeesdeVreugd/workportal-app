"""Opleverdocument (afnamedocument / Document Receipt) bij projecten en orders.

Regels komen uit de orderbon van Komdex (aan te passen), beide partijen tekenen op het scherm (tablet/telefoon),
WorkPortal maakt de PDF in de taal van de klant (NL/EN/DE/FR), zet hem in de projectmap onder "3 Documenten" in
SharePoint en kan hem naar de klant mailen.
"""
import json
import os
import re

import requests
from flask import render_template, request, redirect, url_for, flash, abort, g, Response

from . import sharepoint as sp
from .db import query, execute, get_db
from .permissions import can, BEWERKEN, BEHEER
from .util import (now_iso, audit, to_int, to_float, files_for, file_path, save_signature, save_bytes, delete_file, safe_filename,
                   local, now_utc, fmt_num)
from .werk import bp, KINDS, BP_OF_KIND, _row, werk_url, sp_on

HEAD_FIELDS = ("language", "customer_name", "address", "postcode_city", "country", "attn", "phone", "reference", "description",
               "project_manager", "delivery_date", "collection_date", "location", "serial", "remarks")


def _module(p):
    return KINDS[BP_OF_KIND[p["kind"]]]["module"]


def _need(p, level=BEWERKEN):
    if not can(_module(p), level):
        abort(403)


def deliveries_for(pid):
    rows = query("SELECT d.*, u.name AS creator FROM deliveries d LEFT JOIN users u ON u.id = d.created_by"
                 " WHERE d.project_id = ? ORDER BY d.seq DESC", (pid,))
    if not rows:
        return rows
    from .werk import _contact_email
    p = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True)
    out = []
    for r in rows:
        d = dict(r)
        d["mail_default"] = d["mailed_to"] or _contact_email(p, d["attn"])   # contactpersoon van de order (T.a.v.)
        out.append(d)
    return out


def _get(p, did):
    return query("SELECT * FROM deliveries WHERE id = ? AND project_id = ?", (did, p["id"]), one=True) or abort(404)


def number(p, d):
    return f"{p['number']}-{d['seq']:02d}"


def _num(v):
    """'1,00' / '1' -> nette weergave."""
    f = to_float(v, None)
    if f is None:
        return (v or "").strip()
    return fmt_num(f, 2)


def _lines_from_bon(p):
    if not p["bon_file_id"]:
        return []
    f = query("SELECT * FROM files WHERE id = ?", (p["bon_file_id"],), one=True)
    if not f or not os.path.exists(file_path(f)):
        return []
    from .orderbon import parse
    try:
        with open(file_path(f), "rb") as fh:
            bon = parse(fh.read()) or {}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ln in bon.get("lines") or []:
        q = _num(ln.get("qty"))
        out.append({"pos": ln.get("pos") or "", "unit": ln.get("unit") or "", "description": ln.get("description") or "",
                    "qty": q, "delivered": q, "backorder": _num("0") if q else ""})
    return out


@bp.route("/<int:pid>/oplevering/nieuw", methods=["POST"])
def delivery_new(pid):
    """Nieuw opleverdocument, ingevuld uit het project, de klant en de orderbon (of een kopie van het vorige)."""
    p = _row(pid)
    _need(p)
    c = query("SELECT * FROM customers WHERE id = ?", (p["customer_id"] or 0,), one=True)
    prev = query("SELECT * FROM deliveries WHERE project_id = ? ORDER BY seq DESC LIMIT 1", (pid,), one=True)
    seq = (prev["seq"] + 1) if prev else 1
    today = local(now_utc()).strftime("%Y-%m-%d")
    if prev and request.form.get("kopie"):
        vals = {k: prev[k] for k in HEAD_FIELDS}
        lines = prev["lines_json"]
    else:
        ct = query("SELECT * FROM contacts WHERE customer_id = ? AND name = ?", (p["customer_id"] or 0, p["contact_name"] or ""), one=True)
        vals = {"language": (c["language"] if c and c["language"] else "nl"), "customer_name": c["name"] if c else None,
                "address": c["address"] if c else None,
                "postcode_city": " ".join(x for x in ((c["postcode"] or "") if c else "", (c["city"] or "") if c else "") if x) or None,
                "country": None, "attn": p["contact_name"], "phone": (ct["phone"] if ct else None) or (c["phone"] if c else None),
                "reference": p["reference"], "description": p["name"], "project_manager": p["executor"],
                "delivery_date": p["delivery_date"], "collection_date": None, "location": (c["city"] if c else None),
                "serial": None, "remarks": None}
        lines = json.dumps(_lines_from_bon(p), ensure_ascii=False)
    vals["collection_date"] = today
    vals["serial"] = vals.get("serial") or f"{p['number']}-{seq:02d}"
    now = now_iso()
    did = execute(f"INSERT INTO deliveries (project_id, seq, {', '.join(HEAD_FIELDS)}, lines_json, dv_name, created_by, created_at, updated_at)"
                  f" VALUES (?, ?, {', '.join('?' * len(HEAD_FIELDS))}, ?, ?, ?, ?, ?)",
                  [pid, seq] + [vals.get(k) for k in HEAD_FIELDS] + [lines, g.user["name"], g.user["id"], now, now])
    audit("opleverdocument aangemaakt", "project", pid, f"{p['number']}-{seq:02d}")
    n = len(json.loads(lines or "[]"))
    flash("Opleverdocument aangemaakt" + (f" met {n} regel(s) uit de orderbon" if n and not request.form.get("kopie") else "")
          + ". Controleer de gegevens en laat tekenen.", "ok")
    return redirect(werk_url("delivery_edit", p, pid=pid, did=did))


@bp.route("/<int:pid>/oplevering/<int:did>", methods=["GET", "POST"])
def delivery_edit(pid, did):
    p = _row(pid)
    _need(p)
    d = _get(p, did)
    from .talen import LANGS, LANG_LABELS
    if request.method == "POST":
        if d["cust_signed_at"]:
            flash("Dit document is al getekend en kan niet meer worden gewijzigd. Maak zo nodig een nieuwe versie.", "error")
            return redirect(werk_url("delivery_edit", p, pid=pid, did=did))
        f = request.form
        vals = {k: (f.get(k) or "").strip()[:4000] or None for k in HEAD_FIELDS}
        if vals["language"] not in LANG_LABELS:
            vals["language"] = "nl"
        rows = []
        cols = ("pos", "unit", "description", "qty", "delivered", "backorder")
        lists = {k: f.getlist("l_" + k) for k in cols}
        for i in range(len(lists["description"])):
            r = {k: (lists[k][i] if i < len(lists[k]) else "").strip()[:4000] for k in cols}
            if any(r.values()):
                rows.append(r)
        execute(f"UPDATE deliveries SET {', '.join(k + ' = ?' for k in HEAD_FIELDS)}, lines_json = ?, updated_at = ? WHERE id = ?",
                [vals[k] for k in HEAD_FIELDS] + [json.dumps(rows, ensure_ascii=False), now_iso(), did])
        flash("Opleverdocument opgeslagen.", "ok")
        if f.get("next") == "tekenen":
            return redirect(werk_url("delivery_sign", p, pid=pid, did=did))
        return redirect(werk_url("delivery_edit", p, pid=pid, did=did))
    K = KINDS[BP_OF_KIND[p["kind"]]]
    return render_template("werk/oplevering.html", K=K, B=request.blueprint, p=p, d=d, lines=json.loads(d["lines_json"] or "[]"),
                           number=number(p, d), LANGS=LANGS, sp_ok=bool(p["sp_item_id"] and not p["sp_missing"] and sp_on()))


def _render_pdf(p, d):
    from .pdf import delivery_pdf
    from .talen import deliv_tr
    sig = {f["kind"]: file_path(f) for f in files_for("delivery", d["id"]) if f["kind"] in ("sig_dv", "sig_cust")}
    info = dict(d)
    info.update(number=number(p, d), project_no=p["number"])
    L = deliv_tr(d["language"])
    pdf = delivery_pdf(info, json.loads(d["lines_json"] or "[]"), sig.get("sig_dv"), sig.get("sig_cust"), d["language"],
                       number_label=L["order"] if p["kind"] == "order" else L["project"])
    fname = safe_filename(f"{L['file']} {number(p, d)}.pdf")
    return pdf, fname


@bp.route("/<int:pid>/oplevering/<int:did>/pdf")
def delivery_pdf_view(pid, did):
    p = _row(pid)
    _need(p, 1)
    d = _get(p, did)
    if d["pdf_file_id"] and not request.args.get("voorbeeld"):
        f = query("SELECT * FROM files WHERE id = ?", (d["pdf_file_id"],), one=True)
        if f and os.path.exists(file_path(f)):
            with open(file_path(f), "rb") as fh:
                return Response(fh.read(), mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{f["filename"]}"'})
    pdf, fname = _render_pdf(p, d)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}"'})


def _to_sharepoint(p, fname, pdf):
    conn = get_db()
    if not p["sp_item_id"] or p["sp_missing"] or not sp.connected(conn):
        return None
    from .aantekeningen import _find_or_make, SP_DOCS_RE, SP_DOCS_DEFAULT
    try:
        g_ = sp.client()
        drive, _ = sp._ctx(conn)
        docs = _find_or_make(g_, drive, p["sp_item_id"], SP_DOCS_RE, SP_DOCS_DEFAULT)
        g_.upload(drive, docs["id"], fname, pdf, conflict="replace")
        return True, f"In SharePoint gezet: {docs['name']}/{fname}"
    except sp.GraphError as exc:
        return False, f"Opslaan in SharePoint mislukt: {exc.message}"
    except (requests.RequestException, RuntimeError):
        return False, "SharePoint is op dit moment niet bereikbaar."


@bp.route("/<int:pid>/oplevering/<int:did>/tekenen", methods=["GET", "POST"])
def delivery_sign(pid, did):
    p = _row(pid)
    _need(p)
    d = _get(p, did)
    if d["cust_signed_at"]:
        flash("Dit document is al getekend.", "info")
        return redirect(werk_url("detail", p, pid=pid) + "#oplevering")
    from .werk import _contact_email
    mail_to = _contact_email(p, d["attn"])
    if request.method == "POST":
        f = request.form
        dv_name, cust_name = (f.get("dv_name") or "").strip()[:120], (f.get("cust_name") or "").strip()[:120]
        s1, s2 = f.get("sig_dv") or "", f.get("sig_cust") or ""
        if not (dv_name and cust_name and s1.startswith("data:image/png") and s2.startswith("data:image/png")):
            flash("Vul beide namen in en laat beide partijen tekenen.", "error")
            return render_template("werk/oplevering_tekenen.html", K=KINDS[BP_OF_KIND[p["kind"]]], B=request.blueprint, p=p, d=d,
                                   number=number(p, d), mail_to=f.get("mail_to") or mail_to, form=f)
        now = now_iso()
        for old in files_for("delivery", did):
            if old["kind"] in ("sig_dv", "sig_cust"):
                delete_file(old["id"])
        for kind, data in (("sig_dv", s1), ("sig_cust", s2)):
            fid = save_signature("delivery", did, data)
            execute("UPDATE files SET kind = ? WHERE id = ?", (kind, fid))
        execute("UPDATE deliveries SET dv_name = ?, dv_signed_at = ?, cust_company = ?, cust_name = ?, cust_signed_at = ?, updated_at = ?"
                " WHERE id = ?", (dv_name, now, (f.get("cust_company") or "").strip()[:150] or d["customer_name"], cust_name, now, now, did))
        d = _get(p, did)
        pdf, fname = _render_pdf(p, d)
        fid = save_bytes("delivery", did, pdf, fname, kind="pdf", mime="application/pdf")
        execute("UPDATE deliveries SET pdf_file_id = ? WHERE id = ?", (fid, did))
        audit("opleverdocument getekend", "project", pid, f"{number(p, d)} – {cust_name}")
        msgs = [f"{number(p, d)} getekend."]
        res = _to_sharepoint(p, fname, pdf)
        if res:
            execute("UPDATE deliveries SET sp_status = ? WHERE id = ?", (res[1], did))
            msgs.append(res[1] + ".")
        to = [a.strip() for a in re.split(r"[;,\s]+", f.get("mail_to") or "") if a.strip()]
        if f.get("mail") and to:
            ok, err = _mail(p, d, to, pdf, fname)
            msgs.append(("Gemaild naar " + ", ".join(to) + ".") if ok else f"Mailen mislukt: {err}")
        flash(" ".join(msgs), "ok")
        return redirect(werk_url("detail", p, pid=pid) + "#oplevering")
    return render_template("werk/oplevering_tekenen.html", K=KINDS[BP_OF_KIND[p["kind"]]], B=request.blueprint, p=p, d=d,
                           number=number(p, d), mail_to=mail_to, form={})


def _mail(p, d, to, pdf, fname):
    from .mail import send_mail_with_files
    from .talen import deliv_tr, tr
    from .util import get_setting
    L, T = deliv_tr(d["language"]), tr(d["language"])
    sender = get_setting("cert_sender") or os.environ.get("CERT_MAIL_FROM") or os.environ.get("MAIL_FROM") or ""
    greet = T["greet"].format(n=d["attn"]) if d["attn"] else T["greet_none"]
    body = greet + "\n\n" + L["body"].format(desc=d["description"] or number(p, d)) + "\n\n" + T["questions"]
    bad = [a for a in to if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
    if bad:
        return False, "ongeldig e-mailadres: " + ", ".join(bad)
    cc = [g.user["email"]] if g.user["email"] and g.user["email"] not in to else []
    ok, err = send_mail_with_files(sender, to, L["subject"].format(nr=number(p, d)), body, [(fname, pdf, "application/pdf")],
                                   cc=cc or None, signer="werkvoorbereiding:" + (g.user["name"] or ""))
    if ok:
        execute("UPDATE deliveries SET mailed_at = ?, mailed_to = ? WHERE id = ?", (now_iso(), ", ".join(to)[:300], d["id"]))
    return ok, err


@bp.route("/<int:pid>/oplevering/<int:did>/mailen", methods=["POST"])
def delivery_mail(pid, did):
    p = _row(pid)
    _need(p)
    d = _get(p, did)
    if not d["cust_signed_at"]:
        abort(400)
    to = [a.strip() for a in re.split(r"[;,\s]+", request.form.get("mail_to") or "") if a.strip()]
    pdf, fname = None, None
    f = query("SELECT * FROM files WHERE id = ?", (d["pdf_file_id"] or 0,), one=True)
    if f and os.path.exists(file_path(f)):
        with open(file_path(f), "rb") as fh:
            pdf, fname = fh.read(), f["filename"]
    else:
        pdf, fname = _render_pdf(p, d)
    ok, err = _mail(p, d, to, pdf, fname) if to else (False, "geen e-mailadres")
    flash(("Gemaild naar " + ", ".join(to) + ".") if ok else f"Mailen mislukt: {err}", "ok" if ok else "error")
    return redirect(werk_url("detail", p, pid=pid) + "#oplevering")


@bp.route("/<int:pid>/oplevering/<int:did>/verwijderen", methods=["POST"])
def delivery_delete(pid, did):
    p = _row(pid)
    d = _get(p, did)
    if d["cust_signed_at"]:
        _need(p, BEHEER)
    else:
        _need(p)
    for f in files_for("delivery", did):
        delete_file(f["id"])
    execute("DELETE FROM deliveries WHERE id = ?", (did,))
    audit("opleverdocument verwijderd", "project", pid, number(p, d))
    flash(f"{number(p, d)} verwijderd" + (" (de PDF in SharePoint is niet aangeraakt)." if d["sp_status"] else "."), "ok")
    return redirect(werk_url("detail", p, pid=pid) + "#oplevering")
