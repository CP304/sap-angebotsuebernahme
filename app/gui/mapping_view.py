"""Seite "Zuordnungen".

Zwei Dauerprobleme im Einkaufsalltag:

* Der Lieferant nennt sich im Angebot anders als im SAP-Stammsatz.
* Der Lieferant kennt nur seine eigene Artikelnummer, nicht unsere.

Beides wird hier einmalig gepflegt und danach automatisch angewandt.  Ergaenzt
wird das durch die gelernten Layoutprofile: Wenn ein Lieferant sein Angebot
immer gleich aufbaut, merkt sich die Anwendung, wo welche Spalte steht.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .dialogs import ask_yes_no, show_error

logger = logging.getLogger(__name__)


class MappingView(QWidget):
    """Pflege der lokalen Zuordnungstabellen und Lieferantenprofile."""

    def __init__(self, repository=None, parent=None) -> None:
        super().__init__(parent)
        self.repository = repository
        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        heading = QLabel("Zuordnungen")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        tabs = QTabWidget()
        tabs.addTab(self._build_vendor_tab(), "Lieferanten")
        tabs.addTab(self._build_material_tab(), "Materialien")
        tabs.addTab(self._build_profile_tab(), "Gelernte Angebotsformate")
        layout.addWidget(tabs, 1)

    # -- Lieferanten -----------------------------------------------------
    def _build_vendor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel("Ordnet einen Lieferantennamen oder eine E-Mail-Domain einer "
                      "SAP-Lieferantennummer zu. Die Domain ist der zuverlaessigste "
                      "Schluessel, wenn Angebote per Mail kommen.")
        hint.setWordWrap(True)
        hint.setObjectName("SubHeading")
        layout.addWidget(hint)

        self.vendor_table = QTableWidget(0, 5)
        self.vendor_table.setHorizontalHeaderLabels(
            ["Art", "Suchwert", "SAP-Lieferant", "Name", "Verwendet"])
        self._prepare_table(self.vendor_table)
        layout.addWidget(self.vendor_table, 1)

        form = QGroupBox("Neue Zuordnung")
        form_layout = QFormLayout(form)
        self.vendor_type = QComboBox()
        self.vendor_type.addItem("Lieferantenname", "name")
        self.vendor_type.addItem("E-Mail-Domain", "domain")
        self.vendor_type.addItem("E-Mail-Adresse", "email")
        self.vendor_type.addItem("USt-IdNr.", "vat_id")
        self.vendor_value = QLineEdit()
        self.vendor_value.setPlaceholderText("z. B. muster-dichtungstechnik.de")
        self.vendor_number = QLineEdit()
        self.vendor_number.setPlaceholderText("z. B. 0000100234")
        self.vendor_name = QLineEdit()
        form_layout.addRow("Art:", self.vendor_type)
        form_layout.addRow("Suchwert:", self.vendor_value)
        form_layout.addRow("SAP-Lieferantennummer:", self.vendor_number)
        form_layout.addRow("Bezeichnung:", self.vendor_name)
        layout.addWidget(form)

        buttons = QHBoxLayout()
        add = QPushButton("Zuordnung speichern")
        add.setObjectName("Primary")
        add.clicked.connect(self._add_vendor)
        delete = QPushButton("Markierte loeschen")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_vendor)
        buttons.addStretch(1)
        buttons.addWidget(delete)
        buttons.addWidget(add)
        layout.addLayout(buttons)
        return page

    # -- Materialien -----------------------------------------------------
    def _build_material_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel("Ordnet eine Lieferantenartikelnummer oder eine Textbezeichnung "
                      "der eigenen SAP-Materialnummer zu. Die Zuordnung gilt je "
                      "Lieferant (leer = fuer alle).")
        hint.setWordWrap(True)
        hint.setObjectName("SubHeading")
        layout.addWidget(hint)

        self.material_table = QTableWidget(0, 6)
        self.material_table.setHorizontalHeaderLabels(
            ["Lieferant", "Art", "Suchwert", "Material", "Bezeichnung", "Verwendet"])
        self._prepare_table(self.material_table)
        layout.addWidget(self.material_table, 1)

        form = QGroupBox("Neue Zuordnung")
        form_layout = QFormLayout(form)
        self.material_vendor = QLineEdit()
        self.material_vendor.setPlaceholderText("leer = fuer alle Lieferanten")
        self.material_type = QComboBox()
        self.material_type.addItem("Lieferantenartikelnummer", "vendor_material")
        self.material_type.addItem("Textbezeichnung", "text")
        self.material_value = QLineEdit()
        self.material_number = QLineEdit()
        self.material_description = QLineEdit()
        form_layout.addRow("SAP-Lieferant:", self.material_vendor)
        form_layout.addRow("Art:", self.material_type)
        form_layout.addRow("Suchwert:", self.material_value)
        form_layout.addRow("SAP-Material:", self.material_number)
        form_layout.addRow("Bezeichnung:", self.material_description)
        layout.addWidget(form)

        buttons = QHBoxLayout()
        add = QPushButton("Zuordnung speichern")
        add.setObjectName("Primary")
        add.clicked.connect(self._add_material)
        delete = QPushButton("Markierte loeschen")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_material)
        buttons.addStretch(1)
        buttons.addWidget(delete)
        buttons.addWidget(add)
        layout.addLayout(buttons)
        return page

    # -- Profile ---------------------------------------------------------
    def _build_profile_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel("Die Anwendung merkt sich, wie ein Lieferant seine Angebote "
                      "aufbaut (Spaltenanordnung, Zahlenformat, wiederkehrende "
                      "Textmarken). Gelernt wird ausschliesslich, <b>wo</b> ein Wert "
                      "steht – nie, <b>welcher</b> Wert dort steht.")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setObjectName("SubHeading")
        layout.addWidget(hint)

        self.profile_table = QTableWidget(0, 6)
        self.profile_table.setHorizontalHeaderLabels(
            ["Lieferant", "Schluessel", "Angebote", "Erfolgreich", "Korrekturen",
             "Zuletzt aktualisiert"])
        self._prepare_table(self.profile_table)
        layout.addWidget(self.profile_table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        delete = QPushButton("Markiertes Profil verwerfen")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_profile)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        return page

    @staticmethod
    def _prepare_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    # ------------------------------------------------------------------
    def set_repository(self, repository) -> None:
        self.repository = repository
        self.reload()

    def reload(self) -> None:
        if self.repository is None:
            return
        self._fill(self.vendor_table, self._vendor_rows())
        self._fill(self.material_table, self._material_rows())
        self._fill(self.profile_table, self._profile_rows())

    def _vendor_rows(self) -> list[tuple]:
        try:
            entries = self.repository.all_vendor_mappings()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lieferantenzuordnungen nicht lesbar: %s", exc)
            return []
        rows = []
        for entry in entries:
            rows.append((
                entry.get("match_type", ""), entry.get("match_value", ""),
                entry.get("vendor_number", ""), entry.get("vendor_name", ""),
                str(entry.get("use_count", 0)), entry.get("id"),
            ))
        return rows

    def _material_rows(self) -> list[tuple]:
        try:
            entries = self.repository.all_material_mappings()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Materialzuordnungen nicht lesbar: %s", exc)
            return []
        rows = []
        for entry in entries:
            rows.append((
                entry.get("vendor_number", ""), entry.get("match_type", ""),
                entry.get("match_value", ""), entry.get("material_number", ""),
                entry.get("description", ""), str(entry.get("use_count", 0)),
                entry.get("id"),
            ))
        return rows

    def _profile_rows(self) -> list[tuple]:
        try:
            entries = self.repository.load_profiles()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Profile nicht lesbar: %s", exc)
            return []
        rows = []
        for entry in entries:
            rows.append((
                entry.get("vendor_name", ""), entry.get("vendor_key", ""),
                str(entry.get("sample_count", 0)), str(entry.get("success_count", 0)),
                str(entry.get("correction_count", 0)),
                str(entry.get("updated_at", ""))[:16],
                entry.get("profile_id"),
            ))
        return rows

    @staticmethod
    def _fill(table: QTableWidget, rows: list[tuple]) -> None:
        columns = table.columnCount()
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column in range(columns):
                value = row[column] if column < len(row) else ""
                item = QTableWidgetItem(str(value))
                if column == 0:
                    # letzte Spalte des Tupels ist immer der technische Schluessel
                    item.setData(Qt.ItemDataRole.UserRole, row[-1])
                table.setItem(row_index, column, item)
        table.resizeColumnsToContents()

    @staticmethod
    def _selected_key(table: QTableWidget):
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ------------------------------------------------------------------
    def _add_vendor(self) -> None:
        if self.repository is None:
            return
        value = self.vendor_value.text().strip()
        number = self.vendor_number.text().strip()
        if not value or not number:
            show_error(self, "Angaben fehlen",
                       "Bitte Suchwert und SAP-Lieferantennummer angeben.")
            return
        try:
            self.repository.save_vendor_mapping(
                self.vendor_type.currentData(), value, number,
                self.vendor_name.text().strip())
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Speichern fehlgeschlagen",
                       "Die Zuordnung konnte nicht gespeichert werden.", str(exc))
            return
        self.vendor_value.clear()
        self.vendor_number.clear()
        self.vendor_name.clear()
        self.reload()

    def _delete_vendor(self) -> None:
        key = self._selected_key(self.vendor_table)
        if key is None or self.repository is None:
            return
        if not ask_yes_no(self, "Loeschen", "Markierte Zuordnung loeschen?"):
            return
        self.repository.delete_vendor_mapping(key)
        self.reload()

    def _add_material(self) -> None:
        if self.repository is None:
            return
        value = self.material_value.text().strip()
        number = self.material_number.text().strip()
        if not value or not number:
            show_error(self, "Angaben fehlen",
                       "Bitte Suchwert und SAP-Materialnummer angeben.")
            return
        try:
            self.repository.save_material_mapping(
                self.material_vendor.text().strip(), self.material_type.currentData(),
                value, number, self.material_description.text().strip())
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Speichern fehlgeschlagen",
                       "Die Zuordnung konnte nicht gespeichert werden.", str(exc))
            return
        self.material_value.clear()
        self.material_number.clear()
        self.material_description.clear()
        self.reload()

    def _delete_material(self) -> None:
        key = self._selected_key(self.material_table)
        if key is None or self.repository is None:
            return
        if not ask_yes_no(self, "Loeschen", "Markierte Zuordnung loeschen?"):
            return
        self.repository.delete_material_mapping(key)
        self.reload()

    def _delete_profile(self) -> None:
        key = self._selected_key(self.profile_table)
        if key is None or self.repository is None:
            return
        if not ask_yes_no(self, "Profil verwerfen",
                          "Das gelernte Angebotsformat dieses Lieferanten verwerfen?",
                          "Die Erkennung faellt danach auf die allgemeinen Regeln "
                          "zurueck und lernt beim naechsten Angebot neu."):
            return
        self.repository.delete_profile(key)
        self.reload()
        QMessageBox.information(self, "Verworfen", "Das Profil wurde verworfen.")
