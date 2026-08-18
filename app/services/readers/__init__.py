"""Dokumentenleser: Datei -> :class:`RawDocument`.

Die Registry :class:`ReaderRegistry` waehlt anhand der Dateiendung den
passenden Leser und verdrahtet den E-Mail-Leser so, dass er seine Anhaenge
rekursiv ueber dieselbe Registry einliest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...config.settings import ExtractionSettings
from ...models.enums import SourceKind
from .archive_reader import ArchiveReader
from .base import DocumentReader, RawDocument, TableBlock
from .email_reader import (
    EmailReader,
    HtmlTableParser,
    TextReader,
    html_to_text,
    strip_signature,
)
from .excel_reader import CsvReader, ExcelReader
from .image_reader import IMAGE_EXTENSIONS, ImageReader
from .office_reader import OpenDocumentReader, RichTextReader, WordReader
from .pdf_reader import PdfReader
from .spreadsheet_reader import OdsReader

logger = logging.getLogger(__name__)

__all__ = [
    "DocumentReader",
    "RawDocument",
    "TableBlock",
    "PdfReader",
    "ExcelReader",
    "CsvReader",
    "ImageReader",
    "EmailReader",
    "OpenDocumentReader",
    "OdsReader",
    "ArchiveReader",
    "RichTextReader",
    "TextReader",
    "WordReader",
    "HtmlTableParser",
    "ReaderRegistry",
    "html_to_text",
    "strip_signature",
    "read_document",
    "SUPPORTED_EXTENSIONS",
    "IMAGE_EXTENSIONS",
]

#: Alle Endungen, die die Anwendung importieren kann
SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".xlsx", ".xlsm", ".xltx", ".xls",
    ".csv", ".tsv",
    ".eml", ".msg",
    ".txt", ".text", ".html", ".htm", ".md",
    # Gescannte/fotografierte Angebote -- nur mit Texterkennung auswertbar
    *IMAGE_EXTENSIONS,
)


class ReaderRegistry:
    """Waehlt den passenden Leser und liest Anhaenge rekursiv mit."""

    def __init__(self, settings: ExtractionSettings | None = None) -> None:
        self.settings = settings or ExtractionSettings()
        self.email_reader = EmailReader(self.settings,
                                        attachment_reader=self.read)
        #: ZIP-Archive werden wie eine Mail mit Anhaengen behandelt -- der
        #: Inhalt laeuft ueber dieselbe Registry (siehe EmailReader).
        self.archive_reader = ArchiveReader(attachment_reader=self.read,
                                            can_read=self.can_read_in_archive)
        self.readers: list[DocumentReader] = [
            PdfReader(),
            ExcelReader(),
            CsvReader(),
            self.email_reader,
            WordReader(),
            OpenDocumentReader(),
            OdsReader(),
            self.archive_reader,
            ImageReader(),
            RichTextReader(),
            TextReader(),
        ]

    # ------------------------------------------------------------------
    def supported_extensions(self) -> list[str]:
        extensions: list[str] = []
        for reader in self.readers:
            for extension in reader.extensions:
                if extension not in extensions:
                    extensions.append(extension)
        return extensions

    def can_read(self, path: str) -> bool:
        return any(reader.can_read(path) for reader in self.readers)

    def can_read_in_archive(self, path: str) -> bool:
        """Wie :meth:`can_read`, aber ohne Bilddateien.

        In einem ZIP-Archiv sind Bilder praktisch immer Beiwerk: Firmenlogo,
        Produktfoto, Unterschriftengrafik.  Jedes davon durch die
        Texterkennung zu schicken, kostet Sekunden je Datei und liefert
        Buchstabensalat, den anschliessend jemand aussortieren muss.  Ein
        *eingescanntes Angebot* kommt dagegen als einzelne Datei oder als
        Mailanhang, nicht als Bild im Archiv.

        Wer ein Bild aus einem Archiv wirklich auswerten will, entpackt es und
        oeffnet es einzeln -- dann greift der :class:`ImageReader` regulaer.
        """
        if Path(path).suffix.lower() in IMAGE_EXTENSIONS:
            return False
        return self.can_read(path)

    def reader_for(self, path: str) -> DocumentReader | None:
        for reader in self.readers:
            if reader.can_read(path):
                return reader
        return None

    # ------------------------------------------------------------------
    def read(self, path: str) -> RawDocument:
        """Datei einlesen.  Liefert immer ein Dokument -- nie eine Exception."""
        file_path = Path(path)
        reader = self.reader_for(path)
        if reader is None:
            document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
            document.add_warning(
                f"Dateiformat '{file_path.suffix or '(ohne Endung)'}' wird nicht "
                f"unterstuetzt.  Moeglich sind: {', '.join(self.supported_extensions())}."
            )
            return document
        if not file_path.exists():
            document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
            document.add_warning(f"Datei wurde nicht gefunden: {path}")
            return document
        try:
            document = reader.read(str(path))
        except Exception as exc:  # noqa: BLE001 -- letzte Sicherung
            document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
            document.add_warning(f"Datei konnte nicht verarbeitet werden: {exc}")
            logger.error("Unerwarteter Lesefehler bei %s: %s", path, exc, exc_info=True)
        return document

    def read_text(self, text: str, source_name: str = "Eingefuegter Text") -> RawDocument:
        """Eingefuegten Text (Zwischenablage) als Dokument aufbereiten."""
        return TextReader.from_text(text, source_name)


def read_document(path: str, settings: ExtractionSettings | None = None) -> RawDocument:
    """Bequemer Einzelaufruf ohne eigene Registry."""
    return ReaderRegistry(settings).read(path)
