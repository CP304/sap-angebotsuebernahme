"""Infosatz: erst pruefen, dann entscheiden -- aendern, erweitern oder anlegen.

Der Fall, den man leicht uebersieht
-----------------------------------
Ein Einkaufsinfosatz hat zwei Ebenen: die allgemeinen Daten (Material +
Lieferant) und die Sicht je Einkaufsorganisation bzw. Werk.  Sehr haeufig
existiert der Satz bereits -- nur die Sicht fuer das eigene Werk fehlt.

Wer das nicht unterscheidet, macht einen von zwei Fehlern:

* meldet "vorhanden" und laesst ME12 auf eine Sicht los, die es nicht gibt
* meldet "nicht vorhanden" und laeuft mit ME11 in "Infosatz existiert bereits"

Richtig ist ein dritter Weg: den vorhandenen Satz um die fehlende Sicht
erweitern -- unter Beibehaltung der bestehenden Infosatznummer.  SAP vergibt
dabei keine zweite Nummer, und das darf die Anwendung auch nicht.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_ir_modes_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                    # noqa: E402
from app.models.enums import ResultState                    # noqa: E402
from app.models.offer_position import OfferPosition         # noqa: E402
from app.models.sap_info_record import SapInfoRecord        # noqa: E402
from app.sap.gateway import SapGateway                      # noqa: E402
from app.sap.mock_backend import MockSapSystem              # noqa: E402

#: Testbestand (siehe MockSapSystem.reset)
#:   47110001 -- Infosatz fuer EKorg 1000 UND Werk 1000  -> aendern
#:   48200111 -- Infosatz nur fuer EKorg 1000, ohne Werk -> erweitern
#:   47110004 -- gar kein Infosatz                       -> neu anlegen
MIT_WERK = ("47110001", "0000100234")
NUR_EKORG = ("48200111", "0000102100")
OHNE_ALLES = ("47110004", "0000100234")


def _gateway(dry_run: bool = False) -> SapGateway:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = dry_run
    settings.purchasing.purchasing_org = "1000"
    settings.purchasing.plant = "1000"
    settings.ensure_dirs()
    gateway = SapGateway(settings)
    gateway.reset_mock_data()
    return gateway


def _position(material: str, vendor: str, preis: str = "99.00",
              plant: str = "1000") -> OfferPosition:
    return OfferPosition(
        material_number=material, vendor_number=vendor, purchasing_org="1000",
        plant=plant, price=Decimal(preis), price_unit=1, currency="EUR", uom="ST")


# ---------------------------------------------------------------------------
# Erkennung der Lage
# ---------------------------------------------------------------------------

class ModusErkennungTest(unittest.TestCase):
    """Was steht in SAP -- und was folgt daraus?"""

    def setUp(self) -> None:
        self.gateway = _gateway()

    def test_vorhanden_mit_werk_ergibt_aendern(self) -> None:
        satz = self.gateway.info_records.read(*MIT_WERK, "1000", "1000")
        self.assertTrue(satz.exists)
        self.assertFalse(satz.exists_without_plant)
        self.assertEqual(satz.write_mode, "change")

    def test_nur_auf_ekorg_ebene_ergibt_erweitern(self) -> None:
        """Der entscheidende Fall: Satz da, Werkssicht fehlt."""
        satz = self.gateway.info_records.read(*NUR_EKORG, "1000", "1000")
        self.assertFalse(satz.exists, "darf NICHT als vorhanden gelten")
        self.assertTrue(satz.exists_without_plant)
        self.assertTrue(satz.needs_plant_extension)
        self.assertEqual(satz.write_mode, "extend")

    def test_gar_nichts_ergibt_anlegen(self) -> None:
        satz = self.gateway.info_records.read(*OHNE_ALLES, "1000", "1000")
        self.assertFalse(satz.exists)
        self.assertFalse(satz.exists_without_plant)
        self.assertEqual(satz.write_mode, "create")

    def test_erweiterungsfall_kennt_die_bestehende_nummer(self) -> None:
        """Ohne die Nummer koennte man den Satz nicht wiederfinden."""
        satz = self.gateway.info_records.read(*NUR_EKORG, "1000", "1000")
        self.assertTrue(satz.info_record_number)

    def test_ohne_werk_gefragt_ist_der_ekorg_satz_vorhanden(self) -> None:
        """Wer ohne Werk fragt, bekommt den EKorg-Satz als vorhanden."""
        satz = self.gateway.info_records.read(*NUR_EKORG, "1000", "")
        self.assertTrue(satz.exists)
        self.assertEqual(satz.write_mode, "change")

    def test_nicht_gelesener_satz_hat_keinen_modus_vorgaukelt(self) -> None:
        satz = SapInfoRecord()
        self.assertFalse(satz.was_read)
        self.assertEqual(satz.write_mode, "create")


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

class SchreibModusTest(unittest.TestCase):
    """Die richtige Transaktion und die richtige Nummer."""

    def setUp(self) -> None:
        self.gateway = _gateway()
        self.transaktionen = self.gateway.settings.transactions

    def test_aendern_nutzt_me12(self) -> None:
        ergebnis = self.gateway.info_records.write(
            _position(*MIT_WERK, preis="13.50"), self.gateway.write_context())
        self.assertEqual(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(ergebnis.transaction, self.transaktionen.info_record_change)

    def test_anlegen_nutzt_me11(self) -> None:
        ergebnis = self.gateway.info_records.write(
            _position(*OHNE_ALLES, preis="3.40"), self.gateway.write_context())
        self.assertEqual(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(ergebnis.transaction, self.transaktionen.info_record_create)

    def test_erweitern_nutzt_me11(self) -> None:
        """Die fehlende Sicht wird mit ME11 angelegt, nicht mit ME12."""
        ergebnis = self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        self.assertEqual(ergebnis.state, ResultState.SUCCESS)
        self.assertEqual(ergebnis.transaction, self.transaktionen.info_record_create)

    def test_erweitern_behaelt_die_bestehende_nummer(self) -> None:
        """Kernpunkt: SAP vergibt keine zweite Nummer -- wir auch nicht."""
        vorher = self.gateway.info_records.read(*NUR_EKORG, "1000", "1000")
        alte_nummer = vorher.info_record_number
        self.assertTrue(alte_nummer)

        ergebnis = self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        self.assertEqual(ergebnis.document_number, alte_nummer)

    def test_aendern_behaelt_die_nummer(self) -> None:
        vorher = self.gateway.info_records.read(*MIT_WERK, "1000", "1000")
        ergebnis = self.gateway.info_records.write(
            _position(*MIT_WERK, preis="13.50"), self.gateway.write_context())
        self.assertEqual(ergebnis.document_number, vorher.info_record_number)

    def test_anlegen_vergibt_eine_neue_nummer(self) -> None:
        ergebnis = self.gateway.info_records.write(
            _position(*OHNE_ALLES), self.gateway.write_context())
        self.assertTrue(ergebnis.document_number)

    def test_erweitern_legt_keinen_zweiten_satz_an(self) -> None:
        vorher = len(self.gateway.mock_system.info_records)
        self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        # Es kommt genau die Werkssicht dazu, kein weiterer Satz daneben
        self.assertEqual(len(self.gateway.mock_system.info_records), vorher + 1)

    def test_nach_dem_erweitern_ist_es_ein_aenderungsfall(self) -> None:
        self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        danach = self.gateway.info_records.read(*NUR_EKORG, "1000", "1000")
        self.assertEqual(danach.write_mode, "change")
        self.assertEqual(danach.price, Decimal("299.00"))

    def test_zweimal_schreiben_erzeugt_keinen_dritten_satz(self) -> None:
        for preis in ("299.00", "301.00"):
            self.gateway.info_records.write(
                _position(*NUR_EKORG, preis=preis), self.gateway.write_context())
        schluessel = MockSapSystem.ir_key(NUR_EKORG[0], NUR_EKORG[1], "1000", "1000")
        self.assertIn(schluessel, self.gateway.mock_system.info_records)
        danach = self.gateway.info_records.read(*NUR_EKORG, "1000", "1000")
        self.assertEqual(danach.price, Decimal("301.00"))


# ---------------------------------------------------------------------------
# Meldungen und Dry Run
# ---------------------------------------------------------------------------

class MeldungTest(unittest.TestCase):
    """Der Anwender muss sehen, was tatsaechlich passiert ist."""

    def setUp(self) -> None:
        self.gateway = _gateway()

    def test_erweitern_wird_als_erweitern_gemeldet(self) -> None:
        ergebnis = self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        gesamt = ergebnis.message + " " + " ".join(ergebnis.sap_messages)
        self.assertIn("erweitert", gesamt.lower(),
                      f"Erweiterung nicht erkennbar: {gesamt}")

    def test_alter_wert_erklaert_den_erweiterungsfall(self) -> None:
        ergebnis = self.gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), self.gateway.write_context())
        self.assertIn("Werkssicht", ergebnis.old_value)

    def test_dry_run_erweitern_schreibt_nichts(self) -> None:
        gateway = _gateway(dry_run=True)
        vorher = dict(gateway.mock_system.info_records)
        ergebnis = gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), gateway.write_context())
        self.assertEqual(ergebnis.state, ResultState.SIMULATED)
        self.assertIn("erweitern", ergebnis.message.lower())
        self.assertEqual(vorher, gateway.mock_system.info_records)

    def test_dry_run_nennt_die_bestehende_nummer(self) -> None:
        gateway = _gateway(dry_run=True)
        ergebnis = gateway.info_records.write(
            _position(*NUR_EKORG, preis="299.00"), gateway.write_context())
        self.assertTrue(ergebnis.document_number,
                        "auch im Dry Run muss die bestehende Nummer sichtbar sein")

    def test_dry_run_unterscheidet_alle_drei_faelle(self) -> None:
        gateway = _gateway(dry_run=True)
        erwartet = {MIT_WERK: "aendern", NUR_EKORG: "erweitern",
                    OHNE_ALLES: "anlegen"}
        for schluessel, wort in erwartet.items():
            ergebnis = gateway.info_records.write(
                _position(*schluessel), gateway.write_context())
            self.assertIn(wort, ergebnis.message.lower(),
                          f"{schluessel}: erwartet '{wort}', erhalten "
                          f"'{ergebnis.message}'")


# ---------------------------------------------------------------------------
# Vergleich und Anzeige
# ---------------------------------------------------------------------------

class VergleichTest(unittest.TestCase):
    """Der Erweiterungsfall darf nicht als 'unveraendert' durchgehen."""

    def setUp(self) -> None:
        self.gateway = _gateway()

    def test_erweiterungsfall_ist_kein_unveraendert(self) -> None:
        from app.services.comparison_service import ComparisonService

        position = _position(*NUR_EKORG, preis="299.00")
        self.gateway.load_position_state(position)
        ComparisonService(self.gateway.settings).compare_position(position)
        from app.models.enums import InfoRecordAction

        self.assertIsNot(position.info_record_action, InfoRecordAction.UNCHANGED)

    def test_erweiterungsfall_zeigt_keinen_alten_preis(self) -> None:
        """Es gibt fuer dieses Werk noch keinen Preis -- also auch keinen alten."""
        position = _position(*NUR_EKORG, preis="299.00")
        self.gateway.load_position_state(position)
        self.assertIsNone(position.old_price)


if __name__ == "__main__":
    unittest.main(verbosity=2)
