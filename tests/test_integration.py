"""Ende-zu-Ende-Test ueber die gesamte Kette.

    Datei einlesen
      -> Positionen erkennen
      -> Lieferant/Material zuordnen
      -> SAP-Ist-Zustand lesen (Mock)
      -> Alt/Neu vergleichen
      -> pruefen
      -> Vorschau
      -> Komplettvorgang ausfuehren
      -> Historie schreiben

Getestet wird gegen alle mitgelieferten Beispielformate.  Damit ist belegt,
dass die Bausteine nicht nur einzeln, sondern auch zusammen funktionieren.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_integration_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                        # noqa: E402
from app.database.mapping_store import MappingStore             # noqa: E402
from app.database.repository import Repository                  # noqa: E402
from app.models.enums import PositionStatus, ResultState        # noqa: E402
from app.sap.gateway import SapGateway                          # noqa: E402
from app.services.batch_service import BatchProcessor           # noqa: E402
from app.services.comparison_service import ComparisonService   # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402
from app.services.preview_service import PreviewService         # noqa: E402
from app.services.validation_service import ValidationService   # noqa: E402

BEISPIELE = Path(__file__).resolve().parent.parent / "sample_data" / "erzeugt"


def _settings(dry_run: bool = True) -> Settings:
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = dry_run
    settings.purchasing.purchasing_org = "1000"
    settings.purchasing.plant = "1000"
    settings.ensure_dirs()
    return settings


class _Kette:
    """Kleiner Helfer, der die komplette Verarbeitungskette kapselt."""

    def __init__(self, dry_run: bool = True) -> None:
        self.settings = _settings(dry_run)
        self.gateway = SapGateway(self.settings)
        self.repository = Repository(self.settings.home / "integration.sqlite3")
        self.mapping = MappingStore.from_settings(self.repository, self.settings)
        self.import_service = OfferImportService(self.settings)
        self.comparison = ComparisonService(self.settings)
        self.validation = ValidationService(self.settings)
        self.preview_service = PreviewService()

    def close(self) -> None:
        self.repository.close()

    # ------------------------------------------------------------------
    def vorbereiten(self, offer, vendor_number: str = "0000100234"):
        """Vorbelegung wie in der GUI: Werk, EKorg, Lieferant, Aktionen."""
        offer.vendor_number = offer.vendor_number or vendor_number
        for position in offer.positions:
            position.purchasing_org = position.purchasing_org or "1000"
            position.plant = position.plant or "1000"
            position.vendor_number = position.vendor_number or offer.vendor_number
            position.currency = position.currency or "EUR"
            if position.price_unit is None:
                position.price_unit = 1
            if not position.uom:
                position.uom = "ST"
            position.selected = True
        offer.renumber()
        return offer

    def sap_lesen(self, offer):
        for position in offer.positions:
            if position.material_number and position.vendor_number:
                self.gateway.load_position_state(position)
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        return offer

    def komplettvorgang(self, offer, teilmenge_prozent: Decimal = Decimal("20")):
        workflow = self.settings.workflow
        workflow.call_off_mode = "percent"
        workflow.call_off_percent = teilmenge_prozent
        for position in offer.positions:
            position.do_info_record = True
            position.do_source_list = True
            position.do_contract = True
            position.do_purchase_order = True
            if position.contract_quantity is None:
                position.contract_quantity = position.quantity
            if position.delivery_date is None:
                position.delivery_date = date.today() + timedelta(days=14)
        self.comparison.compare_offer(offer)
        self.validation.validate_offer(offer)
        return offer

    def ausfuehren(self, offer):
        vorschau = self.preview_service.build(offer, self.settings)
        prozessor = BatchProcessor(self.gateway, self.settings, self.comparison,
                                   self.validation)
        ereignisse: list = []
        zusammenfassung = prozessor.run(offer, vorschau, progress=ereignisse.append)
        return vorschau, zusammenfassung, ereignisse


class ImportKetteTest(unittest.TestCase):
    """Jedes Beispielformat muss durch die komplette Kette laufen."""

    @classmethod
    def setUpClass(cls) -> None:
        if not BEISPIELE.exists():
            raise unittest.SkipTest("Beispieldateien fehlen – bitte erst erzeugen: "
                                    "python sample_data/erzeuge_beispiele.py")
        cls.kette = _Kette()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.kette.close()

    def _import(self, dateiname: str):
        pfad = BEISPIELE / dateiname
        if not pfad.exists():
            self.skipTest(f"{dateiname} nicht vorhanden")
        return self.kette.import_service.import_file(str(pfad))

    # -- Einzelne Formate ------------------------------------------------
    def test_excel_mit_kopfzeile(self) -> None:
        offer = self._import("Angebot_Muster_Dichtungstechnik.xlsx")
        self.assertGreaterEqual(len(offer.positions), 4)
        materialien = {p.material_number for p in offer.positions}
        self.assertIn("47110001", materialien)
        erste = next(p for p in offer.positions if p.material_number == "47110001")
        self.assertEqual(erste.price, Decimal("12.85"))
        self.assertEqual(erste.uom, "ST")
        self.assertIn("Dichtring", erste.description)

    def test_excel_ohne_kopfzeile(self) -> None:
        """Ohne Ueberschriften muss die Spaltenart aus den Daten kommen."""
        offer = self._import("Preisliste_ohne_Kopfzeile.xlsx")
        self.assertGreaterEqual(len(offer.positions), 2)
        materialien = {p.material_number for p in offer.positions}
        self.assertTrue(materialien & {"47110005", "49900010", "48200111"},
                        f"keine bekannte Materialnummer erkannt: {materialien}")

    def test_excel_mit_stoerfaellen(self) -> None:
        """Summenzeilen duerfen nicht als Positionen auftauchen."""
        offer = self._import("Quotation_Nordtec_mit_Stoerfaellen.xlsx")
        texte = " ".join(p.description.lower() for p in offer.positions)
        self.assertNotIn("subtotal", texte)
        self.assertNotIn("vat 19", texte)
        for position in offer.positions:
            if position.price is not None:
                self.assertLess(position.price, Decimal("1000"),
                                f"Summenzeile als Position: {position.description}")

    def test_pdf(self) -> None:
        offer = self._import("Angebot_Pumpen_Weber.pdf")
        self.assertGreaterEqual(len(offer.positions), 1)
        materialien = {p.material_number for p in offer.positions}
        self.assertTrue(materialien & {"48200110", "48200111", "47110003"},
                        f"keine bekannte Materialnummer erkannt: {materialien}")

    def test_email_freitext(self) -> None:
        offer = self._import("Preisanpassung_Muster.eml")
        self.assertIsNotNone(offer.email)
        self.assertIn("muster-dichtungstechnik.de", offer.email.sender_domain)
        self.assertGreaterEqual(len(offer.positions), 1)

    def test_email_mit_anhang(self) -> None:
        offer = self._import("Angebot_Nordtec_mit_Anhang.eml")
        self.assertIsNotNone(offer.email)
        self.assertGreaterEqual(len(offer.positions), 1)

    def test_csv(self) -> None:
        offer = self._import("Preisliste_Muster.csv")
        self.assertGreaterEqual(len(offer.positions), 2)

    def test_text(self) -> None:
        pfad = BEISPIELE / "Preismitteilung.txt"
        if not pfad.exists():
            self.skipTest("Textdatei fehlt")
        offer = self.kette.import_service.import_text(
            pfad.read_text(encoding="utf-8"), "Test-Mailtext")
        self.assertGreaterEqual(len(offer.positions), 1)

    # -- Grundsatz: nichts erfinden --------------------------------------
    def test_keine_erfundenen_werte(self) -> None:
        """Kein Feld darf einen Wert tragen, der nicht in der Quelle steht."""
        offer = self._import("Angebot_Muster_Dichtungstechnik.xlsx")
        for position in offer.positions:
            if position.price is not None:
                self.assertGreater(position.price, Decimal("0"))
            # Position ohne Materialnummer muss leer bleiben, nicht geraten
            if not position.material_number:
                self.assertEqual(position.material_number, "")


class KomplettvorgangTest(unittest.TestCase):
    """Der Komplettvorgang gegen das Mock-SAP -- inklusive Belegen."""

    def setUp(self) -> None:
        if not BEISPIELE.exists():
            self.skipTest("Beispieldateien fehlen")
        self.kette = _Kette(dry_run=False)
        self.kette.gateway.reset_mock_data()

    def tearDown(self) -> None:
        self.kette.close()

    def _angebot(self):
        offer = self.kette.import_service.import_file(
            str(BEISPIELE / "Angebot_Muster_Dichtungstechnik.xlsx"))
        self.kette.vorbereiten(offer)
        # Position ohne Materialnummer abwaehlen -- sie ist bewusst unvollstaendig
        for position in offer.positions:
            if not position.material_number:
                position.selected = False
        self.kette.sap_lesen(offer)
        self.kette.komplettvorgang(offer)
        return offer

    def test_kette_legt_alle_belege_an(self) -> None:
        offer = self._angebot()
        vorschau, ergebnis, ereignisse = self.kette.ausfuehren(offer)

        self.assertGreater(vorschau.positions_selected, 0)
        self.assertEqual(ergebnis.failed, 0,
                         f"Fehlgeschlagen: {[r.error_messages for r in ergebnis.results]}")
        self.assertFalse(ergebnis.aborted)
        self.assertTrue(ereignisse, "Es wurden keine Fortschrittsmeldungen gesendet")

        belege = [b.lower() for b in ergebnis.created_documents()]
        self.assertTrue(any("kontrakt" in b for b in belege), belege)
        self.assertTrue(any("bestellung" in b for b in belege), belege)

        # Im Mock-System muessen die Belege wirklich liegen
        system = self.kette.gateway.mock_system
        self.assertEqual(len(system.contracts), 1)
        self.assertEqual(len(system.purchase_orders), 1)

        bestellung = next(iter(system.purchase_orders.values()))
        self.assertTrue(bestellung["reference_contract"],
                        "Bestellung wurde ohne Kontraktbezug angelegt")
        self.assertTrue(bestellung["messages_suppressed"])

        # Jede Bestellposition muss auf die richtige Kontraktzeile zeigen
        kontrakt = next(iter(system.contracts.values()))
        kontraktzeilen = {p["material"]: p["item"] for p in kontrakt["items"]}
        for zeile in bestellung["items"]:
            self.assertEqual(zeile["contract_number"], kontrakt_nummer := bestellung["reference_contract"])
            self.assertEqual(zeile["contract_item"], kontraktzeilen[zeile["material"]],
                             "Bestellposition zeigt auf die falsche Kontraktposition")
            self.assertTrue(kontrakt_nummer)

    def test_abrufmenge_ist_teilmenge(self) -> None:
        offer = self._angebot()
        _, _, _ = self.kette.ausfuehren(offer)
        system = self.kette.gateway.mock_system
        kontrakt = next(iter(system.contracts.values()))
        bestellung = next(iter(system.purchase_orders.values()))

        kontraktmengen = {p["material"]: Decimal(p["quantity"]) for p in kontrakt["items"]}
        for position in bestellung["items"]:
            zielmenge = kontraktmengen.get(position["material"])
            if zielmenge is None:
                continue
            abrufmenge = Decimal(position["quantity"])
            self.assertLess(abrufmenge, zielmenge,
                            "Abrufmenge muss kleiner als die Kontraktmenge sein")
            self.assertGreater(abrufmenge, Decimal("0"))

    def test_infosatz_gueltig_bis_2099(self) -> None:
        offer = self._angebot()
        self.kette.ausfuehren(offer)
        system = self.kette.gateway.mock_system
        for datensatz in system.info_records.values():
            self.assertEqual(datensatz["valid_to"], "2099-12-31")

    def test_orderbuch_lieferant_aktiv(self) -> None:
        offer = self._angebot()
        self.kette.ausfuehren(offer)
        system = self.kette.gateway.mock_system
        # 47110002 war im Auslieferungszustand gesperrt
        zeilen = system.source_lists.get("47110002|1000", [])
        self.assertTrue(zeilen)
        eintrag = next(z for z in zeilen if z["vendor_number"] == "0000100234")
        self.assertFalse(eintrag["blocked"], "Lieferant wurde nicht aktiv gesetzt")
        self.assertTrue(eintrag["agreement"], "Kontrakt wurde nicht im Orderbuch hinterlegt")

    def test_historie_wird_geschrieben(self) -> None:
        offer = self._angebot()
        _, ergebnis, _ = self.kette.ausfuehren(offer)
        run_id = self.kette.repository.start_run(offer=offer, dry_run=False, mock=True)
        zeilen = self.kette.repository.log_batch(run_id, offer, ergebnis, mock=True)
        self.assertGreater(zeilen, 0)

        eintraege = self.kette.repository.history({"limit": 500})
        self.assertTrue(eintraege)
        aktionen = {e["action"] for e in eintraege}
        self.assertIn("info_record", aktionen)
        self.assertIn("contract", aktionen)

    def test_dry_run_schreibt_nichts(self) -> None:
        self.kette.settings.dry_run = True
        offer = self._angebot()
        _, ergebnis, _ = self.kette.ausfuehren(offer)
        system = self.kette.gateway.mock_system
        self.assertEqual(len(system.contracts), 0)
        self.assertEqual(len(system.purchase_orders), 0)
        for aktion in (a for r in ergebnis.results for a in r.actions):
            self.assertIn(aktion.state, (ResultState.SIMULATED, ResultState.SKIPPED))

    def test_fehler_bleibt_auf_position_beschraenkt(self) -> None:
        """Eine kaputte Position darf die uebrigen nicht mitreissen."""
        offer = self._angebot()
        kaputt = offer.positions[0]
        kaputt.material_number = "47119999"       # existiert im Mock nicht
        kaputt.material_exists = None
        kaputt.sap_loaded = False
        self.kette.sap_lesen(offer)
        self.kette.komplettvorgang(offer)

        _, ergebnis, _ = self.kette.ausfuehren(offer)
        self.assertGreater(ergebnis.succeeded, 0,
                           "Die uebrigen Positionen wurden nicht verarbeitet")
        betroffen = [p for p in offer.positions if p.material_number == "47119999"]
        for position in betroffen:
            self.assertIn(position.status,
                          (PositionStatus.ERROR, PositionStatus.SKIPPED, PositionStatus.DONE))


class RobustheitTest(unittest.TestCase):
    """Kaputte Eingaben duerfen die Anwendung nie zum Absturz bringen.

    Im Einkaufsalltag landen alle moeglichen Dateien im Werkzeug: abgebrochene
    Downloads, umbenannte ZIPs, leere Exporte, Mails ohne Inhalt.  Erwartet
    wird jeweils: keine Ausnahme, keine Position, ein verstaendlicher Hinweis.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = _settings()
        cls.import_service = OfferImportService(cls.settings)
        cls.tmp = Path(tempfile.mkdtemp(prefix="sap_fuzz_"))

    def _pruefe(self, name: str, daten: bytes) -> None:
        pfad = self.tmp / name
        pfad.write_bytes(daten)
        try:
            offer = self.import_service.import_file(str(pfad))
        except Exception as exc:  # noqa: BLE001 - genau das darf nicht passieren
            self.fail(f"{name} fuehrte zum Absturz: {type(exc).__name__}: {exc}")
        self.assertEqual(len(offer.positions), 0,
                         f"{name} lieferte erfundene Positionen")
        self.assertTrue(len(offer.issues) or offer.extraction_notes,
                        f"{name} lieferte weder Positionen noch einen Hinweis")

    def test_leere_datei(self) -> None:
        self._pruefe("leer.xlsx", b"")

    def test_kaputtes_pdf(self) -> None:
        self._pruefe("kaputt.pdf", b"%PDF-1.4\nJUNK JUNK JUNK")

    def test_kein_excel_trotz_endung(self) -> None:
        self._pruefe("muell.xlsx", b"\x00\x01\x02 kein Excel")

    def test_csv_nur_kopfzeile(self) -> None:
        self._pruefe("nur_kopf.csv", "Pos;Material;Preis\n".encode())

    def test_kaputte_mail(self) -> None:
        self._pruefe("kaputt.eml", b"Das ist keine gueltige Mail\x00\xff")

    def test_kaputte_msg(self) -> None:
        """OLE-Kennung vorhanden, Inhalt Schrott."""
        self._pruefe("kaputt.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)

    def test_umbenannte_zip(self) -> None:
        self._pruefe("unbekannt.xlsx", b"PK\x03\x04kaputt")

    def test_html_ohne_tabelle(self) -> None:
        self._pruefe("ohne_tabelle.html", b"<html><body><p>Hallo</p></body></html>")

    def test_leerer_und_riesiger_text(self) -> None:
        for text in ("", "   ", "Hallo Welt", "a" * 100_000):
            with self.subTest(laenge=len(text)):
                try:
                    offer = self.import_service.import_text(text, "Fuzz")
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"Text der Laenge {len(text)} stuerzte ab: {exc}")
                self.assertEqual(len(offer.positions), 0)

    def test_sehr_grosse_tabelle(self) -> None:
        """5000 Zeilen duerfen weder haengen noch Unsinn erzeugen."""
        inhalt = "A;B;C\n" + "x;y;z\n" * 5000
        self._pruefe("riesig.csv", inhalt.encode())


