"""Verwaltungsseite "Diagnose": Installation pruefen, ohne Konsole.

Die Seite zeigt zwei Dinge:

1. Die Umgebungspruefung (Pakete, OCR, Datenbank, SAP-Modus) -- laeuft beim
   Oeffnen automatisch, dauert unter einer Sekunde.
2. Den Selbsttest, der jede eingebaute Beispieldatei wirklich einliest --
   auf Knopfdruck, weil er einige Sekunden braucht.

Alles laesst sich als Text kopieren, damit ein Bericht per Mail verschickt
werden kann, wenn der Rechner selbst keinen Zugang hat.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..services.self_check import FAIL, WARN, SelfCheck

logger = logging.getLogger(__name__)

__all__ = ["DiagnosisView"]


class _SelftestWorker(QThread):
    """Der Import-Selbsttest liest echte Dateien -- nicht im GUI-Thread."""

    finished_with_report = Signal(str, int, int)  # Text, Fehler, Hinweise

    def __init__(self, check: SelfCheck, parent=None) -> None:
        super().__init__(parent)
        self._check = check

    def run(self) -> None:  # pragma: no cover - Thread-Huelle
        try:
            ergebnisse = self._check.run_import_selftest()
            fehler = sum(1 for e in ergebnisse if e.status == FAIL)
            hinweise = sum(1 for e in ergebnisse if e.status == WARN)
            text = "\n".join(e.display() for e in ergebnisse)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Selbsttest fehlgeschlagen")
            text, fehler, hinweise = f"Selbsttest fehlgeschlagen: {exc}", 1, 0
        self.finished_with_report.emit(text, fehler, hinweise)


class _StockCheckWorker(QThread):
    """Die eigenen Angebote lesen -- kann Minuten dauern, also nebenher."""

    progressed = Signal(int, int, str)
    finished_with_report = Signal(str, int, int)  # Text, Problemdateien, gesamt

    def __init__(self, settings, folder: str, recursive: bool, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._folder = folder
        self._recursive = recursive

    def run(self) -> None:  # pragma: no cover - Thread-Huelle
        from ..services.bestandspruefung import check_folder, report_text

        try:
            ergebnis = check_folder(
                self._settings, self._folder, recursive=self._recursive,
                progress=lambda n, gesamt, name: self.progressed.emit(n, gesamt, name))
            self.finished_with_report.emit(
                report_text(ergebnis), len(ergebnis.problem_files), ergebnis.total)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bestandspruefung fehlgeschlagen")
            self.finished_with_report.emit(
                f"Pruefung fehlgeschlagen: {exc}", 1, 0)


class DiagnosisView(QWidget):
    """Siehe Modulkopf."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.check = SelfCheck(settings)
        self._worker: _SelftestWorker | None = None
        self._stock_worker: _StockCheckWorker | None = None

        layout = QVBoxLayout(self)

        erklaerung = QLabel(
            "Prueft, ob diese Installation vollstaendig ist. Wenn Angebote "
            "\"nicht erkannt\" werden, zeigt diese Seite, ob es an der "
            "Installation liegt -- oder wirklich am Angebot.")
        erklaerung.setWordWrap(True)
        layout.addWidget(erklaerung)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        layout.addWidget(self.report, stretch=1)

        knopfzeile = QHBoxLayout()
        self.refresh_button = QPushButton("Umgebung erneut pruefen")
        self.refresh_button.clicked.connect(self.refresh)
        knopfzeile.addWidget(self.refresh_button)

        self.selftest_button = QPushButton("Selbsttest: Beispieldateien einlesen")
        self.selftest_button.setToolTip(
            "Liest jede eingebaute Beispieldatei komplett ein. 0 Positionen "
            "bei einer Beispieldatei heisst: die Installation ist beschaedigt, "
            "nicht Ihr Angebot.")
        self.selftest_button.clicked.connect(self.run_selftest)
        knopfzeile.addWidget(self.selftest_button)

        self.stock_button = QPushButton("Eigene Angebote pruefen ...")
        self.stock_button.setObjectName("Primary")
        self.stock_button.setToolTip(
            "Einen Ordner mit Ihren echten Angeboten waehlen. Jede Datei wird "
            "eingelesen und aufgelistet, wie viele Positionen herauskommen -- "
            "ohne irgendetwas nach SAP zu schreiben.\n\n"
            "So wissen Sie VORHER, woran Sie sind, statt es beim ersten "
            "Angebot herauszufinden.")
        self.stock_button.clicked.connect(self.run_stock_check)
        knopfzeile.addWidget(self.stock_button)

        self.copy_button = QPushButton("Bericht kopieren")
        self.copy_button.setToolTip(
            "Kompletten Bericht in die Zwischenablage -- z. B. fuer eine "
            "Support-Mail.")
        self.copy_button.clicked.connect(self._copy_report)
        knopfzeile.addWidget(self.copy_button)
        knopfzeile.addStretch(1)

        self.status_label = QLabel("")
        knopfzeile.addWidget(self.status_label)
        layout.addLayout(knopfzeile)

        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Umgebungspruefung ausfuehren und anzeigen."""
        try:
            ergebnisse = self.check.run_all()
            self.report.setPlainText(self.check.report_text(ergebnisse))
            fehler = sum(1 for e in ergebnisse if e.status == FAIL)
            hinweise = sum(1 for e in ergebnisse if e.status == WARN)
            self._update_status(fehler, hinweise)
        except Exception as exc:  # noqa: BLE001 - Diagnose darf nie crashen
            logger.exception("Umgebungspruefung fehlgeschlagen")
            self.report.setPlainText(f"Pruefung fehlgeschlagen: {exc}")
            self.status_label.setText("Pruefung fehlgeschlagen")

    def run_stock_check(self, folder: str = "") -> None:
        """Die eigenen Angebote eines Ordners durchpruefen.

        Beantwortet die Frage, die vor der ersten Uebernahme wirklich
        zaehlt: "liest er MEINE Belege?" -- und zwar an allen auf einmal,
        bevor der erste Preis nach SAP geht.  Es wird ausschliesslich
        gelesen: nichts nach SAP, nichts in die Historie.
        """
        if self._stock_worker is not None and self._stock_worker.isRunning():
            return
        if not folder:
            folder = QFileDialog.getExistingDirectory(
                self, "Ordner mit Ihren Angeboten waehlen")
        if not folder:
            return

        self.stock_button.setEnabled(False)
        self.status_label.setText("Angebote werden gelesen ...")
        self._stock_worker = _StockCheckWorker(self.settings, folder, False, self)
        self._stock_worker.progressed.connect(self._stock_progress)
        self._stock_worker.finished_with_report.connect(self._stock_done)
        self._stock_worker.start()

    def _stock_progress(self, nummer: int, gesamt: int, name: str) -> None:
        self.status_label.setText(f"{nummer}/{gesamt}: {name}")

    def _stock_done(self, text: str, probleme: int, gesamt: int) -> None:
        self.report.setPlainText(text)
        self.stock_button.setEnabled(True)
        if not gesamt:
            self.status_label.setText("Keine lesbare Datei im Ordner.")
        elif probleme:
            self.status_label.setText(
                f"{gesamt} Datei(en), {probleme} mit Nachholbedarf")
        else:
            self.status_label.setText(
                f"{gesamt} Datei(en) -- aus allen wurden Positionen erkannt")

    def run_selftest(self) -> None:
        """Import-Selbsttest im Hintergrund starten."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.selftest_button.setEnabled(False)
        self.status_label.setText("Selbsttest laeuft ...")
        self._worker = _SelftestWorker(self.check, self)
        self._worker.finished_with_report.connect(self._selftest_done)
        self._worker.start()

    def _selftest_done(self, text: str, fehler: int, hinweise: int) -> None:
        self.report.appendPlainText("\n" + "=" * 60 +
                                    "\nSelbsttest (Beispieldateien einlesen)\n" +
                                    "=" * 60 + "\n" + text)
        self.selftest_button.setEnabled(True)
        self._update_status(fehler, hinweise, prefix="Selbsttest: ")

    def _update_status(self, fehler: int, hinweise: int, prefix: str = "") -> None:
        if fehler:
            self.status_label.setText(f"{prefix}{fehler} Problem(e)")
        elif hinweise:
            self.status_label.setText(f"{prefix}{hinweise} Hinweis(e), kein Problem")
        else:
            self.status_label.setText(f"{prefix}alles in Ordnung")

    def _copy_report(self) -> None:
        QGuiApplication.clipboard().setText(self.report.toPlainText())
        self.status_label.setText("Bericht kopiert")
