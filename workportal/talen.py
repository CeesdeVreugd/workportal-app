"""Talen voor wat de klant krijgt (werkbon-PDF en de mail erbij). De taal staat bij de relatie (leeg = Nederlands)."""

LANGS = [("nl", "Nederlands"), ("en", "English"), ("de", "Deutsch"), ("fr", "Français")]
LANG_LABELS = dict(LANGS)

_ALIASES = {
    "nl": "nl", "nld": "nl", "dut": "nl", "nederlands": "nl", "dutch": "nl", "nederland": "nl", "vlaams": "nl", "be-nl": "nl",
    "en": "en", "eng": "en", "engels": "en", "english": "en", "uk": "en", "gb": "en", "us": "en",
    "de": "de", "deu": "de", "ger": "de", "duits": "de", "deutsch": "de", "german": "de", "duitsland": "de", "at": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "frans": "fr", "français": "fr", "francais": "fr", "french": "fr", "frankrijk": "fr",
}


def norm_lang(v):
    """'Engels', 'EN', 'English' … -> 'en'; onbekend -> None."""
    k = str(v or "").strip().lower()
    return _ALIASES.get(k) or _ALIASES.get(k[:2]) if k else None


T = {
    "nl": {
        "eyebrow": "WERKBON SERVICE & ONDERHOUD", "doc": "Werkbon",
        "ticket": "Ticket", "type": "Type", "customer": "Klant", "location": "Locatie", "installation": "Installatie",
        "serial": "Serienummer", "date": "Datum bezoek", "techs": "Monteur(s)", "hours": "Gewerkte uren",
        "travel": "Reistijd (uur)", "km": "Kilometers", "project": "Project",
        "report": "Melding", "findings": "Bevindingen", "work": "Uitgevoerde werkzaamheden", "materials": "Gebruikte materialen",
        "photos": "Foto's", "approval": "Voor akkoord klant", "name": "Naam",
        "not_signed": "Niet ondertekend – afgerond op {d}", "amended_signed": "Aangevuld na het aftekenen op {d}",
        "amended_closed": "Aangevuld na het afronden op {d}", "by": " door {n}",
        "types": {"storing": "Storing", "onderhoud": "Service & onderhoud", "garantie": "Garantie", "inspectie": "Inspectie", "overig": "Overig"},
        "subject": "Werkbon {nr} – {title}", "subject_short": "Werkbon {nr} – De Vreugd Productietechniek",
        "greet": "Beste {n},", "body": "In de bijlage vindt u de werkbon van ons bezoek op {d}.",
        "body_short": "In de bijlage vindt u de werkbon van ons bezoek.",
        "unsigned": "De werkbon is niet ter plaatse ondertekend.", "questions": "Heeft u vragen, neem dan gerust contact met ons op.",
        "relation": "relatie", "greet_none": "Beste relatie,",
        "plan_subject": "Bezoek ingepland – ticket {nr}", "plan_body": "Graag bevestigen wij ons bezoek op {d}{who}.", "plan_who": " door {n}",
        "plan_q": "Komt dit niet goed uit? Laat het ons dan weten door op deze mail te antwoorden.",
        "days": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"],
        "months": ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"], "file": "Werkbon",
    },
    "en": {
        "eyebrow": "SERVICE & MAINTENANCE WORK ORDER", "doc": "Work order",
        "ticket": "Ticket", "type": "Type", "customer": "Customer", "location": "Location", "installation": "Installation",
        "serial": "Serial number", "date": "Visit date", "techs": "Engineer(s)", "hours": "Hours worked",
        "travel": "Travel time (hrs)", "km": "Kilometres", "project": "Project",
        "report": "Reported issue", "findings": "Findings", "work": "Work carried out", "materials": "Materials used",
        "photos": "Photos", "approval": "Customer approval", "name": "Name",
        "not_signed": "Not signed – completed on {d}", "amended_signed": "Supplemented after signing on {d}",
        "amended_closed": "Supplemented after completion on {d}", "by": " by {n}",
        "types": {"storing": "Breakdown", "onderhoud": "Service & maintenance", "garantie": "Warranty", "inspectie": "Inspection", "overig": "Other"},
        "subject": "Work order {nr} – {title}", "subject_short": "Work order {nr} – De Vreugd Productietechniek",
        "greet": "Dear {n},", "body": "Please find attached the work order of our visit on {d}.",
        "body_short": "Please find attached the work order of our visit.",
        "unsigned": "The work order was not signed on site.", "questions": "If you have any questions, please do not hesitate to contact us.",
        "relation": "customer", "greet_none": "Dear Sir or Madam,",
        "plan_subject": "Visit scheduled – ticket {nr}", "plan_body": "We hereby confirm our visit on {d}{who}.", "plan_who": " by {n}",
        "plan_q": "If this is not convenient for you, please let us know by replying to this email.",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], "file": "Work order",
    },
    "de": {
        "eyebrow": "ARBEITSBERICHT SERVICE & WARTUNG", "doc": "Arbeitsbericht",
        "ticket": "Ticket", "type": "Art", "customer": "Kunde", "location": "Standort", "installation": "Anlage",
        "serial": "Seriennummer", "date": "Datum des Besuchs", "techs": "Monteur(e)", "hours": "Arbeitsstunden",
        "travel": "Reisezeit (Std.)", "km": "Kilometer", "project": "Projekt",
        "report": "Meldung", "findings": "Befund", "work": "Durchgeführte Arbeiten", "materials": "Verwendetes Material",
        "photos": "Fotos", "approval": "Zur Bestätigung durch den Kunden", "name": "Name",
        "not_signed": "Nicht unterschrieben – abgeschlossen am {d}", "amended_signed": "Nach der Unterschrift ergänzt am {d}",
        "amended_closed": "Nach dem Abschluss ergänzt am {d}", "by": " von {n}",
        "types": {"storing": "Störung", "onderhoud": "Service & Wartung", "garantie": "Garantie", "inspectie": "Inspektion", "overig": "Sonstiges"},
        "subject": "Arbeitsbericht {nr} – {title}", "subject_short": "Arbeitsbericht {nr} – De Vreugd Productietechniek",
        "greet": "Guten Tag {n},", "body": "Anbei erhalten Sie den Arbeitsbericht unseres Besuchs am {d}.",
        "body_short": "Anbei erhalten Sie den Arbeitsbericht unseres Besuchs.",
        "unsigned": "Der Arbeitsbericht wurde vor Ort nicht unterschrieben.", "questions": "Bei Fragen stehen wir Ihnen gerne zur Verfügung.",
        "relation": "Kunde", "greet_none": "Sehr geehrte Damen und Herren,",
        "plan_subject": "Besuch eingeplant – Ticket {nr}", "plan_body": "Hiermit bestätigen wir unseren Besuch am {d}{who}.", "plan_who": " durch {n}",
        "plan_q": "Falls dieser Termin nicht passt, antworten Sie bitte einfach auf diese E-Mail.",
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"], "file": "Arbeitsbericht",
    },
    "fr": {
        "eyebrow": "BON D'INTERVENTION SERVICE & MAINTENANCE", "doc": "Bon d'intervention",
        "ticket": "Ticket", "type": "Type", "customer": "Client", "location": "Site", "installation": "Installation",
        "serial": "Numéro de série", "date": "Date de visite", "techs": "Technicien(s)", "hours": "Heures travaillées",
        "travel": "Temps de trajet (h)", "km": "Kilomètres", "project": "Projet",
        "report": "Signalement", "findings": "Constatations", "work": "Travaux effectués", "materials": "Matériel utilisé",
        "photos": "Photos", "approval": "Pour accord client", "name": "Nom",
        "not_signed": "Non signé – clôturé le {d}", "amended_signed": "Complété après signature le {d}",
        "amended_closed": "Complété après clôture le {d}", "by": " par {n}",
        "types": {"storing": "Panne", "onderhoud": "Service & maintenance", "garantie": "Garantie", "inspectie": "Inspection", "overig": "Autre"},
        "subject": "Bon d'intervention {nr} – {title}", "subject_short": "Bon d'intervention {nr} – De Vreugd Productietechniek",
        "greet": "Bonjour {n},", "body": "Veuillez trouver ci-joint le bon d'intervention de notre visite du {d}.",
        "body_short": "Veuillez trouver ci-joint le bon d'intervention de notre visite.",
        "unsigned": "Le bon d'intervention n'a pas été signé sur place.", "questions": "Pour toute question, n'hésitez pas à nous contacter.",
        "relation": "client", "greet_none": "Madame, Monsieur,",
        "plan_subject": "Visite planifiée – ticket {nr}", "plan_body": "Nous vous confirmons notre visite le {d}{who}.", "plan_who": " par {n}",
        "plan_q": "Si cette date ne vous convient pas, merci de nous le faire savoir en répondant à cet e-mail.",
        "days": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
        "months": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"], "file": "Bon d'intervention",
    },
}


def tr(lang):
    return T.get(lang or "nl", T["nl"])


def long_date(iso, lang="nl"):
    """'2026-11-05' -> 'donderdag 5 november 2026' (in de taal)."""
    from datetime import date
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "")
    L = tr(lang)
    return f"{L['days'][d.weekday()]} {d.day} {L['months'][d.month - 1]} {d.year}"
