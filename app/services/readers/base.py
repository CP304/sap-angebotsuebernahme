"""Gemeinsame Datenstrukturen aller Dokumentenleser.

Ein Leser wandelt eine Datei in ein :class:`RawDocument` -- eine
formatunabhaengige Zwischendarstellung aus Text, Seiten, Tabellenbloecken und
Metadaten.  Erst die Extraktion (``app/services/extraction``) macht daraus ein
:class:`~app.models.offer.Offer`.

Wichtig: Ein Leser wirft nach Moeglichkeit **keine** Exception.  Was nicht
gelesen werden konnte, steht als Klartext in ``RawDocument.warnings`` und
landet spaeter als Hinweis in der Oberflaeche.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ...models.enums import SourceKind
from ...models.offer import EmailContext
from ...utils.parsing import normalize_whitespace

logger = logging.getLogger(__name__)

__all__ = ["TableBlock", "RawDocument", "DocumentReader"]


@dataclass
class TableBlock:
    """Ein rechteckiger Tabellenblock aus einer beliebigen Quelle.

    ``origin`` dokumentiert, wie der Block entstanden ist:

    ``"pdf-layout"``  aus Wortkoordinaten rekonstruiert
    ``"pdf-table"``   von PyMuPDF selbst erkannt
    ``"excel"``       Arbeitsblatt
    ``"html"``        HTML-Tabelle aus einer E-Mail
    ``"text-grid"``   aus Trennzeichen/Spaltenabstaenden im Klartext
    """

    rows: list[list[str]] = field(default_factory=list)
    page: int = 0
    origin: str = "text-grid"
    header_row_index: int | None = None
    #: Frei belegbare Zusatzinfo (Blattname, Seitentitel, ...)
    title: str = ""

    # ------------------------------------------------------------------
    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def cell(self, row: int, column: int) -> str:
        """Zellzugriff ohne IndexError."""
        if 0 <= row < len(self.rows) and 0 <= column < len(self.rows[row]):
            return self.rows[row][column]
        return ""

    def normalized(self) -> "TableBlock":
        """Kopie mit getrimmten Zellen, ohne komplett leere Zeilen/Spalten."""
        rows = [[normalize_whitespace(c) for c in row] for row in self.rows]
        width = max((len(r) for r in rows), default=0)
        rows = [r + [""] * (width - len(r)) for r in rows]
        keep = [i for i in range(width) if any(r[i] for r in rows)]
        rows = [[r[i] for i in keep] for r in rows]
        header = self.header_row_index
        cleaned: list[list[str]] = []
        for index, row in enumerate(rows):
            if any(cell for cell in row):
                cleaned.append(row)
            elif header is not None and index < header:
                header -= 1
        return TableBlock(rows=cleaned, page=self.page, origin=self.origin,
                          header_row_index=header, title=self.title)

    def as_text(self) -> str:
        return "\n".join(" | ".join(row) for row in self.rows)


@dataclass
class RawDocument:
    """Rohdaten einer Quelldatei, formatunabhaengig."""

    source_path: str = ""
    source_kind: SourceKind = SourceKind.TEXT
    text: str = ""
    pages: list[str] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    email: EmailContext | None = None
    attachments: list["RawDocument"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def add_warning(self, message: str) -> None:
        """Warnung aufnehmen (ohne Dubletten)."""
        if message and message not in self.warnings:
            self.warnings.append(message)
            logger.debug("Dokumentwarnung (%s): %s", self.display_name, message)

    @property
    def display_name(self) -> str:
        if self.source_path:
            return Path(self.source_path).name
        if self.email is not None:
            return self.email.subject or "E-Mail"
        return "Dokument"

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.tables or self.attachments)

    def iter_documents(self) -> Iterator["RawDocument"]:
        """Dieses Dokument und rekursiv alle Anhaenge."""
        yield self
        for attachment in self.attachments:
            yield from attachment.iter_documents()

    def all_text(self) -> str:
        """Text des Dokuments inklusive aller Tabellenbloecke."""
        parts = [self.text]
        parts.extend(table.as_text() for table in self.tables)
        return "\n".join(p for p in parts if p)

    def largest_table(self) -> TableBlock | None:
        if not self.tables:
            return None
        return max(self.tables, key=lambda t: (t.row_count, t.column_count))


class DocumentReader(ABC):
    """Basisklasse aller Leser."""

    #: Von diesem Leser unterstuetzte Endungen (Kleinschreibung, mit Punkt)
    extensions: tuple[str, ...] = ()

    def can_read(self, path: str) -> bool:
        """Kann dieser Leser die Datei verarbeiten? (Endungsbasiert)"""
        return Path(path).suffix.lower() in self.extensions

    @abstractmethod
    def read(self, path: str) -> RawDocument:
        """Datei einlesen.  Muss immer ein Dokument liefern, nie werfen."""

    # ------------------------------------------------------------------
    def _empty(self, path: str, kind: SourceKind, warning: str = "") -> RawDocument:
        """Hilfsmittel: leeres Dokument mit Warnung (statt Exception)."""
        document = RawDocument(source_path=str(path), source_kind=kind)
        if warning:
            document.add_warning(warning)
        return document
