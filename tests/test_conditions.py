"""Tests der Zusatzkonditionen: Erkennung, Schreiben, Pruefung.

Aufruf:  ``python -m unittest tests.test_conditions -v``

Hintergrund: Bisher wanderte nur der Bruttopreis (PB00) in den Infosatz.  Ein
Angebot nennt aber regelmaessig mehr -- "abzueglich 3 % Mengenrabatt", "zzgl.
45,00 EUR Frachtpauschale", "2 % Skonto bei 10 Tagen".  Das gehoert als eigene
Konditionszeile in den Infosatz und darf NICHT in den Preis eingerechnet
werden, sonst ist die Herkunft des Preises verloren.

Geprueft werden drei Bereiche:

    1. Erkennung   -- deutsch und englisch, und vor allem: was NICHT erkannt
       wird.  Ein falsch erkannter Rabatt verfaelscht den Einkaufspreis
       dauerhaft und unbemerkt.
    2. Schreiben   -- gegen das eingebaute Testsystem: Reihenfolge, Kappung,
       ungepruefte Feld-IDs, Einrechnen in den Nettopreis, Ruecklese-Pruefung.
    3. Pruefungen  -- Warnungen und blockierende Befunde der Validierung.

Wie in den uebrigen Testdateien wird das Basisverzeichnis der Anwendung *vor*
dem Import der Anwendungsmodule auf ein temporaeres Verzeichnis umgebogen,
damit keine echten Anwenderdaten angefasst werden.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="sap_conditions_tests_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import IssueSeverity, ResultState                 # noqa: E402
from app.models.offer import Offer                                      # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.sap.gateway import SapGateway                                  # noqa: E402
from app.sap.info_record_service import (                               # noqa: E402
    plan_conditions,
    unverified_condition_selectors,
    verify_info_record_write,
)
from app.sap.mock_backend import MockSapSystem                          # noqa: E402
from app.services.extraction.condition_rules import (                   # noqa: E402
    ConditionCandidate,
    attach_conditions,
    extract_conditions,
    merge_head_and_position,
)
from app.services.validation_service import ValidationService           # noqa: E402


def tearDownModule() -> None:
    os.environ.pop("SAP_ANGEBOT_HOME", None)
    try:
        _TEMP_HOME.cleanup()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def make_settings() -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = False
    return settings


def make_position(**overrides) -> OfferPosition:
    werte = dict(
        position_number="10",
        material_number="47110001",
        vendor_number="0000100234",
        description="Dichtring NBR 40x52x7",
        quantity=Decimal("100"),
        uom="ST",
        price=Decimal("12.85"),
        price_unit=1,
        currency="EUR",
        purchasing_org="1000",
        plant="1000",
        valid_from=date.today(),
        selected=True,
        do_info_record=True,
    )
    werte.update(overrides)
    return OfferPosition(**werte)


def kandidat(art: str, wert: str, waehrung: str = "", quelltext: str = "Test",
             konfidenz: float = 0.9) -> ConditionCandidate:
    return ConditionCandidate(art=art, wert=Decimal(wert), waehrung=waehrung,
                              quelltext=quelltext, konfidenz=konfidenz)


def arten(text: str, settings=None) -> list[str]:
    return [k.art for k in extract_conditions(text, settings or make_settings())]


class _Umgebung:
    """Gateway samt Mock-System, frisch zurueckgesetzt."""

    def __init__(self) -> None:
        self.settings = make_settings()
        self.gateway = SapGateway(self.settings)
        self.gateway.reset_mock_data()

    @property
    def system(self) -> MockSapSystem:
        return self.gateway.mock_system

    def freigeben(self) -> None:
        """Konditions-Feld-IDs als geprueft markieren (wie nach dem Aufzeichnen)."""
        for key in ("condition_row_type_cell", "condition_row_amount_cell",
                    "condition_row_unit_cell"):
            self.gateway.selectors.mark_verified("info_record_conditions", key, True)

    def schreiben(self, position: OfferPosition):
        return self.gateway.info_records.write(position, self.gateway.write_context())

    def infosatz(self) -> dict:
        schluessel = MockSapSystem.ir_key("47110001", "0000100234", "1000", "1000")
        return self.system.info_records[schluessel]


def alle_meldungen(result) -> str:
    return " | ".join([result.message] + list(result.sap_messages))


# ===========================================================================
# 1. Erkennung
# ===========================================================================

class ErkennungDeutschTest(unittest.TestCase):
    """Die gaengigen deutschen Formulierungen aus echten Angeboten."""

    def test_rabatt_prozent_nachgestellt(self) -> None:
        self.assertEqual(["discount_percent"], arten("3 % Rabatt"))

    def test_rabatt_prozent_mit_abzgl(self) -> None:
        self.assertEqual(["discount_percent"], arten("abzgl. 3%"))

    def test_rabatt_prozent_zusammengesetztes_wort(self) -> None:
        self.assertEqual(["discount_percent"], arten("Mengenrabatt 5 %"))

    def test_rabatt_prozent_mit_umlaut(self) -> None:
        self.assertEqual(["discount_percent"], arten("abzüglich 3 % Mengenrabatt"))

    def test_rabatt_absolut(self) -> None:
        konditionen = extract_conditions("abzueglich 50,00 EUR", make_settings())
        self.assertEqual(1, len(konditionen))
        self.assertEqual("discount_absolute", konditionen[0].art)
        self.assertEqual(Decimal("50.00"), konditionen[0].wert)
        self.assertEqual("EUR", konditionen[0].waehrung)

    def test_zuschlag_prozent(self) -> None:
        self.assertEqual(["surcharge_percent"], arten("zzgl. 2 % Teuerungszuschlag"))

    def test_zuschlag_absolut(self) -> None:
        konditionen = extract_conditions("Mindermengenzuschlag 25,00 EUR",
                                         make_settings())
        self.assertEqual("surcharge_absolute", konditionen[0].art)
        self.assertEqual(Decimal("25.00"), konditionen[0].wert)

    def test_fracht_absolut(self) -> None:
        konditionen = extract_conditions("zzgl. 45,00 EUR Fracht", make_settings())
        self.assertEqual("freight_absolute", konditionen[0].art)
        self.assertEqual(Decimal("45.00"), konditionen[0].wert)

    def test_frachtpauschale(self) -> None:
        self.assertEqual(["freight_absolute"], arten("Frachtpauschale 45,00 EUR"))

    def test_skonto(self) -> None:
        konditionen = extract_conditions("2 % Skonto bei 10 Tagen", make_settings())
        self.assertEqual(["cash_discount"], [k.art for k in konditionen])
        self.assertEqual(Decimal("2"), konditionen[0].wert,
                         "Die '10 Tage' duerfen nicht als Wert landen")

    def test_tausendertrennung_im_betrag(self) -> None:
        konditionen = extract_conditions("abzueglich 1.250,00 EUR Nachlass",
                                         make_settings())
        self.assertEqual(Decimal("1250.00"), konditionen[0].wert)

    def test_waehrungszeichen_vorangestellt(self) -> None:
        konditionen = extract_conditions("Frachtpauschale EUR 45,00", make_settings())
        self.assertEqual("freight_absolute", konditionen[0].art)
        self.assertEqual("EUR", konditionen[0].waehrung)


class ErkennungEnglischTest(unittest.TestCase):
    """Englischsprachige Angebote sind im Einkauf der Normalfall."""

    def test_discount_percent(self) -> None:
        self.assertEqual(["discount_percent"], arten("3% discount"))

    def test_rebate(self) -> None:
        self.assertEqual(["discount_percent"], arten("5 % rebate on all items"))

    def test_surcharge(self) -> None:
        self.assertEqual(["surcharge_percent"], arten("plus 2 % surcharge"))

    def test_freight_percent(self) -> None:
        self.assertEqual(["freight_percent"], arten("freight 3 %"))

    def test_freight_absolute(self) -> None:
        konditionen = extract_conditions("shipping cost 45.00 EUR", make_settings())
        self.assertEqual("freight_absolute", konditionen[0].art)
        self.assertEqual(Decimal("45.00"), konditionen[0].wert)

    def test_cash_discount(self) -> None:
        self.assertEqual(["cash_discount"], arten("2% cash discount"))


class ErkennungGrenzfaelleTest(unittest.TestCase):
    """Bei Unklarheit lieber nichts erkennen."""

    def test_frei_haus_ist_keine_fracht(self) -> None:
        self.assertEqual([], arten("Lieferung frei Haus"))

    def test_frei_haus_verwirft_erkannte_fracht(self) -> None:
        """Widerspruechlicher Text: dann lieber gar keine Frachtkondition."""
        self.assertEqual([], arten("Lieferung frei Haus, zzgl. 45,00 EUR Fracht"))

    def test_frei_haus_laesst_rabatt_bestehen(self) -> None:
        self.assertEqual(["discount_percent"],
                         arten("Lieferung frei Haus, abzueglich 3 % Rabatt"))

    def test_mehrwertsteuer_ist_keine_kondition(self) -> None:
        self.assertEqual([], arten("zzgl. 19 % MwSt"))

    def test_vat_ist_keine_kondition(self) -> None:
        self.assertEqual([], arten("plus 19 % VAT"))

    def test_blosser_preis_ist_keine_kondition(self) -> None:
        self.assertEqual([], arten("Der Preis betraegt 12,85 EUR je Stueck"))

    def test_betrag_ohne_waehrung_wird_verworfen(self) -> None:
        self.assertEqual([], arten("abzueglich 50"))

    def test_zahl_ohne_schluesselwort_wird_verworfen(self) -> None:
        self.assertEqual([], arten("Wir liefern 100 Stueck zum 01.09.2026"))

    def test_prozent_ueber_hundert_wird_verworfen(self) -> None:
        self.assertEqual([], arten("abzueglich 150 % Rabatt"))

    def test_preis_neben_rabatt_wird_nicht_zur_kondition(self) -> None:
        """Der Grundpreis darf nicht versehentlich zur Kondition werden."""
        konditionen = extract_conditions(
            "abzueglich 3 % Rabatt auf den Preis von 12,85 EUR", make_settings())
        self.assertEqual(["discount_percent"], [k.art for k in konditionen])

    def test_mehrere_konditionen_in_einem_satz(self) -> None:
        konditionen = extract_conditions(
            "Preis 12,85 EUR abzueglich 3 % Rabatt und zzgl. 45,00 EUR "
            "Frachtpauschale", make_settings())
        self.assertEqual(["discount_percent", "freight_absolute"],
                         [k.art for k in konditionen])

    def test_konfidenz_ist_bei_richtungswort_niedriger(self) -> None:
        nur_richtung = extract_conditions("abzgl. 3%", make_settings())[0]
        mit_sachbegriff = extract_conditions("abzueglich 3 % Rabatt", make_settings())[0]
        self.assertLess(nur_richtung.konfidenz, mit_sachbegriff.konfidenz)

    def test_dubletten_werden_nur_einmal_gemeldet(self) -> None:
        text = "3 % Rabatt\n3 % Rabatt"
        self.assertEqual(["discount_percent"], arten(text))

    def test_quelltext_wird_mitgefuehrt(self) -> None:
        konditionen = extract_conditions("Wir gewaehren 3 % Mengenrabatt",
                                         make_settings())
        self.assertIn("Mengenrabatt", konditionen[0].quelltext)

    def test_leerer_text(self) -> None:
        self.assertEqual([], arten(""))


class KopfUndPositionsebeneTest(unittest.TestCase):
    """Kopfkonditionen duerfen Positionskonditionen nicht ueberschreiben."""

    def test_kopf_gilt_ohne_positionsangabe(self) -> None:
        ergebnis = merge_head_and_position([kandidat("discount_percent", "3")], [])
        self.assertEqual(["discount_percent"], [k.art for k in ergebnis])
        self.assertEqual("kopf", ergebnis[0].ebene)

    def test_position_gewinnt_bei_gleicher_art(self) -> None:
        ergebnis = merge_head_and_position(
            [kandidat("discount_percent", "3")],
            [kandidat("discount_percent", "5")])
        self.assertEqual(1, len(ergebnis))
        self.assertEqual(Decimal("5"), ergebnis[0].wert)
        self.assertEqual("position", ergebnis[0].ebene)

    def test_andere_arten_werden_ergaenzt(self) -> None:
        ergebnis = merge_head_and_position(
            [kandidat("freight_absolute", "45", "EUR")],
            [kandidat("discount_percent", "5")])
        self.assertEqual({"discount_percent", "freight_absolute"},
                         {k.art for k in ergebnis})

    def test_angebot_bekommt_kopfkondition_an_alle_positionen(self) -> None:
        offer = Offer()
        offer.raw_text = ("Sehr geehrte Damen und Herren,\n"
                          "auf alle Positionen gewaehren wir 3 % Mengenrabatt.\n"
                          "Pos 10 Dichtring 12,85 EUR\n")
        eins = make_position(raw_text="Pos 10 Dichtring 12,85 EUR")
        zwei = make_position(position_number="20",
                             raw_text="Pos 20 O-Ring 8,40 EUR")
        offer.positions = [eins, zwei]
        attach_conditions(offer, make_settings())
        for position in offer.positions:
            self.assertEqual(["discount_percent"],
                             [k.art for k in position.conditions])

    def test_positionsbemerkung_schlaegt_kopf(self) -> None:
        offer = Offer()
        offer.raw_text = "Generell gewaehren wir 3 % Mengenrabatt.\n"
        eins = make_position(remarks="abzueglich 7 % Sonderrabatt")
        zwei = make_position(position_number="20")
        offer.positions = [eins, zwei]
        attach_conditions(offer, make_settings())
        self.assertEqual(Decimal("7"), eins.conditions[0].wert)
        self.assertEqual(Decimal("3"), zwei.conditions[0].wert)

    def test_positionszeile_wird_nicht_zur_kopfkondition(self) -> None:
        """Ein Rabatt in EINER Positionszeile gilt nicht fuer alle."""
        offer = Offer()
        zeile = "Pos 10 Dichtring 12,85 EUR abzueglich 3 % Rabatt"
        offer.raw_text = f"Angebot 4711\n{zeile}\nPos 20 O-Ring 8,40 EUR\n"
        eins = make_position(raw_text=zeile)
        zwei = make_position(position_number="20",
                             raw_text="Pos 20 O-Ring 8,40 EUR")
        offer.positions = [eins, zwei]
        attach_conditions(offer, make_settings())
        self.assertTrue(eins.has_conditions)
        self.assertFalse(zwei.has_conditions,
                         "Positionsrabatt ist auf die zweite Position durchgeschlagen")


# ===========================================================================
# 2. Datenmodell
# ===========================================================================

class DatenmodellTest(unittest.TestCase):

    def test_ohne_konditionen(self) -> None:
        position = make_position()
        self.assertFalse(position.has_conditions)
        self.assertEqual("PB00 12,85 EUR", position.condition_display())

    def test_anzeige_mit_konditionen(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3"),
                               kandidat("freight_absolute", "45.00", "EUR")]
        self.assertTrue(position.has_conditions)
        self.assertEqual("PB00 12,85 EUR, RA01 -3 %, FRB1 +45,00 EUR",
                         position.condition_display())

    def test_anzeige_nutzt_konfigurierte_konditionsarten(self) -> None:
        settings = make_settings()
        settings.conditions.discount_percent = "ZR01"
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.assertIn("ZR01", position.condition_display(settings.conditions))

    def test_infosatz_liefert_zusatzkonditionen(self) -> None:
        umgebung = _Umgebung()
        umgebung.freigeben()
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        umgebung.schreiben(position)
        gelesen = umgebung.gateway.info_records.read("47110001", "0000100234",
                                                     "1000", "1000")
        self.assertEqual(["RA01"],
                         [c.condition_type for c in gelesen.additional_conditions()])
        self.assertIn("RA01", gelesen.conditions_display())


# ===========================================================================
# 3. Schreiben gegen das Testsystem
# ===========================================================================

class SchreibenTest(unittest.TestCase):

    def setUp(self) -> None:
        self.umgebung = _Umgebung()
        self.settings = self.umgebung.settings
        self.umgebung.freigeben()

    def test_konditionen_landen_im_infosatz(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3"),
                               kandidat("freight_absolute", "45.00", "EUR")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        typen = [c["type"] for c in self.umgebung.infosatz()["conditions"]]
        self.assertEqual(["PB00", "RA01", "FRB1"], typen)

    def test_bruttopreis_bleibt_unveraendert(self) -> None:
        """Der Rabatt wird NICHT eingerechnet -- sonst ist die Herkunft weg."""
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)
        self.assertEqual("12.85", self.umgebung.infosatz()["price"])

    def test_reihenfolge_ist_fest(self) -> None:
        """Rabatt vor Zuschlag vor Fracht vor Skonto -- unabhaengig vom Text."""
        position = make_position()
        position.conditions = [kandidat("cash_discount", "2"),
                               kandidat("freight_absolute", "45.00", "EUR"),
                               kandidat("surcharge_percent", "2"),
                               kandidat("discount_percent", "3")]
        plan = plan_conditions(position, self.settings, self.umgebung.gateway.selectors)
        self.assertEqual(["RA01", "ZA01", "FRB1", "SKTO"],
                         [k.condition_type for k in plan.conditions])

    def test_prozentkondition_ohne_waehrung(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)
        zeile = self.umgebung.infosatz()["conditions"][1]
        self.assertTrue(zeile["is_percentage"])
        self.assertEqual("", zeile["currency"])

    def test_absolute_kondition_mit_waehrung(self) -> None:
        position = make_position()
        position.conditions = [kandidat("freight_absolute", "45.00", "EUR")]
        self.umgebung.schreiben(position)
        zeile = self.umgebung.infosatz()["conditions"][1]
        self.assertFalse(zeile["is_percentage"])
        self.assertEqual("EUR", zeile["currency"])

    def test_kappung_bei_zu_vielen(self) -> None:
        self.settings.conditions.max_additional_conditions = 2
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3"),
                               kandidat("surcharge_percent", "2"),
                               kandidat("freight_absolute", "45.00", "EUR")]
        ergebnis = self.umgebung.schreiben(position)
        typen = [c["type"] for c in self.umgebung.infosatz()["conditions"]]
        self.assertEqual(["PB00", "RA01", "ZA01"], typen)
        self.assertIn("gekappt", alle_meldungen(ergebnis))
        self.assertIn("FRB1", alle_meldungen(ergebnis),
                      "Die entfallene Kondition fehlt im Protokoll")

    def test_ohne_konditionen_bleibt_alles_wie_bisher(self) -> None:
        ergebnis = self.umgebung.schreiben(make_position())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(["PB00"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])
        self.assertNotIn("Zusatzkondition", alle_meldungen(ergebnis))

    def test_abgeschaltet_wird_vermerkt(self) -> None:
        self.settings.conditions.write_additional_conditions = False
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertEqual(["PB00"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])
        self.assertIn("write_additional_conditions", alle_meldungen(ergebnis))

    def test_fehlende_konditionsart_wird_gemeldet(self) -> None:
        self.settings.conditions.surcharge_percent = ""
        position = make_position()
        position.conditions = [kandidat("surcharge_percent", "2")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertEqual(["PB00"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])
        self.assertIn("keine SAP-Konditionsart", alle_meldungen(ergebnis))


class UngeprufteFeldIdsTest(unittest.TestCase):
    """Ungeprueft heisst: nur der Bruttopreis, aber mit Vermerk."""

    def setUp(self) -> None:
        self.umgebung = _Umgebung()          # bewusst NICHT freigeben
        self.settings = self.umgebung.settings

    def test_selektoren_sind_im_auslieferungszustand_ungeprueft(self) -> None:
        offen = unverified_condition_selectors(self.umgebung.gateway.selectors)
        self.assertEqual(3, len(offen))

    def test_nur_bruttopreis_mit_vermerk(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(["PB00"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])
        texte = alle_meldungen(ergebnis)
        self.assertIn("Zusatzkonditionen nicht geschrieben", texte)
        self.assertIn("ungeprueft", texte)

    def test_preis_bleibt_trotzdem_korrekt(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)
        self.assertEqual("12.85", self.umgebung.infosatz()["price"])

    def test_teilweise_geprueft_reicht_nicht(self) -> None:
        self.umgebung.gateway.selectors.mark_verified(
            "info_record_conditions", "condition_row_type_cell", True)
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertIn("Zusatzkonditionen nicht geschrieben", alle_meldungen(ergebnis))


class NettopreisEinrechnenTest(unittest.TestCase):
    """``fold_discounts_into_net_price``: einfacher, aber weniger nachvollziehbar."""

    def setUp(self) -> None:
        self.umgebung = _Umgebung()
        self.settings = self.umgebung.settings
        self.settings.conditions.fold_discounts_into_net_price = True
        self.umgebung.freigeben()

    def test_prozentrabatt_wird_eingerechnet(self) -> None:
        position = make_position(price=Decimal("12.85"))
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)
        self.assertEqual("12.46", self.umgebung.infosatz()["price"])

    def test_rechenweg_steht_im_ergebnis(self) -> None:
        position = make_position(price=Decimal("12.85"))
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        texte = alle_meldungen(ergebnis)
        self.assertIn("Nettopreis 12,46 = 12,85 abzueglich 3 %", texte)

    def test_absoluter_rabatt_wird_eingerechnet(self) -> None:
        position = make_position(price=Decimal("100.00"))
        position.conditions = [kandidat("discount_absolute", "10.00", "EUR")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertEqual("90.00", self.umgebung.infosatz()["price"])
        self.assertIn("abzueglich 10,00 EUR", alle_meldungen(ergebnis))

    def test_eingerechneter_rabatt_ist_keine_kondition_mehr(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)
        self.assertEqual(["PB00"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])

    def test_fracht_bleibt_eigene_kondition(self) -> None:
        """Eingerechnet werden nur Rabatte -- Fracht bleibt sichtbar."""
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3"),
                               kandidat("freight_absolute", "45.00", "EUR")]
        self.umgebung.schreiben(position)
        self.assertEqual(["PB00", "FRB1"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])

    def test_skonto_wird_nie_eingerechnet(self) -> None:
        """Skonto haengt am Zahlungsverhalten und gehoert nicht in den Preis."""
        position = make_position()
        position.conditions = [kandidat("cash_discount", "2")]
        self.umgebung.schreiben(position)
        self.assertEqual("12.85", self.umgebung.infosatz()["price"])
        self.assertEqual(["PB00", "SKTO"],
                         [c["type"] for c in self.umgebung.infosatz()["conditions"]])

    def test_negativer_nettopreis_wird_abgelehnt(self) -> None:
        position = make_position(price=Decimal("10.00"))
        position.conditions = [kandidat("discount_absolute", "10.00", "EUR")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertEqual("10.00", self.umgebung.infosatz()["price"])
        self.assertIn("nicht plausibel", alle_meldungen(ergebnis))

    def test_ruecklese_pruefung_erwartet_den_nettopreis(self) -> None:
        position = make_position(price=Decimal("12.85"))
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("bestaetigt", alle_meldungen(ergebnis))


class RuecklesePruefungKonditionenTest(unittest.TestCase):
    """Auch die Zusatzkonditionen muessen nach dem Sichern nachweisbar sein."""

    def setUp(self) -> None:
        self.umgebung = _Umgebung()
        self.settings = self.umgebung.settings
        self.umgebung.freigeben()

    def test_geschriebene_konditionen_werden_bestaetigt(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        ergebnis = self.umgebung.schreiben(position)
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("Zusatzkonditionen bestaetigt", alle_meldungen(ergebnis))

    def test_fehlende_kondition_faellt_auf(self) -> None:
        """SAP meldet 'gesichert', hat die Kondition aber verworfen."""
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)

        gelesen = self.umgebung.gateway.info_records.read("47110001", "0000100234",
                                                          "1000", "1000")
        gelesen.conditions = [c for c in gelesen.conditions
                              if c.condition_type == "PB00"]
        plan = plan_conditions(position, self.settings,
                               self.umgebung.gateway.selectors)
        in_ordnung, hinweise = verify_info_record_write(
            gelesen, position, self.umgebung.gateway.write_context(), self.settings,
            expected_conditions=plan.conditions)
        self.assertFalse(in_ordnung)
        self.assertIn("RA01", hinweise[0])
        self.assertIn("fehlt", hinweise[0])

    def test_abweichender_konditionsbetrag_faellt_auf(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.umgebung.schreiben(position)

        gelesen = self.umgebung.gateway.info_records.read("47110001", "0000100234",
                                                          "1000", "1000")
        for kondition in gelesen.conditions:
            if kondition.condition_type == "RA01":
                kondition.amount = Decimal("5")
        plan = plan_conditions(position, self.settings,
                               self.umgebung.gateway.selectors)
        in_ordnung, hinweise = verify_info_record_write(
            gelesen, position, self.umgebung.gateway.write_context(), self.settings,
            expected_conditions=plan.conditions)
        self.assertFalse(in_ordnung)
        self.assertIn("RA01", hinweise[0])

    def test_ohne_erwartung_bleibt_alles_beim_alten(self) -> None:
        """Aufrufer ohne Konditionen duerfen sich nicht veraendert verhalten."""
        position = make_position()
        self.umgebung.schreiben(position)
        gelesen = self.umgebung.gateway.info_records.read("47110001", "0000100234",
                                                          "1000", "1000")
        in_ordnung, _ = verify_info_record_write(
            gelesen, position, self.umgebung.gateway.write_context(), self.settings)
        self.assertTrue(in_ordnung)


# ===========================================================================
# 4. Pruefungen
# ===========================================================================

class PruefungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.pruefung = ValidationService(self.settings)
        self.offer = Offer(vendor_number="0000100234")

    def _pruefen(self, position: OfferPosition):
        self.offer.positions = [position]
        self.pruefung.validate_position(position, self.offer)
        return {i.code: i for i in position.issues}

    def test_rabatt_erkannt_aber_abgeschaltet_warnt(self) -> None:
        self.settings.conditions.write_additional_conditions = False
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        befunde = self._pruefen(position)
        self.assertIn("discount_not_written", befunde)
        self.assertIs(IssueSeverity.WARNING, befunde["discount_not_written"].severity)
        self.assertFalse(befunde["discount_not_written"].blocking)

    def test_keine_warnung_wenn_eingerechnet_wird(self) -> None:
        self.settings.conditions.write_additional_conditions = False
        self.settings.conditions.fold_discounts_into_net_price = True
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.assertNotIn("discount_not_written", self._pruefen(position))

    def test_keine_warnung_im_normalfall(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "3")]
        self.assertNotIn("discount_not_written", self._pruefen(position))

    def test_zu_hoher_rabatt_warnt(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "70")]
        befunde = self._pruefen(position)
        self.assertIn("condition_discount_implausible", befunde)

    def test_rabatt_an_der_grenze_warnt_nicht(self) -> None:
        position = make_position()
        position.conditions = [kandidat("discount_percent", "50")]
        self.assertNotIn("condition_discount_implausible", self._pruefen(position))

    def test_zu_hoher_zuschlag_warnt(self) -> None:
        position = make_position()
        position.conditions = [kandidat("surcharge_percent", "120")]
        self.assertIn("condition_surcharge_implausible", self._pruefen(position))

    def test_fracht_ueber_positionswert_warnt(self) -> None:
        position = make_position(price=Decimal("12.85"), quantity=Decimal("1"))
        position.conditions = [kandidat("freight_absolute", "500.00", "EUR")]
        self.assertIn("condition_freight_implausible", self._pruefen(position))

    def test_normale_fracht_warnt_nicht(self) -> None:
        position = make_position(price=Decimal("12.85"), quantity=Decimal("100"))
        position.conditions = [kandidat("freight_absolute", "45.00", "EUR")]
        self.assertNotIn("condition_freight_implausible", self._pruefen(position))

    def test_fremde_waehrung_blockiert(self) -> None:
        position = make_position(currency="EUR")
        position.conditions = [kandidat("freight_absolute", "45.00", "USD")]
        befunde = self._pruefen(position)
        self.assertIn("condition_currency_mismatch", befunde)
        self.assertTrue(befunde["condition_currency_mismatch"].blocking)
        self.assertTrue(position.issues.has_blocking)

    def test_gleiche_waehrung_blockiert_nicht(self) -> None:
        position = make_position(currency="EUR")
        position.conditions = [kandidat("freight_absolute", "45.00", "EUR")]
        self.assertNotIn("condition_currency_mismatch", self._pruefen(position))

    def test_prozentkondition_hat_keine_waehrungspruefung(self) -> None:
        position = make_position(currency="USD")
        position.conditions = [kandidat("discount_percent", "3")]
        self.assertNotIn("condition_currency_mismatch", self._pruefen(position))

    def test_ohne_konditionen_keine_befunde(self) -> None:
        befunde = self._pruefen(make_position())
        for code in ("discount_not_written", "condition_currency_mismatch",
                     "condition_discount_implausible"):
            self.assertNotIn(code, befunde)


if __name__ == "__main__":
    unittest.main(verbosity=2)
