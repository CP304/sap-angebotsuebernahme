"""SAP-Feldnamen in Klartext -- damit niemand raten muss.

Der Fehler, um den es geht
--------------------------
Die Aufzeichnung liefert Feld-IDs wie ``EINA-LIFNR``.  Die Maske zeigte
sie an, daneben den Wert, und liess den Anwender damit allein: wer nicht
taeglich in der SAP-Datenbank arbeitet, liest daraus nichts.  Uebrig
blieb, mit auffaelligen Testwerten herumzuprobieren, bis klar war,
welches Feld welches ist -- im Produktivsystem eine schlechte Idee.

Dabei sind die Namen nicht kryptisch, sondern nur abgekuerzt, und ueber
alle SAP-Anlagen hinweg dieselben.  Das laesst sich einmal hinschreiben.

Gleichzeitig gilt der Grundsatz des Projekts weiter: geraten wird nicht.
Ein unbekannter Feldname bekommt keine erfundene Bedeutung, denn die
wuerde ungeprueft bestaetigt und schriebe im Zweifel in das falsche Feld.
"""

from __future__ import annotations

import unittest

from app.sap.feldnamen import (
    FELDNAMEN,
    TABELLEN,
    beschreibe_feld,
    erklaere_feldname,
    erklaere_tabelle,
)


class FeldnamenTest(unittest.TestCase):
    """Die Felder, die in den vier Vorgaengen tatsaechlich vorkommen."""

    def test_infosatz_felder(self):
        faelle = (
            ("wnd[0]/usr/ctxtEINA-LIFNR", "Lieferantennummer"),
            ("wnd[0]/usr/ctxtEINA-MATNR", "Materialnummer"),
            ("wnd[0]/usr/ctxtEINA-IDNLF", "Materialnummer des Lieferanten"),
            ("wnd[0]/usr/txtEINE-NETPR", "Nettopreis"),
            ("wnd[0]/usr/ctxtEINE-WAERS", "Waehrung"),
            ("wnd[0]/usr/ctxtEINE-PEINH", "Preiseinheit"),
            ("wnd[0]/usr/ctxtEINE-APLFZ", "Planlieferzeit"),
            ("wnd[0]/usr/ctxtEINE-EKORG", "Einkaufsorganisation"),
        )
        for kennung, erwartet in faelle:
            with self.subTest(feld=kennung.split("/")[-1]):
                self.assertIn(erwartet, beschreibe_feld(kennung))

    def test_die_herkunft_wird_mitgenannt(self):
        """LIFNR im Infosatz und LIFNR im Beleg sind nicht dasselbe Feld."""
        self.assertIn("Einkaufsinfosatz, Grunddaten",
                      beschreibe_feld("wnd[0]/usr/ctxtEINA-LIFNR"))
        self.assertIn("Daten der Einkaufsorganisation",
                      beschreibe_feld("wnd[0]/usr/txtEINE-NETPR"))

    def test_positionstabelle_der_bestellung(self):
        """Bildstrukturen tragen eine Bildnummer im Namen."""
        kennung = ("wnd[0]/usr/tblSAPLMEGUITC_1211/"
                   "ctxtMEPO1211-EMATN[3,0]")
        beschreibung = beschreibe_feld(kennung)
        self.assertIn("Materialnummer", beschreibung)
        self.assertIn("Bestellung", beschreibung)

    def test_steuerungspraefix_stoert_nicht(self):
        """ctxt, txt, cmbx ... sagen nur, welche Art Bedienelement es ist."""
        for praefix in ("ctxt", "txt", "cmbx", "chk", ""):
            with self.subTest(praefix=praefix or "ohne"):
                self.assertIn("Lieferantennummer",
                              beschreibe_feld(f"wnd[0]/usr/{praefix}EINA-LIFNR"))

    def test_unbekanntes_feld_bekommt_keine_erfundene_bedeutung(self):
        """Kundeneigene Felder (Z*) gibt es in jeder Anlage andere."""
        self.assertEqual(beschreibe_feld("wnd[0]/usr/ctxtZZ-EIGENFELD"), "")
        self.assertEqual(erklaere_feldname("GIBTSNICHT"), "")

    def test_bekannte_tabelle_hilft_auch_ohne_bekanntes_feld(self):
        """Halbes Wissen ist hier besser als keins."""
        beschreibung = beschreibe_feld("wnd[0]/usr/ctxtEINA-ZZSONDER")
        self.assertIn("Einkaufsinfosatz", beschreibung)

    def test_leere_und_kaputte_eingaben(self):
        for kennung in ("", "wnd[0]", "wnd[0]/usr/", "---", "/"):
            with self.subTest(kennung=kennung):
                self.assertIsInstance(beschreibe_feld(kennung), str)

    def test_gross_und_kleinschreibung(self):
        self.assertEqual(erklaere_feldname("lifnr"), erklaere_feldname("LIFNR"))
        self.assertEqual(erklaere_tabelle("eina"), erklaere_tabelle("EINA"))


class VollstaendigkeitTest(unittest.TestCase):
    """Die Liste ist eine Lesehilfe -- sie muss lesbar bleiben."""

    def test_jede_beschreibung_ist_ein_wort_das_man_kennt(self):
        """Ein Kuerzel durch ein anderes zu ersetzen hilft niemandem.

        Dass eine Beschreibung genauso heisst wie das Feld, ist dabei kein
        Mangel: "Menge" fuer MENGE ist die richtige Uebersetzung und nicht
        etwa eine faule.  Gemeint ist, dass keine Abkuerzung stehen bleibt.
        """
        for name, text in FELDNAMEN.items():
            with self.subTest(feld=name):
                self.assertGreaterEqual(len(text), 5, name)
                self.assertTrue(text[0].isupper(), name)

    def test_jede_tabelle_ist_erklaert(self):
        for name, text in TABELLEN.items():
            with self.subTest(tabelle=name):
                self.assertGreaterEqual(len(text), 5, name)

    def test_alle_felder_der_vorschlagsliste_sind_erklaert(self):
        """Was einen Vorschlag bekommt, muss erst recht lesbar sein.

        Sonst stuende in der Zeile ein Vorschlag, aber daneben nichts --
        gerade dort, wo der Anwender ihn bestaetigen soll.
        """
        try:
            from app.gui.vbs_importer import _ID_HINWEISE
        except ImportError:  # pragma: no cover -- ohne PySide6
            self.skipTest("PySide6 ist nicht installiert")
        for muster, _ziel in _ID_HINWEISE:
            with self.subTest(feld=muster):
                self.assertTrue(erklaere_feldname(muster),
                                f"{muster} hat einen Vorschlag, aber keine "
                                f"Erklaerung")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
