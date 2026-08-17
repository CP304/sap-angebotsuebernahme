"""Seite "SAP-Feld-IDs".

Hier traegt der Anwender die am eigenen System aufgezeichneten SAP-GUI-IDs ein
und bestaetigt sie.  Erst wenn alle fuer einen Vorgang benoetigten IDs
bestaetigt sind, gibt die Anwendung das Schreiben in ein echtes SAP frei.

Bequemer Weg: Aufzeichnung des SAP GUI Script Recorders (.vbs) einlesen --
die Seite schlaegt dann automatisch Zuordnungen vor.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..sap.selectors import REQUIRED_SCREENS, SelectorRegistry
from .dialogs import ask_yes_no, show_error
from .style import Colors

logger = logging.getLogger(__name__)

_COLUMNS = ("Maske / Feld", "Beschreibung", "SAP-GUI-ID", "Pflicht", "Geprueft")


class SelectorView(QWidget):
    """Editor fuer die Selektor-Registry."""

    changed = Signal()

    def __init__(self, registry: SelectorRegistry, settings, parent=None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.settings = settings
        self._loading = False
        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        heading = QLabel("SAP-Feld-IDs")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        explanation = QLabel(
            "SAP-Masken sind kundenspezifisch. Die hier hinterlegten IDs sind "
            "<b>Vorschlaege</b> in der ueblichen Notation und ausdruecklich ungeprueft.<br>"
            "Zeichnen Sie den Vorgang in SAP einmal auf (Alt+F12 → Skript-Aufzeichnung), "
            "tragen Sie die echten IDs ein und setzen Sie den Haken „Geprueft“.<br>"
            "<b>Solange Pflicht-IDs ungeprueft sind, schreibt die Anwendung nicht in ein "
            "echtes SAP.</b> Lesen und Dry Run bleiben erlaubt.")
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(explanation)

        controls = QHBoxLayout()
        self.operation_filter = QComboBox()
        self.operation_filter.addItem("Alle Masken", "")
        for operation in REQUIRED_SCREENS:
            self.operation_filter.addItem(f"Nur fuer: {operation}", operation)
        self.operation_filter.currentIndexChanged.connect(self.reload)
        controls.addWidget(QLabel("Ansicht:"))
        controls.addWidget(self.operation_filter)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen (Feldname, Beschreibung, ID) ...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.reload)
        controls.addWidget(self.search_edit, 1)
        layout.addLayout(controls)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(list(_COLUMNS))
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QHBoxLayout()
        import_button = QPushButton("Aufzeichnung (.vbs) einlesen ...")
        import_button.clicked.connect(self._import_vbs)
        buttons.addWidget(import_button)

        verify_visible = QPushButton("Sichtbare als geprueft markieren")
        verify_visible.clicked.connect(self._verify_visible)
        buttons.addWidget(verify_visible)

        reset_button = QPushButton("Auf Vorschlaege zuruecksetzen")
        reset_button.setObjectName("Danger")
        reset_button.clicked.connect(self._reset)
        buttons.addWidget(reset_button)

        buttons.addStretch(1)
        save_button = QPushButton("Speichern")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self.save)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._loading = True
        try:
            self.tree.clear()
            operation = self.operation_filter.currentData() or ""
            wanted = set(REQUIRED_SCREENS.get(operation, ())) if operation else None
            search = self.search_edit.text().strip().lower()

            for screen_key, screen in self.registry.screens.items():
                if wanted is not None and screen_key not in wanted:
                    continue
                verified, required = self.registry.verification_summary().get(screen_key, (0, 0))
                parent = QTreeWidgetItem([
                    f"{screen.title}",
                    screen.transaction,
                    screen.note,
                    "",
                    f"{verified}/{required} geprueft",
                ])
                parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsEditable)
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                if required and verified < required:
                    parent.setForeground(4, Qt.GlobalColor.darkYellow)

                matched = 0
                for key, selector in screen.elements.items():
                    haystack = f"{key} {selector.description} {selector.id}".lower()
                    if search and search not in haystack:
                        continue
                    child = QTreeWidgetItem([
                        key,
                        selector.description,
                        selector.id,
                        "nein" if selector.optional else "ja",
                        "",
                    ])
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEditable
                                   | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(4, Qt.CheckState.Checked if selector.verified
                                        else Qt.CheckState.Unchecked)
                    child.setData(0, Qt.ItemDataRole.UserRole, (screen_key, key))
                    if not selector.verified and not selector.optional:
                        child.setForeground(2, Qt.GlobalColor.darkYellow)
                        child.setToolTip(2, selector.todo_text())
                    if not selector.id and not selector.optional:
                        child.setForeground(2, Qt.GlobalColor.red)
                    parent.addChild(child)
                    matched += 1

                if matched or not search:
                    self.tree.addTopLevelItem(parent)
            self.tree.expandAll()
            self._update_summary()
        finally:
            self._loading = False

    def _update_summary(self) -> None:
        lines = []
        for operation in REQUIRED_SCREENS:
            missing = self.registry.unverified(REQUIRED_SCREENS[operation])
            if missing:
                lines.append(f"<span style='color:{Colors.AMBER}'>✗ {operation}: "
                             f"{len(missing)} offen</span>")
            else:
                lines.append(f"<span style='color:{Colors.GREEN}'>✓ {operation}: bereit</span>")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setText("   •   ".join(lines))

    # ------------------------------------------------------------------
    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        screen_key, element_key = data
        selector = self.registry.get(screen_key, element_key)

        if column == 2:
            new_id = item.text(2).strip()
            if new_id != selector.id:
                self.registry.set_id(screen_key, element_key, new_id)
                self._loading = True
                item.setCheckState(4, Qt.CheckState.Unchecked)
                self._loading = False
                logger.info("Feld-ID geaendert: %s.%s = %s", screen_key, element_key, new_id)
        elif column == 4:
            selector.verified = item.checkState(4) == Qt.CheckState.Checked
        self._update_summary()
        self.changed.emit()

    def _verify_visible(self) -> None:
        count = self.tree.topLevelItemCount()
        elements = 0
        for index in range(count):
            parent = self.tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if not data or not child.text(2).strip():
                    continue
                elements += 1
        if not elements:
            return
        if not ask_yes_no(
                self, "Als geprueft markieren",
                f"{elements} Feld-ID(s) als geprueft markieren?",
                "Bestaetigen Sie das nur, wenn Sie die IDs tatsaechlich am Zielsystem "
                "kontrolliert haben. Danach schreibt die Anwendung damit in SAP."):
            return
        self._loading = True
        for index in range(count):
            parent = self.tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if not data or not child.text(2).strip():
                    continue
                screen_key, element_key = data
                self.registry.get(screen_key, element_key).verified = True
                child.setCheckState(4, Qt.CheckState.Checked)
        self._loading = False
        self._update_summary()
        self.changed.emit()

    def _import_vbs(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Aufzeichnung des SAP GUI Script Recorders waehlen", "",
            "SAP-Aufzeichnung (*.vbs *.txt);;Alle Dateien (*.*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            show_error(self, "Datei nicht lesbar",
                       "Die Aufzeichnung konnte nicht gelesen werden.", str(exc))
            return

        ids = self.registry.ids_from_vbs(text)
        if not ids:
            show_error(self, "Keine IDs gefunden",
                       "In der Datei wurden keine findById-Aufrufe gefunden. "
                       "Stammt sie wirklich vom SAP GUI Script Recorder?")
            return

        mapping = self.registry.suggest_mapping(ids)
        if not mapping:
            QMessageBox.information(
                self, "Keine Zuordnung moeglich",
                f"{len(ids)} ID(s) gelesen, aber keine passt zu den konfigurierten "
                f"Feldern. Bitte die IDs von Hand eintragen.")
            return

        preview = "\n".join(f"{screen}.{key}\n    {new_id}"
                            for (screen, key), new_id in list(mapping.items())[:15])
        more = "" if len(mapping) <= 15 else f"\n... und {len(mapping) - 15} weitere"
        if not ask_yes_no(self, "Zuordnungen uebernehmen",
                          f"{len(mapping)} Feld-ID(s) koennen aktualisiert werden.",
                          preview + more):
            return

        for (screen_key, element_key), new_id in mapping.items():
            self.registry.set_id(screen_key, element_key, new_id)
        logger.info("%d Feld-IDs aus Aufzeichnung uebernommen (%s)", len(mapping), path)
        self.reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Uebernommen",
            f"{len(mapping)} Feld-ID(s) uebernommen.\n\nBitte pruefen Sie die Zuordnung "
            f"und setzen Sie anschliessend die Haken „Geprueft“.")

    def _reset(self) -> None:
        if not ask_yes_no(self, "Zuruecksetzen",
                          "Alle Feld-IDs auf die Auslieferungsvorschlaege zuruecksetzen?",
                          "Ihre eingetragenen IDs und Bestaetigungen gehen dabei verloren."):
            return
        from ..sap.selectors import default_screens

        self.registry.screens = default_screens()
        self.reload()
        self.changed.emit()

    def save(self) -> None:
        try:
            path = self.registry.save(self.settings.selectors_file)
        except OSError as exc:
            show_error(self, "Speichern fehlgeschlagen",
                       "Die Feld-IDs konnten nicht gespeichert werden.", str(exc))
            return
        QMessageBox.information(self, "Gespeichert",
                                f"Die Feld-IDs wurden gespeichert:\n{path}")
        self.changed.emit()
