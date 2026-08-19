"""Preise in einem Rutsch anpassen.

"Alle Preise plus 3 %" ist eine Ansage, die der Lieferant in einem Satz
macht. Sie in vierzig Zeilen von Hand nachzuvollziehen ist eine halbe
Stunde und vierzig Gelegenheiten, sich zu vertippen.

Drei Dinge stehen hier fest:

* Die Bezugsgroesse wird nie geraten. "Plus 3 %" auf einen bereits
  erhoehten Preis gerechnet ergibt stillen Unsinn -- der Anwender waehlt
  ausdruecklich zwischen dem Preis in der Tabelle und dem aus SAP.
* Nichts wird uebersprungen, ohne es zu sagen. Positionen ohne
  Ausgangswert erscheinen im Bericht.
* Ein Ergebnis kleiner oder gleich null wird nicht geschrieben. Ein
  Preis von 0,00 ist im Infosatz nicht guenstig, sondern falsch.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_preis_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.models.enums import FieldOrigin                        # noqa: E402
from app.models.offer_position import OfferPosition             # noqa: E402
from app.services.preisanpassung import (                       # noqa: E402
    BASE_CURRENT,
    BASE_OLD,
    MODE_ABSOLUTE,
    MODE_PERCENT,
    MODE_SET,
    apply_adjustment,
    preview_adjustment,
)


def position(nummer: str, preis: str | None, alt: str | None = None):
    eintrag = OfferPosition(position_number=nummer,
                            material_number=f"47110{nummer}")
    if preis is not None:
        eintrag.price = Decimal(preis)
    if alt is not None:
        from app.models.sap_info_record import SapInfoRecord

        satz = SapInfoRecord()
        satz.net_price = Decimal(alt)
        # "was_read" leitet sich aus read_at ab und laesst sich nicht setzen.
        satz.read_at = datetime.now()
        eintrag.sap_info_record = satz
    return eintrag


class ProzentTest(unittest.TestCase):

    def test_erhoehung(self):
        ergebnis = preview_adjustment([position("10", "2.95")], MODE_PERCENT, 3)
        self.assertEqual(ergebnis.changes[0][2], Decimal("3.04"))

    def test_senkung(self):
        ergebnis = preview_adjustment([position("10", "10.00")], MODE_PERCENT, -10)
        self.assertEqual(ergebnis.changes[0][2], Decimal("9.00"))

    def test_kaufmaennisch_gerundet(self):
        """2,955 wird zu 2,96 -- nicht abgeschnitten."""
        ergebnis = preview_adjustment([position("10", "2.95")], MODE_PERCENT,
                                      Decimal("0.17"))
        self.assertEqual(ergebnis.changes[0][2], Decimal("2.96"))

    def test_null_prozent_aendert_nichts(self):
        ergebnis = preview_adjustment([position("10", "2.95")], MODE_PERCENT, 0)
        self.assertEqual(ergebnis.count, 0)
        self.assertIn("unveraendert", ergebnis.skipped[0][1])


class AbsolutTest(unittest.TestCase):

    def test_aufschlag(self):
        ergebnis = preview_adjustment([position("10", "2.95")], MODE_ABSOLUTE,
                                      "0.10")
        self.assertEqual(ergebnis.changes[0][2], Decimal("3.05"))

    def test_abschlag(self):
        ergebnis = preview_adjustment([position("10", "2.95")], MODE_ABSOLUTE,
                                      "-0.15")
        self.assertEqual(ergebnis.changes[0][2], Decimal("2.80"))


class FestwertTest(unittest.TestCase):

    def test_alle_auf_denselben_preis(self):
        posten = [position("10", "2.95"), position("20", "12.40")]
        ergebnis = preview_adjustment(posten, MODE_SET, "5.00")
        self.assertEqual(ergebnis.count, 2)
        self.assertTrue(all(neu == Decimal("5.00")
                            for _p, _alt, neu in ergebnis.changes))


class BezugsgroesseTest(unittest.TestCase):
    """Der teuerste denkbare Fehler an dieser Stelle."""

    def test_auf_den_tabellenpreis(self):
        posten = [position("10", "3.00", alt="2.00")]
        ergebnis = preview_adjustment(posten, MODE_PERCENT, 10, BASE_CURRENT)
        self.assertEqual(ergebnis.changes[0][2], Decimal("3.30"))

    def test_auf_den_sap_preis(self):
        posten = [position("10", "3.00", alt="2.00")]
        ergebnis = preview_adjustment(posten, MODE_PERCENT, 10, BASE_OLD)
        self.assertEqual(ergebnis.changes[0][2], Decimal("2.20"),
                         "Gerechnet werden muss auf 2,00, nicht auf 3,00")

    def test_ohne_sap_preis_wird_nichts_erfunden(self):
        posten = [position("10", "3.00")]
        ergebnis = preview_adjustment(posten, MODE_PERCENT, 10, BASE_OLD)
        self.assertEqual(ergebnis.count, 0)
        self.assertIn("SAP", ergebnis.skipped[0][1])

    def test_bezugsgroesse_steht_im_bericht(self):
        posten = [position("10", "3.00", alt="2.00")]
        ergebnis = preview_adjustment(posten, MODE_PERCENT, 10, BASE_OLD)
        self.assertIn("SAP", ergebnis.summary())


class NichtsVerschluckenTest(unittest.TestCase):

    def test_position_ohne_preis_wird_gemeldet(self):
        ergebnis = preview_adjustment([position("10", None)], MODE_PERCENT, 3)
        self.assertEqual(ergebnis.count, 0)
        self.assertEqual(len(ergebnis.skipped), 1)
        self.assertIn("kein Preis", ergebnis.skipped[0][1])

    def test_null_wird_nicht_geschrieben(self):
        """0,00 EUR ist im Infosatz nicht guenstig, sondern falsch."""
        ergebnis = preview_adjustment([position("10", "2.95")],
                                      MODE_PERCENT, -100)
        self.assertEqual(ergebnis.count, 0)
        self.assertIn("kein gueltiger Preis", ergebnis.skipped[0][1])

    def test_negatives_ergebnis_wird_nicht_geschrieben(self):
        ergebnis = preview_adjustment([position("10", "2.95")],
                                      MODE_ABSOLUTE, "-5.00")
        self.assertEqual(ergebnis.count, 0)

    def test_unbrauchbarer_wert(self):
        ergebnis = preview_adjustment([position("10", "2.95")],
                                      MODE_PERCENT, "viel")
        self.assertEqual(ergebnis.count, 0)
        self.assertIn("unbrauchbar", ergebnis.skipped[0][1])

    def test_gemischte_liste(self):
        posten = [position("10", "2.95"), position("20", None),
                  position("30", "12.40")]
        ergebnis = preview_adjustment(posten, MODE_PERCENT, 3)
        self.assertEqual(ergebnis.count, 2)
        self.assertEqual(len(ergebnis.skipped), 1,
                         "Die uebersprungene Position muss auftauchen")

    def test_leere_liste(self):
        ergebnis = preview_adjustment([], MODE_PERCENT, 3)
        self.assertEqual(ergebnis.count, 0)


class AnwendenTest(unittest.TestCase):

    def test_vorschau_aendert_nichts(self):
        posten = [position("10", "2.95")]
        preview_adjustment(posten, MODE_PERCENT, 3)
        self.assertEqual(posten[0].price, Decimal("2.95"),
                         "Die Vorschau darf nichts anfassen")

    def test_anwenden_schreibt(self):
        posten = [position("10", "2.95")]
        apply_adjustment(posten, MODE_PERCENT, 3)
        self.assertEqual(posten[0].price, Decimal("3.04"))

    def test_herkunft_ist_manuell(self):
        posten = [position("10", "2.95")]
        apply_adjustment(posten, MODE_PERCENT, 3)
        self.assertEqual(posten[0].origin("price"), FieldOrigin.MANUAL,
                         "Ein gerechneter Preis darf nicht als gelesen gelten")

    def test_vermerk_nennt_die_rechnung(self):
        posten = [position("10", "2.95")]
        apply_adjustment(posten, MODE_PERCENT, 3)
        vermerk = posten[0].confidence_reasons[-1]
        self.assertIn("2,95", vermerk)
        self.assertIn("3,04", vermerk)
        self.assertIn("3,00 %", vermerk)

    def test_zweimal_anwenden_rechnet_auf_das_ergebnis(self):
        """Nachvollziehbar, aber der Anwender muss es wissen."""
        posten = [position("10", "100.00")]
        apply_adjustment(posten, MODE_PERCENT, 10)
        apply_adjustment(posten, MODE_PERCENT, 10)
        self.assertEqual(posten[0].price, Decimal("121.00"))

    def test_bericht_ist_lesbar(self):
        posten = [position("10", "2.95"), position("20", None)]
        ergebnis = apply_adjustment(posten, MODE_PERCENT, 3)
        text = ergebnis.details()
        self.assertIn("Pos. 10", text)
        self.assertIn("Pos. 20", text)
        self.assertIn("2,95", text, "Deutsche Schreibweise im Bericht")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
