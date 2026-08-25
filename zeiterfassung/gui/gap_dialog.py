"""Rueckfrage bei einer Luecke im Tag.

Wurde der Rechner am selben Tag zwischendurch heruntergefahren, kann das
Werkzeug nicht wissen, was in der Zeit war.  Statt zu raten, fragt es genau
einmal nach -- mit einem sinnvollen Vorschlag je nach Laenge der Luecke.
"""

from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from ..rules import als_stunden, als_uhrzeit
from ..storage import ABWESEND, ARBEIT, PAUSE
from ..tracker import OffeneLuecke

AUSWAHL = (
    (ARBEIT, "Gearbeitet", "Besprechung, Aussentermin, anderer Rechner -- zaehlt als Arbeitszeit."),
    (PAUSE, "Pause", "Mittag oder Unterbrechung -- zaehlt als Pause und wird angerechnet."),
    (ABWESEND, "Nicht gearbeitet", "Arzt, Privates, frueher Feierabend -- zaehlt gar nicht."),
)


class LueckenDialog(QDialog):
    """Fragt die Einordnung einer einzelnen Luecke ab."""

    def __init__(self, luecke: OffeneLuecke, parent=None) -> None:
        super().__init__(parent)
        self.luecke = luecke
        self.setWindowTitle("Zeiterfassung -- Luecke im Tag")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumWidth(460)

        aufbau = QVBoxLayout(self)
        kopf = QLabel(
            f"<b>Der Rechner war heute zwischendurch aus.</b><br>"
            f"{als_uhrzeit(luecke.beginn)} bis {als_uhrzeit(luecke.ende)} "
            f"({als_stunden(luecke.dauer)})<br><br>Wie soll diese Zeit gezaehlt werden?"
        )
        kopf.setWordWrap(True)
        aufbau.addWidget(kopf)

        self._gruppe = QButtonGroup(self)
        # Vorschlag: kurze Luecken sind meist Pause, lange meist Abwesenheit.
        vorgabe = PAUSE if luecke.dauer <= timedelta(hours=2) else ABWESEND
        for art, beschriftung, erklaerung in AUSWAHL:
            knopf = QRadioButton(beschriftung)
            knopf.setProperty("art", art)
            knopf.setChecked(art == vorgabe)
            self._gruppe.addButton(knopf)
            aufbau.addWidget(knopf)
            hinweis = QLabel(erklaerung)
            hinweis.setWordWrap(True)
            hinweis.setStyleSheet("color: #666; margin-left: 22px; margin-bottom: 6px;")
            aufbau.addWidget(hinweis)

        self._notiz = QLineEdit()
        self._notiz.setPlaceholderText("Bemerkung (freiwillig)")
        aufbau.addWidget(self._notiz)

        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.button(QDialogButtonBox.Ok).setText("Uebernehmen")
        knoepfe.button(QDialogButtonBox.Cancel).setText("Spaeter entscheiden")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        aufbau.addWidget(knoepfe)

    def ergebnis(self) -> tuple[str, str]:
        knopf = self._gruppe.checkedButton()
        return (str(knopf.property("art")) if knopf else PAUSE), self._notiz.text().strip()
