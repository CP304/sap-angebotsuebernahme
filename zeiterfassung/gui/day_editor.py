"""Tag von Hand korrigieren.

Die automatische Erfassung ist gut, aber nicht allwissend: der Rechner lief
in der Mittagspause weiter, das Programm wurde zu spaet gestartet, oder ein
halber Tag war Urlaub.  Hier laesst sich jede Buchung eines Tages aendern,
nachtragen und loeschen -- und die Tagesart setzen.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
)

from ..rules import als_stunden
from ..storage import (
    ABWESEND,
    ARBEIT,
    ARBEITSTAG,
    PAUSE,
    TAGESART_TEXT,
    Luecke,
    Sitzung,
)
from ..tracker import Zeiterfassung

LUECKENARTEN = ((ARBEIT, "Arbeit"), (PAUSE, "Pause"), (ABWESEND, "Nicht gearbeitet"))
ART_TEXT = dict(LUECKENARTEN)


class BuchungsDialog(QDialog):
    """Eine einzelne Buchung anlegen oder aendern."""

    def __init__(self, tag: date, buchung: Sitzung | Luecke | None = None, parent=None) -> None:
        super().__init__(parent)
        self.tag = tag
        self.buchung = buchung
        self.setWindowTitle("Buchung bearbeiten" if buchung else "Buchung nachtragen")

        aufbau = QVBoxLayout(self)
        zeile = QHBoxLayout()
        self._von = QTimeEdit(QTime(8, 0))
        self._bis = QTimeEdit(QTime(17, 0))
        for feld in (self._von, self._bis):
            feld.setDisplayFormat("HH:mm")
        self._art = QComboBox()
        self._art.addItem("Arbeit am Rechner", "sitzung")
        for schluessel, beschriftung in LUECKENARTEN:
            self._art.addItem(beschriftung, schluessel)

        zeile.addWidget(QLabel("von"))
        zeile.addWidget(self._von)
        zeile.addWidget(QLabel("bis"))
        zeile.addWidget(self._bis)
        zeile.addWidget(QLabel("als"))
        zeile.addWidget(self._art, 1)
        aufbau.addLayout(zeile)

        self._notiz = QLineEdit()
        self._notiz.setPlaceholderText("Bemerkung (freiwillig)")
        aufbau.addWidget(self._notiz)

        if isinstance(buchung, Sitzung):
            self._art.setCurrentIndex(0)
            self._art.setEnabled(False)  # eine Sitzung bleibt eine Sitzung
        elif isinstance(buchung, Luecke):
            self._art.setCurrentIndex(max(0, self._art.findData(buchung.art)))
            self._notiz.setText(buchung.notiz)
        if buchung is not None:
            self._von.setTime(QTime(buchung.beginn.hour, buchung.beginn.minute))
            self._bis.setTime(QTime(buchung.ende.hour, buchung.ende.minute))

        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.accepted.connect(self._pruefen_und_schliessen)
        knoepfe.rejected.connect(self.reject)
        aufbau.addWidget(knoepfe)

    def _zeiten(self) -> tuple[datetime, datetime]:
        beginn = datetime.combine(self.tag, time(self._von.time().hour(), self._von.time().minute()))
        ende = datetime.combine(self.tag, time(self._bis.time().hour(), self._bis.time().minute()))
        if ende <= beginn:
            # Ueber Mitternacht hinaus -- der naechste Tag ist gemeint.
            ende += timedelta(days=1)
        return beginn, ende

    def _pruefen_und_schliessen(self) -> None:
        beginn, ende = self._zeiten()
        if ende - beginn > timedelta(hours=20):
            QMessageBox.warning(self, "Buchung", "Mehr als 20 Stunden am Stueck sind unplausibel.")
            return
        self.accept()

    def ergebnis(self) -> tuple[str, datetime, datetime, str]:
        """(Art, Beginn, Ende, Notiz) -- Art ist ``sitzung`` oder eine Lueckenart."""
        beginn, ende = self._zeiten()
        return str(self._art.currentData()), beginn, ende, self._notiz.text().strip()


class TagesDialog(QDialog):
    """Alle Buchungen eines Tages und die Tagesart."""

    def __init__(self, zeiterfassung: Zeiterfassung, tag: date, parent=None) -> None:
        super().__init__(parent)
        self.zeiterfassung = zeiterfassung
        self.tag = tag
        self.setWindowTitle(f"Tag bearbeiten -- {tag:%d.%m.%Y}")
        self.resize(640, 460)

        aufbau = QVBoxLayout(self)
        self._kopf = QLabel()
        self._kopf.setStyleSheet("font-weight: 600;")
        aufbau.addWidget(self._kopf)

        art_zeile = QHBoxLayout()
        art_zeile.addWidget(QLabel("Tagesart"))
        self._tagesart = QComboBox()
        self._tagesart.addItem(TAGESART_TEXT[ARBEITSTAG], ARBEITSTAG)
        for schluessel, beschriftung in TAGESART_TEXT.items():
            if schluessel != ARBEITSTAG:
                self._tagesart.addItem(beschriftung, schluessel)
        self._anteil = QDoubleSpinBox()
        self._anteil.setRange(0.5, 1.0)
        self._anteil.setSingleStep(0.5)
        self._anteil.setValue(1.0)
        self._anteil.setPrefix("Anteil ")
        self._notiz = QLineEdit()
        self._notiz.setPlaceholderText("Bemerkung zum Tag")
        art_zeile.addWidget(self._tagesart)
        art_zeile.addWidget(self._anteil)
        art_zeile.addWidget(self._notiz, 1)
        aufbau.addLayout(art_zeile)

        self._tabelle = QTableWidget(0, 4)
        self._tabelle.setHorizontalHeaderLabels(["Von", "Bis", "Dauer", "Art"])
        self._tabelle.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._tabelle.verticalHeader().setVisible(False)
        self._tabelle.setSelectionBehavior(QTableWidget.SelectRows)
        self._tabelle.setEditTriggers(QTableWidget.NoEditTriggers)
        self._tabelle.doubleClicked.connect(self._bearbeiten)
        aufbau.addWidget(self._tabelle, 1)

        knopfzeile = QHBoxLayout()
        for beschriftung, aktion in (
            ("Nachtragen", self._nachtragen),
            ("Bearbeiten", self._bearbeiten),
            ("Loeschen", self._loeschen),
        ):
            knopf = QPushButton(beschriftung)
            knopf.clicked.connect(aktion)
            knopfzeile.addWidget(knopf)
        knopfzeile.addStretch(1)
        aufbau.addLayout(knopfzeile)

        schliessen = QDialogButtonBox(QDialogButtonBox.Close)
        schliessen.rejected.connect(self.accept)
        aufbau.addWidget(schliessen)

        self._tagesart_laden()
        self._tagesart.currentIndexChanged.connect(self._tagesart_speichern)
        self._anteil.valueChanged.connect(self._tagesart_speichern)
        self._notiz.editingFinished.connect(self._tagesart_speichern)
        self.aktualisieren()

    # -- Tagesart -----------------------------------------------------------
    def _tagesart_laden(self) -> None:
        vorhanden = self.zeiterfassung.db.tagesart(self.tag)
        self._tagesart.blockSignals(True)
        if vorhanden is None:
            self._tagesart.setCurrentIndex(0)
        else:
            self._tagesart.setCurrentIndex(max(0, self._tagesart.findData(vorhanden.art)))
            self._anteil.setValue(vorhanden.anteil)
            self._notiz.setText(vorhanden.notiz)
        self._tagesart.blockSignals(False)

    def _tagesart_speichern(self) -> None:
        art = str(self._tagesart.currentData())
        self.zeiterfassung.db.tagesart_setzen(
            self.tag, art, self._anteil.value(), self._notiz.text().strip()
        )
        self.aktualisieren()

    # -- Buchungen ----------------------------------------------------------
    def _ausgewaehlt(self) -> Sitzung | Luecke | None:
        zeile = self._tabelle.currentRow()
        if zeile < 0:
            return None
        return self._buchungen[zeile]

    def _nachtragen(self) -> None:
        dialog = BuchungsDialog(self.tag, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        art, beginn, ende, notiz = dialog.ergebnis()
        try:
            if art == "sitzung":
                self.zeiterfassung.sitzung_nachtragen(beginn, ende)
            else:
                self.zeiterfassung.luecke_nachtragen(beginn, ende, art, notiz)
        except ValueError as fehler:
            QMessageBox.warning(self, "Buchung", str(fehler))
            return
        self.aktualisieren()

    def _bearbeiten(self) -> None:
        buchung = self._ausgewaehlt()
        if buchung is None:
            QMessageBox.information(self, "Buchung", "Bitte zuerst eine Zeile auswaehlen.")
            return
        dialog = BuchungsDialog(self.tag, buchung, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        art, beginn, ende, notiz = dialog.ergebnis()
        try:
            if isinstance(buchung, Sitzung):
                self.zeiterfassung.sitzung_korrigieren(buchung.id, beginn, ende)
            else:
                self.zeiterfassung.luecke_korrigieren(buchung.id, beginn, ende, art, notiz)
        except ValueError as fehler:
            QMessageBox.warning(self, "Buchung", str(fehler))
            return
        self.aktualisieren()

    def _loeschen(self) -> None:
        buchung = self._ausgewaehlt()
        if buchung is None:
            QMessageBox.information(self, "Buchung", "Bitte zuerst eine Zeile auswaehlen.")
            return
        antwort = QMessageBox.question(
            self,
            "Buchung loeschen",
            f"{buchung.beginn:%H:%M} bis {buchung.ende:%H:%M} wirklich loeschen?",
        )
        if antwort != QMessageBox.Yes:
            return
        if isinstance(buchung, Sitzung):
            self.zeiterfassung.sitzung_verwerfen(buchung.id)
        else:
            self.zeiterfassung.luecke_verwerfen(buchung.id)
        self.aktualisieren()

    # -- Anzeige ------------------------------------------------------------
    def aktualisieren(self) -> None:
        self._buchungen = self.zeiterfassung.buchungen(self.tag)
        self._tabelle.setRowCount(len(self._buchungen))
        for zeile, buchung in enumerate(self._buchungen):
            if isinstance(buchung, Sitzung):
                art = "Arbeit am Rechner"
                if buchung.manuell:
                    art += " (von Hand)"
                elif buchung.laeuft:
                    art += " (laeuft)"
                elif buchung.ende_geschaetzt:
                    art += " (Ende geschaetzt)"
            else:
                art = ART_TEXT.get(buchung.art, buchung.art)
                if buchung.notiz:
                    art += f" -- {buchung.notiz}"
            werte = [
                f"{buchung.beginn:%H:%M}",
                f"{buchung.ende:%H:%M}",
                als_stunden(buchung.dauer),
                art,
            ]
            for spalte, text in enumerate(werte):
                eintrag = QTableWidgetItem(text)
                if spalte < 3:
                    eintrag.setTextAlignment(Qt.AlignCenter)
                self._tabelle.setItem(zeile, spalte, eintrag)

        werte = self.zeiterfassung.tag(self.tag)
        self._kopf.setText(
            f"{werte.art_beschriftung}  |  Ist {als_stunden(werte.arbeitszeit)}"
            f"  |  Soll {als_stunden(werte.soll)}"
            f"  |  Pause {als_stunden(werte.erfasste_pause + werte.pausenabzug)}"
            f"  |  Saldo {als_stunden(werte.saldo)}"
        )
        self._anteil.setEnabled(str(self._tagesart.currentData()) != ARBEITSTAG)
