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
