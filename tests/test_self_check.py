"""Selbstdiagnose und Ablage nicht erkannter Angebote.

Beides existiert aus demselben Grund: Der Arbeits-PC hat keinen Zugang
nach draussen.  Die Diagnose beantwortet "ist meine Installation
vollstaendig?" vor Ort; die Ablage sorgt dafuer, dass ein gescheitertes
Angebot etwas Verwertbares hinterlaesst, das sich weitergeben laesst.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_diag_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                        # noqa: E402
from app.services.self_check import FAIL, OK, WARN, SelfCheck   # noqa: E402


class UmgebungspruefungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = Settings()
        self.settings.ensure_dirs()
        self.check = SelfCheck(self.settings)

    def test_liefert_alle_kernbereiche(self):
        namen = [e.name for e in self.check.run_all()]
        for erwartet in ("Python", "PySide6", "PyMuPDF", "Texterkennung (OCR)",
                         "Datenbank", "SAP-Modus"):
            self.assertIn(erwartet, namen)

    def test_python_hier_in_ordnung(self):
        ergebnis = next(e for e in self.check.run_all() if e.name == "Python")
        self.assertEqual(ergebnis.status, OK)

    def test_pflichtpakete_hier_in_ordnung(self):
        # In der Entwicklungsumgebung ist alles installiert -- wenn hier
        # etwas FAIL meldet, ist die Pruefung selbst kaputt.
        for name in ("PySide6", "pandas", "openpyxl", "PyMuPDF"):
            ergebnis = next(e for e in self.check.run_all() if e.name == name)
            self.assertEqual(ergebnis.status, OK, f"{name}: {ergebnis.text}")

    def test_mock_modus_ist_ok(self):
        self.settings.use_mock_sap = True
        ergebnis = next(e for e in self.check.run_all() if e.name == "SAP-Modus")
        self.assertEqual(ergebnis.status, OK)

    def test_jeder_fehlbefund_nennt_abhilfe_oder_grund(self):
        for ergebnis in self.check.run_all():
            if ergebnis.status == FAIL:
                self.assertTrue(ergebnis.detail or "fehlt" in ergebnis.text.lower(),
                                f"{ergebnis.name}: Problem ohne Wegweiser")

    def test_bericht_ist_kopierbarer_text(self):
        bericht = self.check.report_text()
        self.assertIn("Selbstdiagnose", bericht)
        self.assertIn("Python", bericht)
        self.assertNotIn("CheckResult", bericht, "kein repr-Muell im Bericht")

    def test_kaputte_datenbank_wird_gemeldet(self):
        self.settings.database_path = str(Path(_TEMP_HOME) /
                                          "gibt_es_nicht" / "tief" / "db.sqlite3")
        ergebnis = next(e for e in SelfCheck(self.settings).run_all()
                        if e.name == "Datenbank")
        self.assertEqual(ergebnis.status, FAIL)


class SelbsttestTest(unittest.TestCase):
    """Der Import-Selbsttest liest die eingebauten Beispieldateien."""

    def test_beispieldateien_werden_gelesen(self):
        ordner = Path(__file__).resolve().parents[1] / "sample_data" / "erzeugt"
        if not ordner.is_dir():
            self.skipTest("Beispieldateien nicht erzeugt")
        ergebnisse = SelfCheck(Settings()).run_import_selftest()
        self.assertGreater(len(ergebnisse), 10)
        fehler = [e for e in ergebnisse if e.status == FAIL]
        self.assertFalse(fehler,
                         "Beispieldateien muessen lesbar sein: "
                         + "; ".join(f"{e.name}: {e.text}" for e in fehler))


class AblageTest(unittest.TestCase):
    """Nicht erkannte Angebote hinterlassen Datei + Protokoll."""

    def setUp(self) -> None:
        from app.services.offer_import_service import OfferImportService

        self.settings = Settings()
        self.settings.ensure_dirs()
        self.dienst = OfferImportService(self.settings)
        # Ablage vor jedem Test leeren
        ordner = self.settings.unrecognized_dir
        if ordner.is_dir():
            for datei in ordner.iterdir():
                datei.unlink()

    def test_unlesbare_datei_landet_in_der_ablage(self):
        quelle = Path(_TEMP_HOME) / "kaputt.pdf"
        quelle.write_bytes(b"kein echtes PDF")
        angebot = self.dienst.import_file(str(quelle))
        self.assertEqual(len(angebot.positions), 0)

        abgelegt = list(self.settings.unrecognized_dir.iterdir())
        namen = [p.name for p in abgelegt]
        self.assertTrue(any("kaputt.pdf" in n for n in namen),
                        f"Originaldatei fehlt in der Ablage: {namen}")
        self.assertTrue(any("protokoll" in n for n in namen),
                        f"Protokoll fehlt in der Ablage: {namen}")

    def test_protokoll_enthaelt_die_befunde(self):
        quelle = Path(_TEMP_HOME) / "leer.txt"
        quelle.write_text("voellig belangloser Text ohne Preise", encoding="utf-8")
        self.dienst.import_file(str(quelle))
        protokolle = [p for p in self.settings.unrecognized_dir.iterdir()
                      if "protokoll" in p.name]
        self.assertTrue(protokolle)
        inhalt = protokolle[0].read_text(encoding="utf-8")
        self.assertIn("Befunde:", inhalt)
        self.assertIn("keine Position", inhalt)

    def test_anwender_erfaehrt_von_der_ablage(self):
        quelle = Path(_TEMP_HOME) / "nochmal_kaputt.pdf"
        quelle.write_bytes(b"auch kein PDF")
        angebot = self.dienst.import_file(str(quelle))
        self.assertTrue(any("abgelegt" in n for n in angebot.extraction_notes),
                        "Ohne Hinweis weiss niemand, dass die Ablage existiert")

    def test_erfolgreicher_import_landet_nicht_in_der_ablage(self):
        quelle = Path(_TEMP_HOME) / "gut.csv"
        quelle.write_text(
            "Pos;Materialnummer;Bezeichnung;Menge;ME;Preis;Waehrung\n"
            "10;4711001;Dichtring;500;ST;2,95;EUR\n", encoding="utf-8")
        angebot = self.dienst.import_file(str(quelle))
        self.assertGreater(len(angebot.positions), 0, "Testdatei muss lesbar sein")
        ablage = (list(self.settings.unrecognized_dir.iterdir())
                  if self.settings.unrecognized_dir.is_dir() else [])
        self.assertEqual(ablage, [], "Erfolge gehoeren nicht in die Ablage")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
