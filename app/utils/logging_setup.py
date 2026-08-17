"""Zentrale Logging-Konfiguration.

Technische Details gehen in die Logdatei, die GUI zeigt nur verstaendliche
Meldungen.  Zusaetzlich kann sich die GUI ueber ``GuiLogHandler`` an den
Log-Stream haengen (Seite "Protokoll").
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Callable
from pathlib import Path

_CONFIGURED = False

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-38s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class GuiLogHandler(logging.Handler):
    """Leitet Logsaetze an einen Callback (typischerweise ein Qt-Signal)."""

    def __init__(self, callback: Callable[[str, str], None], level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - GUI
        try:
            self._callback(record.levelname, self.format(record))
        except Exception:  # noqa: BLE001 - Logging darf die App nie stoppen
            pass


def setup_logging(log_dir: Path | str, level: int = logging.INFO, console: bool = True) -> Path:
    """Richtet Datei- und Konsolenlogging ein und liefert den Logdateipfad."""
    global _CONFIGURED

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "sap_angebotsuebernahme.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if _CONFIGURED:
        return log_file

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # Fremdbibliotheken duerfen das Log nicht zumuellen
    for noisy in ("PIL", "matplotlib", "fitz", "pymupdf"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info("Logging initialisiert -> %s", log_file)
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install_excepthook() -> None:
    """Unbehandelte Ausnahmen ins Log schreiben statt still zu sterben."""

    def _hook(exc_type, exc_value, exc_tb):  # pragma: no cover - Notfallpfad
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("unhandled").critical(
            "Unbehandelte Ausnahme", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _hook
