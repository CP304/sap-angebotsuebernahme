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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidgetAction,
    QWidget,
)

from ..config.settings import Settings
from ..models.enums import FieldOrigin, PositionStatus
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..sap.gateway import SapGateway
from ..services.queue_service import OfferQueue
from ..services.vendor_master_service import VendorMasterService
from ..utils.logging_setup import GuiLogHandler
from ..utils.parsing import format_date
from .dialogs import (
    ChainDialog,
    PasteTextDialog,
    PreviewDialog,
    ResultDialog,
    SessionDialog,
    VendorAssignmentDialog,
    VendorCreateDialog,
    ask_yes_no,
    show_error,
)
from .admin_window import AdminWindow
from .history_view import HistoryView
from .mapping_view import MappingView
from .offer_table import POSITION_ROLE, OfferFilterProxy, OfferTableModel, OfferTableView
from .quick_entry import QuickEntryBar
from .position_details import PositionDetails
from .queue_bar import QueueBar
from .selector_view import SelectorView
from .settings_view import SettingsView
from .table_import_dialog import TableImportDialog
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
        self.queue = OfferQueue()
        self._queue_index_loading: int = -1
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
        self.setCentralWidget(self._build_offer_page())
        self._build_admin_pages()
        self._build_menu()
        self._build_toolbar()
        self._fill_menu()
        self._build_statusbar()
        self._build_shortcuts()

    def _build_admin_pages(self) -> None:
        """Verwaltungsseiten anlegen -- sie leben in einem eigenen Fenster."""
        self.history_view = HistoryView(self.repository)
        self.mapping_view = MappingView(self.repository)
        self.selector_view = SelectorView(self.gateway.selectors, self.settings)
        self.selector_view.changed.connect(self._update_mode_badges)
        self.settings_view = SettingsView(self.settings)
        self.settings_view.modeChanged.connect(self._mode_changed)
        self.settings_view.settingsSaved.connect(self._mode_changed)
        self.settings_view.resetMockRequested.connect(self._reset_mock)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)

        from .diagnosis_view import DiagnosisView
        self.diagnosis_view = DiagnosisView(self.settings)

        from .vbs_importer import VbsImporterWidget
        self.vbs_importer = VbsImporterWidget(self.settings)

        self._admin_pages = [
            ("Historie", self.history_view),
            ("Zuordnungen", self.mapping_view),
            ("SAP Feld-ID Zuordnung", self.vbs_importer),
            ("Einstellungen", self.settings_view),
            ("Diagnose", self.diagnosis_view),
            ("Protokoll", self.log_view),
        ]
        self._admin_window: AdminWindow | None = None

    def open_admin(self, title: str) -> None:
        """Verwaltungsfenster oeffnen (wird erst bei Bedarf erzeugt)."""
        if self._admin_window is None:
            self._admin_window = AdminWindow(self._admin_pages, self)
        self._admin_window.show_page(title)

    def _build_menu(self) -> None:
        """Menueleiste anlegen.  Befuellt wird sie in ``_fill_menu``, sobald die
        Aktionen existieren -- so steht jede Aktion nur einmal im Code."""
        menu = self.menuBar()
        self._menu_datei = menu.addMenu("&Datei")
        self._menu_sap = menu.addMenu("&SAP")
        self._menu_ansicht = menu.addMenu("&Ansicht")
        self._menu_verwaltung = menu.addMenu("&Verwaltung")
        self._menu_hilfe = menu.addMenu("&Hilfe")

    def _fill_menu(self) -> None:
        """Menue befuellen, nachdem die Aktionen existieren."""
        datei = self._menu_datei
        datei.addAction(self.action_open)
        datei.addAction(self.action_paste)
        datei.addAction(self.action_table)
        datei.addAction(self.action_teach)
        datei.addSeparator()
        beenden = QAction("Beenden", self)
        beenden.setShortcut(QKeySequence.StandardKey.Quit)
        beenden.triggered.connect(self.close)
        datei.addAction(beenden)

        sap = self._menu_sap
        sap.addAction(self.action_connect)
        sap.addAction(self.action_session)
        sap.addSeparator()
        sap.addAction(self.action_load)
        sap.addAction(self.action_process)
        sap.addAction(self.action_cancel)

        ansicht = self._menu_ansicht
        self.action_detailed_columns = QAction("Alle Spalten anzeigen", self)
        self.action_detailed_columns.setCheckable(True)
        self.action_detailed_columns.setShortcut(QKeySequence("Strg+Umschalt+S"))
        self.action_detailed_columns.toggled.connect(self._toggle_detailed_columns)
        ansicht.addAction(self.action_detailed_columns)

        for titel, _widget in self._admin_pages:
            aktion = QAction(f"{titel} ...", self)
            aktion.triggered.connect(lambda _=False, t=titel: self.open_admin(t))
            self._menu_verwaltung.addAction(aktion)

        hilfe = self._menu_hilfe
        kurz = QAction("Tastenkuerzel", self)
        kurz.triggered.connect(self._show_shortcuts)
        hilfe.addAction(kurz)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self, "Tastenkuerzel",
            "Strg+O\tAngebot oeffnen\n"
            "F5\tSAP-Daten laden\n"
            "F9\tUebernehmen\n"
            "Leertaste\tPosition an-/abwaehlen\n"
            "Eingabe\tDetails der Position\n"
            "Strg+C / Strg+V\tKopieren / Einfuegen\n"
            "Strg+Z / Strg+Y\tRueckgaengig / Wiederherstellen\n"
            "Strg+Umschalt+S\tAlle Spalten anzeigen")

    def _toggle_detailed_columns(self, checked: bool) -> None:
        self.table.set_detailed_columns(checked)

    def _build_toolbar(self) -> None:
        """Werkzeugleiste als Ablauf in drei Schritten.

        Der Alltag besteht aus genau drei Handgriffen: Angebot laden, SAP-Daten
        holen, uebernehmen.  Diese drei stehen gross da; alles Weitere haengt
        als Untermenue an dem Schritt, zu dem es gehoert, oder liegt im Menue.
        """
        bar = QToolBar("Ablauf")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(bar)

        self.action_open = QAction("Angebot oeffnen", self)
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.setToolTip("PDF, Excel, E-Mail (.msg/.eml) oder CSV oeffnen (Strg+O)")
        self.action_open.triggered.connect(self.open_offer_dialog)

        # Nebenwege zum Laden -- haengen als Untermenue am ersten Schritt
        self.action_paste = QAction("Text einfuegen ...", self)
        self.action_paste.setToolTip("Angebotstext aus einer E-Mail einfuegen")
        self.action_paste.triggered.connect(self.paste_offer_text)

        self.action_table = QAction("Tabelle einfuegen ...", self)
        self.action_table.setToolTip(
            "Tabelle aus Excel einfuegen oder laden und die Spalten selbst zuordnen")
        self.action_table.triggered.connect(self.import_table_manually)

        self.action_teach = QAction("Erkennung anlernen ...", self)
        self.action_teach.setToolTip(
            "Im PDF grafisch markieren, wo Positionen und Spalten stehen")
        self.action_teach.triggered.connect(self.teach_recognition)

        self.action_connect = QAction("SAP verbinden", self)
        self.action_connect.triggered.connect(self.connect_sap)

        self.action_session = QAction("Session waehlen ...", self)
        self.action_session.triggered.connect(self.choose_session)

        self.action_load = QAction("SAP-Daten laden", self)
        self.action_load.setShortcut(QKeySequence("F5"))
        self.action_load.setToolTip("Ist-Zustand aus SAP lesen (F5) – es wird nichts geaendert")
        self.action_load.triggered.connect(self.load_sap_data)

        self.action_chain = QAction("Was soll passieren? ...", self)
        self.action_chain.setToolTip(
            "Infosatz, Mengenkontrakt, Orderbuch und Bestellung festlegen")
        self.action_chain.triggered.connect(self.configure_chain)

        self.action_process = QAction("Uebernehmen", self)
        self.action_process.setShortcut(QKeySequence("F9"))
        self.action_process.setToolTip("Ausgewaehlte Positionen in SAP verarbeiten (F9)")
        self.action_process.triggered.connect(self.process_offer)

        self.action_cancel = QAction("Abbrechen", self)
        self.action_cancel.triggered.connect(self.cancel_worker)
        self.action_cancel.setEnabled(False)

        # -- Schritt 1 ---------------------------------------------------
        self.step1_button = self._step_button(
            "1   Angebot laden", self.action_open,
            [self.action_paste, self.action_table, self.action_teach])
        bar.addWidget(self.step1_button)
        bar.addWidget(self._arrow())

        # -- Schritt 2 ---------------------------------------------------
        self.step2_button = self._step_button(
            "2   SAP-Daten laden", self.action_load,
            [self.action_connect, self.action_session])
        bar.addWidget(self.step2_button)
        bar.addWidget(self._arrow())

        # -- Schritt 3 ---------------------------------------------------
        self.step3_button = self._step_button(
            "3   Uebernehmen", self.action_process, [self.action_chain],
            primary=True)
        bar.addWidget(self.step3_button)

        bar.addAction(self.action_cancel)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        bar.addWidget(spacer)

        # Betriebsart und Verbindung: zwei Plaketten, kein Bedienelement
        self.mode_badge = QLabel("")
        self.mode_badge.setMinimumWidth(170)
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_badge.setToolTip("Betriebsart aendern: Verwaltung → Einstellungen")
        self.mode_badge.mousePressEvent = (            # noqa: SLF001 - bewusst
            lambda _event: self.open_admin("Einstellungen"))
        bar.addWidget(self.mode_badge)

        self.connection_badge = QLabel("")
        self.connection_badge.setMinimumWidth(200)
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.connection_badge)

        # Dry Run bleibt erreichbar, aber unauffaellig
        self.dry_run_box = QCheckBox("Dry Run")
        self.dry_run_box.setToolTip("SAP nur lesen, nichts schreiben")
        self.dry_run_box.setChecked(self.settings.dry_run)
        self.dry_run_box.toggled.connect(self._dry_run_toggled)
        bar.addWidget(self.dry_run_box)

    def _step_button(self, text: str, main_action: QAction,
                     extras: list[QAction], primary: bool = False) -> QToolButton:
        """Ein Ablaufschritt: grosse Schaltflaeche mit Zusatzmenue."""
        button = QToolButton()
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setDefaultAction(main_action)
        button.setMinimumHeight(34)
        button.setMinimumWidth(170)
        if primary:
            button.setObjectName("Primary")
            button.setStyleSheet(
                f"QToolButton {{ background: {Colors.ACCENT}; color: white; "
                f"font-weight: 600; border-radius: 4px; padding: 6px 14px; }}"
                f"QToolButton:disabled {{ background: {Colors.GREY}; color: #eeeeee; }}")
        if extras:
            menu = QMenu(button)
            for aktion in extras:
                menu.addAction(aktion)
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # Beschriftung darf nicht von der Aktion ueberschrieben werden
        button.setText(text)
        main_action.changed.connect(lambda b=button, t=text: b.setText(t))
        return button

    @staticmethod
    def _arrow() -> QLabel:
        pfeil = QLabel("→")
        pfeil.setStyleSheet(f"color: {Colors.GREY}; font-size: 15pt;")
        pfeil.setContentsMargins(6, 0, 6, 0)
        return pfeil

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

        self.queue_bar = QueueBar()
        self.queue_bar.entrySelected.connect(self._queue_entry_selected)
        self.queue_bar.skipRequested.connect(self._skip_current_queue_entry)
        self.queue_bar.nextRequested.connect(
            lambda: self._load_next_from_queue(force=False))
        self.queue_bar.clearRequested.connect(self._clear_queue)
        layout.addWidget(self.queue_bar)
        layout.addWidget(self._build_header_card())
        layout.addWidget(self._build_selection_bar())

        # Schnellerfassung: standardmaessig eingeklappt, damit sie keinen
        # Platz kostet, wenn die Erkennung ohnehin gegriffen hat.
        self.quick_entry = QuickEntryBar()
        self.quick_entry.positionEntered.connect(self._quick_position_entered)
        # Ueber eine Methode statt direkt auf das Label: die Statuszeile
        # entsteht weiter unten erst nach dieser Leiste.
        self.quick_entry.message.connect(self._quick_message)
        self.quick_entry.setVisible(False)
        layout.addWidget(self.quick_entry)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = OfferTableView()
        self.table.setModel(self.proxy)
        self.table.apply_column_widths()
        self.table.requestDetails.connect(self._show_details)
        self.table.requestVendorAssignment.connect(self.assign_vendor)
        self.table.requestRemove.connect(self._remove_positions)
        self.table.requestFillDown.connect(self._fill_down)
        splitter.addWidget(self.table)

        self.details = PositionDetails(self.comparison, settings=self.settings)
        self.details.positionChanged.connect(self._position_changed)
        self.details.requestVendorAssignment.connect(self.assign_vendor)
        self.details.requestVendorMaster.connect(self._maintain_vendor_master)
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
        """Angebotskopf in einer Zeile.

        Bewusst knapp: Der Kopf ist Kontext, nicht Arbeitsflaeche.  Wer etwas
        aendern muss, klappt ihn auf -- das ist der seltene Fall.
        """
        card = QFrame()
        card.setObjectName("Card")
        aussen = QVBoxLayout(card)
        aussen.setContentsMargins(12, 8, 12, 8)
        aussen.setSpacing(6)

        zeile = QHBoxLayout()
        zeile.setSpacing(14)

        self.offer_title = QLabel("Kein Angebot geladen")
        self.offer_title.setObjectName("Heading")
        zeile.addWidget(self.offer_title)

        self.offer_meta = QLabel("")
        self.offer_meta.setObjectName("SubHeading")
        zeile.addWidget(self.offer_meta)
        zeile.addStretch(1)

        self.vendor_number_label = QLabel("kein SAP-Lieferant")
        zeile.addWidget(self.vendor_number_label)

        assign_button = QPushButton("Lieferant zuordnen ...")
        assign_button.clicked.connect(lambda: self.assign_vendor(None))
        zeile.addWidget(assign_button)

        self.header_toggle = QPushButton("Kopfdaten")
        self.header_toggle.setCheckable(True)
        self.header_toggle.setToolTip("Angebotskopf anzeigen und bearbeiten")
        self.header_toggle.toggled.connect(self._toggle_header)
        zeile.addWidget(self.header_toggle)
        aussen.addLayout(zeile)

        # -- ausklappbarer Bereich mit den Kopffeldern -------------------
        self.header_panel = QWidget()
        grid = QGridLayout(self.header_panel)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)

        self.header_fields: dict[str, QLineEdit] = {}
        definitions = (
            ("vendor_name", "Lieferant", 0, 0),
            ("offer_number", "Angebotsnummer", 0, 2),
            ("offer_date", "Angebotsdatum", 0, 4),
            ("payment_terms", "Zahlungsbedingungen", 1, 0),
            ("incoterm", "Incoterm", 1, 2),
            ("currency", "Waehrung", 1, 4),
        )
        for key, label, row, column in definitions:
            caption = QLabel(label + ":")
            caption.setObjectName("FieldLabel")
            edit = QLineEdit()
            edit.editingFinished.connect(lambda k=key: self._header_edited(k))
            self.header_fields[key] = edit
            grid.addWidget(caption, row, column)
            grid.addWidget(edit, row, column + 1)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(5, 2)

        self.header_panel.setVisible(False)
        aussen.addWidget(self.header_panel)

        # Erkennungshinweise: eine Zeile, Rest im Sprechblasentext
        self.extraction_label = QLabel("")
        self.extraction_label.setObjectName("SubHeading")
        self.extraction_label.setVisible(False)
        aussen.addWidget(self.extraction_label)
        return card

    def _toggle_header(self, checked: bool) -> None:
        self.header_panel.setVisible(checked)

    def _build_selection_bar(self) -> QWidget:
        """Suche links, Auswahl und Filter als je ein Menue rechts."""
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen ...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(320)
        self.search_edit.textChanged.connect(self.proxy.set_search)
        layout.addWidget(self.search_edit)

        # -- Auswahl -----------------------------------------------------
        auswahl_button = QToolButton()
        auswahl_button.setText("Auswahl")
        auswahl_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        auswahl_menu = QMenu(auswahl_button)
        auswahl_menu.addAction("Alle auswaehlen", lambda: self._select_all(True))
        auswahl_menu.addAction("Auswahl aufheben", lambda: self._select_all(False))
        auswahl_menu.addSeparator()
        auswahl_menu.addAction("Nur geaenderte", self._select_changed)
        auswahl_menu.addAction("Nur fehlerfreie", self._select_clean)
        auswahl_menu.addSeparator()
        auswahl_menu.addAction("Position ergaenzen", self._add_position)
        self.quick_entry_action = auswahl_menu.addAction(
            "Schnellerfassung", lambda: self.toggle_quick_entry())
        self.quick_entry_action.setCheckable(True)
        self.quick_entry_action.setShortcut("Ctrl+E")
        self.quick_entry_action.setToolTip(
            "Eine Zeile tippen, Enter -- ohne die Maus anzufassen (Strg+E)")
        auswahl_button.setMenu(auswahl_menu)
        layout.addWidget(auswahl_button)

        # -- Filter ------------------------------------------------------
        self.filter_button = QToolButton()
        self.filter_button.setText("Filter")
        self.filter_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        filter_menu = QMenu(self.filter_button)

        self.status_filter = QComboBox()
        self.status_filter.addItem("Alle Status", None)
        for status in (PositionStatus.READY, PositionStatus.CHECK, PositionStatus.ERROR,
                       PositionStatus.DONE, PositionStatus.SKIPPED):
            self.status_filter.addItem(status.label, status)
        self.status_filter.currentIndexChanged.connect(self._status_filter_changed)
        status_aktion = QWidgetAction(filter_menu)
        status_aktion.setDefaultWidget(self.status_filter)
        filter_menu.addAction(status_aktion)

        self.only_special_action = QAction("Nur Sonderpositionen", self)
        self.only_special_action.setCheckable(True)
        self.only_special_action.setToolTip(
            "Einmalkosten, Alternativpositionen und Zwischensummen -- also "
            "genau die Zeilen, ueber die Sie entscheiden muessen")
        self.only_special_action.toggled.connect(self.proxy.set_only_special)
        filter_menu.addAction(self.only_special_action)

        self.only_changed_action = QAction("Nur mit Aenderung", self)
        self.only_changed_action.setCheckable(True)
        self.only_changed_action.toggled.connect(self.proxy.set_only_changed)
        self.only_changed_action.toggled.connect(self._update_filter_label)
        filter_menu.addAction(self.only_changed_action)

        self.only_selected_action = QAction("Nur ausgewaehlte", self)
        self.only_selected_action.setCheckable(True)
        self.only_selected_action.toggled.connect(self.proxy.set_only_selected)
        self.only_selected_action.toggled.connect(self._update_filter_label)
        filter_menu.addAction(self.only_selected_action)

        filter_menu.addSeparator()
        filter_menu.addAction("Filter zuruecksetzen", self._reset_filters)
        self.filter_button.setMenu(filter_menu)
        layout.addWidget(self.filter_button)

        layout.addStretch(1)
        self.counter_inline = QLabel("")
        self.counter_inline.setObjectName("SubHeading")
        layout.addWidget(self.counter_inline)
        return bar

    def _update_filter_label(self) -> None:
        aktiv = sum(1 for aktion in (self.only_changed_action, self.only_selected_action)
                    if aktion.isChecked())
        if self.status_filter.currentData() is not None:
            aktiv += 1
        self.filter_button.setText(f"Filter ({aktiv})" if aktiv else "Filter")

    def _reset_filters(self) -> None:
        self.only_changed_action.setChecked(False)
        self.only_selected_action.setChecked(False)
        self.status_filter.setCurrentIndex(0)
        self.search_edit.clear()
        self._update_filter_label()

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
        """Angebote oeffnen.

        Bei mehreren Dateien ist die entscheidende Frage, ob es sich um *ein*
        Angebot handelt (Anschreiben plus Preisliste) oder um mehrere
        eigenstaendige, die nacheinander abzuarbeiten sind.  Das kann die
        Anwendung nicht wissen -- also fragt sie, statt zu raten.
        """
        if self.import_service is None:
            show_error(self, "Import nicht verfuegbar",
                       "Die Erkennungskomponente konnte nicht geladen werden.")
            return

        if len(paths) > 1 and self.settings.ui.ask_batch_or_merge:
            als_stapel = self._ask_batch_or_merge(paths)
            if als_stapel is None:
                return
            if als_stapel:
                self.queue.add_paths(paths)
                self.queue_bar.bind(self.queue)
                self._load_next_from_queue(force=True)
                return

        if not self._confirm_discard():
            return
        self._queue_index_loading = -1
        worker = ImportWorker(self.import_service, paths=paths)
        worker.finished_ok.connect(self._offer_loaded)
        worker.failed.connect(lambda m, d: show_error(self, "Import fehlgeschlagen", m, d))
        self._start_worker(worker, f"{len(paths)} Datei(en) werden gelesen ...")

    def _ask_batch_or_merge(self, paths: list[str]) -> bool | None:
        """True = nacheinander, False = ein gemeinsames Angebot, None = Abbruch."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Mehrere Dateien")
        box.setText(f"{len(paths)} Dateien ausgewaehlt.")
        box.setInformativeText(
            "Gehoeren sie zu einem gemeinsamen Angebot (z. B. Anschreiben und "
            "Preisliste), oder sind es mehrere eigenstaendige Angebote?")
        box.setDetailedText("\n".join(Path(p).name for p in paths))
        stapel = box.addButton("Nacheinander abarbeiten",
                               QMessageBox.ButtonRole.AcceptRole)
        gemeinsam = box.addButton("Ein gemeinsames Angebot",
                                  QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stapel)
        box.exec()

        geklickt = box.clickedButton()
        if geklickt is stapel:
            return True
        if geklickt is gemeinsam:
            return False
        return None

    # ------------------------------------------------------------------
    # Arbeitsvorrat: mehrere Angebote nacheinander
    # ------------------------------------------------------------------
    def _load_next_from_queue(self, force: bool = False) -> None:
        index = self.queue.next_pending_index()
        if index < 0:
            self.queue_bar.refresh()
            self._show_queue_finished()
            return
        if not force and not self._confirm_discard():
            return
        self._open_queue_entry(index)

    def _open_queue_entry(self, index: int) -> None:
        eintrag = self.queue.select(index)
        if eintrag is None:
            return
        if eintrag.offer is not None:
            # Schon gelesen -- nur wieder anzeigen, nicht erneut einlesen
            self._queue_index_loading = -1
            self.offer = eintrag.offer
            self.table_model.set_offer(eintrag.offer)
            self.table.apply_column_widths()
            self._fill_header()
            self._revalidate()
            self._update_counters()
            self.queue_bar.refresh()
            return

        self._queue_index_loading = index
        worker = ImportWorker(self.import_service, paths=[eintrag.path])
        worker.finished_ok.connect(self._offer_loaded)
        worker.failed.connect(self._queue_import_failed)
        self._start_worker(worker, f"{eintrag.name} wird gelesen ...")

    def _queue_import_failed(self, message: str, detail: str) -> None:
        """Eine nicht lesbare Datei darf den Stapel nicht anhalten."""
        index = self._queue_index_loading
        if index < 0:
            show_error(self, "Import fehlgeschlagen", message, detail)
            return
        self.queue.mark_import_failed(index, message)
        self.queue_bar.refresh()
        self.counter_label.setText(f"{self.queue.entries[index].name}: {message}")
        logger.warning("Angebot im Arbeitsvorrat nicht lesbar: %s", message)
        if self.settings.ui.auto_advance_queue:
            self._load_next_from_queue(force=True)

    def _queue_entry_selected(self, index: int) -> None:
        if index == self.queue.current_index:
            return
        if not self._confirm_discard():
            self.queue_bar.refresh()
            return
        self._open_queue_entry(index)

    def _skip_current_queue_entry(self) -> None:
        if self.queue.current_index < 0:
            return
        name = self.queue.entries[self.queue.current_index].name
        self.queue.mark_skipped(self.queue.current_index,
                                "vom Anwender uebersprungen")
        self.queue_bar.refresh()
        self.counter_label.setText(f"{name} uebersprungen")
        self._load_next_from_queue(force=True)

    def _clear_queue(self) -> None:
        if self.queue.is_empty:
            return
        if not ask_yes_no(self, "Arbeitsvorrat leeren",
                          f"{self.queue.total} Angebot(e) aus der Liste entfernen?",
                          "Bereits verarbeitete Angebote bleiben in SAP und in der "
                          "Historie erhalten."):
            return
        self.queue.clear()
        self.queue_bar.refresh()
        self.counter_label.setText("Arbeitsvorrat geleert")

    def _show_queue_finished(self) -> None:
        if self.queue.is_empty or not self.queue.is_finished:
            return
        QMessageBox.information(self, "Arbeitsvorrat abgearbeitet",
                                self.queue.overall_result())

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

        for path in offer.source_files:
            self.settings.add_recent_file(path)

        if self._queue_index_loading >= 0:
            self.queue.mark_loaded(self._queue_index_loading, offer)
            self._queue_index_loading = -1
        self.queue_bar.refresh()
        self._fill_extraction_notes(offer)
        logger.info("Angebot geladen: %s (%d Positionen)", offer.source_label,
                    len(offer.positions))

        if not offer.positions:
            self._offer_fallback_workflow()

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
            # Hat die Erkennung eine Position bewusst abgewaehlt (z. B. weil im
            # Mailtext "Position 30 entfaellt" steht), darf die Vorbelegung das
            # nicht wieder aufheben.
            position.selected = ui.autoselect_after_import and position.selected
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
    # Auffang-Workflow: wenn die automatische Erkennung nicht greift
    # ==================================================================
    def _offer_fallback_workflow(self) -> None:
        """Bei null erkannten Positionen aktiv einen Ausweg anbieten.

        Eine blosse Fehlermeldung waere hier das Schlimmste: Der Anwender hat
        ein Angebot vor sich, das er verarbeiten muss, und das Werkzeug sagt
        nur „geht nicht“.  Stattdessen werden beide Auffangwege direkt
        angeboten -- und beide lernen fuer das naechste Mal mit.
        """
        offer = self.offer
        ursache = self._empty_result_cause(offer)
        gruende = "\n".join(f"• {note}" for note in (offer.extraction_notes[:5]
                                                     if offer else []))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Keine Positionen erkannt")
        box.setText("In diesem Angebot wurden keine Positionen gefunden.")

        hinweis = ""
        if ursache:
            # Der Grund gehoert nach vorn, nicht hinter "Details".  Wer nicht
            # weiss, dass sein PDF ein Scan ist, sucht sonst den Fehler bei
            # sich -- und das Anlernen bringt ihn dort auch nicht weiter.
            hinweis = f"Grund:\n{ursache}\n\n"
        box.setInformativeText(
            hinweis +
            "Sie haben zwei schnelle Wege – beide merkt sich die Anwendung fuer "
            "kuenftige Angebote dieses Lieferanten:\n\n"
            "• Tabelle einfuegen: Bereich in Excel kopieren, hier einfuegen, Spalten "
            "zuordnen\n"
            "• Grafisch anlernen: im PDF markieren, wo Positionen und Spalten stehen")
        if gruende:
            box.setDetailedText(f"Was die Erkennung versucht hat:\n{gruende}")

        table_button = box.addButton("Tabelle einfuegen",
                                     QMessageBox.ButtonRole.AcceptRole)
        teach_button = box.addButton("Grafisch anlernen",
                                     QMessageBox.ButtonRole.AcceptRole)
        manual_button = box.addButton("Von Hand erfassen",
                                      QMessageBox.ButtonRole.ActionRole)
        box.addButton("Spaeter", QMessageBox.ButtonRole.RejectRole)
        teach_button.setEnabled(self._teachable_pdf() is not None)
        if not teach_button.isEnabled():
            hat_pdf = any(str(p).lower().endswith(".pdf")
                          for p in (offer.source_files if offer else []))
            teach_button.setToolTip(
                "Dieses PDF hat keine Textebene (Scan). Das Anlernen liest die "
                "Woerter aus der Textebene und findet hier nichts. Bitte ein "
                "Text-PDF anfordern oder die Tabelle einfuegen."
                if hat_pdf else
                "Grafisches Anlernen gibt es nur fuer PDF-Angebote.")
        box.exec()

        clicked = box.clickedButton()
        if clicked is table_button:
            self.import_table_manually()
        elif clicked is teach_button:
            self.teach_recognition()
        elif clicked is manual_button:
            # Schnellerfassung statt leerer Tabellenzeile: der Anwender kann
            # sofort lostippen, ohne erst die richtige Zelle zu suchen.
            self.toggle_quick_entry(True)

    #: Befunde, die *erklaeren*, warum nichts erkannt wurde -- im Unterschied
    #: zu den Folgemeldungen ("Waehrung fehlt", "keine Position"), die nur
    #: wiederholen, was der Anwender ohnehin sieht.
    _URSACHEN_CODES = ("reader_warning", "reader_error", "file_error", "no_input")

    def _empty_result_cause(self, offer: Offer | None) -> str:
        """Den erklaerenden Befund heraussuchen, nicht die Folgemeldungen.

        Ohne das steht im Leer-Dialog nur "keine Positionen gefunden" -- der
        Anwender erfaehrt nicht, dass sein PDF ein Scan ohne Textebene ist,
        und probiert dann das Anlernen, das genau daran ebenfalls scheitert.
        """
        if offer is None:
            return ""
        texte: list[str] = []
        for problem in offer.issues:
            if problem.code not in self._URSACHEN_CODES:
                continue
            text = (problem.message or "").strip()
            if text and text not in texte:
                texte.append(text)
        return "\n\n".join(texte[:2])

    def _teachable_pdf(self) -> str | None:
        """Pfad des PDFs, auf dem angelernt werden kann (falls vorhanden).

        Ein PDF ohne Textebene zaehlt hier ausdruecklich *nicht*: Das
        Anlernen liest die Woerter aus der Textebene: Wo keine ist, liefert
        auch das sorgfaeltigste Rechteck nichts.  Den Weg anzubieten waere
        eine Einladung in eine Sackgasse.
        """
        if self.offer is None:
            return None
        for path in self.offer.source_files:
            if str(path).lower().endswith(".pdf") and Path(path).is_file():
                if not self._pdf_has_text(str(path)):
                    continue
                return str(path)
        return None

    @staticmethod
    def _pdf_has_text(path: str) -> bool:
        """Hat das PDF eine Textebene?  Im Zweifel ja -- lieber anbieten."""
        try:
            import fitz  # noqa: PLC0415 - nur hier gebraucht

            with fitz.open(path) as dokument:
                for seite in dokument:
                    if (seite.get_text() or "").strip():
                        return True
            return False
        except Exception as exc:  # noqa: BLE001 - Pruefung darf nie stoppen
            logger.info("Textebene von %s nicht pruefbar (%s) -- Anlernen bleibt "
                        "angeboten.", path, exc)
            return True

    def import_table_manually(self) -> None:
        """Tabelle einfuegen/laden und Spalten selbst zuordnen."""
        vendor = self.offer.vendor_name if self.offer else ""
        dialog = TableImportDialog(self.settings, vendor, self)
        if dialog.exec() != TableImportDialog.DialogCode.Accepted:
            return
        ergebnis = dialog.result_data
        if not ergebnis.positions:
            return
        self._add_positions(ergebnis.positions, ergebnis.source_name)
        if ergebnis.remember and ergebnis.column_map:
            self._remember_column_map(ergebnis.column_map)
        self.counter_label.setText(
            f"{len(ergebnis.positions)} Position(en) aus der Tabelle uebernommen")

    def teach_recognition(self) -> None:
        """Grafisches Anlernen auf dem PDF-Seitenbild."""
        pfad = self._teachable_pdf()
        if pfad is None:
            show_error(
                self, "Kein PDF geladen",
                "Grafisches Anlernen funktioniert auf dem Seitenbild eines PDFs.\n\n"
                "Fuer Excel- oder Textangebote nutzen Sie „Tabelle einfuegen“ – dort "
                "ordnen Sie die Spalten direkt zu.")
            return

        from .teach_dialog import TeachDialog

        vendor = self.offer.vendor_name if self.offer else ""
        dialog = TeachDialog(pfad, vendor, self)
        if dialog.exec() != TeachDialog.DialogCode.Accepted:
            return
        ergebnis = dialog.result_data
        if not ergebnis.positions:
            return
        self._add_positions(ergebnis.positions, f"angelernt: {Path(pfad).name}")
        if ergebnis.remember and ergebnis.column_map:
            self._remember_column_map(ergebnis.column_map)
        self.counter_label.setText(
            f"{len(ergebnis.positions)} Position(en) angelernt "
            f"({ergebnis.pages_applied} Seite(n))")

    def _add_positions(self, positions: list[OfferPosition], quelle: str) -> None:
        """Manuell gewonnene Positionen in das aktuelle Angebot uebernehmen."""
        if self.offer is None:
            self.offer = Offer()
            self.offer.set_field("currency", self.settings.purchasing.currency,
                                 FieldOrigin.DEFAULT)
        self._snapshot("Positionen ergaenzt")

        vorhanden = len(self.offer.positions)
        self.offer.positions.extend(positions)
        self.offer.add_note(f"{len(positions)} Position(en) manuell erfasst ({quelle})")

        self._apply_defaults_to_new(positions)
        self._resolve_materials(self.offer)
        self.offer.renumber()
        self._revalidate()

        self.table_model.set_offer(self.offer)
        self.table.apply_column_widths()
        self._fill_header()
        self._update_counters()
        self._update_actions()
        logger.info("%d Position(en) ergaenzt (%s), vorher %d",
                    len(positions), quelle, vorhanden)

    def _apply_defaults_to_new(self, positions: list[OfferPosition]) -> None:
        """Vorbelegung nur fuer neu hinzugekommene Positionen."""
        purchasing = self.settings.purchasing
        workflow = self.settings.workflow
        offer = self.offer
        for position in positions:
            position.purchasing_org = position.purchasing_org or purchasing.purchasing_org
            position.plant = position.plant or purchasing.plant
            position.vendor_number = position.vendor_number or (offer.vendor_number
                                                                if offer else "")
            if not position.currency:
                position.set_field("currency", (offer.currency if offer else "")
                                   or purchasing.currency, FieldOrigin.DEFAULT)
            if position.price_unit is None:
                position.set_field("price_unit", purchasing.price_unit, FieldOrigin.DEFAULT)
            if not position.uom:
                position.set_field("uom", purchasing.order_unit, FieldOrigin.DEFAULT)
            if position.valid_from is None and offer is not None and offer.valid_from:
                position.set_field("valid_from", offer.valid_from, FieldOrigin.EXTRACTED)
            if position.delivery_date is None:
                position.delivery_date = date.today() + timedelta(
                    days=purchasing.default_delivery_days)
            position.selected = True
            position.do_info_record = workflow.chain_info_record
            position.do_source_list = workflow.chain_source_list
            position.do_contract = workflow.chain_contract
            position.do_purchase_order = workflow.chain_purchase_order

    def _remember_column_map(self, column_map: dict[str, str]) -> None:
        """Manuelle Spaltenzuordnung als Lieferantenprofil sichern.

        Das ist der eigentliche Gewinn des Auffang-Workflows: Aus einer
        einmaligen Handarbeit wird dauerhaftes Wissen ueber diesen Lieferanten.
        """
        if self.offer is None or not column_map:
            return
        schluessel = self.offer.vendor_number or self.offer.vendor_name
        if not schluessel:
            logger.info("Zuordnung nicht gespeichert: Lieferant noch unbekannt.")
            return
        try:
            merker = getattr(self.import_service, "remember_column_map", None)
            if callable(merker):
                merker(schluessel, self.offer.vendor_name, column_map)
            else:
                self._save_profile_directly(schluessel, column_map)
        except Exception as exc:  # noqa: BLE001 - Lernen darf nie den Ablauf stoppen
            logger.warning("Spaltenzuordnung konnte nicht gespeichert werden: %s", exc)
            return
        logger.info("Spaltenzuordnung fuer %s gespeichert: %s", schluessel, column_map)
        self.mapping_view.reload()
        self.counter_label.setText(
            f"Zuordnung gespeichert – kuenftige Angebote von "
            f"{self.offer.vendor_name or schluessel} werden so gelesen")

    def _save_profile_directly(self, vendor_key: str, column_map: dict[str, str]) -> None:
        """Ersatzweg, falls die Erkennung keine eigene Merkfunktion anbietet."""
        from ..services.extraction.profiles import VendorProfile

        store = getattr(self.import_service, "profile_store", None)
        if store is None:
            logger.info("Kein Profilspeicher verfuegbar – Zuordnung gilt nur diesmal.")
            return

        profil = None
        for vorhanden in store.load_profiles():
            if getattr(vorhanden, "vendor_key", "") == vendor_key:
                profil = vorhanden
                break
        if profil is None:
            profil = VendorProfile(vendor_key=vendor_key,
                                   vendor_name=self.offer.vendor_name if self.offer else "")
            if not getattr(profil, "profile_id", ""):
                profil.profile_id = f"manuell-{vendor_key}"
        profil.column_map.update(column_map)
        profil.correction_count = getattr(profil, "correction_count", 0) + 1
        store.save_profile(profil)

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
        if dialog.maintain_vendor_master:
            # Stammdatenpflege gilt je Lieferant und laeuft deshalb nicht im
            # Stapel mit, sondern genau einmal -- mit eigener Bestaetigung.
            self._maintain_vendor_master()

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

        # Arbeitsvorrat fortschreiben und -- wenn gewuenscht -- weiterblaettern
        if self.queue.current_index >= 0:
            self.queue.mark_processed(self.queue.current_index, summary)
            self.queue_bar.refresh()
            if self.settings.ui.auto_advance_queue and self.queue.pending:
                if ask_yes_no(
                        self, "Naechstes Angebot",
                        f"Noch {self.queue.pending} Angebot(e) im Arbeitsvorrat.",
                        "Soll das naechste jetzt geoeffnet werden?"):
                    self._load_next_from_queue(force=True)
            else:
                self._show_queue_finished()

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
        dialog = VendorAssignmentDialog(name, domain, candidates, current, self,
                                        settings=self.settings)
        result_code = dialog.exec()
        if result_code == VendorAssignmentDialog.CREATE_REQUESTED:
            self._create_vendor_in_sap(name)
            return
        if result_code != VendorAssignmentDialog.DialogCode.Accepted:
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

    def _maintain_vendor_master(self, position=None) -> None:
        """Stammdaten des Lieferanten pflegen (XK02).

        Bewusst je LIEFERANT und nicht je Position: derselbe Stammsatz darf
        nicht mehrfach angefasst werden, nur weil das Angebot zwanzig Zeilen
        hat.  Der Name kommt deshalb aus dem Angebotskopf.
        """
        if self.offer is None:
            show_error(self, "Kein Angebot geladen",
                       "Bitte zuerst ein Angebot einlesen.")
            return
        self._create_vendor_in_sap(self.offer.vendor_name)

    def _create_vendor_in_sap(self, vendor_name: str) -> None:
        """Neuanlage-Dialog oeffnen und -- nach Bestaetigung -- in SAP anlegen (XK01).

        Es wird bewusst NICHT im Hintergrund geschrieben: der Dialog verlangt
        eine eigene Bestaetigung, danach wird sofort (kein Dry Run, kein
        Batch) genau dieser eine Lieferant angelegt.
        """
        if self.offer is None:
            return
        create_dialog = VendorCreateDialog(vendor_name, self.settings, self)
        if create_dialog.exec() != VendorCreateDialog.DialogCode.Accepted:
            return
        plan = create_dialog.plan()
        service = VendorMasterService(self.gateway)
        context = self.gateway.write_context()
        result = service.create_or_update(plan, context)
        if not result.ok:
            show_error(self, "Lieferant konnte nicht angelegt werden", result.message)
            return

        number = result.document_number
        if not number:
            show_error(self, "Lieferant konnte nicht angelegt werden",
                       "SAP hat keine Lieferantennummer gemeldet.")
            return

        self._snapshot("Lieferant in SAP angelegt")
        self.offer.set_field("vendor_number", number, FieldOrigin.MANUAL)
        for item in self.offer.positions:
            if not item.vendor_number:
                item.vendor_number = number

        self._revalidate()
        self.table_model.refresh_all()
        self._fill_header()
        self.details.refresh()
        self._update_counters()
        QMessageBox.information(self, "Lieferant angelegt",
                                f"Lieferant {number} wurde in SAP angelegt: {result.message}")

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

    # ------------------------------------------------------------------
    # Schnellerfassung
    # ------------------------------------------------------------------
    def _quick_message(self, text: str) -> None:
        """Rueckmeldung der Schnellerfassung in die Statuszeile."""
        if getattr(self, "counter_label", None) is not None:
            self.counter_label.setText(text)

    def toggle_quick_entry(self, sichtbar: bool | None = None) -> None:
        """Schnellerfassung ein- oder ausblenden und den Fokus setzen."""
        ziel = (not self.quick_entry.isVisible()) if sichtbar is None else sichtbar
        self.quick_entry.setVisible(ziel)
        if hasattr(self, "quick_entry_action"):
            self.quick_entry_action.setChecked(ziel)
        if ziel:
            self.quick_entry.focus_first()

    def _quick_position_entered(self, position: OfferPosition) -> None:
        """Eine schnell erfasste Position uebernehmen.

        Laeuft ueber denselben Weg wie jeder andere manuelle Zugang --
        Vorbelegung, Materialabgleich, Neunummerierung, Pruefung.  Sonst
        haette die Schnellerfassung andere Ergebnisse als die Tabelle, und
        genau das faellt spaeter niemandem auf.
        """
        self._add_positions([position], "Schnellerfassung")
        self.quick_entry.focus_first()

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
        self._update_filter_label()

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
            self.offer_title.setText("Kein Angebot geladen")
            self.offer_meta.setText("")
            self.vendor_number_label.setText("kein SAP-Lieferant")
            self.vendor_number_label.setStyleSheet(f"color: {Colors.GREY};")
            self.extraction_label.setVisible(False)
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
            self.vendor_number_label.setText(offer.vendor_number)
            self.vendor_number_label.setStyleSheet(
                f"color: {Colors.GREEN}; font-weight: 600;")
            self.vendor_number_label.setToolTip("Zugeordneter SAP-Lieferant")
        else:
            self.vendor_number_label.setText("kein SAP-Lieferant")
            self.vendor_number_label.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")
            self.vendor_number_label.setToolTip(
                "Ohne SAP-Lieferant kann nichts gepflegt werden")

        # Titel: Lieferant.  Nebenzeile: die wenigen wirklich wichtigen Angaben.
        self.offer_title.setText(offer.vendor_name or "Angebot ohne Lieferantennamen")
        angaben = [teil for teil in (
            offer.offer_number,
            format_date(offer.offer_date),
            offer.currency,
        ) if teil]
        self.offer_meta.setText("   •   ".join(angaben))

        quelle = offer.source_label
        if offer.email:
            quelle += f" · von {offer.email.from_address or offer.email.from_name}"
        self.offer_meta.setToolTip(quelle)
        self._fill_extraction_notes(offer)

    def _fill_extraction_notes(self, offer: Offer) -> None:
        """Erkennungshinweise auf eine Zeile eindampfen.

        Die ausfuehrliche Begruendung steht in der Sprechblase -- sichtbar nur,
        wenn jemand sie sucht.
        """
        notizen = list(offer.extraction_notes)
        unsicher = sum(1 for position in offer.positions if position.uncertain_fields)
        if not notizen and not unsicher:
            self.extraction_label.setVisible(False)
            return

        text = f"Erkennung: {len(offer.positions)} Position(en)"
        if unsicher:
            text += f"   •   {unsicher} mit unsicheren Werten"
        if notizen:
            text += f"   •   {len(notizen)} Hinweis(e)"
        self.extraction_label.setText(text)
        self.extraction_label.setToolTip("\n".join(notizen) if notizen else "")
        self.extraction_label.setVisible(True)

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
        self.counter_inline.setText(
            f"{counts['ausgewaehlt']} von {counts['gesamt']} ausgewaehlt"
            + (f"   •   {counts['fehler']} Fehler" if counts["fehler"] else "")
            + (f"   •   {counts['pruefen']} zu pruefen" if counts["pruefen"] else ""))
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
        # Auffangwege: Tabelle geht immer, Anlernen nur auf einem PDF
        self.action_table.setEnabled(not running)
        self.action_teach.setEnabled(not running and self._teachable_pdf() is not None)

    def _update_mode_badges(self) -> None:
        """Eine Plakette fuer die Betriebsart, eine fuer die Verbindung.

        Beide sagten vorher fast dasselbe.  Jetzt gilt: die Betriebsart sagt,
        *was* passiert; die Verbindungsplakette *womit* -- und sie erscheint
        nur, wenn es ueberhaupt eine echte Verbindung geben kann.
        """
        status = self.gateway.status()

        if self.settings.use_mock_sap:
            self.mode_badge.setText("Testsystem")
            self.mode_badge.setStyleSheet(badge_style(Colors.BLUE, Colors.BLUE_BG))
            self.mode_badge.setToolTip(
                "Es wird kein echtes SAP angesprochen. "
                "Umschalten unter Verwaltung → Einstellungen.")
            self.connection_badge.setVisible(False)
            self.dry_run_box.setVisible(False)
            self.dry_run_box.setChecked(self.settings.dry_run)
            return

        if self.settings.dry_run:
            self.mode_badge.setText("Dry Run – nur lesen")
            self.mode_badge.setStyleSheet(badge_style(Colors.AMBER, Colors.AMBER_BG))
        else:
            self.mode_badge.setText("ECHTBETRIEB – schreibend")
            self.mode_badge.setStyleSheet(badge_style(Colors.RED, Colors.RED_BG))
        self.mode_badge.setToolTip("Betriebsart aendern: Verwaltung → Einstellungen")

        colours = {"gruen": (Colors.GREEN, Colors.GREEN_BG),
                   "gelb": (Colors.AMBER, Colors.AMBER_BG),
                   "rot": (Colors.RED, Colors.RED_BG),
                   "blau": (Colors.BLUE, Colors.BLUE_BG),
                   "grau": (Colors.GREY, Colors.GREY_BG)}
        colour, background = colours.get(status.color, (Colors.GREY, Colors.GREY_BG))
        self.connection_badge.setText(
            f"{status.system or 'SAP'} / {status.user}" if status.connected
            else "nicht verbunden")
        self.connection_badge.setVisible(True)
        self.dry_run_box.setVisible(True)
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
