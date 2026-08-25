"""Das Mini-Fenster zum Hotkey.

Es erscheint, solange die Tastenkombination gehalten wird, zeigt die
Kennzahlen des Tages und verschwindet beim Loslassen wieder.  Es nimmt
bewusst keinen Fokus (``WA_ShowWithoutActivating``) -- man tippt einfach
weiter, wo man gerade war.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from ..rules import als_stunden, als_uhrzeit
from ..tracker import Zeiterfassung

STIL = """
QFrame#Karte {
    background-color: rgba(23, 32, 56, 235);
    border: 1px solid rgba(120, 150, 210, 160);
    border-radius: 12px;
}
QLabel { color: #E8EDF7; }
QLabel#Titel     { font-size: 15px; font-weight: 600; }
QLabel#Status    { font-size: 12px; color: #9DB2D9; }
QLabel#Schild    { font-size: 12px; color: #9DB2D9; }
QLabel#Wert      { font-size: 15px; font-weight: 600; }
QLabel#Gross     { font-size: 26px; font-weight: 700; color: #7FD1A0; }
QLabel#Fusszeile { font-size: 11px; color: #7A8CB0; }
"""


class MiniFenster(QWidget):
    """Randloses Fenster ueber allem anderen -- nur zum Nachschauen."""

    def __init__(self, zeiterfassung: Zeiterfassung) -> None:
        super().__init__(None)
        self.zeiterfassung = zeiterfassung
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(STIL)

        aussen = QVBoxLayout(self)
        aussen.setContentsMargins(12, 12, 12, 12)
        karte = QFrame(objectName="Karte")
        aussen.addWidget(karte)

        raster = QGridLayout(karte)
        raster.setContentsMargins(18, 14, 18, 14)
        raster.setHorizontalSpacing(18)
        raster.setVerticalSpacing(6)

        self._titel = QLabel("Zeiterfassung", objectName="Titel")
        self._status = QLabel("", objectName="Status")
        raster.addWidget(self._titel, 0, 0, 1, 2)
        raster.addWidget(self._status, 1, 0, 1, 2)

        self._gross = QLabel("0:00 h", objectName="Gross")
        raster.addWidget(self._gross, 2, 0, 1, 2)

        self._werte: dict[str, QLabel] = {}
        zeile = 3
        for schluessel, beschriftung in (
            ("eingestempelt", "eingestempelt seit"),
            ("taetig", "taetig seit"),
            ("pause", "Pause"),
            ("rest", "noch heute"),
            ("feierabend", "Feierabend etwa"),
            ("woche", "Woche (Ist/Soll)"),
            ("wochenrest", "noch diese Woche"),
        ):
            raster.addWidget(QLabel(beschriftung, objectName="Schild"), zeile, 0)
            wert = QLabel("--", objectName="Wert")
            wert.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            raster.addWidget(wert, zeile, 1)
            self._werte[schluessel] = wert
            zeile += 1

        self._fuss = QLabel("", objectName="Fusszeile")
        raster.addWidget(self._fuss, zeile, 0, 1, 2)

        # Solange das Fenster steht, laeuft die Uhr sichtbar weiter.
        self._takt = QTimer(self)
        self._takt.setInterval(1000)
        self._takt.timeout.connect(self.aktualisieren)

    # -- Anzeigen und Verbergen --------------------------------------------
    def einblenden(self) -> None:
        self.aktualisieren()
        self.adjustSize()
        self._positionieren()
        self.show()
        self.raise_()
        self._takt.start()

    def ausblenden(self) -> None:
        self._takt.stop()
        self.hide()

    def _positionieren(self) -> None:
        """Neben den Mauszeiger, aber immer vollstaendig auf dem Bildschirm."""
        bildschirm = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        flaeche = bildschirm.availableGeometry()
        punkt = QCursor.pos()
        x = min(max(flaeche.left(), punkt.x() + 16), flaeche.right() - self.width())
        y = min(max(flaeche.top(), punkt.y() + 16), flaeche.bottom() - self.height())
        self.move(x, y)

    # -- Inhalt -------------------------------------------------------------
    def aktualisieren(self) -> None:
        jetzt = datetime.now()
        kennzahlen = self.zeiterfassung.kennzahlen(jetzt)
        tag = kennzahlen["tag"]
        woche = kennzahlen["woche"]

        self._titel.setText(f"Zeiterfassung -- {jetzt:%A, %d.%m.%Y}")
        if tag.laeuft:
            self._status.setText("eingestempelt und taetig")
        else:
            self._status.setText("nicht eingestempelt")

        self._gross.setText(als_stunden(tag.arbeitszeit))
        self._gross.setStyleSheet(
            "color: #7FD1A0;" if tag.arbeitszeit >= tag.soll else "color: #F2C879;"
        )

        taetig = kennzahlen["taetig_seit"]
        self._werte["eingestempelt"].setText(als_uhrzeit(tag.erste_anmeldung))
        self._werte["taetig"].setText(
            f"{als_uhrzeit(taetig)} ({als_stunden(jetzt - taetig)})" if taetig else "--:--"
        )
        self._werte["pause"].setText(als_stunden(tag.erfasste_pause + tag.pausenabzug))
        rest = tag.rest
        self._werte["rest"].setText(
            als_stunden(rest) if rest > timedelta(0) else f"erfuellt (+{als_stunden(tag.saldo)})"
        )
        self._werte["feierabend"].setText(als_uhrzeit(tag.feierabend))
        self._werte["woche"].setText(f"{als_stunden(woche.arbeitszeit)} / {als_stunden(woche.soll)}")
        self._werte["wochenrest"].setText(als_stunden(woche.rest))

        hinweise = [f"Soll heute {als_stunden(tag.soll)}", f"Saldo {als_stunden(tag.saldo)}"]
        if tag.pausenabzug > timedelta(0):
            hinweise.append(f"Pflichtpause {als_stunden(tag.pausenabzug)} abgezogen")
        self._fuss.setText(" -- ".join(hinweise))
