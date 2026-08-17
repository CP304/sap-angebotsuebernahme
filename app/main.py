"""Startpunkt der Anwendung.

    python -m app.main          (aus dem Projektverzeichnis)
    python app/main.py

Zusaetzliche Startoptionen:

    --mock / --real     Testsystem oder echtes SAP
    --dry-run / --write Dry Run erzwingen bzw. abschalten
    --datei PFAD        Angebot direkt beim Start oeffnen
    --debug             ausfuehrliches Logging
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Direkter Skriptstart (python app/main.py) -- Projektwurzel in den Pfad legen
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import build_services                      # noqa: E402
from app.config.settings import Settings                      # noqa: E402
from app.utils.logging_setup import install_excepthook, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="SAP-Angebotsuebernahme",
        description="Halbautomatische Uebernahme von Lieferantenangeboten in SAP.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true",
                      help="Testsystem verwenden (kein echtes SAP)")
    mode.add_argument("--real", action="store_true",
                      help="Echtes SAP verwenden")
    write = parser.add_mutually_exclusive_group()
    write.add_argument("--dry-run", action="store_true",
                       help="Nichts schreiben, nur simulieren")
    write.add_argument("--write", action="store_true",
                       help="Schreiben zulassen (Vorsicht)")
    parser.add_argument("--datei", "--file", dest="file", default="",
                        help="Angebot beim Start oeffnen")
    parser.add_argument("--debug", action="store_true", help="Ausfuehrliches Logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)

    settings = Settings.load()
    if arguments.mock:
        settings.use_mock_sap = True
    if arguments.real:
        settings.use_mock_sap = False
    if arguments.dry_run:
        settings.dry_run = True
    if arguments.write:
        settings.dry_run = False
    settings.ensure_dirs()

    log_file = setup_logging(settings.log_dir,
                             level=logging.DEBUG if arguments.debug else logging.INFO)
    install_excepthook()
    logger.info("Start – Betriebsart: %s, Dry Run: %s",
                "Testsystem" if settings.use_mock_sap else "echtes SAP", settings.dry_run)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 ist nicht installiert.\n\n"
              "Bitte installieren mit:\n    pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    from app.gui.main_window import MainWindow, apply_application_style

    application = QApplication(sys.argv[:1])
    application.setApplicationName("SAP-Angebotsuebernahme")
    application.setOrganizationName("Einkauf")
    apply_application_style(application, settings)

    services = build_services(settings)
    window = MainWindow(settings, services.as_dict())
    window.show()

    if arguments.file:
        path = Path(arguments.file)
        if path.is_file():
            window.open_offers([str(path)])
        else:
            logger.error("Datei nicht gefunden: %s", path)

    logger.info("Logdatei: %s", log_file)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