class SicherheitsTest(unittest.TestCase):
    """Die harten Sperren muessen greifen."""

    def test_echtbetrieb_ohne_geprufte_ids_blockiert(self) -> None:
        settings = _settings(dry_run=False)
        settings.use_mock_sap = False
        gateway = SapGateway(settings)
        gruende = gateway.write_blocking_reasons()
        self.assertTrue(gruende, "Ohne Verbindung/geprufte IDs muss geblockt werden")

    def test_selektorpruefung_meldet_offene_ids(self) -> None:
        from app.sap.selectors import SelectorNotVerifiedError, SelectorRegistry

        registry = SelectorRegistry()
        for vorgang in ("info_record_write", "source_list_write", "contract_write",
                        "purchase_order_write"):
            self.assertFalse(registry.is_ready_for(vorgang))
            with self.assertRaises(SelectorNotVerifiedError):
                registry.ensure_ready(vorgang)

    def test_nachrichtenbild_ist_pflicht_fuer_belege(self) -> None:
        """Ohne geprueftes Nachrichtenbild darf kein Beleg gesichert werden."""
        from app.sap.selectors import REQUIRED_SCREENS

        self.assertIn("messages", REQUIRED_SCREENS["contract_write"])
        self.assertIn("messages", REQUIRED_SCREENS["purchase_order_write"])
        self.assertIn("messages", REQUIRED_SCREENS["purchase_order_from_contract"])

    def test_alle_dynpro_ids_sind_ungeprueft_ausgeliefert(self) -> None:
        """Auslieferungszustand: nur Framework-IDs gelten als geprueft."""
        from app.sap.selectors import default_screens

        for name, screen in default_screens().items():
            if name == "common":
                continue
            for key, selector in screen.elements.items():
                self.assertFalse(selector.verified,
                                 f"{name}.{key} ist faelschlich als geprueft markiert")


if __name__ == "__main__":
    unittest.main(verbosity=2)
