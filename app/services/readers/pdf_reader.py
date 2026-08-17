"""PDF-Leser mit layoutbewusster Tabellenrekonstruktion (PyMuPDF).

``page.get_text()`` allein reicht fuer Angebote nicht: Spalten verschmelzen,
Zahlen rutschen in die Beschreibung.  Deshalb werden hier die *Wortkoordinaten*
ausgewertet:

1. Woerter mit Rechteck holen (``page.get_text("words")``)
2. Zeilen ueber die y-Mitte clustern (Toleranz aus der Zeilenhoehe)
3. Spaltengrenzen ueber eine Haeufigkeitsanalyse der x-Startpunkte finden --
   in einer Preistabelle beginnen viele Zeilen an denselben x-Positionen
4. Woerter den Spalten zuordnen und daraus :class:`TableBlock` bauen

Zusaetzlich wird ``page.find_tables()`` versucht (existiert erst ab neueren
PyMuPDF-Versionen, deshalb defensiv gekapselt).

Gescannte PDFs ohne Textebene werden erkannt und **klar gemeldet** -- es wird
nichts geraten und keine OCR vorgetaeuscht.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ...models.enums import SourceKind
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["PdfReader", "words_to_tables", "SCAN_WARNING"]

SCAN_WARNING = ("PDF enthaelt keinen durchsuchbaren Text (vermutlich ein Scan). "
                "OCR ist nicht eingebaut -- bitte ein Text-PDF anfordern oder die "
                "Positionen manuell erfassen.")

#: Ab wie vielen Zeichen je Seite gilt eine Seite als "hat Text"
_MIN_CHARS_PER_PAGE = 40


@dataclass(slots=True)
class _Word:
    """Ein Wort mit Position (Koordinaten in PDF-Punkten)."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


class PdfReader(DocumentReader):
    """Liest PDF-Angebote inklusive Tabellenstruktur."""

    extensions = (".pdf",)

    def __init__(self, y_tolerance_factor: float = 0.6, x_bin: float = 4.0) -> None:
        #: Anteil der Zeilenhoehe, bis zu dem Woerter noch als eine Zeile gelten
        self.y_tolerance_factor = y_tolerance_factor
        #: Rasterbreite fuer die Haeufigkeitsanalyse der Spaltenstarts
        self.x_bin = x_bin

    # ------------------------------------------------------------------
    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.PDF)
        try:
            import fitz  # PyMuPDF
        except ImportError:
            document.add_warning(
                "PyMuPDF (fitz) ist nicht installiert -- PDF-Dateien koennen nicht "
                "gelesen werden.  Bitte 'pip install pymupdf' ausfuehren."
            )
            return document

        try:
            handle = fitz.open(path)
        except Exception as exc:  # noqa: BLE001 -- kaputte Datei darf nicht killen
            document.add_warning(f"PDF konnte nicht geoeffnet werden: {exc}")
            logger.warning("PDF nicht lesbar (%s): %s", path, exc)
            return document

        try:
            self._read_document(handle, document)
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"PDF nur teilweise lesbar: {exc}")
            logger.warning("PDF-Auswertung unvollstaendig (%s): %s", path, exc, exc_info=True)
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

        document.text = "\n".join(document.pages).strip()
        self._check_for_scan(document)
        logger.info("PDF gelesen: %s (%d Seiten, %d Tabellenbloecke, %d Zeichen)",
                    Path(path).name, len(document.pages), len(document.tables),
                    len(document.text))
        return document

    # ------------------------------------------------------------------
    def _read_document(self, handle, document: RawDocument) -> None:
        try:
            meta = dict(handle.metadata or {})
        except Exception:  # noqa: BLE001
            meta = {}
        document.meta.update({k: v for k, v in meta.items() if v})
        document.meta["page_count"] = handle.page_count

        for index in range(handle.page_count):
            page_number = index + 1
            try:
                page = handle.load_page(index)
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Seite {page_number} nicht lesbar: {exc}")
                document.pages.append("")
                continue

            try:
                text = page.get_text("text") or ""
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Text der Seite {page_number} nicht lesbar: {exc}")
                text = ""
            document.pages.append(text)

            words = self._page_words(page, page_number, document)
            if words:
                for block in words_to_tables(words, page_number,
                                             y_tolerance_factor=self.y_tolerance_factor,
                                             x_bin=self.x_bin):
                    document.tables.append(block)

            for block in self._native_tables(page, page_number, document):
                document.tables.append(block)

    # ------------------------------------------------------------------
    def _page_words(self, page, page_number: int, document: RawDocument) -> list[_Word]:
        try:
            raw = page.get_text("words") or []
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Wortkoordinaten der Seite {page_number} fehlen: {exc}")
            return []
        words: list[_Word] = []
        for item in raw:
            try:
                x0, y0, x1, y1, text = item[0], item[1], item[2], item[3], item[4]
            except (IndexError, TypeError):
                continue
            text = str(text).strip()
            if not text:
                continue
            words.append(_Word(float(x0), float(y0), float(x1), float(y1), text))
        return words

    def _native_tables(self, page, page_number: int,
                       document: RawDocument) -> list[TableBlock]:
        """``page.find_tables()`` nutzen, falls die PyMuPDF-Version es kennt."""
        finder = getattr(page, "find_tables", None)
        if finder is None:
            return []
        blocks: list[TableBlock] = []
        try:
            found = finder()
            tables = getattr(found, "tables", found)
            for table in tables or []:
                rows = table.extract()
                cleaned = [[("" if cell is None else str(cell)) for cell in row]
                           for row in rows or []]
                cleaned = [row for row in cleaned if any(c.strip() for c in row)]
                if len(cleaned) >= 2 and max(len(r) for r in cleaned) >= 2:
                    blocks.append(TableBlock(rows=cleaned, page=page_number,
                                             origin="pdf-table"))
        except Exception as exc:  # noqa: BLE001 -- API variiert je Version
            logger.debug("find_tables() auf Seite %d nicht verwendbar: %s", page_number, exc)
        return blocks

    # ------------------------------------------------------------------
    def _check_for_scan(self, document: RawDocument) -> None:
        pages = document.pages or [""]
        chars = sum(len(p.strip()) for p in pages)
        if chars < _MIN_CHARS_PER_PAGE * len(pages):
            document.add_warning(SCAN_WARNING)
            document.meta["scanned"] = True
            logger.info("PDF wirkt wie ein Scan ohne Textebene: %s", document.source_path)


