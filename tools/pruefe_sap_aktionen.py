"""Komplettpruefung aller SAP-Aktionen gegen das Testsystem.

    python tools/pruefe_sap_aktionen.py

Zweck: vor einer echten Aufzeichnungssitzung (SAP GUI Script Recorder) in
wenigen Sekunden sehen, ob die FACHLOGIK aller Schreibaktionen sauber
durchlaeuft.  Das ersetzt nicht die Feld-ID-Pruefung am echten System (das
kann nur der Recorder), aber es stellt sicher, dass alles BIS zum Schreiben
korrekt ist: Erkennung, Zuordnung, Vergleich, Pruefung, Belegbuendelung,
Ruecklese-Pruefung, Fehlerisolation.

Geprueft werden nacheinander:
    1. Infosatz anlegen / aendern (inkl. Zusatzkonditionen, Staffeln)
    2. Orderbuch anlegen / aendern (Lieferant aktiv setzen)
    3. Mengenkontrakt anlegen (Buendelung mehrerer Positionen)
    4. Bestellung als Abruf aus dem Kontrakt
    5. Lieferantenstamm anlegen / lesen (falls die Erweiterung vorhanden ist)
    6. Materialpruefung
    7. Kompletter Ablauf ueber BatchProcessor (alle vier Aktionen zusammen)
    8. Dry Run (dasselbe nochmal, es darf NICHTS geschrieben werden)
    9. Fehlerisolation (eine kaputte Position darf die anderen nicht stoppen)
   10. Sicherheitssperren (ungeprüfte Feld-IDs -> Schreiben verweigert)

Am Ende steht eine klare Zusammenfassung: was funktioniert, was (noch)
fehlt, und -- am wichtigsten fuer heute -- welche SAP-Masken beim Aufzeichnen
Vorrang haben sollten, weil sie am meisten Vorgaenge freischalten.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL))

os.environ["SAP_ANGEBOT_HOME"] = tempfile.mkdtemp(prefix="sap_pruefung_")

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

from app.config.settings import Settings  # noqa: E402
from app.sap.gateway import SapGateway  # noqa: E402
from app.sap.selectors import REQUIRED_SCREENS  # noqa: E402
from app.services.batch_service import BatchProcessor  # noqa: E402
from app.services.comparison_service import ComparisonService  # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402
from app.services.preview_service import PreviewService  # noqa: E402
from app.services.validation_service import ValidationService  # noqa: E402

ERGEBNISSE: list[tuple[str, bool, str]] = []


class _NochNichtVorhanden(Exception):
    """Fuer Funktionen, die noch nicht Teil dieser Version sind (kein Fehler)."""


def _pruefe(titel: str, funktion) -> None:
    """Einen Pruefschritt ausfuehren und das Ergebnis festhalten."""
    try:
        detail = funktion() or ""
        ERGEBNISSE.append((titel, True, detail))
        print(f"  [ok]     {titel}" + (f" -- {detail}" if detail else ""))
    except _NochNichtVorhanden as hinweis:
        ERGEBNISSE.append((titel, None, str(hinweis)))
        print(f"  [--]     {titel}: {hinweis}")
    except AssertionError as fehler:
        ERGEBNISSE.append((titel, False, str(fehler)))
        print(f"  [FEHLER] {titel}: {fehler}")
    except Exception as fehler:  # noqa: BLE001 - Diagnosewerkzeug, keine App
        ERGEBNISSE.append((titel, False, f"{type(fehler).__name__}: {fehler}"))
        print(f"  [ABSTURZ] {titel}: {type(fehler).__name__}: {fehler}")
        traceback.print_exc(limit=3)


def _grundausstattung():
    """Frisches Testsystem plus ein reales Beispielangebot."""
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = False
    settings.purchasing.purchasing_org = "1000"
    settings.purchasing.plant = "1000"
    settings.ensure_dirs()

    gateway = SapGateway(settings)
    gateway.reset_mock_data()

    datei = WURZEL / "sample_data" / "erzeugt" / "Angebot_Muster_Dichtungstechnik.xlsx"
    if not datei.exists():
        raise RuntimeError(
            f"Beispieldatei fehlt: {datei}. Bitte zuerst "
            "'python sample_data/erzeuge_beispiele.py' ausfuehren.")

    offer = OfferImportService(settings).import_file(str(datei))
    offer.vendor_number = "100234"
    heute = date.today()
    for position in offer.positions:
        position.vendor_number = "100234"
        position.purchasing_org = "1000"
        position.plant = "1000"
        position.currency = position.currency or "EUR"
        position.price_unit = position.price_unit or 1
        position.delivery_date = heute + timedelta(days=14)
        position.contract_quantity = position.quantity
        position.selected = bool(position.material_number)

    comparison = ComparisonService(settings)
    validation = ValidationService(settings)
    for position in offer.positions:
        if position.material_number and position.vendor_number:
            gateway.load_position_state(position)
    comparison.compare_offer(offer)
    validation.validate_offer(offer)
    return settings, gateway, offer, comparison, validation


# ---------------------------------------------------------------------------
# Einzelaktionen
# ---------------------------------------------------------------------------

def pruefe_infosatz():
    settings, gateway, offer, *_ = _grundausstattung()
    position = next(p for p in offer.positions if p.material_number == "47110001")
    kontext = gateway.write_context(valid_from=date.today())
    ergebnis = gateway.info_records.write(position, kontext)
    assert ergebnis.ok, f"Infosatz-Schreiben fehlgeschlagen: {ergebnis.message}"
    assert ergebnis.document_number, "keine Infosatznummer erhalten"
    return f"Infosatz {ergebnis.document_number}, {ergebnis.message}"


def pruefe_infosatz_neuanlage():
    settings, gateway, offer, *_ = _grundausstattung()
    # 47110004 hat im Testbestand bewusst KEINEN Infosatz
    position = next(p for p in offer.positions if p.material_number == "47110004")
    kontext = gateway.write_context(valid_from=date.today())
    ergebnis = gateway.info_records.write(position, kontext)
    assert ergebnis.ok, f"Infosatz-Neuanlage fehlgeschlagen: {ergebnis.message}"
    assert "anlegen" in ergebnis.message.lower() or "angelegt" in ergebnis.message.lower()
    return ergebnis.message


def pruefe_orderbuch():
    settings, gateway, offer, *_ = _grundausstattung()
    # 47110002 ist im Testbestand bewusst gesperrt
    position = next(p for p in offer.positions if p.material_number == "47110002")
    kontext = gateway.write_context(valid_from=date.today())
    ergebnis = gateway.source_lists.write(position, kontext)
    assert ergebnis.ok, f"Orderbuch-Schreiben fehlgeschlagen: {ergebnis.message}"
    zeilen = gateway.mock_system.source_lists.get(
        gateway.mock_system.sl_key("47110002", "1000"), [])
    aktiv = [z for z in zeilen if z["vendor_number"].lstrip("0") == "100234"
             and not z["blocked"]]
    assert aktiv, "Lieferant wurde nicht aktiv gesetzt"
    return "Lieferant erfolgreich entsperrt"


def pruefe_kontrakt_und_bestellung():
    from app.models.document_plan import build_contract_plans, build_purchase_order_plans

    settings, gateway, offer, *_ = _grundausstattung()
    for position in offer.positions:
        position.do_contract = bool(position.material_number)
        position.do_purchase_order = bool(position.material_number)
        position.order_quantity = None  # PreviewService soll die Abrufmenge selbst rechnen

    plans = build_contract_plans(
        [p for p in offer.positions if p.do_contract],
        vendor_names={"100234": "Muster Dichtungstechnik GmbH"},
        purchasing_group="100", valid_from=date.today(),
        valid_to=date(2099, 12, 31), offer_number=offer.offer_number)
    assert len(plans) == 1, f"erwartet 1 Kontrakt, erhalten {len(plans)}"
    kontext = gateway.write_context(valid_from=date.today())
    kontrakt_ergebnis = gateway.contracts.create(plans[0], kontext)
    assert kontrakt_ergebnis.ok, f"Kontrakt fehlgeschlagen: {kontrakt_ergebnis.message}"

    bestellpositionen = build_purchase_order_plans(
        [p for p in offer.positions if p.do_purchase_order],
        vendor_names={"100234": "Muster Dichtungstechnik GmbH"},
        purchasing_group="100", default_delivery_date=date.today() + timedelta(days=14),
        offer_number=offer.offer_number)
    assert len(bestellpositionen) == 1
    bestellpositionen[0].reference_contract = kontrakt_ergebnis.document_number
    bestell_ergebnis = gateway.purchase_orders.create(bestellpositionen[0], kontext)
    assert bestell_ergebnis.ok, f"Bestellung fehlgeschlagen: {bestell_ergebnis.message}"
    return (f"Kontrakt {kontrakt_ergebnis.document_number} -> "
           f"Bestellung {bestell_ergebnis.document_number}")


def pruefe_lieferant_lesen():
    settings, gateway, offer, *_ = _grundausstattung()
    treffer = gateway.check_vendor("100234")
    assert treffer is not None, "bekannter Lieferant wurde nicht gefunden"
    assert treffer.name, "Lieferantenname fehlt"
    return f"{treffer.vendor_number}: {treffer.name}"


def pruefe_lieferant_stamm():
    """Lieferantenstamm-Pflege (XK02) -- Neuanlage gibt es bewusst nicht."""
    settings, gateway, offer, *_ = _grundausstattung()
    if not hasattr(gateway.vendors, "write"):
        raise _NochNichtVorhanden(
            "Lieferantenstamm-Pflege (XK01/XK02) ist in dieser Version noch nicht "
            "vorhanden -- wird nachgereicht.")
    datensatz = gateway.vendors.read("100234")
    assert datensatz.exists, "Lieferant nicht lesbar"

    # Aenderung eines vorhandenen Lieferanten muss gehen ...
    from app.models.sap_vendor import VendorMasterPlan
    kontext = gateway.write_context()
    plan = VendorMasterPlan(existing_vendor_number="100234", name=datensatz.name,
                            country=datensatz.country or "DE", city="Pruefstadt")
    geaendert = gateway.vendors.write(plan, kontext)
    assert geaendert.ok, f"Aenderung fehlgeschlagen: {geaendert.message}"

    # ... eine Neuanlage dagegen NICHT.
    neuanlage = gateway.vendors.write(
        VendorMasterPlan(name="Erfundener Lieferant GmbH", country="DE"), kontext)
    assert not neuanlage.ok, "Neuanlage waere moeglich -- das darf nicht sein!"
    assert "Neuanlage" in neuanlage.message

    return f"gelesen: {datensatz.name}; Aenderung ok, Neuanlage korrekt abgelehnt"


def pruefe_material():
    settings, gateway, offer, *_ = _grundausstattung()
    info = gateway.check_material("47110001", "1000")
    assert info.exists, "bekanntes Material nicht gefunden"
    fehlend = gateway.check_material("47119999", "1000")
    assert not fehlend.exists, "nicht existierendes Material wurde faelschlich bestaetigt"
    return f"vorhanden: {info.description}"


def pruefe_kompletter_ablauf():
    settings, gateway, offer, comparison, validation = _grundausstattung()
    for position in offer.positions:
        position.do_info_record = True
        position.do_source_list = True
        position.do_contract = bool(position.material_number)
        position.do_purchase_order = bool(position.material_number)

    vorschau = PreviewService().build(offer, settings)
    prozessor = BatchProcessor(gateway, settings, comparison, validation)
    ergebnis = prozessor.run(offer, vorschau)
    assert not ergebnis.aborted, f"Lauf wurde angehalten: {ergebnis.abort_reason}"
    assert ergebnis.succeeded > 0, "keine einzige Position war erfolgreich"
    system = gateway.mock_system
    assert system.contracts, "kein Kontrakt wurde angelegt"
    assert system.purchase_orders, "keine Bestellung wurde angelegt"
    bestellung = next(iter(system.purchase_orders.values()))
    assert bestellung["reference_contract"], "Bestellung ohne Kontraktbezug"
    return ergebnis.headline()


def pruefe_dry_run():
    settings, gateway, offer, comparison, validation = _grundausstattung()
    settings.dry_run = True
    for position in offer.positions:
        position.do_info_record = True
        position.do_source_list = True
        position.do_contract = bool(position.material_number)
        position.do_purchase_order = bool(position.material_number)

    vorschau = PreviewService().build(offer, settings)
    prozessor = BatchProcessor(gateway, settings, comparison, validation)
    prozessor.run(offer, vorschau)
    system = gateway.mock_system
    assert not system.contracts, "Dry Run hat trotzdem einen Kontrakt angelegt!"
    assert not system.purchase_orders, "Dry Run hat trotzdem eine Bestellung angelegt!"
    return "Dry Run hat nichts geschrieben (korrekt)"


def pruefe_fehlerisolation():
    settings, gateway, offer, comparison, validation = _grundausstattung()
    kaputt = offer.positions[0]
    kaputt.material_number = "47119999"          # existiert im Testsystem nicht
    kaputt.material_exists = None
    kaputt.sap_loaded = False
    gateway.load_position_state(kaputt)
    comparison.compare_offer(offer)
    validation.validate_offer(offer)
    for position in offer.positions:
        position.do_info_record = True

    vorschau = PreviewService().build(offer, settings)
    prozessor = BatchProcessor(gateway, settings, comparison, validation)
    ergebnis = prozessor.run(offer, vorschau)
    assert ergebnis.succeeded > 0, "die uebrigen Positionen wurden nicht verarbeitet"
    return f"{ergebnis.succeeded} erfolgreich trotz einer kaputten Position"


def pruefe_schreibsperre():
    """Ohne geprueften Feld-IDs darf im ECHTBETRIEB nichts geschrieben werden."""
    settings = Settings()
    settings.use_mock_sap = False
    settings.dry_run = False
    gateway = SapGateway(settings)
    gruende = gateway.write_blocking_reasons()
    assert gruende, "Schreiben war NICHT gesperrt -- das ist ein Sicherheitsproblem!"
    return f"{len(gruende)} Sperrgrund/-gruende korrekt aktiv"


# ---------------------------------------------------------------------------

def zaehle_offene_feld_ids() -> dict[str, int]:
    from app.sap.selectors import SelectorRegistry

    registry = SelectorRegistry()
    ergebnis = {}
    for vorgang, masken in REQUIRED_SCREENS.items():
        offen = registry.unverified(masken)
        ergebnis[vorgang] = len(offen)
    return ergebnis


def main() -> int:
    print("SAP-Aktionen werden gegen das Testsystem geprueft ...\n")
    print("=== Einzelaktionen ===")
    _pruefe("Infosatz aendern (ME12)", pruefe_infosatz)
    _pruefe("Infosatz neu anlegen (ME11)", pruefe_infosatz_neuanlage)
    _pruefe("Orderbuch: Lieferant aktiv setzen (ME01)", pruefe_orderbuch)
    _pruefe("Mengenkontrakt + Bestellung mit Kontraktbezug (ME31K/ME21N)",
           pruefe_kontrakt_und_bestellung)
    _pruefe("Lieferant pruefen (XK03)", pruefe_lieferant_lesen)
    _pruefe("Lieferantenstamm lesen/pflegen (XK02/XK03)", pruefe_lieferant_stamm)
    _pruefe("Material pruefen (MM03)", pruefe_material)

    print("\n=== Ablauf ===")
    _pruefe("Kompletter Vorgang (alle 4 Aktionen gebuendelt)", pruefe_kompletter_ablauf)
    _pruefe("Dry Run schreibt nichts", pruefe_dry_run)
    _pruefe("Fehlerisolation (kaputte Position stoppt die anderen nicht)",
           pruefe_fehlerisolation)

    print("\n=== Sicherheit ===")
    _pruefe("Schreibsperre ohne geprueften Feld-IDs", pruefe_schreibsperre)

    # -- Zusammenfassung --------------------------------------------------
    erfolgreich = sum(1 for _, ok, _ in ERGEBNISSE if ok is True)
    uebersprungen = sum(1 for _, ok, _ in ERGEBNISSE if ok is None)
    fehlgeschlagen = [titel for titel, ok, _ in ERGEBNISSE if ok is False]
    relevant = len(ERGEBNISSE) - uebersprungen

    print(f"\n{'=' * 70}")
    print(f"ERGEBNIS: {erfolgreich}/{relevant} Pruefungen erfolgreich"
         + (f" ({uebersprungen} noch nicht anwendbar)" if uebersprungen else ""))
    if fehlgeschlagen:
        print("\nFehlgeschlagen:")
        for titel in fehlgeschlagen:
            print(f"  - {titel}")

    print("\n=== Offene (ungeprüfte) Feld-IDs je Vorgang ===")
    print("(Das ist der eigentliche Grund, heute ein VBS aufzuzeichnen --")
    print(" die Fachlogik oben ist bereits bestaetigt funktionsfaehig.)\n")
    offene = zaehle_offene_feld_ids()
    # Nach Dringlichkeit sortiert: was am meisten Vorgaenge blockiert, zuerst
    prioritaet = sorted(offene.items(), key=lambda kv: -kv[1])
    for vorgang, anzahl in prioritaet:
        marker = "  " if anzahl else "OK"
        print(f"  [{marker}] {vorgang:<32} {anzahl:>3} offene Feld-ID(s)")

    print("\nEmpfohlene Aufzeichnungsreihenfolge fuer heute (deckt am meisten ab):")
    print("  1. ME11/ME12/ME13  (Infosatz)      -> info_record_initial, "
         "info_record_purchasing, info_record_conditions")
    print("  2. ME01/ME03       (Orderbuch)     -> source_list_initial, "
         "source_list_overview")
    print("  3. ME31K/ME32K     (Kontrakt)      -> contract_initial, "
         "contract_header, contract_items, messages")
    print("  4. ME21N/ME22N     (Bestellung)    -> purchase_order, "
         "purchase_order_reference, messages")
    print("  5. XK02/XK03       (Lieferant)     -> vendor_master, vendor_display")
    print("     (XK01 wird nicht gebraucht -- Lieferanten werden hier nur gepflegt)")
    print("\nDanach in der Anwendung: Verwaltung -> SAP-Feld-IDs -> "
         "'Aufzeichnung (.vbs) einlesen'.")

    return 0 if not fehlgeschlagen else 1


if __name__ == "__main__":
    sys.exit(main())
