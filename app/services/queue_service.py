"""Arbeitsvorrat: mehrere Angebote nacheinander abarbeiten.

Warum ueberhaupt?
-----------------
Im Alltag kommen selten einzelne Angebote.  Nach dem Wochenende liegen fuenf
Preisanpassungen im Postfach.  Bisher musste jede Datei einzeln geoeffnet,
verarbeitet und wieder geschlossen werden -- und nach jedem Angebot begann die
Klickfolge von vorn.

Der Arbeitsvorrat haelt die Liste, merkt sich pro Angebot den Stand und
schaltet nach dem Abschluss automatisch weiter.  Bewusst *kein*
Vollautomatismus: Jedes Angebot wird weiterhin einzeln geprueft und
freigegeben.  Automatisiert wird das Blaettern, nicht das Entscheiden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..models.offer import Offer
from ..models.results import BatchSummary

logger = logging.getLogger(__name__)

__all__ = ["QueueEntry", "QueueState", "OfferQueue"]


class QueueState(str, Enum):
    """Bearbeitungsstand eines Angebots im Arbeitsvorrat."""

    PENDING = "pending"          # noch nicht geoeffnet
    LOADED = "loaded"            # eingelesen, in Bearbeitung
    FAILED_IMPORT = "failed"     # konnte nicht gelesen werden
    PROCESSED = "processed"      # in SAP verarbeitet
    SKIPPED = "skipped"          # vom Anwender uebersprungen

    @property
    def label(self) -> str:
        return {
            QueueState.PENDING: "offen",
            QueueState.LOADED: "in Bearbeitung",
            QueueState.FAILED_IMPORT: "nicht lesbar",
            QueueState.PROCESSED: "verarbeitet",
            QueueState.SKIPPED: "uebersprungen",
        }[self]

    @property
    def is_done(self) -> bool:
        return self in (QueueState.PROCESSED, QueueState.SKIPPED,
                        QueueState.FAILED_IMPORT)


@dataclass
class QueueEntry:
    """Ein Angebot im Arbeitsvorrat."""

    path: str
    state: QueueState = QueueState.PENDING
    offer: Offer | None = None
    summary: BatchSummary | None = None
    note: str = ""
    finished_at: datetime | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name if self.path else "Eingefuegter Text"

    @property
    def positions(self) -> int:
        return len(self.offer.positions) if self.offer else 0

    def result_text(self) -> str:
        """Kurzfassung fuer die Liste."""
        if self.state is QueueState.FAILED_IMPORT:
            return self.note or "nicht lesbar"
        if self.state is QueueState.PROCESSED and self.summary is not None:
            return (f"{self.summary.succeeded} erfolgreich"
                    + (f", {self.summary.failed} fehlgeschlagen"
                       if self.summary.failed else ""))
        if self.state is QueueState.SKIPPED:
            return self.note or "uebersprungen"
        if self.offer is not None:
            return f"{self.positions} Position(en)"
        return ""


class OfferQueue:
    """Verwaltet die Liste der abzuarbeitenden Angebote."""

    def __init__(self) -> None:
        self.entries: list[QueueEntry] = []
        self.current_index: int = -1

    # ------------------------------------------------------------------
    # Bestuecken
    # ------------------------------------------------------------------
    def add_paths(self, paths: list[str]) -> int:
        """Dateien aufnehmen.  Bereits enthaltene werden uebersprungen."""
        vorhanden = {entry.path for entry in self.entries}
        neu = 0
        for path in paths:
            if path in vorhanden:
                logger.debug("Bereits im Arbeitsvorrat: %s", path)
                continue
            self.entries.append(QueueEntry(path=path))
            vorhanden.add(path)
            neu += 1
        logger.info("%d Angebot(e) in den Arbeitsvorrat aufgenommen (gesamt %d)",
                    neu, len(self.entries))
        return neu

    def clear(self) -> None:
        self.entries.clear()
        self.current_index = -1

    def remove(self, index: int) -> bool:
        if not 0 <= index < len(self.entries):
            return False
        self.entries.pop(index)
        if index < self.current_index:
            self.current_index -= 1
        elif index == self.current_index:
            self.current_index = -1
        return True

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    @property
    def current(self) -> QueueEntry | None:
        if 0 <= self.current_index < len(self.entries):
            return self.entries[self.current_index]
        return None

    def next_pending_index(self) -> int:
        """Naechstes unbearbeitetes Angebot -- ``-1`` wenn keins mehr da ist."""
        for index, entry in enumerate(self.entries):
            if entry.state is QueueState.PENDING:
                return index
        return -1

    def select(self, index: int) -> QueueEntry | None:
        if not 0 <= index < len(self.entries):
            return None
        self.current_index = index
        return self.entries[index]

    # ------------------------------------------------------------------
    # Stand fortschreiben
    # ------------------------------------------------------------------
    def mark_loaded(self, index: int, offer: Offer) -> None:
        entry = self.select(index)
        if entry is None:
            return
        entry.offer = offer
        entry.state = QueueState.LOADED
        entry.note = ""

    def mark_import_failed(self, index: int, reason: str) -> None:
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        entry.state = QueueState.FAILED_IMPORT
        entry.note = reason
        entry.finished_at = datetime.now()
        logger.warning("Angebot nicht lesbar (%s): %s", entry.name, reason)

    def mark_processed(self, index: int, summary: BatchSummary) -> None:
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        entry.summary = summary
        entry.state = QueueState.PROCESSED
        entry.finished_at = datetime.now()

    def mark_skipped(self, index: int, note: str = "") -> None:
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        entry.state = QueueState.SKIPPED
        entry.note = note
        entry.finished_at = datetime.now()

    # ------------------------------------------------------------------
    # Kennzahlen
    # ------------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def done(self) -> int:
        return sum(1 for entry in self.entries if entry.state.is_done)

    @property
    def pending(self) -> int:
        return sum(1 for entry in self.entries
                   if entry.state is QueueState.PENDING)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def is_finished(self) -> bool:
        return bool(self.entries) and all(e.state.is_done for e in self.entries)

    def counts(self) -> dict[str, int]:
        ergebnis = {zustand: 0 for zustand in QueueState}
        for entry in self.entries:
            ergebnis[entry.state] += 1
        return {zustand.value: anzahl for zustand, anzahl in ergebnis.items()}

    def summary_line(self) -> str:
        """Eine Zeile fuer die Statusleiste."""
        if self.is_empty:
            return ""
        teile = [f"Arbeitsvorrat {self.done}/{self.total}"]
        verarbeitet = sum(1 for e in self.entries
                          if e.state is QueueState.PROCESSED)
        fehler = sum(1 for e in self.entries
                     if e.state is QueueState.FAILED_IMPORT)
        if verarbeitet:
            teile.append(f"{verarbeitet} verarbeitet")
        if fehler:
            teile.append(f"{fehler} nicht lesbar")
        if self.pending:
            teile.append(f"{self.pending} offen")
        return "   •   ".join(teile)

    def overall_result(self) -> str:
        """Abschlussmeldung, wenn der Vorrat leer gearbeitet ist."""
        verarbeitet = [e for e in self.entries if e.state is QueueState.PROCESSED]
        positionen = sum(e.summary.succeeded for e in verarbeitet
                         if e.summary is not None)
        fehlgeschlagen = sum(e.summary.failed for e in verarbeitet
                             if e.summary is not None)
        zeilen = [f"{len(verarbeitet)} von {self.total} Angebot(en) verarbeitet",
                  f"{positionen} Position(en) erfolgreich"]
        if fehlgeschlagen:
            zeilen.append(f"{fehlgeschlagen} Position(en) fehlgeschlagen")
        nicht_lesbar = [e for e in self.entries
                        if e.state is QueueState.FAILED_IMPORT]
        if nicht_lesbar:
            zeilen.append(f"{len(nicht_lesbar)} Datei(en) nicht lesbar: "
                          + ", ".join(e.name for e in nicht_lesbar[:5]))
        uebersprungen = [e for e in self.entries if e.state is QueueState.SKIPPED]
        if uebersprungen:
            zeilen.append(f"{len(uebersprungen)} uebersprungen")
        return "\n".join(zeilen)
