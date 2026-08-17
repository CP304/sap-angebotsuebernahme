"""Tests der Auffang-Workflows.

Wenn die automatische Erkennung nicht greift, muss der Anwender trotzdem
weiterarbeiten koennen:

    1. Tabelle einfuegen/laden und Spalten selbst zuordnen
    2. Grafisch auf dem PDF-Seitenbild anlernen

Beide Wege muessen dieselben strengen Regeln einhalten wie die Automatik:
nichts erfinden, Summenzeilen verwerfen, Fliesstext ist keine Position.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_fallback_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from app.config.settings import Settings
    from app.gui.table_import_dialog import TableImportDialog
    from app.gui.teach_dialog import ROW_ROLE, MarkedRegion, TeachDialog

BEISPIELE = Path(__file__).resolve().parent.parent / "sample_data" / "erzeugt"


def _app():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Auffang 1: Tabelle einfuegen
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class TabelleEinfuegenTest(unittest.TestCase):
    """Manuelle Spaltenzuordnung."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()
        cls.settings = Settings()
        cls.settings.ensure_dirs()

    def _dialog(self, text: str) -> TableImportDialog:
        dialog = TableImportDialog(self.settings, "Muster GmbH")
        dialog.set_grid(TableImportDialog._parse_text(text))
        return dialog

    # -- Rastererkennung ------------------------------------------------
    def test_tabulator_aus_excel(self) -> None:
        raster = TableImportDialog._parse_text(
            "Pos\tMaterial\tPreis\n10\t47110001\t12,85\n20\t47110002\t8,90")
        self.assertEqual(len(raster), 3)
        self.assertEqual(raster[1], ["10", "47110001", "12,85"])

    def test_semikolon(self) -> None:
        raster = TableImportDialog._parse_text(
            "Pos;Material;Preis\n10;47110001;12,85")
        self.assertEqual(raster[1][1], "47110001")

    def test_mehrere_leerzeichen(self) -> None:
        """Aus PDFs kopierter Text hat oft nur Leerzeichen als Trenner."""
        raster = TableImportDialog._parse_text(
            "10    47110001    Dichtring NBR    12,85\n"
            "20    47110002    O-Ring Viton     8,90")
        self.assertEqual(len(raster), 2)
        self.assertEqual(raster[0][1], "47110001")

    def test_ungleiche_spaltenzahl_wird_aufgefuellt(self) -> None:
        raster = TableImportDialog._parse_text("a\tb\tc\nd\te")
        self.assertEqual(len(raster[1]), 3)

    def test_leerer_text(self) -> None:
        self.assertEqual(TableImportDialog._parse_text("   "), [])

    # -- Kopfzeile und Vorschlag ---------------------------------------
    def test_kopfzeile_wird_erkannt(self) -> None:
        dialog = self._dialog("Pos\tMaterial\tPreis\n10\t47110001\t12,85")
        self.assertEqual(dialog.header_row.value(), 1)

    def test_ohne_kopfzeile(self) -> None:
        dialog = self._dialog("10\t47110001\t12,85\n20\t47110002\t8,90")
        self.assertEqual(dialog.header_row.value(), 0)

    def test_vorschlag_aus_ueberschriften(self) -> None:
        dialog = self._dialog(
            "Pos\tMaterialnummer\tBezeichnung\tMenge\tME\tPreis\n"
            "10\t47110001\tDichtring\t500\tST\t12,85")
        rollen = [box.currentData() for box in dialog._role_boxes]
        self.assertIn("material_number", rollen)
        self.assertIn("price", rollen)
        self.assertIn("description", rollen)

    def test_vorschlag_erkennt_kundenartikelnummer(self) -> None:
        """Unsere Materialnummer unter fremdem Namen."""
        dialog = self._dialog(
            "Pos\tArtikel-Nr.\tIhre Artikelnummer\tBezeichnung\tPreis\n"
            "10\tDR-405\t47110001\tDichtring\t12,85")
        rollen = [box.currentData() for box in dialog._role_boxes]
        self.assertEqual(rollen[2], "material_number",
                         "'Ihre Artikelnummer' muss unsere Materialnummer sein")
        self.assertEqual(rollen[1], "vendor_material_number")

    def test_doppelte_rolle_blockiert_uebernahme(self) -> None:
        dialog = self._dialog("A\tB\n47110001\t47110002")
        for box in dialog._role_boxes:
            box.setCurrentIndex(box.findData("material_number"))
        dialog._roles_changed()
        self.assertFalse(dialog.ok_button.isEnabled())

    # -- Positionsbildung ----------------------------------------------
    def _zugeordnet(self, text: str, rollen: list[str]) -> TableImportDialog:
        dialog = self._dialog(text)
        for index, rolle in enumerate(rollen):
            if index < len(dialog._role_boxes):
                box = dialog._role_boxes[index]
                box.setCurrentIndex(max(0, box.findData(rolle)))
        dialog._roles_changed()
        return dialog

    def test_positionen_werden_gebildet(self) -> None:
        dialog = self._zugeordnet(
            "Pos\tMaterial\tBezeichnung\tMenge\tME\tPreis\n"
            "10\t47110001\tDichtring NBR\t500\tST\t12,85\n"
            "20\t47110002\tO-Ring Viton\t2000\tST\t8,90",
            ["position_number", "material_number", "description", "quantity", "uom", "price"])
        positionen = dialog.build_positions()
        self.assertEqual(len(positionen), 2)
        self.assertEqual(positionen[0].material_number, "47110001")
        self.assertEqual(positionen[0].price, Decimal("12.85"))
        self.assertEqual(positionen[0].quantity, Decimal("500"))
        self.assertEqual(positionen[0].uom, "ST")

    def test_summenzeilen_werden_verworfen(self) -> None:
        dialog = self._zugeordnet(
            "Pos\tMaterial\tPreis\n"
            "10\t47110001\t12,85\n"
            "\tSumme\t1285,00\n"
            "\tMwSt 19 %\t244,15",
            ["position_number", "material_number", "price"])
        positionen = dialog.build_positions()
        self.assertEqual(len(positionen), 1)

    def test_unlesbare_werte_bleiben_leer(self) -> None:
        """Grundsatz: lieber leer als geraten."""
        dialog = self._zugeordnet(
            "Material\tPreis\n47110001\tauf Anfrage",
            ["material_number", "price"])
        positionen = dialog.build_positions()
        self.assertEqual(len(positionen), 1)
        self.assertIsNone(positionen[0].price)

    def test_englische_zahlen(self) -> None:
        dialog = self._zugeordnet(
            "Material\tPrice\n47110001\t1,234.56",
            ["material_number", "price"])
        self.assertEqual(dialog.build_positions()[0].price, Decimal("1234.56"))

    def test_herkunft_ist_manuell(self) -> None:
        from app.models.enums import FieldOrigin

        dialog = self._zugeordnet("Material\tPreis\n47110001\t12,85",
                                  ["material_number", "price"])
        position = dialog.build_positions()[0]
        self.assertIs(position.field_origins.get("material_number"), FieldOrigin.MANUAL)

    def test_leere_zeilen_werden_uebersprungen(self) -> None:
        dialog = self._zugeordnet(
            "Material\tPreis\n47110001\t12,85\n\t\n47110002\t8,90",
            ["material_number", "price"])
        self.assertEqual(len(dialog.build_positions()), 2)

    def test_zuordnung_wird_als_spaltenkarte_geliefert(self) -> None:
        dialog = self._zugeordnet(
            "Pos\tMaterialnummer\tPreis\n10\t47110001\t12,85",
            ["position_number", "material_number", "price"])
        dialog.remember_box.setChecked(True)
        dialog._accept()
        self.assertEqual(dialog.result_data.column_map.get("Materialnummer"),
                         "material_number")
        self.assertTrue(dialog.result_data.remember)


