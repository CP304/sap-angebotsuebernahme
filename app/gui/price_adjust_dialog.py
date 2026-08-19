"""Preise in einem Rutsch anpassen -- mit Vorschau vor der Zustimmung.

"Alle Preise plus 3 %" oder "jede Position zehn Cent teurer" ist eine
Ansage, die der Lieferant in einem Satz macht.  Sie in vierzig Zeilen
von Hand nachzuvollziehen ist eine halbe Stunde Arbeit und vierzig
Gelegenheiten, sich zu vertippen.

Die Vorschau ist hier kein Beiwerk, sondern der Kern: Der Anwender sieht
vor der Zustimmung Zeile fuer Zeile, was sich aendert -- und was
absichtlich unveraendert bleibt.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
)

from ..services.preisanpassung import (
    BASE_CURRENT,
    BASE_OLD,
    MODE_ABSOLUTE,
    MODE_PERCENT,
    MODE_SET,
    apply_adjustment,
    preview_adjustment,
)

logger = logging.getLogger(__name__)

__all__ = ["PriceAdjustDialog"]


class PriceAdjustDialog(QDialog):
    """Siehe Modulkopf."""

    def __init__(self, positions: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preise anpassen")
        self.positions = list(positions)
        self.result_info = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        kopf = QLabel(f"{len(self.positions)} Position(en) ausgewaehlt.")
        kopf.setObjectName("SubHeading")
        layout.addWidget(kopf)

        # -- Art der Anpassung -------------------------------------------
        art_box = QGroupBox("Was soll passieren?")
        art_layout = QVBoxLayout(art_box)
        self.mode_group = QButtonGroup(self)

        zeile = QHBoxLayout()
        self.mode_percent = QRadioButton("prozentual um")
        self.mode_percent.setChecked(True)
        self.mode_group.addButton(self.mode_percent)
        zeile.addWidget(self.mode_percent)
        self.percent_value = QDoubleSpinBox()
        self.percent_value.setRange(-99.0, 999.0)
        self.percent_value.setDecimals(2)
        self.percent_value.setSuffix(" %")
        self.percent_value.setValue(3.0)
        self.percent_value.setToolTip(
            "Minuszeichen fuer eine Senkung, z. B. -2,5 %")
        zeile.addWidget(self.percent_value)
        zeile.addStretch(1)
        art_layout.addLayout(zeile)

        zeile2 = QHBoxLayout()
        self.mode_absolute = QRadioButton("um einen festen Betrag")
        self.mode_group.addButton(self.mode_absolute)
        zeile2.addWidget(self.mode_absolute)
        self.absolute_value = QDoubleSpinBox()
        self.absolute_value.setRange(-100000.0, 100000.0)
        self.absolute_value.setDecimals(4)
        self.absolute_value.setValue(0.10)
        self.absolute_value.setToolTip(
            "Minuszeichen fuer eine Senkung, z. B. -0,05")
        zeile2.addWidget(self.absolute_value)
        zeile2.addStretch(1)
        art_layout.addLayout(zeile2)

        zeile3 = QHBoxLayout()
        self.mode_set = QRadioButton("auf einen festen Preis setzen")
        self.mode_group.addButton(self.mode_set)
        zeile3.addWidget(self.mode_set)
        self.set_value = QDoubleSpinBox()
        self.set_value.setRange(0.0001, 1000000.0)
        self.set_value.setDecimals(4)
        self.set_value.setValue(1.0)
        zeile3.addWidget(self.set_value)
        zeile3.addStretch(1)
        art_layout.addLayout(zeile3)
        layout.addWidget(art_box)

        # -- Bezugsgroesse ------------------------------------------------
        self.base_box = QGroupBox("Gerechnet auf welchen Preis?")
        base_layout = QVBoxLayout(self.base_box)
        self.base_current = QRadioButton(
            "den jetzigen Preis in der Tabelle")
        self.base_current.setChecked(True)
        self.base_current.setToolTip(
            "Fuer nachtraegliche Korrekturen an einem eingelesenen Angebot.")
        base_layout.addWidget(self.base_current)
        self.base_old = QRadioButton(
            "den bestehenden Preis aus SAP (alter Preis)")
        self.base_old.setToolTip(
            "Fuer den Fall, dass der Lieferant eine Erhoehung auf den "
            "bestehenden Preis nennt, ohne selbst neue Preise zu schicken.")
        base_layout.addWidget(self.base_old)
        hinweis = QLabel(
            "Die Verwechslung dieser beiden ist der teuerste Fehler an "
            "dieser Stelle: \"plus 3 %\" auf einen bereits erhoehten Preis "
            "gerechnet ergibt stillen Unsinn. Die Vorschau unten zeigt, "
            "was tatsaechlich herauskommt.")
        hinweis.setWordWrap(True)
        hinweis.setObjectName("Muted")
        base_layout.addWidget(hinweis)
        layout.addWidget(self.base_box)

        # -- Vorschau -----------------------------------------------------
        layout.addWidget(QLabel("Vorschau:"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(180)
        layout.addWidget(self.preview, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        # -- Knoepfe ------------------------------------------------------
        knoepfe = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = knoepfe.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Anpassen")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        layout.addWidget(knoepfe)

        # Jede Aenderung rechnet die Vorschau neu
        for knopf in (self.mode_percent, self.mode_absolute, self.mode_set):
            knopf.toggled.connect(self._refresh)
        for feld in (self.percent_value, self.absolute_value, self.set_value):
            feld.valueChanged.connect(self._refresh)
        for knopf in (self.base_current, self.base_old):
            knopf.toggled.connect(self._refresh)

        self._refresh()

    # ------------------------------------------------------------------
    def current_mode(self) -> str:
        if self.mode_set.isChecked():
            return MODE_SET
        if self.mode_absolute.isChecked():
            return MODE_ABSOLUTE
        return MODE_PERCENT

    def current_base(self) -> str:
        return BASE_OLD if self.base_old.isChecked() else BASE_CURRENT

    def current_value(self) -> float:
        mode = self.current_mode()
        if mode == MODE_SET:
            return self.set_value.value()
        if mode == MODE_ABSOLUTE:
            return self.absolute_value.value()
        return self.percent_value.value()

    def _refresh(self) -> None:
        """Vorschau neu berechnen."""
        # Bei einem Festwert gibt es nichts zu beziehen.
        self.base_box.setEnabled(self.current_mode() != MODE_SET)

        ergebnis = preview_adjustment(
            self.positions, self.current_mode(), self.current_value(),
            self.current_base())
        self.preview.setPlainText(ergebnis.details() or "Nichts zu aendern.")
        self.summary_label.setText(ergebnis.summary())
        self.ok_button.setEnabled(ergebnis.count > 0)

    def apply(self):
        """Anpassung durchfuehren und Ergebnis zurueckgeben."""
        self.result_info = apply_adjustment(
            self.positions, self.current_mode(), self.current_value(),
            self.current_base())
        return self.result_info
