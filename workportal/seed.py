"""Basisgegevens: rollen, rechten, eerste beheerder en calculatiesjablonen.
Alles is idempotent: bestaande gegevens worden niet overschreven."""
import json
import os

from .db import raw_connection
from .permissions import ROLES, DEFAULT_MATRIX
from .util import now_iso

# Categorieën = de vaste items in de ERP-order (positienummers)
CATEGORIES = [
    ("engineering", "Engineering / Tekenwerk", 100),
    ("inkoop", "Alle inkopen", 200),
    ("werkplaats", "Manuren in de werkplaats", 300),
    ("besturing", "Besturing en bekabeling", 400),
    ("transport", "Transporten", 500),
    ("montage", "Montage op locatie", 600),
]
CATEGORY_TITLES = {k: t for k, t, _ in CATEGORIES}
CATEGORY_POS = {k: p for k, _, p in CATEGORIES}

WORKTYPES = [
    ("procestechniek", "Procestechniek"),
    ("machinebouw", "Machinebouw"),
    ("speciaal", "Speciaal machinebouw"),
    ("engineering", "Engineering"),
]


def L(desc, unit="uur", price=0.0, factor=1.0):
    return {"d": desc, "u": unit, "p": price, "f": factor}


_ENGINEERING = [
    L("Inmeten t.p. en opzet maken", "uur", 85), L("Uitwerken naar compleet concept", "uur", 85),
    L("Uitwerken las samenstellingen", "uur", 85), L("Uitwerken montage samenstellingen", "uur", 85),
    L("Uitwerken koopdelen", "uur", 85), L("Bespreking en opname bij de klant", "uur", 85),
    L("Bespreking leveranciers", "uur", 85), L("Aftersales / service etc.", "uur", 85),
    L("Onvoorzien", "uur", 85),
]
_INKOOP = [
    L("Inkoop verspaning", "post", 0, 1.25), L("Inkoop plaatwerk dun", "post", 0, 1.25),
    L("Inkoop plaatwerk dik", "post", 0, 1.25), L("Inkoop buislaser/profielen", "post", 0, 1.25),
    L("Inkoop beitswerk", "post", 0, 1.25),
]
_BESTURING = [
    L("Programmeren voorbereiden", "uur", 100, 1.15), L("Dag op locatie", "uur", 100, 1.15),
    L("Elektromotor", "stuk", 0, 1.15), L("Bedieningskast", "stuk", 0, 1.15), L("Kabel", "post", 0, 1.15),
    L("Kabelgeleiding", "post", 0, 1.15), L("PVC buizen + zadels", "post", 0, 1.15),
    L("Bedieningsscherm", "stuk", 0, 1.15), L("Installeren bij klant", "uur", 100, 1.15),
    L("Schema tekenen", "uur", 100, 1.15), L("Programmeren", "uur", 100, 1.15),
]
_TRANSPORT = [
    L("Plaatwerker -> De Vreugd Productietechniek", "rit", 40), L("Verspaner -> De Vreugd Productietechniek", "rit", 40),
    L("Plaatwerker -> Verspaner", "rit", 40), L("Lasser -> De Vreugd Productietechniek", "rit", 40),
    L("Lasser -> Verspaner", "rit", 40), L("De Vreugd Productietechniek -> Beitser", "rit", 40),
    L("Beitser -> De Vreugd Productietechniek", "rit", 40), L("De Vreugd Productietechniek -> Klant", "uur", 65),
    L("Klant -> De Vreugd Productietechniek", "uur", 65), L("Brandstof per rit", "rit", 0),
]

