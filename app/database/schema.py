"""Datenbankschema und Migrationsmechanismus.

Grundsatz: Die Datenbank ist ein *Protokoll*, kein Zwischenspeicher.  Sie muss
auch nach Programmabstuerzen, Netzlaufwerk-Aussetzern und Versionswechseln
lesbar bleiben.  Deshalb:

* Jede Schemaaenderung ist eine eigene, nummerierte Migration.  Es wird nie
  ein bestehendes ``CREATE TABLE`` nachtraeglich veraendert -- stattdessen
  kommt eine neue Migration mit ``ALTER TABLE`` dazu.
* Angewendete Migrationen stehen in ``schema_info``; daraus ergibt sich die
  vorgefundene Version.  Auch eine Datenbank aus einer aelteren Programm-
  version laesst sich so sauber hochziehen.
* Zeitstempel werden als ISO-Text ``JJJJ-MM-TT HH:MM:SS`` abgelegt.  Diese
  Schreibweise ist sortierbar und laesst sich mit einfachen ``BETWEEN``-
  Abfragen einschraenken -- ohne von der SQLite-Datumsarithmetik abzuhaengen.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

#: Aktuelle Schemaversion.  Bei jeder neuen Migration um eins erhoehen.
SCHEMA_VERSION = 1

#: Zeitformat aller Zeitstempelspalten (sortierbar, ohne Zeitzone --
#: die Anwendung laeuft an einem Standort).
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_text() -> str:
    """Aktueller Zeitstempel in der Schreibweise der Datenbank."""
    return datetime.now().strftime(TIMESTAMP_FORMAT)


# ---------------------------------------------------------------------------
# Verwaltungstabelle
# ---------------------------------------------------------------------------

#: Wird vor jeder Migrationspruefung angelegt.  Steht bewusst ausserhalb der
#: Migrationsliste -- ohne sie waere die Version gar nicht feststellbar.
SCHEMA_INFO_TABLE = """
CREATE TABLE IF NOT EXISTS schema_info (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Migration 1 -- Ausgangsschema
# ---------------------------------------------------------------------------

#: Ein Satz je durchgefuehrter SAP-Aktion.  Das ist die revisionssichere
#: Spur: Wer hat wann in welchem System welchen Wert veraendert?
CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    sap_user        TEXT    NOT NULL DEFAULT '',
    sap_system      TEXT    NOT NULL DEFAULT '',
    mode            TEXT    NOT NULL DEFAULT 'mock',      -- 'echt' | 'mock'
    dry_run         INTEGER NOT NULL DEFAULT 0,
    vendor_number   TEXT    NOT NULL DEFAULT '',
    vendor_name     TEXT    NOT NULL DEFAULT '',
    material_number TEXT    NOT NULL DEFAULT '',
    offer_number    TEXT    NOT NULL DEFAULT '',
    offer_date      TEXT    NOT NULL DEFAULT '',
    action          TEXT    NOT NULL DEFAULT '',          -- 'info_record' | 'source_list' | 'contract' | 'purchase_order'
    "transaction"   TEXT    NOT NULL DEFAULT '',          -- SQL-Schluesselwort -> immer in Anfuehrungszeichen
    old_value       TEXT    NOT NULL DEFAULT '',
    new_value       TEXT    NOT NULL DEFAULT '',
    document_number TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL DEFAULT '',          -- 'success' | 'failed' | 'skipped' | 'simulated'
    message         TEXT    NOT NULL DEFAULT '',
    detail          TEXT    NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT    NOT NULL DEFAULT ''
)
"""

#: Ein Satz je Verarbeitungslauf (ein Angebot, ein Knopfdruck).
#: Bewusst *ohne* Fremdschluessel auf ``history``: ein Protokolleintrag darf
#: niemals daran scheitern, dass der Laufsatz fehlt.
CREATE_RUN = """
CREATE TABLE IF NOT EXISTS run (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT    NOT NULL,
    finished_at       TEXT    NOT NULL DEFAULT '',
    dry_run           INTEGER NOT NULL DEFAULT 0,
    mock              INTEGER NOT NULL DEFAULT 1,
    offer_number      TEXT    NOT NULL DEFAULT '',
    vendor_name       TEXT    NOT NULL DEFAULT '',
    source_file       TEXT    NOT NULL DEFAULT '',
    positions_total   INTEGER NOT NULL DEFAULT 0,
    positions_success INTEGER NOT NULL DEFAULT 0,
    positions_failed  INTEGER NOT NULL DEFAULT 0,
    positions_skipped INTEGER NOT NULL DEFAULT 0,
    note              TEXT    NOT NULL DEFAULT ''
)
"""

#: Lieferantenname / Maildomain / USt-IdNr. -> SAP-Lieferantennummer.
CREATE_MAPPING_VENDOR = """
CREATE TABLE IF NOT EXISTS mapping_vendor (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type    TEXT NOT NULL,                 -- 'name' | 'domain' | 'vat_id' | 'email'
    match_value   TEXT NOT NULL,                 -- normalisiert (siehe repository)
    vendor_number TEXT NOT NULL,
    vendor_name   TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 1.0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT NOT NULL DEFAULT ''
)
"""

#: Lieferantenmaterialnummer bzw. Beschreibungstext -> eigene Materialnummer.
CREATE_MAPPING_MATERIAL = """
CREATE TABLE IF NOT EXISTS mapping_material (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_number   TEXT NOT NULL DEFAULT '',    -- '' = lieferantenuebergreifend
    match_type      TEXT NOT NULL,               -- 'vendor_material' | 'text'
    match_value     TEXT NOT NULL,               -- normalisiert
    material_number TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 1.0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    use_count       INTEGER NOT NULL DEFAULT 0
)
"""

#: Gelernte Layoutprofile je Lieferant.  ``payload`` ist JSON und wird von der
#: Erkennungskomponente befuellt -- die Datenbankschicht speichert nur.
CREATE_VENDOR_PROFILE = """
CREATE TABLE IF NOT EXISTS vendor_profile (
    profile_id       TEXT PRIMARY KEY,
    vendor_key       TEXT NOT NULL DEFAULT '',
    vendor_name      TEXT NOT NULL DEFAULT '',
    payload          TEXT NOT NULL DEFAULT '{}',
    sample_count     INTEGER NOT NULL DEFAULT 0,
    success_count    INTEGER NOT NULL DEFAULT 0,
    correction_count INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""

#: Schluessel/Wert fuer Fensterposition, zuletzt genutzte Werte usw.
CREATE_APP_STATE = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)
"""

#: Alle Tabellen der Version 1 in Anlegereihenfolge.
CREATE_TABLES: list[str] = [
    CREATE_HISTORY,
    CREATE_RUN,
    CREATE_MAPPING_VENDOR,
    CREATE_MAPPING_MATERIAL,
    CREATE_VENDOR_PROFILE,
    CREATE_APP_STATE,
]

#: Indizes der Version 1.  Die beiden UNIQUE-Indizes sind fachlich wichtig:
#: sie machen die Upserts der Zuordnungstabellen eindeutig.
CREATE_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_history_timestamp ON history (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS ix_history_run ON history (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_history_vendor ON history (vendor_number)",
    "CREATE INDEX IF NOT EXISTS ix_history_vendor_name ON history (vendor_name)",
    "CREATE INDEX IF NOT EXISTS ix_history_material ON history (material_number)",
    "CREATE INDEX IF NOT EXISTS ix_history_state ON history (state)",
    "CREATE INDEX IF NOT EXISTS ix_history_action ON history (action)",
    "CREATE INDEX IF NOT EXISTS ix_run_started ON run (started_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_mapping_vendor "
    "ON mapping_vendor (match_type, match_value)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_mapping_material "
    "ON mapping_material (vendor_number, match_type, match_value)",
    "CREATE INDEX IF NOT EXISTS ix_mapping_material_vendor ON mapping_material (vendor_number)",
    "CREATE INDEX IF NOT EXISTS ix_vendor_profile_key ON vendor_profile (vendor_key)",
]


#: Migrationsliste: ``(Zielversion, [SQL, ...])``, aufsteigend sortiert.
#: Heute existiert nur Version 1 -- der Mechanismus ist trotzdem vollstaendig,
#: damit spaetere Aenderungen ohne Umbau nachgezogen werden koennen.
#:
#: Beispiel fuer eine kuenftige Migration::
#:
#:     (2, ["ALTER TABLE history ADD COLUMN plant TEXT NOT NULL DEFAULT ''"]),
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, CREATE_TABLES + CREATE_INDEXES),
]


# ---------------------------------------------------------------------------
# Mechanismus
# ---------------------------------------------------------------------------

def ensure_schema_info(conn: sqlite3.Connection) -> None:
    """Verwaltungstabelle anlegen, falls sie fehlt."""
    conn.execute(SCHEMA_INFO_TABLE)


def current_version(conn: sqlite3.Connection) -> int:
    """Vorgefundene Schemaversion; 0 bei leerer/neuer Datenbank."""
    ensure_schema_info(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_info").fetchone()
    value = row[0] if row else None
    return int(value) if value is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Alle noch fehlenden Migrationen anwenden.

    Jede Migration laeuft in einer eigenen Transaktion: bricht eine ab, bleibt
    die Datenbank auf dem letzten vollstaendig angewendeten Stand -- und der
    naechste Programmstart versucht es erneut.

    Rueckgabe: die Version nach dem Lauf.
    """
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        # Datenbank stammt aus einer neueren Programmversion.  Nicht anfassen,
        # aber auch nicht abbrechen -- der Anwender soll wenigstens lesen koennen.
        logger.warning(
            "Datenbank hat Schemaversion %s, das Programm kennt nur %s. "
            "Es werden keine Migrationen ausgefuehrt.", version, SCHEMA_VERSION
        )
        return version

    for target, statements in sorted(MIGRATIONS, key=lambda item: item[0]):
        if target <= version:
            continue
        logger.info("Wende Datenbankmigration auf Version %s an", target)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_info (version, applied_at) VALUES (?, ?)",
                (target, now_text()),
            )
        except sqlite3.Error:
            conn.rollback()
            logger.exception("Migration auf Version %s fehlgeschlagen", target)
            raise
        conn.commit()
        version = target

    return version
