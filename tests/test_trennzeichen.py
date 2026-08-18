"""CSV-Trennzeichen bestimmen -- auch wenn ein Briefkopf davorsteht.

Der Fehler, um den es geht
--------------------------
Die Bewertung im Fallback verglich die Trennzeichenanzahl jeder Zeile mit
der *ersten* Zeile.  Steht dort ein Briefkopf ohne Trennzeichen
("Preisliste 2026"), dann "stimmten" alle uebrigen Zeilen mit dessen Null
ueberein -- und es gewann ausgerechnet ein Zeichen, das in der Tabelle
nirgends als Trenner steht.

Bei deutschen Preislisten fiel die Wahl damit auf das Komma.  Die Folge
war doppelt unangenehm: die Spalten blieben ungetrennt, UND "2,95"
zerfiel in "2" und "95".  Die Datei ergab null Positionen, ohne dass
irgendwo ein Trennzeichenproblem sichtbar wurde -- der Anwender sah nur
"nichts erkannt".

Ein Briefkopf ueber der Tabelle ist bei Preislisten der Normalfall, nicht
die Ausnahme.  Deshalb steht dieser Fall hier fest.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_trenn_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                            # noqa: E402
from app.services.offer_import_service import OfferImportService    # noqa: E402
from app.services.readers.excel_reader import detect_delimiter      # noqa: E402

#: Dieselbe Tabelle, einmal je Trennzeichen
TABELLE_SEMIKOLON = (
    "Pos;Artikelnummer;Bezeichnung;Menge;ME;Einzelpreis;Waehrung\n"
    "10;4711001;Dichtring NBR 40x52x7;500;ST;2,95;EUR\n"
    "20;4711002;O-Ring 20x3;250;ST;1,15;EUR\n"
)
TABELLE_KOMMA = (
    "Pos,Artikelnummer,Bezeichnung,Menge,ME,Einzelpreis\n"
    "10,4711001,Dichtring NBR,500,ST,2.95\n"
    "20,4711002,O-Ring,250,ST,1.15\n"
)


class TrennzeichenErkennenTest(unittest.TestCase):
    """Die reine Erkennungsfunktion."""

    def test_semikolon_ohne_vorspann(self):
        self.assertEqual(detect_delimiter(TABELLE_SEMIKOLON), ";")

    def test_semikolon_trotz_titelzeile(self):
        self.assertEqual(detect_delimiter("Preisliste 2026\n" + TABELLE_SEMIKOLON), ";")

    def test_semikolon_trotz_briefkopf(self):
        text = ("Angebot ANG-2026-9001 vom 18.08.2026\n"
                "Muster Dichtungstechnik GmbH\n\n" + TABELLE_SEMIKOLON)
        self.assertEqual(detect_delimiter(text), ";")

    def test_komma_bleibt_komma(self):
        """Englische Listen duerfen durch die Korrektur nicht kaputtgehen."""
        self.assertEqual(detect_delimiter(TABELLE_KOMMA), ",")

    def test_komma_trotz_titelzeile(self):
        self.assertEqual(detect_delimiter("Price list 2026\n" + TABELLE_KOMMA), ",")

    def test_tabulator(self):
        self.assertEqual(detect_delimiter(TABELLE_SEMIKOLON.replace(";", "\t")), "\t")

    def test_tabulator_trotz_briefkopf(self):
        text = "Preisliste 2026\n\n" + TABELLE_SEMIKOLON.replace(";", "\t")
        self.assertEqual(detect_delimiter(text), "\t")

    def test_dezimalkomma_schlaegt_nicht_durch(self):
        """Der eigentliche Ausloeser: Preise mit Komma in einer ;-Tabelle."""
        text = ("Bezeichnung;Preis\n"
                "Dichtring;2,95\n"
                "O-Ring;1,15\n"
                "Wellendichtring;12,40\n")
        self.assertEqual(detect_delimiter(text), ";",
                         "Das Dezimalkomma darf nicht als Trennzeichen gelten")

    def test_leerer_text(self):
        self.assertEqual(detect_delimiter(""), ";")
        self.assertEqual(detect_delimiter("   \n  \n"), ";")

    def test_einzelne_zeile_ohne_trennzeichen(self):
        # Nichts zu erkennen -- Hauptsache kein Absturz.
        self.assertIn(detect_delimiter("nur ein Satz ohne alles"), (";", ",", "\t", "|"))


class ImportMitVorspannTest(unittest.TestCase):
    """Der komplette Weg: Datei rein, Positionen raus."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dienst = OfferImportService(Settings())

    def _import(self, name: str, inhalt: str):
        pfad = Path(_TEMP_HOME) / f"{name}.csv"
        pfad.write_text(inhalt, encoding="utf-8")
        return self.dienst.import_file(str(pfad))

    def test_ohne_vorspann(self):
        angebot = self._import("ohne", TABELLE_SEMIKOLON)
        self.assertEqual(len(angebot.positions), 2)

    def test_mit_titelzeile(self):
        angebot = self._import("titel", "Preisliste 2026\n" + TABELLE_SEMIKOLON)
        self.assertEqual(len(angebot.positions), 2,
                         "Eine Titelzeile darf die Tabelle nicht unlesbar machen")

    def test_mit_briefkopf(self):
        angebot = self._import(
            "briefkopf",
            "Angebot ANG-2026-9001 vom 18.08.2026\n"
            "Muster Dichtungstechnik GmbH\n\n" + TABELLE_SEMIKOLON)
        self.assertEqual(len(angebot.positions), 2)

    def test_preis_bleibt_ganz(self):
        """2,95 darf nicht in 2 und 95 zerfallen."""
        angebot = self._import("preis", "Preisliste 2026\n" + TABELLE_SEMIKOLON)
        self.assertEqual(angebot.positions[0].price, Decimal("2.95"))

    def test_englische_liste_mit_titelzeile(self):
        angebot = self._import("englisch", "Price list 2026\n" + TABELLE_KOMMA)
        self.assertEqual(len(angebot.positions), 2)
        self.assertEqual(angebot.positions[0].price, Decimal("2.95"))

    def test_material_wird_uebernommen(self):
        angebot = self._import("material", "Preisliste 2026\n" + TABELLE_SEMIKOLON)
        nummern = [p.material_number for p in angebot.positions]
        self.assertIn("4711001", nummern)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
