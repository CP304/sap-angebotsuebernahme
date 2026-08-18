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


class DiagnosisView(QWidget):
    """Siehe Modulkopf."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.check = SelfCheck(settings)
        self._worker: _SelftestWorker | None = None

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
