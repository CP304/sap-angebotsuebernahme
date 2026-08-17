"""Rueckgaengig/Wiederherstellen -- ausschliesslich VOR dem SAP-Zugriff.

Bewusste Einschraenkung
-----------------------
Rueckgaengig gemacht werden koennen nur Bearbeitungen in der Tabelle
(Preis korrigiert, Position abgewaehlt, Lieferant zugeordnet ...).  Was in SAP
steht, bleibt in SAP: Ein Werkzeug, das per Strg+Z scheinbar einen Infosatz
zurueckdreht, wuerde einen Zustand vorgaukeln, den es nicht herstellen kann.

Deshalb wird die Historie nach einem Schreibvorgang geleert
(:meth:`clear` bzw. :meth:`lock_after_write`), ``can_undo`` liefert dann
``False`` und ``blocked_reason`` nennt den Grund im Klartext.

Arbeitsweise
------------
Es werden vollstaendige Kopien des Angebots (``copy.deepcopy``) in einer
linearen Historie gehalten.  Der Aufrufer legt nach jeder Bearbeitung einen
Schnappschuss an; ``undo()``/``redo()`` liefern den jeweils passenden Stand
zurueck, den die Oberflaeche uebernimmt.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..models.offer import Offer

logger = logging.getLogger(__name__)

__all__ = ["UndoService", "UndoStep", "WRITE_LOCK_REASON"]

#: Standardbegruendung nach einem SAP-Schreibvorgang
WRITE_LOCK_REASON = ("Nach dem Schreiben in SAP ist kein Rückgängig mehr möglich – "
                     "die Änderungen stehen bereits im System.")

#: Begruendung, wenn schlicht nichts da ist
EMPTY_REASON = "Es gibt keinen früheren Bearbeitungsstand."


@dataclass
class UndoStep:
    """Ein gespeicherter Bearbeitungsstand."""

    label: str
    offer: Offer
    created_at: datetime = field(default_factory=datetime.now)

    def display(self) -> str:
        return f"{self.label} ({self.created_at.strftime('%H:%M:%S')})"


class UndoService:
    """Lineare Undo/Redo-Historie fuer die Angebotsbearbeitung."""

    def __init__(self, depth: int = 50) -> None:
        self.depth = max(1, int(depth))
        self._steps: list[UndoStep] = []
        self._index: int = -1
        self._blocked_reason: str = ""

    # ------------------------------------------------------------------
    # Historie fuellen
    # ------------------------------------------------------------------
    def snapshot(self, offer: Offer, label: str = "Bearbeitung") -> UndoStep:
        """Aktuellen Stand sichern.  Ein offener Redo-Zweig wird verworfen."""
        if self._blocked_reason:
            # Nach einem Schreibvorgang beginnt die Historie neu.
            self._blocked_reason = ""
            self._steps = []
            self._index = -1

        if self._index < len(self._steps) - 1:
            self._steps = self._steps[: self._index + 1]

        step = UndoStep(label=label, offer=copy.deepcopy(offer))
        self._steps.append(step)

        if len(self._steps) > self.depth:
            overflow = len(self._steps) - self.depth
            self._steps = self._steps[overflow:]
        self._index = len(self._steps) - 1
        logger.debug("Schnappschuss '%s' gesichert (%d/%d).", label,
                     self._index + 1, len(self._steps))
        return step

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return not self._blocked_reason and self._index > 0

    @property
    def can_redo(self) -> bool:
        return not self._blocked_reason and 0 <= self._index < len(self._steps) - 1

    def undo(self) -> Offer | None:
        """Vorherigen Stand liefern (oder ``None``, wenn es keinen gibt)."""
        if not self.can_undo:
            logger.debug("Rückgängig nicht möglich: %s", self.blocked_reason)
            return None
        self._index -= 1
        step = self._steps[self._index]
        logger.info("Rückgängig: zurück zu '%s'.", step.label)
        return copy.deepcopy(step.offer)

    def redo(self) -> Offer | None:
        """Naechsten Stand liefern (oder ``None``)."""
        if not self.can_redo:
            return None
        self._index += 1
        step = self._steps[self._index]
        logger.info("Wiederherstellen: vor zu '%s'.", step.label)
        return copy.deepcopy(step.offer)

    def current(self) -> Offer | None:
        """Aktuell eingestellter Stand (ohne die Historie zu bewegen)."""
        if 0 <= self._index < len(self._steps):
            return copy.deepcopy(self._steps[self._index].offer)
        return None

    # ------------------------------------------------------------------
    # Zuruecksetzen / Sperren
    # ------------------------------------------------------------------
    def clear(self, reason: str = "") -> None:
        """Historie verwerfen.  ``reason`` erscheint als ``blocked_reason``."""
        self._steps = []
        self._index = -1
        self._blocked_reason = reason
        logger.info("Undo-Historie geleert%s", f": {reason}" if reason else ".")

    def lock_after_write(self, reason: str = WRITE_LOCK_REASON) -> None:
        """Nach dem SAP-Schreibvorgang aufrufen -- sperrt Undo mit Begruendung."""
        self.clear(reason)

    # ------------------------------------------------------------------
    # Anzeige
    # ------------------------------------------------------------------
    @property
    def blocked_reason(self) -> str:
        """Warum ist Rueckgaengig gerade nicht moeglich?  Leer = es geht."""
        if self._blocked_reason:
            return self._blocked_reason
        if not self.can_undo:
            return EMPTY_REASON
        return ""

    @property
    def undo_label(self) -> str:
        if not self.can_undo:
            return "Rückgängig"
        return f"Rückgängig: {self._steps[self._index].label}"

    @property
    def redo_label(self) -> str:
        if not self.can_redo:
            return "Wiederherstellen"
        return f"Wiederherstellen: {self._steps[self._index + 1].label}"

    @property
    def steps(self) -> list[str]:
        """Beschriftungen der Historie -- fuer ein Aufklappmenue."""
        return [step.display() for step in self._steps]

    def __len__(self) -> int:
        return len(self._steps)
