"""Tests der SAP-Schreibschicht gegen das eingebaute Testsystem (Mock-SAP).

Aufruf:  ``python -m unittest tests.test_sap_write -v``

Geprueft werden die vier Punkte, an denen ein Fehler teuer wird:

    1. Ruecklese-Pruefung   -- ist wirklich angekommen, was gesendet wurde?
    2. Kontraktwiederverwendung -- Bestandskontrakt erweitern statt Nummern
       zu vermehren, und die Bestellung muss auf die RICHTIGE Kontraktzeile
       zeigen.
    3. Kontierung der Bestellung -- lieber ablehnen als falsch kontieren.
    4. Mengenstaffeln im Infosatz -- nichts stillschweigend weglassen.

Wie in den uebrigen Testdateien wird das Basisverzeichnis der Anwendung
*vor* dem Import der Anwendungsmodule auf ein temporaeres Verzeichnis
umgebogen, damit keine echten Anwenderdaten angefasst werden.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="sap_write_tests_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name

from app.config.settings import Settings                                # noqa: E402
from app.models.document_plan import (                                  # noqa: E402
    ContractPlan,
    DocumentItem,
    PurchaseOrderPlan,
    apply_account_assignment,
    validate_account_assignment,
)
from app.models.enums import ResultState                                # noqa: E402
from app.models.offer import Offer                                      # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.sap.gateway import SapGateway                                  # noqa: E402
from app.sap.info_record_service import verify_info_record_write        # noqa: E402
from app.sap.mock_backend import MockSapSystem                          # noqa: E402
from app.services.batch_service import BatchProcessor                   # noqa: E402
from app.services.comparison_service import ComparisonService           # noqa: E402
from app.services.preview_service import PreviewService                 # noqa: E402
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

def make_settings(dry_run: bool = False) -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = dry_run
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


def make_offer(positions: list[OfferPosition]) -> Offer:
    offer = Offer(vendor_number="0000100234", vendor_name="Muster Dichtungstechnik GmbH")
    offer.positions = positions
    return offer


def make_item(**overrides) -> DocumentItem:
    werte = dict(
        position_uid=1,
        material_number="47110001",
        description="Dichtring",
        quantity=Decimal("10"),
        uom="ST",
        net_price=Decimal("12.85"),
        price_unit=1,
        plant="1000",
    )
    werte.update(overrides)
    return DocumentItem(**werte)


def make_order_plan(items: list[DocumentItem], **overrides) -> PurchaseOrderPlan:
    werte = dict(
        vendor_number="0000100234", purchasing_org="1000", purchasing_group="100",
        plant="1000", currency="EUR", document_type="NB",
        delivery_date=date.today() + timedelta(days=14),
    )
    werte.update(overrides)
    plan = PurchaseOrderPlan(**werte)
    plan.items = items
    return plan


def make_contract_plan(items: list[DocumentItem], **overrides) -> ContractPlan:
    werte = dict(
        vendor_number="0000100234", purchasing_org="1000", purchasing_group="100",
        plant="1000", currency="EUR", document_type="MK",
        valid_from=date.today(), valid_to=date(2099, 12, 31),
    )
    werte.update(overrides)
    plan = ContractPlan(**werte)
    plan.items = items
    return plan


class _Umgebung:
    """Gateway samt Mock-System, frisch zurueckgesetzt."""

    def __init__(self, dry_run: bool = False) -> None:
        self.settings = make_settings(dry_run)
        self.gateway = SapGateway(self.settings)
        self.gateway.reset_mock_data()

    @property
    def system(self) -> MockSapSystem:
        return self.gateway.mock_system

    def context(self, valid_from=None, valid_to=None):
        return self.gateway.write_context(valid_from=valid_from, valid_to=valid_to)

    def batch(self) -> BatchProcessor:
        return BatchProcessor(self.gateway, self.settings,
                              ComparisonService(self.settings),
                              ValidationService(self.settings))

    def run(self, offer: Offer):
        vergleich = ComparisonService(self.settings)
        pruefung = ValidationService(self.settings)
        for position in offer.positions:
            self.gateway.load_position_state(position)
        vergleich.compare_offer(offer)
        pruefung.validate_offer(offer)
        vorschau = PreviewService().build(offer, self.settings)
        return self.batch().run(offer, vorschau)


def alle_meldungen(result) -> str:
    return " | ".join([result.message] + list(result.sap_messages))


# ===========================================================================
# 1. Ruecklese-Pruefung
# ===========================================================================

class RuecklesePruefungInfosatzTest(unittest.TestCase):
    """Die Statusleiste ist kein Beweis -- erst der zweite Blick zaehlt."""

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)
        self.settings = self.umgebung.settings

    def _schreiben(self, position=None, **kontext):
        position = position or make_position()
        return self.umgebung.gateway.info_records.write(
            position, self.umgebung.context(**kontext))

    def test_erfolg_wird_bestaetigt(self) -> None:
        ergebnis = self._schreiben()
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))
        self.assertIn("bestaetigt", alle_meldungen(ergebnis))

    def test_bestaetigung_nennt_preis_und_waehrung(self) -> None:
        ergebnis = self._schreiben(make_position(price=Decimal("12.85")))
        text = alle_meldungen(ergebnis)
        self.assertIn("12,85", text)
        self.assertIn("EUR", text)

    def test_abweichung_ist_fehler(self) -> None:
        """SAP speichert stillschweigend etwas anderes -> Position faellt durch."""
        self.settings.sap.verify_failure_is_error = True
        position = make_position()
        schluessel = MockSapSystem.ir_key(position.material_number, position.vendor_number,
                                          position.purchasing_org, position.plant)
        self.umgebung.system.forced_price_deviations[schluessel] = "12.40"

        ergebnis = self._schreiben(position)
        self.assertIs(ergebnis.state, ResultState.FAILED)
        self.assertIn("12,40", ergebnis.message)
        self.assertIn("12,85", ergebnis.message)
        self.assertIn("ME13", ergebnis.message)

    def test_abweichung_nur_warnung(self) -> None:
        self.settings.sap.verify_failure_is_error = False
        position = make_position()
        schluessel = MockSapSystem.ir_key(position.material_number, position.vendor_number,
                                          position.purchasing_org, position.plant)
        self.umgebung.system.forced_price_deviations[schluessel] = "12.40"

        ergebnis = self._schreiben(position)
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("Ruecklese-Pruefung fehlgeschlagen", alle_meldungen(ergebnis))

    def test_toleranz_greift_bei_rundung(self) -> None:
        """SAP legt zweistellig ab -- das darf keinen Fehler ausloesen."""
        self.settings.sap.verify_price_tolerance = Decimal("0.005")
        ergebnis = self._schreiben(make_position(price=Decimal("12.8549")))
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("bestaetigt", alle_meldungen(ergebnis))

    def test_toleranz_null_meldet_rundung(self) -> None:
        """Ohne Toleranz faellt schon die Rundung auf."""
        self.settings.sap.verify_price_tolerance = Decimal("0")
        self.settings.sap.verify_failure_is_error = True
        ergebnis = self._schreiben(make_position(price=Decimal("12.8549")))
        self.assertIs(ergebnis.state, ResultState.FAILED)

    def test_abgeschaltet_keine_pruefung(self) -> None:
        self.settings.sap.verify_after_write = False
        ergebnis = self._schreiben()
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertNotIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))

    def test_dry_run_prueft_nicht(self) -> None:
        """Im Dry Run wurde nichts geschrieben -- also gibt es nichts zu pruefen."""
        umgebung = _Umgebung(dry_run=True)
        ergebnis = umgebung.gateway.info_records.write(make_position(),
                                                       umgebung.context())
        self.assertIs(ergebnis.state, ResultState.SIMULATED)
        self.assertNotIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))
        self.assertEqual({}, {k: v for k, v in umgebung.system.info_records.items()
                              if k.startswith("47110004")})

    def test_waehrungsabweichung_faellt_auf(self) -> None:
        """Waehrung, Preiseinheit und Gueltigkeit werden mitgeprueft."""
        position = make_position(currency="EUR", price_unit=1)
        kontext = self.umgebung.context(valid_to=date(2099, 12, 31))
        self._schreiben(position, valid_to=date(2099, 12, 31))

        gelesen = self.umgebung.gateway.info_records.read(
            position.material_number, position.vendor_number,
            position.purchasing_org, position.plant)

        for abweichend, erwartet in ((make_position(currency="USD"), "Waehrung"),
                                     (make_position(price_unit=10), "Preiseinheit")):
            with self.subTest(feld=erwartet):
                in_ordnung, hinweise = verify_info_record_write(
                    gelesen, abweichend, kontext, self.settings)
                self.assertFalse(in_ordnung)
                self.assertIn(erwartet, hinweise[0])

    def test_gueltigkeitsabweichung_faellt_auf(self) -> None:
        position = make_position()
        self._schreiben(position, valid_to=date(2099, 12, 31))
        gelesen = self.umgebung.gateway.info_records.read(
            position.material_number, position.vendor_number,
            position.purchasing_org, position.plant)
        kontext = self.umgebung.context(valid_to=date(2030, 12, 31))
        in_ordnung, hinweise = verify_info_record_write(gelesen, position, kontext,
                                                        self.settings)
        self.assertFalse(in_ordnung)
        self.assertIn("Gueltig bis", hinweise[0])

    def test_fehlender_satz_ist_kein_erfolg(self) -> None:
        """SAP meldet 'gesichert', der Satz ist aber nicht auffindbar."""
        position = make_position()
        leer = self.umgebung.gateway.info_records.read(
            "47119999", position.vendor_number, position.purchasing_org, position.plant)
        in_ordnung, hinweise = verify_info_record_write(
            leer, position, self.umgebung.context(), self.settings)
        self.assertFalse(in_ordnung)
        self.assertIn("nicht auffindbar", hinweise[0])


class RuecklesePruefungOrderbuchTest(unittest.TestCase):
    """Ist der Lieferant nach dem Sichern wirklich aktiv?"""

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)
        self.settings = self.umgebung.settings

    def test_orderbuch_bestaetigt_aktiven_lieferanten(self) -> None:
        position = make_position(material_number="47110002", do_source_list=True)
        ergebnis = self.umgebung.gateway.source_lists.write(position,
                                                            self.umgebung.context())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertIn("aktiv", alle_meldungen(ergebnis))
        self.assertIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))

    def test_orderbuch_dry_run_prueft_nicht(self) -> None:
        umgebung = _Umgebung(dry_run=True)
        ergebnis = umgebung.gateway.source_lists.write(
            make_position(do_source_list=True), umgebung.context())
        self.assertIs(ergebnis.state, ResultState.SIMULATED)
        self.assertNotIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))


class BelegnummerPflichtTest(unittest.TestCase):
    """Ohne Belegnummer ist voellig offen, was in SAP passiert ist."""

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)

    def test_kontrakt_liefert_belegnummer(self) -> None:
        plan = make_contract_plan([make_item()])
        ergebnis = self.umgebung.gateway.contracts.create(plan, self.umgebung.context())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertTrue(ergebnis.document_number)
        self.assertIn(ergebnis.document_number, self.umgebung.system.contracts)

    def test_bestellung_liefert_belegnummer(self) -> None:
        plan = make_order_plan([make_item()])
        ergebnis = self.umgebung.gateway.purchase_orders.create(plan,
                                                                self.umgebung.context())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertTrue(ergebnis.document_number)
        self.assertIn("Ruecklese-Pruefung", alle_meldungen(ergebnis))

    def test_verschwundener_kontrakt_ist_fehler(self) -> None:
        """Beleg nach dem Sichern nicht auffindbar -> kein Erfolg."""
        gateway = self.umgebung.gateway
        system = self.umgebung.system
        plan = make_contract_plan([make_item()])

        original_save = system.save

        def save_und_loeschen():
            # Der Beleg verschwindet zwischen Sichern und Pruefen
            system.contracts.clear()
            original_save()

        system.save = save_und_loeschen
        try:
            ergebnis = gateway.contracts.create(plan, self.umgebung.context())
        finally:
            system.save = original_save
        self.assertIs(ergebnis.state, ResultState.FAILED)
        self.assertIn("Ruecklese-Pruefung", ergebnis.message)


# ===========================================================================
# 2. Kontraktwiederverwendung
# ===========================================================================

class KontraktWiederverwendungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)
        self.settings = self.umgebung.settings
        self.settings.workflow.contract_reuse_existing = True

    def _bestandskontrakt(self, valid_to: date, document_type: str = "MK",
                          vendor: str = "0000100234",
                          purchasing_org: str = "1000") -> str:
        nummer = self.umgebung.system.next_number("contract", "4600")
        self.umgebung.system.contracts[nummer] = {
            "document_type": document_type, "vendor": vendor,
            "purchasing_org": purchasing_org, "purchasing_group": "100",
            "currency": "EUR", "valid_from": date.today().isoformat(),
            "valid_to": valid_to.isoformat(), "target_value": "1000",
            "reference_offer": "", "messages_suppressed": True,
            "items": [{"item": "00010", "material": "47110003", "quantity": "5",
                       "uom": "ST", "net_price": "18.95", "price_unit": 1,
                       "plant": "1000"}],
        }
        return nummer

    # -- Suche ----------------------------------------------------------
    def test_findet_laufenden_kontrakt(self) -> None:
        nummer = self._bestandskontrakt(date.today() + timedelta(days=200))
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual(nummer, gefunden)

    def test_findet_nichts_ohne_bestand(self) -> None:
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual("", gefunden)

    def test_zu_kurze_restlaufzeit_wird_ignoriert(self) -> None:
        self._bestandskontrakt(date.today() + timedelta(days=5))
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual("", gefunden)

    def test_anderer_lieferant_wird_ignoriert(self) -> None:
        self._bestandskontrakt(date.today() + timedelta(days=200),
                               vendor="0000100987")
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual("", gefunden)

    def test_andere_belegart_wird_ignoriert(self) -> None:
        self._bestandskontrakt(date.today() + timedelta(days=200), document_type="WK")
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual("", gefunden)

    def test_andere_einkaufsorganisation_wird_ignoriert(self) -> None:
        self._bestandskontrakt(date.today() + timedelta(days=200),
                               purchasing_org="2000")
        gefunden = self.umgebung.gateway.contracts.find_existing_contract(
            "0000100234", "1000", "MK", date.today() + timedelta(days=30))
        self.assertEqual("", gefunden)

    # -- Erweitern -------------------------------------------------------
    def test_erweitert_statt_neu_anzulegen(self) -> None:
        nummer = self._bestandskontrakt(date.today() + timedelta(days=200))
        vorher = len(self.umgebung.system.contracts)

        plan = make_contract_plan([make_item(position_uid=7)])
        plan.existing_contract_number = nummer
        ergebnis = self.umgebung.gateway.contracts.create(plan, self.umgebung.context())

        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(nummer, ergebnis.document_number)
        self.assertEqual(vorher, len(self.umgebung.system.contracts))
        self.assertEqual(2, len(self.umgebung.system.contracts[nummer]["items"]))

    def test_laufzeit_des_bestandskontrakts_bleibt(self) -> None:
        ende = date.today() + timedelta(days=200)
        nummer = self._bestandskontrakt(ende)
        plan = make_contract_plan([make_item()], valid_to=date(2099, 12, 31))
        plan.existing_contract_number = nummer
        self.umgebung.gateway.contracts.create(plan, self.umgebung.context())
        self.assertEqual(ende.isoformat(),
                         self.umgebung.system.contracts[nummer]["valid_to"])

    def test_positionsnummern_laufen_fort(self) -> None:
        nummer = self._bestandskontrakt(date.today() + timedelta(days=200))
        plan = make_contract_plan([make_item(), make_item(material_number="47110002")])
        plan.existing_contract_number = nummer
        self.umgebung.gateway.contracts.create(plan, self.umgebung.context())
        zeilen = [p["item"] for p in self.umgebung.system.contracts[nummer]["items"]]
        self.assertEqual(["00010", "00020", "00030"], zeilen)

    def test_abgeschaltet_legt_neuen_kontrakt_an(self) -> None:
        self.settings.workflow.contract_reuse_existing = False
        self._bestandskontrakt(date.today() + timedelta(days=200))
        offer = make_offer([make_position(do_contract=True,
                                          contract_quantity=Decimal("100"))])
        self.umgebung.run(offer)
        self.assertEqual(2, len(self.umgebung.system.contracts))

    def test_komplettvorgang_erweitert_bestandskontrakt(self) -> None:
        nummer = self._bestandskontrakt(date.today() + timedelta(days=200))
        offer = make_offer([make_position(do_contract=True,
                                          contract_quantity=Decimal("100"))])
        ergebnis = self.umgebung.run(offer)

        self.assertEqual(0, ergebnis.failed,
                         [r.error_messages for r in ergebnis.results])
        self.assertEqual(1, len(self.umgebung.system.contracts))
        self.assertEqual(2, len(self.umgebung.system.contracts[nummer]["items"]))

    def test_bestellung_zeigt_auf_richtige_kontraktposition(self) -> None:
        """Die Bestellung muss die *neue* Kontraktzeile abrufen, nicht 00010."""
        nummer = self._bestandskontrakt(date.today() + timedelta(days=200))
        position = make_position(do_contract=True, do_purchase_order=True,
                                 contract_quantity=Decimal("100"),
                                 order_quantity=Decimal("20"),
                                 delivery_date=date.today() + timedelta(days=14))
        offer = make_offer([position])
        ergebnis = self.umgebung.run(offer)

        self.assertEqual(0, ergebnis.failed,
                         [r.error_messages for r in ergebnis.results])
        bestellung = next(iter(self.umgebung.system.purchase_orders.values()))
        self.assertEqual(nummer, bestellung["reference_contract"])
        zeile = bestellung["items"][0]
        self.assertEqual("00020", zeile["contract_item"],
                         "Die Bestellung ruft die falsche Kontraktposition ab")
        self.assertEqual(nummer, zeile["contract_number"])

    def test_protokoll_nennt_die_entscheidung(self) -> None:
        self._bestandskontrakt(date.today() + timedelta(days=200))
        offer = make_offer([make_position(do_contract=True,
                                          contract_quantity=Decimal("100"))])
        ergebnis = self.umgebung.run(offer)
        texte = " ".join(m for a in ergebnis.document_results for m in a.sap_messages)
        self.assertIn("Bestehender Kontrakt", texte)

    def test_protokoll_nennt_neuanlage(self) -> None:
        offer = make_offer([make_position(do_contract=True,
                                          contract_quantity=Decimal("100"))])
        ergebnis = self.umgebung.run(offer)
        texte = " ".join(m for a in ergebnis.document_results for m in a.sap_messages)
        self.assertIn("Kein laufender Kontrakt", texte)


# ===========================================================================
# 3. Kontierung der Bestellung
# ===========================================================================

class KontierungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)
        self.settings = self.umgebung.settings

    def _bestellen(self, item: DocumentItem):
        plan = make_order_plan([item])
        return self.umgebung.gateway.purchase_orders.create(plan,
                                                            self.umgebung.context())

    def test_lager_ohne_kontierung(self) -> None:
        self.settings.purchasing.account_assignment_category = ""
        ergebnis = self._bestellen(make_item())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        zeile = next(iter(self.umgebung.system.purchase_orders.values()))["items"][0]
        self.assertEqual("", zeile["account_assignment"])
        self.assertEqual("", zeile["cost_center"])

    def test_kostenstelle_wird_gesetzt(self) -> None:
        ergebnis = self._bestellen(make_item(account_assignment="K",
                                             cost_center="1000100",
                                             gl_account="400000"))
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        zeile = next(iter(self.umgebung.system.purchase_orders.values()))["items"][0]
        self.assertEqual("K", zeile["account_assignment"])
        self.assertEqual("1000100", zeile["cost_center"])
        self.assertEqual("400000", zeile["gl_account"])

    def test_fehlendes_sachkonto_wird_abgelehnt(self) -> None:
        self.settings.purchasing.require_gl_account_when_assigned = True
        ergebnis = self._bestellen(make_item(account_assignment="K",
                                             cost_center="1000100"))
        self.assertIs(ergebnis.state, ResultState.FAILED)
        self.assertIn("Sachkonto", ergebnis.message)
        self.assertEqual(0, len(self.umgebung.system.purchase_orders),
                         "Es wurde trotz fehlendem Sachkonto geschrieben")

    def test_fehlende_kostenstelle_bei_typ_k_wird_abgelehnt(self) -> None:
        self.settings.purchasing.require_gl_account_when_assigned = False
        ergebnis = self._bestellen(make_item(account_assignment="K"))
        self.assertIs(ergebnis.state, ResultState.FAILED)
        self.assertIn("Kostenstelle", ergebnis.message)

    def test_vorbelegung_aus_einstellungen_greift(self) -> None:
        self.settings.purchasing.account_assignment_category = "K"
        self.settings.purchasing.default_cost_center = "1000200"
        self.settings.purchasing.default_gl_account = "400100"
        ergebnis = self._bestellen(make_item())
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        zeile = next(iter(self.umgebung.system.purchase_orders.values()))["items"][0]
        self.assertEqual("K", zeile["account_assignment"])
        self.assertEqual("1000200", zeile["cost_center"])
        self.assertEqual("400100", zeile["gl_account"])

    def test_position_ueberschreibt_vorbelegung(self) -> None:
        self.settings.purchasing.account_assignment_category = "K"
        self.settings.purchasing.default_cost_center = "1000200"
        self.settings.purchasing.default_gl_account = "400100"
        ergebnis = self._bestellen(make_item(cost_center="9999999"))
        self.assertIs(ergebnis.state, ResultState.SUCCESS)
        zeile = next(iter(self.umgebung.system.purchase_orders.values()))["items"][0]
        self.assertEqual("9999999", zeile["cost_center"])

    def test_vorbelegung_ohne_kontierung_bleibt_leer(self) -> None:
        """Ohne Kontierungstyp sind Kostenstelle/Sachkonto bedeutungslos."""
        self.settings.purchasing.account_assignment_category = ""
        self.settings.purchasing.default_cost_center = "1000200"
        item = make_item()
        apply_account_assignment(item, self.settings)
        self.assertEqual("", item.account_assignment)
        self.assertEqual("", item.cost_center)

    def test_pruefung_meldet_klartext(self) -> None:
        item = make_item(account_assignment="K", cost_center="1000100")
        problem = validate_account_assignment(item, self.settings)
        self.assertIn("Sachkonto", problem)
        self.assertIn("47110001", problem)

    def test_pruefung_ohne_kontierung_ist_still(self) -> None:
        self.assertEqual("", validate_account_assignment(make_item(), self.settings))


# ===========================================================================
# 4. Mengenstaffeln
# ===========================================================================

def _staffel_freigeben(gateway) -> None:
    """Staffel-Feld-IDs als geprueft markieren (wie nach dem Aufzeichnen)."""
    for key in ("scale_quantity_cell", "scale_amount_cell"):
        gateway.selectors.mark_verified("info_record_conditions", key, True)


class StaffelTest(unittest.TestCase):

    def setUp(self) -> None:
        self.umgebung = _Umgebung(dry_run=False)
        self.settings = self.umgebung.settings
        self.settings.workflow.info_record_write_scales = True

    def _staffelangebot(self, mengen_preise) -> Offer:
        positionen = []
        for nummer, (menge, preis) in enumerate(mengen_preise, start=1):
            positionen.append(make_position(
                position_number=str(nummer * 10), quantity=Decimal(menge),
                price=Decimal(preis), remarks="Staffelpreis"))
        return make_offer(positionen)

    def _infosatz(self):
        schluessel = MockSapSystem.ir_key("47110001", "0000100234", "1000", "1000")
        return self.umgebung.system.info_records[schluessel]

    # -- Zusammenfassung ------------------------------------------------
    def test_positionen_werden_zusammengefasst(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10"),
                                      ("1000", "11.40")])
        ergebnis = self.umgebung.run(offer)
        self.assertEqual(0, ergebnis.failed,
                         [r.error_messages for r in ergebnis.results])
        self.assertEqual(3, len(self._infosatz()["scales"]))

    def test_grundpreis_ist_kleinste_abmenge(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        offer = self._staffelangebot([("1000", "11.40"), ("100", "12.85"),
                                      ("500", "12.10")])
        self.umgebung.run(offer)
        self.assertEqual("12.85", self._infosatz()["price"])
        self.assertEqual(["100", "12.85"], self._infosatz()["scales"][0])

    def test_zusammengefasste_position_wird_nicht_doppelt_verarbeitet(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10")])
        ergebnis = self.umgebung.run(offer)
        geschrieben = [a for r in ergebnis.results for a in r.actions
                       if a.action == "info_record" and a.state is ResultState.SUCCESS]
        self.assertEqual(1, len(geschrieben),
                         "Der Infosatz wurde mehrfach geschrieben")

    def test_zusammenfassung_ist_nachvollziehbar(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10")])
        ergebnis = self.umgebung.run(offer)
        texte = " ".join(a.message for r in ergebnis.results for a in r.actions)
        self.assertIn("Mengenstaffel", texte)
        self.assertIn("zusammengefasst", texte)

    def test_fuehrende_position_traegt_die_staffel(self) -> None:
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10")])
        self.umgebung.run(offer)
        fuehrend = offer.positions[0]
        self.assertTrue(fuehrend.has_scales)
        self.assertEqual(2, len(fuehrend.scale_quantities))
        self.assertEqual([offer.positions[1].uid], fuehrend.merged_scale_uids)

    def test_unterschiedliche_materialien_bleiben_getrennt(self) -> None:
        offer = make_offer([make_position(material_number="47110001"),
                            make_position(material_number="47110002",
                                          position_number="20",
                                          price=Decimal("9.10"),
                                          quantity=Decimal("500"))])
        self.umgebung.run(offer)
        for position in offer.positions:
            self.assertFalse(position.has_scales)

    def test_gleiche_menge_wird_nicht_zusammengefasst(self) -> None:
        """Zwei Zeilen mit derselben Menge sind keine Staffel."""
        offer = self._staffelangebot([("100", "12.85"), ("100", "12.10")])
        self.umgebung.run(offer)
        self.assertFalse(offer.positions[0].has_scales)

    # -- Kappung ---------------------------------------------------------
    def test_zu_viele_stufen_werden_gekappt(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        self.settings.workflow.max_scale_levels = 3
        offer = self._staffelangebot([("100", "12.85"), ("200", "12.50"),
                                      ("500", "12.10"), ("1000", "11.40"),
                                      ("2000", "10.90")])
        ergebnis = self.umgebung.run(offer)
        self.assertEqual(3, len(self._infosatz()["scales"]))
        texte = " ".join(m for r in ergebnis.results for a in r.actions
                         for m in a.sap_messages)
        self.assertIn("Staffel gekappt", texte)
        self.assertIn("10,90", texte, "Die entfallenen Stufen fehlen im Protokoll")
        self.assertNotIn("10,90", " ".join(
            m for r in ergebnis.results for a in r.actions
            for m in a.sap_messages if m.startswith("Mengenstaffel")),
            "Eine nicht geschriebene Stufe wird als gepflegt gemeldet")

    # -- Ungepruefte Feld-IDs --------------------------------------------
    def test_ungepruefte_feld_ids_nur_grundpreis(self) -> None:
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10")])
        ergebnis = self.umgebung.run(offer)
        self.assertEqual([], self._infosatz()["scales"])
        self.assertEqual("12.85", self._infosatz()["price"])
        texte = " ".join(m for r in ergebnis.results for a in r.actions
                         for m in a.sap_messages)
        self.assertIn("Staffel nicht geschrieben", texte)
        self.assertIn("ungeprueft", texte)

    def test_abgeschaltete_staffel_schreibt_nur_grundpreis(self) -> None:
        _staffel_freigeben(self.umgebung.gateway)
        self.settings.workflow.info_record_write_scales = False
        offer = self._staffelangebot([("100", "12.85"), ("500", "12.10")])
        self.umgebung.run(offer)
        self.assertEqual([], self._infosatz()["scales"])

    # -- Modell -----------------------------------------------------------
    def test_has_scales_erst_ab_zwei_stufen(self) -> None:
        position = make_position()
        self.assertFalse(position.has_scales)
        position.scale_quantities = [(Decimal("100"), Decimal("12.85"))]
        self.assertFalse(position.has_scales)
        position.scale_quantities.append((Decimal("500"), Decimal("12.10")))
        self.assertTrue(position.has_scales)

    def test_sortierung_und_anzeige(self) -> None:
        position = make_position()
        position.scale_quantities = [(Decimal("500"), Decimal("12.10")),
                                     (Decimal("100"), Decimal("12.85"))]
        self.assertEqual(Decimal("100"), position.sorted_scales()[0][0])
        self.assertIn("ab 100", position.scale_display())


if __name__ == "__main__":
    unittest.main(verbosity=2)
