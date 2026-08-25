"""Hauptfenster: Uebersicht, Einstellungen und Export.

Das Fenster ist der seltene Fall -- im Alltag genuegen der Hotkey und das
Symbol im Infobereich.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import autostart
from ..config import WOCHENTAG_SCHLUESSEL
from ..excel_export import WOCHENTAGE, exportieren
from ..rules import als_stunden, als_uhrzeit, wochenbeginn
from ..storage import ARBEITSTAG, TAGESART_TEXT
from ..tracker import Zeiterfassung
from .absence_dialog import AbwesenheitsDialog
from .day_editor import TagesDialog


class Hauptfenster(QWidget):
    """Tagesuebersicht, Wocheninformation, Einstellungen und Excel-Export."""

    def __init__(self, zeiterfassung: Zeiterfassung) -> None:
        super().__init__()
        self.zeiterfassung = zeiterfassung
        self.einstellungen = zeiterfassung.einstellungen
        self.setWindowTitle("Zeiterfassung")
        self.resize(940, 700)
        self._tage: list = []

        aufbau = QVBoxLayout(self)
        self._kopf = QLabel()
        self._kopf.setStyleSheet("font-size: 15px; font-weight: 600;")
        aufbau.addWidget(self._kopf)

        aufbau.addWidget(self._bereich_zeitraum())
        self._tabelle = QTableWidget(0, 10)
        self._tabelle.setHorizontalHeaderLabels(
            [
                "Datum", "Wochentag", "Art", "Kommen", "Gehen", "Anwesend",
                "Pause", "Ist", "Saldo", "Nachweis",
            ]
        )
        self._tabelle.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._tabelle.verticalHeader().setVisible(False)
        self._tabelle.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tabelle.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tabelle.doubleClicked.connect(self._tag_bearbeiten)
        aufbau.addWidget(self._tabelle, 1)
        hinweis = QLabel(
            "Doppelklick auf einen Tag: Buchungen korrigieren, nachtragen oder die "
            "Tagesart setzen.  Die Spalte \"Nachweis\" zeigt, woher die Zeiten des "
            "Tages stammen; Eingriffe von Hand verlangen eine Begruendung und "
            "stehen im Excel-Blatt \"Protokoll\"."
        )
        hinweis.setWordWrap(True)
        hinweis.setStyleSheet("color: #666;")
        aufbau.addWidget(hinweis)

        aufbau.addLayout(self._knopfzeile())

        aufbau.addWidget(self._bereich_einstellungen())
        self.aktualisieren()

    # -- Aufbau -------------------------------------------------------------
    def _knopfzeile(self) -> QHBoxLayout:
        zeile = QHBoxLayout()
        for beschriftung, aktion in (
            ("Tag bearbeiten...", self._tag_bearbeiten),
            ("Urlaub / Krank / Feiertag...", self._abwesenheit_eintragen),
        ):
            knopf = QPushButton(beschriftung)
            knopf.clicked.connect(aktion)
            zeile.addWidget(knopf)
        zeile.addStretch(1)
        return zeile


    def _bereich_zeitraum(self) -> QWidget:
        kasten = QGroupBox("Zeitraum und Export")
        zeile = QHBoxLayout(kasten)

        heute = date.today()
        self._von = QDateEdit(QDate(wochenbeginn(heute)), calendarPopup=True)
        self._bis = QDateEdit(QDate(heute), calendarPopup=True)
        for feld in (self._von, self._bis):
            feld.setDisplayFormat("dd.MM.yyyy")
            feld.dateChanged.connect(self.aktualisieren)

        self._schnellwahl = QComboBox()
        self._schnellwahl.addItems(["Diese Woche", "Letzte Woche", "Dieser Monat", "Letzte 30 Tage"])
        self._schnellwahl.currentIndexChanged.connect(self._schnellwahl_anwenden)

        knopf = QPushButton("Als Excel exportieren")
        knopf.clicked.connect(self.export_starten)

        zeile.addWidget(QLabel("von"))
        zeile.addWidget(self._von)
        zeile.addWidget(QLabel("bis"))
        zeile.addWidget(self._bis)
        zeile.addWidget(self._schnellwahl)
        zeile.addStretch(1)
        zeile.addWidget(knopf)
        return kasten

    def _bereich_einstellungen(self) -> QWidget:
        kasten = QGroupBox("Einstellungen")
        formular = QFormLayout(kasten)

        self._autostart = QCheckBox("Beim Anmelden automatisch starten")
        self._autostart.setChecked(autostart.ist_eingerichtet() or self.einstellungen.autostart)
        self._autostart.toggled.connect(self._autostart_umschalten)
        formular.addRow(self._autostart)

        self._pausen = QCheckBox("Pflichtpausen nach ArbZG automatisch abziehen (30/45 Minuten)")
        self._pausen.setChecked(self.einstellungen.pausen_automatik)
        self._pausen.toggled.connect(self._pausen_umschalten)
        formular.addRow(self._pausen)

        self._wochensoll = QDoubleSpinBox()
        self._wochensoll.setRange(0, 60)
        self._wochensoll.setSingleStep(0.5)
        self._wochensoll.setSuffix(" h/Woche")
        self._wochensoll.setValue(self.einstellungen.wochen_soll)
        self._wochensoll.valueChanged.connect(self._wochensoll_aendern)
        formular.addRow("Wochenarbeitszeit", self._wochensoll)

        formular.addRow(
            "Hotkey",
            QLabel(f"<b>{self.einstellungen.hotkey}</b> gedrueckt halten zeigt das Mini-Fenster"),
        )
        formular.addRow(
            "Datenablage", QLabel(str(self.zeiterfassung.db.pfad))
        )
        return kasten

    # -- Aktionen -----------------------------------------------------------
    def _schnellwahl_anwenden(self, index: int) -> None:
        heute = date.today()
        if index == 0:
            von, bis = wochenbeginn(heute), heute
        elif index == 1:
            montag = wochenbeginn(heute) - timedelta(days=7)
            von, bis = montag, montag + timedelta(days=6)
        elif index == 2:
            von, bis = heute.replace(day=1), heute
        else:
            von, bis = heute - timedelta(days=29), heute
        self._von.setDate(QDate(von))
        self._bis.setDate(QDate(bis))

    def _gewaehlter_tag(self) -> date:
        zeile = self._tabelle.currentRow()
        if 0 <= zeile < len(self._tage):
            return self._tage[zeile].tag
        return min(date.today(), self._bis.date().toPython())

    def _tag_bearbeiten(self) -> None:
        TagesDialog(self.zeiterfassung, self._gewaehlter_tag(), self).exec()
        self.aktualisieren()

    def abwesenheit_eintragen(self) -> None:
        """Urlaub, Krankheit oder Feiertag fuer einen Zeitraum eintragen."""
        self._abwesenheit_eintragen()

    def _abwesenheit_eintragen(self) -> None:
        dialog = AbwesenheitsDialog(self._gewaehlter_tag(), self)
        if dialog.exec() != AbwesenheitsDialog.Accepted:
            return
        von, bis, art, anteil, notiz = dialog.ergebnis()
        anzahl = self.zeiterfassung.tagesart_setzen(von, bis, art, anteil, notiz)
        beschriftung = TAGESART_TEXT.get(art, art)
        if art == ARBEITSTAG:
            meldung = f"{anzahl} Tage auf Arbeitstag zurueckgesetzt."
        else:
            meldung = f"{beschriftung}: {anzahl} Tage eingetragen (Wochenenden uebersprungen)."
        QMessageBox.information(self, "Abwesenheit", meldung)
        self.aktualisieren()

    def _autostart_umschalten(self, an: bool) -> None:
        erfolg, meldung = autostart.einrichten() if an else autostart.entfernen()
        self.einstellungen.autostart = an and erfolg
        self.einstellungen.speichern()
        if not erfolg:
            QMessageBox.warning(self, "Autostart", meldung)
            self._autostart.blockSignals(True)
            self._autostart.setChecked(autostart.ist_eingerichtet())
            self._autostart.blockSignals(False)

    def _pausen_umschalten(self, an: bool) -> None:
        self.einstellungen.pausen_automatik = an
        self.einstellungen.speichern()
        self.aktualisieren()

    def _wochensoll_aendern(self, wert: float) -> None:
        self.einstellungen.wochen_soll = wert
        # Gleichmaessig auf die bisherigen Arbeitstage verteilen.
        arbeitstage = [t for t in WOCHENTAG_SCHLUESSEL if self.einstellungen.tages_soll.get(t, 0) > 0]
        if arbeitstage:
            je_tag = round(wert / len(arbeitstage), 2)
            for tag in arbeitstage:
                self.einstellungen.tages_soll[tag] = je_tag
        self.einstellungen.speichern()
        self.aktualisieren()

    def zeitraum_diese_woche(self) -> None:
        """Setzt den Zeitraum auf die laufende Woche (fuer das Tray-Menue)."""
        self._schnellwahl.setCurrentIndex(0)
        self._schnellwahl_anwenden(0)

    def export_starten(self) -> None:
        von = self._von.date().toPython()
        bis = self._bis.date().toPython()
        vorschlag = Path(
            self.einstellungen.export_verzeichnis or Path.home() / "Documents"
        ) / f"Zeiterfassung_{von:%Y-%m-%d}_bis_{bis:%Y-%m-%d}.xlsx"
        pfad, _ = QFileDialog.getSaveFileName(
            self, "Zeiterfassung exportieren", str(vorschlag), "Excel-Arbeitsmappe (*.xlsx)"
        )
        if not pfad:
            return
        try:
            ziel = exportieren(self.zeiterfassung, von, bis, pfad)
        except OSError as fehler:
            QMessageBox.critical(
                self,
                "Export",
                "Die Datei konnte nicht geschrieben werden.\n"
                "Ist sie noch in Excel geoeffnet?\n\n"
                f"{fehler}",
            )
            return
        self.einstellungen.export_verzeichnis = str(ziel.parent)
        self.einstellungen.speichern()
        QMessageBox.information(self, "Export", f"Gespeichert unter:\n{ziel}")

    # -- Anzeige ------------------------------------------------------------
    def aktualisieren(self) -> None:
        jetzt = datetime.now()
        heute = self.zeiterfassung.tag(jetzt=jetzt)
        woche = self.zeiterfassung.woche(jetzt=jetzt)
        self._kopf.setText(
            f"Heute {als_stunden(heute.arbeitszeit)} von {als_stunden(heute.soll)}"
            f"   |   noch {als_stunden(heute.rest)}"
            f"   |   Woche {als_stunden(woche.arbeitszeit)} von {als_stunden(woche.soll)}"
            f"   |   Saldo {als_stunden(woche.saldo)}"
        )

        von = self._von.date().toPython()
        bis = self._bis.date().toPython()
        if bis < von:
            self._tage = []
            self._tabelle.setRowCount(0)
            return
        self._tage = self.zeiterfassung.zeitraum(von, bis, jetzt=jetzt)
        self._tabelle.setRowCount(len(self._tage))
        for zeile, tag in enumerate(self._tage):
            werte = [
                f"{tag.tag:%d.%m.%Y}",
                WOCHENTAGE[tag.tag.weekday()],
                tag.art_beschriftung,
                als_uhrzeit(tag.erste_anmeldung),
                als_uhrzeit(tag.letzter_kontakt),
                als_stunden(tag.anwesenheit),
                als_stunden(tag.erfasste_pause + tag.pausenabzug),
                als_stunden(tag.arbeitszeit),
                als_stunden(tag.saldo),
                tag.nachweis,
            ]
            for spalte, text in enumerate(werte):
                eintrag = QTableWidgetItem(text)
                if 3 <= spalte <= 8:
                    eintrag.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if spalte == 9 and not tag.vollautomatisch:
                    eintrag.setToolTip(
                        "Enthaelt Zeiten, die nicht rein automatisch entstanden sind -- "
                        "Einzelheiten im Excel-Blatt \"Buchungen\"."
                    )
                if spalte == 8:
                    eintrag.setForeground(
                        Qt.darkGreen if tag.saldo >= timedelta(0) else Qt.red
                    )
                self._tabelle.setItem(zeile, spalte, eintrag)
