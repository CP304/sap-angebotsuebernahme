"""Seite "Historie".

Jede in SAP durchgefuehrte (oder simulierte) Aktion ist hier nachvollziehbar:
wer, wann, welches Material, welcher Lieferant, alter Wert, neuer Wert,
Ergebnis.  Filterbar und als CSV exportierbar.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import show_error
from .style import Colors

logger = logging.getLogger(__name__)

_COLUMNS = ("Zeitpunkt", "Betrieb", "Aktion", "Lieferant", "Material", "Angebot",
            "Alt", "Neu", "Beleg", "Ergebnis", "Meldung")

_ACTION_LABELS = {
    "": "Alle Aktionen",
    "info_record": "Infosatz",
    "source_list": "Orderbuch",
    "contract": "Mengenkontrakt",
    "purchase_order": "Bestellung",
}

_STATE_LABELS = {
    "": "Alle Ergebnisse",
    "success": "erfolgreich",
    "failed": "fehlgeschlagen",
    "skipped": "uebersprungen",
    "simulated": "simuliert (Dry Run)",
}

_STATE_COLORS = {
    "success": Colors.GREEN,
    "failed": Colors.RED,
    "skipped": Colors.GREY,
    "simulated": Colors.BLUE,
}


class HistoryView(QWidget):
    """Tabellarische Historie mit Filtern."""

    def __init__(self, repository=None, parent=None) -> None:
        super().__init__(parent)
        self.repository = repository
        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        heading = QLabel("Historie")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        filters = QHBoxLayout()
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("dd.MM.yyyy")
        self.date_from.setDate(date.today() - timedelta(days=30))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("dd.MM.yyyy")
        self.date_to.setDate(date.today())
        filters.addWidget(QLabel("Von:"))
        filters.addWidget(self.date_from)
        filters.addWidget(QLabel("Bis:"))
        filters.addWidget(self.date_to)

        self.action_filter = QComboBox()
        for key, label in _ACTION_LABELS.items():
            self.action_filter.addItem(label, key)
        filters.addWidget(self.action_filter)

        self.state_filter = QComboBox()
        for key, label in _STATE_LABELS.items():
            self.state_filter.addItem(label, key)
        filters.addWidget(self.state_filter)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen (Material, Lieferant, Beleg, Meldung) ...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self.reload)
        filters.addWidget(self.search_edit, 1)

        self.only_real = QCheckBox("Nur Echtbetrieb")
        filters.addWidget(self.only_real)

        apply_button = QPushButton("Anzeigen")
        apply_button.clicked.connect(self.reload)
        filters.addWidget(apply_button)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.summary_label = QLabel("")
        footer.addWidget(self.summary_label, 1)
        export_button = QPushButton("Als CSV exportieren ...")
        export_button.clicked.connect(self.export)
        footer.addWidget(export_button)
        layout.addLayout(footer)

    # ------------------------------------------------------------------
    def set_repository(self, repository) -> None:
        self.repository = repository
        self.reload()

    def _filters(self) -> dict:
        return {
            "date_from": self.date_from.date().toString("yyyy-MM-dd"),
            "date_to": self.date_to.date().toString("yyyy-MM-dd"),
            "action": self.action_filter.currentData() or "",
            "state": self.state_filter.currentData() or "",
            "search": self.search_edit.text().strip(),
            "only_real": self.only_real.isChecked(),
            "limit": 5000,
        }

    def reload(self) -> None:
        self.table.setRowCount(0)
        if self.repository is None:
            self.summary_label.setText("Keine Datenbank verbunden.")
            return
        try:
            rows = self.repository.history(self._filters())
        except Exception as exc:  # noqa: BLE001 - Historie darf nie die App stoppen
            logger.exception("Historie konnte nicht gelesen werden")
            self.summary_label.setText("Die Historie konnte nicht gelesen werden.")
            show_error(self, "Historie", "Die Historie konnte nicht gelesen werden.",
                       str(exc))
            return

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                self._format_timestamp(row.get("timestamp", "")),
                "Test" if row.get("mode") == "mock" else ("Dry Run" if row.get("dry_run")
                                                          else "Echt"),
                _ACTION_LABELS.get(row.get("action", ""), row.get("action", "")),
                f"{row.get('vendor_number', '')} {row.get('vendor_name', '')}".strip(),
                row.get("material_number", ""),
                row.get("offer_number", ""),
                row.get("old_value", ""),
                row.get("new_value", ""),
                row.get("document_number", ""),
                _STATE_LABELS.get(row.get("state", ""), row.get("state", "")),
                row.get("message", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 9:
                    colour = _STATE_COLORS.get(row.get("state", ""))
                    if colour:
                        item.setForeground(Qt.GlobalColor.black)
                        item.setToolTip(row.get("detail", "") or "")
                if row.get("detail"):
                    item.setToolTip(row.get("detail", ""))
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

        failed = sum(1 for r in rows if r.get("state") == "failed")
        self.summary_label.setText(
            f"{len(rows)} Eintrag/Eintraege"
            + (f"   •   davon {failed} fehlgeschlagen" if failed else ""))

    @staticmethod
    def _format_timestamp(value: str) -> str:
        text = str(value or "")
        if "T" in text:
            text = text.replace("T", " ")
        return text[:16]

    def export(self) -> None:
        if self.repository is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Historie exportieren", "historie.csv", "CSV-Datei (*.csv)")
        if not path:
            return
        try:
            self.repository.export_history_csv(path, self._filters())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Export fehlgeschlagen")
            show_error(self, "Export fehlgeschlagen",
                       "Die Historie konnte nicht exportiert werden.", str(exc))
            return
        QMessageBox.information(self, "Export abgeschlossen",
                                f"Die Historie wurde exportiert:\n{path}")
