"""Tests fuer Lieferantenstamm-Anlage/-Aenderung (XK01/XK02) gegen Mock-SAP.

Aufruf: ``python -m unittest tests.test_vendor_master -v``

Geprueft werden:
    * Lesen (read()) fuer existierende/gesperrte/nicht existierende Lieferanten
    * Schreiben (write()) -- Neuanlage, Aenderung, Vorbedingungen, Ruecklese-
      Pruefung, Dry Run, Sicherheitsabbruch bei widersprechendem Namen
    * die Selektorpruefung (ungeprueft blockiert im Echtbetrieb)
    * VendorMasterService (Fassade)
    * GUI-Dialoge (VendorAssignmentDialog / VendorCreateDialog)
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="vendor_master_tests_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import ResultState                                # noqa: E402
from app.models.offer import Offer                                      # noqa: E402
from app.models.sap_vendor import SapVendorRecord, VendorMasterPlan     # noqa: E402
from app.sap.gateway import SapGateway                                  # noqa: E402
from app.sap.interfaces import WriteContext                             # noqa: E402
from app.sap.selectors import SelectorNotVerifiedError, SelectorRegistry  # noqa: E402
from app.sap.vendor_service import verify_vendor_master_write           # noqa: E402
from app.services.vendor_master_service import VendorMasterService      # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    from app.gui.dialogs import VendorAssignmentDialog, VendorCreateDialog
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def tearDownModule() -> None:
    os.environ.pop("SAP_ANGEBOT_HOME", None)
    try:
        _TEMP_HOME.cleanup()
    except OSError:
        pass


def _application():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def make_settings(dry_run: bool = False) -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = dry_run
    return settings


def make_gateway(dry_run: bool = False) -> SapGateway:
    return SapGateway(make_settings(dry_run))


def make_context(gateway: SapGateway) -> WriteContext:
    return gateway.write_context()


def make_plan(**overrides) -> VendorMasterPlan:
    werte = dict(
        existing_vendor_number="",
        name="Neu Testlieferant GmbH",
        street="Musterstrasse 1",
        postal_code="12345",
        city="Musterstadt",
        country="DE",
        language="DE",
        purchasing_org="1000",
    )
    werte.update(overrides)
    return VendorMasterPlan(**werte)


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------

class VendorReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = make_gateway()

    def test_existierender_lieferant_wird_gelesen(self) -> None:
        record = self.gateway.vendors.read("0000100234")
        self.assertTrue(record.was_read)
        self.assertTrue(record.exists)
        self.assertEqual(record.name, "Muster Dichtungstechnik GmbH")
        self.assertEqual(record.city, "Bielefeld")
        self.assertFalse(record.blocked)

    def test_gesperrter_lieferant_wird_als_gesperrt_gelesen(self) -> None:
        record = self.gateway.vendors.read("0000103777")
        self.assertTrue(record.exists)
        self.assertTrue(record.blocked)

    def test_nicht_existierender_lieferant(self) -> None:
        record = self.gateway.vendors.read("0000199999")
        self.assertTrue(record.was_read)
        self.assertFalse(record.exists)
        self.assertEqual(record.read_error, "")

    def test_leere_lieferantennummer_liefert_fehler(self) -> None:
        record = self.gateway.vendors.read("")
        self.assertNotEqual(record.read_error, "")

    def test_alpha_konvertierung_beim_lesen(self) -> None:
        """Fuehrende Nullen duerfen keine Rolle spielen."""
        record = self.gateway.vendors.read("100234")
        self.assertTrue(record.exists)
        self.assertEqual(record.name, "Muster Dichtungstechnik GmbH")

    def test_summary_ungelesen(self) -> None:
        record = SapVendorRecord()
        self.assertEqual(record.summary(), {"Lieferant": "noch nicht gelesen"})

    def test_summary_nicht_vorhanden(self) -> None:
        record = self.gateway.vendors.read("0000199999")
        self.assertEqual(record.summary(), {"Lieferant": "nicht vorhanden"})

    def test_summary_vorhanden(self) -> None:
        record = self.gateway.vendors.read("0000100234")
        summary = record.summary()
        self.assertEqual(summary["Name"], "Muster Dichtungstechnik GmbH")
        self.assertIn("Gesperrt", summary)


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

class VendorWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = make_gateway()

    def test_neuanlage_erfolgreich(self) -> None:
        plan = make_plan()
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.SUCCESS)
        self.assertTrue(result.document_number)
        self.assertTrue(plan.document_number)

        gelesen = self.gateway.vendors.read(result.document_number)
        self.assertTrue(gelesen.exists)
        self.assertEqual(gelesen.name, "Neu Testlieferant GmbH")
        self.assertEqual(gelesen.city, "Musterstadt")

    def test_aenderung_erfolgreich(self) -> None:
        plan = make_plan(existing_vendor_number="0000100234",
                         name="Muster Dichtungstechnik GmbH",
                         city="Bielefeld-Neu")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.SUCCESS)
        self.assertEqual(result.document_number, "0000100234")

        gelesen = self.gateway.vendors.read("0000100234")
        self.assertEqual(gelesen.city, "Bielefeld-Neu")

    def test_fehlender_name_blockiert(self) -> None:
        plan = make_plan(name="")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.FAILED)
        self.assertIn("Name", result.message)

    def test_fehlendes_land_blockiert(self) -> None:
        plan = make_plan(country="")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.FAILED)
        self.assertIn("Land", result.message)

    def test_dry_run_schreibt_nichts(self) -> None:
        gateway = make_gateway(dry_run=True)
        plan = make_plan()
        vorher = dict(gateway.mock_system.vendors)
        result = gateway.vendors.write(plan, make_context(gateway))
        self.assertEqual(result.state, ResultState.SIMULATED)
        self.assertEqual(vorher, gateway.mock_system.vendors)

    def test_existierender_lieferant_mit_abweichendem_namen_nicht_ueberschrieben(self) -> None:
        plan = make_plan(existing_vendor_number="0000100234",
                         name="Ganz anderer Name GmbH")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.FAILED)
        self.assertIn("Sicherheitsabbruch", result.message)

        # Bestand bleibt unveraendert
        gelesen = self.gateway.vendors.read("0000100234")
        self.assertEqual(gelesen.name, "Muster Dichtungstechnik GmbH")

    def test_aenderung_an_nicht_existierendem_lieferanten_schlaegt_fehl(self) -> None:
        plan = make_plan(existing_vendor_number="0000199999")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.FAILED)

    def test_ruecklese_pruefung_erfolgreich(self) -> None:
        plan = make_plan()
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertTrue(any("Ruecklese-Pruefung" in m for m in result.sap_messages))

    def test_ruecklese_pruefung_erkennt_abweichung(self) -> None:
        from datetime import datetime as _dt
        record = SapVendorRecord(vendor_number="0000100234", exists=True,
                                 name="Anderer Name", city="Berlin", read_at=_dt.now())
        plan = make_plan(name="Muster Dichtungstechnik GmbH")
        ok, meldungen = verify_vendor_master_write(record, plan)
        self.assertFalse(ok)
        self.assertTrue(any("fehlgeschlagen" in m for m in meldungen))

    def test_ruecklese_pruefung_ohne_treffer_ist_fehler(self) -> None:
        from datetime import datetime as _dt
        record = SapVendorRecord(vendor_number="0000199999", exists=False, read_at=_dt.now())
        plan = make_plan()
        ok, meldungen = verify_vendor_master_write(record, plan)
        self.assertFalse(ok)

    def test_neuanlage_vergibt_neue_nummer(self) -> None:
        plan1 = make_plan(name="Erster Neuer GmbH")
        plan2 = make_plan(name="Zweiter Neuer GmbH")
        result1 = self.gateway.vendors.write(plan1, make_context(self.gateway))
        result2 = self.gateway.vendors.write(plan2, make_context(self.gateway))
        self.assertNotEqual(result1.document_number, result2.document_number)

    def test_alpha_konvertierung_bei_aenderung(self) -> None:
        plan = make_plan(existing_vendor_number="100234",
                         name="Muster Dichtungstechnik GmbH",
                         city="Bielefeld-Alpha")
        result = self.gateway.vendors.write(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.SUCCESS)
        gelesen = self.gateway.vendors.read("0000100234")
        self.assertEqual(gelesen.city, "Bielefeld-Alpha")

    def test_gesperrter_lieferant_bleibt_beim_lesen_gesperrt(self) -> None:
        record = self.gateway.vendors.read("0000103777")
        self.assertTrue(record.blocked)


# ---------------------------------------------------------------------------
# Selektorpruefung -- Echtbetrieb
# ---------------------------------------------------------------------------

class VendorSelectorTests(unittest.TestCase):
    def test_vendor_master_write_ist_ungeprueft(self) -> None:
        registry = SelectorRegistry()
        self.assertFalse(registry.is_ready_for("vendor_master_write"))
        with self.assertRaises(SelectorNotVerifiedError):
            registry.ensure_ready("vendor_master_write")

    def test_vendor_master_screen_ist_registriert(self) -> None:
        registry = SelectorRegistry()
        self.assertIn("vendor_master", registry.screens)
        self.assertTrue(registry.has("vendor_master", "name"))
        self.assertTrue(registry.has("vendor_master", "country"))

    def test_required_screens_enthalten_vendor_master_write(self) -> None:
        from app.sap.selectors import REQUIRED_SCREENS
        self.assertIn("vendor_master_write", REQUIRED_SCREENS)
        self.assertIn("vendor_master", REQUIRED_SCREENS["vendor_master_write"])


# ---------------------------------------------------------------------------
# VendorMasterService (Fassade)
# ---------------------------------------------------------------------------

class VendorMasterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = make_gateway()
        self.service = VendorMasterService(self.gateway)

    def test_check_exists_findet_bestehenden_lieferanten(self) -> None:
        match = self.service.check_exists("0000100234")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Muster Dichtungstechnik GmbH")

    def test_check_exists_liefert_none_fuer_unbekannten_lieferanten(self) -> None:
        match = self.service.check_exists("0000199999")
        self.assertIsNone(match)

    def test_draft_from_offer_setzt_nur_namen(self) -> None:
        offer = Offer(vendor_name="Neuer Lieferant AG", offer_number="ANG-1")
        plan = self.service.draft_from_offer(offer)
        self.assertEqual(plan.name, "Neuer Lieferant AG")
        self.assertEqual(plan.reference_offer, "ANG-1")
        self.assertEqual(plan.existing_vendor_number, "")
        # Alles Weitere bleibt bewusst leer -- keine erfundene Adresse
        self.assertEqual(plan.street, "")
        self.assertEqual(plan.postal_code, "")
        self.assertEqual(plan.city, "")
        self.assertEqual(plan.country, "")

    def test_draft_from_offer_ohne_namen_markiert_fehler(self) -> None:
        offer = Offer(vendor_name="", offer_number="ANG-2")
        plan = self.service.draft_from_offer(offer)
        self.assertNotEqual(plan.error, "")

    def test_missing_fields_erkennt_fehlenden_namen(self) -> None:
        plan = make_plan(name="")
        self.assertIn("Name", self.service.missing_fields(plan))

    def test_missing_fields_erkennt_fehlendes_land(self) -> None:
        plan = make_plan(country="")
        self.assertIn("Land", self.service.missing_fields(plan))

    def test_missing_fields_leer_wenn_vollstaendig(self) -> None:
        plan = make_plan()
        self.assertEqual(self.service.missing_fields(plan), [])

    def test_create_or_update_blockiert_bei_fehlenden_feldern(self) -> None:
        plan = make_plan(name="")
        result = self.service.create_or_update(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.FAILED)

    def test_create_or_update_reicht_an_gateway_durch(self) -> None:
        plan = make_plan(name="Durchgereicht GmbH")
        result = self.service.create_or_update(plan, make_context(self.gateway))
        self.assertEqual(result.state, ResultState.SUCCESS)
        gelesen = self.gateway.vendors.read(result.document_number)
        self.assertEqual(gelesen.name, "Durchgereicht GmbH")


# ---------------------------------------------------------------------------
# GUI-Dialoge
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class VendorGuiDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _application()

    def test_create_button_sichtbar_ohne_treffer(self) -> None:
        dialog = VendorAssignmentDialog("Neuer Lieferant", "", [], "")
        self.assertFalse(dialog.create_button.isHidden())
        self.assertTrue(dialog.create_button.isEnabled())
        dialog.deleteLater()

    def test_create_button_versteckt_mit_treffer(self) -> None:
        class _Match:
            vendor_number = "0000100234"
            name = "Muster Dichtungstechnik GmbH"
            score = 1.0
            blocked = False

        dialog = VendorAssignmentDialog("Muster Dichtungstechnik GmbH", "", [_Match()], "")
        self.assertTrue(dialog.create_button.isHidden())
        dialog.deleteLater()

    def test_create_dialog_vorbelegung(self) -> None:
        settings = make_settings()
        dialog = VendorCreateDialog("Neuer Lieferant GmbH", settings)
        self.assertEqual(dialog.name_edit.text(), "Neuer Lieferant GmbH")
        self.assertEqual(dialog.purchasing_org_edit.text(),
                         settings.purchasing.purchasing_org)
        self.assertTrue(dialog.country_edit.text())
        dialog.deleteLater()

    def test_create_dialog_plan_uebernimmt_eingaben(self) -> None:
        settings = make_settings()
        dialog = VendorCreateDialog("", settings)
        dialog.name_edit.setText("Getippter Name GmbH")
        dialog.street_edit.setText("Teststrasse 5")
        dialog.postal_code_edit.setText("54321")
        dialog.city_edit.setText("Testort")
        dialog.country_edit.setText("DE")
        plan = dialog.plan()
        self.assertEqual(plan.name, "Getippter Name GmbH")
        self.assertEqual(plan.street, "Teststrasse 5")
        self.assertEqual(plan.postal_code, "54321")
        self.assertEqual(plan.city, "Testort")
        self.assertEqual(plan.existing_vendor_number, "")
        dialog.deleteLater()

    def test_create_dialog_ohne_bestaetigung_kein_ergebnis(self) -> None:
        """Ohne Namen fuehrt _confirm() NIE zu einem accept()."""
        from unittest.mock import patch
        settings = make_settings()
        dialog = VendorCreateDialog("", settings)
        dialog.name_edit.setText("")
        with patch("app.gui.dialogs.QMessageBox.warning", return_value=None) as warn:
            dialog._confirm()
            warn.assert_called_once()
        self.assertEqual(dialog.result(), 0)  # QDialog.Rejected/keine Reaktion
        dialog.deleteLater()

    def test_create_dialog_bestaetigung_erforderlich(self) -> None:
        """Mit gueltigen Feldern fragt _confirm() explizit nach -- 'Nein' speichert nicht."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox
        settings = make_settings()
        dialog = VendorCreateDialog("Bestaetigungstest GmbH", settings)
        with patch("app.gui.dialogs.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.No) as frage:
            dialog._confirm()
            frage.assert_called_once()
        self.assertEqual(dialog.result(), 0)
        dialog.deleteLater()

    def test_assignment_dialog_hat_settings_attribut(self) -> None:
        settings = make_settings()
        dialog = VendorAssignmentDialog("X", "", [], "", settings=settings)
        self.assertIs(dialog.settings, settings)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
