"""Die drei Wege, auf denen der Anwender selbst erfasst -- Ende zu Ende.

Warum diese Datei
-----------------
Bisher waren die Bausteine dieser Wege einzeln geprueft: der Parser der
Aufzeichnung, die Zerlegung eingefuegten Textes, die Kodierungserkennung.
Was fehlte, war der Weg selbst -- die Maske, die der Anwender vor sich
hat, mit den Werten, die am Ende in der Tabelle stehen.

Genau daran haengt der praktische Nutzen des Werkzeugs:

1. **Aufzeichnung (.vbs)** -- ohne die Feld-IDs aus der eigenen Anlage
   schreibt die Anwendung nichts nach SAP.
2. **Tabelle einfuegen oder laden** -- der Ausweg, wenn die automatische
   Erkennung einmal nicht greift.
3. **Schnellerfassung** -- fuer die formlose Preismitteilung, aus der
   eine einzige Position wird.

Geprueft wird jeweils bis zum sichtbaren Ergebnis: was in der Tabelle
steht, was vorgeschlagen wird, was als Position herauskommt.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_erfassung_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.config.settings import Settings  # noqa: E402
from app.utils.textkodierung import decode_bytes  # noqa: E402

#: Eine Aufzeichnung, wie der SAP-GUI-Recorder sie fuer ME11 hinterlaesst.
AUFZEICHNUNG_ME11 = '''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
session.findById("wnd[0]").maximize
session.findById("wnd[0]/tbar[0]/okcd").text = "/nme11"
session.findById("wnd[0]").sendVKey 0
session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
session.findById("wnd[0]/usr/ctxtEINE-EKORG").text = "1000"
session.findById("wnd[0]/usr/ctxtEINE-WERKS").text = "1010"
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"
session.findById("wnd[0]/usr/ctxtEINE-WAERS").text = "EUR"
session.findById("wnd[0]/tbar[0]/btn[11]").press
'''

#: Ein aus Excel kopierter Bereich.
TABELLE = ("Pos\tMaterial\tBezeichnung\tMenge\tME\tPreis\tWaehrung\n"
           "10\t4711001\tDichtring 40x52\t100\tST\t2,95\tEUR\n"
           "20\t4711002\tFlanschdichtung DN50\t50\tST\t7,40\tEUR\n")


def wie_aus_falschem_editor(text: str) -> str:
    """Text so verfaelschen, wie ein Editor es tut, der eine UTF-16-Datei
    als Windows-1252 oeffnet."""
    return (b"\xff\xfe" + text.encode("utf-16-le")).decode("cp1252")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class AufzeichnungEinlesenTest(unittest.TestCase):
    """Weg 1: die .vbs aus dem SAP GUI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _maske_mit_datei(self, rohdaten: bytes):
        from app.gui.vbs_importer import VbsImporterWidget

        ordner = Path(tempfile.mkdtemp(prefix="sap_vbs_datei_"))
        pfad = ordner / "me11_anlegen.vbs"
        pfad.write_bytes(rohdaten)
        maske = VbsImporterWidget(Settings())
        # Genau das, was "Datei oeffnen ..." tut -- ohne den Dateidialog.
        inhalt, _kodierung, warnung = decode_bytes(pfad.read_bytes())
        maske.input.setPlainText(inhalt)
        maske._kodierungshinweis = warnung
        maske.parse_input()
        return maske

    def _zuordnungen(self, maske) -> dict[str, str]:
        """Was in der Tabelle steht: Feldname -> vorgeschlagene Bedeutung."""
        ergebnis = {}
        for zeile in range(maske.table.rowCount()):
            auswahl = maske.table.cellWidget(zeile, 2)
            ergebnis[maske.table.item(zeile, 0).text()] = (
                auswahl.currentText() if isinstance(auswahl, QComboBox) else "")
        return ergebnis

    def test_utf16_aufzeichnung_fuellt_die_maske(self):
        """So schreibt der Recorder: UTF-16 mit Byte-Order-Mark."""
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))

        self.assertIn("ME11", maske.transaction_label.text())
        self.assertIn("infosatz", maske.transaction_label.text().lower())
        self.assertEqual(maske.table.rowCount(), 6)

        zuordnung = self._zuordnungen(maske)
        self.assertEqual(zuordnung["EINA-LIFNR"], "Lieferantennummer")
        self.assertEqual(zuordnung["EINA-MATNR"], "Materialnummer")
        self.assertEqual(zuordnung["EINE-EKORG"], "Einkaufsorganisation")
        self.assertEqual(zuordnung["EINE-WERKS"], "Werk")
        self.assertEqual(zuordnung["EINE-NETPR"], "Preis")
        self.assertEqual(zuordnung["EINE-WAERS"], "Waehrung")

        # Kommandofeld und Werkzeugleiste sind Navigation, keine Daten.
        self.assertNotIn("okcd", zuordnung)

    def test_werte_stehen_im_klartext_in_der_tabelle(self):
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))
        werte = {maske.table.item(z, 0).text(): maske.table.item(z, 1).text()
                 for z in range(maske.table.rowCount())}
        self.assertEqual(werte["EINA-LIFNR"], "100234")
        self.assertEqual(werte["EINE-NETPR"], "2,95")

    def test_jede_kodierung_ergibt_dieselbe_maske(self):
        varianten = (
            ("utf-16 mit Marke",
             b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le")),
            ("utf-16 ohne Marke", AUFZEICHNUNG_ME11.encode("utf-16-le")),
            ("utf-8", AUFZEICHNUNG_ME11.encode("utf-8")),
            ("cp1252", AUFZEICHNUNG_ME11.encode("cp1252")),
        )
        for name, rohdaten in varianten:
            with self.subTest(kodierung=name):
                maske = self._maske_mit_datei(rohdaten)
                self.assertEqual(maske.table.rowCount(), 6)
                self.assertIn("ME11", maske.transaction_label.text())

    def test_gespeicherte_zuordnung_landet_in_den_einstellungen(self):
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))
        maske.save_mapping()
        gepflegt = maske.settings.sap_field_ids
        self.assertEqual(gepflegt["wnd[0]/usr/ctxtEINA-LIFNR"], "vendor_number")
        self.assertEqual(gepflegt["wnd[0]/usr/txtEINE-NETPR"], "price")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class TabelleUebernehmenTest(unittest.TestCase):
    """Weg 2: Bereich aus Excel einfuegen oder Datei laden."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog_mit_text(self, text: str):
        from app.gui.table_import_dialog import TableImportDialog

        dialog = TableImportDialog(Settings(), "Nordtec GmbH")
        dialog.set_grid(TableImportDialog._parse_text(text))
        return dialog

    def _steckbrief(self, positionen):
        return [(p.material_number, p.description, str(p.quantity), p.uom,
                 str(p.price), p.currency) for p in positionen]

    ERWARTET = [
        ("4711001", "Dichtring 40x52", "100", "ST", "2.95", "EUR"),
        ("4711002", "Flanschdichtung DN50", "50", "ST", "7.40", "EUR"),
    ]

    def test_eingefuegter_bereich_wird_zu_positionen(self):
        dialog = self._dialog_mit_text(TABELLE)
        self.assertEqual(dialog.table.rowCount(), 3)   # Kopf + 2 Datenzeilen
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)

    def test_spalten_werden_von_selbst_vorgeschlagen(self):
        """Der Anwender soll bestaetigen, nicht von vorn zuordnen."""
        dialog = self._dialog_mit_text(TABELLE)
        self.assertTrue(dialog.result_data.column_map or dialog.build_positions())

    def test_semikolon_statt_tabulator(self):
        dialog = self._dialog_mit_text(TABELLE.replace("\t", ";"))
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)

    def test_datei_laden_in_jeder_kodierung(self):
        from app.gui.table_import_dialog import TableImportDialog

        ordner = Path(tempfile.mkdtemp(prefix="sap_tabelle_datei_"))
        varianten = (
            ("utf-8", TABELLE.encode("utf-8")),
            ("utf-16 mit Marke", b"\xff\xfe" + TABELLE.encode("utf-16-le")),
            ("utf-16 ohne Marke", TABELLE.encode("utf-16-le")),
            ("cp1252", TABELLE.encode("cp1252")),
        )
        for name, rohdaten in varianten:
            with self.subTest(kodierung=name):
                pfad = ordner / f"listexport_{len(name)}_{name[:4]}.txt"
                pfad.write_bytes(rohdaten)
                raster = TableImportDialog._read_file(str(pfad))
                self.assertEqual(len(raster), 3)
                self.assertEqual(raster[1][2], "Dichtring 40x52")

    def test_zeichensalat_ergibt_lesbare_zellen(self):
        dialog = self._dialog_mit_text(wie_aus_falschem_editor(TABELLE))
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class SchnellerfassungTest(unittest.TestCase):
    """Weg 3: von Hand tippen -- eine Zeile, Enter, fertig."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _leiste(self):
        from app.gui.quick_entry import QuickEntryBar

        leiste = QuickEntryBar()
        erfasst: list = []
        leiste.positionEntered.connect(erfasst.append)
        return leiste, erfasst

    def test_getippte_zeile_wird_zur_position(self):
        leiste, erfasst = self._leiste()
        for schluessel, wert in (("material_number", "4711003"),
                                 ("description", "Kegelstück grün"),
                                 ("quantity", "25"), ("uom", "ST"),
                                 ("price", "3,10"), ("price_unit", "1"),
                                 ("currency", "EUR")):
            leiste.edits[schluessel].setText(wert)
        leiste.commit()

        self.assertEqual(len(erfasst), 1)
        position = erfasst[0]
        self.assertEqual(position.material_number, "4711003")
        self.assertEqual(position.description, "Kegelstück grün")
        self.assertEqual(str(position.price), "3.10")
        self.assertEqual(position.currency, "EUR")

    def test_felder_sind_nach_enter_wieder_leer(self):
        """Sonst wandert der Wert der letzten Zeile in die naechste."""
        leiste, _erfasst = self._leiste()
        leiste.edits["material_number"].setText("4711003")
        leiste.edits["price"].setText("3,10")
        leiste.commit()
        self.assertTrue(all(not edit.text() for edit in leiste.edits.values()))

    def test_kopierte_excel_zeile_verteilt_sich(self):
        leiste, _erfasst = self._leiste()
        leiste._distribute("4711004\tO-Ring 25x3\t500\tST\t0,45\t1\tEUR"
                           .split("\t"))
        self.assertEqual(leiste.edits["material_number"].text(), "4711004")
        self.assertEqual(leiste.edits["description"].text(), "O-Ring 25x3")
        self.assertEqual(leiste.edits["quantity"].text(), "500")
        self.assertEqual(leiste.edits["price"].text(), "0,45")
        self.assertEqual(leiste.edits["currency"].text(), "EUR")

    def test_zeichensalat_verteilt_sich_lesbar(self):
        from app.gui.quick_entry import split_pasted_row

        leiste, _erfasst = self._leiste()
        leiste._distribute(split_pasted_row(wie_aus_falschem_editor(
            "4711005\tDichtring 60x80\t10\tST\t9,90\t1\tEUR")))
        self.assertEqual(leiste.edits["material_number"].text(), "4711005")
        self.assertEqual(leiste.edits["description"].text(), "Dichtring 60x80")
        self.assertEqual(leiste.edits["price"].text(), "9,90")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
