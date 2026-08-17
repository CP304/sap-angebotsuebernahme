"""Bildschirmfotos der Oberflaeche erzeugen (zur Beurteilung der UI).

    python tools/screenshots.py

Es werden *echte* Aufnahmen der laufenden Anwendung gemacht -- keine gemalten
Entwuerfe.  Was hier zu sehen ist, sieht der Anwender genauso.

Die Bilder landen in ``tools/ansichten/``.  Verwendet wird das Testsystem
(Mock-SAP) mit den mitgelieferten Beispielangeboten, damit realistische Werte
und Statusfarben zu sehen sind.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL))

# Eigenes Zuhause, damit echte Anwenderdaten unberuehrt bleiben
os.environ["SAP_ANGEBOT_HOME"] = tempfile.mkdtemp(prefix="sap_ansichten_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.bootstrap import build_services  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.gui.dialogs import ChainDialog, PreviewDialog, ResultDialog  # noqa: E402
from app.gui.main_window import MainWindow, apply_application_style  # noqa: E402
from app.gui.table_import_dialog import TableImportDialog  # noqa: E402
from app.models.results import ActionResult, BatchSummary, PositionResult  # noqa: E402
from app.models.enums import ResultState  # noqa: E402

ZIEL = WURZEL / "tools" / "ansichten"
BEISPIELE = WURZEL / "sample_data" / "erzeugt"


def _warten(app: QApplication, runden: int = 6) -> None:
    for _ in range(runden):
        app.processEvents()


def _aufnehmen(app: QApplication, widget, name: str) -> None:
    _warten(app)
    widget.grab().save(str(ZIEL / f"{name}.png"))
    print(f"  [ok] {name}.png")


def _beispiel_ergebnis(offer) -> BatchSummary:
    """Ein realistisches Verarbeitungsergebnis fuer die Ergebnisansicht."""
    summary = BatchSummary(dry_run=False)
    from datetime import datetime

    summary.started_at = datetime.now()
    summary.finished_at = datetime.now()
    for index, position in enumerate(offer.positions[:4]):
        ergebnis = PositionResult(position_uid=position.uid,
                                  label=position.display_name)
        if index == 2:
            ergebnis.actions.append(ActionResult(
                "info_record", ResultState.FAILED,
                "Material im Materialstamm nicht vorhanden", transaction="ME11",
                detail="SAP-Meldung M3 305: Material 47119999 existiert nicht"))
        else:
            ergebnis.actions.append(ActionResult(
                "info_record", ResultState.SUCCESS, "Infosatz geaendert",
                transaction="ME12", document_number="5300000123",
                old_value="12,40 EUR / 1 ST", new_value="12,85 EUR / 1 ST",
                duration_ms=1840))
            ergebnis.actions.append(ActionResult(
                "contract", ResultState.SUCCESS, "Mengenkontrakt angelegt",
                transaction="ME31K", document_number="4600001234", duration_ms=3120))
            ergebnis.actions.append(ActionResult(
                "source_list", ResultState.SUCCESS,
                "Orderbucheintrag angelegt, Lieferant aktiv",
                transaction="ME01", duration_ms=1450))
            ergebnis.actions.append(ActionResult(
                "purchase_order", ResultState.SUCCESS,
                "Bestellung angelegt (Abruf 20 % aus Kontrakt)",
                transaction="ME21N", document_number="4500009876", duration_ms=4210))
        summary.results.append(ergebnis)
    summary.document_results = [
        ActionResult("contract", ResultState.SUCCESS, "angelegt",
                     document_number="4600001234"),
        ActionResult("purchase_order", ResultState.SUCCESS, "angelegt",
                     document_number="4500009876"),
    ]
    return summary


def main() -> int:
    ZIEL.mkdir(parents=True, exist_ok=True)
    print(f"Ansichten werden erzeugt in: {ZIEL}")

    app = QApplication.instance() or QApplication([])
    settings = Settings()
    settings.use_mock_sap = True
    settings.dry_run = True
    settings.ensure_dirs()
    apply_application_style(app, settings)
    # Ohne echte Bildschirmausgabe findet Qt die Systemschrift nicht immer.
    # Fuer die Aufnahmen deshalb eine sicher vorhandene Schrift setzen.
    from PySide6.QtGui import QFont, QFontDatabase

    for name in ("Segoe UI", "Tahoma", "Verdana", "Arial", "DejaVu Sans"):
        if name in QFontDatabase.families():
            app.setFont(QFont(name, settings.ui.font_size))
            print(f"  Schrift: {name}")
            break

    services = build_services(settings)
    fenster = MainWindow(settings, services.as_dict())
    fenster.resize(1600, 960)
    fenster.show()
    _warten(app)

    # -- Leerer Startzustand -------------------------------------------
    _aufnehmen(app, fenster, "01_start_leer")

    # -- Angebot laden --------------------------------------------------
    datei = BEISPIELE / "Angebot_Muster_Dichtungstechnik.xlsx"
    if datei.exists() and services.import_service is not None:
        offer = services.import_service.import_file(str(datei))
        offer.vendor_number = "0000100234"
        fenster._offer_loaded(offer)
        _warten(app)
        _aufnehmen(app, fenster, "02_angebot_geladen")

        # -- SAP-Ist-Zustand lesen (Mock) -------------------------------
        for position in offer.positions:
            if position.material_number and position.vendor_number:
                services.gateway.load_position_state(position)
        if services.comparison:
            services.comparison.compare_offer(offer)
        if services.validation:
            services.validation.validate_offer(offer)
        fenster.table_model.refresh_all()
        fenster._update_counters()
        _warten(app)
        _aufnehmen(app, fenster, "03_sap_abgleich_alt_neu")

        # -- Detailansicht einer Position -------------------------------
        fenster.details.set_position(offer.positions[0])
        _warten(app)
        _aufnehmen(app, fenster, "04_detailansicht")
        _aufnehmen(app, fenster.details, "05_detailansicht_gross")

        # -- Komplettvorgang --------------------------------------------
        chain = ChainDialog(settings, len(offer.positions), fenster)
        chain.show()
        _aufnehmen(app, chain, "06_komplettvorgang")
        chain.apply_to([p for p in offer.positions if p.selected])
        chain.close()

        for position in offer.positions:
            if position.contract_quantity is None:
                position.contract_quantity = position.quantity
            position.delivery_date = date.today() + timedelta(days=14)
        if services.comparison:
            services.comparison.compare_offer(offer)
        if services.validation:
            services.validation.validate_offer(offer)
        fenster.table_model.refresh_all()
        _warten(app)
        _aufnehmen(app, fenster, "07_alle_aktionen_gesetzt")

        # -- Vorschau ----------------------------------------------------
        if services.preview is not None:
            vorschau = services.preview.build(offer, settings)
            dialog = PreviewDialog(vorschau, settings.dry_run, settings.use_mock_sap,
                                   fenster)
            dialog.resize(700, 620)
            dialog.show()
            _aufnehmen(app, dialog, "08_vorschau")
            dialog.close()

        # -- Ergebnis ----------------------------------------------------
        ergebnis = ResultDialog(_beispiel_ergebnis(offer), fenster)
        ergebnis.resize(820, 600)
        ergebnis.show()
        _aufnehmen(app, ergebnis, "09_ergebnis")
        ergebnis.close()

    # -- Mail mit Ergaenzungen im Text ----------------------------------
    # Der Alltagsfall: Preistabelle im Anhang, die entscheidenden Zusaetze
    # ("Position 30 entfaellt", "Mindestmenge 500") stehen im Mailtext.
    mail = BEISPIELE / "Mail_mit_Ergaenzungen_im_Text.eml"
    if mail.exists() and services.import_service is not None:
        try:
            zusammengefuehrt = services.import_service.import_file(str(mail))
            fenster._offer_loaded(zusammengefuehrt)
            for position in zusammengefuehrt.positions:
                if position.material_number and position.vendor_number:
                    services.gateway.load_position_state(position)
            if services.comparison:
                services.comparison.compare_offer(zusammengefuehrt)
            if services.validation:
                services.validation.validate_offer(zusammengefuehrt)
            fenster.table_model.refresh_all()
            fenster._update_counters()
            if zusammengefuehrt.positions:
                fenster.details.set_position(zusammengefuehrt.positions[0])
            _warten(app)
            _aufnehmen(app, fenster, "18_mail_mit_anhang_zusammengefuehrt")
        except Exception as fehler:  # noqa: BLE001
            print(f"  [uebersprungen] Mail-Ansicht: {fehler}")

    # -- Auffang 1: Tabelle einfuegen -----------------------------------
    tabelle = TableImportDialog(settings, "Muster Dichtungstechnik GmbH", fenster)
    tabelle.set_grid(TableImportDialog._parse_text(
        "Pos\tArtikel-Nr.\tIhre Artikelnummer\tBezeichnung\tMenge\tME\tPreis\n"
        "10\tDR-40527-NBR\t47110001\tDichtring NBR 40x52x7\t500\tST\t12,85\n"
        "20\tOR-2503-FPM\t47110002\tO-Ring Viton 25x3\t2000\tST\t8,90\n"
        "30\tWDR-30477\t47110003\tWellendichtring FPM\t250\tST\t18,95\n"
        "\t\t\tSumme\t\t\t9.912,50"))
    tabelle.resize(1100, 700)
    tabelle.show()
    _aufnehmen(app, tabelle, "10_auffang_tabelle_einfuegen")
    tabelle.close()

    # -- Auffang 2: grafisch anlernen -----------------------------------
    pdf = BEISPIELE / "Angebot_Pumpen_Weber.pdf"
    if pdf.exists():
        try:
            from app.gui.teach_dialog import ROW_ROLE, MarkedRegion, TeachDialog

            lernen = TeachDialog(str(pdf), "Pumpen Weber GmbH & Co. KG", fenster)
            lernen.resize(1200, 840)
            lernen.show()
            _warten(app)
            _aufnehmen(app, lernen, "11_auffang_anlernen_schritt1")

            lernen.regions.extend([
                MarkedRegion(ROW_ROLE, 50, 258, 540, 272, page=0),
                MarkedRegion("position_number", 50, 258, 100, 272, page=0),
                MarkedRegion("material_number", 105, 258, 170, 272, page=0),
                MarkedRegion("description", 175, 258, 325, 272, page=0),
                MarkedRegion("quantity", 330, 258, 380, 272, page=0),
                MarkedRegion("uom", 385, 258, 425, 272, page=0),
                MarkedRegion("price", 430, 258, 485, 272, page=0),
            ])
            lernen._refresh_regions()
            lernen._update_step()
            lernen._preview()
            _warten(app)
            _aufnehmen(app, lernen, "12_auffang_anlernen_markiert")
            lernen.close()
        except Exception as fehler:  # noqa: BLE001
            print(f"  [uebersprungen] Anlernansicht: {fehler}")

    # -- Ausfuehrliche Spaltenansicht -----------------------------------
    fenster.table.set_detailed_columns(True)
    _warten(app)
    _aufnehmen(app, fenster, "13_alle_spalten")
    fenster.table.set_detailed_columns(False)

    # -- Verwaltungsfenster ---------------------------------------------
    seiten = {"Historie": "14_historie", "Zuordnungen": "15_zuordnungen",
              "SAP-Feld-IDs": "16_sap_feld_ids", "Einstellungen": "17_einstellungen"}
    for titel, name in seiten.items():
        fenster.open_admin(titel)
        verwaltung = fenster._admin_window
        verwaltung.resize(1180, 800)
        _warten(app)
        _aufnehmen(app, verwaltung, name)
    if fenster._admin_window is not None:
        fenster._admin_window.hide()
    print("\nFertig. Bilder liegen in:", ZIEL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
