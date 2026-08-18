"""Texterkennung (OCR) fuer eingescannte Angebote.

Wozu das gut ist
----------------
Ein Teil der Angebote kommt als Scan oder als Handyfoto -- ohne Textebene.
Bisher hat die Anwendung das korrekt erkannt und abgelehnt; der Anwender stand
danach aber ohne Loesung da.  Mit diesem Paket kann er die Positionen wenigstens
vorbefuellt bekommen, statt sie vollstaendig abzutippen.

Wozu es ausdruecklich NICHT gut ist
-----------------------------------
OCR-Werte sind **Vorschlaege**, keine Belege.  Es gilt derselbe Grundsatz wie
im ganzen Projekt: Es wird nie ein Wert erfunden -- und bei OCR heisst das, dass
jeder erkannte Wert als unsicher gekennzeichnet bleibt, bis ein Mensch ihn
gesehen hat.

Backends
--------
``windows``    Bordmittel von Windows (``Windows.Media.Ocr``).  Keine
               Zusatzinstallation ausser dem Python-Paket ``winsdk``, liefert
               dafuer keine Konfidenzwerte.
``tesseract``  Tesseract-OCR ueber ``pytesseract``.  Braucht zusaetzlich das
               Programm, liefert dafuer Konfidenzen je Wort.

Beide sind **optional**.  Fehlen sie, laeuft die Anwendung unveraendert weiter
und meldet verstaendlich, was zu tun waere (:func:`ocr_status_text`).
"""

from __future__ import annotations

import logging

from .base import OcrBackend, OcrResult, OcrWord, mean_of
from .quality import (
    CONFUSABLE_CHARACTERS,
    cell_confidences,
    confidence_percent,
    ocr_warning,
    suspicious_number,
    uncertain_cells,
)
from .tesseract_ocr import TesseractOcrBackend
from .windows_ocr import WindowsOcrBackend

logger = logging.getLogger(__name__)

__all__ = [
    "OcrBackend",
    "OcrResult",
    "OcrWord",
    "WindowsOcrBackend",
    "TesseractOcrBackend",
    "BACKENDS",
    "DEFAULT_ORDER",
    "all_backends",
    "backend_by_name",
    "available_backends",
    "best_backend",
    "ocr_status_text",
    "ocr_settings_of",
    "mean_of",
    "cell_confidences",
    "uncertain_cells",
    "suspicious_number",
    "ocr_warning",
    "confidence_percent",
    "CONFUSABLE_CHARACTERS",
    "NO_BACKEND_HINT",
]

#: Alle bekannten Backends nach Kurzname
BACKENDS: dict[str, type] = {
    WindowsOcrBackend.name: WindowsOcrBackend,
    TesseractOcrBackend.name: TesseractOcrBackend,
}

#: Reihenfolge, wenn die Einstellungen nichts anderes sagen
DEFAULT_ORDER: tuple[str, ...] = ("windows", "tesseract")

NO_BACKEND_HINT = (
    "Fuer die Texterkennung ist keine Engine installiert.  Moeglich sind:\n"
    f"  * {WindowsOcrBackend.install_hint}\n"
    f"  * {TesseractOcrBackend.install_hint}"
)


def ocr_settings_of(settings: object | None):
    """Die OCR-Einstellungen aus ``Settings``, ``OcrSettings`` oder ``None``.

    Bequemlichkeit fuer die Aufrufer: Sie duerfen die Gesamtkonfiguration
    uebergeben, nur den OCR-Teil -- oder gar nichts.
    """
    from ...config.settings import OcrSettings

    if settings is None:
        return OcrSettings()
    inner = getattr(settings, "ocr", None)
    if inner is not None:
        return inner
    if isinstance(settings, OcrSettings):
        return settings
    # Ente-Test: Alles, was die noetigen Felder hat, ist gut genug
    if hasattr(settings, "backend_order") and hasattr(settings, "enabled"):
        return settings
    return OcrSettings()


def backend_order(settings: object | None = None) -> list[str]:
    """Gewuenschte Reihenfolge der Backends (unbekannte Namen fliegen raus)."""
    ocr = ocr_settings_of(settings)
    order = list(getattr(ocr, "backend_order", None) or DEFAULT_ORDER)
    cleaned = [str(name).strip().lower() for name in order]
    known = [name for name in cleaned if name in BACKENDS]
    for name in DEFAULT_ORDER:
        if name not in known:
            known.append(name)
    return known


def all_backends(settings: object | None = None) -> list:
    """Je eine Instanz aller bekannten Backends in gewuenschter Reihenfolge."""
    return [BACKENDS[name]() for name in backend_order(settings)]


def backend_by_name(name: str):
    """Ein Backend anhand seines Kurznamens erzeugen (oder ``None``)."""
    factory = BACKENDS.get(str(name or "").strip().lower())
    return factory() if factory is not None else None


def available_backends(settings: object | None = None) -> list:
    """Alle auf diesem Rechner tatsaechlich einsatzbereiten Backends.

    Ein Backend, dessen Pruefung wirft, gilt als nicht verfuegbar -- eine
    fehlende Zusatzbibliothek darf niemals den Import einer Datei verhindern.
    """
    ready = []
    for backend in all_backends(settings):
        try:
            if backend.is_available():
                ready.append(backend)
        except Exception as exc:  # noqa: BLE001 -- doppelter Boden
            logger.debug("Backend '%s' meldet einen Fehler: %s", backend.name, exc)
    return ready


def best_backend(settings: object | None = None):
    """Das erste verfuegbare Backend gemaess Reihenfolge -- sonst ``None``.

    Liefert auch dann ``None``, wenn OCR in den Einstellungen abgeschaltet ist.
    Der Aufrufer muss also nicht zusaetzlich pruefen.
    """
    ocr = ocr_settings_of(settings)
    if not bool(getattr(ocr, "enabled", True)):
        logger.debug("OCR ist in den Einstellungen abgeschaltet")
        return None
    ready = available_backends(settings)
    return ready[0] if ready else None


def ocr_status_text(settings: object | None = None) -> str:
    """Verstaendliche Auskunft: Was ist da, was fehlt, was waere zu tun?"""
    ocr = ocr_settings_of(settings)
    if not bool(getattr(ocr, "enabled", True)):
        return ("Texterkennung ist in den Einstellungen abgeschaltet.  "
                "Gescannte Belege werden weiterhin nur gemeldet, nicht gelesen.")

    ready = available_backends(settings)
    if not ready:
        return NO_BACKEND_HINT

    names = ", ".join(getattr(b, "label", b.name) for b in ready)
    aktiv = ready[0]
    text = (f"Texterkennung einsatzbereit ueber: {names}.  "
            f"Verwendet wird: {getattr(aktiv, 'label', aktiv.name)}.")
    if aktiv.name == WindowsOcrBackend.name:
        text += ("  Hinweis: Diese Engine liefert keine Sicherheitswerte je Wort -- "
                 "alle uebernommenen Angaben sind ungeprueft.")
    fehlend = [BACKENDS[name] for name in backend_order(settings)
               if name not in {b.name for b in ready}]
    if fehlend:
        text += "\nNicht verfuegbar:\n" + "\n".join(
            f"  * {cls.label}: {cls.install_hint}" for cls in fehlend)
    return text
