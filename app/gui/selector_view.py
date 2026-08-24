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

from ..sap.feldnamen import beschreibe_feld
from ..services.selektoren_excel import (exportiere_selektoren,
                                         lies_selektoren,
                                         uebernimm_selektoren)
from ..sap.selectors import REQUIRED_SCREENS, SelectorRegistry
from ..services.vbs_parser import TRANSACTION_NAMES, detect_transaction
from ..utils.textkodierung import decode_bytes
from .dialogs import ask_yes_no, show_error
from .style import Colors

logger = logging.getLogger(__name__)

_COLUMNS = ("Maske / Feld", "Beschreibung", "SAP-GUI-ID",
            "So heisst das Feld in SAP", "Pflicht", "Geprueft")


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
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QHBoxLayout()
        import_button = QPushButton("Aufzeichnung (.vbs) einlesen ...")
        import_button.clicked.connect(self._import_vbs)
        buttons.addWidget(import_button)

        excel_export = QPushButton("Nach Excel sichern ...")
        excel_export.setToolTip(
            "Alle Feld-IDs als Arbeitsmappe sichern -- zum Aufheben, "
            "Weitergeben oder Durchsehen")
        excel_export.clicked.connect(self._export_excel)
        buttons.addWidget(excel_export)

        excel_import = QPushButton("Aus Excel einlesen ...")
        excel_import.setToolTip(
            "Eine gesicherte Arbeitsmappe zurueckspielen oder die eines "
            "Kollegen uebernehmen")
        excel_import.clicked.connect(self._import_excel)
        buttons.addWidget(excel_import)

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
                    "",
                    f"{verified}/{required} geprueft",
                ])
                parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsEditable)
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                if required and verified < required:
                    parent.setForeground(5, Qt.GlobalColor.darkYellow)

                matched = 0
                for key, selector in screen.elements.items():
                    haystack = f"{key} {selector.description} {selector.id}".lower()
                    if search and search not in haystack:
                        continue
                    # Was der SAP-Feldname bedeutet.  Beim Eintragen von
                    # Hand ist das die eigentliche Hilfe: die Beschreibung
                    # sagt, welches Feld gemeint ist, diese Spalte sagt,
                    # ob die eingetragene ID dazu passt.
                    child = QTreeWidgetItem([
                        key,
                        selector.description,
                        selector.id,
                        beschreibe_feld(selector.id),
                        "nein" if selector.optional else "ja",
                        "",
                    ])
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEditable
                                   | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(5, Qt.CheckState.Checked if selector.verified
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
                item.setCheckState(5, Qt.CheckState.Unchecked)
                # Die Lesehilfe gilt fuer die neue ID, nicht mehr fuer die alte.
                item.setText(3, beschreibe_feld(new_id))
                self._loading = False
                logger.info("Feld-ID geaendert: %s.%s = %s", screen_key, element_key, new_id)
        elif column == 5:
            selector.verified = item.checkState(5) == Qt.CheckState.Checked
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
                child.setCheckState(5, Qt.CheckState.Checked)
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
            rohdaten = Path(path).read_bytes()
        except OSError as exc:
            show_error(self, "Datei nicht lesbar",
                       "Die Aufzeichnung konnte nicht gelesen werden.", str(exc))
            return
        # Der Recorder schreibt UTF-16LE.  Fest als utf-8 gelesen bleibt
        # zwischen je zwei Buchstaben ein Nullzeichen stehen, und kein
        # findById-Aufruf ist mehr als solcher zu erkennen.
        text, kodierung, _warnung = decode_bytes(rohdaten)
        logger.info("Aufzeichnung %s gelesen (Kodierung %s)",
                    Path(path).name, kodierung)

        ids = self.registry.ids_from_vbs(text)
        if not ids:
            show_error(self, "Keine IDs gefunden",
                       "In der Datei wurden keine findById-Aufrufe gefunden. "
                       "Stammt sie wirklich vom SAP GUI Script Recorder?")
            return

        # Aus welcher Transaktion stammt die Aufzeichnung?  Das grenzt die
        # Zuordnung auf deren Bildschirme ein: eine ME11-Aufzeichnung kann
        # keine Kontraktfelder enthalten, und was nicht zugeordnet werden
        # kann, wird auch nicht ueberschrieben.
        transaktion = detect_transaction(text)
        herkunft = (f" aus {transaktion}"
                    f" ({TRANSACTION_NAMES.get(transaktion, '')})".rstrip(" ()")
                    if transaktion else "")

        mapping = self.registry.suggest_mapping(ids, transaktion)
        if not mapping:
            QMessageBox.information(
                self, "Nichts zu aendern",
                f"{len(ids)} ID(s) gelesen{herkunft}.\n\n"
                "Es gibt nichts zu aktualisieren: entweder stimmen die "
                "hinterlegten IDs bereits mit der Aufzeichnung ueberein, "
                "oder die aufgezeichneten Felder lassen sich nicht "
                "zweifelsfrei zuordnen.\n\n"
                "Schaltflaechen werden bewusst nie zugeordnet -- welcher "
                "Knopf welcher ist, verraet nur seine Nummer, und die ist "
                "nicht uebertragbar. Solche Felder bitte von Hand eintragen.")
            return

        # Alt und neu nebeneinander: sonst sieht der Anwender nicht, was
        # ueberschrieben wird, und bestaetigt im Zweifel blind.
        zeilen = []
        for (screen_key, element_key), neue_id in sorted(mapping.items())[:12]:
            selector = self.registry.get(screen_key, element_key)
            zeilen.append(f"{screen_key}.{element_key} — {selector.description}\n"
                          f"    bisher: {selector.id or '(leer)'}\n"
                          f"    neu:    {neue_id}")
        preview = "\n\n".join(zeilen)
        more = "" if len(mapping) <= 12 else f"\n\n... und {len(mapping) - 12} weitere"
        if not ask_yes_no(self, "Zuordnungen uebernehmen",
                          f"{len(mapping)} von {len(ids)} gelesenen ID(s)"
                          f"{herkunft} weichen ab und koennen uebernommen werden.",
                          preview + more):
            return

        for (screen_key, element_key), new_id in mapping.items():
            self.registry.set_id(screen_key, element_key, new_id)
        logger.info("%d Feld-IDs aus Aufzeichnung uebernommen (%s, %s)",
                    len(mapping), path, transaktion or "Transaktion unbekannt")
        self.reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Uebernommen",
            f"{len(mapping)} Feld-ID(s) uebernommen.\n\nSie gelten als "
            "ungeprueft: bitte am Zielsystem kontrollieren und dann den "
            "Haken „Geprueft“ setzen. Vorher schreibt die Anwendung damit "
            "nicht in ein echtes SAP.")

    def _export_excel(self) -> None:
        """Alle Feld-IDs als Arbeitsmappe sichern."""
        vorschlag = str(Path(self.settings.selectors_file).with_name(
            "SAP-Feld-IDs.xlsx"))
        pfad, _filter = QFileDialog.getSaveFileName(
            self, "Feld-IDs sichern", vorschlag, "Excel-Arbeitsmappe (*.xlsx)")
        if not pfad:
            return
        if not pfad.lower().endswith(".xlsx"):
            pfad += ".xlsx"
        try:
            ziel = exportiere_selektoren(self.registry, Path(pfad))
        except (OSError, RuntimeError) as fehler:
            show_error(self, "Sichern fehlgeschlagen",
                       "Die Feld-IDs konnten nicht als Excel gesichert werden.",
                       str(fehler))
            return
        QMessageBox.information(
            self, "Gesichert",
            f"Die Feld-IDs wurden gesichert:\n{ziel}\n\n"
            "Die Mappe enthaelt zu jedem Feld die ID, ihre Bedeutung im "
            "Klartext und eine Spalte fuer Bemerkungen. Sie laesst sich "
            "hier wieder einlesen.")

    def _import_excel(self) -> None:
        """Eine gesicherte Arbeitsmappe zurueckspielen."""
        pfad, _filter = QFileDialog.getOpenFileName(
            self, "Gesicherte Feld-IDs waehlen", "",
            "Excel-Arbeitsmappe (*.xlsx *.xlsm);;Alle Dateien (*.*)")
        if not pfad:
            return
        try:
            ergebnis = lies_selektoren(self.registry, Path(pfad))
        except (OSError, RuntimeError, ValueError) as fehler:
            show_error(self, "Einlesen fehlgeschlagen",
                       "Die Arbeitsmappe konnte nicht gelesen werden.",
                       str(fehler))
            return

        if not ergebnis.hat_aenderungen:
            QMessageBox.information(
                self, "Nichts zu aendern",
                "Die Mappe stimmt mit dem aktuellen Stand ueberein.\n\n"
                + "\n".join(ergebnis.warnungen))
            return

        # Alt und neu nebeneinander -- wer nicht sieht, was ueberschrieben
        # wird, bestaetigt im Zweifel blind.
        zeilen = []
        for (screen_key, element_key), (alt_id, neu_id) in \
                sorted(ergebnis.geaendert.items())[:12]:
            beschreibung = self.registry.get(screen_key, element_key).description
            zeilen.append(f"{screen_key}.{element_key} — {beschreibung}\n"
                          f"    bisher: {alt_id or '(leer)'}\n"
                          f"    neu:    {neu_id}")
        if len(ergebnis.geaendert) > 12:
            zeilen.append(f"... und {len(ergebnis.geaendert) - 12} weitere")
        if ergebnis.freigaben:
            zeilen.append(f"Ausserdem {len(ergebnis.freigaben)} unveraenderte "
                          "Feld-ID(s), die laut Mappe geprueft sind.")
        zeilen.extend(ergebnis.warnungen)

        if not ask_yes_no(
                self, "Aus Excel uebernehmen",
                f"{len(ergebnis.geaendert)} Feld-ID(s) weichen ab.",
                "\n\n".join(zeilen)):
            return

        anzahl = uebernimm_selektoren(self.registry, ergebnis)
        self.reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Uebernommen",
            f"{anzahl} Feld(er) uebernommen.\n\nGeaenderte IDs gelten wieder "
            "als ungeprueft: ob eine ID aus einer fremden Mappe zu diesem "
            "System passt, weiss nur, wer sie hier kontrolliert. Bitte "
            "pruefen und dann den Haken „Geprueft“ setzen.\n\n"
            "Nicht vergessen: „Speichern“, damit es dauerhaft gilt.")

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
