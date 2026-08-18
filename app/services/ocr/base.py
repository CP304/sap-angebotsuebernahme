"""Gemeinsame Datenstrukturen und Schnittstelle der OCR-Backends.

Grundsatz dieses Pakets
-----------------------
Eine Texterkennung liefert **immer** ein Ergebnis -- auch dann, wenn sie nichts
Sinnvolles gelesen hat.  Genau das macht sie im Einkauf gefaehrlich: eine ``8``
statt einer ``3`` im Preis kostet mehr Geld, als eine nicht erkannte Position
je kosten koennte.  Deshalb gilt hier durchgaengig:

* Jedes Wort traegt seine Konfidenz mit (``None`` = das Backend liefert keine).
* Nichts wird "korrigiert".  Es wird nie ``O`` zu ``0`` geraten, sondern der
  Verdacht gemeldet.
* Wortkoordinaten werden mitgefuehrt, wo das Backend sie hergibt.  Nur damit
  laesst sich aus einem Scan wieder eine *Tabelle* rekonstruieren statt eines
  Fliesstextes, in dem Menge und Preis verschmelzen.

Koordinatensystem
-----------------
Die Rechtecke in :class:`OcrWord` sind **Bildpixel** des uebergebenen Bildes.
Der Aufrufer (z. B. der PDF-Leser) rechnet sie in seine eigene Einheit um.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "OcrWord",
    "OcrResult",
    "OcrBackend",
    "mean_of",
]


@dataclass(slots=True)
class OcrWord:
    """Ein erkanntes Wort mit Position und Sicherheit.

    ``confidence`` ist ein Wert zwischen 0.0 und 1.0 oder ``None``, wenn das
    verwendete Backend keine Konfidenz liefert (die in Windows eingebaute
    Engine tut das zum Beispiel nicht).  ``None`` bedeutet ausdruecklich
    "unbekannt" -- nicht "gut".
    """

    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    confidence: float | None = None
    #: Seite/Bildnummer, aus der das Wort stammt (1-basiert)
    page: int = 1

    @property
    def is_low_confidence(self) -> bool:
        """Nur *bekannte* niedrige Werte gelten als niedrig."""
        return self.confidence is not None and self.confidence < 0.60

    def below(self, threshold: float) -> bool:
        """Liegt eine bekannte Konfidenz unterhalb der Schwelle?"""
        return self.confidence is not None and self.confidence < threshold


@dataclass
class OcrResult:
    """Ergebnis einer Texterkennung fuer *ein* Bild."""

    text: str = ""
    words: list[OcrWord] = field(default_factory=list)
    #: Mittlere Konfidenz aller Woerter mit bekanntem Wert, sonst ``None``
    mean_confidence: float | None = None
    backend_name: str = ""
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def add_warning(self, message: str) -> None:
        """Warnung aufnehmen (ohne Dubletten)."""
        if message and message not in self.warnings:
            self.warnings.append(message)

    def low_confidence_words(self, threshold: float) -> list[OcrWord]:
        """Alle Woerter mit *bekannter* Konfidenz unterhalb der Schwelle."""
        return [w for w in self.words if w.below(threshold)]

    @property
    def has_confidence(self) -> bool:
        """Liefert das Backend ueberhaupt Konfidenzwerte?"""
        return any(w.confidence is not None for w in self.words)

    @property
    def word_count(self) -> int:
        return len(self.words)

    def recompute_mean(self) -> None:
        """Mittlere Konfidenz aus den Woertern neu bestimmen."""
        self.mean_confidence = mean_of(w.confidence for w in self.words)


def mean_of(values) -> float | None:
    """Mittelwert ueber alle nicht-``None``-Werte (sonst ``None``)."""
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


@runtime_checkable
class OcrBackend(Protocol):
    """Schnittstelle, die jedes Backend erfuellen muss."""

    #: Kurzname zur Auswahl in den Einstellungen ("windows", "tesseract")
    name: str

    def is_available(self) -> bool:
        """Ist das Backend auf diesem Rechner einsatzbereit?

        Muss **immer** sauber ``True``/``False`` liefern und niemals werfen --
        auch nicht, wenn das zugehoerige Python-Paket fehlt.
        """
        ...

    def recognize(self, image_bytes: bytes, language: str) -> OcrResult:
        """Ein Bild (PNG/JPEG-Bytes) erkennen.

        Wirft nach Moeglichkeit nicht: Was nicht ging, steht als Klartext in
        :attr:`OcrResult.warnings`.
        """
        ...
