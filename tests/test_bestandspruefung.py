"""Eigene Angebote pruefen, bevor man sich auf das Werkzeug verlaesst.

"Liest er meine Belege?" laesst sich nicht durch Zusicherungen
beantworten, sondern nur, indem man es an den eigenen Belegen
ausprobiert -- an allen auf einmal, bevor der erste Preis nach SAP geht.

Zwei Eigenschaften stehen hier fest:

* Es wird ausschliesslich GELESEN. Weder SAP noch die Historie werden
  angefasst. Sonst waere die Pruefung selbst ein Risiko.
* Eine kaputte Datei bricht den Durchlauf nicht ab. Gerade sie ist ja
  der interessante Fall -- wer nach dem ersten Fehler aufhoert, sieht
  die uebrigen siebzehn nie.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_bestand_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                          # noqa: E402
from app.services.bestandspruefung import (                       # noqa: E402
    FileResult,
    check_folder,
    report_text,
)

GUTE_TABELLE = (
    "Pos;Artikelnummer;Bezeichnung;Menge;ME;Einzelpreis;Waehrung\n"
    "10;4711001;Dichtring NBR;500;ST;2,95;EUR\n"
    "20;4711002;O-Ring 20x3;250;ST;1,15;EUR\n"
)


class UrteilTest(unittest.TestCase):
    """Das Kurzurteil je Datei."""

    def test_vollstaendig(self):
        eintrag = FileResult("a.csv", positions=2, complete=2)
        self.assertEqual(eintrag.verdict, "vollstaendig")
        self.assertTrue(eintrag.ok)
        self.assertFalse(eintrag.needs_work)

    def test_nichts_erkannt(self):
        eintrag = FileResult("a.pdf")
        self.assertEqual(eintrag.verdict, "nichts erkannt")
        self.assertTrue(eintrag.needs_work)

    def test_ohne_preise(self):
        eintrag = FileResult("a.csv", positions=3, complete=0)
        self.assertEqual(eintrag.verdict, "ohne Preise")
        self.assertTrue(eintrag.needs_work)

    def test_teilweise(self):
        eintrag = FileResult("a.csv", positions=3, complete=2)
        self.assertEqual(eintrag.verdict, "teilweise")
        self.assertFalse(eintrag.needs_work,
                         "Teilerfolge sind Nacharbeit, kein Fehlschlag")

    def test_unsicheres_ist_nicht_vollstaendig(self):
        """Gelbe Felder heissen: jemand muss hinsehen."""
        eintrag = FileResult("a.csv", positions=2, complete=2, uncertain=1)
        self.assertEqual(eintrag.verdict, "teilweise")

    def test_fehler(self):
        eintrag = FileResult("a.pdf", error="kaputt")
        self.assertEqual(eintrag.verdict, "Fehler")
        self.assertTrue(eintrag.needs_work)


class OrdnerPruefenTest(unittest.TestCase):

    def setUp(self) -> None:
        self.ordner = Path(tempfile.mkdtemp(prefix="pruef_"))
        self.settings = Settings()

    def _datei(self, name: str, inhalt: str) -> Path:
        pfad = self.ordner / name
        pfad.write_text(inhalt, encoding="utf-8")
        return pfad

    def test_gute_datei_wird_erkannt(self):
        self._datei("angebot.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertEqual(ergebnis.total, 1)
        eintrag = ergebnis.results[0]
        self.assertEqual(eintrag.positions, 2)
        self.assertEqual(eintrag.complete, 2, "Preis und Menge stehen ueberall")
        self.assertFalse(eintrag.needs_work)

    def test_ohne_lieferant_bleibt_die_materialnummer_unsicher(self):
        """Kein Mangel der Pruefung, sondern richtiges Verhalten.

        Ohne bekannten Lieferanten laesst sich nicht entscheiden, ob
        "4711001" unsere Materialnummer ist oder die des Lieferanten.
        Das Urteil lautet deshalb "teilweise" -- jemand muss hinsehen.
        """
        self._datei("angebot.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        eintrag = ergebnis.results[0]
        self.assertEqual(eintrag.uncertain, 2)
        self.assertEqual(eintrag.verdict, "teilweise")

    def test_mehrere_dateien(self):
        self._datei("a.csv", GUTE_TABELLE)
        self._datei("b.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertEqual(ergebnis.total, 2)
        self.assertEqual(ergebnis.positions, 4)

    def test_unlesbare_datei_bricht_nicht_ab(self):
        """Wer nach dem ersten Fehler aufhoert, sieht die uebrigen nie."""
        self._datei("kaputt.csv", "\x00\x01 kein Text \x02")
        self._datei("gut.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertEqual(ergebnis.total, 2)
        gute = [r for r in ergebnis.results if r.name == "gut.csv"]
        self.assertEqual(gute[0].positions, 2,
                         "Die gute Datei muss trotzdem gelesen werden")

    def test_fremde_endungen_werden_uebergangen(self):
        self._datei("notizen.log", "irgendwas")
        self._datei("bild.png", "nicht wirklich ein Bild")
        self._datei("angebot.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        namen = [r.name for r in ergebnis.results]
        self.assertIn("angebot.csv", namen)
        self.assertNotIn("notizen.log", namen)

    def test_leerer_ordner(self):
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertEqual(ergebnis.total, 0)
        self.assertIn("Keine lesbaren Dateien", ergebnis.summary())

    def test_ordner_gibt_es_nicht(self):
        ergebnis = check_folder(self.settings, self.ordner / "gibtesnicht")
        self.assertEqual(ergebnis.total, 0)

    def test_obergrenze_wird_beachtet(self):
        for nummer in range(5):
            self._datei(f"a{nummer}.csv", GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner, limit=3)
        self.assertEqual(ergebnis.total, 3)

    def test_fortschritt_wird_gemeldet(self):
        self._datei("a.csv", GUTE_TABELLE)
        self._datei("b.csv", GUTE_TABELLE)
        meldungen = []
        check_folder(self.settings, self.ordner,
                     progress=lambda n, g, name: meldungen.append((n, g, name)))
        self.assertEqual(len(meldungen), 2)
        self.assertEqual(meldungen[0][1], 2, "Gesamtzahl gehoert in die Meldung")

    def test_kopffelder_werden_gezaehlt(self):
        self._datei("mit_kopf.csv",
                    "Angebot ANG-2026-1234 vom 18.08.2026\n"
                    "Beispiel GmbH\n\n" + GUTE_TABELLE)
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertGreater(ergebnis.results[0].header_found, 0)

    def test_problemdateien_werden_benannt(self):
        self._datei("leer.csv", "Nur ein Satz ohne jede Tabelle.\n")
        ergebnis = check_folder(self.settings, self.ordner)
        self.assertEqual(len(ergebnis.problem_files), 1)
        self.assertEqual(ergebnis.problem_files[0].name, "leer.csv")

    def test_es_wird_nichts_geschrieben(self):
        """Die Pruefung darf selbst kein Risiko sein."""
        self._datei("a.csv", GUTE_TABELLE)
        vorher = sorted(p.name for p in self.ordner.iterdir())
        check_folder(self.settings, self.ordner)
        nachher = sorted(p.name for p in self.ordner.iterdir())
        self.assertEqual(vorher, nachher,
                         "Im geprueften Ordner darf nichts entstehen")


class BerichtTest(unittest.TestCase):

    def setUp(self) -> None:
        self.ordner = Path(tempfile.mkdtemp(prefix="bericht_"))
        self.settings = Settings()

    def test_bericht_nennt_jede_datei(self):
        (self.ordner / "a.csv").write_text(GUTE_TABELLE, encoding="utf-8")
        text = report_text(check_folder(self.settings, self.ordner))
        self.assertIn("a.csv", text)
        self.assertIn("vollstaendig", text)

    def test_bericht_bei_leerem_ordner(self):
        text = report_text(check_folder(self.settings, self.ordner))
        self.assertIn("keine lesbare datei", text.lower())

    def test_bericht_weist_auf_den_ablageordner_hin(self):
        (self.ordner / "leer.csv").write_text("Kein Angebot.\n", encoding="utf-8")
        text = report_text(check_folder(self.settings, self.ordner))
        self.assertIn("nicht_erkannt", text,
                      "Der Anwender muss wissen, wo die Faelle liegen")

    def test_langer_dateiname_sprengt_die_spalte_nicht(self):
        name = "A" * 80 + ".csv"
        (self.ordner / name).write_text(GUTE_TABELLE, encoding="utf-8")
        text = report_text(check_folder(self.settings, self.ordner))
        for zeile in text.splitlines():
            self.assertLess(len(zeile), 100, zeile)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
