"""Leser fuer Tabellendateien: xlsx/xlsm (openpyxl) und csv/txt-Tabellen.

Besonderheiten aus der Praxis:

* Lieferanten liefern gern *mehrere* Arbeitsblaetter (Deckblatt + Preisliste),
  deshalb wird jedes Blatt als eigener :class:`TableBlock` gelesen.
* Verbundene Zellen ("Preisliste 2026" ueber fuenf Spalten) werden aufgeloest,
  damit die Kopfzeilenerkennung nicht ins Leere laeuft.
* CSV kommt mit allen denkbaren Trennzeichen und Kodierungen -- beides wird
  erkannt, nicht angenommen.
* ``.xls`` (altes Binaerformat) braucht ``xlrd``.  Fehlt das Paket, gibt es
  eine klare, freundliche Meldung statt eines Absturzes.
"""

from __future__ import annotations

import csv
import datetime as _dt
import logging
from decimal import Decimal
from pathlib import Path

from ...models.enums import SourceKind
from ...utils.parsing import format_date
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["ExcelReader", "CsvReader", "XLS_HINT", "cell_to_text"]

XLS_HINT = ("Das alte Excel-Format .xls kann nicht gelesen werden (Paket 'xlrd' "
            "fehlt).  Bitte die Datei in Excel als .xlsx speichern -- oder "
            "'pip install xlrd' ausfuehren.")

#: Kodierungen in der Reihenfolge, in der sie probiert werden
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: Trennzeichen-Kandidaten, wenn der Sniffer versagt
_DELIMITERS = (";", ",", "\t", "|")

#: Maximale Zeilenzahl je Blatt (Schutz vor Riesendateien)
_MAX_ROWS = 20_000


