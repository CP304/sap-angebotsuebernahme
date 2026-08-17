"""Hauptfenster der Anwendung.

Die GUI enthaelt bewusst **keine** SAP-Automationslogik.  Sie ruft
ausschliesslich Services auf (``SapGateway``, ``BatchProcessor``,
``OfferImportService`` ...) und stellt deren Ergebnisse dar.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from ..models.enums import FieldOrigin, PositionStatus
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..sap.gateway import SapGateway
from ..utils.logging_setup import GuiLogHandler
from ..utils.parsing import format_date
from .dialogs import (
    ChainDialog,
    PasteTextDialog,
    PreviewDialog,
    ResultDialog,
    SessionDialog,
    VendorAssignmentDialog,
    ask_yes_no,
    show_error,
)
from .history_view import HistoryView
from .mapping_view import MappingView
from .offer_table import POSITION_ROLE, OfferFilterProxy, OfferTableModel, OfferTableView
from .position_details import PositionDetails
from .selector_view import SelectorView
from .settings_view import SettingsView
from .style import Colors, badge_style, build_stylesheet
from .workers import BatchWorker, ConnectWorker, ImportWorker, SapLoadWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Hauptfenster: Angebot, Positionen, Details, Verwaltung."""

    logMessage = Signal(str, str)

    def __init__(self, settings: Settings, services: dict, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.gateway: SapGateway = services["gateway"]
        self.import_service = services.get("import")
        self.comparison = services.get("comparison")
        self.validation = services.get("validation")
        self.preview_service = services.get("preview")
        self.batch_factory = services.get("batch_factory")
        self.undo = services.get("undo")
        self.repository = services.get("repository")
        self.mapping_store = services.get("mapping")
        self.startup_problems: list[str] = services.get("problems", [])

        self.offer: Offer | None = None
        self._offer_before_edits: Offer | None = None
        self._worker = None

        self.setWindowTitle("SAP-Angebotsuebernahme")
        self.resize(1560, 940)
        self.setAcceptDrops(True)

        self._build()
        self._connect_log()
        self._update_mode_badges()
        self._update_actions()
        QTimer.singleShot(200, self._show_startup_problems)

    # ==================================================================
    # Aufbau
    # ==================================================================
    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tabs.addTab(self._build_offer_page(), "Angebot")

        self.history_view = HistoryView(self.repository)
        self.tabs.addTab(self.history_view, "Historie")

        self.mapping_view = MappingView(self.repository)
        self.tabs.addTab(self.mapping_view, "Zuordnungen")

        self.selector_view = SelectorView(self.gateway.selectors, self.settings)
        self.selector_view.changed.connect(self._update_mode_badges)
        self.tabs.addTab(self.selector_view, "SAP-Feld-IDs")

        self.settings_view = SettingsView(self.settings)
        self.settings_view.modeChanged.connect(self._mode_changed)
        self.settings_view.settingsSaved.connect(self._mode_changed)
        self.settings_view.resetMockRequested.connect(self._reset_mock)
        self.tabs.addTab(self.settings_view, "Einstellungen")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log_view, "Protokoll")

        self._build_toolbar()
        self._build_statusbar()
        self._build_shortcuts()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Hauptfunktionen")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(bar)

        self.action_open = QAction("Angebot oeffnen", self)
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.setToolTip("PDF, Excel, E-Mail (.msg/.eml) oder CSV oeffnen (Strg+O)")
        self.action_open.triggered.connect(self.open_offer_dialog)
        bar.addAction(self.action_open)

        self.action_paste = QAction("Text einfuegen", self)
        self.action_paste.setToolTip("Angebotstext aus einer E-Mail einfuegen")
        self.action_paste.triggered.connect(self.paste_offer_text)
        bar.addAction(self.action_paste)

        bar.addSeparator()

        self.action_connect = QAction("SAP verbinden", self)
        self.action_connect.triggered.connect(self.connect_sap)
        bar.addAction(self.action_connect)

        self.action_session = QAction("Session waehlen", self)
        self.action_session.triggered.connect(self.choose_session)
        bar.addAction(self.action_session)

        self.action_load = QAction("SAP-Daten laden", self)
        self.action_load.setShortcut(QKeySequence("F5"))
        self.action_load.setToolTip("Ist-Zustand aus SAP lesen (F5) – es wird nichts geaendert")
        self.action_load.triggered.connect(self.load_sap_data)
        bar.addAction(self.action_load)

        bar.addSeparator()

        self.action_chain = QAction("Komplettvorgang ...", self)
        self.action_chain.setToolTip(
            "Infosatz, Mengenkontrakt, Orderbuch und Bestellung in einem Zug festlegen")
        self.action_chain.triggered.connect(self.configure_chain)
        bar.addAction(self.action_chain)

        self.action_process = QAction("Uebernehmen", self)
        self.action_process.setShortcut(QKeySequence("F9"))
        self.action_process.setToolTip("Ausgewaehlte Positionen in SAP verarbeiten (F9)")
        self.action_process.triggered.connect(self.process_offer)
        bar.addAction(self.action_process)

        self.action_cancel = QAction("Abbrechen", self)
        self.action_cancel.triggered.connect(self.cancel_worker)
        self.action_cancel.setEnabled(False)
        bar.addAction(self.action_cancel)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        bar.addWidget(spacer)

        self.dry_run_box = QCheckBox("Dry Run")
        self.dry_run_box.setToolTip("SAP nur lesen, nichts schreiben")
        self.dry_run_box.setChecked(self.settings.dry_run)
        self.dry_run_box.toggled.connect(self._dry_run_toggled)
        bar.addWidget(self.dry_run_box)

        self.mode_badge = QLabel("")
        self.mode_badge.setMinimumWidth(190)
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.mode_badge)

        self.connection_badge = QLabel("")
        self.connection_badge.setMinimumWidth(210)
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.connection_badge)

    def _build_offer_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Modell und Filter zuerst -- die Auswahlleiste haengt sich daran
        self.table_model = OfferTableModel(self)
        self.table_model.aboutToEdit.connect(self._snapshot)
        self.table_model.positionEdited.connect(self._position_edited)
        self.table_model.selectionChanged.connect(self._update_counters)

        self.proxy = OfferFilterProxy(self)
        self.proxy.setSourceModel(self.table_model)

        layout.addWidget(self._build_header_card())
        layout.addWidget(self._build_selection_bar())

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = OfferTableView()
        self.table.setModel(self.proxy)
        self.table.apply_column_widths()
        self.table.requestDetails.connect(self._show_details)
        self.table.requestVendorAssignment.connect(self.assign_vendor)
        self.table.requestRemove.connect(self._remove_positions)
        self.table.requestFillDown.connect(self._fill_down)
        splitter.addWidget(self.table)

        self.details = PositionDetails(self.comparison)
        self.details.positionChanged.connect(self._position_changed)
        self.details.requestVendorAssignment.connect(self.assign_vendor)
        self.details.requestReloadSap.connect(self._reload_single_position)
        self.details.issueAcknowledged.connect(lambda *_: self._revalidate())
        splitter.addWidget(self.details)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        if self.table.selectionModel() is not None:
            self.table.selectionModel().currentRowChanged.connect(self._current_row_changed)
        return page

    def _build_header_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(18)

        self.header_fields: dict[str, QLineEdit] = {}
        definitions = (
            ("vendor_name", "Lieferant", 0, 0),
            ("offer_number", "Angebotsnummer", 0, 2),
            ("offer_date", "Angebotsdatum", 0, 4),
            ("currency", "Waehrung", 1, 4),
            ("payment_terms", "Zahlungsbedingungen", 1, 0),
            ("incoterm", "Incoterm", 1, 2),
        )
        for key, label, row, column in definitions:
            caption = QLabel(label + ":")
            caption.setObjectName("FieldLabel")
            edit = QLineEdit()
            edit.editingFinished.connect(lambda k=key: self._header_edited(k))
            self.header_fields[key] = edit
            grid.addWidget(caption, row, column)
            grid.addWidget(edit, row, column + 1)

        self.vendor_number_label = QLabel("– kein SAP-Lieferant zugeordnet –")
        self.vendor_number_label.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")
        grid.addWidget(self.vendor_number_label, 2, 0, 1, 3)

        assign_button = QPushButton("SAP-Lieferant zuordnen ...")
        assign_button.clicked.connect(lambda: self.assign_vendor(None))
        grid.addWidget(assign_button, 2, 3)

        self.source_label = QLabel("Kein Angebot geladen")
        self.source_label.setObjectName("SubHeading")
        grid.addWidget(self.source_label, 2, 4, 1, 2)

        self.extraction_label = QLabel("")
        self.extraction_label.setObjectName("SubHeading")
        self.extraction_label.setWordWrap(True)
        grid.addWidget(self.extraction_label, 3, 0, 1, 6)

        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(5, 2)
        return card

    def _build_selection_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        for text, tip, handler in (
                ("Alle", "Alle Positionen auswaehlen", lambda: self._select_all(True)),
                ("Keine", "Auswahl aufheben", lambda: self._select_all(False)),
                ("Nur geaenderte", "Nur Positionen mit SAP-Aenderung auswaehlen",
                 self._select_changed),
                ("Nur fehlerfreie", "Nur Positionen ohne blockierende Fehler auswaehlen",
                 self._select_clean)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            layout.addWidget(button)

        layout.addSpacing(16)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen (Material, Beschreibung, Lieferant) ...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.proxy.set_search)
        layout.addWidget(self.search_edit, 1)

        self.status_filter = QComboBox()
        self.status_filter.addItem("Alle Status", None)
        for status in (PositionStatus.READY, PositionStatus.CHECK, PositionStatus.ERROR,
                       PositionStatus.DONE, PositionStatus.SKIPPED):
            self.status_filter.addItem(status.label, status)
        self.status_filter.currentIndexChanged.connect(self._status_filter_changed)
        layout.addWidget(self.status_filter)

        self.only_changed_box = QCheckBox("nur mit Aenderung")
        self.only_changed_box.toggled.connect(self.proxy.set_only_changed)
        layout.addWidget(self.only_changed_box)

        self.only_selected_box = QCheckBox("nur ausgewaehlte")
        self.only_selected_box.toggled.connect(self.proxy.set_only_selected)
        layout.addWidget(self.only_selected_box)

        layout.addSpacing(12)
        add_button = QPushButton("Position ergaenzen")
        add_button.clicked.connect(self._add_position)
        layout.addWidget(add_button)
        return bar

    def _build_statusbar(self) -> None:
        status = self.statusBar()
        self.counter_label = QLabel("Kein Angebot geladen")
        status.addWidget(self.counter_label, 1)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(280)
        self.progress.setVisible(False)
        status.addPermanentWidget(self.progress)

        self.progress_label = QLabel("")
        status.addPermanentWidget(self.progress_label)

    def _build_shortcuts(self) -> None:
        undo_action = QAction("Rueckgaengig", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self._undo)
        self.addAction(undo_action)

        redo_action = QAction("Wiederherstellen", self)
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.triggered.connect(self._redo)
        self.addAction(redo_action)

    def _connect_log(self) -> None:
        handler = GuiLogHandler(lambda level, text: self.logMessage.emit(level, text))
        logging.getLogger().addHandler(handler)
        self.logMessage.connect(self._append_log)

    def _append_log(self, level: str, text: str) -> None:
        self.log_view.appendPlainText(text)

    # ==================================================================
    # Angebot laden
    # ==================================================================
    def open_offer_dialog(self) -> None:
        extensions = "*.pdf *.xlsx *.xlsm *.xls *.csv *.msg *.eml *.txt *.html"
        if self.import_service is not None:
            try:
                extensions = " ".join(f"*{e}" for e in self.import_service.supported_extensions())
            except Exception:  # noqa: BLE001
                pass
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Angebot oeffnen", "",
            f"Angebote ({extensions});;Alle Dateien (*.*)")
        if paths:
            self.open_offers(paths)

    def open_offers(self, paths: list[str]) -> None:
        if self.import_service is None:
            show_error(self, "Import nicht verfuegbar",
                       "Die Erkennungskomponente konnte nicht geladen werden.")
            return
        if not self._confirm_discard():
            return
        worker = ImportWorker(self.import_service, paths=paths)
        worker.finished_ok.connect(self._offer_loaded)
        worker.failed.connect(lambda m, d: show_error(self, "Import fehlgeschlagen", m, d))
        self._start_worker(worker, f"{len(paths)} Datei(en) werden gelesen ...")

    def paste_offer_text(self) -> None:
        if self.import_service is None:
            show_error(self, "Import nicht verfuegbar",
                       "Die Erkennungskomponente konnte nicht geladen werden.")
            return
        dialog = PasteTextDialog(self)
        if dialog.exec() != PasteTextDialog.DialogCode.Accepted:
            return
        if not dialog.text.strip():
            return
        if not self._confirm_discard():
            return
        worker = ImportWorker(self.import_service, text=dialog.text,
                              source_name=dialog.source_name)
        worker.finished_ok.connect(self._offer_loaded)
        worker.failed.connect(lambda m, d: show_error(self, "Auswertung fehlgeschlagen", m, d))
        self._start_worker(worker, "Text wird ausgewertet ...")

    def _offer_loaded(self, offer: Offer) -> None:
        self.offer = offer
        self._apply_defaults(offer)
        self._resolve_vendor(offer)
        self._resolve_materials(offer)
        self._revalidate()

        self._offer_before_edits = copy.deepcopy(offer)
        if self.undo is not None:
            self.undo.clear()

        self.table_model.set_offer(offer)
        self.table.apply_column_widths()
        self._fill_header()
        self._update_counters()
        self._update_actions()
        self.tabs.setCurrentIndex(0)

        for path in offer.source_files:
            self.settings.add_recent_file(path)

        notes = "   •   ".join(offer.extraction_notes[:4])
        self.extraction_label.setText(notes)
        logger.info("Angebot geladen: %s (%d Positionen)", offer.source_label,
                    len(offer.positions))

        if not offer.positions:
            show_error(self, "Keine Positionen erkannt",
                       "In der Datei wurden keine Angebotspositionen gefunden.\n\n"
                       "Sie koennen Positionen von Hand ergaenzen oder den Text ueber "
                       "„Text einfuegen“ auswerten lassen.",
                       "\n".join(offer.extraction_notes))

    def _apply_defaults(self, offer: Offer) -> None:
        """Werk, Einkaufsorganisation und Vorbelegungen setzen."""
        purchasing = self.settings.purchasing
        workflow = self.settings.workflow
        ui = self.settings.ui

        if not offer.currency:
            offer.set_field("currency", purchasing.currency, FieldOrigin.DEFAULT)

        for position in offer.positions:
            if not position.purchasing_org:
                position.purchasing_org = purchasing.purchasing_org
            if not position.plant:
                position.plant = purchasing.plant
            if not position.currency:
                position.set_field("currency", offer.currency or purchasing.currency,
                                   FieldOrigin.DEFAULT)
            if position.price_unit is None:
                position.set_field("price_unit", purchasing.price_unit, FieldOrigin.DEFAULT)
            if not position.uom:
                position.set_field("uom", purchasing.order_unit, FieldOrigin.DEFAULT)
            if position.valid_from is None and offer.valid_from is not None:
                position.set_field("valid_from", offer.valid_from, FieldOrigin.EXTRACTED)
            position.selected = ui.autoselect_after_import
            position.do_info_record = workflow.chain_info_record
            position.do_source_list = workflow.chain_source_list
            position.do_contract = workflow.chain_contract
            position.do_purchase_order = workflow.chain_purchase_order
            if position.delivery_date is None:
                position.delivery_date = date.today() + timedelta(
                    days=purchasing.default_delivery_days)
        offer.renumber()

    def _resolve_vendor(self, offer: Offer) -> None:
        """SAP-Lieferant ueber die lokalen Zuordnungen bestimmen (nie raten)."""
        if self.mapping_store is None:
            return
        domain = offer.email.sender_domain if offer.email else ""
        try:
            resolution = self.mapping_store.resolve_vendor(
                offer.vendor_name, domain, offer.vendor_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lieferantenzuordnung fehlgeschlagen: %s", exc)
            return
        number = getattr(resolution, "vendor_number", "")
        if not number:
            return
        origin = (FieldOrigin.EXTRACTED if getattr(resolution, "source", "") == "hint"
                  else FieldOrigin.MAPPED)
        offer.set_field("vendor_number", number, origin)
        if getattr(resolution, "vendor_name", "") and not offer.vendor_name:
            offer.set_field("vendor_name", resolution.vendor_name, FieldOrigin.MAPPED)
        for position in offer.positions:
            if not position.vendor_number:
                position.vendor_number = number

    def _resolve_materials(self, offer: Offer) -> None:
        if self.mapping_store is None:
            return
        for position in offer.positions:
            if position.material_number:
                continue
            try:
                resolution = self.mapping_store.resolve_material(
                    offer.vendor_number, position.vendor_material_number,
                    position.description)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Materialzuordnung fehlgeschlagen: %s", exc)
                continue
            number = getattr(resolution, "material_number", "")
            if number:
                position.set_field("material_number", number, FieldOrigin.MAPPED)

    # ==================================================================
    # SAP
    # ==================================================================
    def connect_sap(self) -> None:
        if self.settings.use_mock_sap:
            QMessageBox.information(
                self, "Testsystem aktiv",
                "Die Anwendung laeuft im Testsystem (Mock-SAP).\n\n"
                "Zum Verbinden mit einem echten SAP schalten Sie unter "
                "Einstellungen → Betriebsart das Testsystem ab.")
            return
        worker = ConnectWorker(self.gateway)
        worker.finished_ok.connect(self._connected)
        worker.failed.connect(self._connection_failed)
        self._start_worker(worker, "Verbindung zu SAP wird aufgebaut ...")

    def _connected(self, status) -> None:
        self._update_mode_badges()
        sessions = self.gateway.get_sessions()
        message = f"Verbunden: {status.label}"
        if len(sessions) > 1:
            message += f"   ({len(sessions)} Sessions offen – ggf. Session waehlen)"
        self.counter_label.setText(message)
        if status.blocking_reasons:
            QMessageBox.warning(
                self, "Schreiben noch gesperrt",
                "Die Verbindung steht, das Schreiben ist aber noch gesperrt:\n\n• "
                + "\n• ".join(status.blocking_reasons)
                + "\n\nBitte pruefen Sie die SAP-Feld-IDs auf der gleichnamigen Seite.\n"
                  "Lesen und Dry Run sind bereits moeglich.")

    def _connection_failed(self, message: str, detail: str) -> None:
        self._update_mode_badges()
        show_error(self, "SAP-Verbindung fehlgeschlagen", message, detail)

    def choose_session(self) -> None:
        if self.settings.use_mock_sap:
            QMessageBox.information(self, "Testsystem aktiv",
                                    "Im Testsystem gibt es keine SAP-Sessions.")
            return
        sessions = self.gateway.get_sessions()
        if not sessions:
            show_error(self, "Keine Sessions",
                       "Es wurde keine offene SAP-Session gefunden. Bitte zuerst "
                       "in SAP anmelden und dann erneut verbinden.")
            return
        dialog = SessionDialog(sessions, self)
        if dialog.exec() != SessionDialog.DialogCode.Accepted:
            return
        selection = dialog.selection()
        if selection is None:
            return
        try:
            status = self.gateway.select_session(*selection)
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Session konnte nicht gewechselt werden", str(exc))
            return
        self._update_mode_badges()
        self.counter_label.setText(f"Session gewechselt: {status.label}")

    def load_sap_data(self) -> None:
        if self.offer is None or not self.offer.positions:
            return
        if not self.gateway.is_connected():
            show_error(self, "Keine SAP-Verbindung",
                       "Bitte zuerst „SAP verbinden“ waehlen.")
            return
        positions = [p for p in self.offer.positions if p.selected] or self.offer.positions
        worker = SapLoadWorker(self.gateway, positions, self.comparison,
                               self.validation, self.offer)
        worker.progress.connect(self._on_progress)
        worker.position_loaded.connect(self.table_model.refresh_row)
        worker.finished_ok.connect(self._sap_loaded)
        worker.failed.connect(lambda m, d: show_error(self, "SAP-Abgleich", m, d))
        self._start_worker(worker, "SAP-Daten werden gelesen ...")

    def _sap_loaded(self, loaded: int, skipped: int) -> None:
        self._revalidate()
        self.table_model.refresh_all()
        self.details.refresh()
        self._update_counters()
        message = f"SAP-Abgleich abgeschlossen: {loaded} Position(en) gelesen"
        if skipped:
            message += f", {skipped} uebersprungen (Material oder Lieferant fehlt)"
        self.counter_label.setText(message)

    def _reload_single_position(self, position: OfferPosition) -> None:
        if not self.gateway.is_connected():
            show_error(self, "Keine SAP-Verbindung", "Bitte zuerst „SAP verbinden“ waehlen.")
            return
        worker = SapLoadWorker(self.gateway, [position], self.comparison,
                               self.validation, self.offer)
        worker.finished_ok.connect(lambda *_: (self.table_model.refresh_row(position.uid),
                                               self.details.refresh()))
        self._start_worker(worker, f"SAP-Daten fuer {position.display_name} ...")

    # ==================================================================
    # Verarbeitung
    # ==================================================================
    def configure_chain(self) -> None:
        if self.offer is None:
            return
        selected = [p for p in self.offer.positions if p.selected]
        if not selected:
            show_error(self, "Keine Auswahl",
                       "Bitte waehlen Sie zuerst die Positionen aus, fuer die der "
                       "Komplettvorgang gelten soll.")
            return
        dialog = ChainDialog(self.settings, len(selected), self)
        if dialog.exec() != ChainDialog.DialogCode.Accepted:
            return
        self._snapshot("Komplettvorgang festgelegt")
        count = dialog.apply_to(selected)
        self._revalidate()
        self.table_model.refresh_all()
        self.details.refresh()
        self._update_counters()
        self.counter_label.setText(f"Komplettvorgang fuer {count} Position(en) festgelegt")

    def process_offer(self) -> None:
        if self.offer is None or self.preview_service is None or self.batch_factory is None:
            show_error(self, "Verarbeitung nicht verfuegbar",
                       "Die Verarbeitungskomponente konnte nicht geladen werden.")
            return

        self._revalidate()
        try:
            preview = self.preview_service.build(self.offer, self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Vorschau konnte nicht erstellt werden")
            show_error(self, "Vorschau fehlgeschlagen",
                       "Die Vorschau konnte nicht erstellt werden.", str(exc))
            return

        if getattr(preview, "positions_selected", 0) == 0:
            show_error(self, "Nichts zu tun",
                       "Es ist keine Position ausgewaehlt, fuer die in SAP etwas zu "
                       "tun waere.")
            return

        if not self.settings.dry_run and not self.settings.use_mock_sap:
            reasons = self.gateway.write_blocking_reasons()
            if reasons:
                show_error(self, "Schreiben gesperrt",
                           "In SAP kann noch nicht geschrieben werden:\n\n• "
                           + "\n• ".join(reasons)
                           + "\n\nPruefen Sie die SAP-Feld-IDs oder arbeiten Sie im "
                             "Dry Run bzw. im Testsystem.")
                return

        dialog = PreviewDialog(preview, self.settings.dry_run, self.settings.use_mock_sap, self)
        if dialog.exec() != PreviewDialog.DialogCode.Accepted:
            return

        self._learn_from_corrections()

        try:
            processor = self.batch_factory()
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Verarbeitung nicht moeglich", str(exc))
            return

        worker = BatchWorker(processor, self.offer, preview)
        worker.progress.connect(self._on_batch_progress)
        worker.finished_ok.connect(self._batch_finished)
        worker.failed.connect(lambda m, d: show_error(self, "Verarbeitung abgebrochen", m, d))
        self._start_worker(worker, "Verarbeitung laeuft ...")

    def _on_batch_progress(self, event) -> None:
        total = getattr(event, "total", 0) or 1
        index = getattr(event, "index", 0)
        self.progress.setMaximum(total)
        self.progress.setValue(index)
        label = getattr(event, "label", "")
        phase = getattr(event, "phase", "")
        phase_labels = {"info_record": "Infosatz", "contract": "Mengenkontrakt",
                        "source_list": "Orderbuch", "purchase_order": "Bestellung"}
        self.progress_label.setText(f"{phase_labels.get(phase, phase)}  {label}")

        uid = getattr(event, "position_uid", 0)
        if uid:
            self.table_model.refresh_row(uid)

    def _batch_finished(self, summary) -> None:
        self.table_model.refresh_all()
        self.details.refresh()
        self._update_counters()

        if self.repository is not None and self.offer is not None:
            try:
                run_id = self.repository.start_run(
                    offer=self.offer,
                    dry_run=self.settings.dry_run,
                    mock=self.settings.use_mock_sap,
                    source_file=self.offer.source_label,
                )
                written = self.repository.log_batch(
                    run_id, self.offer, summary,
                    sap_user=self.gateway.sap_user(),
                    sap_system=self.gateway.sap_system(),
                    mock=self.settings.use_mock_sap,
                )
                logger.info("Historie geschrieben: %s Zeile(n), Lauf %s", written, run_id)
            except Exception as exc:  # noqa: BLE001 - Protokoll darf nie das Ergebnis kippen
                logger.exception("Historie konnte nicht geschrieben werden: %s", exc)
            self.history_view.reload()

        if self.undo is not None:
            self.undo.clear()

        ResultDialog(summary, self).exec()

    def cancel_worker(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.counter_label.setText("Abbruch angefordert – laufender Schritt wird "
                                       "noch beendet ...")

    # ==================================================================
    # Lieferantenzuordnung
    # ==================================================================
    def assign_vendor(self, position: OfferPosition | None) -> None:
        if self.offer is None:
            return
        name = self.offer.vendor_name
        domain = self.offer.email.sender_domain if self.offer.email else ""
        candidates: list = []
        try:
            candidates = self.gateway.vendors.search_by_name(name) if name else []
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lieferantensuche nicht verfuegbar: %s", exc)
        if not candidates and self.mapping_store is not None:
            try:
                resolution = self.mapping_store.resolve_vendor(name, domain)
                candidates = list(getattr(resolution, "candidates", []) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Kandidatensuche fehlgeschlagen: %s", exc)

        current = position.vendor_number if position else self.offer.vendor_number
        dialog = VendorAssignmentDialog(name, domain, candidates, current, self)
        if dialog.exec() != VendorAssignmentDialog.DialogCode.Accepted:
            return
        number = dialog.vendor_number
        if not number:
            return

        self._snapshot("Lieferant zugeordnet")
        if dialog.apply_to_all or position is None:
            self.offer.set_field("vendor_number", number, FieldOrigin.MANUAL)
            for item in self.offer.positions:
                item.vendor_number = number
        else:
            position.vendor_number = number

        if dialog.remember and self.mapping_store is not None and (name or domain):
            try:
                self.mapping_store.remember_vendor(
                    vendor_name=name, vendor_number=number, email_domain=domain,
                    created_by="GUI")
                self.mapping_view.reload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Zuordnung konnte nicht gespeichert werden: %s", exc)

        self._revalidate()
        self.table_model.refresh_all()
        self._fill_header()
        self.details.refresh()
        self._update_counters()

    # ==================================================================
    # Bearbeitung
    # ==================================================================
    def _header_edited(self, key: str) -> None:
        if self.offer is None:
            return
        edit = self.header_fields[key]
        text = edit.text().strip()
        self._snapshot("Angebotskopf geaendert")
        if key == "offer_date":
            from ..utils.parsing import parse_date

            value = parse_date(text) if text else None
            if text and value is None:
                edit.setText(format_date(self.offer.offer_date))
                return
            self.offer.set_field("offer_date", value, FieldOrigin.MANUAL)
        elif key == "currency":
            self.offer.set_field("currency", text.upper()[:5], FieldOrigin.MANUAL)
            for position in self.offer.positions:
                if not position.currency:
                    position.set_field("currency", self.offer.currency, FieldOrigin.MANUAL)
        else:
            self.offer.set_field(key, text, FieldOrigin.MANUAL)
        self._revalidate()
        self.table_model.refresh_all()
        self._update_counters()

    def _position_edited(self, uid: int, field: str) -> None:
        self._revalidate()
        position = self._position_by_uid(uid)
        if position is not None and self.details.position is not None and \
                self.details.position.uid == uid:
            self.details.refresh()
        self._update_counters()

    def _position_changed(self, position: OfferPosition) -> None:
        self._snapshot("Position geaendert")
        self._revalidate()
        self.table_model.refresh_row(position.uid)
        self._update_counters()

    def _position_by_uid(self, uid: int) -> OfferPosition | None:
        if self.offer is None:
            return None
        for position in self.offer.positions:
            if position.uid == uid:
                return position
        return None

    def _current_row_changed(self, current, _previous) -> None:
        position = current.data(POSITION_ROLE) if current.isValid() else None
        self.details.set_position(position)

    def _show_details(self, position: OfferPosition) -> None:
        self.details.set_position(position)

    def _remove_positions(self, uids: list) -> None:
        if not uids:
            return
        if not ask_yes_no(self, "Positionen entfernen",
                          f"{len(uids)} Position(en) aus der Liste entfernen?",
                          "Das betrifft nur diese Sitzung – die Datei bleibt unveraendert."):
            return
        self.table_model.remove_positions(uids)
        self.details.set_position(None)
        self._update_counters()

    def _add_position(self) -> None:
        position = self.table_model.add_empty_position()
        if position is not None:
            self._revalidate()
            self._update_counters()

    def _fill_down(self, key: str) -> None:
        position = self.details.position or self.table.current_position()
        if position is None or self.offer is None:
            return
        value = getattr(position, key, "")
        text = "" if value is None else str(value)
        count = self.table_model.apply_value_to_selected(key, text)
        self._revalidate()
        self.counter_label.setText(f"Wert auf {count} Position(en) uebertragen")

    # -- Auswahl --------------------------------------------------------
    def _select_all(self, selected: bool) -> None:
        self.table_model.set_all_selected(selected)
        self._revalidate()
        self._update_counters()

    def _select_changed(self) -> None:
        count = self.table_model.select_where(lambda p: p.has_changes,
                                              "Nur geaenderte ausgewaehlt")
        self._revalidate()
        self.counter_label.setText(f"{count} geaenderte Position(en) ausgewaehlt")

    def _select_clean(self) -> None:
        count = self.table_model.select_where(
            lambda p: not p.issues.has_blocking, "Nur fehlerfreie ausgewaehlt")
        self._revalidate()
        self.counter_label.setText(f"{count} fehlerfreie Position(en) ausgewaehlt")

    def _status_filter_changed(self) -> None:
        status = self.status_filter.currentData()
        self.proxy.set_status_filter({status} if status else set())

    # -- Undo -----------------------------------------------------------
    def _snapshot(self, label: str) -> None:
        if self.undo is not None and self.offer is not None:
            try:
                self.undo.snapshot(self.offer, label)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Undo-Schnappschuss fehlgeschlagen: %s", exc)

    def _undo(self) -> None:
        if self.undo is None:
            return
        restored = self.undo.undo()
        if restored is None:
            self.counter_label.setText("Nichts rueckgaengig zu machen")
            return
        self.offer = restored
        self.table_model.set_offer(restored)
        self._fill_header()
        self._revalidate()
        self.details.set_position(None)
        self._update_counters()
        self.counter_label.setText("Aenderung rueckgaengig gemacht")

    def _redo(self) -> None:
        if self.undo is None:
            return
        restored = self.undo.redo()
        if restored is None:
            return
        self.offer = restored
        self.table_model.set_offer(restored)
        self._fill_header()
        self._revalidate()
        self._update_counters()

    # -- Lernen ---------------------------------------------------------
    def _learn_from_corrections(self) -> None:
        """Aus den Korrekturen des Anwenders das Lieferantenprofil verbessern."""
        if self.import_service is None or self.offer is None or \
                self._offer_before_edits is None:
            return
        try:
            profile = self.import_service.learn(self._offer_before_edits, self.offer)
        except Exception as exc:  # noqa: BLE001 - Lernen darf nie die Verarbeitung stoppen
            logger.warning("Lernen aus Korrekturen fehlgeschlagen: %s", exc)
            return
        if profile is not None:
            logger.info("Angebotsformat gelernt/aktualisiert: %s",
                        getattr(profile, "vendor_name", ""))
            self.mapping_view.reload()

    # ==================================================================
    # Anzeige
    # ==================================================================
    def _fill_header(self) -> None:
        offer = self.offer
        if offer is None:
            for edit in self.header_fields.values():
                edit.clear()
            self.source_label.setText("Kein Angebot geladen")
            self.vendor_number_label.setText("– kein SAP-Lieferant zugeordnet –")
            return
        values = {
            "vendor_name": offer.vendor_name,
            "offer_number": offer.offer_number,
            "offer_date": format_date(offer.offer_date),
            "currency": offer.currency,
            "payment_terms": offer.payment_terms,
            "incoterm": " ".join(x for x in (offer.incoterm, offer.incoterm_location) if x),
        }
        for key, edit in self.header_fields.items():
            edit.setText(values.get(key, ""))
            origin = offer.field_origins.get(key)
            if origin is FieldOrigin.UNCERTAIN:
                edit.setStyleSheet(f"QLineEdit {{ background: {Colors.AMBER_BG}; }}")
                edit.setToolTip("Unsicher erkannt – bitte pruefen")
            elif origin is FieldOrigin.MISSING or not values.get(key):
                edit.setStyleSheet("")
                edit.setToolTip("Nicht erkannt – bitte ergaenzen")
            else:
                edit.setStyleSheet("")
                edit.setToolTip(origin.label if origin else "")

        if offer.vendor_number:
            self.vendor_number_label.setText(f"SAP-Lieferant: {offer.vendor_number}")
            self.vendor_number_label.setStyleSheet(f"color: {Colors.GREEN}; font-weight: 600;")
        else:
            self.vendor_number_label.setText("– kein SAP-Lieferant zugeordnet –")
            self.vendor_number_label.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")

        source = offer.source_label
        if offer.email:
            source += f"   •   von {offer.email.from_address or offer.email.from_name}"
        self.source_label.setText(source)

    def _revalidate(self) -> None:
        if self.offer is None:
            return
        try:
            if self.comparison is not None:
                self.comparison.compare_offer(self.offer)
            if self.validation is not None:
                self.validation.validate_offer(self.offer)
        except Exception as exc:  # noqa: BLE001 - Pruefung darf die GUI nie stoppen
            logger.exception("Pruefung fehlgeschlagen: %s", exc)
        self.table_model.refresh_all()

    def _update_counters(self) -> None:
        if self.offer is None:
            self.counter_label.setText("Kein Angebot geladen")
            self._update_actions()
            return
        counts = self.table_model.counts()
        self.counter_label.setText(
            f"{counts['gesamt']} Positionen   •   {counts['ausgewaehlt']} ausgewaehlt   •   "
            f"{counts['geaendert']} mit Aenderung   •   {counts['pruefen']} zu pruefen   •   "
            f"{counts['fehler']} mit Fehler"
            + (f"   •   {counts['verarbeitet']} verarbeitet" if counts["verarbeitet"] else ""))
        self._update_actions()

    def _update_actions(self) -> None:
        has_offer = self.offer is not None and bool(self.offer.positions)
        running = self._worker is not None and self._worker.isRunning()
        self.action_load.setEnabled(has_offer and not running)
        self.action_process.setEnabled(has_offer and not running)
        self.action_chain.setEnabled(has_offer and not running)
        self.action_open.setEnabled(not running)
        self.action_paste.setEnabled(not running)
        self.action_cancel.setEnabled(running)

    def _update_mode_badges(self) -> None:
        if self.settings.use_mock_sap:
            self.mode_badge.setText("Testsystem (Mock-SAP)")
            self.mode_badge.setStyleSheet(badge_style(Colors.BLUE, Colors.BLUE_BG))
        elif self.settings.dry_run:
            self.mode_badge.setText("Dry Run – nur lesen")
            self.mode_badge.setStyleSheet(badge_style(Colors.AMBER, Colors.AMBER_BG))
        else:
            self.mode_badge.setText("ECHTBETRIEB – schreibend")
            self.mode_badge.setStyleSheet(badge_style(Colors.RED, Colors.RED_BG))

        status = self.gateway.status()
        colours = {"gruen": (Colors.GREEN, Colors.GREEN_BG),
                   "gelb": (Colors.AMBER, Colors.AMBER_BG),
                   "rot": (Colors.RED, Colors.RED_BG),
                   "blau": (Colors.BLUE, Colors.BLUE_BG),
                   "grau": (Colors.GREY, Colors.GREY_BG)}
        colour, background = colours.get(status.color, (Colors.GREY, Colors.GREY_BG))
        self.connection_badge.setText(status.short())
        self.connection_badge.setStyleSheet(badge_style(colour, background))
        self.connection_badge.setToolTip(status.detail)
        self.dry_run_box.setChecked(self.settings.dry_run)

    def _dry_run_toggled(self, checked: bool) -> None:
        if not checked and not self.settings.use_mock_sap:
            if not ask_yes_no(self, "Dry Run abschalten",
                              "Dry Run abschalten und tatsaechlich in SAP schreiben?",
                              "Die Anwendung legt dann Infosaetze, Orderbucheintraege, "
                              "Kontrakte und Bestellungen wirklich an."):
                self.dry_run_box.setChecked(True)
                return
        self.settings.dry_run = checked
        self.settings_view.load()
        self._update_mode_badges()

    def _mode_changed(self) -> None:
        self.gateway.set_mode(self.settings.use_mock_sap)
        self._update_mode_badges()
        self._update_actions()

    def _reset_mock(self) -> None:
        if self.gateway.reset_mock_data():
            QMessageBox.information(self, "Testdaten zurueckgesetzt",
                                    "Der Testbestand wurde auf den Auslieferungszustand "
                                    "zurueckgesetzt.")
        else:
            show_error(self, "Nicht moeglich",
                       "Testdaten koennen nur im Testsystem zurueckgesetzt werden.")

    def _show_startup_problems(self) -> None:
        if not self.startup_problems:
            return
        QMessageBox.warning(
            self, "Eingeschraenkte Funktion",
            "Beim Start konnten nicht alle Komponenten geladen werden:\n\n• "
            + "\n• ".join(self.startup_problems)
            + "\n\nDie uebrigen Funktionen stehen normal zur Verfuegung.")

    # ==================================================================
    # Worker-Verwaltung
    # ==================================================================
    def _start_worker(self, worker, message: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            show_error(self, "Bitte warten",
                       "Es laeuft bereits ein Vorgang. Bitte warten Sie, bis dieser "
                       "abgeschlossen ist.")
            return
        self._worker = worker
        worker.message.connect(self.counter_label.setText)
        worker.finished.connect(self._worker_finished)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText(message)
        self.counter_label.setText(message)
        self._update_actions()
        worker.start()

    def _on_progress(self, current: int, total: int, label: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.progress_label.setText(f"{current}/{total}   {label}")

    def _worker_finished(self) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self.progress_label.setText("")
        self._worker = None
        self._update_actions()
        self._update_mode_badges()

    # ==================================================================
    # Drag & Drop
    # ==================================================================
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        paths = [p for p in paths if Path(p).is_file()]
        if paths:
            self.open_offers(paths)
            event.acceptProposedAction()

    # ==================================================================
    def _confirm_discard(self) -> bool:
        if self.offer is None or not self.offer.positions:
            return True
        processed = any(p.status is PositionStatus.DONE for p in self.offer.positions)
        if processed:
            return ask_yes_no(self, "Angebot schliessen",
                              "Das aktuelle Angebot wurde bereits teilweise verarbeitet.",
                              "Ein neues Angebot laden und das aktuelle schliessen?")
        return ask_yes_no(self, "Angebot schliessen",
                          "Aktuelles Angebot schliessen und neues laden?",
                          "Nicht uebernommene Aenderungen gehen verloren.")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            if not ask_yes_no(self, "Beenden",
                              "Es laeuft noch ein Vorgang. Trotzdem beenden?",
                              "In SAP koennte ein Vorgang unvollstaendig bleiben."):
                event.ignore()
                return
            self._worker.cancel()
            self._worker.wait(3000)
        try:
            self.settings.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Einstellungen konnten nicht gespeichert werden: %s", exc)
        if self.repository is not None:
            try:
                self.repository.close()
            except Exception:  # noqa: BLE001
                pass
        event.accept()


def apply_application_style(app: QApplication, settings: Settings) -> None:
    """Stylesheet auf die Anwendung anwenden."""
    app.setStyleSheet(build_stylesheet(settings.ui.font_size))
