"""Tests fuer die Zeiterfassung (Einschaltzeiten, Pausen, Export).

Aufruf: ``python -m unittest tests.test_zeiterfassung -v``

Geprueft werden:
    * Pflichtpausen nach ArbZG (30 / 45 Minuten) und die Anrechnung bereits
      erfasster Pausen
    * Tages- und Wochenkennzahlen bei 38 Stunden auf Montag bis Freitag
    * Erkennung einer Luecke, wenn der Rechner am selben Tag aus war --
      einschliesslich der drei moeglichen Einordnungen
    * hartes Ausschalten: das Ende kommt aus dem letzten Herzschlag
    * Hotkey-Zerlegung und der Excel-Export
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from zeiterfassung.config import Einstellungen
from zeiterfassung.excel_export import exportieren
from zeiterfassung.hotkey import tastencodes
from zeiterfassung.rules import als_stunden, pflichtpause, wochenbeginn
from zeiterfassung.storage import ABWESEND, ARBEIT, PAUSE, Datenbank
from zeiterfassung.tracker import Zeiterfassung


def _zeit(stunde: int, minute: int = 0, tag: int = 25) -> datetime:
    return datetime(2026, 8, tag, stunde, minute)


class Pausenregel(unittest.TestCase):
    def test_schwellen(self) -> None:
        self.assertEqual(pflichtpause(timedelta(hours=6)), timedelta(0))
        self.assertEqual(pflichtpause(timedelta(hours=6, minutes=1)), timedelta(minutes=30))
        self.assertEqual(pflichtpause(timedelta(hours=9)), timedelta(minutes=30))
        self.assertEqual(pflichtpause(timedelta(hours=9, minutes=1)), timedelta(minutes=45))


class Zeiterfassungstest(unittest.TestCase):
    def setUp(self) -> None:
        self._ordner = tempfile.TemporaryDirectory()
        self.db = Datenbank(Path(self._ordner.name) / "test.db")
        self.zeit = Zeiterfassung(self.db, Einstellungen())

    def tearDown(self) -> None:
        self.db.schliessen()
        self._ordner.cleanup()

    # -- Grundfall ----------------------------------------------------------
    def test_durchgehender_tag_zieht_pflichtpause_ab(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(17))

        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18))
        self.assertEqual(tag.anwesenheit, timedelta(hours=9))
        self.assertEqual(tag.pausenabzug, timedelta(minutes=30))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=8, minutes=30))
        self.assertEqual(tag.soll, timedelta(hours=7, minutes=36))
        self.assertEqual(als_stunden(tag.saldo), "0:54 h")

    def test_kurzer_tag_ohne_pausenabzug(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(13, 30))
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(14))
        self.assertEqual(tag.pausenabzug, timedelta(0))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=5, minutes=30))
        self.assertEqual(tag.rest, timedelta(hours=2, minutes=6))

    def test_pausenautomatik_abschaltbar(self) -> None:
        self.zeit.einstellungen.pausen_automatik = False
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(17))
        self.assertEqual(self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18)).arbeitszeit, timedelta(hours=9))

    # -- Luecken ------------------------------------------------------------
    def test_luecke_am_selben_tag_wird_zur_rueckfrage(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        offen = self.zeit.starten(_zeit(13))

        self.assertEqual(len(offen), 1)
        self.assertEqual(offen[0].beginn, _zeit(12))
        self.assertEqual(offen[0].dauer, timedelta(hours=1))

    def test_luecke_ueber_nacht_wird_nicht_gefragt(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(17))
        self.assertEqual(self.zeit.starten(_zeit(8, tag=26)), [])

    def test_kurzer_aussetzer_gilt_als_arbeit(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        self.assertEqual(self.zeit.starten(_zeit(12, 2)), [])
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(12, 30))
        self.assertEqual(tag.anwesenheit, timedelta(hours=4, minutes=30))

    def test_einordnung_der_luecke_wirkt_auf_die_tageszahlen(self) -> None:
        erwartet = {
            PAUSE: timedelta(hours=8),        # 8-12 und 13-17, Pause angerechnet
            ARBEIT: timedelta(hours=8, minutes=30),
            ABWESEND: timedelta(hours=8),
        }
        for art, arbeitszeit in erwartet.items():
            with self.subTest(art=art):
                ordner = tempfile.TemporaryDirectory()
                db = Datenbank(Path(ordner.name) / "t.db")
                zeit = Zeiterfassung(db, Einstellungen())
                zeit.starten(_zeit(8))
                zeit.beenden(_zeit(12))
                offen = zeit.starten(_zeit(13))
                zeit.luecke_einordnen(offen[0], art)
                zeit.beenden(_zeit(17))
                self.assertEqual(zeit.tag(date(2026, 8, 25), jetzt=_zeit(18)).arbeitszeit, arbeitszeit)
                db.schliessen()
                ordner.cleanup()

    def test_eingeordnete_luecke_wird_nicht_erneut_gefragt(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        offen = self.zeit.starten(_zeit(13))
        self.zeit.luecke_einordnen(offen[0], PAUSE)
        self.assertTrue(self.db.ist_eingeordnet(_zeit(12), _zeit(13)))

    # -- Hartes Ausschalten -------------------------------------------------
    def test_hartes_ausschalten_endet_beim_letzten_herzschlag(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.herzschlag(_zeit(11, 30))
        # Kein sauberes Beenden -- der naechste Start raeumt auf.
        neu = Zeiterfassung(self.db, Einstellungen())
        offen = neu.starten(_zeit(12, 15))
        self.assertEqual(offen[0].beginn, _zeit(11, 30))
        sitzungen = self.db.sitzungen(_zeit(0), _zeit(23, 59))
        self.assertTrue(sitzungen[0].ende_geschaetzt)
        self.assertEqual(sitzungen[0].ende, _zeit(11, 30))

    # -- Woche --------------------------------------------------------------
    def test_wochensoll_betraegt_achtunddreissig_stunden(self) -> None:
        self.zeit.starten(_zeit(8, tag=24))
        self.zeit.beenden(_zeit(16, tag=24))
        woche = self.zeit.woche(date(2026, 8, 24), jetzt=_zeit(17, tag=24))
        self.assertEqual(woche.soll, timedelta(hours=38))
        self.assertEqual(woche.montag, date(2026, 8, 24))
        self.assertEqual(woche.arbeitszeit, timedelta(hours=7, minutes=30))
        self.assertEqual(woche.rest, timedelta(hours=30, minutes=30))

    def test_wochenbeginn_ist_montag(self) -> None:
        self.assertEqual(wochenbeginn(date(2026, 8, 30)), date(2026, 8, 24))

    # -- Kennzahlen des Mini-Fensters --------------------------------------
    def test_kennzahlen_nennen_stempelzeit_und_laufende_sitzung(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        offen = self.zeit.starten(_zeit(12, 30))
        self.zeit.luecke_einordnen(offen[0], PAUSE)
        self.zeit.herzschlag(_zeit(15))

        kennzahlen = self.zeit.kennzahlen(_zeit(15))
        self.assertEqual(kennzahlen["eingestempelt_seit"], _zeit(8))
        self.assertEqual(kennzahlen["taetig_seit"], _zeit(12, 30))
        self.assertEqual(kennzahlen["tag"].arbeitszeit, timedelta(hours=6, minutes=30))
        self.assertTrue(kennzahlen["tag"].laeuft)

    def test_feierabendprognose_beruecksichtigt_die_pausenschwelle(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.herzschlag(_zeit(12))
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(12))
        # 4 h geleistet, 3:36 h fehlen -- dabei wird die 6-Stunden-Schwelle
        # ueberschritten, die halbe Stunde Pflichtpause kommt obendrauf.
        self.assertEqual(tag.feierabend, _zeit(16, 6))

    # -- Export -------------------------------------------------------------
    def test_excel_export_enthaelt_alle_blaetter(self) -> None:
        from openpyxl import load_workbook

        self.zeit.starten(_zeit(8, tag=24))
        self.zeit.beenden(_zeit(17, tag=24))
        ziel = Path(self._ordner.name) / "export.xlsx"
        exportieren(self.zeit, date(2026, 8, 24), date(2026, 8, 28), ziel, jetzt=_zeit(18, tag=28))

        mappe = load_workbook(ziel)
        self.assertEqual(mappe.sheetnames, ["Uebersicht", "Tage", "Buchungen"])
        self.assertEqual(mappe["Tage"].max_row, 5 + 2)          # 5 Tage + Kopf + Summe
        self.assertEqual(mappe["Tage"]["G2"].value, 8.5)        # Ist am Montag
        self.assertEqual(mappe["Tage"]["H2"].value, 7.6)        # Soll am Montag


class Hotkeytest(unittest.TestCase):
    def test_zerlegung(self) -> None:
        self.assertEqual(tastencodes("Strg+Shift+Z"), [0x11, 0x10, 0x5A])
        self.assertEqual(tastencodes("Alt+F12"), [0x12, 0x7B])

    def test_unbekannte_taste_meldet_sich(self) -> None:
        with self.assertRaises(ValueError):
            tastencodes("Strg+Mondtaste")


if __name__ == "__main__":
    unittest.main()
