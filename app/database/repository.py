"""Zugriffsschicht auf die SQLite-Datenbank (Historie, Zuordnungen, Profile).

Entwurfsgrundsaetze
-------------------

* **Kein ORM.**  Reines ``sqlite3`` mit ausschliesslich parametrisierten
  Abfragen.  Werte werden *niemals* in SQL-Text formatiert; wo dynamisch
  gefiltert wird, entstehen nur Platzhalter (``?``) aus einer festen
  Spaltenliste.
* **Threadsicher.**  Die Oberflaeche arbeitet mit ``QThread``s; jede
  Verarbeitung schreibt waehrend des Laufs Protokollsaetze.  Ein
  ``threading.RLock`` schuetzt deshalb jeden Zugriff, die Verbindung wird mit
  ``check_same_thread=False`` geoeffnet.
* **Nie die Anwendung killen.**  Ein defektes Netzlaufwerk oder eine gesperrte
  Datei darf den Einkaeufer nicht mitten im Vorgang aus dem Programm werfen.
  Alle Methoden fangen ``sqlite3.Error`` ab, protokollieren und liefern einen
  unschaedlichen Ersatzwert.  Offensichtliche Programmierfehler (falscher
  Datentyp, unbekannter Filterschluessel) fliegen dagegen bewusst weiter.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..models.enums import ResultState
from ..models.offer import Offer
from ..models.results import ActionResult, BatchSummary
from ..utils.parsing import (
    normalize_material_number,
    normalize_vendor_number,
    normalize_whitespace,
)
from .schema import SCHEMA_VERSION, TIMESTAMP_FORMAT, apply_migrations, current_version, now_text

logger = logging.getLogger(__name__)

__all__ = [
    "Repository",
    "normalize_vendor_match_value",
    "normalize_material_match_value",
    "ACTION_LABELS",
    "STATE_LABELS",
]

#: Deutsche Beschriftungen fuer Export und Oberflaeche.
ACTION_LABELS = {
    "info_record": "Infosatz",
    "source_list": "Orderbuch",
    "contract": "Mengenkontrakt",
    "purchase_order": "Bestellung",
}

STATE_LABELS = {
    "success": "erfolgreich",
    "failed": "fehlgeschlagen",
    "skipped": "uebersprungen",
    "simulated": "simuliert (Dry Run)",
}

#: Spalten, ueber die die Volltextsuche der Historie laeuft.
_FULLTEXT_COLUMNS = (
    "vendor_name",
    "vendor_number",
    "material_number",
    "offer_number",
    "document_number",
    "message",
    "detail",
    "old_value",
    "new_value",
)

#: Spalten der Historie in fester Reihenfolge (Insert + Export).
_HISTORY_COLUMNS = (
    "timestamp",
    "sap_user",
    "sap_system",
    "mode",
    "dry_run",
    "vendor_number",
    "vendor_name",
    "material_number",
    "offer_number",
    "offer_date",
    "action",
    "transaction",
    "old_value",
    "new_value",
    "document_number",
    "state",
    "message",
    "detail",
    "duration_ms",
    "run_id",
)

#: Ueberschriften des CSV-Exports (deutsches Excel).
_CSV_HEADERS = (
    ("timestamp", "Zeitpunkt"),
    ("mode", "Betriebsart"),
    ("dry_run", "Simulation"),
    ("vendor_number", "Lieferantennummer"),
    ("vendor_name", "Lieferant"),
    ("material_number", "Material"),
    ("offer_number", "Angebotsnummer"),
    ("offer_date", "Angebotsdatum"),
    ("action", "Aktion"),
    ("transaction", "Transaktion"),
    ("old_value", "Alter Wert"),
    ("new_value", "Neuer Wert"),
    ("document_number", "Belegnummer"),
    ("state", "Status"),
    ("message", "Meldung"),
    ("detail", "Detail"),
    ("duration_ms", "Dauer (ms)"),
    ("sap_user", "SAP-Benutzer"),
    ("sap_system", "SAP-System"),
    ("run_id", "Lauf"),
)


# ---------------------------------------------------------------------------
# Normalisierung der Zuordnungsschluessel
# ---------------------------------------------------------------------------

def normalize_vendor_match_value(match_type: str, value: object) -> str:
    """Suchwert einer Lieferantenzuordnung vereinheitlichen.

    Beim Schreiben *und* beim Lesen wird exakt dieselbe Funktion verwendet --
    sonst findet man eine gespeicherte Zuordnung spaeter nicht wieder.
    """
    text = normalize_whitespace(value)
    if not text:
        return ""
    kind = (match_type or "").strip().lower()
    if kind in ("domain", "email"):
        # "Einkauf@Muster-GmbH.DE" -> "einkauf@muster-gmbh.de", "<...>" faellt weg
        return text.strip("<>").lower()
    if kind == "vat_id":
        return text.replace(" ", "").replace(".", "").replace("-", "").upper()
    return text.lower()


def normalize_material_match_value(match_type: str, value: object) -> str:
    """Suchwert einer Materialzuordnung vereinheitlichen."""
    kind = (match_type or "").strip().lower()
    if kind == "vendor_material":
        return normalize_material_number(value)
    return normalize_whitespace(value).lower()


def _escape_like(value: str) -> str:
    """Sonderzeichen fuer ``LIKE ... ESCAPE '\\'`` entschaerfen."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_timestamp_text(value: object, *, end_of_day: bool = False) -> str:
    """Beliebige Datumsangabe in den Textzeitstempel der Datenbank wandeln."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime(TIMESTAMP_FORMAT)
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d") + (" 23:59:59" if end_of_day else " 00:00:00")
    text = str(value).strip()
    if len(text) == 10:  # nur Datum uebergeben
        return text + (" 23:59:59" if end_of_day else " 00:00:00")
    return text


def _german_datetime(value: str) -> str:
    """``2026-08-17 14:05:09`` -> ``17.08.2026 14:05``."""
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in (TIMESTAMP_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%d.%m.%Y %H:%M")
    return text


def _german_date(value: str) -> str:
    """``2026-08-17`` -> ``17.08.2026``; unbekannte Formate bleiben stehen."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    """Alle Datenbankzugriffe der Anwendung an einer Stelle."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._closed = False
        #: True, wenn beim Start eine defekte Datei beiseitegelegt wurde.
        self.recovered_from_corruption = False
        #: Pfad der beiseitegelegten Datei (fuer eine Meldung an den Anwender).
        self.corrupt_backup_path: Path | None = None

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()

    # -- Verbindung ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,   # Zugriff aus QThreads, Serialisierung ueber _lock
            isolation_level=None,      # Autocommit; Transaktionen steuern wir selbst
            timeout=15.0,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """PRAGMAs setzen und Migrationen ausfuehren."""
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            # Auf manchen Netzlaufwerken nicht moeglich -- kein Grund aufzugeben.
            logger.warning("WAL-Modus nicht verfuegbar (%s), es bleibt beim Journal", exc)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        apply_migrations(conn)

    def _open(self) -> sqlite3.Connection:
        """Datenbank oeffnen; eine defekte Datei wird beiseitegelegt."""
        conn = self._connect()
        try:
            self._prepare(conn)
            return conn
        except sqlite3.DatabaseError as exc:
            logger.error("Datenbank %s ist nicht lesbar: %s", self.db_path, exc)
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._quarantine_broken_file(exc)
            conn = self._connect()
            self._prepare(conn)
            return conn

    def _quarantine_broken_file(self, exc: Exception) -> None:
        """Unbrauchbare Datei umbenennen, damit neu begonnen werden kann.

        Die Historie ist wertvoll -- deshalb wird nichts geloescht, sondern nur
        zur Seite gelegt.  Der Anwender kann die Datei dem Support geben.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.db_path.with_suffix(self.db_path.suffix + f".defekt-{stamp}")
        try:
            if self.db_path.exists():
                shutil.move(str(self.db_path), str(backup))
                self.corrupt_backup_path = backup
                logger.error(
                    "Defekte Datenbank wurde nach %s verschoben, es wird eine neue "
                    "angelegt (Ursache: %s)", backup, exc
                )
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.db_path) + suffix)
                if side.exists():
                    side.unlink()
        except OSError as os_exc:
            logger.error("Defekte Datenbank konnte nicht beiseitegelegt werden: %s", os_exc)
            raise
        self.recovered_from_corruption = True

    # -- Lebenszyklus ----------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning("Datenbank konnte nicht sauber geschlossen werden: %s", exc)
            self._closed = True

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover - reine Diagnose
        return f"<Repository {self.db_path}>"

    @property
    def schema_version(self) -> int:
        with self._lock:
            try:
                return current_version(self._conn)
            except sqlite3.Error as exc:
                logger.error("Schemaversion nicht lesbar: %s", exc)
                return 0

    # -- interne Helfer --------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Schreibtransaktion (verschachtelbar, aussen wird committet)."""
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth = 0
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    logger.exception("Rollback fehlgeschlagen")
                raise
            self._tx_depth = 0
            self._conn.commit()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        """Leseabfrage; bei Fehlern leere Liste statt Programmabbruch."""
        with self._lock:
            try:
                rows = self._conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.Error:
                logger.exception("Abfrage fehlgeschlagen: %s", sql.strip().splitlines()[0])
                return []
        return [dict(row) for row in rows]

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Schreibbefehl; Rueckgabe ist ``rowcount`` bzw. -1 bei Fehler."""
        try:
            with self._tx() as conn:
                cursor = conn.execute(sql, tuple(params))
                return cursor.rowcount
        except sqlite3.Error:
            logger.exception("Schreibbefehl fehlgeschlagen: %s", sql.strip().splitlines()[0])
            return -1

    # ==================================================================
    # Laeufe und Historie
    # ==================================================================
    def start_run(
        self,
        *,
        offer: Offer | None = None,
        dry_run: bool = False,
        mock: bool = True,
        offer_number: str = "",
        vendor_name: str = "",
        source_file: str = "",
        note: str = "",
        run_id: str | None = None,
    ) -> str:
        """Neuen Verarbeitungslauf anlegen und dessen Kennung zurueckgeben."""
        identifier = run_id or uuid.uuid4().hex
        if offer is not None:
            offer_number = offer_number or offer.offer_number
            vendor_name = vendor_name or offer.vendor_name
            if not source_file and offer.source_files:
                source_file = offer.source_files[0]
        self._execute(
            "INSERT OR REPLACE INTO run (run_id, started_at, finished_at, dry_run, mock, "
            "offer_number, vendor_name, source_file, positions_total, positions_success, "
            "positions_failed, positions_skipped, note) "
            "VALUES (?, ?, '', ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)",
            (
                identifier,
                now_text(),
                int(bool(dry_run)),
                int(bool(mock)),
                offer_number or "",
                vendor_name or "",
                source_file or "",
                note or "",
            ),
        )
        logger.debug("Lauf %s gestartet (dry_run=%s, mock=%s)", identifier, dry_run, mock)
        return identifier

    def finish_run(self, run_id: str, summary: BatchSummary | None = None,
                   note: str = "") -> None:
        """Lauf abschliessen und die Zaehler aus der Zusammenfassung sichern."""
        total = success = failed = skipped = 0
        if summary is not None:
            total = len(summary.results)
            success = summary.succeeded
            failed = summary.failed
            skipped = summary.skipped
            if summary.aborted and summary.abort_reason and not note:
                note = f"Abgebrochen: {summary.abort_reason}"
        finished = ""
        if summary is not None and summary.finished_at:
            finished = summary.finished_at.strftime(TIMESTAMP_FORMAT)
        finished = finished or now_text()

        sql = (
            "UPDATE run SET finished_at = ?, positions_total = ?, positions_success = ?, "
            "positions_failed = ?, positions_skipped = ?"
        )
        params: list[Any] = [finished, total, success, failed, skipped]
        if note:
            sql += ", note = ?"
            params.append(note)
        sql += " WHERE run_id = ?"
        params.append(run_id)
        self._execute(sql, params)

    def log_action(
        self,
        run_id: str,
        position_context: dict | None,
        result: ActionResult,
        *,
        sap_user: str = "",
        sap_system: str = "",
        mode: str = "mock",
        dry_run: bool = False,
        timestamp: datetime | None = None,
    ) -> int:
        """Eine einzelne SAP-Aktion protokollieren.

        ``position_context`` liefert die fachlichen Schluessel der Position
        (``vendor_number``, ``vendor_name``, ``material_number``,
        ``offer_number``, ``offer_date``).  Fehlende Angaben sind erlaubt --
        die Protokollzeile entsteht trotzdem.
        """
        context = position_context or {}
        offer_date = context.get("offer_date")
        if isinstance(offer_date, (date, datetime)):
            offer_date_text = offer_date.strftime("%Y-%m-%d")
        else:
            offer_date_text = str(offer_date or "")

        state = result.state.value if isinstance(result.state, ResultState) else str(result.state)
        detail = result.detail or ""
        if result.sap_messages:
            joined = "\n".join(result.sap_messages)
            detail = f"{detail}\n{joined}".strip()

        values = (
            (timestamp or datetime.now()).strftime(TIMESTAMP_FORMAT),
            sap_user or "",
            sap_system or "",
            # Alles ausser einer ausdruecklichen Echtbetriebs-Angabe gilt als Mock --
            # lieber ein Eintrag zu vorsichtig als ein falsches "echt" im Protokoll.
            "echt" if str(mode).strip().lower() in ("echt", "real", "live") else "mock",
            int(bool(dry_run)),
            str(context.get("vendor_number") or ""),
            str(context.get("vendor_name") or ""),
            str(context.get("material_number") or ""),
            str(context.get("offer_number") or ""),
            offer_date_text,
            result.action or "",
            result.transaction or "",
            result.old_value or "",
            result.new_value or "",
            result.document_number or "",
            state,
            result.message or "",
            detail,
            int(result.duration_ms or 0),
            run_id or "",
        )
        columns = ", ".join(f'"{name}"' for name in _HISTORY_COLUMNS)
        placeholders = ", ".join("?" for _ in _HISTORY_COLUMNS)
        try:
            with self._tx() as conn:
                cursor = conn.execute(
                    f"INSERT INTO history ({columns}) VALUES ({placeholders})", values
                )
                return int(cursor.lastrowid or 0)
        except sqlite3.Error:
            logger.exception("Protokolleintrag konnte nicht geschrieben werden")
            return 0

    def log_batch(
        self,
        run_id: str,
        offer: Offer,
        summary: BatchSummary,
        *,
        sap_user: str = "",
        sap_system: str = "",
        mock: bool = True,
        finish: bool = True,
    ) -> int:
        """Kompletten Lauf wegschreiben: alle Aktionen plus Laufabschluss.

        Bequemlichkeitsfunktion fuer den Verarbeitungsdienst -- damit dieser
        sich nicht um Einzelheiten der Protokollierung kuemmern muss.
        Rueckgabe ist die Anzahl geschriebener Protokollzeilen.
        """
        mode = "mock" if mock else "echt"
        by_uid = {position.uid: position for position in offer.positions}
        written = 0
        try:
            with self._tx():
                for position_result in summary.results:
                    position = by_uid.get(position_result.position_uid)
                    context = {
                        "vendor_number": getattr(position, "vendor_number", "") or offer.vendor_number,
                        "vendor_name": offer.vendor_name,
                        "material_number": (
                            getattr(position, "material_number", "") or position_result.label
                        ),
                        "offer_number": offer.offer_number,
                        "offer_date": offer.offer_date,
                    }
                    for action in position_result.actions:
                        if self.log_action(
                            run_id, context, action,
                            sap_user=sap_user, sap_system=sap_system,
                            mode=mode, dry_run=summary.dry_run,
                            timestamp=position_result.finished_at,
                        ):
                            written += 1

                # Belegweite Ergebnisse (Kontrakt/Bestellung ueber alle Positionen)
                header_context = {
                    "vendor_number": offer.vendor_number,
                    "vendor_name": offer.vendor_name,
                    "material_number": "",
                    "offer_number": offer.offer_number,
                    "offer_date": offer.offer_date,
                }
                for action in summary.document_results:
                    if self.log_action(
                        run_id, header_context, action,
                        sap_user=sap_user, sap_system=sap_system,
                        mode=mode, dry_run=summary.dry_run,
                    ):
                        written += 1
        except sqlite3.Error:
            logger.exception("Lauf %s konnte nicht vollstaendig protokolliert werden", run_id)

        if finish:
            self.finish_run(run_id, summary)
        return written

    # -- Abfragen --------------------------------------------------------
    def _build_history_filter(self, filters: dict | None) -> tuple[str, list[Any]]:
        """WHERE-Teil und Parameterliste aus dem Filterwoerterbuch bauen.

        Es werden ausschliesslich feste Spaltennamen in den SQL-Text
        geschrieben, alle Werte gehen als Parameter in die Abfrage.
        """
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []

        date_from = _as_timestamp_text(filters.get("date_from"))
        if date_from:
            clauses.append("timestamp >= ?")
            params.append(date_from)
        date_to = _as_timestamp_text(filters.get("date_to"), end_of_day=True)
        if date_to:
            clauses.append("timestamp <= ?")
            params.append(date_to)

        vendor = normalize_whitespace(filters.get("vendor"))
        if vendor:
            clauses.append("(vendor_name LIKE ? ESCAPE '\\' OR vendor_number LIKE ? ESCAPE '\\')")
            pattern = f"%{_escape_like(vendor)}%"
            params.extend([pattern, pattern])

        material = normalize_whitespace(filters.get("material"))
        if material:
            clauses.append("material_number LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(material)}%")

        action = filters.get("action")
        if action:
            actions = [action] if isinstance(action, str) else list(action)
            clauses.append("action IN (%s)" % ", ".join("?" for _ in actions))
            params.extend(actions)

        state = filters.get("state")
        if state:
            states = [state] if isinstance(state, str) else list(state)
            states = [s.value if isinstance(s, ResultState) else str(s) for s in states]
            clauses.append("state IN (%s)" % ", ".join("?" for _ in states))
            params.extend(states)

        run_id = filters.get("run_id")
        if run_id:
            clauses.append("run_id = ?")
            params.append(str(run_id))

        text = normalize_whitespace(filters.get("text"))
        if text:
            pattern = f"%{_escape_like(text)}%"
            clauses.append(
                "(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in _FULLTEXT_COLUMNS) + ")"
            )
            params.extend([pattern] * len(_FULLTEXT_COLUMNS))

        mode = filters.get("mode")
        if filters.get("only_real"):
            mode = "echt"
        elif filters.get("only_mock"):
            mode = "mock"
        if mode:
            clauses.append("mode = ?")
            params.append(str(mode))

        if "dry_run" in filters and filters["dry_run"] is not None:
            clauses.append("dry_run = ?")
            params.append(int(bool(filters["dry_run"])))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def history(self, filters: dict | None = None) -> list[dict]:
        """Protokollzeilen, neueste zuerst.

        Unterstuetzte Filter: ``date_from``, ``date_to``, ``vendor``,
        ``material``, ``action``, ``state``, ``run_id``, ``text``, ``mode``,
        ``only_real``, ``only_mock``, ``dry_run``, ``limit``, ``offset``.
        """
        filters = filters or {}
        where, params = self._build_history_filter(filters)
        limit = int(filters.get("limit") or 500)
        offset = int(filters.get("offset") or 0)
        sql = (
            'SELECT id, timestamp, sap_user, sap_system, mode, dry_run, vendor_number, '
            'vendor_name, material_number, offer_number, offer_date, action, "transaction", '
            'old_value, new_value, document_number, state, message, detail, duration_ms, run_id '
            "FROM history" + where + " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
        )
        return self._query(sql, [*params, limit, offset])

    def history_count(self, filters: dict | None = None) -> int:
        """Anzahl der Treffer zu einem Filter (fuer Blaetterleisten)."""
        where, params = self._build_history_filter(filters)
        rows = self._query("SELECT COUNT(*) AS anzahl FROM history" + where, params)
        return int(rows[0]["anzahl"]) if rows else 0

    def history_for_run(self, run_id: str) -> list[dict]:
        """Alle Protokollzeilen eines Laufs in Entstehungsreihenfolge."""
        return self._query(
            'SELECT id, timestamp, mode, dry_run, vendor_number, vendor_name, material_number, '
            'offer_number, offer_date, action, "transaction", old_value, new_value, '
            "document_number, state, message, detail, duration_ms, run_id "
            "FROM history WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        )

    def documents_already_created(self, offer_number: str, action: str,
                                  material_number: str = "") -> list[dict]:
        """Belege, die dieses Angebot in frueheren Laeufen schon erzeugt hat.

        Das ist der Doppel-Schutz fuer Kontrakt und Bestellung: SAP selbst
        laesst sich zu derselben Angebotsnummer beliebig oft beschicken, und
        eine Suchmaske dafuer wuerde geratene Feld-IDs erfordern.  Die eigene
        Historie weiss es dagegen sicher -- allerdings nur ueber das, was
        dieses Werkzeug selbst geschrieben hat.  Genau so wird es dem
        Anwender auch gemeldet.

        Beruecksichtigt werden ausschliesslich echte, erfolgreiche Schreib-
        vorgaenge: Probelauf und Testsystem zaehlen nicht, sonst warnt das
        Werkzeug vor Belegen, die es in SAP gar nicht gibt.
        """
        if not offer_number.strip() or not action.strip():
            return []
        sql = ("SELECT document_number, timestamp, material_number, vendor_number, "
               "run_id, new_value FROM history "
               "WHERE offer_number = ? AND action = ? AND state = 'success' "
               "AND dry_run = 0 AND mode = 'echt' AND document_number <> ''")
        params: list[Any] = [offer_number.strip(), action.strip()]
        if material_number.strip():
            sql += " AND material_number = ?"
            params.append(material_number.strip())
        sql += " ORDER BY id DESC"

        gesehen: set[str] = set()
        eindeutig: list[dict] = []
        for row in self._query(sql, params):
            nummer = str(row.get("document_number") or "")
            if nummer in gesehen:
                continue
            gesehen.add(nummer)
            eindeutig.append(row)
        return eindeutig

    def runs(self, limit: int = 50) -> list[dict]:
        """Die letzten Verarbeitungslaeufe, neueste zuerst."""
        return self._query(
            "SELECT run_id, started_at, finished_at, dry_run, mock, offer_number, vendor_name, "
            "source_file, positions_total, positions_success, positions_failed, "
            "positions_skipped, note FROM run ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        )

    def run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM run WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def history_stats(self) -> dict:
        """Kennzahlen fuer die Startseite: Gesamt, je Status, letzte 30 Tage."""
        stats: dict[str, Any] = {
            "total": 0,
            "by_state": {},
            "last_30_days": {"total": 0, "by_state": {}},
            "runs": 0,
            "first_entry": "",
            "last_entry": "",
            "schema_version": SCHEMA_VERSION,
        }
        for row in self._query("SELECT state, COUNT(*) AS anzahl FROM history GROUP BY state"):
            stats["by_state"][row["state"]] = int(row["anzahl"])
            stats["total"] += int(row["anzahl"])

        cutoff = (datetime.now() - timedelta(days=30)).strftime(TIMESTAMP_FORMAT)
        for row in self._query(
            "SELECT state, COUNT(*) AS anzahl FROM history WHERE timestamp >= ? GROUP BY state",
            (cutoff,),
        ):
            stats["last_30_days"]["by_state"][row["state"]] = int(row["anzahl"])
            stats["last_30_days"]["total"] += int(row["anzahl"])

        rows = self._query("SELECT MIN(timestamp) AS erster, MAX(timestamp) AS letzter FROM history")
        if rows:
            stats["first_entry"] = rows[0]["erster"] or ""
            stats["last_entry"] = rows[0]["letzter"] or ""
        rows = self._query("SELECT COUNT(*) AS anzahl FROM run")
        if rows:
            stats["runs"] = int(rows[0]["anzahl"])
        return stats

    def distinct_vendors(self) -> list[dict]:
        """Alle in der Historie vorkommenden Lieferanten (fuer Filterlisten)."""
        return self._query(
            "SELECT vendor_number, vendor_name, COUNT(*) AS anzahl FROM history "
            "WHERE vendor_name <> '' OR vendor_number <> '' "
            "GROUP BY vendor_number, vendor_name ORDER BY vendor_name COLLATE NOCASE"
        )

    # -- Export ----------------------------------------------------------
    def export_history_csv(self, path: Path, filters: dict | None = None) -> int:
        """Historie als CSV fuer deutsches Excel schreiben.

        UTF-8 *mit* BOM und Semikolon als Trenner -- nur so oeffnet Excel die
        Datei ohne Importassistent und mit korrekten Umlauten.
        Rueckgabe: Anzahl exportierter Zeilen (-1 bei Schreibfehler).
        """
        target = Path(path)
        rows = self.history({**(filters or {}), "limit": (filters or {}).get("limit") or 1_000_000})
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL,
                                    lineterminator="\r\n")
                writer.writerow([header for _, header in _CSV_HEADERS])
                for row in rows:
                    writer.writerow([self._csv_value(row, key) for key, _ in _CSV_HEADERS])
        except OSError:
            logger.exception("CSV-Export nach %s fehlgeschlagen", target)
            return -1
        logger.info("%s Protokollzeilen nach %s exportiert", len(rows), target)
        return len(rows)

    @staticmethod
    def _csv_value(row: dict, key: str) -> str:
        """Einen Datenbankwert fuer die CSV-Spalte aufbereiten."""
        value = row.get(key, "")
        if key == "timestamp":
            return _german_datetime(str(value))
        if key == "offer_date":
            return _german_date(str(value))
        if key == "dry_run":
            return "ja" if value else "nein"
        if key == "action":
            return ACTION_LABELS.get(str(value), str(value))
        if key == "state":
            return STATE_LABELS.get(str(value), str(value))
        if key in ("message", "detail"):
            # Zeilenumbrueche wuerden die Tabelle in Excel zerreissen
            return normalize_whitespace(value)
        return "" if value is None else str(value)

    # ==================================================================
    # Zuordnungen Lieferant
    # ==================================================================
    def save_vendor_mapping(
        self,
        match_type: str,
        match_value: str,
        vendor_number: str,
        vendor_name: str = "",
        confidence: float = 1.0,
        created_by: str = "",
    ) -> int:
        """Lieferantenzuordnung anlegen oder aktualisieren (Upsert).

        ``use_count`` bleibt beim Aktualisieren erhalten -- die Statistik,
        wie oft eine Zuordnung schon geholfen hat, darf nicht verlorengehen.
        """
        key = normalize_vendor_match_value(match_type, match_value)
        number = normalize_vendor_number(vendor_number)
        if not key or not number:
            logger.warning(
                "Lieferantenzuordnung ohne Schluessel oder Nummer wird verworfen (%r/%r)",
                match_value, vendor_number,
            )
            return 0
        stamp = now_text()
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO mapping_vendor (match_type, match_value, vendor_number, "
                    "vendor_name, confidence, created_at, updated_at, use_count, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?) "
                    "ON CONFLICT (match_type, match_value) DO UPDATE SET "
                    "vendor_number = excluded.vendor_number, "
                    "vendor_name = CASE WHEN excluded.vendor_name <> '' "
                    "THEN excluded.vendor_name ELSE mapping_vendor.vendor_name END, "
                    "confidence = excluded.confidence, updated_at = excluded.updated_at",
                    (str(match_type).lower(), key, number, vendor_name or "",
                     float(confidence), stamp, stamp, created_by or ""),
                )
                row = conn.execute(
                    "SELECT id FROM mapping_vendor WHERE match_type = ? AND match_value = ?",
                    (str(match_type).lower(), key),
                ).fetchone()
                return int(row["id"]) if row else 0
        except sqlite3.Error:
            logger.exception("Lieferantenzuordnung konnte nicht gespeichert werden")
            return 0

    def find_vendor_mapping(self, match_type: str, match_value: str) -> dict | None:
        """Zuordnung suchen und ihre Nutzung mitzaehlen."""
        key = normalize_vendor_match_value(match_type, match_value)
        if not key:
            return None
        rows = self._query(
            "SELECT * FROM mapping_vendor WHERE match_type = ? AND match_value = ?",
            (str(match_type).lower(), key),
        )
        if not rows:
            return None
        entry = rows[0]
        self._execute(
            "UPDATE mapping_vendor SET use_count = use_count + 1 WHERE id = ?",
            (entry["id"],),
        )
        entry["use_count"] = int(entry.get("use_count") or 0) + 1
        return entry

    def all_vendor_mappings(self, match_type: str = "") -> list[dict]:
        if match_type:
            return self._query(
                "SELECT * FROM mapping_vendor WHERE match_type = ? "
                "ORDER BY vendor_name COLLATE NOCASE, match_value",
                (str(match_type).lower(),),
            )
        return self._query(
            "SELECT * FROM mapping_vendor ORDER BY vendor_name COLLATE NOCASE, match_value"
        )

    def delete_vendor_mapping(self, mapping_id: int) -> bool:
        return self._execute("DELETE FROM mapping_vendor WHERE id = ?", (int(mapping_id),)) > 0

    # ==================================================================
    # Zuordnungen Material
    # ==================================================================
    def save_material_mapping(
        self,
        vendor_number: str,
        match_type: str,
        match_value: str,
        material_number: str,
        description: str = "",
        confidence: float = 1.0,
    ) -> int:
        """Materialzuordnung anlegen oder aktualisieren (Upsert)."""
        key = normalize_material_match_value(match_type, match_value)
        material = normalize_material_number(material_number)
        vendor = normalize_vendor_number(vendor_number)
        if not key or not material:
            logger.warning(
                "Materialzuordnung ohne Schluessel oder Materialnummer wird verworfen (%r/%r)",
                match_value, material_number,
            )
            return 0
        stamp = now_text()
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO mapping_material (vendor_number, match_type, match_value, "
                    "material_number, description, confidence, created_at, updated_at, use_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT (vendor_number, match_type, match_value) DO UPDATE SET "
                    "material_number = excluded.material_number, "
                    "description = CASE WHEN excluded.description <> '' "
                    "THEN excluded.description ELSE mapping_material.description END, "
                    "confidence = excluded.confidence, updated_at = excluded.updated_at",
                    (vendor, str(match_type).lower(), key, material, description or "",
                     float(confidence), stamp, stamp),
                )
                row = conn.execute(
                    "SELECT id FROM mapping_material WHERE vendor_number = ? "
                    "AND match_type = ? AND match_value = ?",
                    (vendor, str(match_type).lower(), key),
                ).fetchone()
                return int(row["id"]) if row else 0
        except sqlite3.Error:
            logger.exception("Materialzuordnung konnte nicht gespeichert werden")
            return 0

    def find_material_mapping(
        self, vendor_number: str, match_type: str, match_value: str
    ) -> dict | None:
        """Materialzuordnung suchen (erst lieferantenspezifisch, dann global)."""
        key = normalize_material_match_value(match_type, match_value)
        if not key:
            return None
        vendor = normalize_vendor_number(vendor_number)
        candidates = [vendor, ""] if vendor else [""]
        for candidate in candidates:
            rows = self._query(
                "SELECT * FROM mapping_material WHERE vendor_number = ? "
                "AND match_type = ? AND match_value = ?",
                (candidate, str(match_type).lower(), key),
            )
            if not rows:
                continue
            entry = rows[0]
            self._execute(
                "UPDATE mapping_material SET use_count = use_count + 1 WHERE id = ?",
                (entry["id"],),
            )
            entry["use_count"] = int(entry.get("use_count") or 0) + 1
            return entry
        return None

    def all_material_mappings(self, vendor_number: str = "") -> list[dict]:
        if vendor_number:
            vendor = normalize_vendor_number(vendor_number)
            return self._query(
                "SELECT * FROM mapping_material WHERE vendor_number IN (?, '') "
                "ORDER BY material_number, match_value",
                (vendor,),
            )
        return self._query(
            "SELECT * FROM mapping_material ORDER BY vendor_number, material_number, match_value"
        )

    def delete_material_mapping(self, mapping_id: int) -> bool:
        return self._execute("DELETE FROM mapping_material WHERE id = ?", (int(mapping_id),)) > 0

    # ==================================================================
    # Layoutprofile
    # ==================================================================
    def load_profiles(self, vendor_key: str = "") -> list[dict]:
        """Profile laden; ``payload`` kommt bereits als Woerterbuch zurueck."""
        if vendor_key:
            rows = self._query(
                "SELECT * FROM vendor_profile WHERE vendor_key = ? ORDER BY updated_at DESC",
                (normalize_whitespace(vendor_key).lower(),),
            )
        else:
            rows = self._query("SELECT * FROM vendor_profile ORDER BY updated_at DESC")
        for row in rows:
            row["payload"] = self._loads(row.get("payload"), {})
        return rows

    def load_profile(self, profile_id: str) -> dict | None:
        rows = self._query("SELECT * FROM vendor_profile WHERE profile_id = ?", (profile_id,))
        if not rows:
            return None
        rows[0]["payload"] = self._loads(rows[0].get("payload"), {})
        return rows[0]

    def save_profile(
        self,
        profile_id: str,
        vendor_key: str,
        vendor_name: str,
        payload: dict,
        sample_count: int = 0,
        success_count: int = 0,
        correction_count: int = 0,
    ) -> bool:
        """Layoutprofil speichern (Upsert).  ``created_at`` bleibt erhalten."""
        if not profile_id:
            logger.warning("Profil ohne Kennung wird nicht gespeichert")
            return False
        try:
            payload_text = json.dumps(payload or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            logger.exception("Profil %s enthaelt nicht serialisierbare Daten", profile_id)
            return False
        stamp = now_text()
        return self._execute(
            "INSERT INTO vendor_profile (profile_id, vendor_key, vendor_name, payload, "
            "sample_count, success_count, correction_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (profile_id) DO UPDATE SET vendor_key = excluded.vendor_key, "
            "vendor_name = excluded.vendor_name, payload = excluded.payload, "
            "sample_count = excluded.sample_count, success_count = excluded.success_count, "
            "correction_count = excluded.correction_count, updated_at = excluded.updated_at",
            (
                str(profile_id),
                normalize_whitespace(vendor_key).lower(),
                vendor_name or "",
                payload_text,
                int(sample_count),
                int(success_count),
                int(correction_count),
                stamp,
                stamp,
            ),
        ) > 0

    def delete_profile(self, profile_id: str) -> bool:
        return self._execute(
            "DELETE FROM vendor_profile WHERE profile_id = ?", (str(profile_id),)
        ) > 0

    # ==================================================================
    # Anwendungszustand
    # ==================================================================
    @staticmethod
    def _loads(raw: object, default: Any) -> Any:
        if raw in (None, ""):
            return default
        try:
            return json.loads(str(raw))
        except (TypeError, ValueError):
            logger.warning("Gespeicherter Wert ist kein gueltiges JSON, Standard wird verwendet")
            return default

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM app_state WHERE key = ?", (str(key),))
        if not rows:
            return default
        return self._loads(rows[0]["value"], default)

    def set_state(self, key: str, value: Any) -> bool:
        """Wert JSON-serialisiert ablegen (Fensterposition, letzte Auswahl ...).

        Bewusst *ohne* ``default=str``: was nicht sauber als JSON zurueckgelesen
        werden kann, wird gar nicht erst gespeichert -- sonst stuende spaeter
        eine Zeichenkette da, wo ein Objekt erwartet wird.
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.exception("Zustand '%s' ist nicht serialisierbar", key)
            return False
        return self._execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (str(key), payload, now_text()),
        ) > 0

    def delete_state(self, key: str) -> bool:
        return self._execute("DELETE FROM app_state WHERE key = ?", (str(key),)) > 0

    # ==================================================================
    # Wartung
    # ==================================================================
    def vacuum(self) -> bool:
        """Datenbank komprimieren (nach groesseren Loeschungen sinnvoll)."""
        with self._lock:
            try:
                self._conn.execute("VACUUM")
                return True
            except sqlite3.Error:
                logger.exception("VACUUM fehlgeschlagen")
                return False

    def purge_history(self, older_than_days: int) -> int:
        """Protokollzeilen aelter als X Tage loeschen.

        Laeufe, zu denen danach keine Zeile mehr existiert, verschwinden
        ebenfalls.  Rueckgabe: Anzahl geloeschter Protokollzeilen.
        """
        days = int(older_than_days)
        if days < 0:
            raise ValueError("older_than_days darf nicht negativ sein")
        cutoff = (datetime.now() - timedelta(days=days)).strftime(TIMESTAMP_FORMAT)
        try:
            with self._tx() as conn:
                cursor = conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
                deleted = cursor.rowcount
                conn.execute(
                    "DELETE FROM run WHERE started_at < ? AND run_id NOT IN "
                    "(SELECT DISTINCT run_id FROM history)",
                    (cutoff,),
                )
        except sqlite3.Error:
            logger.exception("Bereinigung der Historie fehlgeschlagen")
            return 0
        logger.info("%s Protokollzeilen aelter als %s Tage geloescht", deleted, days)
        return max(deleted, 0)

    def database_size_bytes(self) -> int:
        """Groesse der Datenbank inklusive WAL-Dateien in Byte."""
        total = 0
        for candidate in (self.db_path, Path(str(self.db_path) + "-wal"),
                          Path(str(self.db_path) + "-shm")):
            try:
                if candidate.exists():
                    total += candidate.stat().st_size
            except OSError as exc:
                logger.warning("Groesse von %s nicht ermittelbar: %s", candidate, exc)
        return total