# ---------------------------------------------------------------------------
# Layoutanalyse (bewusst als freie Funktionen -- so direkt testbar)
# ---------------------------------------------------------------------------

def _cluster_lines(words: list[_Word], y_tolerance_factor: float) -> list[list[_Word]]:
    """Woerter zu Textzeilen zusammenfassen (Clustern ueber die y-Mitte)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.y_center, w.x0))
    median_height = sorted(w.height for w in ordered)[len(ordered) // 2]
    tolerance = max(median_height * y_tolerance_factor, 1.0)

    lines: list[list[_Word]] = []
    current: list[_Word] = [ordered[0]]
    reference = ordered[0].y_center
    for word in ordered[1:]:
        if abs(word.y_center - reference) <= tolerance:
            current.append(word)
            # Referenz mitziehen: leicht schraeg gesetzte Zeilen bleiben zusammen
            reference = sum(w.y_center for w in current) / len(current)
        else:
            lines.append(sorted(current, key=lambda w: w.x0))
            current = [word]
            reference = word.y_center
    lines.append(sorted(current, key=lambda w: w.x0))
    return lines


def _column_starts(lines: list[list[_Word]], x_bin: float) -> list[float]:
    """Spaltengrenzen ueber die Haeufigkeit der x-Startpunkte bestimmen.

    In einer Tabelle beginnen viele Zeilen an denselben x-Positionen.  Wir
    rastern die Startpunkte, zaehlen sie und behalten die haeufigen -- das sind
    die Spalten.  Fliesstext erzeugt dagegen breit gestreute Startpunkte und
    liefert daher (gewollt) kaum Spalten.
    """
    if len(lines) < 2:
        return []
    counter: Counter[int] = Counter()
    for line in lines:
        seen: set[int] = set()
        for word in line:
            bin_index = int(round(word.x0 / x_bin))
            if bin_index not in seen:
                seen.add(bin_index)
                counter[bin_index] += 1

    if not counter:
        return []
    threshold = max(2, int(len(lines) * 0.25))
    candidates = sorted(b for b, count in counter.items() if count >= threshold)
    if len(candidates) < 2:
        # Schwelle war zu streng -- mit den haeufigsten Positionen erneut versuchen
        candidates = sorted(b for b, _ in counter.most_common(8))

    # Dicht beieinander liegende Kandidaten verschmelzen (Rundungsrauschen)
    starts: list[float] = []
    for candidate in candidates:
        value = candidate * x_bin
        if starts and value - starts[-1] < x_bin * 1.5:
            continue
        starts.append(value)
    return starts


def _row_from_line(line: list[_Word], starts: list[float], x_bin: float) -> list[str]:
    """Woerter einer Zeile auf die Spalten verteilen."""
    cells: list[list[str]] = [[] for _ in starts]
    for word in line:
        index = 0
        for position, start in enumerate(starts):
            if word.x0 + x_bin * 0.75 >= start:
                index = position
            else:
                break
        cells[index].append(word.text)
    return [" ".join(cell).strip() for cell in cells]


def words_to_tables(words: list[_Word], page: int, y_tolerance_factor: float = 0.6,
                    x_bin: float = 4.0) -> list[TableBlock]:
    """Aus Wortkoordinaten einen Tabellenblock je Seite rekonstruieren.

    Liefert eine leere Liste, wenn die Seite erkennbar kein Raster hat -- dann
    arbeitet die Extraktion mit dem reinen Text weiter.
    """
    lines = _cluster_lines(words, y_tolerance_factor)
    if len(lines) < 2:
        return []
    starts = _column_starts(lines, x_bin)
    if len(starts) < 2:
        return []

    rows = [_row_from_line(line, starts, x_bin) for line in lines]
    rows = [row for row in rows if any(cell for cell in row)]
    # Nur Bloecke behalten, in denen mehrere Zeilen wirklich mehrspaltig sind
    multi = sum(1 for row in rows if sum(1 for c in row if c) >= 2)
    if len(rows) < 2 or multi < 2:
        return []
    return [TableBlock(rows=rows, page=page, origin="pdf-layout")]


def make_word(x0: float, y0: float, x1: float, y1: float, text: str) -> _Word:
    """Hilfsfunktion fuer Tests: ein Wort mit Koordinaten erzeugen."""
    return _Word(x0, y0, x1, y1, text)
