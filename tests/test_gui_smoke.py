"""Rauchtest der Oberflaeche.

Prueft, dass sich das Hauptfenster mit allen Seiten aufbauen laesst, ein
Angebot in die Tabelle uebernommen wird und die wichtigsten Bedienschritte
keine Ausnahme werfen.  Laeuft ohne sichtbares Fenster (Offscreen-Plattform)
und ohne SAP.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

# Muss VOR dem Import der Anwendung gesetzt werden
_TEMP_HOME = tempfile.mkdtemp(prefix="sap_gui_test_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from app.bootstrap import build_services
    from app.config.settings import Settings
    from app.gui.dialogs import ChainDialog, PreviewDialog, ResultDialog
    from app.gui.main_window import MainWindow
    from app.gui.offer_table import COLUMN_INDEX, OfferTableModel
    from app.models.enums import SourceKind
    from app.models.offer import Offer
    from app.models.offer_position import OfferPosition
    from app.models.results import BatchSummary


def _application() -> "QApplication":
    return QApplication.instance() or QApplication([])


def _demo_offer() -> Offer:
    offer = Offer(vendor_name="Muster Dichtungstechnik GmbH",
                  vendor_number="0000100234",
                  offer_number="ANG-2026-04711", currency="EUR",
                  source_kind=SourceKind.EXCEL)
    for index, (material, price) in enumerate(
            (("47110001", "12.85"), ("47110002", "8.90"), ("47110004", "3.40")), start=1):
        offer.positions.append(OfferPosition(
            position_number=str(index * 10), material_number=material,
            description=f"Testposition {index}", quantity=Decimal("100"),
            uom="ST", price=Decimal(price), price_unit=1, currency="EUR",
            vendor_number="0000100234", purchasing_org="1000", plant="1000",
        ))
    return offer


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class GuiSmokeTest(unittest.TestCase):
    """Baut das komplette Fenster auf und bedient es programmgesteuert."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _application()
        cls.settings = Settings()
        cls.settings.use_mock_sap = True
        cls.settings.dry_run = True
        cls.settings.ensure_dirs()
        cls.services = build_services(cls.settings)
        cls.window = MainWindow(cls.settings, cls.services.as_dict())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        if cls.services.repository is not None:
            cls.services.repository.close()

    # ------------------------------------------------------------------
    def test_01_arbeitsflaeche_ohne_verwaltung(self) -> None:
        """Das Hauptfenster zeigt nur die Arbeitsflaeche, keine Verwaltung."""
        self.assertFalse(hasattr(self.window, "tabs"),
                         "Die Verwaltung gehoert nicht ins Hauptfenster")
        self.assertIsNotNone(self.window.table)
        self.assertIsNotNone(self.window.details)

        titel = [t for t, _w in self.window._admin_pages]
        for erwartet in ("Historie", "Zuordnungen", "SAP-Feld-IDs",
                         "Einstellungen", "Protokoll"):
            self.assertIn(erwartet, titel)

    def test_01b_verwaltungsfenster_oeffnet(self) -> None:
        self.window.open_admin("Einstellungen")
        fenster = self.window._admin_window
        self.assertIsNotNone(fenster)
        self.assertEqual(fenster.tabs.tabText(fenster.tabs.currentIndex()),
                         "Einstellungen")
        fenster.hide()

    def test_01c_ablauf_in_drei_schritten(self) -> None:
        """Die Werkzeugleiste fuehrt durch genau drei Schritte."""
        for button in (self.window.step1_button, self.window.step2_button,
                       self.window.step3_button):
            self.assertTrue(button.text())
        self.assertIn("1", self.window.step1_button.text())
        self.assertIn("3", self.window.step3_button.text())

    def test_01d_tabelle_zeigt_standardansicht(self) -> None:
        """Standardmaessig nur die knappe Spaltenauswahl."""
        from app.gui.offer_table import COLUMNS, DETAILED_ONLY, COLUMN_INDEX

        self.assertFalse(self.window.table.detailed_columns)
        for key in DETAILED_ONLY:
            self.assertTrue(self.window.table.isColumnHidden(COLUMN_INDEX[key]),
                            f"Spalte {key} sollte in der Standardansicht verborgen sein")
        self.window.table.set_detailed_columns(True)
        for index in range(len(COLUMNS)):
            self.assertFalse(self.window.table.isColumnHidden(index))
        self.window.table.set_detailed_columns(False)

    def test_02_startprobleme_gemeldet(self) -> None:
        """Fehlende Komponenten muessen gemeldet, nicht verschwiegen werden."""
        for problem in self.services.problems:
            self.assertIsInstance(problem, str)
            self.assertTrue(problem.strip())

    def test_03_angebot_uebernehmen(self) -> None:
        self.window._offer_loaded(_demo_offer())
        self.assertEqual(self.window.table_model.rowCount(), 3)
        self.assertIsNotNone(self.window.offer)
        self.assertEqual(self.window.header_fields["offer_number"].text(),
                         "ANG-2026-04711")

    def test_04_tabelle_bearbeiten(self) -> None:
        model: OfferTableModel = self.window.table_model
        index = model.index(0, COLUMN_INDEX["price"])
        self.assertTrue(model.setData(index, "13,50"))
        self.assertEqual(model.position_at(0).price, Decimal("13.50"))

        # Ungueltige Eingabe darf den Wert nicht veraendern
        self.assertFalse(model.setData(index, "keine Zahl"))
        self.assertEqual(model.position_at(0).price, Decimal("13.50"))

    def test_05_auswahl_und_filter(self) -> None:
        model = self.window.table_model
        model.set_all_selected(False)
        self.assertEqual(model.counts()["ausgewaehlt"], 0)
        model.set_all_selected(True)
        self.assertEqual(model.counts()["ausgewaehlt"], 3)

        self.window.proxy.set_search("Testposition 2")
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.proxy.set_search("")
        self.assertEqual(self.window.proxy.rowCount(), 3)

    def test_06_detailansicht(self) -> None:
        position = self.window.table_model.position_at(0)
        self.window.details.set_position(position)
        self.assertIn(position.material_number, self.window.details.title_label.text())
        self.window.details.set_position(None)

    def test_07_sap_daten_lesen(self) -> None:
        """Mock-SAP lesen und Alt/Neu-Vergleich pruefen (ohne Thread)."""
        offer = self.window.offer
        for position in offer.positions:
            self.services.gateway.load_position_state(position)
        if self.services.comparison is not None:
            self.services.comparison.compare_offer(offer)

        erste = offer.positions[0]
        self.assertTrue(erste.sap_info_record.was_read)
        self.assertTrue(erste.sap_info_record.exists)
        self.assertIsNotNone(erste.price_change_percent)

        dritte = offer.positions[2]           # 47110004 hat keinen Infosatz
        self.assertFalse(dritte.sap_info_record.exists)

    def test_08_komplettvorgang_dialog(self) -> None:
        dialog = ChainDialog(self.settings, 3, self.window)
        geaendert = dialog.apply_to(self.window.offer.positions)
        self.assertEqual(geaendert, 3)
        dialog.deleteLater()

    def test_09_vorschau_und_ergebnis(self) -> None:
        if self.services.preview is None:
            self.skipTest("Vorschaudienst nicht verfuegbar")
        vorschau = self.services.preview.build(self.window.offer, self.settings)
        dialog = PreviewDialog(vorschau, True, True, self.window)
        self.assertIn("Position", dialog._heading_text())
        self.assertTrue(dialog._lines())
        dialog.deleteLater()

        ergebnis = ResultDialog(BatchSummary(dry_run=True), self.window)
        self.assertIn("DRY RUN", ergebnis.summary.headline())
        ergebnis.deleteLater()

    def test_10_selektorseite(self) -> None:
        """Ungepruefte Feld-IDs muessen als offen angezeigt werden."""
        seite = self.window.selector_view
        seite.reload()
        self.assertGreater(seite.tree.topLevelItemCount(), 0)
        self.assertIn("offen", seite.summary_label.text())

    def test_11_einstellungen_laden_speichern(self) -> None:
        seite = self.window.settings_view
        seite.load()
        self.assertEqual(seite.purchasing_org.text(), self.settings.purchasing.purchasing_org)
        self.assertEqual(seite.ir_valid_to.text(), "31.12.2099")

    def test_12_betriebsart_anzeige(self) -> None:
        self.window._update_mode_badges()
        self.assertIn("Testsystem", self.window.mode_badge.text())

        self.settings.use_mock_sap = False
        self.settings.dry_run = True
        self.window._update_mode_badges()
        self.assertIn("Dry Run", self.window.mode_badge.text())

        self.settings.use_mock_sap = True
        self.window._update_mode_badges()

    def test_13_position_ergaenzen_und_entfernen(self) -> None:
        model = self.window.table_model
        vorher = model.rowCount()
        neue = model.add_empty_position()
        self.assertIsNotNone(neue)
        self.assertEqual(model.rowCount(), vorher + 1)
        self.assertEqual(model.remove_positions([neue.uid]), 1)
        self.assertEqual(model.rowCount(), vorher)

    def test_14_undo(self) -> None:
        if self.services.undo is None:
            self.skipTest("Undo nicht verfuegbar")
        model = self.window.table_model
        original = model.position_at(0).description
        self.window._snapshot("Test")
        model.setData(model.index(0, COLUMN_INDEX["description"]), "geaendert")
        self.assertEqual(model.position_at(0).description, "geaendert")
        self.window._undo()
        self.assertEqual(self.window.table_model.position_at(0).description, original)

    def test_15_beispieldateien_vorhanden(self) -> None:
        ordner = Path(__file__).resolve().parent.parent / "sample_data" / "erzeugt"
        if not ordner.exists():
            self.skipTest("Beispieldateien wurden noch nicht erzeugt")
        dateien = list(ordner.iterdir())
        self.assertGreaterEqual(len(dateien), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