TEMPLATES = {
    "procestechniek": {
        "engineering": _ENGINEERING,
        "inkoop": _INKOOP,
        "werkplaats": [
            L("WVB / inkoop materialen", "uur", 85), L("Zagen buizen", "uur", 70), L("Boren sokken", "uur", 70),
            L("Lassen lang leidingdeel", "uur", 70), L("Lassen kort leidingdeel", "uur", 70),
            L("Aanpassen bestaand leidingdeel", "uur", 70), L("Lassen beugels", "uur", 70),
            L("Druktest in werkplaats", "uur", 70),
        ],
        "besturing": _BESTURING,
        "transport": _TRANSPORT,
        "montage": [
            L("De-montage isolatie en aftappen water", "uur", 70),
            L("De-montage bestaande leiding tegen de wand", "uur", 70),
            L("Montage bestaande en nieuwe leiding", "uur", 70), L("Montage beugels", "uur", 70),
            L("Vullen watersysteem en testen", "uur", 70), L("Voorbereiding en opruimwerk", "uur", 70),
        ],
    },
    "machinebouw": {
        "engineering": _ENGINEERING,
        "inkoop": _INKOOP,
        "werkplaats": [
            L("WVB / inkoop materialen", "uur", 85), L("Zagen", "uur", 70), L("Boren", "uur", 70),
            L("Lassen frame", "uur", 70), L("Lassen onderdelen", "uur", 70),
            L("Mechanische montage", "uur", 70), L("Afwerken / beitsen / polijsten", "uur", 70),
            L("Testen en inregelen", "uur", 70),
        ],
        "besturing": _BESTURING,
        "transport": _TRANSPORT,
        "montage": [
            L("Montage", "uur", 65), L("Inbedrijfstelling", "uur", 65),
            L("Voorbereiding en opruimwerk", "uur", 65),
        ],
    },
    "speciaal": {
        "engineering": _ENGINEERING + [L("Prototype / proefopstelling", "uur", 85), L("Risicobeoordeling / CE", "uur", 85)],
        "inkoop": _INKOOP,
        "werkplaats": [
            L("WVB / inkoop materialen", "uur", 85), L("Zagen", "uur", 70), L("Boren", "uur", 70),
            L("Lassen", "uur", 70), L("Mechanische montage", "uur", 70), L("Testen en inregelen", "uur", 70),
        ],
        "besturing": _BESTURING,
        "transport": _TRANSPORT,
        "montage": [L("Montage", "uur", 65), L("Inbedrijfstelling", "uur", 65), L("Voorbereiding en opruimwerk", "uur", 65)],
    },
    "engineering": {
        "engineering": _ENGINEERING + [L("Documentatie / tekeningenpakket", "uur", 85)],
        "transport": [L("De Vreugd Productietechniek -> Klant", "uur", 65), L("Klant -> De Vreugd Productietechniek", "uur", 65)],
    },
}


def seed(app):
    conn = raw_connection(app.config["DB_PATH"])
    try:
        now = now_iso()
        for i, (key, name) in enumerate(ROLES):
            conn.execute("INSERT OR IGNORE INTO roles (key, name, sort) VALUES (?,?,?)", (key, name, i))
        roles = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM roles")}
        for module, per_role in DEFAULT_MATRIX.items():
            for rkey, level in per_role.items():
                conn.execute("INSERT OR IGNORE INTO role_permissions (role_id, module, level) VALUES (?,?,?)",
                             (roles[rkey], module, level))

        admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip()
        if admin_email:
            exists = conn.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
            if not exists:
                cur = conn.execute(
                    "INSERT INTO users (email, name, role_id, is_admin, active, created_at) VALUES (?,?,?,?,1,?)",
                    (admin_email, os.environ.get("ADMIN_NAME", "Beheerder"), roles["directie"], 1, now))
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (cur.lastrowid, roles["directie"]))
                print(f"[WorkPortal] Beheerder aangemaakt: {admin_email}", flush=True)

        for key, name in WORKTYPES:
            conn.execute("INSERT OR IGNORE INTO calc_templates (worktype, name, data) VALUES (?,?,?)",
                         (key, name, json.dumps(TEMPLATES.get(key, {}), ensure_ascii=False)))

        if not conn.execute("SELECT 1 FROM articles LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO articles (title, category, tags, body, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                ("Werken met het Kenniscentrum", "Algemeen", "uitleg",
                 "In het Kenniscentrum vind je rekenhulpen, beslisbomen en kennisartikelen.\n\n"
                 "Gebruik de zoekbalk bovenaan om snel iets te vinden. Mis je een onderwerp? "
                 "Vraag een collega met schrijfrechten om een artikel toe te voegen.", now, now))
        conn.commit()
    finally:
        conn.close()
