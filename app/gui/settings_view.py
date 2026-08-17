"""Seite "Einstellungen".

Alles, was sich je Anwender, Werk oder Mandant unterscheidet, ist hier
einstellbar -- nichts davon steht im Programmcode.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from .dialogs import ask_yes_no, show_error
from .style import Colors

logger = logging.getLogger(__name__)


class SettingsView(QWidget):
    """Editor fuer die Anwendungskonfiguration."""

    settingsSaved = Signal()
    modeChanged = Signal()
    resetMockRequested = Signal()

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._loading = False
        self._build()
        self.load()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        layout.addWidget(self._build_mode_group())
        layout.addWidget(self._build_purchasing_group())
        layout.addWidget(self._build_workflow_group())
        layout.addWidget(self._build_threshold_group())
        layout.addWidget(self._build_transaction_group())
        layout.addWidget(self._build_runtime_group())
        layout.addWidget(self._build_path_group())
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        reload_button = QPushButton("Verwerfen")
        reload_button.clicked.connect(self.load)
        buttons.addWidget(reload_button)
        save_button = QPushButton("Einstellungen speichern")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self.save)
        buttons.addWidget(save_button)
        outer.addLayout(buttons)

    # -- Gruppen --------------------------------------------------------
    def _build_mode_group(self) -> QGroupBox:
        group = QGroupBox("Betriebsart")
        layout = QVBoxLayout(group)

        self.mock_box = QCheckBox("Testsystem verwenden (Mock-SAP, kein echtes SAP)")
        self.mock_box.setToolTip("Fuer Einarbeitung, Vorfuehrung und Tests ohne SAP-Zugang.")
        self.dry_box = QCheckBox("Dry Run – SAP nur lesen, nichts schreiben")
        self.dry_box.setToolTip("Alle Aktionen werden simuliert und angezeigt, aber nicht "
                                "ausgefuehrt.")
        for box in (self.mock_box, self.dry_box):
            box.toggled.connect(self._mode_toggled)
            layout.addWidget(box)

        self.mode_hint = QLabel("")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setObjectName("SubHeading")
        layout.addWidget(self.mode_hint)

        reset_button = QPushButton("Testdaten auf Auslieferungszustand zuruecksetzen")
        reset_button.clicked.connect(self._reset_mock)
        layout.addWidget(reset_button, 0, Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_purchasing_group(self) -> QGroupBox:
        group = QGroupBox("Einkauf – Vorbelegung")
        form = QFormLayout(group)
        self.purchasing_org = QLineEdit()
        self.purchasing_group = QLineEdit()
        self.plant = QLineEdit()
        self.currency = QLineEdit()
        self.order_unit = QLineEdit()
        self.price_unit = QSpinBox()
        self.price_unit.setRange(1, 100000)
        self.contract_type = QLineEdit()
        self.po_type = QLineEdit()
        self.contract_months = QSpinBox()
        self.contract_months.setRange(1, 120)
        self.delivery_days = QSpinBox()
        self.delivery_days.setRange(0, 365)

        form.addRow("Einkaufsorganisation:", self.purchasing_org)
        form.addRow("Einkaeufergruppe:", self.purchasing_group)
        form.addRow("Werk:", self.plant)
        form.addRow("Standardwaehrung:", self.currency)
        form.addRow("Standard-Mengeneinheit:", self.order_unit)
        form.addRow("Standard-Preiseinheit:", self.price_unit)
        form.addRow("Belegart Kontrakt:", self.contract_type)
        form.addRow("Belegart Bestellung:", self.po_type)
        form.addRow("Kontraktlaufzeit (Monate):", self.contract_months)
        form.addRow("Standard-Lieferzeit (Tage):", self.delivery_days)
        return group

    def _build_workflow_group(self) -> QGroupBox:
        group = QGroupBox("Komplettvorgang")
        layout = QVBoxLayout(group)

        grid = QGridLayout()
        self.chain_info = QCheckBox("Infosatz pflegen")
        self.chain_contract = QCheckBox("Mengenkontrakt schreiben")
        self.chain_source = QCheckBox("Orderbuch pflegen")
        self.chain_order = QCheckBox("Bestellung anlegen")
        for index, box in enumerate((self.chain_info, self.chain_contract,
                                     self.chain_source, self.chain_order)):
            grid.addWidget(box, index // 2, index % 2)
        layout.addLayout(grid)

        form = QFormLayout()
        self.ir_valid_to = QLineEdit()
        self.ir_valid_to.setToolTip("Hauseigener Platzhalter, z. B. 31.12.2099")
        self.sl_valid_to = QLineEdit()
        self.contract_valid_to = QLineEdit()
        self.sl_active = QCheckBox("Lieferant im Orderbuch aktiv setzen (Sperre entfernen)")
        self.sl_mrp = QLineEdit()
        self.sl_mrp.setToolTip("Dispokennzeichen, z. B. 1 (leer = keins)")
        self.sl_fixed = QCheckBox("Als feste Bezugsquelle kennzeichnen")
        self.sl_reference = QCheckBox("Angelegten Kontrakt im Orderbuch hinterlegen")
        self.po_from_contract = QCheckBox("Bestellung mit Bezug auf den Kontrakt anlegen")
        self.call_off_mode = QComboBox()
        self.call_off_mode.addItem("Prozent der Kontraktmenge", "percent")
        self.call_off_mode.addItem("Feste Menge", "absolute")
        self.call_off_mode.addItem("Volle Menge", "full")
        self.call_off_percent = QDoubleSpinBox()
        self.call_off_percent.setRange(0.1, 100.0)
        self.call_off_percent.setSuffix(" %")
        self.call_off_round = QCheckBox("Abrufmenge auf ganze Einheiten runden")

        form.addRow("Infosatz gueltig bis:", self.ir_valid_to)
        form.addRow("Orderbuch gueltig bis:", self.sl_valid_to)
        form.addRow("Kontrakt gueltig bis:", self.contract_valid_to)
        form.addRow("", self.sl_active)
        form.addRow("Dispokennzeichen Orderbuch:", self.sl_mrp)
        form.addRow("", self.sl_fixed)
        form.addRow("", self.sl_reference)
        form.addRow("", self.po_from_contract)
        form.addRow("Abrufmenge:", self.call_off_mode)
        form.addRow("Abrufanteil:", self.call_off_percent)
        form.addRow("", self.call_off_round)
        layout.addLayout(form)

        safety = QGroupBox("Sicherheit – Nachrichten")
        safety_layout = QVBoxLayout(safety)
        self.suppress_messages = QCheckBox(
            "Nachrichtenfindung unterdruecken (kein Versand an den Lieferanten)")
        self.abort_on_messages = QCheckBox(
            "Beleg NICHT sichern, wenn Nachrichten nicht entfernt werden konnten")
        note = QLabel("Diese beiden Einstellungen verhindern, dass durch dieses Werkzeug "
                      "ungewollt eine Bestellung oder ein Kontrakt an den Lieferanten "
                      "hinausgeht. Abschalten nur nach Ruecksprache mit dem SAP-Team.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {Colors.AMBER};")
        safety_layout.addWidget(self.suppress_messages)
        safety_layout.addWidget(self.abort_on_messages)
        safety_layout.addWidget(note)
        layout.addWidget(safety)
        return group

    def _build_threshold_group(self) -> QGroupBox:
        group = QGroupBox("Grenzwerte")
        form = QFormLayout(group)
        self.warn_percent = QDoubleSpinBox()
        self.warn_percent.setRange(0.0, 1000.0)
        self.warn_percent.setSuffix(" %")
        self.error_percent = QDoubleSpinBox()
        self.error_percent.setRange(0.0, 1000.0)
        self.error_percent.setSuffix(" %")
        self.max_price = QDoubleSpinBox()
        self.max_price.setRange(0.0, 100_000_000.0)
        self.max_price.setDecimals(2)
        self.vendor_threshold = QDoubleSpinBox()
        self.vendor_threshold.setRange(0.0, 1.0)
        self.vendor_threshold.setSingleStep(0.01)
        self.offer_age = QSpinBox()
        self.offer_age.setRange(0, 3650)
        self.offer_age.setSuffix(" Tage")

        form.addRow("Preisabweichung gelb ab:", self.warn_percent)
        form.addRow("Preisabweichung rot ab:", self.error_percent)
        form.addRow("Preisobergrenze (Tippfehlerschutz):", self.max_price)
        form.addRow("Mindestaehnlichkeit Lieferantenzuordnung:", self.vendor_threshold)
        form.addRow("Warnen, wenn Angebot aelter als:", self.offer_age)
        return group

    def _build_transaction_group(self) -> QGroupBox:
        group = QGroupBox("SAP-Transaktionen")
        form = QFormLayout(group)
        self.transaction_fields: dict[str, QLineEdit] = {}
        labels = {
            "info_record_display": "Infosatz anzeigen",
            "info_record_create": "Infosatz anlegen",
            "info_record_change": "Infosatz aendern",
            "source_list_maintain": "Orderbuch pflegen",
            "source_list_display": "Orderbuch anzeigen",
            "contract_create": "Kontrakt anlegen",
            "contract_change": "Kontrakt aendern",
            "contract_display": "Kontrakt anzeigen",
            "purchase_order_create": "Bestellung anlegen",
            "purchase_order_change": "Bestellung aendern",
            "purchase_order_display": "Bestellung anzeigen",
            "material_display": "Material anzeigen",
            "vendor_display": "Lieferant anzeigen",
        }
        for key, label in labels.items():
            edit = QLineEdit()
            edit.setMaximumWidth(120)
            self.transaction_fields[key] = edit
            form.addRow(f"{label}:", edit)
        return group

    def _build_runtime_group(self) -> QGroupBox:
        group = QGroupBox("SAP-Laufzeitverhalten")
        form = QFormLayout(group)
        self.element_timeout = QDoubleSpinBox()
        self.element_timeout.setRange(1.0, 120.0)
        self.element_timeout.setSuffix(" s")
        self.poll_interval = QDoubleSpinBox()
        self.poll_interval.setRange(0.01, 2.0)
        self.poll_interval.setSingleStep(0.05)
        self.poll_interval.setSuffix(" s")
        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 10)
        self.conditions_box = QCheckBox("Infosatzpreis ueber das Konditionsbild pflegen")
        self.verify_context = QCheckBox("Vor dem Schreiben Material/Lieferant in der Maske pruefen")
        self.read_status = QCheckBox("Statusleiste nach jedem Schritt auswerten")

        form.addRow("Zeitlimit je Bildelement:", self.element_timeout)
        form.addRow("Abfrageintervall:", self.poll_interval)
        form.addRow("Wiederholungen bei COM-Fehlern:", self.retry_count)
        form.addRow("", self.conditions_box)
        form.addRow("", self.verify_context)
        form.addRow("", self.read_status)
        return group

    def _build_path_group(self) -> QGroupBox:
        group = QGroupBox("Pfade")
        form = QFormLayout(group)
        self.db_path = QLineEdit()
        self.log_path = QLineEdit()
        self.selector_path = QLineEdit()

        for edit, caption, mode in ((self.db_path, "Datenbank:", "file"),
                                    (self.log_path, "Logverzeichnis:", "dir"),
                                    (self.selector_path, "Feld-IDs (JSON):", "file")):
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            button = QPushButton("...")
            button.setMaximumWidth(36)
            button.clicked.connect(lambda _=False, e=edit, m=mode: self._pick_path(e, m))
            row.addWidget(button, 0)
            container = QWidget()
            container.setLayout(row)
            form.addRow(caption, container)

        hint = QLabel(f"Standardablage: {self.settings.home}")
        hint.setObjectName("SubHeading")
        form.addRow("", hint)
        return group

    def _pick_path(self, edit: QLineEdit, mode: str) -> None:
        if mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "Verzeichnis waehlen", edit.text())
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Datei waehlen", edit.text())
        if path:
            edit.setText(path)

    # ------------------------------------------------------------------
    def load(self) -> None:
        self._loading = True
        try:
            s = self.settings
            self.mock_box.setChecked(s.use_mock_sap)
            self.dry_box.setChecked(s.dry_run)

            p = s.purchasing
            self.purchasing_org.setText(p.purchasing_org)
            self.purchasing_group.setText(p.purchasing_group)
            self.plant.setText(p.plant)
            self.currency.setText(p.currency)
            self.order_unit.setText(p.order_unit)
            self.price_unit.setValue(p.price_unit)
            self.contract_type.setText(p.contract_document_type)
            self.po_type.setText(p.purchase_order_document_type)
            self.contract_months.setValue(p.contract_duration_months)
            self.delivery_days.setValue(p.default_delivery_days)

            w = s.workflow
            self.chain_info.setChecked(w.chain_info_record)
            self.chain_contract.setChecked(w.chain_contract)
            self.chain_source.setChecked(w.chain_source_list)
            self.chain_order.setChecked(w.chain_purchase_order)
            self.ir_valid_to.setText(w.info_record_valid_to)
            self.sl_valid_to.setText(w.source_list_valid_to)
            self.contract_valid_to.setText(w.contract_valid_to)
            self.sl_active.setChecked(w.source_list_set_active)
            self.sl_mrp.setText(w.source_list_mrp_indicator)
            self.sl_fixed.setChecked(w.source_list_set_fixed)
            self.sl_reference.setChecked(w.source_list_reference_contract)
            self.po_from_contract.setChecked(w.purchase_order_from_contract)
            index = self.call_off_mode.findData(w.call_off_mode)
            self.call_off_mode.setCurrentIndex(max(0, index))
            self.call_off_percent.setValue(float(w.call_off_percent))
            self.call_off_round.setChecked(w.call_off_round_to_integer)
            self.suppress_messages.setChecked(w.suppress_output_messages)
            self.abort_on_messages.setChecked(w.abort_if_messages_present)

            t = s.thresholds
            self.warn_percent.setValue(float(t.price_warn_percent))
            self.error_percent.setValue(float(t.price_error_percent))
            self.max_price.setValue(float(t.max_absolute_price))
            self.vendor_threshold.setValue(float(t.vendor_match_threshold))
            self.offer_age.setValue(t.offer_age_warn_days)

            for key, edit in self.transaction_fields.items():
                edit.setText(getattr(s.transactions, key, ""))

            r = s.sap
            self.element_timeout.setValue(r.element_timeout_s)
            self.poll_interval.setValue(r.poll_interval_s)
            self.retry_count.setValue(r.retry_count)
            self.conditions_box.setChecked(r.info_record_price_via_conditions)
            self.verify_context.setChecked(r.verify_context_before_write)
            self.read_status.setChecked(r.read_status_bar)

            self.db_path.setText(str(s.db_file))
            self.log_path.setText(str(s.log_dir))
            self.selector_path.setText(str(s.selectors_file))
        finally:
            self._loading = False
        self._update_mode_hint()

    def save(self) -> None:
        s = self.settings
        try:
            s.use_mock_sap = self.mock_box.isChecked()
            s.dry_run = self.dry_box.isChecked()

            p = s.purchasing
            p.purchasing_org = self.purchasing_org.text().strip()
            p.purchasing_group = self.purchasing_group.text().strip()
            p.plant = self.plant.text().strip()
            p.currency = self.currency.text().strip().upper()
            p.order_unit = self.order_unit.text().strip().upper()
            p.price_unit = self.price_unit.value()
            p.contract_document_type = self.contract_type.text().strip().upper()
            p.purchase_order_document_type = self.po_type.text().strip().upper()
            p.contract_duration_months = self.contract_months.value()
            p.default_delivery_days = self.delivery_days.value()

            w = s.workflow
            w.chain_info_record = self.chain_info.isChecked()
            w.chain_contract = self.chain_contract.isChecked()
            w.chain_source_list = self.chain_source.isChecked()
            w.chain_purchase_order = self.chain_order.isChecked()
            w.info_record_valid_to = self.ir_valid_to.text().strip()
            w.source_list_valid_to = self.sl_valid_to.text().strip()
            w.contract_valid_to = self.contract_valid_to.text().strip()
            w.source_list_set_active = self.sl_active.isChecked()
            w.source_list_mrp_indicator = self.sl_mrp.text().strip()
            w.source_list_set_fixed = self.sl_fixed.isChecked()
            w.source_list_reference_contract = self.sl_reference.isChecked()
            w.purchase_order_from_contract = self.po_from_contract.isChecked()
            w.call_off_mode = self.call_off_mode.currentData()
            w.call_off_percent = Decimal(str(self.call_off_percent.value()))
            w.call_off_round_to_integer = self.call_off_round.isChecked()
            w.suppress_output_messages = self.suppress_messages.isChecked()
            w.abort_if_messages_present = self.abort_on_messages.isChecked()

            t = s.thresholds
            t.price_warn_percent = Decimal(str(self.warn_percent.value()))
            t.price_error_percent = Decimal(str(self.error_percent.value()))
            t.price_confirm_percent = t.price_error_percent
            t.max_absolute_price = Decimal(str(self.max_price.value()))
            t.vendor_match_threshold = Decimal(str(self.vendor_threshold.value()))
            t.offer_age_warn_days = self.offer_age.value()

            for key, edit in self.transaction_fields.items():
                setattr(s.transactions, key, edit.text().strip().upper())

            r = s.sap
            r.element_timeout_s = self.element_timeout.value()
            r.poll_interval_s = self.poll_interval.value()
            r.retry_count = self.retry_count.value()
            r.info_record_price_via_conditions = self.conditions_box.isChecked()
            r.verify_context_before_write = self.verify_context.isChecked()
            r.read_status_bar = self.read_status.isChecked()

            s.database_path = self.db_path.text().strip()
            s.log_path = self.log_path.text().strip()
            s.selectors_path = self.selector_path.text().strip()

            s.save()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Einstellungen konnten nicht gespeichert werden")
            show_error(self, "Speichern fehlgeschlagen",
                       "Die Einstellungen konnten nicht gespeichert werden.", str(exc))
            return

        QMessageBox.information(self, "Gespeichert",
                                "Die Einstellungen wurden gespeichert.\n\n"
                                "Aenderungen an Datenbank- und Logpfad wirken nach einem "
                                "Neustart der Anwendung.")
        self.settingsSaved.emit()

    # ------------------------------------------------------------------
    def _mode_toggled(self) -> None:
        if self._loading:
            return
        if not self.mock_box.isChecked() and not self.dry_box.isChecked():
            if not ask_yes_no(
                    self, "Echtbetrieb ohne Dry Run",
                    "Sie schalten auf echtes SAP OHNE Dry Run um.",
                    "Die Anwendung wird dann tatsaechlich Infosaetze, Orderbucheintraege, "
                    "Kontrakte und Bestellungen in SAP anlegen. Fortfahren?"):
                self._loading = True
                self.dry_box.setChecked(True)
                self._loading = False
        self.settings.use_mock_sap = self.mock_box.isChecked()
        self.settings.dry_run = self.dry_box.isChecked()
        self._update_mode_hint()
        self.modeChanged.emit()

    def _update_mode_hint(self) -> None:
        if self.mock_box.isChecked():
            text = ("Testsystem: Es wird kein echtes SAP angesprochen. Ideal zum "
                    "Einarbeiten und Vorfuehren.")
            colour = Colors.BLUE
        elif self.dry_box.isChecked():
            text = ("Echtes SAP, aber nur lesend. Alle Schreibaktionen werden nur "
                    "simuliert und angezeigt.")
            colour = Colors.AMBER
        else:
            text = ("ECHTBETRIEB: Es wird in SAP geschrieben. Zusaetzlich muessen alle "
                    "benoetigten Feld-IDs geprueft sein.")
            colour = Colors.RED
        self.mode_hint.setText(text)
        self.mode_hint.setStyleSheet(f"color: {colour}; font-weight: 600;")

    def _reset_mock(self) -> None:
        if ask_yes_no(self, "Testdaten zuruecksetzen",
                      "Alle im Testsystem angelegten Infosaetze, Kontrakte und "
                      "Bestellungen loeschen?",
                      "Der Auslieferungszustand wird wiederhergestellt. Echte SAP-Daten "
                      "sind davon nicht betroffen."):
            self.resetMockRequested.emit()
