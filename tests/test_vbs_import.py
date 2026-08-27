"""Feld-IDs aus einer beliebigen Scripting-Aufzeichnung uebernehmen.

Der Anwender soll eine .vbs einfuegen koennen, ohne zu wissen, was er
aufgezeichnet hat und wie die Felder heissen.  Also muss die Auswertung
zweierlei leisten: die Transaktion selbst erkennen, und zu jedem Feld
einen Vorschlag machen, den der Anwender nur noch bestaetigt.

Wichtig ist die Gegenrichtung: Es darf NICHT geraten werden.  Der
Vorschlag stuetzt sich ausschliesslich auf den SAP-Feldnamen (EINA-LIFNR
ist die Lieferantennummer, das ist eindeutig), niemals auf den
eingetippten Wert -- "1000" kann eine Einkaufsorganisation, ein Werk oder
eine Menge sein.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_vbs_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.services.vbs_parser import (                              # noqa: E402
    TRANSACTION_NAMES,
    detect_transaction,
    parse_vbs_recording,
)

#: Eine Aufzeichnung, wie das SAP GUI sie schreibt -- mit Vorspann,
#: Kommandofeld, Werkzeugleiste und doppelt gesetztem Preis.
AUFZEICHNUNG_ME11 = r'''
If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"
session.findById("wnd[0]").sendVKey 0
session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "0000100234"
session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
session.findById("wnd[0]/usr/ctxtEINE-EKORG").text = "1000"
session.findById("wnd[0]/usr/ctxtEINE-WERKS").text = "1000"
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"
session.findById("wnd[0]/usr/txtEINE-PEINH").text = "1"
session.findById("wnd[0]/usr/ctxtEINE-WAERS").text = "EUR"
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"
session.findById("wnd[0]/tbar[0]/btn[11]").press
'''


class TransaktionErkennenTest(unittest.TestCase):

    def test_ueber_kommandofeld(self):
        self.assertEqual(detect_transaction(AUFZEICHNUNG_ME11), "ME11")

    def test_ueber_starttransaction(self):
        self.assertEqual(
            detect_transaction('session.startTransaction "ME21N"'), "ME21N")

    def test_kontrakt(self):
        self.assertEqual(
            detect_transaction('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME31K"'),
            "ME31K")

    def test_unbekannte_transaktion_wird_nicht_behauptet(self):
        """Lieber nichts sagen als etwas Falsches."""
        self.assertEqual(
            detect_transaction('session.findById("x").text = "/nZZ99"'), "")

    def test_leerer_text(self):
        self.assertEqual(detect_transaction(""), "")

    def test_alle_genannten_transaktionen_haben_klartext(self):
        for code, name in TRANSACTION_NAMES.items():
            self.assertTrue(name, f"{code} ohne Klartext")


class AufzeichnungAuswertenTest(unittest.TestCase):

    def setUp(self) -> None:
        self.felder = parse_vbs_recording(AUFZEICHNUNG_ME11)
        self.kennungen = [f.short_id() for f in self.felder]

    def test_datenfelder_gefunden(self):
        for erwartet in ("EINA-LIFNR", "EINA-MATNR", "EINE-EKORG",
                         "EINE-WERKS", "EINE-NETPR", "EINE-WAERS"):
            self.assertIn(erwartet, self.kennungen)

    def test_kommandofeld_taucht_nicht_auf(self):
        """Das Kommandofeld ist Navigation, keine Dateneingabe."""
        self.assertNotIn("okcd", self.kennungen)
        self.assertFalse([k for k in self.kennungen if "tbar" in k])

    def test_praefix_wird_entfernt(self):
        """"txtEINE-NETPR" ist fuer den Anwender nur Rauschen."""
        self.assertIn("EINE-NETPR", self.kennungen)
        self.assertNotIn("txtEINE-NETPR", self.kennungen)

    def test_doppeltes_feld_nur_einmal(self):
        self.assertEqual(self.kennungen.count("EINE-NETPR"), 1)

    def test_werte_bleiben_erhalten(self):
        werte = {f.short_id(): f.value for f in self.felder}
        self.assertEqual(werte["EINA-LIFNR"], "0000100234")
        self.assertEqual(werte["EINE-NETPR"], "2,95")

    def test_volle_kennung_bleibt_erhalten(self):
        """Zum Schreiben wird die vollstaendige ID gebraucht."""
        feld = next(f for f in self.felder if f.short_id() == "EINA-LIFNR")
        self.assertEqual(feld.field_id, "wnd[0]/usr/ctxtEINA-LIFNR")

    def test_beliebiger_text_stuerzt_nicht_ab(self):
        for text in ("", "irgendwas", "session.findById(", "'nur ein Kommentar"):
            self.assertIsInstance(parse_vbs_recording(text), list)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class VorschlagTest(unittest.TestCase):
    """Der Vorschlag stuetzt sich auf den Feldnamen, nie auf den Wert.

    Vorgeschlagen wird ein Feld eines Bildschirms der erkannten
    Transaktion -- nicht mehr eine allgemeine Bedeutung.  Eine
    Lieferantennummer gibt es im Infosatz, im Kontrakt und in der
    Bestellung, und es sind drei verschiedene Felder mit drei
    verschiedenen IDs.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app.gui.vbs_importer import VbsImporterWidget

        cls.app = QApplication.instance() or QApplication([])
        cls.Widget = VbsImporterWidget

    def _vorschlag(self, kennung: str, transaktion: str = "ME11") -> str:
        from app.config.settings import Settings
        from app.sap.selectors import SelectorRegistry
        from app.services.vbs_parser import VbsField

        maske = self.Widget(Settings(), SelectorRegistry())
        maske.transaction = transaktion
        return maske._vorschlag_aus_registry(VbsField(kennung, "1", "text"))

    def test_lieferant(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEINA-LIFNR"),
                         "info_record_initial.vendor")

    def test_material(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEINA-MATNR"),
                         "info_record_initial.material")

    def test_preis_und_preiseinheit(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/txtEINE-NETPR"),
                         "info_record_purchasing.net_price")
        self.assertEqual(self._vorschlag("wnd[0]/usr/txtEINE-PEINH"),
                         "info_record_purchasing.price_unit")

    def test_dasselbe_feld_in_einer_anderen_transaktion(self):
        """Der Fall, an dem die flache Liste scheiterte."""
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtRM06E-LIFNR", "ME31K"),
                         "contract_initial.vendor")
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEORD-MATNR", "ME01"),
                         "source_list_initial.material")

    def test_unbekanntes_feld_bekommt_keinen_vorschlag(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtZZ-EIGEN"), "")

    def test_feld_einer_fremden_transaktion_wird_nicht_vorgeschlagen(self):
        """In ME01 gibt es keinen Nettopreis des Infosatzes."""
        self.assertEqual(self._vorschlag("wnd[0]/usr/txtEINE-NETPR", "ME01"), "")

@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class ImportMaskeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        from app.gui.vbs_importer import VbsImporterWidget

        cls.app = QApplication.instance() or QApplication([])
        cls.Widget = VbsImporterWidget

    def _maske(self):
        from app.config.settings import Settings

        maske = self.Widget(Settings())
        maske.input.setPlainText(AUFZEICHNUNG_ME11)
        maske.parse_input()
        return maske

    def test_transaktion_wird_angezeigt(self):
        maske = self._maske()
        self.assertIn("ME11", maske.transaction_label.text())
        self.assertIn("infosatz", maske.transaction_label.text().lower())

    def test_tabelle_ist_gefuellt(self):
        maske = self._maske()
        self.assertEqual(maske.table.rowCount(), len(maske.fields))
        self.assertGreater(maske.table.rowCount(), 0)

    def test_zuordnung_ist_vorbelegt(self):
        """Zugeordnet wird auf Bildschirm und Element, nicht auf eine
        allgemeine Bedeutung -- eine Lieferantennummer gibt es im
        Infosatz, im Kontrakt und in der Bestellung, und es sind drei
        verschiedene Felder."""
        maske = self._maske()
        zuordnung = maske.current_mapping()
        self.assertEqual(zuordnung.get(("info_record_initial", "vendor")),
                         "wnd[0]/usr/ctxtEINA-LIFNR")
        self.assertEqual(zuordnung.get(("info_record_purchasing", "net_price")),
                         "wnd[0]/usr/txtEINE-NETPR")

    def test_speichern_landet_dort_wo_geschrieben_wird(self):
        """Frueher endete alles in einer flachen Liste, die niemand las."""
        maske = self._maske()
        maske.save_mapping()
        self.assertEqual(
            maske.registry.get("info_record_initial", "vendor").id,
            "wnd[0]/usr/ctxtEINA-LIFNR")

    def test_uebernommene_id_gilt_als_ungeprueft(self):
        """Bis der Anwender sie bestaetigt, wird damit nicht geschrieben."""
        maske = self._maske()
        maske.save_mapping()
        self.assertFalse(
            maske.registry.get("info_record_initial", "vendor").verified)

    def test_zweite_aufzeichnung_ergaenzt_statt_zu_ersetzen(self):
        """Die vier Vorgaenge werden nacheinander aufgezeichnet."""
        maske = self._maske()
        maske.save_mapping()

        maske.input.setPlainText(
            'session.findById("wnd[0]/tbar[0]/okcd").text = "/nME01"\n'
            'session.findById("wnd[0]/usr/ctxtEORD-MATNR").text = "4711001"\n')
        maske.parse_input()
        maske.save_mapping()

        self.assertEqual(
            maske.registry.get("info_record_initial", "vendor").id,
            "wnd[0]/usr/ctxtEINA-LIFNR",
            "Die erste Aufzeichnung darf nicht geloescht werden")
        self.assertEqual(
            maske.registry.get("source_list_initial", "material").id,
            "wnd[0]/usr/ctxtEORD-MATNR")

    def test_leere_eingabe_speichert_nichts(self):
        from app.config.settings import Settings

        maske = self.Widget(Settings())
        maske.save_mapping()
        self.assertEqual(maske.current_mapping(), {})

    def test_unbrauchbarer_text_meldet_sich(self):
        from app.config.settings import Settings

        maske = self.Widget(Settings())
        maske.input.setPlainText("Das ist kein Skript.")
        maske.parse_input()
        self.assertEqual(maske.table.rowCount(), 0)
        self.assertIn("Keine Eingabefelder", maske.status_label.text())

    def test_ohne_transaktion_geht_es_trotzdem(self):
        from app.config.settings import Settings

        maske = self.Widget(Settings())
        maske.input.setPlainText(
            'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"')
        maske.parse_input()
        self.assertEqual(maske.table.rowCount(), 1)
        self.assertIn("trotzdem", maske.transaction_label.text())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
