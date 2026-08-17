"""Tests der Datenbankschicht (Schema, Repository, MappingStore).

Ausgefuehrt mit ``python -m unittest tests.test_database -v``.
Es wird ausschliesslich in einem temporaeren Verzeichnis gearbeitet -- die
echte Datenbank des Anwenders wird nie angefasst.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from app.database import schema
from app.database.mapping_store import MappingCandidate, MappingStore
from app.database.repository import (
    Repository,
    normalize_material_match_value,
    normalize_vendor_match_value,
)
from app.models.enums import ResultState, SourceKind
from app.models.offer import EmailContext, Offer
from app.models.offer_position import OfferPosition
from app.models.results import ActionResult, BatchSummary, PositionResult


# ---------------------------------------------------------------------------
# Hilfsmittel
# ---------------------------------------------------------------------------

def make_action(
    action: str = "info_record",
    state: ResultState = ResultState.SUCCESS,
    **kwargs,
) -> ActionResult:
    """Ergebnisobjekt mit sinnvollen Vorbelegungen."""
    values = {
        "message": "Infosatz aktualisiert",
        "transaction": "ME12",
        "document_number": "5300001234",
        "old_value": "10,00 EUR",
        "new_value": "12,85 EUR",
        "duration_ms": 1234,
    }
    values.update(kwargs)
    return ActionResult(action=action, state=state, **values)


def context(**kwargs) -> dict:
    base = {
        "vendor_number": "100200",
        "vendor_name": "Nordmann Industrietechnik GmbH",
        "material_number": "4711",
        "offer_number": "AN-2026-0815",
        "offer_date": date(2026, 8, 1),
    }
    base.update(kwargs)
    return base


class DatabaseTestCase(unittest.TestCase):
    """Basisklasse: frische Datenbank je Test, sauberes Aufraeumen."""

    def setUp(self) -> None:
        # Erwartete Fehlerpfade werden bewusst provoziert -- die Protokoll-
        # ausgabe wuerde den Testbericht sonst unlesbar machen.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._tmp = tempfile.TemporaryDirectory(prefix="sap_angebot_test_")
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "unterordner" / "historie.sqlite3"
        self.repo = Repository(self.db_path)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.repo.close()
        self._tmp.cleanup()

    def raw_connection(self, path: Path | None = None) -> sqlite3.Connection:
        """Direkte Verbindung fuer Pruefungen am Schema (wird sicher geschlossen)."""
        conn = sqlite3.connect(path or self.db_path)
        self.addCleanup(conn.close)
        return conn


# ---------------------------------------------------------------------------
# 1 -- Schema und Migrationen
# ---------------------------------------------------------------------------

class SchemaTests(DatabaseTestCase):

    def test_01_datenbank_und_verzeichnis_werden_angelegt(self) -> None:
        self.assertTrue(self.db_path.exists(), "Datenbankdatei fehlt")
        self.assertTrue(self.db_path.parent.is_dir(), "Verzeichnis wurde nicht angelegt")
        self.assertEqual(self.repo.schema_version, schema.SCHEMA_VERSION)

    def test_02_alle_tabellen_und_unique_indizes_vorhanden(self) -> None:
        conn = self.raw_connection()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for table in ("history", "run", "mapping_vendor", "mapping_material",
                      "vendor_profile", "app_state", "schema_info"):
            self.assertIn(table, tables)
        self.assertIn("ux_mapping_vendor", indexes)
        self.assertIn("ux_mapping_material", indexes)

    def test_03_migration_ist_wiederholbar_und_wird_nur_einmal_geschrieben(self) -> None:
        self.repo.close()
        for _ in range(3):
            repo = Repository(self.db_path)
            repo.close()
        rows = self.raw_connection().execute("SELECT version FROM schema_info").fetchall()
        self.assertEqual([row[0] for row in rows], [schema.SCHEMA_VERSION],
                         "Migration wurde mehrfach protokolliert")
        self.repo = Repository(self.db_path)

    def test_04_zusaetzliche_migration_wird_hochgezogen(self) -> None:
        """Der Mechanismus muss von der vorgefundenen Version hochmigrieren."""
        self.repo.close()
        future = schema.MIGRATIONS + [
            (2, ["ALTER TABLE history ADD COLUMN plant TEXT NOT NULL DEFAULT ''"]),
        ]
        with mock.patch.object(schema, "MIGRATIONS", future), \
                mock.patch.object(schema, "SCHEMA_VERSION", 2):
            repo = Repository(self.db_path)
            self.assertEqual(schema.current_version(repo._conn), 2)
            columns = {row[1] for row in repo._conn.execute("PRAGMA table_info(history)")}
            self.assertIn("plant", columns)
            repo.close()
        self.repo = Repository(self.db_path)

    def test_05_neuere_datenbank_wird_nicht_veraendert(self) -> None:
        """Eine Datenbank aus einer neueren Programmversion bleibt unangetastet."""
        conn = self.raw_connection()
        conn.execute("INSERT INTO schema_info (version, applied_at) VALUES (?, ?)",
                     (99, schema.now_text()))
        conn.commit()
        self.assertEqual(schema.apply_migrations(conn), 99)


# ---------------------------------------------------------------------------
# 2 -- Historie schreiben und lesen
# ---------------------------------------------------------------------------

class HistoryTests(DatabaseTestCase):

    def test_06_lauf_anlegen_und_abschliessen(self) -> None:
        run_id = self.repo.start_run(dry_run=True, mock=True, offer_number="AN-1",
                                     vendor_name="Nordmann", source_file="angebot.pdf")
        self.assertTrue(run_id)
        summary = BatchSummary(dry_run=True, started_at=datetime.now(),
                               finished_at=datetime.now())
        summary.results = [
            PositionResult(position_uid=1, actions=[make_action()]),
            PositionResult(position_uid=2, actions=[make_action(state=ResultState.FAILED)]),
            PositionResult(position_uid=3, actions=[]),
        ]
        self.repo.finish_run(run_id, summary)

        stored = self.repo.run(run_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["positions_total"], 3)
        self.assertEqual(stored["positions_success"], 1)
        self.assertEqual(stored["positions_failed"], 1)
        self.assertEqual(stored["positions_skipped"], 1)
        self.assertTrue(stored["finished_at"])
        self.assertEqual(len(self.repo.runs(limit=10)), 1)

    def test_07_aktion_protokollieren(self) -> None:
        run_id = self.repo.start_run(mock=False)
        row_id = self.repo.log_action(run_id, context(), make_action(),
                                      sap_user="MUELLERC", sap_system="P01", mode="echt")
        self.assertGreater(row_id, 0)
        rows = self.repo.history()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["vendor_number"], "100200")
        self.assertEqual(row["material_number"], "4711")
        self.assertEqual(row["offer_date"], "2026-08-01")
        self.assertEqual(row["action"], "info_record")
        self.assertEqual(row["transaction"], "ME12")
        self.assertEqual(row["state"], "success")
        self.assertEqual(row["mode"], "echt")
        self.assertEqual(row["sap_user"], "MUELLERC")
        self.assertEqual(row["duration_ms"], 1234)
        self.assertEqual(row["run_id"], run_id)

    def test_08_sap_meldungen_landen_im_detail(self) -> None:
        run_id = self.repo.start_run()
        action = make_action(detail="ME12 Bildfolge", sap_messages=["S: Infosatz geaendert"])
        self.repo.log_action(run_id, context(), action)
        row = self.repo.history()[0]
        self.assertIn("ME12 Bildfolge", row["detail"])
        self.assertIn("Infosatz geaendert", row["detail"])

    def test_09_filter_der_historie(self) -> None:
        run_a = self.repo.start_run(offer_number="AN-A")
        run_b = self.repo.start_run(offer_number="AN-B")
        now = datetime.now()
        self.repo.log_action(run_a, context(), make_action(), mode="echt",
                             timestamp=now - timedelta(days=1))
        self.repo.log_action(
            run_a,
            context(material_number="4712", vendor_name="Suedwerk Praezisionsteile GmbH",
                    vendor_number="100300"),
            make_action(action="source_list", state=ResultState.FAILED,
                        message="Orderbuch gesperrt"),
            mode="echt", timestamp=now - timedelta(days=10),
        )
        self.repo.log_action(run_b, context(material_number="4713"),
                             make_action(action="contract", state=ResultState.SIMULATED),
                             mode="mock", dry_run=True, timestamp=now - timedelta(days=40))

        self.assertEqual(len(self.repo.history()), 3)
        self.assertEqual(len(self.repo.history({"vendor": "Suedwerk"})), 1)
        self.assertEqual(len(self.repo.history({"vendor": "100200"})), 2)
        self.assertEqual(len(self.repo.history({"material": "4712"})), 1)
        self.assertEqual(len(self.repo.history({"action": "contract"})), 1)
        self.assertEqual(len(self.repo.history({"action": ["contract", "source_list"]})), 2)
        self.assertEqual(len(self.repo.history({"state": ResultState.FAILED})), 1)
        self.assertEqual(len(self.repo.history({"text": "gesperrt"})), 1)
        self.assertEqual(len(self.repo.history({"only_real": True})), 2)
        self.assertEqual(len(self.repo.history({"only_mock": True})), 1)
        self.assertEqual(len(self.repo.history({"dry_run": True})), 1)
        self.assertEqual(len(self.repo.history({"run_id": run_b})), 1)
        self.assertEqual(len(self.repo.history({"date_from": (now - timedelta(days=15)).date()})), 2)
        self.assertEqual(len(self.repo.history({"date_to": (now - timedelta(days=5)).date()})), 2)
        self.assertEqual(
            len(self.repo.history({"date_from": (now - timedelta(days=15)).date(),
                                   "date_to": (now - timedelta(days=5)).date()})), 1)
        self.assertEqual(self.repo.history_count({"state": "failed"}), 1)

    def test_10_sortierung_limit_und_offset(self) -> None:
        run_id = self.repo.start_run()
        now = datetime.now()
        for index in range(5):
            self.repo.log_action(
                run_id, context(material_number=f"47{index}"),
                make_action(message=f"Zeile {index}"),
                timestamp=now - timedelta(minutes=index),
            )
        rows = self.repo.history({"limit": 2})
        self.assertEqual([row["material_number"] for row in rows], ["470", "471"])
        rows = self.repo.history({"limit": 2, "offset": 2})
        self.assertEqual([row["material_number"] for row in rows], ["472", "473"])
        self.assertEqual(self.repo.history_count(), 5)

    def test_11_filterwerte_werden_parametrisiert(self) -> None:
        """Ein boesartiger Filterwert darf niemals als SQL ausgefuehrt werden."""
        run_id = self.repo.start_run()
        self.repo.log_action(run_id, context(), make_action())
        angriff = "'; DROP TABLE history; --"
        self.assertEqual(self.repo.history({"vendor": angriff}), [])
        self.assertEqual(self.repo.history({"text": angriff}), [])
        self.assertEqual(len(self.repo.history()), 1, "Tabelle wurde beschaedigt")

    def test_12_platzhalter_im_suchtext_sind_woertlich(self) -> None:
        run_id = self.repo.start_run()
        self.repo.log_action(run_id, context(material_number="A%B"), make_action())
        self.repo.log_action(run_id, context(material_number="AXB"), make_action())
        self.assertEqual(len(self.repo.history({"material": "A%B"})), 1)

    def test_13_historie_je_lauf_und_kennzahlen(self) -> None:
        run_a = self.repo.start_run()
        run_b = self.repo.start_run()
        self.repo.log_action(run_a, context(), make_action())
        self.repo.log_action(run_a, context(), make_action(action="source_list",
                                                           state=ResultState.SKIPPED))
        self.repo.log_action(run_b, context(), make_action(state=ResultState.FAILED))

        eintraege = self.repo.history_for_run(run_a)
        self.assertEqual(len(eintraege), 2)
        self.assertEqual([e["action"] for e in eintraege], ["info_record", "source_list"])

        stats = self.repo.history_stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_state"], {"success": 1, "skipped": 1, "failed": 1})
        self.assertEqual(stats["last_30_days"]["total"], 3)
        self.assertEqual(stats["runs"], 2)
        self.assertTrue(stats["last_entry"])

    def test_14_distinct_vendors(self) -> None:
        run_id = self.repo.start_run()
        self.repo.log_action(run_id, context(), make_action())
        self.repo.log_action(run_id, context(), make_action(action="source_list"))
        self.repo.log_action(run_id, context(vendor_number="100300",
                                             vendor_name="Suedwerk Praezisionsteile GmbH"),
                             make_action())
        vendors = self.repo.distinct_vendors()
        self.assertEqual(len(vendors), 2)
        self.assertEqual({v["vendor_name"] for v in vendors},
                         {"Nordmann Industrietechnik GmbH", "Suedwerk Praezisionsteile GmbH"})
        self.assertEqual({v["vendor_number"]: v["anzahl"] for v in vendors},
                         {"100200": 2, "100300": 1})

    def test_15_log_batch_schreibt_alles_weg(self) -> None:
        offer = Offer(vendor_name="Nordmann Industrietechnik GmbH", vendor_number="100200",
                      offer_number="AN-2026-0815", offer_date=date(2026, 8, 1),
                      source_kind=SourceKind.PDF, source_files=["C:/angebote/nordmann.pdf"],
                      email=EmailContext(from_address="einkauf@nordmann-technik.de"))
        position_a = OfferPosition(material_number="4711", vendor_number="100200")
        position_b = OfferPosition(material_number="4712", vendor_number="100200")
        offer.positions = [position_a, position_b]

        summary = BatchSummary(dry_run=False, started_at=datetime.now(),
                               finished_at=datetime.now())
        summary.results = [
            PositionResult(position_uid=position_a.uid, label="4711",
                           actions=[make_action(), make_action(action="source_list")]),
            PositionResult(position_uid=position_b.uid, label="4712",
                           actions=[make_action(state=ResultState.FAILED,
                                                message="Material gesperrt")]),
        ]
        summary.document_results = [
            make_action(action="contract", document_number="4600001111",
                        message="Kontrakt angelegt"),
        ]

        run_id = self.repo.start_run(offer=offer, mock=False)
        written = self.repo.log_batch(run_id, offer, summary, sap_user="MUELLERC",
                                      sap_system="P01", mock=False)
        self.assertEqual(written, 4)

        rows = self.repo.history_for_run(run_id)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["mode"] for row in rows}, {"echt"})
        self.assertEqual({row["material_number"] for row in rows}, {"4711", "4712", ""})
        gespeicherter_lauf = self.repo.run(run_id)
        self.assertEqual(gespeicherter_lauf["offer_number"], "AN-2026-0815")
        self.assertEqual(gespeicherter_lauf["source_file"], "C:/angebote/nordmann.pdf")
        self.assertEqual(gespeicherter_lauf["positions_total"], 2)
        self.assertEqual(gespeicherter_lauf["positions_failed"], 1)


# ---------------------------------------------------------------------------
# 3 -- CSV-Export
# ---------------------------------------------------------------------------

class CsvExportTests(DatabaseTestCase):

    def test_16_export_inhalt_und_format(self) -> None:
        run_id = self.repo.start_run(mock=False)
        self.repo.log_action(
            run_id, context(), make_action(message="Preis von 10,00 auf 12,85 geaendert"),
            sap_user="MUELLERC", sap_system="P01", mode="echt",
            timestamp=datetime(2026, 8, 17, 14, 5, 9),
        )
        ziel = self.tmp_path / "export" / "historie.csv"
        anzahl = self.repo.export_history_csv(ziel)
        self.assertEqual(anzahl, 1)
        self.assertTrue(ziel.exists())

        roh = ziel.read_bytes()
        self.assertTrue(roh.startswith(b"\xef\xbb\xbf"), "UTF-8-BOM fehlt (deutsches Excel)")
        text = roh.decode("utf-8-sig")
        self.assertIn(";", text.splitlines()[0])
        self.assertNotIn(",", text.splitlines()[0])

        with ziel.open(encoding="utf-8-sig", newline="") as handle:
            zeilen = list(csv.reader(handle, delimiter=";"))
        kopf, daten = zeilen[0], zeilen[1]
        self.assertEqual(kopf[0], "Zeitpunkt")
        self.assertIn("Lieferantennummer", kopf)
        self.assertIn("Angebotsdatum", kopf)
        self.assertIn("Alter Wert", kopf)
        spalten = dict(zip(kopf, daten))
        self.assertEqual(spalten["Zeitpunkt"], "17.08.2026 14:05")
        self.assertEqual(spalten["Angebotsdatum"], "01.08.2026")
        self.assertEqual(spalten["Aktion"], "Infosatz")
        self.assertEqual(spalten["Status"], "erfolgreich")
        self.assertEqual(spalten["Simulation"], "nein")
        self.assertEqual(spalten["Betriebsart"], "echt")
        self.assertEqual(spalten["Lieferant"], "Nordmann Industrietechnik GmbH")
        self.assertEqual(spalten["Alter Wert"], "10,00 EUR")
        self.assertEqual(spalten["Dauer (ms)"], "1234")

    def test_17_export_beachtet_filter_und_umlaute(self) -> None:
        run_id = self.repo.start_run()
        self.repo.log_action(run_id, context(vendor_name="Müller Präzision GmbH"),
                             make_action())
        self.repo.log_action(run_id, context(), make_action(state=ResultState.FAILED))
        ziel = self.tmp_path / "nur_fehler.csv"
        self.assertEqual(self.repo.export_history_csv(ziel, {"state": "failed"}), 1)
        self.assertEqual(len(ziel.read_text(encoding="utf-8-sig").strip().splitlines()), 2)

        alles = self.tmp_path / "alles.csv"
        self.repo.export_history_csv(alles)
        self.assertIn("Müller Präzision GmbH", alles.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------------
# 4 -- Zuordnungen (Repository-Ebene)
# ---------------------------------------------------------------------------

class MappingRepositoryTests(DatabaseTestCase):

    def test_18_normalisierung_ist_beim_schreiben_und_lesen_gleich(self) -> None:
        self.assertEqual(normalize_vendor_match_value("name", "  Nordmann   GmbH "),
                         "nordmann gmbh")
        self.assertEqual(normalize_vendor_match_value("domain", "Nordmann-Technik.DE"),
                         "nordmann-technik.de")
        self.assertEqual(normalize_vendor_match_value("vat_id", "de 123.456-789"),
                         "DE123456789")
        self.assertEqual(normalize_material_match_value("vendor_material", " 00047-11 "),
                         "00047-11")
        self.assertEqual(normalize_material_match_value("vendor_material", "0004711"), "4711")
        self.assertEqual(normalize_material_match_value("text", " Hydraulik  Schlauch "),
                         "hydraulik schlauch")

        self.repo.save_vendor_mapping("name", "  Nordmann  Industrietechnik GmbH ", "0100200",
                                      "Nordmann Industrietechnik GmbH")
        treffer = self.repo.find_vendor_mapping("name", "NORDMANN INDUSTRIETECHNIK GMBH")
        self.assertIsNotNone(treffer)
        self.assertEqual(treffer["vendor_number"], "100200")

    def test_19_upsert_erhaelt_use_count(self) -> None:
        mapping_id = self.repo.save_vendor_mapping("name", "Nordmann GmbH", "100200",
                                                   "Nordmann GmbH")
        self.assertGreater(mapping_id, 0)
        self.repo.find_vendor_mapping("name", "Nordmann GmbH")
        self.repo.find_vendor_mapping("name", "Nordmann GmbH")
        zwischenstand = self.repo.all_vendor_mappings()[0]
        self.assertEqual(zwischenstand["use_count"], 2)

        gleiche_id = self.repo.save_vendor_mapping("name", "nordmann gmbh", "100999",
                                                   "Nordmann Industrietechnik GmbH", 0.9)
        self.assertEqual(gleiche_id, mapping_id, "Upsert hat einen zweiten Satz angelegt")
        eintraege = self.repo.all_vendor_mappings()
        self.assertEqual(len(eintraege), 1)
        self.assertEqual(eintraege[0]["vendor_number"], "100999")
        self.assertEqual(eintraege[0]["vendor_name"], "Nordmann Industrietechnik GmbH")
        self.assertAlmostEqual(eintraege[0]["confidence"], 0.9)
        self.assertEqual(eintraege[0]["use_count"], 2, "use_count wurde zurueckgesetzt")

    def test_20_find_erhoeht_use_count_und_loeschen_funktioniert(self) -> None:
        mapping_id = self.repo.save_vendor_mapping("domain", "Nordmann-Technik.de", "100200",
                                                   "Nordmann Industrietechnik GmbH")
        treffer = self.repo.find_vendor_mapping("domain", "nordmann-technik.de")
        self.assertEqual(treffer["use_count"], 1)
        self.assertEqual(self.repo.all_vendor_mappings("domain")[0]["use_count"], 1)
        self.assertIsNone(self.repo.find_vendor_mapping("domain", "unbekannt.de"))
        self.assertIsNone(self.repo.find_vendor_mapping("domain", ""))
        self.assertTrue(self.repo.delete_vendor_mapping(mapping_id))
        self.assertFalse(self.repo.delete_vendor_mapping(mapping_id))
        self.assertEqual(self.repo.all_vendor_mappings(), [])

    def test_21_unvollstaendige_zuordnung_wird_abgewiesen(self) -> None:
        self.assertEqual(self.repo.save_vendor_mapping("name", "", "100200"), 0)
        self.assertEqual(self.repo.save_vendor_mapping("name", "Nordmann", ""), 0)
        self.assertEqual(self.repo.save_material_mapping("100200", "text", "", "4711"), 0)
        self.assertEqual(self.repo.all_vendor_mappings(), [])
        self.assertEqual(self.repo.all_material_mappings(), [])

    def test_22_material_zuordnung_lieferantenspezifisch_und_global(self) -> None:
        self.repo.save_material_mapping("100200", "vendor_material", "NX-0815", "4711",
                                        "Hydraulikschlauch DN12")
        self.repo.save_material_mapping("100300", "vendor_material", "NX-0815", "4899",
                                        "Anderer Artikel")
        self.repo.save_material_mapping("", "text", "Kugellager 6204 2RS", "5000",
                                        "Kugellager 6204 2RS")

        treffer = self.repo.find_material_mapping("100200", "vendor_material", "nx-0815")
        self.assertEqual(treffer["material_number"], "4711")
        treffer = self.repo.find_material_mapping("100300", "vendor_material", "NX-0815")
        self.assertEqual(treffer["material_number"], "4899")
        # Globaler Eintrag (ohne Lieferant) gilt fuer jeden Lieferanten
        treffer = self.repo.find_material_mapping("100200", "text", "kugellager 6204 2rs")
        self.assertEqual(treffer["material_number"], "5000")
        self.assertIsNone(self.repo.find_material_mapping("100200", "text", "gibt es nicht"))

        self.assertEqual(len(self.repo.all_material_mappings()), 3)
        self.assertEqual(len(self.repo.all_material_mappings("100200")), 2)
        eintrag = self.repo.all_material_mappings("100300")[0]
        self.assertTrue(self.repo.delete_material_mapping(eintrag["id"]))


# ---------------------------------------------------------------------------
# 5 -- MappingStore (fachliche Aufloesung)
# ---------------------------------------------------------------------------

class MappingStoreTests(DatabaseTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.store = MappingStore(self.repo)

    def test_23_hinweis_aus_dem_angebot_gewinnt(self) -> None:
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik GmbH", "100200",
                                      "Nordmann Industrietechnik GmbH")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH",
                                             email_domain="nordmann-technik.de",
                                             vendor_number_hint="0100999")
        self.assertEqual(ergebnis.vendor_number, "100999")
        self.assertEqual(ergebnis.source, "hint")
        self.assertEqual(ergebnis.confidence, 1.0)
        self.assertTrue(ergebnis.resolved)

    def test_24_domain_vor_name(self) -> None:
        self.repo.save_vendor_mapping("domain", "nordmann-technik.de", "100200",
                                      "Nordmann Industrietechnik GmbH")
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik GmbH", "100777",
                                      "Nordmann Industrietechnik GmbH")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH",
                                             email_domain="einkauf@Nordmann-Technik.de")
        self.assertEqual(ergebnis.vendor_number, "100200")
        self.assertEqual(ergebnis.source, "mapping_domain")

    def test_25_exakter_name_wird_gefunden(self) -> None:
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik GmbH", "100200",
                                      "Nordmann Industrietechnik GmbH")
        ergebnis = self.store.resolve_vendor("  nordmann   industrietechnik gmbh  ")
        self.assertEqual(ergebnis.vendor_number, "100200")
        self.assertEqual(ergebnis.source, "mapping_name")

    def test_26_fuzzy_oberhalb_des_schwellwerts(self) -> None:
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik GmbH & Co. KG",
                                      "100200", "Nordmann Industrietechnik GmbH & Co. KG")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH")
        self.assertEqual(ergebnis.source, "mapping_fuzzy")
        self.assertEqual(ergebnis.vendor_number, "100200")
        self.assertGreaterEqual(ergebnis.confidence, self.store.auto_threshold)
        # Nutzung wurde mitgezaehlt
        self.assertEqual(self.repo.all_vendor_mappings()[0]["use_count"], 1)

    def test_27_unterhalb_des_schwellwerts_nur_vorschlaege(self) -> None:
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik Vertrieb GmbH",
                                      "100200", "Nordmann Industrietechnik Vertrieb GmbH")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH")
        self.assertEqual(ergebnis.vendor_number, "", "Es wurde geraten!")
        self.assertEqual(ergebnis.source, "unresolved")
        self.assertTrue(ergebnis.needs_decision)
        self.assertEqual(len(ergebnis.candidates), 1)
        vorschlag = ergebnis.candidates[0]
        self.assertEqual(vorschlag.number, "100200")
        self.assertGreaterEqual(vorschlag.score, self.store.suggest_threshold)
        self.assertLess(vorschlag.score, self.store.auto_threshold)
        self.assertIn("%", vorschlag.display())

    def test_28_voellig_fremder_name_liefert_gar_nichts(self) -> None:
        self.repo.save_vendor_mapping("name", "Suedwerk Praezisionsteile GmbH", "100300",
                                      "Suedwerk Praezisionsteile GmbH")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH")
        self.assertEqual(ergebnis.source, "unresolved")
        self.assertEqual(ergebnis.candidates, [])
        self.assertFalse(ergebnis.needs_decision)
        self.assertEqual(ergebnis.confidence, 0.0)

    def test_29_uneindeutige_lage_wird_nicht_automatisch_entschieden(self) -> None:
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik G.m.b.H.", "100100",
                                      "Nordmann Industrietechnik G.m.b.H.")
        self.repo.save_vendor_mapping("name", "Nordmann Industrietechnik GmbH & Co. KG",
                                      "100200", "Nordmann Industrietechnik GmbH & Co. KG")
        ergebnis = self.store.resolve_vendor("Nordmann Industrietechnik GmbH")
        self.assertEqual(ergebnis.vendor_number, "")
        self.assertEqual(ergebnis.source, "unresolved")
        self.assertEqual(len(ergebnis.candidates), 2)
        self.assertFalse(MappingStore._is_unambiguous(ergebnis.candidates))
        self.assertTrue(MappingStore._is_unambiguous([ergebnis.candidates[0]]))
        self.assertTrue(MappingStore._is_unambiguous(
            [MappingCandidate(number="1", score=0.99), MappingCandidate(number="2", score=0.70)]))

    def test_30_remember_und_forget_vendor(self) -> None:
        ids = self.store.remember_vendor("Nordmann Industrietechnik GmbH", "0100200",
                                         email_domain="nordmann-technik.de",
                                         vat_id="DE 123 456 789", created_by="MUELLERC")
        self.assertEqual(len(ids), 3)
        typen = {eintrag["match_type"] for eintrag in self.store.list_vendors()}
        self.assertEqual(typen, {"name", "domain", "vat_id"})

        self.assertEqual(
            self.store.resolve_vendor("Nordmann Industrietechnik GmbH").vendor_number, "100200")
        self.assertEqual(
            self.store.resolve_vendor("", email_domain="nordmann-technik.de").vendor_number,
            "100200")
        self.assertEqual(self.repo.find_vendor_mapping("vat_id", "de123456789")["vendor_number"],
                         "100200")

        self.assertEqual(self.store.remember_vendor("Ohne Nummer", ""), [])
        for eintrag in self.store.list_vendors():
            self.assertTrue(self.store.forget_vendor(eintrag["id"]))
        self.assertEqual(self.store.list_vendors(), [])

    def test_31_material_aufloesung(self) -> None:
        self.store.remember_material("100200", "0004711", vendor_material_number="NX-0815",
                                     description="Hydraulikschlauch DN12 2SN")
        exakt = self.store.resolve_material("100200", "nx-0815", "")
        self.assertEqual(exakt.material_number, "4711")
        self.assertEqual(exakt.source, "mapping_vendor_material")
        self.assertTrue(exakt.resolved)

        ueber_text = self.store.resolve_material("100200", "", "hydraulikschlauch dn12 2sn")
        self.assertEqual(ueber_text.material_number, "4711")
        self.assertEqual(ueber_text.source, "mapping_text")

        aehnlich = self.store.resolve_material("100200", "", "Hydraulikschlauch DN 12 2SN")
        self.assertEqual(aehnlich.material_number, "4711")
        self.assertEqual(aehnlich.source, "mapping_fuzzy")

        fremd = self.store.resolve_material("100200", "", "Kugellager 6204 2RS")
        self.assertEqual(fremd.material_number, "")
        self.assertEqual(fremd.source, "unresolved")
        self.assertFalse(fremd.resolved)

        self.assertEqual(self.store.remember_material("100200", ""), [])
        eintraege = self.store.list_materials("100200")
        self.assertEqual(len(eintraege), 2)
        self.assertTrue(self.store.forget_material(eintraege[0]["id"]))

    def test_32_schwellwerte_aus_der_konfiguration(self) -> None:
        from app.config.settings import Settings

        store = MappingStore.from_settings(self.repo, Settings())
        self.assertAlmostEqual(store.auto_threshold, 0.88)
        self.assertAlmostEqual(store.suggest_threshold, 0.60)


# ---------------------------------------------------------------------------
# 6 -- Profile und Anwendungszustand
# ---------------------------------------------------------------------------

class ProfileAndStateTests(DatabaseTestCase):

    def test_33_profil_json_roundtrip(self) -> None:
        payload = {
            "spalten": {"preis": 4, "menge": 3},
            "kopfmuster": [r"Angebot\s+Nr\.?\s*(\S+)"],
            "waehrung": "EUR",
            "umlaute": "Müller & Söhne – Präzision",
            "verschachtelt": {"liste": [1, 2, {"a": None, "b": True}]},
        }
        self.assertTrue(self.repo.save_profile("nordmann-v1", "Nordmann GmbH",
                                               "Nordmann Industrietechnik GmbH", payload,
                                               sample_count=3, success_count=2,
                                               correction_count=1))
        profile = self.repo.load_profiles()
        self.assertEqual(len(profile), 1)
        self.assertEqual(profile[0]["payload"], payload)
        self.assertEqual(profile[0]["sample_count"], 3)
        self.assertEqual(profile[0]["vendor_key"], "nordmann gmbh")

        # Upsert: gleiche Kennung -> aktualisieren, nicht doppeln
        payload["waehrung"] = "USD"
        self.repo.save_profile("nordmann-v1", "Nordmann GmbH", "Nordmann Industrietechnik GmbH",
                               payload, sample_count=4, success_count=3, correction_count=1)
        self.assertEqual(len(self.repo.load_profiles()), 1)
        einzeln = self.repo.load_profile("nordmann-v1")
        self.assertEqual(einzeln["payload"]["waehrung"], "USD")
        self.assertEqual(einzeln["sample_count"], 4)

        self.assertEqual(self.repo.load_profiles("Nordmann GmbH")[0]["profile_id"], "nordmann-v1")
        self.assertEqual(self.repo.load_profiles("unbekannt"), [])
        self.assertTrue(self.repo.delete_profile("nordmann-v1"))
        self.assertEqual(self.repo.load_profiles(), [])
        self.assertIsNone(self.repo.load_profile("nordmann-v1"))

    def test_34_defektes_profil_json_wirft_nicht(self) -> None:
        self.repo.save_profile("kaputt", "x", "X", {"a": 1})
        self.repo._conn.execute("UPDATE vendor_profile SET payload = '{kein json' "
                                "WHERE profile_id = 'kaputt'")
        self.assertEqual(self.repo.load_profiles()[0]["payload"], {})
        self.assertFalse(self.repo.save_profile("", "x", "X", {}))

    def test_35_app_state(self) -> None:
        self.assertIsNone(self.repo.get_state("fenster"))
        self.assertEqual(self.repo.get_state("fenster", {"breite": 1}), {"breite": 1})
        self.assertTrue(self.repo.set_state("fenster", {"breite": 1280, "hoehe": 800,
                                                        "maximiert": False}))
        self.assertEqual(self.repo.get_state("fenster"),
                         {"breite": 1280, "hoehe": 800, "maximiert": False})
        self.repo.set_state("fenster", {"breite": 1920})
        self.assertEqual(self.repo.get_state("fenster"), {"breite": 1920})
        self.repo.set_state("zuletzt_verwendet", ["C:/a.pdf", "C:/b.xlsx"])
        self.assertEqual(self.repo.get_state("zuletzt_verwendet"), ["C:/a.pdf", "C:/b.xlsx"])
        self.assertTrue(self.repo.delete_state("fenster"))
        self.assertIsNone(self.repo.get_state("fenster"))
        self.assertFalse(self.repo.set_state("kaputt", {1, 2, 3}))


# ---------------------------------------------------------------------------
# 7 -- Nebenlaeufigkeit, Wartung, defekte Datei
# ---------------------------------------------------------------------------

class ConcurrencyTests(DatabaseTestCase):

    def test_36_mehrere_threads_schreiben_gleichzeitig(self) -> None:
        run_id = self.repo.start_run()
        fehler: list[BaseException] = []
        start = threading.Barrier(6)

        def arbeiten(nummer: int) -> None:
            try:
                start.wait(timeout=10)
                for index in range(25):
                    self.repo.log_action(
                        run_id, context(material_number=f"{nummer}-{index}"),
                        make_action(message=f"Thread {nummer} Zeile {index}"),
                    )
                    self.repo.save_vendor_mapping("name", f"Firma {nummer}", f"20{nummer}0",
                                                  f"Firma {nummer}")
                    self.repo.find_vendor_mapping("name", f"Firma {nummer}")
                    self.repo.set_state(f"zaehler-{nummer}", index)
            except BaseException as exc:  # noqa: BLE001 - Fehler in den Hauptthread tragen
                fehler.append(exc)

        threads = [threading.Thread(target=arbeiten, args=(nummer,)) for nummer in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(fehler, [], f"Fehler in Threads: {fehler}")
        self.assertEqual(self.repo.history_count(), 150)
        self.assertEqual(len(self.repo.all_vendor_mappings()), 6)
        for nummer in range(6):
            self.assertEqual(self.repo.get_state(f"zaehler-{nummer}"), 24)
            eintrag = self.repo.find_vendor_mapping("name", f"Firma {nummer}")
            self.assertEqual(eintrag["use_count"], 26)  # 25 Suchen + diese hier


class MaintenanceTests(DatabaseTestCase):

    def test_37_purge_history(self) -> None:
        alt = self.repo.start_run(offer_number="alt")
        neu = self.repo.start_run(offer_number="neu")
        now = datetime.now()
        for tage in (200, 120, 45):
            self.repo.log_action(alt, context(), make_action(),
                                 timestamp=now - timedelta(days=tage))
        self.repo.log_action(neu, context(), make_action(), timestamp=now - timedelta(days=2))
        self.repo._conn.execute("UPDATE run SET started_at = ? WHERE run_id = ?",
                                ((now - timedelta(days=300)).strftime(schema.TIMESTAMP_FORMAT),
                                 alt))

        self.assertEqual(self.repo.history_count(), 4)
        self.assertEqual(self.repo.purge_history(100), 2)
        self.assertEqual(self.repo.history_count(), 2)
        self.assertEqual(self.repo.purge_history(30), 1)
        self.assertEqual(self.repo.history_count(), 1)
        self.assertEqual([lauf["run_id"] for lauf in self.repo.runs()], [neu],
                         "Leerer Altlauf wurde nicht entfernt")
        self.assertEqual(self.repo.purge_history(365), 0)
        with self.assertRaises(ValueError):
            self.repo.purge_history(-1)

    def test_38_vacuum_und_groesse(self) -> None:
        run_id = self.repo.start_run()
        for index in range(50):
            self.repo.log_action(run_id, context(material_number=str(index)), make_action())
        groesse = self.repo.database_size_bytes()
        self.assertGreater(groesse, 0)
        self.assertTrue(self.repo.vacuum())
        self.assertGreater(self.repo.database_size_bytes(), 0)
        self.assertEqual(self.repo.history_count(), 50)

    def test_39_kontextmanager_schliesst_die_verbindung(self) -> None:
        pfad = self.tmp_path / "kontext.sqlite3"
        with Repository(pfad) as repo:
            run_id = repo.start_run()
            repo.log_action(run_id, context(), make_action())
            self.assertEqual(repo.history_count(), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            repo._conn.execute("SELECT 1")
        repo.close()  # doppeltes Schliessen ist erlaubt


class BrokenDatabaseTests(DatabaseTestCase):

    def test_40_defekte_datei_wird_beiseitegelegt(self) -> None:
        pfad = self.tmp_path / "defekt.sqlite3"
        pfad.write_bytes(b"Das ist ganz sicher keine SQLite-Datenbank\n" * 200)

        with Repository(pfad) as repo:
            self.assertTrue(repo.recovered_from_corruption)
            self.assertIsNotNone(repo.corrupt_backup_path)
            self.assertTrue(repo.corrupt_backup_path.exists(),
                            "Die defekte Datei wurde geloescht statt gesichert")
            self.assertEqual(repo.schema_version, schema.SCHEMA_VERSION)
            run_id = repo.start_run()
            repo.log_action(run_id, context(), make_action())
            self.assertEqual(repo.history_count(), 1)

    def test_41_fehler_bei_abfrage_killt_die_anwendung_nicht(self) -> None:
        """Auch wenn die Verbindung wegbricht, liefern Abfragen nur leere Listen."""
        run_id = self.repo.start_run()
        self.repo.log_action(run_id, context(), make_action())
        self.repo._conn.close()
        self.assertEqual(self.repo.history(), [])
        self.assertEqual(self.repo.history_count(), 0)
        self.assertEqual(self.repo.runs(), [])
        self.assertEqual(self.repo.all_vendor_mappings(), [])
        self.assertIsNone(self.repo.find_vendor_mapping("name", "Nordmann"))
        self.assertEqual(self.repo.save_vendor_mapping("name", "Nordmann", "100200"), 0)
        self.assertEqual(self.repo.log_action(run_id, context(), make_action()), 0)
        self.assertFalse(self.repo.set_state("x", 1))
        self.assertFalse(self.repo.vacuum())
        self.assertEqual(self.repo.purge_history(1), 0)
        self.assertEqual(self.repo.schema_version, 0)
        self.assertEqual(self.repo.history_stats()["total"], 0)
        # Ein Export ohne Daten erzeugt trotzdem eine gueltige Datei mit Kopfzeile
        ziel = self.tmp_path / "leer.csv"
        self.assertEqual(self.repo.export_history_csv(ziel), 0)
        self.assertTrue(ziel.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
