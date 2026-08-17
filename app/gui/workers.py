"""Hintergrundverarbeitung.

Die Oberflaeche darf waehrend SAP-Zugriffen nie einfrieren.  Alle langsamen
Vorgaenge -- Angebotsimport, SAP-Lesen, Batchverarbeitung -- laufen deshalb in
eigenen ``QThread``s und melden ihren Fortschritt ueber Signale zurueck.

Wichtig: In den Threads wird **keine** GUI angefasst.  Sie arbeiten
ausschliesslich auf Datenmodellen und Services.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class _BaseWorker(QThread):
    """Gemeinsames Verhalten: Abbruchwunsch und Fehlerweiterleitung."""

    failed = Signal(str, str)          # (verstaendliche Meldung, technisches Detail)
    message = Signal(str)              # Statuszeile

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        """Abbruch anfordern (wird an definierten Stellen ausgewertet)."""
        self._cancelled = True
        logger.info("Abbruch angefordert: %s", type(self).__name__)

    def is_cancelled(self) -> bool:
        return self._cancelled

    def _fail(self, message: str, exc: Exception | None = None) -> None:
        detail = "".join(traceback.format_exception(exc)) if exc else ""
        logger.error("%s: %s", type(self).__name__, message, exc_info=exc)
        self.failed.emit(message, detail)


class ImportWorker(_BaseWorker):
    """Angebot(e) einlesen."""

    finished_ok = Signal(object)       # Offer

    def __init__(self, import_service: Any, paths: list[str] | None = None,
                 text: str = "", source_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.import_service = import_service
        self.paths = paths or []
        self.text = text
        self.source_name = source_name or "Eingefuegter Text"

    def run(self) -> None:  # noqa: D102
        try:
            if self.text:
                self.message.emit("Text wird ausgewertet ...")
                offer = self.import_service.import_text(self.text, self.source_name)
            elif len(self.paths) == 1:
                self.message.emit(f"{self.paths[0]} wird gelesen ...")
                offer = self.import_service.import_file(self.paths[0])
            else:
                self.message.emit(f"{len(self.paths)} Dateien werden gelesen ...")
                offer = self.import_service.import_files(self.paths)
            self.finished_ok.emit(offer)
        except FileNotFoundError as exc:
            self._fail(f"Die Datei wurde nicht gefunden: {exc}", exc)
        except PermissionError as exc:
            self._fail("Die Datei ist gesperrt (vermutlich in Excel geoeffnet). "
                       "Bitte schliessen und erneut versuchen.", exc)
        except Exception as exc:  # noqa: BLE001 - Import darf nie die App killen
            self._fail("Das Angebot konnte nicht gelesen werden. Bitte pruefen Sie das "
                       "Dateiformat.", exc)


class SapLoadWorker(_BaseWorker):
    """SAP-Ist-Zustand fuer die Positionen einsammeln (nur lesend)."""

    progress = Signal(int, int, str)   # (aktuell, gesamt, Beschriftung)
    position_loaded = Signal(int)      # position.uid
    finished_ok = Signal(int, int)     # (gelesen, uebersprungen)

    def __init__(self, gateway: Any, positions: list[Any], comparison: Any = None,
                 validation: Any = None, offer: Any = None, parent=None) -> None:
        super().__init__(parent)
        self.gateway = gateway
        self.positions = positions
        self.comparison = comparison
        self.validation = validation
        self.offer = offer

    def run(self) -> None:  # noqa: D102
        loaded = 0
        skipped = 0
        total = len(self.positions)
        try:
            for index, position in enumerate(self.positions, start=1):
                if self.is_cancelled():
                    self.message.emit("SAP-Abgleich abgebrochen.")
                    break
                label = position.display_name
                self.progress.emit(index, total, label)

                if not position.material_number or not position.vendor_number:
                    skipped += 1
                    continue
                try:
                    self.gateway.load_position_state(position)
                    if self.comparison is not None:
                        self.comparison.compare_position(position)
                    if self.validation is not None:
                        self.validation.validate_position(position, self.offer)
                    loaded += 1
                    self.position_loaded.emit(position.uid)
                except Exception as exc:  # noqa: BLE001 - Fehler pro Position isolieren
                    skipped += 1
                    logger.warning("SAP-Daten fuer %s nicht lesbar: %s", label, exc)
                    self.message.emit(f"{label}: {exc}")
            self.finished_ok.emit(loaded, skipped)
        except Exception as exc:  # noqa: BLE001
            self._fail("Der SAP-Abgleich konnte nicht durchgefuehrt werden.", exc)


class BatchWorker(_BaseWorker):
    """Ausgewaehlte Positionen in SAP verarbeiten (Komplettvorgang)."""

    progress = Signal(object)          # ProgressEvent
    finished_ok = Signal(object)       # BatchSummary

    def __init__(self, processor: Any, offer: Any, preview: Any, parent=None) -> None:
        super().__init__(parent)
        self.processor = processor
        self.offer = offer
        self.preview = preview

    def run(self) -> None:  # noqa: D102
        try:
            summary = self.processor.run(
                self.offer,
                self.preview,
                progress=self.progress.emit,
                is_cancelled=self.is_cancelled,
            )
            self.finished_ok.emit(summary)
        except Exception as exc:  # noqa: BLE001
            self._fail("Die Verarbeitung wurde wegen eines unerwarteten Fehlers "
                       "abgebrochen. In SAP ist moeglicherweise ein Vorgang offen.", exc)


class ConnectWorker(_BaseWorker):
    """SAP-Verbindung aufbauen (kann bei haengender GUI Sekunden dauern)."""

    finished_ok = Signal(object)       # ConnectionStatus

    def __init__(self, gateway: Any, parent=None) -> None:
        super().__init__(parent)
        self.gateway = gateway

    def run(self) -> None:  # noqa: D102
        try:
            status = self.gateway.connect()
            self.finished_ok.emit(status)
        except Exception as exc:  # noqa: BLE001
            message = getattr(exc, "message", None) or str(exc)
            detail = getattr(exc, "detail", "") or "".join(traceback.format_exception(exc))
            logger.error("SAP-Verbindung fehlgeschlagen: %s", message)
            self.failed.emit(message, detail)
