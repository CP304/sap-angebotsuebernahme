"""Was nach SAP geschrieben wird -- der letzte Halt vor dem echten System.

Der Fehler, um den es geht
--------------------------
``set_text`` reichte den Wert ungeprueft an SAP weiter.  Fuer den
Normalfall ist das richtig, fuer Steuerzeichen nicht:

* Ein **Nullzeichen** ist ueber COM besonders heimtueckisch.  Der Text
  wird dort abgeschnitten, ohne dass irgendwo ein Fehler entsteht -- in
  SAP steht dann ein halber Kurztext, und niemand erfaehrt davon.
* **Zeilenumbruch und Tabulator** gehoeren nicht in ein einzeiliges
  SAP-Eingabefeld.

Steuerzeichen koennen aus jeder Richtung kommen: aus einem PDF kopierter
Text traegt Seitenvorschuebe, eine Excel-Zelle Zeilenumbrueche, eine
falsch geoeffnete UTF-16-Datei Nullzeichen.  Die Eingangswege raeumen das
auf -- diese Funktion ist die letzte Stelle, an der es noch auffallen
kann, und die einzige, die alle Wege gemeinsam haben.

Zwei Dinge zaehlen deshalb gleichermassen: dass Steuerzeichen NICHT
durchgehen, und dass ein gewoehnlicher Wert **unveraendert** bleibt --
ein Wall, der die Nutzlast verbiegt, waere schlimmer als keiner.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_feldtext_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.sap.connection import feldtext  # noqa: E402


class UnveraenderteWerteTest(unittest.TestCase):
    """Der Normalfall darf sich nicht aendern."""

    def test_gewoehnliche_werte(self):
        werte = (
            "Dichtring 40x52",
            "4711001",
            "2,95",
            "EUR",
            "ST",
            "31.12.2026",
            "100234",
            "Grösse 40 Stück à 2,95",
            'Rohr 2" NPT, verzinkt',
            "Dichtring NBR 70 Shore, -30 bis +120 °C",
            "",
        )
        for wert in werte:
            with self.subTest(wert=wert):
                text, veraendert = feldtext(wert)
                self.assertEqual(text, wert)
                self.assertFalse(veraendert)

    def test_leerraum_am_rand_bleibt(self):
        """Ueber fuehrende Nullen und Leerraum entscheidet nicht diese Stelle."""
        text, veraendert = feldtext("  0004711001  ")
        self.assertEqual(text, "  0004711001  ")
        self.assertFalse(veraendert)

    def test_none_wird_zur_leeren_zeichenkette(self):
        self.assertEqual(feldtext(None), ("", False))

    def test_zahlen_werden_zu_text(self):
        self.assertEqual(feldtext(100), ("100", False))


class SteuerzeichenTest(unittest.TestCase):
    """Was nicht durchgehen darf."""

    def test_nullzeichen_verschwindet(self):
        """Sonst schneidet COM den Text genau dort ab."""
        text, veraendert = feldtext("Dichtring 40x52\x00NBR")
        self.assertNotIn("\x00", text)
        self.assertTrue(veraendert)

    def test_zeilenumbruch_wird_zum_leerzeichen(self):
        """Nicht ersatzlos: sonst klebten zwei Woerter zusammen."""
        text, _veraendert = feldtext("Dichtring 40x52\nNBR 70 Shore")
        self.assertEqual(text, "Dichtring 40x52 NBR 70 Shore")

    def test_alle_trennenden_steuerzeichen(self):
        for zeichen in ("\t", "\n", "\r", "\r\n", "\v", "\f",
                        "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            with self.subTest(zeichen=repr(zeichen)):
                text, veraendert = feldtext(f"Dichtring{zeichen}NBR")
                self.assertEqual(text, "Dichtring NBR")
                self.assertTrue(veraendert)

    def test_kein_doppeltes_leerzeichen(self):
        text, _veraendert = feldtext("Dichtring \n NBR")
        self.assertEqual(text, "Dichtring NBR")

    def test_rand_wird_nur_bei_bereinigung_getrimmt(self):
        text, _veraendert = feldtext("\n  Dichtring  \n")
        self.assertEqual(text, "Dichtring")

    def test_zeichensalat_einer_utf16_datei(self):
        """Der Fall, der diese ganze Kette ausgeloest hat."""
        salat = (b"\xff\xfe" + "Dichtring 40x52".encode("utf-16-le")
                 ).decode("cp1252")
        text, veraendert = feldtext(salat)
        self.assertNotIn("\x00", text)
        self.assertTrue(veraendert)
        # Die Umlaut-freien Buchstaben ueberleben -- verwertbar ist das
        # Ergebnis damit noch nicht, aber es zerschiesst SAP nicht.
        self.assertIn("Dichtring", text)


class SchreibwegTest(unittest.TestCase):
    """Der Wall sitzt in set_text, nicht nur in der Hilfsfunktion."""

    def test_set_text_bereinigt_und_protokolliert(self):
        from app.sap.connection import SapGuiConnection

        geschrieben = {}

        class _Element:
            def __init__(self):
                self._text = ""

            @property
            def text(self):
                return self._text

            @text.setter
            def text(self, wert):
                self._text = wert
                geschrieben["wert"] = wert

        element = _Element()
        verbindung = SapGuiConnection.__new__(SapGuiConnection)
        verbindung.find_element = lambda _id, required=True: element
        verbindung._retry = lambda aktion, _beschreibung: aktion()
        verbindung._trace = lambda _text: None

        with self.assertLogs("app.sap.connection", level="WARNING") as protokoll:
            verbindung.set_text("wnd[0]/usr/ctxtEINA-TXZ01",
                                "Dichtring 40x52\x00 NBR")
        self.assertEqual(geschrieben["wert"], "Dichtring 40x52 NBR")
        self.assertTrue(any("Steuerzeichen" in zeile
                            for zeile in protokoll.output))

    def test_set_text_laesst_gewoehnliche_werte_in_ruhe(self):
        from app.sap.connection import SapGuiConnection

        geschrieben = {}

        class _Element:
            text = ""

            def __setattr__(self, name, wert):
                geschrieben[name] = wert
                object.__setattr__(self, name, wert)

        verbindung = SapGuiConnection.__new__(SapGuiConnection)
        verbindung.find_element = lambda _id, required=True: _Element()
        verbindung._retry = lambda aktion, _beschreibung: aktion()
        verbindung._trace = lambda _text: None

        verbindung.set_text("wnd[0]/usr/ctxtEINA-LIFNR", "100234")
        self.assertEqual(geschrieben["text"], "100234")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
