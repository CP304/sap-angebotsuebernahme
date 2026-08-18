"""Tests fuer die Anlage des Angebotsdokuments am SAP-Objekt (GOS).

Aufruf: ``python -m unittest tests.test_attachments -v``

Geprueft werden:
    * Einstellungen (``AttachmentSettings``) -- Voreinstellungen und Speicherung
    * Modell -- welche Datei angehaengt wird und wie "kein Dokument vorhanden"
      gemeldet wird
    * Service gegen das Testsystem -- Anlage haengt am Objekt, zu grosse Datei
      wird abgelehnt, ungepruefte Feld-IDs sperren, Dry Run haengt nichts an,
      nicht ansprechbarer Windows-Dateidialog -> ehrliche Teilmeldung
    * Ablauf (``BatchProcessor``) -- Anlage nur nach erfolgreicher Aktion, ein
      Fehlschlag beim Anhaengen laesst die Position erfolgreich, je Beleg wird
      nur einmal angehaengt
    * Oberflaeche -- beide Gruppen, Materialpruefung ist KEIN Schreibhaken,
      Lieferantenpflege nicht je Position, Anlagen-Haken vorbelegt
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

# --- Testumgebung VOR dem Import der Anwendung setzen ----------------------
_TEMP_HOME = tempfile.TemporaryDirectory(prefix="attachment_tests_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config.settings import AttachmentSettings, Settings              # noqa: E402
from app.models.document_plan import (                                    # noqa: E402
    AttachmentRef,
    attachment_from_paths,
    build_contract_plans,
)
from app.models.enums import ResultState, SourceKind                      # noqa: E402
from app.models.offer import EmailContext, Offer                          # noqa: E402
from app.models.offer_position import OfferPosition                       # noqa: E402
from app.models.results import ActionResult                               # noqa: E402
from app.sap.attachment_service import (                                  # noqa: E402
    OBJECT_LABELS,
    SapAttachmentService,
    build_summary_lines,
    check_attachment,
    is_enabled,
    object_label,
    write_summary_file,
)
from app.sap.gateway import SapGateway                                    # noqa: E402
from app.sap.selectors import (                                           # noqa: E402
    REQUIRED_SCREENS,
    SelectorNotVerifiedError,
    SelectorRegistry,
)
from app.services.batch_service import BatchProcessor                     # noqa: E402
from app.services.comparison_service import ComparisonService             # noqa: E402
from app.services.preview_service import PreviewService                   # noqa: E402
from app.services.validation_service import ValidationService             # noqa: E402

try:
    from PySide6.QtWidgets import QApplication

    from app.gui.dialogs import ChainDialog
    from app.gui.position_details import PositionDetails
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


def make_gateway(settings: Settings | None = None) -> SapGateway:
    """Frisches Testsystem -- alle Suiten teilen sich dasselbe Home."""
    gateway = SapGateway(settings or make_settings())
    gateway.reset_mock_data()
    return gateway


def make_file(name: str = "angebot.pdf", size: int = 512) -> Path:
    ordner = Path(tempfile.mkdtemp(prefix="anlage_", dir=_TEMP_HOME.name))
    ziel = ordner / name
    ziel.write_bytes(b"x" * size)
    return ziel


def make_ref(name: str = "angebot.pdf", size: int = 512) -> AttachmentRef:
    datei = make_file(name, size)
    return AttachmentRef(path=str(datei), display_name=datei.name)


def make_offer(with_file: bool = True) -> Offer:
    offer = Offer(vendor_name="Muster Dichtungstechnik GmbH",
                  vendor_number="0000100234", offer_number="ANG-2026-04711",
                  currency="EUR", source_kind=SourceKind.EXCEL)
    if with_file:
        offer.source_files = [str(make_file("preisliste.xlsx"))]
    else:
        offer.source_kind = SourceKind.TEXT
    # Preise bewusst dicht am Bestand des Testsystems -- ein Preissprung
    # wuerde die Position blockieren und damit am eigentlichen Testgegenstand
    # (der Anlage) vorbeilaufen.
    for index, (material, preis, einheit) in enumerate(
            (("47110001", "13.20", 1), ("47110002", "8.90", 10)), start=1):
        offer.positions.append(OfferPosition(
            position_number=str(index * 10), material_number=material,
            description=f"Testposition {index}", quantity=Decimal("100"),
            uom="ST", price=Decimal(preis), price_unit=einheit, currency="EUR",
            vendor_number="0000100234", purchasing_org="1000", plant="1000",
        ))
    return offer


def run_batch(gateway: SapGateway, settings: Settings, offer: Offer):
    comparison = ComparisonService(settings)
    validation = ValidationService(settings)
    for position in offer.positions:
        gateway.load_position_state(position)
    comparison.compare_offer(offer)
    validation.validate_offer(offer)
    preview = PreviewService().build(offer, settings)
    processor = BatchProcessor(gateway, settings, comparison, validation)
    return processor.run(offer, preview)


def attachment_actions(summary) -> list[ActionResult]:
    ergebnisse: list[ActionResult] = []
    for result in summary.results:
        ergebnisse.extend(a for a in result.actions if a.action == "attachment")
    return ergebnisse


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

class AttachmentSettingsTests(unittest.TestCase):
    def test_voreinstellung_infosatz_an(self) -> None:
        self.assertTrue(AttachmentSettings().attach_to_info_record)

    def test_voreinstellung_orderbuch_aus(self) -> None:
        """Orderbuch traegt in vielen Systemen keine Anlagen."""
        self.assertFalse(AttachmentSettings().attach_to_source_list)

    def test_voreinstellung_kontrakt_und_bestellung_an(self) -> None:
        einstellungen = AttachmentSettings()
        self.assertTrue(einstellungen.attach_to_contract)
        self.assertTrue(einstellungen.attach_to_purchase_order)

    def test_voreinstellung_lieferant_aus(self) -> None:
        self.assertFalse(AttachmentSettings().attach_to_vendor)

    def test_voreinstellung_originaldatei_an_zusammenfassung_aus(self) -> None:
        einstellungen = AttachmentSettings()
        self.assertTrue(einstellungen.attach_original_file)
        self.assertFalse(einstellungen.attach_summary)

    def test_voreinstellung_groessengrenze(self) -> None:
        self.assertEqual(AttachmentSettings().max_attachment_mb, 10)

    def test_settings_traegt_attachments(self) -> None:
        settings = Settings()
        self.assertIsInstance(settings.attachments, AttachmentSettings)

    def test_einstellungen_werden_gespeichert_und_gelesen(self) -> None:
        settings = Settings()
        settings.attachments.attach_to_source_list = True
        settings.attachments.max_attachment_mb = 3
        ziel = Path(_TEMP_HOME.name) / "settings_anlage.json"
        settings.save(ziel)
        geladen = Settings.load(ziel)
        self.assertTrue(geladen.attachments.attach_to_source_list)
        self.assertEqual(geladen.attachments.max_attachment_mb, 3)

    def test_is_enabled_folgt_der_einstellung(self) -> None:
        settings = make_settings()
        self.assertTrue(is_enabled(settings, "info_record"))
        self.assertFalse(is_enabled(settings, "source_list"))
        settings.attachments.attach_to_source_list = True
        self.assertTrue(is_enabled(settings, "source_list"))

    def test_is_enabled_kennt_unbekannte_objektart_nicht(self) -> None:
        self.assertFalse(is_enabled(make_settings(), "irgendwas"))

    def test_objektbezeichnungen_vollstaendig(self) -> None:
        for schluessel in ("info_record", "source_list", "contract",
                           "purchase_order", "vendor"):
            self.assertIn(schluessel, OBJECT_LABELS)
        self.assertEqual(object_label("contract"), "Mengenkontrakt")


# ---------------------------------------------------------------------------
# Modell
# ---------------------------------------------------------------------------

class AttachmentModelTests(unittest.TestCase):
    def test_dateipfad_wird_ermittelt(self) -> None:
        offer = make_offer(with_file=True)
        ref = offer.resolve_attachment()
        self.assertTrue(ref.available)
        self.assertEqual(ref.display_name, "preisliste.xlsx")
        self.assertEqual(ref.error, "")

    def test_ergebnis_wird_am_angebot_gemerkt(self) -> None:
        offer = make_offer(with_file=True)
        ref = offer.resolve_attachment()
        self.assertIs(offer.attachment, ref)

    def test_kein_dokument_wird_gemeldet(self) -> None:
        """Eingefuegter Text hat keine Datei -- das muss gesagt werden."""
        offer = make_offer(with_file=False)
        ref = offer.resolve_attachment()
        self.assertFalse(ref.available)
        self.assertIn("keine Datei", ref.error)
        self.assertIn("eingefuegter Text", ref.error)

    def test_verschwundene_datei_wird_gemeldet(self) -> None:
        offer = make_offer(with_file=False)
        offer.source_files = [str(Path(_TEMP_HOME.name) / "gibtsnicht.pdf")]
        ref = offer.resolve_attachment()
        self.assertFalse(ref.available)
        self.assertIn("nicht mehr auffindbar", ref.error)

    def test_email_anhangspfad_wird_verwendet(self) -> None:
        datei = make_file("mailanhang.pdf")
        offer = make_offer(with_file=False)
        offer.source_files = []
        offer.email = EmailContext(subject="Preisanpassung",
                                   attachment_names=["mailanhang.pdf"],
                                   attachment_paths=[str(datei)])
        ref = offer.resolve_attachment()
        self.assertTrue(ref.available)
        self.assertEqual(ref.display_name, "mailanhang.pdf")

    def test_email_ohne_abgelegten_anhang_wird_gemeldet(self) -> None:
        offer = make_offer(with_file=False)
        offer.source_files = []
        offer.source_kind = SourceKind.EMAIL_BODY
        offer.email = EmailContext(subject="Preise", attachment_names=["a.pdf"])
        ref = offer.resolve_attachment()
        self.assertFalse(ref.available)
        self.assertIn("nicht abgelegt", ref.error)

    def test_attachment_from_paths_nimmt_erste_vorhandene_datei(self) -> None:
        fehlt = str(Path(_TEMP_HOME.name) / "weg.pdf")
        da = str(make_file("zweite.pdf"))
        ref = attachment_from_paths([fehlt, da])
        self.assertEqual(ref.path, da)

    def test_groesse_wird_ermittelt(self) -> None:
        ref = make_ref(size=2048)
        self.assertEqual(ref.size_bytes, 2048)
        self.assertGreater(ref.size_mb, 0)

    def test_leere_referenz_ist_nicht_verfuegbar(self) -> None:
        self.assertFalse(AttachmentRef().available)
        self.assertEqual(AttachmentRef().size_bytes, 0)

    def test_display_zeigt_fehler_wenn_vorhanden(self) -> None:
        self.assertEqual(AttachmentRef(error="kaputt").display(), "kaputt")

    def test_belegplan_traegt_die_anlage(self) -> None:
        offer = make_offer(with_file=True)
        ref = offer.resolve_attachment()
        for position in offer.positions:
            position.do_contract = True
        plaene = build_contract_plans(
            offer.positions, vendor_names={"0000100234": offer.vendor_name},
            purchasing_group="100", valid_from=None, valid_to=None,
            attachment=ref)
        self.assertEqual(len(plaene), 1)
        self.assertIs(plaene[0].attachment, ref)

    def test_belegplan_ohne_anlage_bleibt_leer(self) -> None:
        offer = make_offer(with_file=True)
        for position in offer.positions:
            position.do_contract = True
        plaene = build_contract_plans(
            offer.positions, vendor_names={}, purchasing_group="100",
            valid_from=None, valid_to=None)
        self.assertIsNone(plaene[0].attachment)


# ---------------------------------------------------------------------------
# Vorpruefungen
# ---------------------------------------------------------------------------

class AttachmentCheckTests(unittest.TestCase):
    def test_gueltige_datei_ist_in_ordnung(self) -> None:
        self.assertEqual(check_attachment(make_ref(), make_settings()), "")

    def test_fehlende_referenz_wird_gemeldet(self) -> None:
        self.assertIn("kein Angebotsdokument",
                      check_attachment(None, make_settings()))

    def test_fehlerbehaftete_referenz_wird_durchgereicht(self) -> None:
        meldung = check_attachment(AttachmentRef(error="Kein Dokument da."),
                                   make_settings())
        self.assertEqual(meldung, "Kein Dokument da.")

    def test_zu_grosse_datei_wird_abgelehnt(self) -> None:
        settings = make_settings()
        settings.attachments.max_attachment_mb = 1
        ref = make_ref("gross.pdf", size=2 * 1024 * 1024)
        meldung = check_attachment(ref, settings)
        self.assertIn("Grenze", meldung)
        self.assertIn("MB", meldung)

    def test_grenze_null_schaltet_pruefung_ab(self) -> None:
        settings = make_settings()
        settings.attachments.max_attachment_mb = 0
        ref = make_ref("gross2.pdf", size=2 * 1024 * 1024)
        self.assertEqual(check_attachment(ref, settings), "")

    def test_zusammenfassung_wird_erzeugt(self) -> None:
        settings = make_settings()
        offer = make_offer(with_file=True)
        zeilen = build_summary_lines(offer, offer.positions)
        self.assertTrue(any("ANG-2026-04711" in z for z in zeilen))
        ref = write_summary_file(settings, offer.offer_number, zeilen)
        self.assertTrue(ref.available)
        self.assertIn("ANG", ref.display_name)


# ---------------------------------------------------------------------------
# Service gegen das Testsystem
# ---------------------------------------------------------------------------

class MockAttachmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.gateway = make_gateway(self.settings)

    def test_anlage_haengt_am_objekt(self) -> None:
        ref = make_ref()
        result = self.gateway.attachments.attach(
            ref, "info_record", "5300001", self.gateway.write_context())
        self.assertEqual(result.state, ResultState.SUCCESS)
        self.assertEqual(result.action, "attachment")
        self.assertEqual(self.gateway.mock_system.attachments["info_record|5300001"],
                         [ref.display_name])

    def test_zweimal_anhaengen_erzeugt_keinen_doppeleintrag(self) -> None:
        ref = make_ref()
        for _ in range(2):
            self.gateway.attachments.attach(ref, "contract", "4600001",
                                            self.gateway.write_context())
        self.assertEqual(self.gateway.mock_system.attachments["contract|4600001"],
                         [ref.display_name])

    def test_zu_grosse_datei_wird_abgelehnt(self) -> None:
        self.settings.attachments.max_attachment_mb = 1
        ref = make_ref("riesig.pdf", size=2 * 1024 * 1024)
        result = self.gateway.attachments.attach(
            ref, "info_record", "5300002", self.gateway.write_context())
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertIn("Grenze", result.message)
        self.assertNotIn("info_record|5300002", self.gateway.mock_system.attachments)

    def test_ohne_dokument_wird_nichts_angehaengt(self) -> None:
        result = self.gateway.attachments.attach(
            AttachmentRef(error="Kein Dokument vorhanden."), "info_record",
            "5300003", self.gateway.write_context())
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertEqual(self.gateway.mock_system.attachments, {})

    def test_ohne_objektnummer_wird_nichts_angehaengt(self) -> None:
        result = self.gateway.attachments.attach(
            make_ref(), "contract", "", self.gateway.write_context())
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertIn("keine Nummer", result.message)

    def test_dry_run_haengt_nichts_an(self) -> None:
        settings = make_settings(dry_run=True)
        gateway = make_gateway(settings)
        result = gateway.attachments.attach(
            make_ref(), "purchase_order", "4500001", gateway.write_context())
        self.assertEqual(result.state, ResultState.SIMULATED)
        self.assertEqual(gateway.mock_system.attachments, {})

    def test_reset_leert_die_anlagen(self) -> None:
        self.gateway.attachments.attach(make_ref(), "info_record", "5300004",
                                        self.gateway.write_context())
        self.gateway.reset_mock_data()
        self.assertEqual(self.gateway.mock_system.attachments, {})

    def test_gateway_stellt_den_dienst_bereit(self) -> None:
        self.assertTrue(hasattr(self.gateway, "attachments"))
        self.assertTrue(hasattr(self.gateway.attachments, "attach"))


# ---------------------------------------------------------------------------
# Selektoren und Echtbetrieb
# ---------------------------------------------------------------------------

class AttachmentSelectorTests(unittest.TestCase):
    def test_maske_attachment_ist_registriert(self) -> None:
        registry = SelectorRegistry()
        self.assertIn("attachment", registry.screens)
        self.assertTrue(registry.has("attachment", "file_dialog_path"))

    def test_maske_ist_ungeprueft_und_mit_todo_versehen(self) -> None:
        registry = SelectorRegistry()
        self.assertFalse(registry.is_ready_for("attachment_write"))
        self.assertIn("TODO", registry.screens["attachment"].note)

    def test_required_screens_kennt_attachment_write(self) -> None:
        self.assertIn("attachment_write", REQUIRED_SCREENS)
        self.assertIn("attachment", REQUIRED_SCREENS["attachment_write"])

    def test_ensure_ready_wirft(self) -> None:
        with self.assertRaises(SelectorNotVerifiedError):
            SelectorRegistry().ensure_ready("attachment_write")

    def test_ungepruefte_feld_ids_sperren_das_schreiben(self) -> None:
        """Ungeprueft = keine Anlage, aber auch kein falsches Haekchen."""
        settings = make_settings()
        service = SapAttachmentService(None, settings, SelectorRegistry())
        ref = make_ref()
        result = service.attach(ref, "info_record", "5300005",
                                _context(settings, dry_run=False))
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertIn("ungeprueft", result.message)
        self.assertIn(ref.path, result.message)

    def test_windows_dateidialog_liefert_ehrliche_teilmeldung(self) -> None:
        """Ist die Dateiauswahl kein SAP-GUI-Element, gilt das NICHT als Erfolg."""
        settings = make_settings()
        registry = _verified_registry()
        service = SapAttachmentService(_FakeConnection(has_file_dialog=False),
                                       settings, registry)
        ref = make_ref()
        result = service.attach(ref, "info_record", "5300006",
                                _context(settings, dry_run=False))
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertNotEqual(result.state, ResultState.SUCCESS)
        self.assertIn("Betriebssystems", result.message)
        self.assertIn(ref.path, result.message)

    def test_sap_eigener_dateidialog_wird_bedient(self) -> None:
        settings = make_settings()
        verbindung = _FakeConnection(has_file_dialog=True)
        service = SapAttachmentService(verbindung, settings, _verified_registry())
        ref = make_ref()
        result = service.attach(ref, "contract", "4600002",
                                _context(settings, dry_run=False))
        self.assertEqual(result.state, ResultState.SUCCESS)
        self.assertIn(ref.path, verbindung.texts.values())

    def test_echtbetrieb_dry_run_haengt_nichts_an(self) -> None:
        settings = make_settings(dry_run=True)
        verbindung = _FakeConnection(has_file_dialog=True)
        service = SapAttachmentService(verbindung, settings, _verified_registry())
        result = service.attach(make_ref(), "contract", "4600003",
                                _context(settings, dry_run=True))
        self.assertEqual(result.state, ResultState.SIMULATED)
        self.assertEqual(verbindung.texts, {})

    def test_echtbetrieb_ohne_verbindung_meldet_ehrlich(self) -> None:
        settings = make_settings()
        service = SapAttachmentService(None, settings, _verified_registry())
        result = service.attach(make_ref(), "contract", "4600004",
                                _context(settings, dry_run=False))
        self.assertEqual(result.state, ResultState.SKIPPED)
        self.assertIn("keine SAP-Verbindung", result.message)


def _context(settings: Settings, dry_run: bool):
    from datetime import date

    from app.sap.interfaces import WriteContext
    return WriteContext(dry_run=dry_run, valid_from=date.today(), valid_to=None,
                        settings=settings)


def _verified_registry() -> SelectorRegistry:
    """Registry, in der die Anlagen-Maske als geprueft gilt."""
    registry = SelectorRegistry()
    for selector in registry.screens["attachment"].elements.values():
        selector.verified = True
    return registry


class _FakeStatus:
    is_error = False
    text = "Anlage wurde erstellt"

    def display(self) -> str:
        return self.text


class _FakeConnection:
    """Minimal nachgebaute SAP-Verbindung fuer den Echtbetriebspfad."""

    def __init__(self, has_file_dialog: bool) -> None:
        self.has_file_dialog = has_file_dialog
        self.allow_write = False
        self.texts: dict[str, str] = {}
        self.pressed: list[str] = []

    def ensure_transaction(self, transaction: str) -> None:
        self.transaction = transaction

    def leave_transaction(self) -> None:
        return None

    def press_button(self, element_id: str, expect_write: bool = False) -> None:
        self.pressed.append(element_id)

    def exists(self, element_id: str) -> bool:
        if "DY_PATH" in element_id or "DY_FILENAME" in element_id:
            return self.has_file_dialog
        return True

    def set_text(self, element_id: str, value: str, wait: bool = False) -> None:
        self.texts[element_id] = value

    def read_status(self) -> _FakeStatus:
        return _FakeStatus()

    def detect_popup(self):
        return None


# ---------------------------------------------------------------------------
# Ablauf (BatchProcessor)
# ---------------------------------------------------------------------------

class AttachmentBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.gateway = make_gateway(self.settings)

    def test_anlage_nach_erfolgreichem_infosatz(self) -> None:
        offer = make_offer(with_file=True)
        summary = run_batch(self.gateway, self.settings, offer)
        anlagen = attachment_actions(summary)
        self.assertTrue(anlagen)
        self.assertTrue(any(a.state is ResultState.SUCCESS for a in anlagen))
        self.assertTrue(any(k.startswith("info_record|")
                            for k in self.gateway.mock_system.attachments))

    def test_ohne_dokument_wird_gemeldet_statt_still_uebergangen(self) -> None:
        offer = make_offer(with_file=False)
        summary = run_batch(self.gateway, self.settings, offer)
        anlagen = attachment_actions(summary)
        self.assertTrue(anlagen)
        self.assertTrue(all(a.state is ResultState.SKIPPED for a in anlagen))
        self.assertEqual(self.gateway.mock_system.attachments, {})

    def test_fehlschlag_beim_anhaengen_laesst_die_position_erfolgreich(self) -> None:
        """Der Preis ist gepflegt -- eine fehlende Anlage entwertet das nicht."""
        offer = make_offer(with_file=False)     # -> Anlage schlaegt fehl
        summary = run_batch(self.gateway, self.settings, offer)
        self.assertEqual(summary.failed, 0)
        self.assertGreater(summary.succeeded, 0)
        for result in summary.results:
            self.assertIsNot(result.state, ResultState.FAILED)

    def test_abgeschaltete_einstellung_haengt_nichts_an(self) -> None:
        self.settings.attachments.attach_to_info_record = False
        self.settings.attachments.attach_to_contract = False
        self.settings.attachments.attach_to_purchase_order = False
        offer = make_offer(with_file=True)
        summary = run_batch(self.gateway, self.settings, offer)
        self.assertEqual(attachment_actions(summary), [])
        self.assertEqual(self.gateway.mock_system.attachments, {})

    def test_orderbuch_standardmaessig_ohne_anlage(self) -> None:
        offer = make_offer(with_file=True)
        for position in offer.positions:
            position.do_info_record = False
            position.do_source_list = True
        summary = run_batch(self.gateway, self.settings, offer)
        self.assertEqual(attachment_actions(summary), [])

    def test_orderbuch_mit_eingeschalteter_einstellung(self) -> None:
        self.settings.attachments.attach_to_source_list = True
        offer = make_offer(with_file=True)
        for position in offer.positions:
            position.do_info_record = False
            position.do_source_list = True
        summary = run_batch(self.gateway, self.settings, offer)
        self.assertTrue(attachment_actions(summary))
        self.assertTrue(any(k.startswith("source_list|")
                            for k in self.gateway.mock_system.attachments))

    def test_anlage_je_beleg_nur_einmal(self) -> None:
        """Ein Kontrakt mit zwei Positionen bekommt EINE Anlage."""
        offer = make_offer(with_file=True)
        for position in offer.positions:
            position.do_info_record = False
            position.do_contract = True
        summary = run_batch(self.gateway, self.settings, offer)
        kontrakt_anlagen = [a for a in attachment_actions(summary)
                            if a.state is ResultState.SUCCESS]
        self.assertEqual(len(kontrakt_anlagen), 1)
        eintraege = [v for k, v in self.gateway.mock_system.attachments.items()
                     if k.startswith("contract|")]
        self.assertEqual(len(eintraege), 1)
        self.assertEqual(len(eintraege[0]), 1)

    def test_keine_anlage_an_nicht_entstandenem_beleg(self) -> None:
        """Ohne Materialnummer entsteht nichts -- dann haengt auch nichts."""
        offer = make_offer(with_file=True)
        for position in offer.positions:
            position.material_number = ""
        summary = run_batch(self.gateway, self.settings, offer)
        self.assertEqual(self.gateway.mock_system.attachments, {})
        self.assertTrue(all(a.state is not ResultState.SUCCESS
                            for a in attachment_actions(summary)))

    def test_dry_run_haengt_nichts_an(self) -> None:
        settings = make_settings(dry_run=True)
        gateway = make_gateway(settings)
        offer = make_offer(with_file=True)
        summary = run_batch(gateway, settings, offer)
        self.assertEqual(gateway.mock_system.attachments, {})
        self.assertTrue(all(a.state is ResultState.SIMULATED
                            for a in attachment_actions(summary)))

    def test_zusammenfassung_als_zusatzanlage(self) -> None:
        self.settings.attachments.attach_summary = True
        offer = make_offer(with_file=True)
        run_batch(self.gateway, self.settings, offer)
        namen = [n for liste in self.gateway.mock_system.attachments.values()
                 for n in liste]
        self.assertTrue(any(n.startswith("Uebernahme_") for n in namen))
        self.assertTrue(any(n == "preisliste.xlsx" for n in namen))

    def test_nur_zusammenfassung_ohne_originaldatei(self) -> None:
        self.settings.attachments.attach_original_file = False
        self.settings.attachments.attach_summary = True
        offer = make_offer(with_file=True)
        run_batch(self.gateway, self.settings, offer)
        namen = [n for liste in self.gateway.mock_system.attachments.values()
                 for n in liste]
        self.assertTrue(namen)
        self.assertNotIn("preisliste.xlsx", namen)

    def test_ergebnisbezeichnung_der_anlage(self) -> None:
        self.assertEqual(
            ActionResult(action="attachment", state=ResultState.SUCCESS).action_label,
            "Anlage")


# ---------------------------------------------------------------------------
# Oberflaeche
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class AttachmentGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _application()

    # -- Detailansicht --------------------------------------------------
    def test_detailansicht_hat_beide_gruppen(self) -> None:
        details = PositionDetails(settings=make_settings())
        self.assertEqual(details.master_group_label.text(), "Stammdaten")
        self.assertEqual(details.process_group_label.text(), "Einkaufsvorgang")
        details.deleteLater()

    def test_materialpruefung_ist_kein_schreibhaken(self) -> None:
        """MM03 ist reines Lesen -- es darf dafuer kein Ankreuzfeld geben."""
        details = PositionDetails(settings=make_settings())
        self.assertFalse(hasattr(details, "check_material"))
        self.assertTrue(hasattr(details, "material_state_label"))
        details.deleteLater()

    def test_materialzustand_wird_angezeigt(self) -> None:
        details = PositionDetails(settings=make_settings())
        position = OfferPosition(material_number="47110001")
        details.set_position(position)
        self.assertIn("noch nicht geprueft", details.material_state_label.text())
        position.material_exists = True
        details.set_position(position)
        self.assertIn("vorhanden", details.material_state_label.text())
        position.material_exists = False
        details.set_position(position)
        self.assertIn("nicht vorhanden", details.material_state_label.text())
        details.deleteLater()

    def test_lieferantenpflege_ist_kein_haken_je_position(self) -> None:
        details = PositionDetails(settings=make_settings())
        self.assertFalse(hasattr(details, "check_vendor"))
        self.assertTrue(hasattr(details, "vendor_master_button"))
        details.deleteLater()

    def test_lieferantenpflege_meldet_sich_ueber_signal(self) -> None:
        details = PositionDetails(settings=make_settings())
        gemeldet: list = []
        details.requestVendorMaster.connect(gemeldet.append)
        details.vendor_master_button.click()
        self.assertEqual(len(gemeldet), 1)
        details.deleteLater()

    def test_anlagen_haken_standardmaessig_gesetzt(self) -> None:
        details = PositionDetails(settings=make_settings())
        self.assertTrue(details.attach_info.isChecked())
        self.assertTrue(details.attach_contract.isChecked())
        self.assertTrue(details.attach_order.isChecked())
        self.assertFalse(details.attach_source.isChecked())
        details.deleteLater()

    def test_anlagen_haken_wirken_auf_die_einstellungen(self) -> None:
        settings = make_settings()
        details = PositionDetails(settings=settings)
        details.attach_info.setChecked(False)
        self.assertFalse(settings.attachments.attach_to_info_record)
        details.attach_source.setChecked(True)
        self.assertTrue(settings.attachments.attach_to_source_list)
        details.deleteLater()

    def test_vier_belegaktionen_bleiben_in_ihrer_reihenfolge(self) -> None:
        details = PositionDetails(settings=make_settings())
        for box, text in ((details.check_info, "Infosatz"),
                          (details.check_source, "Orderbuch"),
                          (details.check_contract, "Kontrakt"),
                          (details.check_order, "Bestellung")):
            self.assertEqual(box.text(), text)
        details.deleteLater()

    # -- Komplettvorgang ------------------------------------------------
    def test_chain_dialog_hat_beide_gruppen(self) -> None:
        dialog = ChainDialog(make_settings(), 3)
        self.assertEqual(dialog.master_heading.text(), "Stammdaten")
        self.assertEqual(dialog.process_heading.text(), "Einkaufsvorgang")
        dialog.deleteLater()

    def test_chain_dialog_lieferantenpflege_mit_hinweis(self) -> None:
        dialog = ChainDialog(make_settings(), 3)
        self.assertIn("je Lieferant", dialog.check_vendor_master.text())
        self.assertIn("genau einmal", dialog.vendor_master_hint.text())
        dialog.deleteLater()

    def test_chain_dialog_materialpruefung_ohne_haken(self) -> None:
        dialog = ChainDialog(make_settings(), 3)
        self.assertFalse(hasattr(dialog, "check_material"))
        self.assertIn("MM03", dialog.vendor_master_hint.text())
        dialog.deleteLater()

    def test_chain_dialog_anlagen_haken_vorbelegt(self) -> None:
        dialog = ChainDialog(make_settings(), 3)
        self.assertTrue(dialog.attach_info.isChecked())
        self.assertTrue(dialog.attach_contract.isChecked())
        self.assertTrue(dialog.attach_order.isChecked())
        self.assertFalse(dialog.attach_source.isChecked())
        dialog.deleteLater()

    def test_chain_dialog_uebernimmt_anlagen_einstellungen(self) -> None:
        settings = make_settings()
        dialog = ChainDialog(settings, 2)
        dialog.attach_info.setChecked(False)
        dialog.attach_source.setChecked(True)
        dialog.apply_to(make_offer(with_file=True).positions)
        self.assertFalse(settings.attachments.attach_to_info_record)
        self.assertTrue(settings.attachments.attach_to_source_list)
        dialog.deleteLater()

    def test_chain_dialog_lieferantenpflege_nicht_je_position(self) -> None:
        """Der Stammdatenhaken darf keine Positionseigenschaft setzen."""
        settings = make_settings()
        dialog = ChainDialog(settings, 2)
        dialog.check_vendor_master.setChecked(True)
        positionen = make_offer(with_file=True).positions
        dialog.apply_to(positionen)
        self.assertTrue(dialog.maintain_vendor_master)
        self.assertTrue(settings.workflow.chain_vendor_master)
        for position in positionen:
            self.assertFalse(hasattr(position, "do_vendor_master"))
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)
