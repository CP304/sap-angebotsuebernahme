"""Urlaub, Krankheit und Feiertage fuer einen ganzen Zeitraum eintragen."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
)

from ..storage import ARBEITSTAG, TAGESART_TEXT, URLAUB


class AbwesenheitsDialog(QDialog):
    """Zeitraum, Art und Anteil -- Wochenenden bleiben unberuehrt."""

    def __init__(self, vorgabe: date, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Abwesenheit eintragen")
        self.setMinimumWidth(420)

        formular = QFormLayout(self)
        self._von = QDateEdit(QDate(vorgabe), calendarPopup=True)
        self._bis = QDateEdit(QDate(vorgabe), calendarPopup=True)
        for feld in (self._von, self._bis):
            feld.setDisplayFormat("dd.MM.yyyy")

        self._art = QComboBox()
        for schluessel, beschriftung in TAGESART_TEXT.items():
            self._art.addItem(beschriftung, schluessel)
        self._art.setCurrentIndex(max(0, self._art.findData(URLAUB)))

        self._anteil = QDoubleSpinBox()
        self._anteil.setRange(0.5, 1.0)
        self._anteil.setSingleStep(0.5)
        self._anteil.setValue(1.0)
        self._anteil.setToolTip("1,0 = ganzer Tag, 0,5 = halber Tag")

        self._notiz = QLineEdit()
        self._notiz.setPlaceholderText("Bemerkung (freiwillig)")

        formular.addRow("von", self._von)
        formular.addRow("bis", self._bis)
        formular.addRow("Art", self._art)
        formular.addRow("Anteil je Tag", self._anteil)
        formular.addRow("Bemerkung", self._notiz)
        hinweis = QLabel(
            "Tage ohne Sollstunden (in der Regel Samstag und Sonntag) werden "
            "uebersprungen.\n"
            "\"Arbeitstag\" nimmt eine eingetragene Abwesenheit wieder zurueck."
        )
        hinweis.setWordWrap(True)
        hinweis.setStyleSheet("color: #666;")
        formular.addRow(hinweis)

        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.button(QDialogButtonBox.Ok).setText("Eintragen")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        formular.addRow(knoepfe)

        self._art.currentIndexChanged.connect(
            lambda: self._anteil.setEnabled(str(self._art.currentData()) != ARBEITSTAG)
        )

    def ergebnis(self) -> tuple[date, date, str, float, str]:
        von = self._von.date().toPython()
        bis = self._bis.date().toPython()
        if bis < von:
            von, bis = bis, von
        return von, bis, str(self._art.currentData()), self._anteil.value(), self._notiz.text().strip()
