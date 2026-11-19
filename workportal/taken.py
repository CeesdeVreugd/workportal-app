"""Taken per persoon: voor jezelf (bijv. 'niet vergeten voor het wegrijden') of voor een collega, met herinnering
(melding op telefoon/pc en eventueel mail) en de mogelijkheid een taak door te geven aan iemand anders.

Iedere ingelogde gebruiker mag dit gebruiken (geen modulerecht nodig). Een taak is alleen zichtbaar voor de maker,
de huidige uitvoerder en wie de taak eerder heeft gehad of doorgegeven."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from .db import query, execute, get_db
from .util import now_iso, now_utc, to_int, TZ, local, audit

bp = Blueprint("taken", __name__, url_prefix="/taken")

REMIND_OPTS = [("", "Geen herinnering"), ("0", "Op het tijdstip"), ("15", "15 minuten van tevoren"),
               ("60", "1 uur van tevoren"), ("1440", "1 dag van tevoren"), ("eigen", "Ander moment…")]
DEFAULT_TIME = "08:00"   # herinnering bij een taak zonder tijd
DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


# ---------------------------------------------------------------- hulpfuncties

def _today():
    return now_utc().astimezone(TZ).date()


def _utc_iso(dt_local):
    return dt_local.replace(tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(v):
    try:
        return datetime.strptime((v or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(v):
    v = (v or "").strip()[:5]
    try:
        datetime.strptime(v, "%H:%M")
        return v
    except ValueError:
        return None


def remind_at_for(due_date, due_time, remind, custom=None):
    """Tijdstip (UTC, ISO) van de herinnering, of None."""
    if remind == "eigen":
        try:
            dt = datetime.strptime((custom or "")[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
        return _utc_iso(dt)
    if remind in ("", None) or not due_date:
        return None
    try:
        mins = int(remind)
    except ValueError:
        return None
    dt = datetime.strptime(f"{due_date} {due_time or DEFAULT_TIME}", "%Y-%m-%d %H:%M") - timedelta(minutes=mins)
    return _utc_iso(dt)


def when_label(due_date, due_time=None):
    d = _parse_date(due_date)
    if not d:
        return ""
    diff = (d - _today()).days
    if diff == 0:
        s = "vandaag"
    elif diff == 1:
        s = "morgen"
    elif diff == -1:
        s = "gisteren"
    elif 1 < diff < 7:
        s = {0: "maandag", 1: "dinsdag", 2: "woensdag", 3: "donderdag", 4: "vrijdag", 5: "zaterdag", 6: "zondag"}[d.weekday()]
    else:
        s = f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if d.year != _today().year else "")
    return s + (f" {due_time}" if due_time else "")


def is_late(t):
    if t["done_at"] or not t["due_date"]:
        return False
    today = _today().isoformat()
    if t["due_date"] < today:
        return True
    if t["due_date"] == today and t["due_time"]:
        return t["due_time"] < now_utc().astimezone(TZ).strftime("%H:%M")
    return False


def _safe_link(url):
    """Alleen een pad binnen WorkPortal (geen externe adressen)."""
    url = (url or "").strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url or any(ord(c) < 32 for c in url):
        return None
    return url[:300]


def _staff():
    return query("SELECT id, name FROM users WHERE active = 1 ORDER BY name")


def _valid_user(uid):
    return uid if uid and query("SELECT 1 FROM users WHERE id = ? AND active = 1", (uid,), one=True) else None


VISIBLE = ("(t.assigned_to = :me OR t.created_by = :me OR EXISTS (SELECT 1 FROM task_log l WHERE l.task_id = t.id"
           " AND (l.user_id = :me OR l.to_user = :me)))")
SELECT = ("SELECT t.*, a.name AS assignee, c.name AS creator, d.name AS done_name FROM tasks t"
          " LEFT JOIN users a ON a.id = t.assigned_to LEFT JOIN users c ON c.id = t.created_by LEFT JOIN users d ON d.id = t.done_by")


def _task(tid):
    return query(SELECT + " WHERE t.id = :id AND " + VISIBLE, {"id": tid, "me": g.user["id"]}, one=True) or abort(404)


def _decorate(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["when"] = when_label(r["due_date"], r["due_time"])
        d["late"] = is_late(r)
        out.append(d)
    return out


def _log(tid, action, to_user=None, note=None):
    execute("INSERT INTO task_log (task_id, user_id, to_user, action, note, created_at) VALUES (?,?,?,?,?,?)",
            (tid, g.user["id"], to_user, action, note, now_iso()))


def _notify(uid, title, body, tid, email):
    if not uid or uid == g.user["id"]:
        return
    try:
        from .notify import notify_user
        notify_user(get_db(), uid, title[:120], body, url=url_for("taken.detail", tid=tid), email=email)
    except Exception as exc:  # pragma: no cover
        print(f"[WorkPortal] Melding taak mislukt: {exc}", flush=True)


def _desc_when(t):
    w = when_label(t["due_date"], t["due_time"])
    return f" (voor {w})" if w else ""


def open_count(uid):
    """Voor het menu: nieuwe taken (nog niet bekeken) en taken die vandaag/eerder moeten."""
    if "_task_count" not in g:
        g._task_count = query("SELECT COUNT(*) c FROM tasks WHERE assigned_to = ? AND done_at IS NULL AND (seen_at IS NULL OR due_date <= ?)",
                              (uid, _today().isoformat()), one=True)["c"]
    return g._task_count


def my_open(uid, limit=8):
    return _decorate(query(SELECT + " WHERE t.assigned_to = ? AND t.done_at IS NULL"
                           " ORDER BY (t.due_date IS NULL), t.due_date, COALESCE(t.due_time, '99'), t.id LIMIT ?", (uid, limit)))


def _group(rows):
    today = _today()
    groups = {"late": [], "vandaag": [], "morgen": [], "week": [], "later": [], "zonder": []}
    for r in rows:
        d = _parse_date(r["due_date"])
        if r["late"]:
            groups["late"].append(r)
        elif not d:
            groups["zonder"].append(r)
        elif d == today:
            groups["vandaag"].append(r)
        elif d == today + timedelta(days=1):
            groups["morgen"].append(r)
        elif d < today + timedelta(days=7):
            groups["week"].append(r)
        else:
            groups["later"].append(r)
    labels = {"late": "Te laat", "vandaag": "Vandaag", "morgen": "Morgen", "week": "Deze week", "later": "Later", "zonder": "Zonder datum"}
    return [(k, labels[k], v) for k, v in groups.items() if v]


def _form_values(f):
    """Gemeenschappelijke velden van nieuw/wijzigen. Geeft (dict, foutmelding)."""
    title = (f.get("title") or "").strip()[:200]
    if not title:
        return None, "Vul in wat er moet gebeuren."
    due_date = _parse_date(f.get("due_date"))
    due_date = due_date.isoformat() if due_date else None
    due_time = _parse_time(f.get("due_time")) if due_date else None
    remind = f.get("remind") or ""
    if remind not in dict(REMIND_OPTS):
        remind = ""
    custom = f.get("remind_custom") or ""
    if remind not in ("", "eigen") and not due_date:
        return None, "Kies een datum voor de herinnering, of kies 'Ander moment'."
    r_at = remind_at_for(due_date, due_time, remind, custom)
    if remind == "eigen" and not r_at:
        return None, "Kies het moment van de herinnering."
    return {"title": title, "description": (f.get("description") or "").strip()[:4000] or None,
            "due_date": due_date, "due_time": due_time, "remind": remind or None, "remind_at": r_at,
            "remind_mail": 1 if f.get("remind_mail") and r_at else 0}, None


def _back(default="taken.index", **kw):
    nxt = _safe_link(request.form.get("next"))
    return redirect(nxt or url_for(default, **kw))


# ---------------------------------------------------------------- pagina's

@bp.route("/")
def index():
    me = g.user["id"]
    view = request.args.get("v", "open")
    if view not in ("open", "uitgezet", "klaar", "vast"):
        view = "open"
    if view == "open":
        rows = _decorate(query(SELECT + " WHERE t.assigned_to = ? AND t.done_at IS NULL"
                               " ORDER BY (t.due_date IS NULL), t.due_date, COALESCE(t.due_time, '99'), t.id", (me,)))
        groups = _group(rows)
    elif view == "uitgezet":
        rows = _decorate(query(SELECT + " WHERE t.done_at IS NULL AND IFNULL(t.assigned_to, 0) <> :me AND (t.created_by = :me OR EXISTS"
                               " (SELECT 1 FROM task_log l WHERE l.task_id = t.id AND l.user_id = :me AND l.action = 'doorgegeven'))"
                               " ORDER BY (t.due_date IS NULL), t.due_date, COALESCE(t.due_time, '99'), t.id", {"me": me}))
        groups = [("uitgezet", "", rows)] if rows else []
    elif view == "vast":
        groups = []
    else:
        rows = _decorate(query(SELECT + " WHERE t.done_at IS NOT NULL AND " + VISIBLE + " ORDER BY t.done_at DESC LIMIT 100", {"me": me}))
        groups = [("klaar", "", rows)] if rows else []
    counts = {
        "open": query("SELECT COUNT(*) c FROM tasks WHERE assigned_to = ? AND done_at IS NULL", (me,), one=True)["c"],
        "uitgezet": query("SELECT COUNT(*) c FROM tasks t WHERE t.done_at IS NULL AND IFNULL(t.assigned_to, 0) <> :me AND (t.created_by = :me"
                          " OR EXISTS (SELECT 1 FROM task_log l WHERE l.task_id = t.id AND l.user_id = :me AND l.action = 'doorgegeven'))",
                          {"me": me}, one=True)["c"],
    }
    if view == "open":   # nieuwe taken zijn nu gezien
        execute("UPDATE tasks SET seen_at = ? WHERE assigned_to = ? AND seen_at IS NULL AND done_at IS NULL", (now_iso(), me))
    link = _safe_link(request.args.get("link"))
    prefill = {"link_url": link, "link_label": (request.args.get("label") or "")[:120] if link else None,
               "title": (request.args.get("titel") or "")[:200]}
    series = my_series(me) if view == "vast" else []
    counts["vast"] = query("SELECT COUNT(*) c FROM task_series WHERE active = 1 AND (assigned_to = ? OR created_by = ?)", (me, me), one=True)["c"]
    return render_template("taken/index.html", view=view, groups=groups, counts=counts, staff=_staff(), series=series,
                           FREQS=FREQS, WEEKDAY_NAMES=WEEKDAY_NAMES,
                           REMIND_OPTS=REMIND_OPTS, prefill=prefill, show_new=bool(link or request.args.get("nieuw")))


@bp.route("/nieuw", methods=["POST"])
def new():
    f = request.form
    if f.get("repeat") and not _parse_date(f.get("due_date")):
        f = f.copy()
        f["due_date"] = _today().isoformat()   # herhalen zonder datum: vanaf vandaag
    vals, err = _form_values(f)
    if err:
        flash(err, "error")
        return _back()
    if f.get("repeat"):
        sv, err = _series_values(f)
        if err:
            flash(err, "error")
            return _back()
        s = create_series(f, vals, sv)
        flash(f"Terugkerende taak gemaakt: {series_label(s).lower()}" + (f", voor {s['assignee']}" if s["assigned_to"] != g.user["id"] else "")
              + (f". Volgende keer: {when_label(s['next_date'])}." if s["next_date"] else "."), "ok")
        return redirect(url_for("taken.index", v="vast"))
    uid = _valid_user(to_int(f.get("assigned_to"))) or g.user["id"]
    link = _safe_link(f.get("link_url"))
    now = now_iso()
    tid = execute("INSERT INTO tasks (title, description, assigned_to, created_by, due_date, due_time, remind, remind_at, remind_mail,"
                  " seen_at, link_url, link_label, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vals["title"], vals["description"], uid, g.user["id"], vals["due_date"], vals["due_time"], vals["remind"],
                   vals["remind_at"], vals["remind_mail"], now if uid == g.user["id"] else None,
                   link, ((f.get("link_label") or "")[:120] or None) if link else None, now))
    _log(tid, "aangemaakt", to_user=uid if uid != g.user["id"] else None)
    if uid != g.user["id"]:
        t = _task(tid)
        _notify(uid, f"Taak van {g.user['name']}: {vals['title']}",
                f"{g.user['name']} heeft een taak voor je gemaakt{_desc_when(t)}: {vals['title']}"
                + (f"\n\n{vals['description']}" if vals["description"] else ""), tid, email=True)
        audit("taak gegeven", "task", tid, vals["title"][:120])
        flash(f"Taak gegeven aan {t['assignee']}. Die krijgt een melding.", "ok")
    else:
        flash("Taak toegevoegd" + (" met herinnering." if vals["remind_at"] else "."), "ok")
    return _back()


@bp.route("/<int:tid>")
def detail(tid):
    t = _task(tid)
    if t["assigned_to"] == g.user["id"] and not t["seen_at"]:
        execute("UPDATE tasks SET seen_at = ? WHERE id = ?", (now_iso(), tid))
    log = query("SELECT l.*, u.name AS who, v.name AS to_name FROM task_log l LEFT JOIN users u ON u.id = l.user_id"
                " LEFT JOIN users v ON v.id = l.to_user WHERE l.task_id = ? ORDER BY l.id DESC", (tid,))
    custom = local(t["remind_at"]).strftime("%Y-%m-%dT%H:%M") \
        if t["remind_at"] else ""
    return render_template("taken/detail.html", t=_decorate([t])[0], log=log, staff=_staff(), REMIND_OPTS=REMIND_OPTS,
                           remind_custom=custom, can_delete=_can_delete(t))


def _can_edit(t):
    return g.user["id"] in (t["assigned_to"], t["created_by"])


def _can_delete(t):
    return t["created_by"] == g.user["id"] or (t["created_by"] is None and t["assigned_to"] == g.user["id"])


@bp.route("/<int:tid>/wijzigen", methods=["POST"])
def edit(tid):
    t = _task(tid)
    if not _can_edit(t):
        abort(403)
    vals, err = _form_values(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("taken.detail", tid=tid))
    reset = vals["remind_at"] != t["remind_at"]
    execute("UPDATE tasks SET title = ?, description = ?, due_date = ?, due_time = ?, remind = ?, remind_at = ?, remind_mail = ?,"
            " reminded_at = CASE WHEN ? THEN NULL ELSE reminded_at END, updated_at = ? WHERE id = ?",
            (vals["title"], vals["description"], vals["due_date"], vals["due_time"], vals["remind"], vals["remind_at"],
             vals["remind_mail"], 1 if reset else 0, now_iso(), tid))
    _log(tid, "gewijzigd")
    flash("Taak opgeslagen.", "ok")
    return redirect(url_for("taken.detail", tid=tid))


@bp.route("/<int:tid>/klaar", methods=["POST"])
def done(tid):
    t = _task(tid)
    if not _can_edit(t):
        abort(403)
    if not t["done_at"]:
        execute("UPDATE tasks SET done_at = ?, done_by = ?, updated_at = ? WHERE id = ?", (now_iso(), g.user["id"], now_iso(), tid))
        _log(tid, "afgerond")
        if t["created_by"] and t["created_by"] != g.user["id"]:
            _notify(t["created_by"], f"Taak klaar: {t['title']}", f"{g.user['name']} heeft de taak afgerond: {t['title']}", tid, email=False)
        flash(f"'{t['title']}' afgerond.", "ok")
    return _back()


@bp.route("/<int:tid>/heropenen", methods=["POST"])
def reopen(tid):
    t = _task(tid)
    if not _can_edit(t):
        abort(403)
    execute("UPDATE tasks SET done_at = NULL, done_by = NULL, updated_at = ? WHERE id = ?", (now_iso(), tid))
    _log(tid, "weer open")
    flash("Taak staat weer open.", "ok")
    return _back("taken.detail", tid=tid)


@bp.route("/<int:tid>/doorgeven", methods=["POST"])
def handover(tid):
    t = _task(tid)
    if not _can_edit(t):
        abort(403)
    uid = _valid_user(to_int(request.form.get("to")))
    if not uid or uid == t["assigned_to"]:
        flash("Kies een andere collega om de taak aan door te geven.", "error")
        return redirect(url_for("taken.detail", tid=tid))
    note = (request.form.get("note") or "").strip()[:2000] or None
    execute("UPDATE tasks SET assigned_to = ?, seen_at = ?, done_at = NULL, done_by = NULL, updated_at = ? WHERE id = ?",
            (uid, now_iso() if uid == g.user["id"] else None, now_iso(), tid))
    _log(tid, "doorgegeven", to_user=uid, note=note)
    t2 = _task(tid)
    _notify(uid, f"Taak van {g.user['name']}: {t['title']}",
            f"{g.user['name']} heeft een taak aan je doorgegeven{_desc_when(t)}: {t['title']}" + (f"\n\nNotitie: {note}" if note else ""),
            tid, email=True)
    if t["created_by"] and t["created_by"] not in (g.user["id"], uid):
        _notify(t["created_by"], f"Taak doorgegeven: {t['title']}", f"{g.user['name']} heeft je taak doorgegeven aan {t2['assignee']}.",
                tid, email=False)
    audit("taak doorgegeven", "task", tid, f"{t['title'][:80]} → {t2['assignee']}")
    flash(f"Doorgegeven aan {t2['assignee']}.", "ok")
    if uid == g.user["id"]:
        return redirect(url_for("taken.detail", tid=tid))
    return redirect(url_for("taken.index"))


@bp.route("/<int:tid>/later", methods=["POST"])
def snooze(tid):
    """Herinnering uitstellen: over 1 uur of morgen om 08:00."""
    t = _task(tid)
    if not _can_edit(t):
        abort(403)
    if request.form.get("when") == "morgen":
        dt = datetime.combine(_today() + timedelta(days=1), datetime.strptime(DEFAULT_TIME, "%H:%M").time())
        r_at, label = _utc_iso(dt), "morgen 08:00"
    else:
        r_at, label = (now_utc() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "over een uur"
    execute("UPDATE tasks SET remind = 'eigen', remind_at = ?, reminded_at = NULL, updated_at = ? WHERE id = ?", (r_at, now_iso(), tid))
    _log(tid, "herinnering " + label)
    flash(f"Je krijgt {label} opnieuw een herinnering.", "ok")
    return _back("taken.detail", tid=tid)


@bp.route("/<int:tid>/verwijderen", methods=["POST"])
def delete(tid):
    t = _task(tid)
    if not _can_delete(t):
        abort(403)
    execute("DELETE FROM tasks WHERE id = ?", (tid,))
    if t["assigned_to"] and t["assigned_to"] != g.user["id"] and not t["done_at"]:
        _notify(t["assigned_to"], f"Taak vervallen: {t['title']}", f"{g.user['name']} heeft de taak verwijderd: {t['title']}", tid, email=False)
    flash("Taak verwijderd.", "ok")
    return redirect(url_for("taken.index"))


# ---------------------------------------------------------------- herinneringen (planner)

def scheduled_reminders(conn):
    """Elke 30 s vanuit de planner: herinneringen die aan de beurt zijn versturen (push, eventueel mail)."""
    from .notify import notify_user
    now = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT * FROM tasks WHERE remind_at IS NOT NULL AND remind_at <= ? AND reminded_at IS NULL"
                        " AND done_at IS NULL AND assigned_to IS NOT NULL LIMIT 50", (now,)).fetchall()
    for t in rows:
        conn.execute("UPDATE tasks SET reminded_at = ? WHERE id = ?", (now, t["id"]))
        conn.commit()
        w = when_label(t["due_date"], t["due_time"])
        body = (f"{t['title']}" + (f" – {w}" if w else "") + (f"\n\n{t['description']}" if t["description"] else ""))
        try:
            notify_user(conn, t["assigned_to"], f"Herinnering: {t['title']}"[:120], body, url=f"/taken/{t['id']}",
                        email=bool(t["remind_mail"]))
        except Exception as exc:  # pragma: no cover
            print(f"[WorkPortal] Herinnering taak {t['id']} mislukt: {exc}", flush=True)


# ---------------------------------------------------------------- terugkerende taken
# Een vaste taak (bijv. 'elke maandag de Excel uit SnelStart importeren') maakt op de dag zelf een gewone taak aan
# voor de uitvoerder, met herinnering. Afvinken gaat per keer; de reeks loopt door tot hij gepauzeerd of verwijderd wordt.

FREQS = [("dag", "Elke dag"), ("werkdag", "Elke werkdag (ma–vr)"), ("week", "Elke week"), ("maand", "Elke maand")]
WEEKDAY_NAMES = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
SERIES_REMIND = [o for o in REMIND_OPTS if o[0] != "eigen"]


def _add_months(d, n, day):
    import calendar
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return d.replace(year=y, month=m, day=last if day == -1 or day > last else day)


def _wdays(s):
    return sorted({int(x) for x in (s["weekdays"] or "").split(",") if x.strip().isdigit() and 0 <= int(x) <= 6}) or [0]


def occurs_on(s, d):
    start = _parse_date(s["start_date"])
    if not start or d < start:
        return False
    every = max(1, int(s["every"] or 1))
    if s["freq"] == "dag":
        return (d - start).days % every == 0
    if s["freq"] == "werkdag":
        return d.weekday() < 5
    if s["freq"] == "week":
        if d.weekday() not in _wdays(s):
            return False
        w0 = start - timedelta(days=start.weekday())
        return ((d - timedelta(days=d.weekday())) - w0).days // 7 % every == 0
    if s["freq"] == "maand":
        import calendar
        md = int(s["monthday"] or start.day)
        last = calendar.monthrange(d.year, d.month)[1]
        want = last if md == -1 or md > last else md
        return d.day == want and ((d.year - start.year) * 12 + d.month - start.month) % every == 0
    return False


def next_occurrence(s, after):
    """Eerste datum ná 'after' (date) waarop de reeks een taak geeft."""
    d = after + timedelta(days=1)
    for _ in range(800):
        if occurs_on(s, d):
            return d
        d += timedelta(days=1)
    return None


def series_label(s):
    every = max(1, int(s["every"] or 1))
    t = f" om {s['due_time']}" if s["due_time"] else ""
    if s["freq"] == "dag":
        base = "Elke dag" if every == 1 else f"Elke {every} dagen"
    elif s["freq"] == "werkdag":
        base = "Elke werkdag"
    elif s["freq"] == "week":
        days = " en ".join(", ".join(WEEKDAY_NAMES[i] for i in _wdays(s)).rsplit(", ", 1))
        base = (f"Elke {days}" if every == 1 else f"Om de {every} weken op {days}")
    else:
        md = int(s["monthday"] or 1)
        dag = "de laatste dag" if md == -1 else f"de {md}e"
        base = (f"Elke maand op {dag}" if every == 1 else f"Om de {every} maanden op {dag}")
    return base + t


def _series_values(f):
    freq = f.get("freq") if f.get("freq") in dict(FREQS) else "week"
    every = max(1, min(52, to_int(f.get("every")) or 1))
    wd = sorted({int(x) for x in f.getlist("weekdays") if x.isdigit() and 0 <= int(x) <= 6})
    if freq == "week" and not wd:
        return None, "Kies op welke dag(en) van de week."
    md = to_int(f.get("monthday"))
    if freq == "maand" and not (md == -1 or (md and 1 <= md <= 31)):
        return None, "Kies de dag van de maand."
    start = _parse_date(f.get("due_date")) or _today()
    remind = f.get("remind") or ""
    if remind not in dict(SERIES_REMIND):
        remind = ""
    return {"freq": freq, "every": every, "weekdays": ",".join(map(str, wd)) or None, "monthday": md if freq == "maand" else None,
            "start_date": start.isoformat(), "due_time": _parse_time(f.get("due_time")), "remind": remind or None,
            "remind_mail": 1 if f.get("remind_mail") and remind else 0}, None


def _first_date(s):
    """Eerste keer vanaf de startdatum, maar niet in het verleden (geen achterstallige taak bij het aanmaken)."""
    start = max(_parse_date(s["start_date"]) or _today(), _today())
    return start if occurs_on(s, start) else next_occurrence(s, start)


def generate_series(conn, today=None):
    """Planner: voor elke actieve reeks die aan de beurt is de taak van vandaag aanmaken (gemiste dagen: alleen de laatste)."""
    today = today or _today()
    made = 0
    for s in conn.execute("SELECT * FROM task_series WHERE active = 1 AND next_date IS NOT NULL AND next_date <= ?", (today.isoformat(),)).fetchall():
        d = _parse_date(s["next_date"])
        last = d
        while d and d <= today:   # bij een gemiste dag (server uit) alleen de meest recente aanmaken
            last, d = d, next_occurrence(s, d)
        due = last.isoformat()
        if not conn.execute("SELECT 1 FROM tasks WHERE series_id = ? AND due_date = ?", (s["id"], due)).fetchone():
            now = now_iso()
            r_at = remind_at_for(due, s["due_time"], s["remind"] or "")
            if r_at and r_at < now:
                r_at = None if s["remind"] is None else now   # herinnering al voorbij: meteen
            cur = conn.execute("INSERT INTO tasks (title, description, assigned_to, created_by, due_date, due_time, remind, remind_at, remind_mail,"
                               " link_url, link_label, series_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (s["title"], s["description"], s["assigned_to"], s["created_by"], due, s["due_time"], s["remind"], r_at,
                                s["remind_mail"], s["link_url"], s["link_label"], s["id"], now))
            conn.execute("INSERT INTO task_log (task_id, user_id, to_user, action, note, created_at) VALUES (?,?,?,?,?,?)",
                         (cur.lastrowid, None, s["assigned_to"], "aangemaakt", "Terugkerende taak: " + series_label(s), now))
            made += 1
        conn.execute("UPDATE task_series SET next_date = ? WHERE id = ?", (d.isoformat() if d else None, s["id"]))
        conn.commit()
    return made


def _series(sid):
    s = query("SELECT s.*, a.name AS assignee, c.name AS creator FROM task_series s LEFT JOIN users a ON a.id = s.assigned_to"
              " LEFT JOIN users c ON c.id = s.created_by WHERE s.id = ?", (sid,), one=True) or abort(404)
    if g.user["id"] not in (s["assigned_to"], s["created_by"]):
        abort(404)
    return s


def my_series(uid):
    rows = query("SELECT s.*, a.name AS assignee, c.name AS creator FROM task_series s LEFT JOIN users a ON a.id = s.assigned_to"
                 " LEFT JOIN users c ON c.id = s.created_by WHERE s.assigned_to = ? OR s.created_by = ? ORDER BY s.active DESC, s.title", (uid, uid))
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = series_label(r)
        d["next"] = when_label(r["next_date"]) if r["next_date"] and r["active"] else None
        out.append(d)
    return out


def create_series(f, vals, sv):
    uid = _valid_user(to_int(f.get("assigned_to"))) or g.user["id"]
    link = _safe_link(f.get("link_url"))
    now = now_iso()
    sid = execute("INSERT INTO task_series (title, description, assigned_to, created_by, freq, every, weekdays, monthday, due_time, remind,"
                  " remind_mail, link_url, link_label, start_date, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vals["title"], vals["description"], uid, g.user["id"], sv["freq"], sv["every"], sv["weekdays"], sv["monthday"],
                   sv["due_time"], sv["remind"], sv["remind_mail"], link, ((f.get("link_label") or "")[:120] or None) if link else None,
                   sv["start_date"], now))
    s = query("SELECT * FROM task_series WHERE id = ?", (sid,), one=True)
    first = _first_date(s)
    execute("UPDATE task_series SET next_date = ? WHERE id = ?", (first.isoformat() if first else None, sid))
    generate_series(get_db())
    s = query("SELECT s.*, a.name AS assignee FROM task_series s LEFT JOIN users a ON a.id = s.assigned_to WHERE s.id = ?", (sid,), one=True)
    audit("terugkerende taak", "task_series", sid, f"{vals['title'][:80]} – {series_label(s)}")
    if uid != g.user["id"]:
        try:
            from .notify import notify_user
            notify_user(get_db(), uid, f"Vaste taak van {g.user['name']}: {vals['title']}"[:120],
                        f"{g.user['name']} heeft een terugkerende taak voor je gemaakt: {vals['title']} ({series_label(s).lower()}).",
                        url=url_for("taken.index", v="vast"), email=True)
        except Exception as exc:  # pragma: no cover
            print(f"[WorkPortal] Melding vaste taak mislukt: {exc}", flush=True)
    return s


@bp.route("/vast/<int:sid>", methods=["GET", "POST"])
def series_edit(sid):
    s = _series(sid)
    if request.method == "POST":
        f = request.form
        act = f.get("action")
        if act == "pauze":
            execute("UPDATE task_series SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), sid))
            flash("Gepauzeerd: er worden geen nieuwe taken meer aangemaakt.", "ok")
        elif act == "hervat":
            nxt = next_occurrence(s, _today() - timedelta(days=1))
            execute("UPDATE task_series SET active = 1, next_date = ?, updated_at = ? WHERE id = ?", (nxt.isoformat() if nxt else None, now_iso(), sid))
            generate_series(get_db())
            flash("Hervat.", "ok")
        elif act == "verwijderen":
            if s["created_by"] != g.user["id"] and s["created_by"] is not None:
                abort(403)
            execute("DELETE FROM task_series WHERE id = ?", (sid,))
            audit("terugkerende taak verwijderd", "task_series", sid, s["title"][:120])
            flash("Terugkerende taak verwijderd. Al aangemaakte taken blijven staan.", "ok")
            return redirect(url_for("taken.index", v="vast"))
        else:
            vals, err = _form_values(f)
            sv, err2 = _series_values(f) if not err else (None, None)
            if err or err2:
                flash(err or err2, "error")
                return redirect(url_for("taken.series_edit", sid=sid))
            uid = _valid_user(to_int(f.get("assigned_to"))) or s["assigned_to"]
            execute("UPDATE task_series SET title = ?, description = ?, assigned_to = ?, freq = ?, every = ?, weekdays = ?, monthday = ?,"
                    " due_time = ?, remind = ?, remind_mail = ?, start_date = ?, updated_at = ? WHERE id = ?",
                    (vals["title"], vals["description"], uid, sv["freq"], sv["every"], sv["weekdays"], sv["monthday"], sv["due_time"],
                     sv["remind"], sv["remind_mail"], sv["start_date"], now_iso(), sid))
            s2 = query("SELECT * FROM task_series WHERE id = ?", (sid,), one=True)
            # volgende keer opnieuw bepalen (vandaag telt mee als er vandaag nog geen taak is)
            today = _today()
            has_today = query("SELECT 1 FROM tasks WHERE series_id = ? AND due_date = ?", (sid, today.isoformat()), one=True)
            nxt = next_occurrence(s2, today if has_today else today - timedelta(days=1))
            st = _parse_date(s2["start_date"])
            if st and st > today:
                nxt = _first_date(s2)
            elif not has_today and occurs_on(s2, today):
                nxt = today
            execute("UPDATE task_series SET next_date = ? WHERE id = ?", (nxt.isoformat() if nxt else None, sid))
            if s2["active"]:
                generate_series(get_db())
            flash("Terugkerende taak opgeslagen. Wijzigingen gelden voor de volgende keren.", "ok")
        return redirect(url_for("taken.series_edit", sid=sid))
    recent = _decorate(query(SELECT + " WHERE t.series_id = ? ORDER BY t.due_date DESC LIMIT 10", (sid,)))
    d = dict(s)
    d["label"] = series_label(s)
    d["next"] = when_label(s["next_date"]) if s["next_date"] and s["active"] else None
    return render_template("taken/vast.html", s=d, recent=recent, staff=_staff(), REMIND_OPTS=SERIES_REMIND, FREQS=FREQS,
                           WEEKDAY_NAMES=WEEKDAY_NAMES, wdays=_wdays(s), can_delete=s["created_by"] in (g.user["id"], None))
