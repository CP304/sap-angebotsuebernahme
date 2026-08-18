"""Wenn nichts erkannt wird, muss der Grund vorne stehen -- nicht nirgends.

Der gemeldete Fall
------------------
Ein einfaches Angebot, "nichts erkannt", und nach dem Anlernen ebenfalls
fast nichts.  Beides zusammen ist typisch fuer ein PDF ohne Textebene: die
Erkennung findet keinen Text, und das Anlernen liest die Woerter ebenfalls
aus der Textebene -- unter dem aufgezogenen Rechteck liegt also nichts.

Die Erkennung *hat* das erkannt und sauber begruendet.  Nur stand die
Begruendung in ``offer.issues``, waehrend der Leer-Dialog ausschliesslich
``offer.extraction_notes`` anzeigte -- und darin steht bloss "Import
abgeschlossen: 0 Position(en)".  Der Anwender sah damit eine Sackgasse ohne
Wegweiser und wurde zusaetzlich zum Anlernen eingeladen, das hier gar nicht
funktionieren kann.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_leer_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                        # noqa: E402
from app.models.issue import Issue, IssueSeverity               # noqa: E402
from app.models.offer import Offer                              # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402

try:
    import fitz  # noqa: F401
    HAS_PDF = True
except ImportError:  # pragma: no cover
    HAS_PDF = False

try:
    from PySide6.QtWidgets import QApplication  # noqa: F401
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def _scan_pdf(ziel: Path) -> Path:
    """Ein PDF bauen, das wie ein Scan aussieht: Bild ohne Textebene."""
    import fitz

    quelle = fitz.open()
    seite = quelle.new_page()
    seite.insert_text((72, 100), "Angebot ANG-2026-0001", fontsize=16)
    seite.insert_text((72, 140), "10  Dichtring NBR 40x52x7  500 ST  2,95 EUR", fontsize=11)
    pixmap = seite.get_pixmap(dpi=110)
    quelle.close()

    bild = fitz.open()
    breite = pixmap.width * 72 / 110
    hoehe = pixmap.height * 72 / 110
    bildseite = bild.new_page(width=breite, height=hoehe)
    bildseite.insert_image(bildseite.rect, pixmap=pixmap)
    bild.save(ziel)
    bild.close()
    return ziel


@unittest.skipUnless(HAS_PDF, "PyMuPDF ist nicht installiert")
class ScanWirdBegruendet(unittest.TestCase):
    """Die Erkennung selbst muss den Grund liefern."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pfad = _scan_pdf(Path(_TEMP_HOME) / "scan.pdf")
        cls.angebot = OfferImportService(Settings()).import_file(str(cls.pfad))

    def test_wirklich_keine_textebene(self):
        import fitz

        with fitz.open(self.pfad) as dokument:
            self.assertEqual("".join(s.get_text() for s in dokument).strip(), "",
                             "Die Testdatei ist kein echter Scan")

    def test_keine_positionen(self):
        self.assertEqual(len(self.angebot.positions), 0)

    def test_grund_steht_in_den_befunden(self):
        texte = " ".join(p.message for p in self.angebot.issues)
        self.assertIn("keinen durchsuchbaren Text", texte,
                      "Der Scan muss ausdruecklich benannt werden")

    def test_befund_nennt_den_ausweg(self):
        texte = " ".join(p.message for p in self.angebot.issues)
        self.assertTrue("Texterkennung" in texte or "OCR" in texte,
                        "Ohne Hinweis auf die Texterkennung ist der Befund eine Sackgasse")


@unittest.skipUnless(HAS_QT and HAS_PDF, "PySide6/PyMuPDF nicht installiert")
class LeerDialogZeigtDenGrund(unittest.TestCase):
    """Und das Fenster muss ihn auch anzeigen."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        from app.bootstrap import build_services
        from app.gui.main_window import MainWindow

        cls.app = QApplication.instance() or QApplication([])
        cls.settings = Settings()
        cls.settings.use_mock_sap = True
        cls.settings.dry_run = True
        cls.settings.ensure_dirs()
        cls.services = build_services(cls.settings)
        cls.window = MainWindow(cls.settings, cls.services.as_dict())
        cls.pfad = _scan_pdf(Path(_TEMP_HOME) / "scan_gui.pdf")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        if cls.services.repository is not None:
            cls.services.repository.close()

    def _angebot_mit_scan(self) -> Offer:
        angebot = OfferImportService(self.settings).import_file(str(self.pfad))
        self.window.offer = angebot
        return angebot

    def test_grund_wird_herausgesucht(self):
        self._angebot_mit_scan()
        ursache = self.window._empty_result_cause(self.window.offer)
        self.assertIn("keinen durchsuchbaren Text", ursache)

    def test_folgemeldungen_stehen_nicht_im_grund(self):
        """"Waehrung fehlt" ist eine Folge, keine Ursache -- das verwaessert nur."""
        self._angebot_mit_scan()
        ursache = self.window._empty_result_cause(self.window.offer)
        self.assertNotIn("Waehrung", ursache)

    def test_ohne_angebot_kein_grund(self):
        self.assertEqual(self.window._empty_result_cause(None), "")

    def test_angebot_ohne_befunde(self):
        self.window.offer = Offer()
        self.assertEqual(self.window._empty_result_cause(self.window.offer), "")

    def test_unbekannter_befundtyp_wird_nicht_als_ursache_gedeutet(self):
        angebot = Offer()
        angebot.issues.add(Issue("material_missing", "Material fehlt",
                                 IssueSeverity.WARNING))
        self.assertEqual(self.window._empty_result_cause(angebot), "")

    # -- Anlernen ------------------------------------------------------
    def test_anlernen_wird_bei_scan_nicht_angeboten(self):
        self._angebot_mit_scan()
        self.assertIsNone(self.window._teachable_pdf(),
                          "Anlernen auf einem Scan ist eine Sackgasse -- die "
                          "Woerter kommen aus der Textebene, die es nicht gibt")

    def test_anlernen_bleibt_bei_text_pdf_moeglich(self):
        import fitz

        ziel = Path(_TEMP_HOME) / "mit_text.pdf"
        dokument = fitz.open()
        seite = dokument.new_page()
        seite.insert_text((72, 100), "10  Dichtring NBR  500 ST  2,95 EUR", fontsize=11)
        dokument.save(ziel)
        dokument.close()

        angebot = Offer()
        angebot.source_files = [str(ziel)]
        self.window.offer = angebot
        self.assertEqual(self.window._teachable_pdf(), str(ziel))

    def test_kaputtes_pdf_sperrt_das_anlernen_nicht(self):
        """Im Zweifel anbieten: eine unpruefbare Datei ist kein Beweis."""
        ziel = Path(_TEMP_HOME) / "kaputt.pdf"
        ziel.write_bytes(b"kein echtes PDF")
        self.assertTrue(self.window._pdf_has_text(str(ziel)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
