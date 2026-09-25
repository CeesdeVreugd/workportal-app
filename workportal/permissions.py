from functools import wraps
from flask import g, abort, redirect, url_for, request

from .db import query

GEEN, LEZEN, BEWERKEN, BEHEER = 0, 1, 2, 3
LEVEL_NAMES = {0: "Geen", 1: "Lezen", 2: "Bewerken", 3: "Beheer"}

MODULES = [
    ("klanten", "Relaties & projecten"),
    ("service", "Service & onderhoud"),
    ("druktest", "Druktestregistratie"),
    ("calculatie", "Calculatie"),
    ("nacalculatie", "Na-calculatie"),
    ("kennis", "Kenniscentrum (gebruiken)"),
    ("kennis_schrijven", "Kenniscentrum (artikelen schrijven)"),
    ("beheer", "Beheer (gebruikers & rechten)"),
]
MODULE_KEYS = [m for m, _ in MODULES]

# Modules gegroepeerd onder koppen (menu en rechtenpagina)
MODULE_GROUPS = [
    ("Relaties", ["klanten"]),
    ("Uitvoering", ["service", "druktest"]),
    ("Calculatie & financiën", ["calculatie", "nacalculatie"]),
    ("Kennis", ["kennis", "kennis_schrijven"]),
    ("Systeem", ["beheer"]),
]

ROLES = [
    ("administratie", "Administratie"),
    ("directie", "Directie"),
    ("verkoop", "Verkoop-Inkoop-WVB"),
    ("engineering", "Engineering"),
    ("werkplaats", "Werkplaats-Service"),
]

# Standaardinstelling uit de opzet (aanpasbaar in Beheer)
DEFAULT_MATRIX = {
    "klanten":          {"administratie": 3, "directie": 3, "verkoop": 2, "engineering": 1, "werkplaats": 1},
    "service":          {"administratie": 1, "directie": 1, "verkoop": 1, "engineering": 2, "werkplaats": 2},
    "druktest":         {"administratie": 1, "directie": 1, "verkoop": 1, "engineering": 2, "werkplaats": 2},
    "calculatie":       {"administratie": 1, "directie": 3, "verkoop": 2, "engineering": 2, "werkplaats": 0},
    "nacalculatie":     {"administratie": 1, "directie": 3, "verkoop": 1, "engineering": 1, "werkplaats": 0},
    "kennis":           {"administratie": 1, "directie": 1, "verkoop": 1, "engineering": 1, "werkplaats": 1},
    "kennis_schrijven": {"administratie": 0, "directie": 2, "verkoop": 0, "engineering": 3, "werkplaats": 2},
    "beheer":           {"administratie": 3, "directie": 3, "verkoop": 0, "engineering": 0, "werkplaats": 0},
}


def load_permissions(user):
    perms = {m: 0 for m in MODULE_KEYS}
    if not user:
        return perms
    if user["is_admin"]:
        return {m: BEHEER for m in MODULE_KEYS}
    # meerdere functierollen: per module geldt het hoogste niveau
    for r in query("SELECT rp.module, MAX(rp.level) AS level FROM role_permissions rp"
                   " JOIN user_roles ur ON ur.role_id = rp.role_id WHERE ur.user_id = ? GROUP BY rp.module", (user["id"],)):
        perms[r["module"]] = r["level"]
    for r in query("SELECT module, level FROM user_permissions WHERE user_id = ?", (user["id"],)):
        perms[r["module"]] = max(perms.get(r["module"], 0), r["level"])
    return perms


def can(module, level=LEZEN):
    perms = getattr(g, "perms", None) or {}
    return perms.get(module, 0) >= level


def require(module, level=LEZEN):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not getattr(g, "user", None):
                return redirect(url_for("auth.login", next=request.path))
            if not can(module, level):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco
