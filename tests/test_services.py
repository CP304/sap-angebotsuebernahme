"""Tests der Fachlogik: Vergleich, Validierung, Vorschau, Komplettvorgang, Undo.

Aufruf:  ``python -m unittest tests.test_services -v``

Die Tests laufen ausschliesslich gegen das eingebaute Testsystem (Mock-SAP).
Damit dabei keine echten Anwenderdaten angefasst werden, wird das
Basisverzeichnis der Anwendung *vor* dem Import der Anwendungsmodule auf ein
temporaeres Verzeichnis umgebogen (Umgebungsvariable ``SAP_ANGEBOT_HOME``).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="sap_angebot_tests_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import (                                          # noqa: E402
    FieldOrigin,
    InfoRecordAction,
    IssueSeverity,
    PositionStatus,
    ResultState,
    SourceListAction,
)
from app.models.offer import Offer                                      # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.models.sap_info_record import SapInfoRecord                    # noqa: E402
from app.models.sap_source_list import SapSourceList, SourceListEntry   # noqa: E402
from app.sap.connection import SapError                                 # noqa: E402
from app.sap.gateway import SapGateway
from app.sap.mock_backend import MockSapSystem                                  # noqa: E402
from app.sap.interfaces import VendorMatch                              # noqa: E402
from app.sap.message_guard import MessageSuppressionError               # noqa: E402
from app.services.batch_service import BatchProcessor, ProgressEvent    # noqa: E402
from app.services.comparison_service import ComparisonService           # noqa: E402
from app.services.preview_service import PreviewService, call_off_quantity  # noqa: E402
from app.services.undo_service import UndoService                       # noqa: E402
from app.services.validation_service import ValidationService           # noqa: E402


def tearDownModule() -> None:
    os.environ.pop("SAP_ANGEBOT_HOME", None)
    try:
        _TEMP_HOME.cleanup()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def make_settings(dry_run: bool = False) -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = dry_run
    return settings


def make_position(**overrides) -> OfferPosition:
    """Vollstaendig gepflegte Position -- Abweichungen per Schluesselwort."""
    values = dict(
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
    values.update(overrides)
    return OfferPosition(**values)


def make_offer(positions: list[OfferPosition] | None = None, **overrides) -> Offer:
    values = dict(
        vendor_name="Muster Dichtungstechnik GmbH",
        vendor_number="0000100234",
        offer_number="AG-2026-4711",
        offer_date=date.today(),
        currency="EUR",
    )
    values.update(overrides)
    offer = Offer(**values)
    offer.positions = positions or []
    return offer


def make_record(price: str = "12.40", price_unit: int = 1, currency: str = "EUR",
                order_unit: str = "ST", exists: bool = True,
                valid_from: date | None = None, valid_to: date | None = None,
                read: bool = True) -> SapInfoRecord:
    return SapInfoRecord(
        material_number="47110001", vendor_number="0000100234",
        purchasing_org="1000", plant="1000",
        exists=exists, info_record_number="5300000123",
        price=Decimal(price) if exists else None,
        price_unit=price_unit if exists else None,
        currency=currency if exists else "",
        order_unit=order_unit if exists else "",
        valid_from=valid_from or date.today() - timedelta(days=365),
        valid_to=valid_to or date(2099, 12, 31),
        read_at=datetime.now() if read else None,
    )


def make_source_list(entries: list[SourceListEntry] | None = None) -> SapSourceList:
    source_list = SapSourceList(material_number="47110001", plant="1000",
                                read_at=datetime.now())
    source_list.entries = entries or []
    source_list.exists = bool(source_list.entries)
    return source_list


def make_entry(**overrides) -> SourceListEntry:
    values = dict(
        vendor_number="0000100234", plant="1000", purchasing_org="1000",
        valid_from=date.today() - timedelta(days=365), valid_to=date(2099, 12, 31),
        fixed=True, blocked=False, mrp_indicator="1", agreement="",
    )
    values.update(overrides)
    return SourceListEntry(**values)


def codes(position: OfferPosition) -> set[str]:
    return {issue.code for issue in position.issues}


def issue_with(position: OfferPosition, code: str):
    for issue in position.issues:
        if issue.code == code:
            return issue
    return None


def loaded(position: OfferPosition, record: SapInfoRecord | None = None,
           source_list: SapSourceList | None = None) -> OfferPosition:
    """Position so vorbereiten, als waere der SAP-Stand gelesen worden."""
    position.sap_info_record = record
    position.sap_source_list = source_list
    position.sap_loaded = True
    position.material_exists = True
    position.vendor_exists = True
    return position


# ===========================================================================
# Vergleich -- Infosatz
# ===========================================================================

class TestComparisonInfoRecord(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = ComparisonService(self.settings)

    def test_not_read_yields_none(self):
        position = make_position()
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.NONE)

    def test_missing_record_yields_create(self):
        position = loaded(make_position(), make_record(exists=False))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.CREATE)

    def test_identical_price_yields_unchanged(self):
        position = loaded(make_position(price=Decimal("12.40")), make_record("12.40"))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UNCHANGED)

    def test_price_unit_normalisation_is_no_change(self):
        """12,40 je 1 ST und 124,00 je 10 ST sind derselbe Preis."""
        position = loaded(make_position(price=Decimal("124.00"), price_unit=10),
                          make_record("12.40", price_unit=1))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UNCHANGED)

    def test_price_unit_normalisation_other_direction(self):
        position = loaded(make_position(price=Decimal("0.84"), price_unit=1),
                          make_record("8.40", price_unit=10))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UNCHANGED)

    def test_changed_price_yields_update(self):
        position = loaded(make_position(price=Decimal("12.85")), make_record("12.40"))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UPDATE)

    def test_changed_currency_yields_update(self):
        position = loaded(make_position(price=Decimal("12.40"), currency="USD"),
                          make_record("12.40", currency="EUR"))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UPDATE)

    def test_changed_uom_yields_update(self):
        position = loaded(make_position(price=Decimal("12.40"), uom="M"),
                          make_record("12.40", order_unit="ST"))
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UPDATE)

    def test_expired_validity_yields_update(self):
        record = make_record("12.40", valid_to=date.today() - timedelta(days=10))
        position = loaded(make_position(price=Decimal("12.40")), record)
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.UPDATE)

    def test_missing_material_yields_blocked(self):
        position = loaded(make_position(material_number=""), make_record())
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.BLOCKED)

    def test_missing_vendor_yields_blocked(self):
        position = loaded(make_position(vendor_number=""), make_record())
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.BLOCKED)

    def test_missing_price_yields_blocked(self):
        position = loaded(make_position(price=None), make_record())
        self.service.compare_position(position)
        self.assertIs(position.info_record_action, InfoRecordAction.BLOCKED)
        self.assertIn("kein Preis erkannt",
                      self.service.info_record_blocking_reasons(position))


# ===========================================================================
# Vergleich -- Orderbuch
# ===========================================================================

class TestComparisonSourceList(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = ComparisonService(self.settings)

    def test_not_read_yields_none(self):
        position = make_position(do_source_list=True)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.NONE)

    def test_no_entry_for_vendor_yields_create(self):
        source_list = make_source_list([make_entry(vendor_number="0000109999")])
        position = loaded(make_position(do_source_list=True), make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.CREATE)

    def test_matching_entry_yields_unchanged(self):
        source_list = make_source_list([make_entry()])
        position = loaded(make_position(do_source_list=True), make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.UNCHANGED)

    def test_blocked_entry_yields_update(self):
        source_list = make_source_list([make_entry(blocked=True)])
        position = loaded(make_position(do_source_list=True), make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.UPDATE)
        self.assertIn("Eintrag ist gesperrt",
                      self.service.source_list_update_reasons(position))

    def test_expiring_validity_yields_update(self):
        source_list = make_source_list([make_entry(valid_to=date.today() + timedelta(days=30))])
        position = loaded(make_position(do_source_list=True), make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.UPDATE)

    def test_different_mrp_indicator_yields_update(self):
        source_list = make_source_list([make_entry(mrp_indicator="")])
        position = loaded(make_position(do_source_list=True), make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.UPDATE)
        self.assertIn("Dispokennzeichen weicht ab",
                      self.service.source_list_update_reasons(position))

    def test_contract_reference_yields_update(self):
        source_list = make_source_list([make_entry()])
        position = loaded(make_position(do_source_list=True, do_contract=True),
                          make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.UPDATE)

    def test_missing_plant_yields_blocked(self):
        source_list = make_source_list([make_entry()])
        position = loaded(make_position(do_source_list=True, plant=""),
                          make_record(), source_list)
        self.service.compare_position(position)
        self.assertIs(position.source_list_action, SourceListAction.BLOCKED)


# ===========================================================================
# Vergleich -- Darstellung
# ===========================================================================

class TestComparisonTexts(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.service = ComparisonService(self.settings)

    def test_price_delta_text_increase(self):
        position = loaded(make_position(price=Decimal("12.85")), make_record("12.40"))
        self.assertEqual("+3,63 %", self.service.price_delta_text(position))

    def test_price_delta_text_decrease(self):
        position = loaded(make_position(price=Decimal("12.35")), make_record("12.50"))
        self.assertEqual("–1,20 %", self.service.price_delta_text(position))

    def test_price_delta_text_without_record(self):
        position = loaded(make_position(), make_record(exists=False))
        self.assertEqual("", self.service.price_delta_text(position))

    def test_describe_change(self):
        source_list = make_source_list([make_entry()])
        position = loaded(
            make_position(price=Decimal("12.85"), valid_from=date(2026, 9, 1),
                          do_source_list=True),
            make_record("12.40"), source_list)
        self.service.compare_position(position)
        described = self.service.describe_change(position)
        self.assertEqual("12,40 EUR", described["Alter Preis"])
        self.assertEqual("12,85 EUR", described["Neuer Preis"])
        self.assertEqual("+3,63 %", described["Änderung"])
        self.assertEqual("1 ST", described["Preiseinheit"])
        self.assertEqual("01.09.2026", described["Gültig ab"])
        self.assertEqual("Lieferant vorhanden – keine Änderung notwendig",
                         described["Orderbuch"])


# ===========================================================================
# Validierung
# ===========================================================================

class TestValidation(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.comparison = ComparisonService(self.settings)
        self.validation = ValidationService(self.settings)

    def _check(self, position: OfferPosition, offer: Offer | None = None) -> OfferPosition:
        offer = offer or make_offer([position])
        if position not in offer.positions:
            offer.positions.append(position)
        self.comparison.compare_position(position)
        self.validation.validate_position(position, offer)
        return position

    # -- Material -------------------------------------------------------
    def test_material_missing_blocks(self):
        position = self._check(make_position(material_number=""))
        self.assertIn("material_missing", codes(position))
        self.assertTrue(issue_with(position, "material_missing").blocking)
        self.assertIs(position.status, PositionStatus.ERROR)

    def test_material_not_in_sap_blocks(self):
        position = make_position()
        position.material_exists = False
        self._check(position)
        self.assertIn("material_not_in_sap", codes(position))
        self.assertTrue(position.issues.has_blocking)

    def test_material_purchasing_blocked(self):
        position = make_position()

        class _Material:
            exists = True
            description = "Schlauchschelle"
            purchasing_blocked = True

        self.validation.apply_sap_flags(position, material=_Material())
        self._check(position)
        self.assertIn("material_purchasing_blocked", codes(position))
        self.assertTrue(position.issues.has_blocking)

    # -- Lieferant ------------------------------------------------------
    def test_vendor_unresolved_blocks(self):
        position = self._check(make_position(vendor_number=""))
        self.assertIn("vendor_unresolved", codes(position))
        self.assertTrue(position.issues.has_blocking)

    def test_vendor_not_in_sap_blocks(self):
        position = make_position()
        position.vendor_exists = False
        self._check(position)
        self.assertIn("vendor_not_in_sap", codes(position))

    def test_vendor_blocked_blocks(self):
        position = make_position()
        self.validation.apply_sap_flags(
            position, vendor=VendorMatch(vendor_number="0000103777", name="Gesperrt",
                                         blocked=True))
        self._check(position)
        self.assertIn("vendor_blocked", codes(position))
        self.assertTrue(issue_with(position, "vendor_blocked").blocking)

    def test_vendor_ambiguous_is_warning(self):
        position = make_position()
        self.validation.apply_sap_flags(position, candidates=[
            VendorMatch(vendor_number="0000100234", name="Muster GmbH", score=0.71),
            VendorMatch(vendor_number="0000100987", name="Muster AG", score=0.68),
        ])
        self._check(position)
        issue = issue_with(position, "vendor_ambiguous")
        self.assertIsNotNone(issue)
        self.assertIs(issue.severity, IssueSeverity.WARNING)
        self.assertFalse(issue.blocking)

    # -- Preis ----------------------------------------------------------
    def test_price_missing_blocks(self):
        position = self._check(make_position(price=None))
        self.assertIn("price_missing", codes(position))
        self.assertTrue(position.issues.has_blocking)

    def test_price_zero_or_negative_blocks(self):
        position = self._check(make_position(price=Decimal("0")))
        self.assertIn("price_zero_or_negative", codes(position))

    def test_price_above_limit_blocks(self):
        position = self._check(make_position(price=Decimal("128500")))
        self.assertIn("price_above_limit", codes(position))
        self.assertTrue(position.issues.has_blocking)

    def test_price_change_warning(self):
        position = loaded(make_position(price=Decimal("14.50")), make_record("12.40"))
        self._check(position)
        self.assertIn("price_change_warn", codes(position))
        self.assertNotIn("price_change_high", codes(position))
        self.assertIs(position.status, PositionStatus.CHECK)

    def test_price_change_high_blocks_until_acknowledged(self):
        position = loaded(make_position(price=Decimal("20.00")), make_record("12.40"))
        self._check(position)
        self.assertIn("price_change_high", codes(position))
        self.assertIs(position.status, PositionStatus.ERROR)

        self.assertTrue(self.validation.acknowledge(position, "price_change_high"))
        self.assertFalse(position.issues.has_blocking)
        self.assertIs(position.status, PositionStatus.READY)

    def test_acknowledgement_survives_revalidation(self):
        position = loaded(make_position(price=Decimal("20.00")), make_record("12.40"))
        self._check(position)
        self.validation.acknowledge(position, "price_change_high")
        self._check(position)          # zweiter Durchlauf
        self.assertTrue(issue_with(position, "price_change_high").acknowledged)
        self.assertFalse(position.issues.has_blocking)

    def test_price_unit_and_currency_and_uom_differences(self):
        position = loaded(
            make_position(price=Decimal("124.00"), price_unit=10, currency="USD", uom="M"),
            make_record("12.40", price_unit=1, currency="EUR", order_unit="ST"))
        self._check(position)
        self.assertIn("price_unit_changed", codes(position))
        self.assertIn("currency_changed", codes(position))
        self.assertIn("uom_differs_from_sap", codes(position))

    # -- Organisation / Gueltigkeit --------------------------------------
    def test_plant_and_purchasing_org_missing(self):
        position = self._check(make_position(plant="", purchasing_org="",
                                             do_source_list=True))
        self.assertIn("plant_missing", codes(position))
        self.assertIn("purchasing_org_missing", codes(position))
        self.assertTrue(issue_with(position, "plant_missing").blocking)

    def test_valid_from_missing(self):
        position = make_position(valid_from=None)
        offer = make_offer([position], valid_from=None)
        self._check(position, offer)
        self.assertIn("valid_from_missing", codes(position))

    def test_valid_from_in_past(self):
        position = self._check(make_position(valid_from=date.today() - timedelta(days=5)))
        self.assertIn("valid_from_in_past", codes(position))

    # -- Belege ---------------------------------------------------------
    def test_contract_quantity_missing(self):
        position = self._check(make_position(do_contract=True, quantity=None,
                                             contract_quantity=None))
        self.assertIn("contract_quantity_missing", codes(position))
        self.assertTrue(position.issues.has_blocking)

    def test_order_quantity_and_delivery_date(self):
        position = self._check(make_position(do_purchase_order=True, quantity=None))
        self.assertIn("order_quantity_missing", codes(position))
        self.assertIn("delivery_date_missing", codes(position))
        self.assertFalse(issue_with(position, "delivery_date_missing").blocking)

    # -- Hinweise -------------------------------------------------------
    def test_info_record_missing_hint(self):
        position = loaded(make_position(do_info_record=False, do_source_list=True),
                          make_record(exists=False), make_source_list())
        self._check(position)
        issue = issue_with(position, "info_record_missing")
        self.assertIsNotNone(issue)
        self.assertIs(issue.severity, IssueSeverity.INFO)

    def test_source_list_missing_hint(self):
        position = loaded(make_position(do_info_record=True, do_source_list=False),
                          make_record(), make_source_list())
        self._check(position)
        self.assertIn("source_list_missing", codes(position))

    # -- Unsichere Felder ------------------------------------------------
    def test_uncertain_mandatory_field_blocks(self):
        position = make_position()
        position.field_origins["price"] = FieldOrigin.UNCERTAIN
        self._check(position)
        issue = issue_with(position, "uncertain_fields")
        self.assertIsNotNone(issue)
        self.assertTrue(issue.blocking)
        self.assertIs(position.status, PositionStatus.ERROR)

    def test_uncertain_optional_field_only_warns(self):
        position = make_position()
        position.field_origins["lead_time_days"] = FieldOrigin.UNCERTAIN
        self._check(position)
        issue = issue_with(position, "uncertain_fields")
        self.assertIsNotNone(issue)
        self.assertFalse(issue.blocking)
        self.assertIs(position.status, PositionStatus.CHECK)

    # -- Dubletten / Kopf ------------------------------------------------
    def test_duplicate_position_warns(self):
        first = make_position(position_number="10")
        second = make_position(position_number="20")
        offer = make_offer([first, second])
        self.validation.validate_offer(offer)
        self.assertIn("duplicate_position", codes(first))
        self.assertIn("duplicate_position", codes(second))

    def test_offer_header_incomplete(self):
        offer = make_offer([make_position()], offer_number="", currency="")
        self.validation.validate_offer(offer)
        offer_codes = {i.code for i in offer.issues}
        self.assertIn("offer_header_incomplete", offer_codes)

    def test_offer_too_old(self):
        offer = make_offer([make_position()],
                           offer_date=date.today() - timedelta(days=200))
        self.validation.validate_offer(offer)
        offer_codes = {i.code for i in offer.issues}
        self.assertIn("offer_too_old", offer_codes)

    # -- Status / Zusammenfassung ---------------------------------------
    def test_status_not_selected(self):
        position = self._check(make_position(selected=False))
        self.assertIs(position.status, PositionStatus.NOT_SELECTED)

    def test_status_ready_with_nothing_to_do(self):
        position = loaded(make_position(price=Decimal("12.40")), make_record("12.40"))
        self._check(position)
        self.assertIs(position.status, PositionStatus.READY)
        self.assertIn("nothing_to_do", codes(position))

    def test_status_ready_for_clean_change(self):
        position = loaded(make_position(price=Decimal("12.85")), make_record("12.40"))
        self._check(position)
        self.assertIs(position.status, PositionStatus.READY)
        self.assertIs(position.info_record_action, InfoRecordAction.UPDATE)

    def test_summary_counts(self):
        good = loaded(make_position(position_number="10", price=Decimal("12.85")),
                      make_record("12.40"))
        bad = make_position(position_number="20", material_number="", price=None)
        offer = make_offer([good, bad])
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        summary = self.validation.summary(offer)
        self.assertEqual(2, summary["positions"])
        self.assertEqual(2, summary["selected"])
        self.assertGreaterEqual(summary["error"], 2)
        self.assertGreaterEqual(summary["blocking"], 2)
        self.assertEqual(1, summary["error_positions"])


# ===========================================================================
# Vorschau
# ===========================================================================

class TestPreview(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings()
        self.comparison = ComparisonService(self.settings)
        self.validation = ValidationService(self.settings)
        self.preview = PreviewService()

    # -- Abrufmenge ------------------------------------------------------
    def test_call_off_percent(self):
        self.settings.workflow.call_off_mode = "percent"
        self.settings.workflow.call_off_percent = Decimal("20")
        self.assertEqual(Decimal("20"), call_off_quantity(Decimal("100"), self.settings))

    def test_call_off_percent_rounds_commercially(self):
        self.settings.workflow.call_off_mode = "percent"
        self.settings.workflow.call_off_percent = Decimal("20")
        # 20 % von 7 = 1,4 -> 1
        self.assertEqual(Decimal("1"), call_off_quantity(Decimal("7"), self.settings))
        # 20 % von 8 = 1,6 -> 2
        self.assertEqual(Decimal("2"), call_off_quantity(Decimal("8"), self.settings))

    def test_call_off_percent_minimum_one(self):
        self.settings.workflow.call_off_mode = "percent"
        self.settings.workflow.call_off_percent = Decimal("20")
        # 20 % von 2 = 0,4 -> aufgerundet auf die kleinste sinnvolle Menge
        self.assertEqual(Decimal("1"), call_off_quantity(Decimal("2"), self.settings))

    def test_call_off_absolute(self):
        self.settings.workflow.call_off_mode = "absolute"
        self.settings.workflow.call_off_quantity = Decimal("25")
        self.assertEqual(Decimal("25"), call_off_quantity(Decimal("100"), self.settings))

    def test_call_off_full(self):
        self.settings.workflow.call_off_mode = "full"
        self.assertEqual(Decimal("100"), call_off_quantity(Decimal("100"), self.settings))

    def test_call_off_without_rounding_keeps_decimals(self):
        self.settings.workflow.call_off_mode = "percent"
        self.settings.workflow.call_off_percent = Decimal("20")
        self.settings.workflow.call_off_round_to_integer = False
        self.assertEqual(Decimal("1.4"), call_off_quantity(Decimal("7"), self.settings))

    # -- Aufbau ----------------------------------------------------------
    def _two_position_offer(self) -> Offer:
        first = loaded(make_position(position_number="10", price=Decimal("12.85"),
                                     do_source_list=True, do_contract=True,
                                     do_purchase_order=True,
                                     contract_quantity=Decimal("100"),
                                     delivery_date=date.today() + timedelta(days=14)),
                       make_record("12.40"), make_source_list([make_entry(agreement="X")]))
        second = loaded(make_position(position_number="20", material_number="47110004",
                                      price=Decimal("5.50"), quantity=Decimal("50"),
                                      do_source_list=True, do_contract=True,
                                      do_purchase_order=True,
                                      contract_quantity=Decimal("50"),
                                      delivery_date=date.today() + timedelta(days=14)),
                        make_record(exists=False), make_source_list())
        offer = make_offer([first, second])
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        return offer

    def test_preview_counts_and_plans(self):
        offer = self._two_position_offer()
        summary = self.preview.build(offer, self.settings)
        self.assertEqual(2, summary.positions_selected)
        self.assertEqual(1, summary.info_records_update)
        self.assertEqual(1, summary.info_records_create)
        self.assertEqual(1, summary.source_lists_create)
        self.assertEqual(1, summary.source_lists_unchanged)
        self.assertEqual(1, len(summary.contract_plans))
        self.assertEqual(2, summary.contract_items)
        self.assertEqual(1, len(summary.purchase_order_plans))
        self.assertEqual(2, summary.purchase_order_items)

    def test_preview_writes_order_quantity(self):
        offer = self._two_position_offer()
        self.preview.build(offer, self.settings)
        self.assertEqual(Decimal("20"), offer.positions[0].order_quantity)
        self.assertEqual(Decimal("10"), offer.positions[1].order_quantity)

    def test_preview_keeps_existing_order_quantity(self):
        offer = self._two_position_offer()
        offer.positions[0].order_quantity = Decimal("5")
        self.preview.build(offer, self.settings)
        self.assertEqual(Decimal("5"), offer.positions[0].order_quantity)

    def test_preview_lines(self):
        offer = self._two_position_offer()
        summary = self.preview.build(offer, self.settings)
        self.assertEqual("2 Positionen ausgewählt", summary.lines[0])
        self.assertIn("Infosätze: 1 ändern, 1 neu anlegen", summary.lines)
        self.assertIn("Orderbuch: 1 neu anlegen, 1 unverändert", summary.lines)
        self.assertIn("Mengenkontrakte: 1 Beleg mit 2 Positionen", summary.lines)
        self.assertIn("Bestellungen: 1 Beleg mit 2 Positionen (Abruf 20 %)", summary.lines)

    def test_preview_chain_order_is_fixed(self):
        offer = self._two_position_offer()
        summary = self.preview.build(offer, self.settings)
        self.assertEqual(4, len(summary.chain_order))
        self.assertIn("Infosätze", summary.chain_order[0])
        self.assertIn("Mengenkontrakte", summary.chain_order[1])
        self.assertIn("Orderbuch", summary.chain_order[2])
        self.assertIn("Bestellungen", summary.chain_order[3])

    def test_preview_reports_blocking(self):
        position = make_position(material_number="")
        offer = make_offer([position])
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        summary = self.preview.build(offer, self.settings)
        self.assertTrue(summary.blocking)
        self.assertGreaterEqual(summary.errors, 1)


# ===========================================================================
# Komplettvorgang gegen das Mock-SAP
# ===========================================================================

class TestBatchProcessor(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = make_settings(dry_run=False)
        self.gateway = SapGateway(self.settings)
        self.gateway.reset_mock_data()
        self.comparison = ComparisonService(self.settings)
        self.validation = ValidationService(self.settings)
        self.preview_service = PreviewService()
        self.batch = BatchProcessor(self.gateway, self.settings,
                                    self.comparison, self.validation)

    # ------------------------------------------------------------------
    def _offer(self, second_material: str = "47110004") -> Offer:
        first = make_position(
            position_number="10", material_number="47110001", price=Decimal("12.85"),
            quantity=Decimal("100"), contract_quantity=Decimal("100"),
            delivery_date=date.today() + timedelta(days=14),
            do_info_record=True, do_source_list=True, do_contract=True,
            do_purchase_order=True)
        second = make_position(
            position_number="20", material_number=second_material, price=Decimal("5.50"),
            quantity=Decimal("50"), contract_quantity=Decimal("50"),
            delivery_date=date.today() + timedelta(days=14),
            do_info_record=True, do_source_list=True, do_contract=True,
            do_purchase_order=True)
        return make_offer([first, second])

    def _prepare(self, offer: Offer):
        for position in offer.positions:
            self.gateway.load_position_state(position)
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        return self.preview_service.build(offer, self.settings)

    # ------------------------------------------------------------------
    def test_complete_run_succeeds(self):
        offer = self._offer()
        preview = self._prepare(offer)
        events: list[ProgressEvent] = []
        summary = self.batch.run(offer, preview, progress=events.append)

        self.assertFalse(summary.aborted, summary.abort_reason)
        self.assertEqual(0, summary.failed)
        for position in offer.positions:
            self.assertIs(position.status, PositionStatus.DONE, position.result_text)
            self.assertTrue(position.created_info_record)
            self.assertTrue(position.created_contract)
            self.assertTrue(position.created_purchase_order)
        self.assertEqual(1, len(self.gateway.mock_system.contracts))
        self.assertEqual(1, len(self.gateway.mock_system.purchase_orders))
        self.assertTrue(events)
        self.assertEqual("done", events[-1].phase)

    def test_run_order_is_info_contract_source_order(self):
        offer = self._offer()
        preview = self._prepare(offer)
        phases: list[str] = []
        self.batch.run(offer, preview,
                       progress=lambda e: phases.append(e.phase))
        # Die Anlage laeuft als Nachlauf zu jeder Aktion und sagt ueber die
        # Reihenfolge der Arbeitsschritte nichts aus.
        without_done = [p for p in phases if p not in ("done", "attachment")]
        first_contract = without_done.index("contract")
        first_source = without_done.index("source_list")
        first_order = without_done.index("purchase_order")
        self.assertTrue(all(p == "info_record" for p in without_done[:first_contract]))
        self.assertLess(first_contract, first_source)
        self.assertLess(first_source, first_order)

    def test_purchase_order_references_contract(self):
        offer = self._offer()
        preview = self._prepare(offer)
        self.batch.run(offer, preview)
        contract_number = next(iter(self.gateway.mock_system.contracts))
        order = next(iter(self.gateway.mock_system.purchase_orders.values()))
        self.assertEqual(contract_number, order["reference_contract"])

    def test_source_list_gets_contract_as_agreement(self):
        offer = self._offer()
        preview = self._prepare(offer)
        self.batch.run(offer, preview)
        contract_number = next(iter(self.gateway.mock_system.contracts))
        rows = self.gateway.mock_system.source_lists[
            MockSapSystem.sl_key("47110001", "1000")]
        row = next(r for r in rows if r["vendor_number"] == "0000100234")
        self.assertEqual(contract_number, row["agreement"])

    def test_info_record_price_is_written(self):
        offer = self._offer()
        preview = self._prepare(offer)
        self.batch.run(offer, preview)
        record = self.gateway.mock_system.info_records[
            MockSapSystem.ir_key("47110001", "0000100234", "1000", "1000")]
        self.assertEqual("12.85", record["price"])

    # ------------------------------------------------------------------
    def test_error_in_one_position_is_isolated(self):
        offer = self._offer()
        preview = self._prepare(offer)
        original = self.gateway.info_records.write

        def failing(position, context):
            if position.material_number == "47110004":
                raise SapError("Feld 'Nettopreis' wurde nicht gefunden.", "Testfehler")
            return original(position, context)

        self.gateway.info_records.write = failing
        summary = self.batch.run(offer, preview)

        self.assertFalse(summary.aborted)
        self.assertIs(offer.positions[0].status, PositionStatus.DONE)
        self.assertIs(offer.positions[1].status, PositionStatus.ERROR)
        # Der Beleg wird trotzdem angelegt -- die Isolation gilt je Position.
        self.assertEqual(1, len(self.gateway.mock_system.contracts))

    def test_blocking_position_is_skipped(self):
        offer = self._offer(second_material="47119999")   # existiert in SAP nicht
        preview = self._prepare(offer)
        summary = self.batch.run(offer, preview)

        self.assertFalse(summary.aborted)
        self.assertIs(offer.positions[1].status, PositionStatus.SKIPPED)
        self.assertNotIn(
            MockSapSystem.ir_key("47119999", "0000100234", "1000", "1000"),
            self.gateway.mock_system.info_records)
        self.assertIn("Übersprungen", offer.positions[1].result_text)

    def test_failed_contract_skips_dependent_purchase_order(self):
        offer = self._offer()
        preview = self._prepare(offer)

        def failing(plan, context):
            raise SapError("Belegart MK ist nicht zulaessig.", "Testfehler")

        self.gateway.contracts.create = failing
        summary = self.batch.run(offer, preview)

        self.assertFalse(summary.aborted)
        self.assertEqual({}, self.gateway.mock_system.purchase_orders)
        for position in offer.positions:
            self.assertEqual("", position.created_purchase_order)
            self.assertIn("Mengenkontrakt", position.result_text)

    def test_purchase_order_without_contract_reference_when_disabled(self):
        self.settings.workflow.purchase_order_from_contract = False
        offer = self._offer()
        preview = self._prepare(offer)

        def failing(plan, context):
            raise SapError("Belegart MK ist nicht zulaessig.", "Testfehler")

        self.gateway.contracts.create = failing
        self.batch.run(offer, preview)
        self.assertEqual(1, len(self.gateway.mock_system.purchase_orders))
        order = next(iter(self.gateway.mock_system.purchase_orders.values()))
        self.assertEqual("", order["reference_contract"])

    # ------------------------------------------------------------------
    def test_dry_run_writes_nothing(self):
        self.settings.dry_run = True
        offer = self._offer()
        preview = self._prepare(offer)
        summary = self.batch.run(offer, preview)

        self.assertTrue(summary.dry_run)
        self.assertEqual({}, self.gateway.mock_system.contracts)
        self.assertEqual({}, self.gateway.mock_system.purchase_orders)
        record = self.gateway.mock_system.info_records[
            MockSapSystem.ir_key("47110001", "0000100234", "1000", "1000")]
        self.assertEqual("12.40", record["price"])
        self.assertTrue(preview.dry_run)

    def test_user_cancellation_skips_the_rest(self):
        offer = self._offer()
        preview = self._prepare(offer)
        calls: list[int] = []

        def is_cancelled() -> bool:
            calls.append(1)
            return len(calls) > 2

        summary = self.batch.run(offer, preview, is_cancelled=is_cancelled)
        self.assertTrue(summary.aborted)
        self.assertIn("abgebrochen", summary.abort_reason)
        self.assertEqual({}, self.gateway.mock_system.contracts)
        self.assertEqual({}, self.gateway.mock_system.purchase_orders)

    def test_message_suppression_error_stops_the_run(self):
        offer = self._offer()
        preview = self._prepare(offer)

        def guard(plan, context):
            raise MessageSuppressionError(
                "Sicherheitsabbruch: Der Beleg wurde NICHT gesichert.",
                detail="Nachrichtentabelle nicht leer.")

        self.gateway.contracts.create = guard
        summary = self.batch.run(offer, preview)

        self.assertTrue(summary.aborted)
        self.assertIn("Sicherheitsabbruch", summary.abort_reason)
        self.assertEqual({}, self.gateway.mock_system.purchase_orders)
        # Das Orderbuch kommt nach dem Kontrakt -- es darf nicht mehr laufen.
        self.assertNotIn("47110004|1000", self.gateway.mock_system.source_lists)

    def test_progress_events_are_complete(self):
        offer = self._offer()
        preview = self._prepare(offer)
        events: list[ProgressEvent] = []
        self.batch.run(offer, preview, progress=events.append)
        # Die Anlage ist ein Nachlauf zur eigentlichen Aktion und wird
        # gesondert gemeldet -- hier geht es um die Arbeitsschritte selbst.
        work = [e for e in events if e.phase not in ("done", "attachment")]
        # 6 Einheiten (2 Infosaetze, 1 Kontrakt, 2 Orderbuch, 1 Bestellung);
        # Belegergebnisse werden auf beide beteiligten Positionen verteilt.
        self.assertTrue(all(e.total == 6 for e in work))
        self.assertTrue(all(e.result is not None for e in work))
        self.assertTrue(all(e.result.state is not ResultState.SKIPPED for e in work))
        for phase in ("info_record", "contract", "source_list", "purchase_order"):
            self.assertEqual(2, len([e for e in work if e.phase == phase]), phase)
        self.assertEqual(100, events[-1].percent)


# ===========================================================================
# Undo / Redo
# ===========================================================================

class TestUndoService(unittest.TestCase):

    def setUp(self) -> None:
        self.service = UndoService(depth=5)
        self.offer = make_offer([make_position()])

    def test_nothing_to_undo_at_start(self):
        self.assertFalse(self.service.can_undo)
        self.assertFalse(self.service.can_redo)
        self.assertIsNone(self.service.undo())
        self.assertTrue(self.service.blocked_reason)

    def test_undo_returns_previous_state(self):
        self.service.snapshot(self.offer, "Import")
        self.offer.positions[0].price = Decimal("99.00")
        self.service.snapshot(self.offer, "Preis geändert")

        self.assertTrue(self.service.can_undo)
        previous = self.service.undo()
        self.assertIsNotNone(previous)
        self.assertEqual(Decimal("12.85"), previous.positions[0].price)

    def test_redo_returns_forward_state(self):
        self.service.snapshot(self.offer, "Import")
        self.offer.positions[0].price = Decimal("99.00")
        self.service.snapshot(self.offer, "Preis geändert")
        self.service.undo()

        self.assertTrue(self.service.can_redo)
        forward = self.service.redo()
        self.assertEqual(Decimal("99.00"), forward.positions[0].price)
        self.assertFalse(self.service.can_redo)

    def test_snapshot_after_undo_drops_redo_branch(self):
        self.service.snapshot(self.offer, "Import")
        self.offer.positions[0].price = Decimal("99.00")
        self.service.snapshot(self.offer, "Preis 99")
        self.service.undo()
        self.offer.positions[0].price = Decimal("55.00")
        self.service.snapshot(self.offer, "Preis 55")
        self.assertFalse(self.service.can_redo)

    def test_snapshot_is_deep_copy(self):
        self.service.snapshot(self.offer, "Import")
        self.offer.positions[0].description = "geändert"
        self.service.snapshot(self.offer, "Text geändert")
        previous = self.service.undo()
        self.assertEqual("Dichtring NBR 40x52x7", previous.positions[0].description)

    def test_depth_is_limited(self):
        for index in range(12):
            self.offer.positions[0].price = Decimal(index)
            self.service.snapshot(self.offer, f"Schritt {index}")
        self.assertEqual(5, len(self.service))

    def test_clear_after_sap_write_blocks_undo(self):
        self.service.snapshot(self.offer, "Import")
        self.offer.positions[0].price = Decimal("99.00")
        self.service.snapshot(self.offer, "Preis geändert")
        self.assertTrue(self.service.can_undo)

        self.service.lock_after_write()
        self.assertFalse(self.service.can_undo)
        self.assertFalse(self.service.can_redo)
        self.assertIn("SAP", self.service.blocked_reason)
        self.assertIsNone(self.service.undo())

    def test_clear_with_own_reason(self):
        self.service.snapshot(self.offer, "Import")
        self.service.clear("Neues Angebot geladen.")
        self.assertFalse(self.service.can_undo)
        self.assertEqual("Neues Angebot geladen.", self.service.blocked_reason)

    def test_snapshot_reactivates_history_after_lock(self):
        self.service.snapshot(self.offer, "Import")
        self.service.lock_after_write()
        self.service.snapshot(self.offer, "Neue Bearbeitung")
        self.offer.positions[0].price = Decimal("1.00")
        self.service.snapshot(self.offer, "Preis 1")
        self.assertTrue(self.service.can_undo)
        self.assertEqual("", self.service.blocked_reason)


if __name__ == "__main__":
    unittest.main()
