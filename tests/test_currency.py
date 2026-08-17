"""Tests der Fremdwaehrungsbehandlung.

Aufruf:  ``python -m unittest tests.test_currency -v``

Der entscheidende Punkt, den diese Tests absichern: Ein umgerechneter Betrag
dient ausschliesslich dem Vergleich.  Er darf niemals in die Position
zurueckgeschrieben werden und geht damit auch nie nach SAP.

Wie in den uebrigen Testmodulen wird das Basisverzeichnis der Anwendung *vor*
dem Import der Anwendungsmodule auf ein temporaeres Verzeichnis umgebogen.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="sap_angebot_waehrung_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import InfoRecordAction                           # noqa: E402
from app.models.offer import Offer                                      # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.models.sap_info_record import SapInfoRecord                    # noqa: E402
from app.services.comparison_service import (                           # noqa: E402
    ComparisonService,
    price_change_percent,
)
from app.services.currency_service import (                             # noqa: E402
    ConversionResult,
    CurrencyService,
)
from app.services.preview_service import PreviewService                 # noqa: E402


def tearDownModule() -> None:
    os.environ.pop("SAP_ANGEBOT_HOME", None)
    try:
        _TEMP_HOME.cleanup()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def make_settings() -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = True
    settings.currency.company_currency = "EUR"
    settings.currency.convert_for_comparison = True
    settings.currency.exchange_rates = {"USD": "0.92", "CHF": "1.06", "GBP": "1.17"}
    settings.currency.rate_date = ""
    settings.currency.max_rate_age_days = 30
    return settings


def make_position(**overrides) -> OfferPosition:
    values = dict(
        position_number="10",
        material_number="47110001",
        vendor_number="0000100234",
        description="Dichtring NBR 40x52x7",
        quantity=Decimal("100"),
        uom="ST",
        price=Decimal("14.20"),
        price_unit=1,
        currency="USD",
        purchasing_org="1000",
        plant="1000",
        valid_from=date.today(),
        selected=True,
        do_info_record=True,
    )
    values.update(overrides)
    return OfferPosition(**values)


def make_record(price: str = "12.40", price_unit: int = 1, currency: str = "EUR",
                order_unit: str = "ST", exists: bool = True) -> SapInfoRecord:
    return SapInfoRecord(
        material_number="47110001", vendor_number="0000100234",
        purchasing_org="1000", plant="1000",
        exists=exists, info_record_number="5300000123",
        price=Decimal(price) if exists else None,
        price_unit=price_unit if exists else None,
        currency=currency if exists else "",
        order_unit=order_unit if exists else "",
        valid_from=date.today() - timedelta(days=365),
        valid_to=date(2099, 12, 31),
        read_at=datetime.now(),
    )


def loaded(position: OfferPosition, record: SapInfoRecord) -> OfferPosition:
    position.sap_info_record = record
    position.sap_loaded = True
    return position


def make_offer(positions: list[OfferPosition]) -> Offer:
    offer = Offer(vendor_name="Muster GmbH", vendor_number="0000100234",
                  offer_number="AG-2026-4711", offer_date=date.today(),
                  currency="USD")
    offer.positions = positions
    return offer


# ===========================================================================
# Kurse
# ===========================================================================

class TestRates(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = CurrencyService(self.settings)

    def test_known_rate(self):
        self.assertEqual(Decimal("0.92"), self.service.rate("USD"))

    def test_known_rate_case_insensitive(self):
        self.assertEqual(Decimal("0.92"), self.service.rate("usd"))

    def test_unknown_currency_yields_none(self):
        # Der gefaehrlichste denkbare Fehler waere hier ein angenommener Kurs 1.
        self.assertIsNone(self.service.rate("JPY"))

    def test_same_currency_yields_one(self):
        self.assertEqual(Decimal(1), self.service.rate("USD", "USD"))

    def test_home_currency_yields_one(self):
        self.assertEqual(Decimal(1), self.service.rate("EUR"))

    def test_empty_currency_yields_none(self):
        self.assertIsNone(self.service.rate(""))

    def test_rate_between_two_foreign_currencies(self):
        # 1 CHF = 1,06 EUR, 1 USD = 0,92 EUR  ->  1 CHF = 1,06/0,92 USD
        erwartet = Decimal("1.06") / Decimal("0.92")
        self.assertEqual(erwartet, self.service.rate("CHF", "USD"))

    def test_rate_from_home_to_foreign(self):
        self.assertEqual(Decimal(1) / Decimal("0.92"), self.service.rate("EUR", "USD"))

    def test_rate_with_unknown_target_yields_none(self):
        self.assertIsNone(self.service.rate("USD", "JPY"))

    def test_broken_rate_value_is_ignored(self):
        self.settings.currency.exchange_rates["USD"] = "keine Zahl"
        self.assertIsNone(self.service.rate("USD"))

    def test_empty_rate_value_is_ignored(self):
        self.settings.currency.exchange_rates["USD"] = ""
        self.assertIsNone(self.service.rate("USD"))

    def test_zero_rate_is_ignored(self):
        self.settings.currency.exchange_rates["USD"] = "0"
        self.assertIsNone(self.service.rate("USD"))

    def test_negative_rate_is_ignored(self):
        self.settings.currency.exchange_rates["USD"] = "-0.92"
        self.assertIsNone(self.service.rate("USD"))

    def test_rate_with_comma_decimal(self):
        self.settings.currency.exchange_rates["USD"] = "0,92"
        self.assertEqual(Decimal("0.92"), self.service.rate("USD"))

    def test_other_home_currency(self):
        self.settings.currency.company_currency = "USD"
        self.assertEqual(Decimal(1), self.service.rate("USD"))


# ===========================================================================
# Kursalter
# ===========================================================================

class TestRateAge(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = CurrencyService(self.settings)

    def test_age_without_date_is_none(self):
        self.assertIsNone(self.service.rate_age_days())

    def test_age_without_date_is_not_stale(self):
        self.assertFalse(self.service.is_stale())

    def test_age_is_calculated(self):
        tag = date.today() - timedelta(days=5)
        self.settings.currency.rate_date = tag.strftime("%d.%m.%Y")
        self.assertEqual(5, self.service.rate_age_days())

    def test_fresh_rates_are_not_stale(self):
        tag = date.today() - timedelta(days=5)
        self.settings.currency.rate_date = tag.strftime("%d.%m.%Y")
        self.assertFalse(self.service.is_stale())

    def test_old_rates_are_stale(self):
        tag = date.today() - timedelta(days=100)
        self.settings.currency.rate_date = tag.strftime("%d.%m.%Y")
        self.assertTrue(self.service.is_stale())

    def test_broken_rate_date_is_ignored(self):
        self.settings.currency.rate_date = "irgendwann"
        self.assertIsNone(self.service.rate_age_days())
        self.assertFalse(self.service.is_stale())


# ===========================================================================
# Umrechnung
# ===========================================================================

class TestConversion(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = CurrencyService(self.settings)

    def test_convert_amount(self):
        # 14,20 USD * 0,92 = 13,064 EUR
        self.assertEqual(Decimal("13.0640"), self.service.convert(Decimal("14.20"), "USD"))

    def test_convert_none_amount(self):
        self.assertIsNone(self.service.convert(None, "USD"))

    def test_convert_without_rate(self):
        self.assertIsNone(self.service.convert(Decimal("10"), "JPY"))

    def test_convert_same_currency_keeps_amount(self):
        self.assertEqual(Decimal("14.20"), self.service.convert(Decimal("14.20"), "EUR"))

    def test_convert_rounds_to_four_decimals(self):
        # 3,3333 USD * 0,92 = 3,06663_6 -> kaufmaennisch 3,0666
        self.assertEqual(Decimal("3.0666"), self.service.convert(Decimal("3.3333"), "USD"))

    def test_conversion_result_carries_origin(self):
        ergebnis = self.service.conversion(Decimal("14.20"), "USD", "EUR")
        self.assertIsInstance(ergebnis, ConversionResult)
        self.assertEqual(Decimal("14.20"), ergebnis.amount)
        self.assertEqual("USD", ergebnis.currency)
        self.assertEqual("EUR", ergebnis.target_currency)
        self.assertEqual(Decimal("0.92"), ergebnis.rate)
        self.assertEqual(Decimal("13.0640"), ergebnis.converted)
        self.assertIn("13,06", ergebnis.note)
        self.assertIn("0,92", ergebnis.note)

    def test_conversion_result_without_rate(self):
        ergebnis = self.service.conversion(Decimal("14.20"), "JPY", "EUR")
        self.assertFalse(ergebnis.ok)
        self.assertIsNone(ergebnis.converted)
        self.assertEqual("Nicht vergleichbar: kein Kurs fuer JPY hinterlegt",
                         ergebnis.note)

    def test_conversion_result_when_disabled(self):
        self.settings.currency.convert_for_comparison = False
        ergebnis = self.service.conversion(Decimal("14.20"), "USD", "EUR")
        self.assertFalse(ergebnis.ok)
        self.assertIn("abgeschaltet", ergebnis.note)


# ===========================================================================
# Klartextmeldungen
# ===========================================================================

class TestProblems(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.settings.currency.rate_date = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")
        self.service = CurrencyService(self.settings)

    def test_no_foreign_currency_no_problem(self):
        self.assertEqual([], self.service.problems({"EUR"}))

    def test_known_currency_no_problem(self):
        self.assertEqual([], self.service.problems({"EUR", "USD"}))

    def test_missing_rate_is_reported(self):
        meldungen = self.service.problems({"JPY"})
        self.assertTrue(any("JPY" in m for m in meldungen))

    def test_stale_rates_are_reported(self):
        self.settings.currency.rate_date = (date.today() - timedelta(days=99)).strftime("%d.%m.%Y")
        meldungen = self.service.problems({"USD"})
        self.assertTrue(any("99" in m for m in meldungen))

    def test_missing_rate_date_is_reported(self):
        self.settings.currency.rate_date = ""
        meldungen = self.service.problems({"USD"})
        self.assertTrue(any("Pflegedatum" in m for m in meldungen))

    def test_disabled_conversion_is_reported(self):
        self.settings.currency.convert_for_comparison = False
        meldungen = self.service.problems({"USD"})
        self.assertEqual(1, len(meldungen))
        self.assertIn("abgeschaltet", meldungen[0])


# ===========================================================================
# Vergleich ueber die Waehrungsgrenze
# ===========================================================================

class TestComparisonAcrossCurrencies(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = ComparisonService(self.settings)

    def test_percent_is_calculated_across_currencies(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        # Von Hand: 14,20 * 0,92 = 13,0640 EUR gegen 12,40 EUR
        erwartet = (Decimal("13.0640") - Decimal("12.40")) / Decimal("12.40") * Decimal(100)
        self.assertEqual(erwartet, price_change_percent(position, self.settings))

    def test_percent_text_across_currencies(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.assertEqual("+5,35 %", self.service.price_delta_text(position))

    def test_percent_respects_price_unit(self):
        position = loaded(make_position(price=Decimal("142.00"), price_unit=10,
                                        currency="USD"),
                          make_record("12.40", price_unit=1, currency="EUR"))
        erwartet = (Decimal("13.0640") - Decimal("12.40")) / Decimal("12.40") * Decimal(100)
        self.assertEqual(erwartet, price_change_percent(position, self.settings))

    def test_missing_rate_yields_no_percent(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="JPY"),
                          make_record("12.40", currency="EUR"))
        self.assertIsNone(price_change_percent(position, self.settings))

    def test_missing_rate_is_not_zero_percent(self):
        position = loaded(make_position(price=Decimal("12.40"), currency="JPY"),
                          make_record("12.40", currency="EUR"))
        self.assertIsNone(price_change_percent(position, self.settings))
        self.assertEqual("", self.service.price_delta_text(position))

    def test_disabled_conversion_yields_no_percent(self):
        self.settings.currency.convert_for_comparison = False
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.assertIsNone(price_change_percent(position, self.settings))

    def test_same_currency_still_works(self):
        position = loaded(make_position(price=Decimal("12.85"), currency="EUR"),
                          make_record("12.40", currency="EUR"))
        erwartet = (Decimal("12.85") - Decimal("12.40")) / Decimal("12.40") * Decimal(100)
        self.assertEqual(erwartet, price_change_percent(position, self.settings))

    def test_currency_change_never_yields_unchanged(self):
        # Gleicher Zahlenwert, andere Waehrung -> trotzdem eine Aenderung.
        position = loaded(make_position(price=Decimal("12.40"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        self.assertIs(InfoRecordAction.UPDATE, position.info_record_action)

    def test_currency_change_without_rate_never_yields_unchanged(self):
        position = loaded(make_position(price=Decimal("12.40"), currency="JPY"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        self.assertIs(InfoRecordAction.UPDATE, position.info_record_action)

    def test_equal_converted_amount_still_yields_update(self):
        # 13,4783 USD * 0,92 = 12,4000 EUR -- rechnerisch derselbe Wert.
        position = loaded(make_position(price=Decimal("13.4783"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        self.assertIs(InfoRecordAction.UPDATE, position.info_record_action)


# ===========================================================================
# Darstellung
# ===========================================================================

class TestComparisonTexts(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = ComparisonService(self.settings)

    def test_describe_change_shows_rate_and_converted_value(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        beschreibung = self.service.describe_change(position)
        self.assertEqual("14,20 USD (= 13,06 EUR bei Kurs 0,92)",
                         beschreibung["Neuer Preis"])
        self.assertEqual("+5,35 % (umgerechnet)", beschreibung["Änderung"])
        self.assertIn("Originalbetrag", beschreibung["Währung"])

    def test_describe_change_without_rate(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="JPY"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        beschreibung = self.service.describe_change(position)
        self.assertEqual("Nicht vergleichbar: kein Kurs fuer JPY hinterlegt",
                         beschreibung["Änderung"])
        self.assertEqual("14,20 JPY", beschreibung["Neuer Preis"])

    def test_describe_change_same_currency_unchanged_format(self):
        position = loaded(make_position(price=Decimal("12.85"), currency="EUR"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        beschreibung = self.service.describe_change(position)
        self.assertEqual("12,85 EUR", beschreibung["Neuer Preis"])
        self.assertEqual("+3,63 %", beschreibung["Änderung"])
        self.assertNotIn("Währung", beschreibung)

    def test_comparison_note_for_foreign_currency(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.assertEqual("= 13,06 EUR bei Kurs 0,92",
                         self.service.comparison_note(position))

    def test_comparison_note_empty_for_home_currency(self):
        position = loaded(make_position(price=Decimal("12.85"), currency="EUR"),
                          make_record("12.40", currency="EUR"))
        self.assertEqual("", self.service.comparison_note(position))

    def test_comparison_note_without_rate(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="JPY"),
                          make_record("12.40", currency="EUR"))
        self.assertIn("kein Kurs", self.service.comparison_note(position))

    def test_comparison_note_mentions_stale_rate(self):
        self.settings.currency.rate_date = (date.today() - timedelta(days=90)).strftime("%d.%m.%Y")
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.assertIn("90 Tage alt", self.service.comparison_note(position))


# ===========================================================================
# Vorschau
# ===========================================================================

class TestPreviewCurrency(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.settings.currency.rate_date = (date.today() - timedelta(days=3)).strftime("%d.%m.%Y")
        self.comparison = ComparisonService(self.settings)
        self.preview = PreviewService(self.settings)

    def _summary(self, position: OfferPosition):
        offer = make_offer([position])
        self.comparison.compare_offer(offer)
        return self.preview.build(offer, self.settings)

    def test_foreign_currency_is_listed(self):
        summary = self._summary(loaded(make_position(currency="USD"),
                                       make_record("12.40", currency="EUR")))
        self.assertEqual(["USD"], summary.foreign_currencies)

    def test_preview_names_rate_and_age(self):
        summary = self._summary(loaded(make_position(currency="USD"),
                                       make_record("12.40", currency="EUR")))
        text = summary.as_text()
        self.assertIn("USD", text)
        self.assertIn("0,92", text)
        self.assertIn("3 Tage", text)

    def test_preview_states_original_currency_is_written(self):
        summary = self._summary(loaded(make_position(currency="USD"),
                                       make_record("12.40", currency="EUR")))
        self.assertIn("Originalwährung", summary.as_text())

    def test_missing_rate_lands_in_blocking(self):
        summary = self._summary(loaded(make_position(currency="JPY"),
                                       make_record("12.40", currency="EUR")))
        self.assertTrue(any("JPY" in eintrag for eintrag in summary.blocking))

    def test_known_rate_does_not_block(self):
        summary = self._summary(loaded(make_position(currency="USD"),
                                       make_record("12.40", currency="EUR")))
        self.assertEqual([], summary.blocking)

    def test_home_currency_has_no_currency_lines(self):
        summary = self._summary(loaded(make_position(currency="EUR"),
                                       make_record("12.40", currency="EUR")))
        self.assertEqual([], summary.currency_lines)
        self.assertEqual([], summary.foreign_currencies)

    def test_disabled_conversion_is_shown_and_does_not_block(self):
        self.settings.currency.convert_for_comparison = False
        summary = self._summary(loaded(make_position(currency="USD"),
                                       make_record("12.40", currency="EUR")))
        self.assertIn("abgeschaltet", summary.as_text())
        self.assertEqual([], summary.blocking)


# ===========================================================================
# Der wichtigste Test ueberhaupt
# ===========================================================================

class TestOriginalAmountIsNeverOverwritten(unittest.TestCase):
    """Kein Pfad darf einen umgerechneten Betrag in die Position schreiben."""

    def setUp(self) -> None:
        self.settings = make_settings()
        self.comparison = ComparisonService(self.settings)
        self.preview = PreviewService(self.settings)

    def test_original_price_and_currency_survive_all_steps(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        offer = make_offer([position])

        self.comparison.compare_offer(offer)
        self.comparison.describe_change(position)
        self.comparison.comparison_note(position)
        self.comparison.price_delta_text(position)
        self.comparison.info_record_text(position)
        price_change_percent(position, self.settings)
        self.preview.build(offer, self.settings)

        self.assertEqual(Decimal("14.20"), position.price)
        self.assertEqual("USD", position.currency)

    def test_conversion_does_not_touch_the_position(self):
        position = loaded(make_position(price=Decimal("14.20"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        ergebnis = self.comparison.conversion_for(position)
        self.assertEqual(Decimal("13.0640"), ergebnis.converted)
        self.assertEqual(Decimal("14.20"), position.price)
        self.assertEqual("USD", position.currency)


if __name__ == "__main__":
    unittest.main(verbosity=2)
