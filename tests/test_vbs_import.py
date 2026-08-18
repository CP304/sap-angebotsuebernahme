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
    """Der Vorschlag stuetzt sich auf den Feldnamen, nie auf den Wert."""

    @classmethod
    def setUpClass(cls) -> None:
        from app.gui.vbs_importer import vorschlag_fuer

        cls.app = QApplication.instance() or QApplication([])
        cls.vorschlag_fuer = staticmethod(vorschlag_fuer)

    def _vorschlag(self, kennung: str, wert: str = "1") -> str:
        from app.services.vbs_parser import VbsField

        return self.vorschlag_fuer(VbsField(kennung, wert, "text"))

    def test_lieferant(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEINA-LIFNR"),
                         "vendor_number")

    def test_material(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEINA-MATNR"),
                         "material_number")

    def test_lieferantenmaterial_vor_material(self):
        """IDNLF ist das Lieferantenmaterial -- nicht unser Material."""
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtEINA-IDNLF"),
                         "vendor_material_number")

    def test_preis_und_preiseinheit(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/txtEINE-NETPR"), "price")
        self.assertEqual(self._vorschlag("wnd[0]/usr/txtEINE-PEINH"),
                         "price_unit")

    def test_unbekanntes_feld_bekommt_keinen_vorschlag(self):
        self.assertEqual(self._vorschlag("wnd[0]/usr/ctxtZZ-EIGEN"), "")

    def test_wert_beeinflusst_den_vorschlag_nicht(self):
        """"1000" kann EKorg, Werk oder Menge sein -- nicht raten."""
        ohne_hinweis = self._vorschlag("wnd[0]/usr/ctxtZZ-UNBEKANNT", "1000")
        self.assertEqual(ohne_hinweis, "",
                         "Aus dem Wert allein darf nichts geschlossen werden")

    def test_alle_vorschlaege_stehen_zur_auswahl(self):
        """Ein Vorschlag, den die Auswahlliste nicht kennt, waere unsichtbar."""
        from app.gui.vbs_importer import FIELD_MAPPINGS, _ID_HINWEISE

        auswaehlbar = {schluessel for schluessel, _ in FIELD_MAPPINGS}
        for _muster, schluessel in _ID_HINWEISE:
            self.assertIn(schluessel, auswaehlbar, schluessel)


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
        maske = self._maske()
        zuordnung = maske.current_mapping()
        self.assertEqual(zuordnung.get("wnd[0]/usr/ctxtEINA-LIFNR"),
                         "vendor_number")
        self.assertEqual(zuordnung.get("wnd[0]/usr/txtEINE-NETPR"), "price")

    def test_speichern_legt_in_den_einstellungen_ab(self):
        maske = self._maske()
        maske.save_mapping()
        gespeichert = maske.settings.sap_field_ids
        self.assertIn("wnd[0]/usr/ctxtEINA-LIFNR", gespeichert)
        self.assertEqual(gespeichert["wnd[0]/usr/ctxtEINA-LIFNR"],
                         "vendor_number")

    def test_zweite_aufzeichnung_ergaenzt_statt_zu_ersetzen(self):
        """Die vier Vorgaenge werden nacheinander aufgezeichnet."""
        maske = self._maske()
        maske.save_mapping()
        anzahl_vorher = len(maske.settings.sap_field_ids)

        maske.input.setPlainText(
            'session.findById("wnd[0]/tbar[0]/okcd").text = "/nME21N"\n'
            'session.findById("wnd[0]/usr/ctxtMEPO1211-KOSTL").text = "4711"\n')
        maske.parse_input()
        maske.save_mapping()

        self.assertIn("wnd[0]/usr/ctxtEINA-LIFNR", maske.settings.sap_field_ids,
                      "Die erste Aufzeichnung darf nicht geloescht werden")
        self.assertGreater(len(maske.settings.sap_field_ids), anzahl_vorher)

    def test_leere_eingabe_speichert_nichts(self):
        from app.config.settings import Settings

        maske = self.Widget(Settings())
        maske.save_mapping()
        self.assertEqual(maske.settings.sap_field_ids, {})

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
