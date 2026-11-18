"""SQLite-database met eenvoudige, oplopende migraties.

Nieuwe databasewijzigingen worden ALTIJD als nieuw item onderaan MIGRATIONS
toegevoegd; bestaande migraties worden nooit aangepast. Zo blijft data bij
een update ("Pull and redeploy") altijd behouden.
"""
import sqlite3
import threading
from flask import g, current_app

_local = threading.local()

MIGRATIONS = [
    # 1 - basis
    """
    CREATE TABLE roles (
        id INTEGER PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        sort INTEGER DEFAULT 0
    );
    CREATE TABLE role_permissions (
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        module TEXT NOT NULL,
        level INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (role_id, module)
    );
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        email TEXT UNIQUE NOT NULL COLLATE NOCASE,
        name TEXT NOT NULL,
        role_id INTEGER REFERENCES roles(id),
        is_admin INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT
    );
    CREATE TABLE user_permissions (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        module TEXT NOT NULL,
        level INTEGER NOT NULL,
        PRIMARY KEY (user_id, module)
    );
    CREATE TABLE login_codes (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT UNIQUE NOT NULL,
        pin_hash TEXT,
        pin_attempts INTEGER NOT NULL DEFAULT 0,
        verified_at TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL,
        last_used TEXT
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT,
        entity_id INTEGER,
        details TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        entity TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'photo',
        filename TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        mime TEXT,
        size INTEGER,
        caption TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_files_entity ON files(entity, entity_id);
    CREATE TABLE push_subscriptions (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint TEXT UNIQUE NOT NULL,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE customers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        debtor_no TEXT,
        address TEXT, postcode TEXT, city TEXT,
        phone TEXT, email TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE locations (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        address TEXT, postcode TEXT, city TEXT
    );
    CREATE TABLE contacts (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        function TEXT, phone TEXT, email TEXT
    );
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        number TEXT NOT NULL,
        name TEXT NOT NULL,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'actief',
        sharepoint_path TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE installations (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        serial TEXT, year TEXT,
        service_interval_months INTEGER,
        next_service TEXT,
        notes TEXT
    );

    CREATE TABLE tickets (
        id INTEGER PRIMARY KEY,
        number TEXT UNIQUE NOT NULL,
        type TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'normaal',
        status TEXT NOT NULL DEFAULT 'nieuw',
        title TEXT NOT NULL,
        description TEXT,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
        installation_id INTEGER REFERENCES installations(id) ON DELETE SET NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        reported_by TEXT,
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        planned_date TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT
    );
    CREATE TABLE visits (
        id INTEGER PRIMARY KEY,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        technicians TEXT,
        hours REAL DEFAULT 0,
        travel_hours REAL DEFAULT 0,
        km REAL DEFAULT 0,
        findings TEXT,
        work_done TEXT,
        materials TEXT,
        signed_name TEXT,
        signed_at TEXT,
        sharepoint_status TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE pressure_tests (
        id INTEGER PRIMARY KEY,
        number TEXT UNIQUE NOT NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        drawing_no TEXT NOT NULL,
        drawing_rev TEXT,
        line_no TEXT,
        part TEXT NOT NULL,
        medium TEXT NOT NULL,
        test_pressure REAL NOT NULL,
        max_drop REAL,
        gauge_id TEXT,
        gauge_cal_date TEXT,
        executor TEXT,
        witness TEXT,
        status TEXT NOT NULL DEFAULT 'lopend',
        result TEXT,
        remarks TEXT,
        timer_minutes INTEGER,
        timer_started_at TEXT,
        next_check_at TEXT,
        notified_at TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        finished_by INTEGER,
        signed_name TEXT,
        sharepoint_status TEXT
    );
    CREATE TABLE pressure_readings (
        id INTEGER PRIMARY KEY,
        test_id INTEGER NOT NULL REFERENCES pressure_tests(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        pressure REAL NOT NULL,
        temperature REAL,
        note TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE calc_templates (
        id INTEGER PRIMARY KEY,
        worktype TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        data TEXT NOT NULL
    );
    CREATE TABLE calculations (
        id INTEGER PRIMARY KEY,
        number TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        customer_text TEXT,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        worktypes TEXT,
        answers TEXT,
        quantity INTEGER NOT NULL DEFAULT 1,
        agreed_price REAL,
        status TEXT NOT NULL DEFAULT 'concept',
        notes TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE calc_parts (
        id INTEGER PRIMARY KEY,
        calc_id INTEGER NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
        sort INTEGER NOT NULL,
        name TEXT NOT NULL
    );
    CREATE TABLE calc_sections (
        id INTEGER PRIMARY KEY,
        part_id INTEGER NOT NULL REFERENCES calc_parts(id) ON DELETE CASCADE,
        sort INTEGER NOT NULL,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        erp_pos INTEGER
    );
    CREATE TABLE calc_lines (
        id INTEGER PRIMARY KEY,
        section_id INTEGER NOT NULL REFERENCES calc_sections(id) ON DELETE CASCADE,
        sort INTEGER NOT NULL,
        description TEXT NOT NULL,
        unit TEXT,
        qty REAL NOT NULL DEFAULT 0,
        price REAL NOT NULL DEFAULT 0,
        factor REAL NOT NULL DEFAULT 1,
        is_koopdelen INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE calc_koopdelen (
        id INTEGER PRIMARY KEY,
        part_id INTEGER NOT NULL REFERENCES calc_parts(id) ON DELETE CASCADE,
        sort INTEGER NOT NULL,
        description TEXT NOT NULL,
        unit_price REAL NOT NULL DEFAULT 0,
        qty REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE nacalcs (
        id INTEGER PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        calc_id INTEGER REFERENCES calculations(id) ON DELETE SET NULL,
        order_no TEXT,
        order_desc TEXT,
        order_total REAL,
        invoiced_total REAL,
        divide_by REAL NOT NULL DEFAULT 1,
        no_divide TEXT,
        source TEXT,
        filename TEXT,
        imported_by INTEGER,
        imported_at TEXT NOT NULL
    );
    CREATE TABLE nacalc_lines (
        id INTEGER PRIMARY KEY,
        nacalc_id INTEGER NOT NULL REFERENCES nacalcs(id) ON DELETE CASCADE,
        item_id TEXT,
        item_desc TEXT,
        pos INTEGER,
        qty REAL,
        line_type INTEGER,
        unit TEXT,
        article TEXT,
        description TEXT,
        cost_unit REAL, cost_factor REAL, cost_discount REAL, cost_subtotal REAL,
        margin REAL,
        sale_unit REAL, sale_factor REAL, sale_discount REAL, sale_subtotal REAL
    );
    CREATE INDEX idx_nacalc_lines ON nacalc_lines(nacalc_id);

    CREATE TABLE articles (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        category TEXT,
        tags TEXT,
        body TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE bends (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        diameter REAL,
        radius REAL NOT NULL,
        tangent REAL NOT NULL DEFAULT 0,
        notes TEXT
    );
    """,
    # 2 - relaties uit Komdex (klanten en leveranciers), klantnummer SnelStart
    """
    ALTER TABLE customers ADD COLUMN komdex_id TEXT;
    ALTER TABLE customers ADD COLUMN komdex_snapshot TEXT;
    ALTER TABLE customers ADD COLUMN short_name TEXT;
    ALTER TABLE customers ADD COLUMN website TEXT;
    ALTER TABLE customers ADD COLUMN relation_type TEXT;
    ALTER TABLE customers ADD COLUMN relation_group TEXT;
    ALTER TABLE customers ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE customers ADD COLUMN snelstart_asked INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE customers ADD COLUMN updated_at TEXT;
    CREATE UNIQUE INDEX idx_customers_komdex ON customers(komdex_id) WHERE komdex_id IS NOT NULL;
    CREATE INDEX idx_customers_name ON customers(name);
    """,
    # 3 - meldingen bij machine en relatie ("meenemen", "let op")
    """
    ALTER TABLE installations ADD COLUMN alert TEXT;
    ALTER TABLE installations ADD COLUMN bring TEXT;
    ALTER TABLE customers ADD COLUMN alert TEXT;
    ALTER TABLE tickets ADD COLUMN alert_sent_at TEXT;
    """,
    # 4 - SharePoint-koppeling (klantmappen en projectmappen via Microsoft Graph)
    """
    CREATE TABLE sp_folders (
        item_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        web_url TEXT,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        auto INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    );
    CREATE INDEX idx_sp_folders_customer ON sp_folders(customer_id);
    ALTER TABLE projects ADD COLUMN sp_item_id TEXT;
    ALTER TABLE projects ADD COLUMN sp_parent_id TEXT;
    ALTER TABLE projects ADD COLUMN sp_name TEXT;
    ALTER TABLE projects ADD COLUMN sp_web_url TEXT;
    ALTER TABLE projects ADD COLUMN sp_missing INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projects ADD COLUMN source TEXT;
    CREATE UNIQUE INDEX idx_projects_sp ON projects(sp_item_id) WHERE sp_item_id IS NOT NULL;
    CREATE INDEX idx_projects_number ON projects(number);
    """,
    # 5 - meerdere functierollen per gebruiker
    """
    CREATE TABLE user_roles (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        PRIMARY KEY (user_id, role_id)
    );
    INSERT INTO user_roles (user_id, role_id) SELECT id, role_id FROM users WHERE role_id IS NOT NULL;
    """,
    # 6 - Projecten en Orders als losse modules, actievenster voor nieuwe ordermappen uit Komdex
    """
    ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'project';
    CREATE INDEX idx_projects_kind ON projects(kind);
    INSERT OR IGNORE INTO role_permissions (role_id, module, level) SELECT role_id, 'projecten', level FROM role_permissions WHERE module = 'klanten';
    INSERT OR IGNORE INTO role_permissions (role_id, module, level) SELECT role_id, 'orders', level FROM role_permissions WHERE module = 'klanten';
    INSERT OR IGNORE INTO user_permissions (user_id, module, level) SELECT user_id, 'projecten', level FROM user_permissions WHERE module = 'klanten';
    INSERT OR IGNORE INTO user_permissions (user_id, module, level) SELECT user_id, 'orders', level FROM user_permissions WHERE module = 'klanten';
    CREATE TABLE order_inbox (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'komdex',
        path TEXT NOT NULL UNIQUE,
        folder TEXT,
        parent TEXT,
        number TEXT,
        description TEXT,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'nieuw',
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT,
        missing INTEGER NOT NULL DEFAULT 0,
        handled_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        handled_at TEXT
    );
    CREATE INDEX idx_inbox_status ON order_inbox(status);
    CREATE INDEX idx_inbox_number ON order_inbox(number);
    """,
    # 7 - orderbon uit Komdex: gegevens bij project/order en automatisch verwerken op ordertype
    """
    ALTER TABLE projects ADD COLUMN order_type TEXT;
    ALTER TABLE projects ADD COLUMN executor TEXT;
    ALTER TABLE projects ADD COLUMN order_date TEXT;
    ALTER TABLE projects ADD COLUMN delivery_date TEXT;
    ALTER TABLE projects ADD COLUMN delivery_week TEXT;
    ALTER TABLE projects ADD COLUMN reference TEXT;
    ALTER TABLE projects ADD COLUMN contact_name TEXT;
    ALTER TABLE projects ADD COLUMN work_description TEXT;
    ALTER TABLE projects ADD COLUMN bon_file_id INTEGER;
    ALTER TABLE order_inbox ADD COLUMN bon_json TEXT;
    ALTER TABLE order_inbox ADD COLUMN bon_file_id INTEGER;
    ALTER TABLE order_inbox ADD COLUMN bon_received_at TEXT;
    ALTER TABLE order_inbox ADD COLUMN notified_at TEXT;
    ALTER TABLE order_inbox ADD COLUMN note TEXT;
    """,
    # 8 - bestaande Komdex-mappen mogen hun orderbon nog sturen (alleen aanvullen, nooit aanmaken)
    """
    UPDATE order_inbox SET bon_received_at = NULL WHERE status = 'bestaand' AND bon_json IS NULL;
    """,
    # 9 - gespreksnotities bij projecten en orders
    """
    CREATE TABLE project_notes (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'telefoon',
        contact TEXT,
        body TEXT NOT NULL,
        follow_up TEXT,
        follow_done INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_notes_project ON project_notes(project_id, date);
    CREATE INDEX idx_notes_follow ON project_notes(follow_up) WHERE follow_up IS NOT NULL AND follow_done = 0;
    """,
    # 10 - na-calculatie (Excel "Nacalculatie...") uit de ordermap op de server
    """
    ALTER TABLE order_inbox ADD COLUMN nacalc_sig TEXT;
    ALTER TABLE order_inbox ADD COLUMN nacalc_at TEXT;
    ALTER TABLE order_inbox ADD COLUMN nacalc_id INTEGER;
    ALTER TABLE order_inbox ADD COLUMN nacalc_note TEXT;
    """,
    # 11 - opmerkingen/nuances bij de na-calculatie (per order, zichtbaar bij elke import)
    """
    CREATE TABLE nacalc_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT,
        nacalc_id INTEGER REFERENCES nacalcs(id) ON DELETE SET NULL,
        body TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_nacalc_notes_order ON nacalc_notes(order_no);
    """,
    # 12 - orderbon printen via Printix
    """
    ALTER TABLE order_inbox ADD COLUMN printed_at TEXT;
    ALTER TABLE order_inbox ADD COLUMN print_note TEXT;
    """,
    # 13 - vinkjes van de orderbon (vaste prijs, geleverd, ...) en herkennen van een gewijzigde orderbon
    """
    ALTER TABLE projects ADD COLUMN order_props TEXT;
    ALTER TABLE order_inbox ADD COLUMN bon_sig TEXT;
    """,
    # 14 - calculatie uit Excel bij een na-calculatie
    """
    ALTER TABLE nacalcs ADD COLUMN xcalc_json TEXT;
    ALTER TABLE nacalcs ADD COLUMN xcalc_name TEXT;
    ALTER TABLE nacalcs ADD COLUMN xcalc_file_id INTEGER;
    """,
    # 15 - inkoopfacturen (crediteuren)
    """
    CREATE TABLE invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'nieuw',
        supplier TEXT,
        invoice_no TEXT,
        invoice_date TEXT,
        due_date TEXT,
        amount_incl REAL,
        amount_excl REAL,
        description TEXT,
        erp_ref TEXT,
        source TEXT,
        mail_id TEXT,
        mail_from TEXT,
        mail_subject TEXT,
        received_at TEXT,
        sha256 TEXT,
        file_id INTEGER,
        seen_at TEXT,
        seen_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        question_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        question_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        question_at TEXT,
        erp_at TEXT,
        erp_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        snelstart_at TEXT,
        snelstart_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        rejected_at TEXT,
        rejected_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        reject_reason TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_invoices_status ON invoices(status, received_at);
    CREATE INDEX idx_invoices_sha ON invoices(sha256);
    CREATE INDEX idx_invoices_question ON invoices(question_to);
    CREATE TABLE invoice_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'opmerking',
        body TEXT NOT NULL,
        to_user INTEGER REFERENCES users(id) ON DELETE SET NULL,
        for_accounting INTEGER NOT NULL DEFAULT 0,
        handled INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_invoice_notes ON invoice_notes(invoice_id);
    CREATE TABLE invoice_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_invoice_log ON invoice_log(invoice_id);
    CREATE TABLE invoice_mail_seen (
        message_id TEXT PRIMARY KEY,
        received_at TEXT
    );
    """,
    # 16 - geen extra rol 'boekhouding': de rollen liggen vast (IT-strategie)
    """
    UPDATE users SET role_id = NULL WHERE role_id IN (SELECT id FROM roles WHERE key = 'boekhouding');
    DELETE FROM roles WHERE key = 'boekhouding';
    """,
    # 17 - notities bij een serviceticket
    """
    CREATE TABLE ticket_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_ticket_notes ON ticket_notes(ticket_id, created_at);
    """,
    # 18 - bezoek afronden zonder handtekening; status 'gefactureerd' vervalt
    """
    ALTER TABLE visits ADD COLUMN closed_at TEXT;
    ALTER TABLE visits ADD COLUMN closed_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    ALTER TABLE visits ADD COLUMN closed_note TEXT;
    UPDATE tickets SET status = 'afgerond' WHERE status = 'gefactureerd';
    """,
    # 19 - opgeslagen berekeningen (rekentools: leidingen, pompen, CIP)
    """
    CREATE TABLE calc_saves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        title TEXT,
        inputs TEXT,
        results TEXT,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        ticket_id INTEGER REFERENCES tickets(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_calc_saves_project ON calc_saves(project_id);
    CREATE INDEX idx_calc_saves_ticket ON calc_saves(ticket_id);
    """,
    # 20 - aantekeningen (productie-opmerkingen) bij projecten en orders: afbeelding met markering, status en actie
    """
    CREATE TABLE project_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        body TEXT,
        image_file_id INTEGER,
        status TEXT NOT NULL DEFAULT 'nieuw',
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        due_date TEXT,
        done_note TEXT,
        done_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        done_at TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_annot_project ON project_annotations(project_id, number);
    CREATE INDEX idx_annot_assigned ON project_annotations(assigned_to, status);
    """,
    # 21 - gespreksnotities: wie er van De Vreugd bij was, en reacties op een notitie
    """
    ALTER TABLE project_notes ADD COLUMN our_people TEXT;
    CREATE TABLE project_note_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id INTEGER NOT NULL REFERENCES project_notes(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_note_replies ON project_note_replies(note_id, id);
    """,
    # 22 - werkbon (ook zonder handtekening) achteraf versturen: laatst verstuurd naar/op
    """
    ALTER TABLE visits ADD COLUMN sent_at TEXT;
    ALTER TABLE visits ADD COLUMN sent_to TEXT;
    """,
    # 23 - inkoopfacturen: leverancier gekoppeld aan de relaties (en waarop dat herkend is)
    """
    ALTER TABLE invoices ADD COLUMN supplier_id INTEGER REFERENCES customers(id) ON DELETE SET NULL;
    ALTER TABLE invoices ADD COLUMN supplier_match TEXT;
    """,
    # 24 - inkoopfacturen: welke weg (in Komdex of buiten Komdex om), ook nadat de factuur in SnelStart staat
    """
    ALTER TABLE invoices ADD COLUMN route TEXT;
    UPDATE invoices SET route = 'buiten' WHERE status = 'buiten';
    UPDATE invoices SET route = 'komdex' WHERE status IN ('erp', 'snelstart') AND erp_at IS NOT NULL;
    """,
    # 25 - inkoopfacturen: mailen met de leverancier vanaf de kaart (verstuurd en reacties)
    """
    ALTER TABLE invoice_notes ADD COLUMN mail_meta TEXT;
    """,
    # 26 - inkoopfacturen: mailwisseling (conversationId) van de binnengekomen factuurmail, voor antwoorden in hetzelfde draadje
    """
    ALTER TABLE invoices ADD COLUMN mail_conv TEXT;
    CREATE INDEX idx_invoices_conv ON invoices(mail_conv);
    """,
    # 27 - inkoopfacturen: afgeronde factuur -> vraag aan de boekhouding of de mail naar de map 'Verwerkt' mag
    #      (NULL / gevraagd / bezig / verplaatst / laten / mislukt)
    """
    ALTER TABLE invoices ADD COLUMN mail_archive TEXT;
    """,
    # 28 - inkoopfacturen: Outlook-categorieën van de factuurmail (JSON-lijst met namen), alleen uitgelezen
    """
    ALTER TABLE invoices ADD COLUMN mail_categories TEXT;
    """,
    # 29 - inkoopfacturen: tussenstap 'af te ronden door boekhouder'; wat al afgerond was blijft afgerond
    """
    UPDATE invoices SET mail_archive = 'afgerond' WHERE status = 'snelstart' AND mail_archive IS NULL;
    """,
    # 30 - inkoopfacturen: vraag bij de leverancier staat open tot iemand hem afrondt (knop); zolang geen status/afronden
    """
    ALTER TABLE invoices ADD COLUMN ext_question INTEGER NOT NULL DEFAULT 0;
    UPDATE invoices SET ext_question = 1 WHERE status NOT IN ('snelstart', 'afgewezen') AND (
      SELECT n.kind FROM invoice_notes n WHERE n.invoice_id = invoices.id AND n.kind IN ('mail_uit', 'mail_in')
      ORDER BY n.created_at DESC, n.id DESC LIMIT 1) = 'mail_uit';
    """,
    # 31 - inkoopfacturen: door een collega doorgestuurde factuurmail (mail_from = de leverancier uit de doorgestuurde mail)
    """
    ALTER TABLE invoices ADD COLUMN mail_fwd_by TEXT;
    """,
    # 32 - tickets: mailen met de klant vanuit het ticket; notities intern (kind 'intern') en klant ('mail_uit'/'mail_in')
    """
    ALTER TABLE ticket_notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'intern';
    ALTER TABLE ticket_notes ADD COLUMN mail_meta TEXT;
    ALTER TABLE tickets ADD COLUMN mail_conv TEXT;
    CREATE INDEX idx_tickets_conv ON tickets(mail_conv);
    """,
    # 33 - bezoeken: na aftekenen/afronden nog aanvullen (foto's, tekst); vastleggen wanneer en door wie
    """
    ALTER TABLE visits ADD COLUMN amended_at TEXT;
    ALTER TABLE visits ADD COLUMN amended_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    """,
    # 34 - relaties: taal voor werkbonnen en mails aan de klant (nl/en/de/fr)
    """
    ALTER TABLE customers ADD COLUMN language TEXT DEFAULT 'nl';
    """,
    # 35 - service: antwoorden van klanten ophalen staat standaard aan (stond na opslaan van Instellingen soms uit);
    #      opnieuw 14 dagen terugkijken zodat gemiste antwoorden alsnog binnenkomen
    """
    INSERT OR REPLACE INTO settings (key, value) VALUES ('svc_mail_on', '1');
    DELETE FROM settings WHERE key = 'svc_since';
    """,
    # 36 - service: eigen verzonden mails (uit Verzonden items) die ten onrechte als "van de klant" zijn opgehaald
    """
    CREATE TEMP TABLE _own AS SELECT id FROM ticket_notes WHERE kind = 'mail_in' AND (mail_meta LIKE '%"from": "/o=%'
        OR mail_meta LIKE '%"from": "%@' || (SELECT substr(v, instr(v, '@') + 1) FROM (SELECT COALESCE(
            NULLIF((SELECT value FROM settings WHERE key = 'svc_mailbox'), ''),
            NULLIF((SELECT value FROM settings WHERE key = 'cert_sender'), ''), 'devreugd-pt.nl') AS v)) || '"%');
    DELETE FROM files WHERE entity = 'ticket_note' AND entity_id IN (SELECT id FROM _own);
    DELETE FROM ticket_notes WHERE id IN (SELECT id FROM _own);
    DROP TABLE _own;
    """,
    # 37 - service: bezoeken plannen (datum + monteurs); na uitvoeren gekoppeld aan het vastgelegde bezoek
    """
    CREATE TABLE visit_plans (
        id INTEGER PRIMARY KEY,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        visit_id INTEGER REFERENCES visits(id) ON DELETE SET NULL,
        mailed_at TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE TABLE visit_plan_users (
        plan_id INTEGER NOT NULL REFERENCES visit_plans(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (plan_id, user_id)
    );
    CREATE INDEX idx_visit_plans_date ON visit_plans(date);
    CREATE INDEX idx_visit_plans_ticket ON visit_plans(ticket_id);
    """,
    # 38 - planning: ook een extern bedrijf (leverancier) met contactpersoon
    """
    ALTER TABLE visit_plans ADD COLUMN ext_customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL;
    ALTER TABLE visit_plans ADD COLUMN ext_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL;
    """,
    # 39 - telefoonnummer van een gebruiker (monteur) voor de bevestiging aan de klant
    """
    ALTER TABLE users ADD COLUMN phone TEXT;
    """,
    # 40 - externe monteur rondt het bezoek af via een persoonlijke link; daarna controle door ons
    """
    ALTER TABLE visit_plans ADD COLUMN ext_token_hash TEXT;
    ALTER TABLE visit_plans ADD COLUMN ext_token_until TEXT;
    ALTER TABLE visit_plans ADD COLUMN ext_done_at TEXT;
    CREATE UNIQUE INDEX idx_visit_plans_token ON visit_plans(ext_token_hash) WHERE ext_token_hash IS NOT NULL;
    ALTER TABLE visits ADD COLUMN review_pending INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE visits ADD COLUMN reviewed_at TEXT;
    ALTER TABLE visits ADD COLUMN reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    """,
    # 41 - externe monteur: tussentijds opslaan (op de server, ook verder op een ander apparaat)
    """
    ALTER TABLE visit_plans ADD COLUMN ext_draft TEXT;
    ALTER TABLE visit_plans ADD COLUMN ext_draft_at TEXT;
    """,
    # 42 - vertaling van de ingevulde teksten van een bezoek (werkbon in de taal van de klant)
    """
    ALTER TABLE visits ADD COLUMN tr_json TEXT;
    """,
    # 43 - projecten/orders: actiepunten (afvinklijst met foto's) en opleverdocumenten (afnamedocument, getekend op het scherm)
    """
    CREATE TABLE action_items (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        description TEXT,
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        due_date TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        done_at TEXT,
        done_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        done_note TEXT
    );
    CREATE INDEX idx_action_items_project ON action_items(project_id);
    CREATE INDEX idx_action_items_user ON action_items(assigned_to, done_at);
    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL DEFAULT 1,
        language TEXT NOT NULL DEFAULT 'nl',
        customer_name TEXT, address TEXT, postcode_city TEXT, country TEXT,
        attn TEXT, phone TEXT, reference TEXT,
        description TEXT, project_manager TEXT, delivery_date TEXT,
        collection_date TEXT, location TEXT, serial TEXT,
        lines_json TEXT, remarks TEXT,
        dv_name TEXT, dv_signed_at TEXT, cust_company TEXT, cust_name TEXT, cust_signed_at TEXT,
        pdf_file_id INTEGER, sp_status TEXT, mailed_at TEXT, mailed_to TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL, updated_at TEXT
    );
    CREATE INDEX idx_deliveries_project ON deliveries(project_id);
    """,
    # 44 - taken per persoon: voor jezelf of voor een collega, met herinnering (push en/of mail) en doorgeven
    """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        due_date TEXT,
        due_time TEXT,
        remind TEXT,
        remind_at TEXT,
        remind_mail INTEGER NOT NULL DEFAULT 0,
        reminded_at TEXT,
        seen_at TEXT,
        link_url TEXT,
        link_label TEXT,
        done_at TEXT,
        done_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_tasks_user ON tasks(assigned_to, done_at);
    CREATE INDEX idx_tasks_creator ON tasks(created_by, done_at);
    CREATE INDEX idx_tasks_remind ON tasks(remind_at) WHERE remind_at IS NOT NULL AND reminded_at IS NULL AND done_at IS NULL;
    CREATE TABLE task_log (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        to_user INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_task_log ON task_log(task_id, id);
    CREATE INDEX idx_task_log_user ON task_log(user_id);
    """,
    # 45 - debiteuren: verkoopfacturen (openstaande posten uit Excel of SnelStart, PDF uit het DigiDossier op DC01)
    """
    CREATE TABLE sales_invoices (
        id INTEGER PRIMARY KEY,
        number TEXT NOT NULL UNIQUE,
        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
        debtor_no TEXT,
        customer_name TEXT,
        invoice_date TEXT,
        due_date TEXT,
        amount REAL,
        paid REAL,
        open_amount REAL,
        status TEXT NOT NULL DEFAULT 'onbekend',
        status_source TEXT,
        status_at TEXT,
        paid_at TEXT,
        dossier_path TEXT,
        pdf_file_id INTEGER,
        pdf_sig TEXT,
        pdf_json TEXT,
        mail_to TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX idx_sales_inv_status ON sales_invoices(status, due_date);
    CREATE INDEX idx_sales_inv_customer ON sales_invoices(customer_id);
    CREATE TABLE sales_imports (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        filename TEXT,
        status TEXT NOT NULL DEFAULT 'voorbeeld',
        rows_json TEXT,
        summary_json TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT
    );
    """,
    # 46 - verkoopfacturen: order/project uit 'Onze ref.' op de factuur, en T.a.v. van het factuuradres
    """
    ALTER TABLE sales_invoices ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
    ALTER TABLE sales_invoices ADD COLUMN order_no TEXT;
    ALTER TABLE sales_invoices ADD COLUMN attn TEXT;
    CREATE INDEX idx_sales_inv_project ON sales_invoices(project_id);
    """,
    # 47 - herinneren en aanmanen: factuur-e-mail bij de relatie, uitstel/niet aanmanen per factuur, verstuurde brieven
    """
    ALTER TABLE customers ADD COLUMN invoice_email TEXT;
    ALTER TABLE sales_invoices ADD COLUMN postpone_until TEXT;
    ALTER TABLE sales_invoices ADD COLUMN postpone_reason TEXT;
    ALTER TABLE sales_invoices ADD COLUMN no_dunning INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sales_invoices ADD COLUMN dun_level INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sales_invoices ADD COLUMN dunned_at TEXT;
    CREATE TABLE dunning_letters (
        id INTEGER PRIMARY KEY,
        invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id) ON DELETE CASCADE,
        level INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'voorstel',
        auto INTEGER NOT NULL DEFAULT 0,
        language TEXT,
        to_addr TEXT,
        subject TEXT,
        body TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        sent_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        sent_at TEXT,
        error TEXT,
        note TEXT
    );
    CREATE INDEX idx_dunning_invoice ON dunning_letters(invoice_id, status);
    CREATE INDEX idx_dunning_status ON dunning_letters(status);
    """,
    # 48 - herinneringen/aanmaningen die al in het DigiDossier staan (uit het ERP verstuurd)
    """
    ALTER TABLE sales_invoices ADD COLUMN dossier_reminders TEXT;
    """,
    # 49 - herinneringen per klant (meerdere facturen in één brief) met PDF-brief
    """
    ALTER TABLE dunning_letters ADD COLUMN invoice_ids TEXT;
    ALTER TABLE dunning_letters ADD COLUMN group_key TEXT;
    ALTER TABLE dunning_letters ADD COLUMN customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL;
    ALTER TABLE dunning_letters ADD COLUMN pdf_file_id INTEGER;
    UPDATE dunning_letters SET invoice_ids = '[' || invoice_id || ']',
        customer_id = (SELECT customer_id FROM sales_invoices s WHERE s.id = dunning_letters.invoice_id),
        group_key = (SELECT CASE WHEN s.customer_id IS NOT NULL THEN 'c' || s.customer_id WHEN s.debtor_no IS NOT NULL THEN 'd' || s.debtor_no
                     ELSE 'i' || s.id END FROM sales_invoices s WHERE s.id = dunning_letters.invoice_id);
    CREATE INDEX idx_dunning_group ON dunning_letters(group_key, status);
    """,
    # 50 - mailgesprek bij een herinnering (verstuurde brief, antwoorden van de klant, onze antwoorden en notities)
    """
    ALTER TABLE dunning_letters ADD COLUMN mail_conv TEXT;
    CREATE TABLE dunning_messages (
        id INTEGER PRIMARY KEY,
        letter_id INTEGER NOT NULL REFERENCES dunning_letters(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        body TEXT,
        mail_meta TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_dunning_msg ON dunning_messages(letter_id, id);
    """,
    # 51 - herinneringen krijgen een eigen nummer (H-jaar-volgnummer) bij het versturen
    """
    ALTER TABLE dunning_letters ADD COLUMN number TEXT;
    UPDATE dunning_letters SET number = (SELECT 'H-' || substr(x.sent_at, 1, 4) || '-' || printf('%04d', x.rn) FROM
        (SELECT id, sent_at, ROW_NUMBER() OVER (PARTITION BY substr(sent_at, 1, 4) ORDER BY sent_at, id) AS rn
         FROM dunning_letters WHERE status = 'verstuurd') x WHERE x.id = dunning_letters.id)
        WHERE status = 'verstuurd';
    CREATE UNIQUE INDEX idx_dunning_number ON dunning_letters(number) WHERE number IS NOT NULL;
    """,
    # 52 - inkoopfacturen: betalingsherinneringen van leveranciers apart (tabblad Herinneringen voor de boekhouding)
    """
    ALTER TABLE invoices ADD COLUMN is_reminder INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE invoices ADD COLUMN reminder_of INTEGER;
    ALTER TABLE invoices ADD COLUMN reminder_done_at TEXT;
    ALTER TABLE invoices ADD COLUMN reminder_done_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    ALTER TABLE invoices ADD COLUMN reminder_note TEXT;
    CREATE INDEX idx_invoices_reminder ON invoices(is_reminder, reminder_done_at);
    """,
    # 53 - inkoopfacturen: zelflerend uitlezen (per leverancier en veld: waar staat de waarde in de PDF)
    """
    CREATE TABLE extract_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        field TEXT NOT NULL,
        kind TEXT NOT NULL,
        label TEXT,
        page INTEGER,
        x REAL,
        y REAL,
        sample TEXT,
        invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        UNIQUE(supplier_id, field)
    );
    """,
    # 54 - verkoopfacturen: nieuwe facturen klaarzetten, laten goedkeuren en verzenden
    """
    ALTER TABLE sales_invoices ADD COLUMN send_state TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_to TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_cc TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_subject TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_body TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_prepared_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    ALTER TABLE sales_invoices ADD COLUMN send_prepared_at TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    ALTER TABLE sales_invoices ADD COLUMN send_approved_at TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_note TEXT;
    ALTER TABLE sales_invoices ADD COLUMN sent_at TEXT;
    ALTER TABLE sales_invoices ADD COLUMN send_log TEXT;
    CREATE INDEX idx_sales_inv_send ON sales_invoices(send_state);
    INSERT OR REPLACE INTO settings (key, value) VALUES ('deb_send_since', strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'));
    UPDATE sales_invoices SET send_state = 'klaarzetten'
     WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day') AND pdf_file_id IS NOT NULL
       AND IFNULL(status, 'onbekend') <> 'betaald' AND (invoice_date IS NULL OR invoice_date >= date('now', '-14 days'));
    """,
]


def _connect(path):
    conn = sqlite3.connect(path, timeout=30, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DB_PATH"])
    return g.db


def raw_connection(path):
    """Verbinding buiten een request (scheduler, seed)."""
    return _connect(path)


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def migrate(path):
    conn = _connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
        if not row:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")
            conn.commit()
        for idx, sql in enumerate(MIGRATIONS, start=1):
            if idx <= current:
                continue
            conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
            conn.execute("UPDATE schema_version SET version = ?", (idx,))
            conn.commit()
            print(f"[WorkPortal] Databasemigratie {idx} uitgevoerd", flush=True)
    finally:
        conn.close()


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def backup(path, dest):
    src = _connect(path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
