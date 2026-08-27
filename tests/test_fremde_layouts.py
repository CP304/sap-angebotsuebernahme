"""Angebote in Stilen, die nicht zu den eigenen Regeln passen.

Warum diese Datei
-----------------
Die mitgelieferten Beispieldateien sind von diesem Projekt selbst
erzeugt.  Sie decken viel ab, aber sie sind zwangslaeufig freundlich: sie
wurden gebaut, waehrend die Regeln entstanden.  Im B2B-Alltag baut jeder
Lieferant sein Angebot anders auf, und genau daran entscheidet sich, ob
das Werkzeug taugt.

Hier stehen deshalb Layouts, die absichtlich anders sind -- und zwei
Fehler, die dabei herauskamen.  Beide waren still: sie erzeugten keinen
Befund, sondern falsche Werte, und die waeren so nach SAP gegangen.

1. *Menge und Einheit in einer Spalte.*  "Menge/Einheit" mit "50 St"
   liess die Menge leer und schrieb "50 ST" in die Mengeneinheit.
2. *Positionsnummer als Menge.*  Ohne Kopfzeile wurden die Spalten ueber
   ihre Datentypen bestimmt -- und "10, 20" sieht aus wie eine Menge.
   Die echte Menge (5.000) landete auf der Positionsnummer.

Der zweite Fehler laesst sich nur vorsichtig beheben: fortlaufende
Materialnummern ("4711000, 4711001, ...") steigen ebenfalls in gleichen
Schritten.  Beide Faelle stehen deshalb hier nebeneinander.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_fremd_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings  # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402


def _mappe(zeilen: list[list]) -> Path:
    from openpyxl import Workbook

    mappe = Workbook()
    blatt = mappe.active
    for zeile in zeilen:
        blatt.append(zeile)
    pfad = Path(tempfile.mkdtemp(prefix="fremd_")) / "angebot.xlsx"
    mappe.save(pfad)
    return pfad


class MengeUndEinheitInEinerSpalteTest(unittest.TestCase):
    """"50 St" in einer Spalte "Menge/Einheit"."""

    def setUp(self):
        self.dienst = OfferImportService(Settings())

    def test_menge_und_einheit_werden_getrennt(self):
        pfad = _mappe([
            ["Angebot", "Techno Parts"],
            ["Pos.", "Bezeichnung", "Menge/Einheit", "Preis"],
            [1, "Zahnriemen HTD 8M", "50 St", "24,80 EUR"],
            [2, "Spannrolle", "10 St", "89,00 EUR"],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 2)
        self.assertEqual(positionen[0].quantity, Decimal("50"))
        self.assertEqual(positionen[0].uom, "ST")
        self.assertEqual(positionen[1].quantity, Decimal("10"))

    def test_die_trennung_wird_protokolliert(self):
        """Stillschweigend trennen waere auch wieder falsch."""
        pfad = _mappe([
            ["Pos.", "Bezeichnung", "Menge/Einheit", "Preis"],
            [1, "Zahnriemen", "50 St", "24,80"],
            [2, "Spannrolle", "10 St", "89,00"],
        ])
        angebot = self.dienst.import_file(pfad)
        self.assertTrue(any("einer Zelle" in str(notiz)
                            for notiz in angebot.extraction_notes),
                        angebot.extraction_notes)

    def test_eine_echte_einheitenspalte_bleibt_unangetastet(self):
        pfad = _mappe([
            ["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"],
            [10, "4711001", "Dichtring", 100, "ST", "2,95"],
            [20, "4711002", "Flansch", 50, "ST", "7,40"],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(positionen[0].quantity, Decimal("100"))
        self.assertEqual(positionen[0].uom, "ST")


class PositionsnummerIstKeineMengeTest(unittest.TestCase):
    """Ohne Kopfzeile sehen "10, 20, 30" und eine Menge gleich aus."""

    def setUp(self):
        self.dienst = OfferImportService(Settings())

    def test_fortlaufende_nummern_links_sind_die_position(self):
        pfad = _mappe([
            [None, None, None, None, "Elektro Schmitt KG"],
            [None, None, None, None, "Angebot Nr. 77-2026"],
            [],
            ["10", "4711999", "Kabelbinder 200mm", "5.000", "ST", "0,04"],
            ["20", "4712000", "Schrumpfschlauch 6mm", "300", "M", "1,25"],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 2)
        self.assertEqual(positionen[0].position_number, "10")
        self.assertEqual(positionen[0].quantity, Decimal("5000"))
        self.assertEqual(positionen[1].quantity, Decimal("300"))

    def test_fortlaufende_materialnummern_bleiben_materialnummern(self):
        """Der Gegenfall -- sie steigen ebenfalls in gleichen Schritten."""
        pfad = _mappe([
            ["4711000", "Dichtring Typ 0", "100", "ST", "38,50"],
            ["4711001", "Dichtring Typ 1", "250", "ST", "1,15"],
            ["4711002", "Dichtring Typ 2", "500", "ST", "12,40"],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual([p.material_number for p in positionen],
                         ["4711000", "4711001", "4711002"])
        self.assertEqual([p.quantity for p in positionen],
                         [Decimal("100"), Decimal("250"), Decimal("500")])

    def test_unregelmaessige_zahlen_sind_keine_nummern(self):
        """Eine Menge ist das Ergebnis eines Bedarfs, keine Reihe.

        Steigen die Zahlen links unregelmaessig, wird nichts behauptet --
        dann bleibt es bei der Zuordnung ueber die Datentypen.
        """
        pfad = _mappe([
            ["120", "4711001", "Dichtring 40x52", "ST", "2,95"],
            ["75", "4711002", "Flansch DN50", "ST", "7,40"],
            ["310", "4711003", "Kegelstueck", "ST", "3,10"],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 3)
        self.assertNotEqual([p.position_number for p in positionen],
                            ["120", "75", "310"])
        self.assertEqual([p.material_number for p in positionen],
                         ["4711001", "4711002", "4711003"])

    def test_die_entscheidung_wird_protokolliert(self):
        pfad = _mappe([
            ["10", "4711999", "Kabelbinder", "5.000", "ST", "0,04"],
            ["20", "4712000", "Schrumpfschlauch", "300", "M", "1,25"],
        ])
        angebot = self.dienst.import_file(pfad)
        self.assertTrue(any("fortlaufende Nummern" in str(notiz)
                            for notiz in angebot.extraction_notes),
                        angebot.extraction_notes)


class WeitereFremdeLayoutsTest(unittest.TestCase):
    """Stile, die schon vorher liefen -- damit sie es bleiben."""

    def setUp(self):
        self.dienst = OfferImportService(Settings())

    def test_andere_spaltennamen(self):
        """"Art.-Nr." ist die Nummer DES LIEFERANTEN, nicht unsere."""
        pfad = _mappe([
            ["Angebot 2026-0815 · Muster Technik GmbH"],
            [],
            ["Art.-Nr.", "Artikelbezeichnung", "Abnahme", "Einh.", "VK netto"],
            ["A-88120", "Wellendichtring 25x40x7", 250, "Stk", 3.45],
            ["A-88121", "O-Ring NBR 30x2", 1000, "Stk", 0.18],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 2)
        self.assertEqual(positionen[0].vendor_material_number, "A-88120")
        self.assertEqual(positionen[0].quantity, Decimal("250"))
        self.assertEqual(positionen[0].price, Decimal("3.45"))

    def test_englische_ueberschriften_mit_punkt_als_dezimaltrenner(self):
        pfad = _mappe([
            ["QUOTATION QT-2026-4455"],
            ["Supplier: Nordic Bearings AB"],
            [],
            ["Item", "Part No.", "Description", "Qty", "UoM", "Unit Price"],
            [10, "SKF-6204", "Deep groove ball bearing", 500, "PCS", 4.75],
            [20, "SKF-6205", "Deep groove ball bearing", 200, "PCS", 6.10],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 2)
        self.assertEqual(positionen[0].quantity, Decimal("500"))
        self.assertEqual(positionen[0].price, Decimal("4.75"))

    def test_zwischensummen_werden_nicht_zu_positionen(self):
        pfad = _mappe([
            ["Pos", "Material", "Bezeichnung", "Menge", "ME", "Einzelpreis"],
            [10, "H-1000", "Hydraulikschlauch DN12", 100, "M", 8.90],
            [None, None, "Zwischensumme", None, None, 1510.50],
            [20, "H-1001", "Pressarmatur 12L", 200, "ST", 3.20],
        ])
        positionen = self.dienst.import_file(pfad).positions
        self.assertEqual(len(positionen), 2)
        self.assertEqual([p.material_number for p in positionen],
                         ["H-1000", "H-1001"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
