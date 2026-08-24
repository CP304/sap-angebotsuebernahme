"""Aufzeichnungen so lesen, wie sie tatsaechlich aussehen.

Der Fehler, um den es geht
--------------------------
Der Parser war auf genau eine Schreibweise festgelegt::

    session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"

Alles, was davon abwich, ergab null Felder -- und zwar stumm.  Fuenf
Abweichungen kommen in echten Aufzeichnungen regelmaessig vor:

* **Ein anderer Objektname.**  Der Recorder schreibt ``session``; wer die
  Aufzeichnung nachbearbeitet oder in eine Sub packt, benutzt ``sess``
  oder ``oSession``.  Am Aufruf aendert das nichts.
* **Gross- und Kleinschreibung.**  VBS unterscheidet keine; ``FindById``
  und ``.Text`` sind dasselbe wie ``findById`` und ``.text``.
* **Verkettete Aufrufe.**  Geht die Aufzeichnung ueber einen Subscreen,
  teilt der Recorder den Pfad auf zwei ``findById``-Aufrufe auf.
* **Zeilenfortsetzungen.**  Ein Unterstrich am Zeilenende setzt die
  Anweisung in der naechsten Zeile fort; zeilenweise gelesen bleiben zwei
  Bruchstuecke uebrig, von denen keines ein Treffer ist.
* **Verdoppelte Anfuehrungszeichen.**  So schreibt VBS ein
  Anfuehrungszeichen im Text: ``"Dichtring 1"" NPT"``.  Wer bis zum
  naechsten Anfuehrungszeichen liest, schneidet mitten im Kurztext ab --
  und der halbe Text landet im Infosatz.

Beide Einlesewege sind betroffen (Uebernahme-Assistent und
Selektorenpflege), deshalb pruefen die Faelle hier beide.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_vbs_varianten_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.sap.selectors import SelectorRegistry  # noqa: E402
from app.services.vbs_parser import (  # noqa: E402
    detect_transaction,
    parse_vbs_recording,
)

#: Der Kopf, den der Recorder jeder Aufzeichnung voranstellt.
RECORDER_KOPF = '''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If
If IsObject(WScript) Then
   WScript.ConnectObject session,     "on"
   WScript.ConnectObject application, "on"
End If
'''


class AufzeichnungsvariantenTest(unittest.TestCase):
    """Jede Schreibweise muss dieselben Felder ergeben."""

    def setUp(self):
        self.registry = SelectorRegistry()

    def _felder(self, vbs: str) -> dict[str, str]:
        return {f.short_id(): f.value for f in parse_vbs_recording(vbs)}

    def test_vollstaendige_recorder_aufzeichnung(self):
        vbs = RECORDER_KOPF + '''session.findById("wnd[0]").maximize
session.findById("wnd[0]/tbar[0]/okcd").text = "/nme11"
session.findById("wnd[0]").sendVKey 0
session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
session.findById("wnd[0]/usr/ctxtEINA-MATNR").caretPosition = 7
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"
session.findById("wnd[0]/tbar[0]/btn[11]").press
'''
        self.assertEqual(detect_transaction(vbs), "ME11")
        felder = self._felder(vbs)
        self.assertEqual(felder["EINA-LIFNR"], "100234")
        self.assertEqual(felder["EINA-MATNR"], "4711001")
        self.assertEqual(felder["EINE-NETPR"], "2,95")
        # Kommandofeld, Werkzeugleiste und caretPosition tragen keine Daten
        self.assertNotIn("okcd", felder)
        self.assertEqual(len(felder), 3)

    def test_objektname_ist_frei(self):
        """``session`` ist eine Variable, kein Schluesselwort."""
        for objekt in ("session", "sess", "oSession", "Session"):
            with self.subTest(objekt=objekt):
                vbs = (f'{objekt}.findById("wnd[0]/usr/ctxtEINA-LIFNR")'
                       f'.text = "100234"')
                self.assertEqual(self._felder(vbs), {"EINA-LIFNR": "100234"})
                self.assertEqual(self.registry.ids_from_vbs(vbs),
                                 ["wnd[0]/usr/ctxtEINA-LIFNR"])

    def test_gross_und_kleinschreibung(self):
        """VBS unterscheidet keine -- die Aufzeichnung ist dieselbe."""
        for zeile in (
            'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"',
            'session.FindById("wnd[0]/usr/ctxtEINA-LIFNR").Text = "100234"',
            'SESSION.FINDBYID("wnd[0]/usr/ctxtEINA-LIFNR").TEXT = "100234"',
            'session.FindById( "wnd[0]/usr/ctxtEINA-LIFNR" ) .Text  =  "100234"',
        ):
            with self.subTest(zeile=zeile[:40]):
                self.assertEqual(self._felder(zeile), {"EINA-LIFNR": "100234"})
                self.assertEqual(self.registry.ids_from_vbs(zeile),
                                 ["wnd[0]/usr/ctxtEINA-LIFNR"])

    def test_verkettete_aufrufe_ergeben_den_vollen_pfad(self):
        """Ein Subscreen teilt den Pfad auf zwei Aufrufe auf.

        Beide Schreibweisen meinen dasselbe Feld, also muss auch dieselbe
        ID herauskommen -- sonst passt eine frueher gespeicherte
        Zuordnung nicht mehr.
        """
        einteilig = ('session.findById("wnd[0]/usr/subSUB0:SAPLMEGUI:0030/'
                     'ctxtEINA-LIFNR").text = "100234"')
        zweiteilig = ('session.findById("wnd[0]/usr/subSUB0:SAPLMEGUI:0030")'
                      '.findById("ctxtEINA-LIFNR").text = "100234"')
        felder_einteilig = parse_vbs_recording(einteilig)
        felder_zweiteilig = parse_vbs_recording(zweiteilig)
        self.assertEqual(felder_einteilig[0].field_id,
                         felder_zweiteilig[0].field_id)
        self.assertEqual(felder_zweiteilig[0].value, "100234")
        self.assertEqual(self.registry.ids_from_vbs(einteilig),
                         self.registry.ids_from_vbs(zweiteilig))

    def test_zeilenfortsetzung(self):
        vbs = '''session.findById( _
   "wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
session.findById("wnd[0]/usr/txtEINE-NETPR").text _
   = "2,95"
'''
        self.assertEqual(self._felder(vbs),
                         {"EINA-LIFNR": "100234", "EINE-NETPR": "2,95"})

    def test_unterstrich_im_feldnamen_ist_keine_fortsetzung(self):
        """Nur ein Unterstrich hinter Leerraum setzt fort.

        Feldnamen enthalten Unterstriche (``tblSAPLMEGUITC_1211``).  Wuerde
        jeder als Fortsetzung gelten, zerfiele die Aufzeichnung.
        """
        vbs = ('session.findById("wnd[0]/usr/tblSAPLMEGUITC_1211/'
               'ctxtMEPO1211-EMATN[3,0]").text = "4711001"')
        felder = parse_vbs_recording(vbs)
        self.assertEqual(len(felder), 1)
        self.assertEqual(felder[0].value, "4711001")

    def test_verdoppelte_anfuehrungszeichen_im_wert(self):
        """``""`` ist in VBS ein Anfuehrungszeichen, kein Textende."""
        vbs = ('session.findById("wnd[0]/usr/ctxtEINA-TXZ01").text = '
               '"Dichtring 1"" NPT"')
        felder = parse_vbs_recording(vbs)
        self.assertEqual(felder[0].value, 'Dichtring 1" NPT')

    def test_kommentar_hinter_der_anweisung(self):
        vbs = ('session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = '
               '"100234"   \' Lieferantennummer')
        self.assertEqual(self._felder(vbs), {"EINA-LIFNR": "100234"})

    def test_auskommentierte_zeile_zaehlt_nicht(self):
        vbs = ('\' session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "999"\n'
               'REM session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "888"\n'
               'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"\n')
        self.assertEqual(self._felder(vbs), {"EINA-LIFNR": "100234"})

    def test_positionstabelle_behaelt_ihre_zeilen(self):
        """In einer Tabelle ist die Zeilennummer Teil der ID."""
        vbs = '''session.findById("wnd[0]/tbar[0]/okcd").text = "/nME21N"
session.findById("wnd[0]/usr/tblSAPLMEGUITC_1211/ctxtMEPO1211-EMATN[3,0]").text = "4711001"
session.findById("wnd[0]/usr/tblSAPLMEGUITC_1211/ctxtMEPO1211-EMATN[3,1]").text = "4711002"
'''
        self.assertEqual(detect_transaction(vbs), "ME21N")
        self.assertEqual(len(parse_vbs_recording(vbs)), 2)

    def test_popupfenster(self):
        vbs = 'session.findById("wnd[1]/usr/ctxtRM06E-EVRTN").text = "4600001234"'
        self.assertEqual(self._felder(vbs), {"RM06E-EVRTN": "4600001234"})

    def test_transaktion_in_jeder_schreibweise(self):
        faelle = (
            ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nme11"', "ME11"),
            ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME12"', "ME12"),
            ('session.startTransaction "ME31K"', "ME31K"),
            ('session.startTransaction("ME21N")', "ME21N"),
        )
        for vbs, erwartet in faelle:
            with self.subTest(erwartet=erwartet):
                self.assertEqual(detect_transaction(vbs), erwartet)

    def test_aufzeichnung_ohne_werte_ergibt_nichts(self):
        """Nur Klicks -- kein Feld, aber auch kein Absturz."""
        vbs = (RECORDER_KOPF +
               'session.findById("wnd[0]").maximize\n'
               'session.findById("wnd[0]/tbar[0]/btn[11]").press\n'
               'session.findById("wnd[0]").sendVKey 0\n')
        self.assertEqual(parse_vbs_recording(vbs), [])

    def test_leeres_feld_wird_nicht_uebernommen(self):
        """Ein geleertes Feld traegt keine Zuordnung."""
        vbs = 'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = ""'
        self.assertEqual(parse_vbs_recording(vbs), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
