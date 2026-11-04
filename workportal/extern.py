"""Externe monteur: het geplande bezoek invullen en de klant laten tekenen via een persoonlijke link, zonder account.

Veiligheid:
- de link bevat een willekeurige sleutel van 256 bits; in de database staat alleen de SHA-256 ervan;
- de link geeft alleen toegang tot dít bezoek (geen andere WorkPortal-pagina's, geen bestanden van de klant);
- geldig tot en met 2 dagen na de geplande datum, één keer te gebruiken, in te trekken vanuit het ticket;
- mislukte pogingen worden per IP-adres afgeremd; het formulier heeft een CSRF-sleutel;
- het ingevulde bezoek wordt eerst door ons gecontroleerd voordat de werkbon naar de klant gaat.
"""
import hashlib

from flask import Blueprint, render_template, request, abort, g

from .db import query, execute, get_db
from .util import (now_iso, local, now_utc, to_float, save_uploads, save_signature, check_csrf, rate_limited, audit,
                   fmt_date)

bp = Blueprint("extern", __name__, url_prefix="/extern")


def _plan(token):
    if not token or len(token) > 100:
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    today = local(now_utc()).strftime("%Y-%m-%d")
    return query(
        "SELECT p.*, t.number, t.title, t.description, t.status AS ticket_status, t.assigned_to, c.name AS customer,"
        " TRIM(COALESCE(NULLIF(TRIM(IFNULL(l.address,'') || ' ' || IFNULL(l.postcode,'') || ' ' || IFNULL(l.city,'')), ''),"
        "  TRIM(IFNULL(c.address,'') || ' ' || IFNULL(c.postcode,'') || ' ' || IFNULL(c.city,'')))) AS visit_address,"
        " i.name AS installation, i.serial, ct.name AS contact, ct.phone AS contact_phone,"
        " xc.name AS ext_name, xc.language AS ext_lang, xp.name AS ext_contact"
        " FROM visit_plans p JOIN tickets t ON t.id = p.ticket_id LEFT JOIN customers c ON c.id = t.customer_id"
        " LEFT JOIN locations l ON l.id = t.location_id LEFT JOIN installations i ON i.id = t.installation_id"
        " LEFT JOIN contacts ct ON ct.id = t.contact_id LEFT JOIN customers xc ON xc.id = p.ext_customer_id"
        " LEFT JOIN contacts xp ON xp.id = p.ext_contact_id"
        " WHERE p.ext_token_hash = ? AND p.visit_id IS NULL AND p.ext_token_until >= ?", (h, today), one=True)


@bp.route("/<token>", methods=["GET", "POST"])
def visit(token):
    from .talen import tr, ext_tr, long_date, LANG_LABELS
    if rate_limited("extern", 40, 900, hit=False):
        abort(429)
    p = _plan(token)
    if not p:
        rate_limited("extern", 40, 900)   # alleen mislukte pogingen tellen
        lang = request.accept_languages.best_match(list(LANG_LABELS)) or "nl"
        return render_template("extern/visit.html", bad=True, X=ext_tr(lang), L=tr(lang), lang=lang), 404
    lang = p["ext_lang"] if p["ext_lang"] in LANG_LABELS else "nl"
    X, L = ext_tr(lang), tr(lang)
    ctx = dict(p=p, X=X, L=L, lang=lang, until=long_date(p["ext_token_until"], lang), date_long=long_date(p["date"], lang))
    if request.method == "POST":
        check_csrf()
        f = request.form
        findings, work = (f.get("findings") or "").strip()[:8000], (f.get("work_done") or "").strip()[:8000]
        sig, sname = f.get("signature") or "", (f.get("signed_name") or "").strip()[:120]
        no_sign = bool(f.get("no_sign"))
        if not (findings or work) or not ((sig.startswith("data:image/png;base64,") and sname) or no_sign):
            return render_template("extern/visit.html", error=X["need"], form=f, **ctx), 400
        now = now_iso()
        today = local(now_utc()).strftime("%Y-%m-%d")
        who = " ".join(x for x in (p["ext_contact"] or "", f"({p['ext_name']})" if p["ext_name"] else "") if x) or "Externe monteur"
        vid = execute("INSERT INTO visits (date, technicians, hours, travel_hours, km, findings, work_done, materials, ticket_id,"
                      " created_by, created_at, review_pending) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                      (today, who, to_float(f.get("hours"), 0), to_float(f.get("travel_hours"), 0), to_float(f.get("km"), 0),
                       findings or None, work or None, (f.get("materials") or "").strip()[:4000] or None, p["ticket_id"], None, now))
        save_uploads("visit", vid)
        if no_sign:
            execute("UPDATE visits SET closed_at = ?, closed_note = ? WHERE id = ?",
                    (now, ("Externe monteur: " + (f.get("reason") or "").strip())[:300], vid))
        else:
            try:
                save_signature("visit", vid, sig)
                execute("UPDATE visits SET signed_name = ?, signed_at = ? WHERE id = ?", (sname, now, vid))
            except Exception:  # noqa: BLE001  onleesbare handtekening: als niet getekend vastleggen
                execute("UPDATE visits SET closed_at = ?, closed_note = ? WHERE id = ?", (now, "Handtekening onleesbaar", vid))
        # link is hiermee gebruikt
        execute("UPDATE visit_plans SET visit_id = ?, ext_done_at = ?, ext_token_hash = NULL WHERE id = ?", (vid, now, p["id"]))
        if p["ticket_status"] in ("nieuw", "ingepland"):
            execute("UPDATE tickets SET status = 'in_uitvoering' WHERE id = ?", (p["ticket_id"],))
        execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, p["ticket_id"]))
        execute("INSERT INTO ticket_notes (ticket_id, kind, body, created_at) VALUES (?,?,?,?)",
                (p["ticket_id"], "intern", f"Bezoek ingevuld door externe monteur {who}"
                 + (" (klant heeft getekend)." if not no_sign else " (klant heeft niet getekend).") + " Nog controleren en de werkbon versturen.", now))
        audit("bezoek ingevuld door externe monteur", "ticket", p["ticket_id"], who)
        try:
            from .notify import notify_user
            for uid in {x for x in (p["created_by"], p["assigned_to"]) if x}:
                notify_user(get_db(), uid, f"Bezoek ingevuld: ticket {p['number']}",
                            f"{who} heeft het bezoek van {fmt_date(today)} ingevuld bij {p['customer'] or 'de klant'}. Controleer het en verstuur de werkbon.",
                            url=f"/service/{p['ticket_id']}#bezoek{vid}", email=True)
        except Exception as exc:  # pragma: no cover
            print(f"[WorkPortal] Melding extern bezoek mislukt: {exc}", flush=True)
        return render_template("extern/visit.html", done=True, **ctx)
    return render_template("extern/visit.html", form={}, **ctx)
