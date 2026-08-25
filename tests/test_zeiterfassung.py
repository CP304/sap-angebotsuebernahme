"""Tests fuer die Zeiterfassung (Einschaltzeiten, Pausen, Export).

Aufruf: ``python -m unittest tests.test_zeiterfassung -v``

Geprueft werden:
    * Pflichtpausen nach ArbZG (30 / 45 Minuten) und die Anrechnung bereits
      erfasster Pausen
    * Tages- und Wochenkennzahlen bei 38 Stunden auf Montag bis Freitag
    * Erkennung einer Luecke, wenn der Rechner am selben Tag aus war --
      einschliesslich der drei moeglichen Einordnungen
    * hartes Ausschalten: das Ende kommt aus dem letzten Herzschlag
    * Urlaub, Krankheit und Feiertag -- ganz- und halbtags
    * manuelle Korrektur: nachtragen, aendern, loeschen
    * Belastbarkeit: Herkunft jedes Zeitstempels, Begruendungspflicht bei
      Eingriffen von Hand und das fortgeschriebene Aenderungsprotokoll
    * Hotkey-Zerlegung und der Excel-Export mit Auswertung und Diagrammen
    * Oberflaeche (nur wenn PySide6 vorhanden ist): Absprung aus dem
      Mini-Fenster, Tagesart im Tagesdialog
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
from zeiterfassung.storage import (
    ABWESEND,
    ARBEIT,
    ARBEITSTAG,
    AUTOMATISCH,
    FEIERTAG,
    GESCHAETZT,
    KRANK,
    MANUELL,
    PAUSE,
    RUECKFRAGE,
    URLAUB,
    Datenbank,
)
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
        self.assertEqual(
            mappe.sheetnames, ["Uebersicht", "Tage", "Auswertung", "Buchungen", "Protokoll"]
        )
        self.assertEqual(mappe["Tage"].max_row, 5 + 2)          # 5 Tage + Kopf + Summe
        self.assertEqual(mappe["Tage"]["C2"].value, "Arbeitstag")
        self.assertEqual(mappe["Tage"]["H2"].value, 8.5)        # Ist am Montag
        self.assertEqual(mappe["Tage"]["I2"].value, 7.6)        # Soll am Montag

    def test_export_zeigt_abwesenheiten_und_diagramme(self) -> None:
        from openpyxl import load_workbook

        self.zeit.starten(_zeit(8, tag=24))
        self.zeit.beenden(_zeit(17, tag=24))
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 26), URLAUB, notiz="Kurzurlaub")
        self.zeit.tagesart_setzen(date(2026, 8, 27), date(2026, 8, 27), KRANK)
        ziel = Path(self._ordner.name) / "auswertung.xlsx"
        exportieren(self.zeit, date(2026, 8, 24), date(2026, 8, 28), ziel, jetzt=_zeit(18, tag=28))

        mappe = load_workbook(ziel)
        self.assertEqual(mappe["Tage"]["C3"].value, "Urlaub")
        self.assertEqual(mappe["Tage"]["L3"].value, "Kurzurlaub")
        self.assertEqual(mappe["Tage"]["H3"].value, 7.6)          # Gutschrift als Ist

        auswertung = mappe["Auswertung"]
        kennzahlen = {
            zeile[0].value: zeile[1].value
            for zeile in auswertung.iter_rows(min_row=3, max_row=20, max_col=2)
            if zeile[0].value
        }
        self.assertEqual(kennzahlen["Urlaubstage"], 2)
        self.assertEqual(kennzahlen["Krankheitstage"], 1)
        self.assertEqual(kennzahlen["Kommen im Mittel"], "08:00")

        # Ein Diagramm auf der Uebersicht, drei in der Auswertung.
        self.assertEqual(len(mappe["Uebersicht"]._charts), 1)
        self.assertEqual(len(auswertung._charts), 3)


class Abwesenheiten(unittest.TestCase):
    def setUp(self) -> None:
        self._ordner = tempfile.TemporaryDirectory()
        self.db = Datenbank(Path(self._ordner.name) / "test.db")
        self.zeit = Zeiterfassung(self.db, Einstellungen())

    def tearDown(self) -> None:
        self.db.schliessen()
        self._ordner.cleanup()

    def test_urlaub_schreibt_das_tagessoll_gut(self) -> None:
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), URLAUB, notiz="Sommer")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(20))
        self.assertEqual(tag.gutschrift, timedelta(hours=7, minutes=36))
        self.assertEqual(tag.arbeitszeit, tag.soll)
        self.assertEqual(tag.saldo, timedelta(0))
        self.assertEqual(tag.art_beschriftung, "Urlaub")
        self.assertIsNone(tag.feierabend)

    def test_ganztags_urlaub_ignoriert_die_rechnerlaufzeit(self) -> None:
        self.zeit.starten(_zeit(9))
        self.zeit.beenden(_zeit(11))
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), KRANK)
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(20))
        self.assertEqual(tag.erfasste_arbeitszeit, timedelta(0))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=7, minutes=36))

    def test_halber_urlaubstag_wird_addiert(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), URLAUB, anteil=0.5)
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(20))
        self.assertEqual(tag.gutschrift, timedelta(hours=3, minutes=48))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=7, minutes=48))

    def test_wochenende_wird_uebersprungen(self) -> None:
        # 24.08.2026 ist ein Montag -- die Woche endet am Sonntag, dem 30.
        anzahl = self.zeit.tagesart_setzen(date(2026, 8, 24), date(2026, 8, 30), URLAUB)
        self.assertEqual(anzahl, 5)
        self.assertIsNone(self.db.tagesart(date(2026, 8, 29)))
        woche = self.zeit.woche(date(2026, 8, 24), jetzt=_zeit(20, tag=30))
        self.assertEqual(woche.arbeitszeit, timedelta(hours=38))
        self.assertEqual(woche.saldo, timedelta(0))

    def test_arbeitstag_nimmt_die_abwesenheit_zurueck(self) -> None:
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), FEIERTAG)
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), ARBEITSTAG)
        self.assertIsNone(self.db.tagesart(date(2026, 8, 25)))
        self.assertEqual(self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(20)).arbeitszeit, timedelta(0))

    def test_unbekannte_tagesart_wird_abgelehnt(self) -> None:
        with self.assertRaises(ValueError):
            self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), "sabbatical")


class ManuelleKorrektur(unittest.TestCase):
    def setUp(self) -> None:
        self._ordner = tempfile.TemporaryDirectory()
        self.db = Datenbank(Path(self._ordner.name) / "test.db")
        self.zeit = Zeiterfassung(self.db, Einstellungen())

    def tearDown(self) -> None:
        self.db.schliessen()
        self._ordner.cleanup()

    def test_vergessene_zeit_nachtragen(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(7), _zeit(12), "Programm zu spaet gestartet")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(13))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=5))
        self.assertTrue(self.zeit.buchungen(date(2026, 8, 25))[0].manuell)

    def test_korrektur_der_laufenden_sitzung_schreibt_sie_fest(self) -> None:
        kennung = self.zeit.starten(_zeit(8)) or None
        laufende = self.db.letzte_sitzung_vor(_zeit(9))
        self.zeit.sitzung_korrigieren(laufende.id, _zeit(9), _zeit(15), "erst ab 9 Uhr gearbeitet")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(16))
        self.assertEqual(tag.anwesenheit, timedelta(hours=6))
        self.assertFalse(tag.laeuft)
        self.assertIsNone(self.zeit.sitzung_id)
        self.assertIsNone(kennung)

    def test_verdrehte_zeiten_werden_abgelehnt(self) -> None:
        with self.assertRaises(ValueError):
            self.zeit.sitzung_nachtragen(_zeit(12), _zeit(8), "verdreht")
        with self.assertRaises(ValueError):
            self.zeit.luecke_nachtragen(_zeit(12), _zeit(8), PAUSE, "verdreht")
        with self.assertRaises(ValueError):
            self.zeit.luecke_nachtragen(_zeit(8), _zeit(12), "kaffee", "unbekannte Art")

    def test_sitzung_loeschen(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "vergessen zu starten")
        buchung = self.zeit.buchungen(date(2026, 8, 25))[0]
        self.zeit.sitzung_verwerfen(buchung.id, "versehentlich doppelt erfasst")
        self.assertEqual(self.zeit.buchungen(date(2026, 8, 25)), [])

    def test_pause_nachtragen_und_aendern(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(17), "Tag nachgetragen")
        self.zeit.luecke_nachtragen(_zeit(12), _zeit(12, 30), PAUSE, "Mittagspause")
        luecke = [b for b in self.zeit.buchungen(date(2026, 8, 25)) if not hasattr(b, "laeuft")][0]
        self.zeit.luecke_korrigieren(luecke.id, _zeit(12), _zeit(13), PAUSE, "Mittagspause verlaengert")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18))
        # 9 h Anwesenheit, 1 h erfasste Pause deckt die Pflichtpause ab.
        self.assertEqual(tag.erfasste_pause, timedelta(hours=1))
        self.assertEqual(tag.pausenabzug, timedelta(0))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=8))

        self.zeit.luecke_verwerfen(luecke.id, "Pause doch durchgearbeitet")
        self.assertEqual(
            self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18)).arbeitszeit,
            timedelta(hours=8, minutes=30),
        )

    def test_ueberschneidende_sitzungen_zaehlen_nur_einmal(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "vergessen zu starten")
        self.zeit.sitzung_nachtragen(_zeit(11), _zeit(14), "Termin ausser Haus")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(15))
        self.assertEqual(tag.anwesenheit, timedelta(hours=6))
        self.assertEqual(tag.erste_anmeldung, _zeit(8))
        self.assertEqual(tag.letzter_kontakt, _zeit(14))

    def test_pause_innerhalb_einer_sitzung_kuerzt_die_anwesenheit(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(16), "Tag nachgetragen")
        self.zeit.luecke_nachtragen(_zeit(12), _zeit(13), PAUSE, "Mittag")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(17))
        self.assertEqual(tag.anwesenheit, timedelta(hours=7))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=7))

    def test_abwesenheit_innerhalb_einer_sitzung_zaehlt_nicht(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(17), "Tag nachgetragen")
        self.zeit.luecke_nachtragen(_zeit(14), _zeit(15), ABWESEND, "Arzttermin")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18))
        self.assertEqual(tag.anwesenheit, timedelta(hours=8))
        self.assertEqual(tag.arbeitszeit, timedelta(hours=8))

    def test_buchungen_sind_nach_uhrzeit_sortiert(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(13), _zeit(17), "Nachmittag nachgetragen")
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "vergessen zu starten")
        self.zeit.luecke_nachtragen(_zeit(12), _zeit(13), PAUSE, "Mittagspause")
        zeiten = [b.beginn.hour for b in self.zeit.buchungen(date(2026, 8, 25))]
        self.assertEqual(zeiten, [8, 12, 13])


class Nachweisbarkeit(unittest.TestCase):
    """Jeder Zeitstempel im Bericht muss sagen koennen, woher er kommt."""

    def setUp(self) -> None:
        self._ordner = tempfile.TemporaryDirectory()
        self.db = Datenbank(Path(self._ordner.name) / "test.db")
        self.zeit = Zeiterfassung(self.db, Einstellungen())

    def tearDown(self) -> None:
        self.db.schliessen()
        self._ordner.cleanup()

    # -- Herkunft -----------------------------------------------------------
    def test_rechnerlaufzeit_ist_automatisch(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(16))
        sitzung = self.zeit.buchungen(date(2026, 8, 25))[0]
        self.assertEqual(sitzung.quelle, AUTOMATISCH)
        self.assertIn("automatisch gemessen", sitzung.herkunft)
        self.assertTrue(self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(17)).vollautomatisch)

    def test_hartes_ausschalten_ist_als_schaetzung_gekennzeichnet(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.herzschlag(_zeit(11, 30))
        Zeiterfassung(self.db, Einstellungen()).starten(_zeit(12, 15))

        sitzung = self.zeit.buchungen(date(2026, 8, 25))[0]
        self.assertEqual(sitzung.quelle, GESCHAETZT)
        self.assertIn("letzten Herzschlag", sitzung.herkunft)
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(13))
        self.assertFalse(tag.vollautomatisch)
        self.assertIn("Ende geschaetzt", tag.nachweis)

    def test_eingeordnete_luecke_nennt_rueckfrage_und_grund(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        offen = self.zeit.starten(_zeit(13))
        self.zeit.luecke_einordnen(offen[0], PAUSE, "Mittagessen in der Kantine")

        luecke = [b for b in self.zeit.buchungen(date(2026, 8, 25)) if not hasattr(b, "laeuft")][0]
        self.assertEqual(luecke.quelle, RUECKFRAGE)
        self.assertIn("Rechner war aus", luecke.herkunft)
        self.assertIn("Mittagessen in der Kantine", luecke.herkunft)

    def test_handeintrag_nennt_sich_selbst_und_die_begruendung(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "Kundentermin, Rechner blieb aus")
        sitzung = self.zeit.buchungen(date(2026, 8, 25))[0]
        self.assertEqual(sitzung.quelle, MANUELL)
        self.assertTrue(sitzung.manuell)
        self.assertIn("von Hand erfasst", sitzung.herkunft)
        self.assertIn("Kundentermin", sitzung.herkunft)
        self.assertTrue(sitzung.erfasst_am)

    def test_nachweis_fasst_die_quellen_des_tages_zusammen(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        self.zeit.sitzung_nachtragen(_zeit(13), _zeit(17), "Aussentermin")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18))
        self.assertEqual(tag.quellen, {AUTOMATISCH: 1, MANUELL: 1})
        self.assertEqual(tag.nachweis, "automatisch, von Hand")
        self.assertFalse(tag.vollautomatisch)

    def test_tag_ohne_erfassung_sagt_das_auch(self) -> None:
        self.assertEqual(self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18)).nachweis, "keine Erfassung")

    def test_urlaubstag_weist_die_gutschrift_als_handeintrag_aus(self) -> None:
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), URLAUB, notiz="Sommerurlaub")
        tag = self.zeit.tag(date(2026, 8, 25), jetzt=_zeit(18))
        self.assertEqual(tag.nachweis, "Urlaub -- von Hand eingetragen")
        self.assertFalse(tag.vollautomatisch)

    # -- Begruendungspflicht ------------------------------------------------
    def test_ohne_begruendung_kein_handeintrag(self) -> None:
        for aufruf in (
            lambda: self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), ""),
            lambda: self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "  "),
            lambda: self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "ok"),
            lambda: self.zeit.luecke_nachtragen(_zeit(8), _zeit(9), PAUSE, ""),
        ):
            with self.subTest(aufruf=aufruf):
                with self.assertRaises(ValueError):
                    aufruf()
        self.assertEqual(self.zeit.buchungen(date(2026, 8, 25)), [])

    def test_loeschen_verlangt_eine_begruendung(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "nachgetragen")
        buchung = self.zeit.buchungen(date(2026, 8, 25))[0]
        with self.assertRaises(ValueError):
            self.zeit.sitzung_verwerfen(buchung.id, "")
        self.assertEqual(len(self.zeit.buchungen(date(2026, 8, 25))), 1)

    # -- Aenderungsprotokoll ------------------------------------------------
    def test_protokoll_haelt_jeden_eingriff_fest(self) -> None:
        self.zeit.sitzung_nachtragen(_zeit(8), _zeit(12), "Rechner war in Reparatur")
        buchung = self.zeit.buchungen(date(2026, 8, 25))[0]
        self.zeit.sitzung_korrigieren(buchung.id, _zeit(8), _zeit(13), "Ende war spaeter")
        self.zeit.sitzung_verwerfen(buchung.id, "doppelt erfasst")

        protokoll = self.zeit.protokoll()
        self.assertEqual([e.aktion for e in protokoll], ["angelegt", "geaendert", "geloescht"])
        self.assertEqual(protokoll[1].vorher, "08:00 bis 12:00")
        self.assertEqual(protokoll[1].nachher, "08:00 bis 13:00")
        self.assertEqual(protokoll[2].begruendung, "doppelt erfasst")
        self.assertTrue(all(e.benutzer and e.rechner for e in protokoll))

    def test_automatische_erfassung_erzeugt_kein_protokollrauschen(self) -> None:
        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(12))
        self.zeit.starten(_zeit(12, 2))          # kurzer Aussetzer
        self.assertEqual(self.zeit.protokoll(), [])

    def test_tagesart_steht_im_protokoll(self) -> None:
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), URLAUB, notiz="Sommerurlaub")
        self.zeit.tagesart_setzen(date(2026, 8, 25), date(2026, 8, 25), ARBEITSTAG)
        protokoll = self.zeit.protokoll()
        self.assertEqual(protokoll[0].nachher, "Urlaub")
        self.assertEqual(protokoll[0].begruendung, "Sommerurlaub")
        self.assertEqual(protokoll[1].aktion, "geloescht")

    def test_protokoll_laesst_sich_nach_aenderungszeitpunkt_eingrenzen(self) -> None:
        # Eingegrenzt wird nach dem Zeitpunkt der Aenderung, nicht nach dem
        # betroffenen Arbeitstag -- das Protokoll ist ein Journal.
        self.zeit.sitzung_nachtragen(_zeit(8, tag=24), _zeit(12, tag=24), "Montag nachgetragen")
        heute = date.today()
        self.assertEqual(len(self.zeit.protokoll(heute, heute)), 1)
        self.assertEqual(
            len(self.zeit.protokoll(heute - timedelta(days=30), heute - timedelta(days=29))), 0
        )

    # -- Bericht ------------------------------------------------------------
    def test_export_belegt_jede_buchung_und_zeigt_das_protokoll(self) -> None:
        from openpyxl import load_workbook

        self.zeit.starten(_zeit(8, tag=24))
        self.zeit.herzschlag(_zeit(12, tag=24))
        Zeiterfassung(self.db, Einstellungen()).starten(_zeit(13, tag=24))  # harter Aus
        self.zeit.sitzung_nachtragen(_zeit(14, tag=25), _zeit(17, tag=25), "Aussentermin beim Kunden")

        ziel = Path(self._ordner.name) / "nachweis.xlsx"
        exportieren(self.zeit, date(2026, 8, 24), date(2026, 8, 25), ziel, jetzt=_zeit(18, tag=25))
        mappe = load_workbook(ziel)

        buchungen = mappe["Buchungen"]
        self.assertEqual(
            [zelle.value for zelle in buchungen[1]],
            ["Datum", "Von", "Bis", "Dauer", "Art", "Herkunft", "Beleg / Begruendung"],
        )
        herkuenfte = [buchungen.cell(row=zeile, column=6).value for zeile in range(2, buchungen.max_row + 1)]
        self.assertIn("Ende aus letztem Herzschlag", herkuenfte)
        self.assertIn("von Hand erfasst", herkuenfte)
        self.assertTrue(all(herkuenfte), "jede Buchung muss eine Herkunft nennen")
        belege = [buchungen.cell(row=zeile, column=7).value for zeile in range(2, buchungen.max_row + 1)]
        self.assertTrue(any("Aussentermin beim Kunden" in (beleg or "") for beleg in belege))

        protokoll = mappe["Protokoll"]
        self.assertEqual(protokoll["D2"].value, "angelegt")
        self.assertEqual(protokoll["H2"].value, "Aussentermin beim Kunden")
        self.assertTrue(protokoll["B2"].value)      # Benutzer
        self.assertTrue(protokoll["C2"].value)      # Rechner

        # Der Kopf nennt Erstellzeit, Benutzer und Programmstand.
        kopf = mappe["Uebersicht"]["A2"].value
        self.assertIn("erstellt am 25.08.2026 um 18:00:00", kopf)
        self.assertIn("Zeiterfassung", kopf)

    def test_export_ohne_handeintraege_sagt_das_deutlich(self) -> None:
        from openpyxl import load_workbook

        self.zeit.starten(_zeit(8))
        self.zeit.beenden(_zeit(16))
        ziel = Path(self._ordner.name) / "sauber.xlsx"
        exportieren(self.zeit, date(2026, 8, 25), date(2026, 8, 25), ziel, jetzt=_zeit(17))
        mappe = load_workbook(ziel)
        self.assertIn("Keine Aenderungen von Hand", mappe["Protokoll"]["A2"].value)
        self.assertEqual(mappe["Tage"]["K2"].value, "automatisch")


class Hotkeytest(unittest.TestCase):
    def test_zerlegung(self) -> None:
        self.assertEqual(tastencodes("Strg+Shift+Z"), [0x11, 0x10, 0x5A])
        self.assertEqual(tastencodes("Alt+F12"), [0x12, 0x7B])

    def test_unbekannte_taste_meldet_sich(self) -> None:
        with self.assertRaises(ValueError):
            tastencodes("Strg+Mondtaste")


class Oberflaeche(unittest.TestCase):
    """Nur wenn PySide6 vorhanden ist -- laeuft ohne Bildschirm (offscreen)."""

    @classmethod
    def setUpClass(cls) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError as fehler:  # pragma: no cover - Umgebung ohne Qt
            raise unittest.SkipTest(f"PySide6 nicht vorhanden: {fehler}")
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._ordner = tempfile.TemporaryDirectory()
        self.db = Datenbank(Path(self._ordner.name) / "test.db")
        self.zeit = Zeiterfassung(self.db, Einstellungen())

    def tearDown(self) -> None:
        self.db.schliessen()
        self._ordner.cleanup()

    def test_mini_fenster_springt_in_die_uebersicht(self) -> None:
        from zeiterfassung.gui.mini_window import MiniFenster

        self.zeit.sitzung_nachtragen(datetime.now() - timedelta(hours=2), datetime.now(), "Testeintrag")
        fenster = MiniFenster(self.zeit)
        gesehen: list[int] = []
        fenster.uebersicht_gewuenscht.connect(lambda: gesehen.append(1))

        fenster.einblenden()
        self.assertTrue(fenster.isVisible())
        fenster._absprung.click()
        self.assertEqual(gesehen, [1])
        self.assertFalse(fenster.isVisible())

    def test_mini_fenster_bleibt_nach_dem_loslassen_kurz_stehen(self) -> None:
        from zeiterfassung.gui.mini_window import MiniFenster

        fenster = MiniFenster(self.zeit)
        fenster.einblenden()
        fenster.loslassen()
        self.assertTrue(fenster.isVisible())        # noch anklickbar
        self.assertTrue(fenster._nachlauf.isActive())
        fenster.ausblenden()
        self.assertFalse(fenster.isVisible())

    def test_tagesdialog_setzt_die_tagesart(self) -> None:
        from zeiterfassung.gui.day_editor import TagesDialog

        heute = date.today()
        dialog = TagesDialog(self.zeit, heute)
        dialog._tagesart.setCurrentIndex(dialog._tagesart.findData(URLAUB))
        self.assertTrue(dialog._anteil.isEnabled())
        gespeichert = self.db.tagesart(heute)
        self.assertIsNotNone(gespeichert)
        self.assertEqual(gespeichert.art, URLAUB)

    def test_tagesdialog_zeigt_alle_buchungen(self) -> None:
        from zeiterfassung.gui.day_editor import TagesDialog

        heute = date.today()
        beginn = datetime.combine(heute, datetime.min.time()) + timedelta(hours=8)
        self.zeit.sitzung_nachtragen(beginn, beginn + timedelta(hours=8), "Testeintrag")
        self.zeit.luecke_nachtragen(beginn + timedelta(hours=4), beginn + timedelta(hours=5), PAUSE, "Mittagspause")
        dialog = TagesDialog(self.zeit, heute)
        self.assertEqual(dialog._tabelle.rowCount(), 2)
        self.assertIn("Pause", dialog._tabelle.item(1, 3).text())

    def test_buchungsdialog_sperrt_ohne_begruendung(self) -> None:
        from zeiterfassung.gui.day_editor import BuchungsDialog

        dialog = BuchungsDialog(date.today())
        self.assertFalse(dialog._uebernehmen.isEnabled())
        dialog._notiz.setText("ab")
        self.assertFalse(dialog._uebernehmen.isEnabled())
        dialog._notiz.setText("Kundentermin")
        self.assertTrue(dialog._uebernehmen.isEnabled())

    def test_rueckfrage_traegt_die_einordnung_als_begruendung(self) -> None:
        from zeiterfassung.gui.gap_dialog import LueckenDialog
        from zeiterfassung.tracker import OffeneLuecke

        luecke = OffeneLuecke(datetime.now() - timedelta(hours=1), datetime.now())
        dialog = LueckenDialog(luecke)
        art, notiz = dialog.ergebnis()
        self.assertEqual(art, PAUSE)
        self.assertTrue(notiz, "auch ohne Eingabe muss eine Begruendung entstehen")

    def test_tagesdialog_zeigt_die_herkunft(self) -> None:
        from zeiterfassung.gui.day_editor import TagesDialog

        heute = date.today()
        beginn = datetime.combine(heute, datetime.min.time()) + timedelta(hours=8)
        self.zeit.sitzung_nachtragen(beginn, beginn + timedelta(hours=4), "Aussentermin")
        dialog = TagesDialog(self.zeit, heute)
        self.assertIn("von Hand erfasst", dialog._tabelle.item(0, 4).text())
        self.assertIn("Aussentermin", dialog._tabelle.item(0, 4).text())
        self.assertIn("Nachweis:", dialog._kopf.text())

    def test_hauptfenster_zeigt_die_tagesart(self) -> None:
        from zeiterfassung.gui.main_window import Hauptfenster

        # Direkt in der Datenbank -- so bleibt der Test auch am Wochenende gueltig.
        heute = date.today()
        self.db.tagesart_setzen(heute, KRANK)
        fenster = Hauptfenster(self.zeit)
        fenster._von.setDate(fenster._von.date().fromString(heute.isoformat(), "yyyy-MM-dd"))
        fenster.aktualisieren()
        arten = [
            fenster._tabelle.item(zeile, 2).text() for zeile in range(fenster._tabelle.rowCount())
        ]
        self.assertIn("Krank", arten)


if __name__ == "__main__":
    unittest.main()