def cell_to_text(value: object) -> str:
    """Excel-Zellwert in Text wandeln, ohne Information zu verlieren.

    Zahlen behalten ihre Nachkommastellen (kein ``1.2000000000000002``),
    Datumswerte werden deutsch formatiert, ``None`` wird zu ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, _dt.datetime):
        if value.time() == _dt.time(0, 0):
            return format_date(value.date())
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, _dt.date):
        return format_date(value)
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        # Repraesentationsrauschen von Gleitkomma abschneiden
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


class ExcelReader(DocumentReader):
    """xlsx/xlsm ueber openpyxl, xls mit klarer Fehlermeldung."""

    extensions = (".xlsx", ".xlsm", ".xltx", ".xls")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.EXCEL)
        suffix = Path(path).suffix.lower()
        if suffix == ".xls":
            return self._read_legacy(path, document)

        try:
            from openpyxl import load_workbook
        except ImportError:
            document.add_warning("openpyxl ist nicht installiert -- Excel-Dateien "
                                 "koennen nicht gelesen werden.")
            return document

        try:
            workbook = load_workbook(filename=path, data_only=True, read_only=False)
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Excel-Datei konnte nicht geoeffnet werden: {exc}")
            logger.warning("XLSX nicht lesbar (%s): %s", path, exc)
            return document

        try:
            self._read_workbook(workbook, document)
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Excel-Datei nur teilweise lesbar: {exc}")
            logger.warning("XLSX-Auswertung unvollstaendig (%s): %s", path, exc, exc_info=True)
        finally:
            try:
                workbook.close()
            except Exception:  # noqa: BLE001
                pass

        document.text = "\n\n".join(table.as_text() for table in document.tables)
        logger.info("Excel gelesen: %s (%d Blaetter)", Path(path).name, len(document.tables))
        return document

    # ------------------------------------------------------------------
    def _read_workbook(self, workbook, document: RawDocument) -> None:
        document.meta["sheet_names"] = list(workbook.sheetnames)
        for sheet in workbook.worksheets:
            try:
                rows, merged = self._read_sheet(sheet)
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Arbeitsblatt '{sheet.title}' nicht lesbar: {exc}")
                continue
            if not rows:
                continue
            block = TableBlock(rows=rows, page=0, origin="excel", title=str(sheet.title),
                               merged_cells=merged)
            document.tables.append(block)
            document.pages.append(block.as_text())

    def _read_sheet(self, sheet) -> tuple[list[list[str]], set[tuple[int, int]]]:
        """Ein Arbeitsblatt als Textmatrix; verbundene Zellen aufgeloest.

        Rueckgabe: (Zeilen, Koordinaten der aus Verbuenden gefuellten Zellen).
        """
        rows: list[list[str]] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= _MAX_ROWS:
                logger.warning("Arbeitsblatt '%s' bei %d Zeilen abgeschnitten",
                               sheet.title, _MAX_ROWS)
                break
            rows.append([cell_to_text(value) for value in row])
        merged = self._fill_merged(sheet, rows)
        # Leere Zeilen am Ende entfernen
        while rows and not any(cell for cell in rows[-1]):
            rows.pop()
        return rows, merged

    @staticmethod
    def _fill_merged(sheet, rows: list[list[str]]) -> set[tuple[int, int]]:
        """Wert einer verbundenen Zelle in alle beteiligten Zellen kopieren.

        Rueckgabe: die (Zeile, Spalte)-Koordinaten aller Zellen, die dabei
        *gefuellt* wurden -- die Extraktion markiert deren Werte als unsicher.
        """
        filled: set[tuple[int, int]] = set()
        try:
            ranges = list(sheet.merged_cells.ranges)
        except Exception:  # noqa: BLE001 -- read_only-Modus kennt das nicht
            return filled
        for cell_range in ranges:
            try:
                min_row, min_col = cell_range.min_row, cell_range.min_col
                max_row, max_col = cell_range.max_row, cell_range.max_col
                if min_row - 1 >= len(rows):
                    continue
                source_row = rows[min_row - 1]
                if min_col - 1 >= len(source_row):
                    continue
                value = source_row[min_col - 1]
                if not value:
                    continue
                for r in range(min_row - 1, min(max_row, len(rows))):
                    row = rows[r]
                    for c in range(min_col - 1, min(max_col, len(row))):
                        if not row[c]:
                            row[c] = value
                            if (r, c) != (min_row - 1, min_col - 1):
                                filled.add((r, c))
            except Exception:  # noqa: BLE001
                continue
        return filled

    # ------------------------------------------------------------------
    def _read_legacy(self, path: str, document: RawDocument) -> RawDocument:
        try:
            import xlrd  # type: ignore
        except ImportError:
            document.add_warning(XLS_HINT)
            document.meta["needs_conversion"] = True
            logger.info("XLS ohne xlrd angefordert: %s", path)
            return document

        try:
            book = xlrd.open_workbook(path)
            for sheet in book.sheets():
                rows = [[cell_to_text(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                        for r in range(min(sheet.nrows, _MAX_ROWS))]
                if rows:
                    document.tables.append(
                        TableBlock(rows=rows, origin="excel", title=str(sheet.name))
                    )
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"XLS-Datei nicht lesbar: {exc}")
        document.text = "\n\n".join(table.as_text() for table in document.tables)
        return document


class CsvReader(DocumentReader):
    """CSV/TSV mit automatischer Trennzeichen- und Kodierungserkennung."""

    extensions = (".csv", ".tsv")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.EXCEL)
        raw, encoding, warning = _read_text_file(path)
        if warning:
            document.add_warning(warning)
        if raw is None:
            return document
        document.meta["encoding"] = encoding

        delimiter = detect_delimiter(raw)
        document.meta["delimiter"] = delimiter
        try:
            reader = csv.reader(raw.splitlines(), delimiter=delimiter)
            rows = [[cell.strip() for cell in row] for row in reader]
        except csv.Error as exc:
            document.add_warning(f"CSV-Datei konnte nicht zerlegt werden: {exc}")
            document.text = raw
            return document

        rows = [row for row in rows if any(cell for cell in row)]
        if rows:
            document.tables.append(
                TableBlock(rows=rows, origin="excel", title=Path(path).stem)
            )
        document.text = raw
        logger.info("CSV gelesen: %s (Trennzeichen %r, Kodierung %s, %d Zeilen)",
                    Path(path).name, delimiter, encoding, len(rows))
        return document


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _read_text_file(path: str) -> tuple[str | None, str, str]:
    """Textdatei lesen und dabei die Kodierung bestimmen."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return None, "", f"Datei konnte nicht gelesen werden: {exc}"
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding, ""
        except UnicodeDecodeError:
            continue
    return (data.decode("latin-1", "replace"), "latin-1",
            "Kodierung konnte nicht sicher bestimmt werden -- Umlaute pruefen.")


def detect_delimiter(sample: str) -> str:
    """Trennzeichen einer CSV bestimmen (Sniffer mit robustem Fallback)."""
    head = "\n".join(sample.splitlines()[:20])
    if not head.strip():
        return ";"
    try:
        dialect = csv.Sniffer().sniff(head, delimiters="".join(_DELIMITERS))
        if dialect.delimiter in _DELIMITERS:
            return dialect.delimiter
    except csv.Error:
        logger.debug("CSV-Sniffer ohne Ergebnis, es wird gezaehlt")

    # Fallback: das Zeichen, das in den meisten Zeilen gleich oft vorkommt
    lines = [line for line in head.splitlines() if line.strip()]
    best, best_score = ";", -1.0
    for candidate in _DELIMITERS:
        counts = [line.count(candidate) for line in lines]
        if not counts or max(counts) == 0:
            continue
        consistent = sum(1 for c in counts if c == counts[0])
        score = consistent + max(counts) * 0.1
        if score > best_score:
            best, best_score = candidate, score
    return best
