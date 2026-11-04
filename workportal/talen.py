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
        "plan_subject": "Bezoek ingepland – ticket {nr}", "plan_body": "Graag bevestigen wij ons bezoek op {d}{who}.", "plan_who": " door {n}", "tel": "tel.", "plan_tech": "Monteur", "plan_techs": "Monteurs", "of_company": "van", "x_subject": "Ingepland: bezoek {d} – ticket {nr}", "x_body": "Hierbij bevestigen wij dat u bent ingepland voor een bezoek op {d}.",
        "x_customer": "Klant", "x_address": "Adres", "x_install": "Installatie", "x_ticket": "Ticket", "x_contact": "Contactpersoon klant", "x_report": "Melding",
        "x_informed": "De klant is hierover per mail geïnformeerd en heeft uw naam en telefoonnummer ontvangen.", "x_q": "Heeft u vragen of past deze datum niet? Antwoord dan op deze mail.",
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
        "plan_subject": "Visit scheduled – ticket {nr}", "plan_body": "We hereby confirm our visit on {d}{who}.", "plan_who": " by {n}", "tel": "phone", "plan_tech": "Engineer", "plan_techs": "Engineers", "of_company": "of", "x_subject": "Scheduled: visit {d} – ticket {nr}", "x_body": "We hereby confirm that you are scheduled for a visit on {d}.",
        "x_customer": "Customer", "x_address": "Address", "x_install": "Installation", "x_ticket": "Ticket", "x_contact": "Customer contact", "x_report": "Reported issue",
        "x_informed": "The customer has been informed by email and has received your name and phone number.", "x_q": "If you have any questions or this date does not suit you, please reply to this email.",
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
        "plan_subject": "Besuch eingeplant – Ticket {nr}", "plan_body": "Hiermit bestätigen wir unseren Besuch am {d}{who}.", "plan_who": " durch {n}", "tel": "Tel.", "plan_tech": "Monteur", "plan_techs": "Monteure", "of_company": "von", "x_subject": "Eingeplant: Besuch {d} – Ticket {nr}", "x_body": "Hiermit bestätigen wir, dass Sie für einen Besuch am {d} eingeplant sind.",
        "x_customer": "Kunde", "x_address": "Adresse", "x_install": "Anlage", "x_ticket": "Ticket", "x_contact": "Ansprechpartner Kunde", "x_report": "Meldung",
        "x_informed": "Der Kunde wurde per E-Mail informiert und hat Ihren Namen und Ihre Telefonnummer erhalten.", "x_q": "Bei Fragen oder falls der Termin nicht passt, antworten Sie bitte auf diese E-Mail.",
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
        "plan_subject": "Visite planifiée – ticket {nr}", "plan_body": "Nous vous confirmons notre visite le {d}{who}.", "plan_who": " par {n}", "tel": "tél.", "plan_tech": "Technicien", "plan_techs": "Techniciens", "of_company": "de", "x_subject": "Planifié : visite {d} – ticket {nr}", "x_body": "Nous vous confirmons que vous êtes planifié pour une visite le {d}.",
        "x_customer": "Client", "x_address": "Adresse", "x_install": "Installation", "x_ticket": "Ticket", "x_contact": "Contact client", "x_report": "Signalement",
        "x_informed": "Le client a été informé par e-mail et a reçu votre nom et votre numéro de téléphone.", "x_q": "Pour toute question ou si cette date ne convient pas, merci de répondre à cet e-mail.",
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