# ---------------------------------------------------------------------------
# Auffang 2: Grafisch anlernen
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class AnlernenTest(unittest.TestCase):
    """Grafisches Anlernen auf dem Beispiel-PDF.

    Layout von ``Angebot_Pumpen_Weber.pdf`` (in PDF-Punkten):
        Kopfzeile bei y=240, Positionen ab y=265 im Abstand von 20
        Spalten: Pos 50 | Material 105 | Bezeichnung 175 | Menge 330
                 ME 385 | Preis 430 | gueltig ab 490
    """

    PDF = BEISPIELE / "Angebot_Pumpen_Weber.pdf"

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()
        if not cls.PDF.exists():
            raise unittest.SkipTest("Beispiel-PDF fehlt")

    def _dialog(self, mit_feldern: bool = True) -> TeachDialog:
        dialog = TeachDialog(str(self.PDF))
        # Erste Positionszeile markieren (y 258..272 umfasst die Zeile bei y=265)
        dialog.regions.append(MarkedRegion(ROW_ROLE, 50, 258, 540, 272, page=0))
        if mit_feldern:
            dialog.regions.extend([
                MarkedRegion("position_number", 50, 258, 100, 272, page=0),
                MarkedRegion("material_number", 105, 258, 170, 272, page=0),
                MarkedRegion("description", 175, 258, 325, 272, page=0),
                MarkedRegion("quantity", 330, 258, 380, 272, page=0),
                MarkedRegion("uom", 385, 258, 425, 272, page=0),
                MarkedRegion("price", 430, 258, 485, 272, page=0),
            ])
        return dialog

    def test_pdf_laesst_sich_oeffnen(self) -> None:
        dialog = self._dialog(mit_feldern=False)
        self.assertGreaterEqual(dialog._page_count, 1)
        self.assertGreater(dialog._page_size[0], 0)

    def test_beispielzeile_wird_gelesen(self) -> None:
        dialog = self._dialog()
        zellen = dialog._example_row_cells()
        self.assertEqual(zellen.get("material_number", "").strip(), "48200110")

    def test_ankerspalte_ist_material(self) -> None:
        self.assertEqual(self._dialog()._anchor_role(), "material_number")

    def test_muster_der_ankerspalte(self) -> None:
        self.assertEqual(self._dialog()._anchor_pattern("material_number"), "ziffern")

    def test_alle_positionen_werden_gefunden(self) -> None:
        positionen = self._dialog().build_positions()
        materialien = [p.material_number for p in positionen]
        self.assertEqual(materialien, ["48200110", "48200111", "47110003"])

    def test_werte_der_ersten_position(self) -> None:
        position = self._dialog().build_positions()[0]
        self.assertEqual(position.quantity, Decimal("12"))
        self.assertEqual(position.uom, "ST")
        self.assertEqual(position.price, Decimal("1298.00"))
        self.assertIn("Kreiselpumpe", position.description)

    def test_fliesstext_unter_der_tabelle_ist_keine_position(self) -> None:
        """Der Absatz 'Preise verstehen sich netto ...' darf nicht auftauchen."""
        positionen = self._dialog().build_positions()
        self.assertEqual(len(positionen), 3)
        for position in positionen:
            self.assertNotIn("netto", position.description.lower())
            self.assertNotIn("gruessen", position.description.lower())

    def test_kopfzeile_wird_nicht_als_position_gelesen(self) -> None:
        for position in self._dialog().build_positions():
            self.assertNotEqual(position.material_number.lower(), "material")

    def test_spaltenkarte_aus_der_kopfzeile(self) -> None:
        """Aus der Geometrie wird eine normale Spaltenzuordnung fuer das Profil."""
        karte = self._dialog()._header_map()
        self.assertEqual(karte.get("Material"), "material_number")
        self.assertEqual(karte.get("Preis"), "price")

    def test_ohne_feldmarkierung_keine_positionen(self) -> None:
        self.assertEqual(self._dialog(mit_feldern=False).build_positions(), [])

    def test_zweite_zeile_bestimmt_den_zeilenabstand(self) -> None:
        dialog = self._dialog()
        dialog.regions.append(MarkedRegion(ROW_ROLE, 50, 278, 540, 292, page=0))
        _start, hoehe, _toleranz = dialog._row_geometry()
        self.assertAlmostEqual(hoehe, 20.0, delta=1.0)

    def test_geometrie_wird_relativ_gespeichert(self) -> None:
        """Spalten als Anteil der Seitenbreite -- robust gegen Formatwechsel."""
        dialog = self._dialog()
        dialog._accept()
        geometrie = dialog.result_data.geometry
        self.assertIn("material_number", geometrie)
        for x0, x1 in geometrie.values():
            self.assertGreaterEqual(x0, 0.0)
            self.assertLessEqual(x1, 1.0)
            self.assertLess(x0, x1)

    def test_ankermuster_weist_saetze_ab(self) -> None:
        self.assertFalse(TeachDialog._anchor_matches("Preise verstehen sich", "ziffern"))
        self.assertFalse(TeachDialog._anchor_matches("", "ziffern"))
        self.assertTrue(TeachDialog._anchor_matches("48200110", "ziffern"))
        self.assertFalse(TeachDialog._anchor_matches("ABC", "alphanumerisch"))
        self.assertTrue(TeachDialog._anchor_matches("DR-40527", "alphanumerisch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
