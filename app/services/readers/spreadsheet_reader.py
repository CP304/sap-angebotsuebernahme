"""Leser fuer OpenDocument-Tabellen (.ods) -- die LibreOffice-Entsprechung
zu Excel.

Warum ein eigener Leser?
------------------------
``office_reader.py`` liest bereits ``.odt`` (Textdokument).  Die
*Tabellen*variante ``.ods`` folgt zwar demselben ZIP+XML-Prinzip, hat aber
eine Eigenheit, die man leicht uebersieht und die einen naiven Leser sofort
in die Knie zwingt:

``table:number-columns-repeated`` / ``table:number-rows-repeated``
    LibreOffice speichert gleichartige (meist leere) Bereiche
    *zusammengefasst*.  Eine Zeile endet regelmaessig mit einer einzigen
    leeren Zelle und ``table:number-columns-repeated="1014"``; ein Blatt
    endet mit einer Zeile und ``table:number-rows-repeated="1048000"``.
    Wird das naiv expandiert, entstehen Millionen leerer Zellen.  Deshalb
    werden Wiederholungen hier gedeckelt (:data:`_MAX_COLUMNS` /
    :data:`_MAX_ROWS`) und nachlaufende Leerspalten und -zeilen danach
    abgeschnitten.

Zellwerte
---------
Massgeblich ist -- falls vorhanden -- das Attribut ``office:value`` (bzw.
``office:date-value``, ``office:boolean-value``, ``office:time-value``).
Der *angezeigte* Text ist formatiert ("1.234,50 EUR") und damit schlechter
zu parsen als der rohe Wert ("1234.5").  Nur wenn kein Rohwert vorliegt --
also bei echtem Text -- wird der Anzeigetext genommen.

Jedes Blatt wird ein eigener :class:`TableBlock` mit ``origin="ods-table"``
und dem Blattnamen in ``title`` -- genau wie beim Excel-Leser, damit die
nachgelagerte Spalten- und Kopfzeilenerkennung unveraendert greift.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from ...models.enums import SourceKind
from ...utils.parsing import format_date
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["OdsReader"]

# -- Namensraeume ----------------------------------------------------------
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OFFICE_NS = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

#: Mehr Spalten hat kein Angebot -- Schutz gegen aufgeblaehte Wiederholungen
_MAX_COLUMNS = 256

#: Obergrenze je Blatt
_MAX_ROWS = 10_000


def _clean(text: str) -> str:
    """Weiche Trennzeichen und Mehrfach-Leerzeichen entfernen."""
    return re.sub(r"[ \t ]+", " ", text.replace("­", "")).strip()


def _int_attr(element, name: str, default: int = 1) -> int:
    """Ganzzahliges Attribut robust lesen (kaputte Werte -> Standard)."""
    rohwert = element.get(name)
    if not rohwert:
        return default
    try:
        wert = int(rohwert)
    except (TypeError, ValueError):
        return default
    return wert if wert > 0 else default


def _number_to_text(rohwert: str) -> str:
    """``office:value`` in eine verlustfreie Textdarstellung bringen.

    ``"1000"`` bleibt ``"1000"``, ``"4.5500"`` wird ``"4.55"`` -- aber nur
    dann, wenn der Wert wirklich eine Zahl ist.  Ist er es nicht, bleibt er
    unveraendert stehen; raten waere hier falsch.
    """
    text = rohwert.strip()
    if not text:
        return ""
    try:
        zahl = float(text)
    except ValueError:
        return text
    if zahl == int(zahl) and abs(zahl) < 1e15:
        return str(int(zahl))
    gekuerzt = f"{zahl:.10f}".rstrip("0").rstrip(".")
    return gekuerzt or "0"


def _date_to_text(rohwert: str) -> str:
    """ISO-Datum aus ``office:date-value`` deutsch formatieren."""
    text = rohwert.strip()
    if not text:
        return ""
    kern = text.split("T")[0]
    try:
        return format_date(_dt.date.fromisoformat(kern))
    except ValueError:
        return text


class OdsReader(DocumentReader):
    """Liest OpenDocument-Tabellen (LibreOffice Calc)."""

    extensions = (".ods", ".ots")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.EXCEL)
        try:
            with zipfile.ZipFile(path) as archiv:
                if "content.xml" not in archiv.namelist():
                    return self._empty(
                        path, SourceKind.EXCEL,
                        "Die Datei sieht nicht wie eine OpenDocument-Tabelle aus "
                        "(content.xml fehlt).")
                rohdaten = archiv.read("content.xml")
        except zipfile.BadZipFile:
            return self._empty(path, SourceKind.EXCEL,
                               "Die OpenDocument-Tabelle ist beschaedigt.")
        except OSError as fehler:
            return self._empty(path, SourceKind.EXCEL,
                               f"Die Datei konnte nicht gelesen werden: {fehler}")

        try:
            wurzel = ElementTree.fromstring(rohdaten)
        except ElementTree.ParseError as fehler:
            return self._empty(path, SourceKind.EXCEL,
                               f"Der Inhalt ist unlesbar: {fehler}")

        blattnamen: list[str] = []
        for tabelle in wurzel.iter(f"{_TABLE_NS}table"):
            name = tabelle.get(f"{_TABLE_NS}name") or f"Blatt {len(blattnamen) + 1}"
            blattnamen.append(name)
            try:
                zeilen, gedeckelt = self._sheet_rows(tabelle)
            except Exception as fehler:  # noqa: BLE001 -- ein Blatt darf nie alles kippen
                document.add_warning(f"Blatt '{name}' konnte nicht gelesen werden: "
                                     f"{fehler}")
                logger.warning("ODS-Blatt %s nicht lesbar: %s", name, fehler,
                               exc_info=True)
                continue
            if gedeckelt:
                document.add_warning(
                    f"Blatt '{name}' ist sehr gross und wurde bei "
                    f"{_MAX_ROWS} Zeilen bzw. {_MAX_COLUMNS} Spalten abgeschnitten.")
            if not zeilen:
                continue
            block = TableBlock(rows=zeilen, page=0, origin="ods-table", title=name)
            document.tables.append(block)
            document.pages.append(block.as_text())

        document.text = "\n\n".join(tabelle.as_text() for tabelle in document.tables)
        document.meta.update({
            "leser": "ods",
            "sheet_names": blattnamen,
            "tabellen": len(document.tables),
        })
        if not document.tables:
            document.add_warning(
                "Die OpenDocument-Tabelle enthaelt keine auswertbaren Zellen.")
        logger.info("ODS gelesen: %s (%d Blatt/Blaetter, %d mit Inhalt)",
                    Path(path).name, len(blattnamen), len(document.tables))
        return document

    # ------------------------------------------------------------------
    def _sheet_rows(self, tabelle) -> tuple[list[list[str]], bool]:
        """Ein Blatt als Textmatrix; Wiederholungen gedeckelt expandiert."""
        zeilen: list[list[str]] = []
        gedeckelt = False

        for zeile in tabelle.findall(f"{_TABLE_NS}table-row"):
            zellen, spalten_gedeckelt = self._row_cells(zeile)
            gedeckelt = gedeckelt or spalten_gedeckelt

            wiederholungen = _int_attr(zeile, f"{_TABLE_NS}number-rows-repeated")
            hat_inhalt = any(zellen)
            rest = _MAX_ROWS - len(zeilen)
            if rest <= 0:
                gedeckelt = gedeckelt or hat_inhalt
                break
            if not hat_inhalt:
                # Leere Wiederholungszeilen sind reine Auffueller.  Eine
                # genuegt -- der Rest wird ohnehin hinten abgeschnitten.
                # Das ist keine Kuerzung, sondern spart nur Speicher.
                wiederholungen = 1
            if wiederholungen > rest:
                wiederholungen = rest
                gedeckelt = gedeckelt or hat_inhalt
            for _ in range(wiederholungen):
                zeilen.append(list(zellen))

        # Nachlaufende Leerzeilen und Leerspalten entfernen
        while zeilen and not any(zelle for zelle in zeilen[-1]):
            zeilen.pop()
        if not zeilen:
            return [], gedeckelt
        breite = max(len(z) for z in zeilen)
        zeilen = [z + [""] * (breite - len(z)) for z in zeilen]
        while breite > 0 and not any(z[breite - 1] for z in zeilen):
            breite -= 1
        zeilen = [z[:breite] for z in zeilen]
        if not any(any(z) for z in zeilen):
            return [], gedeckelt
        return zeilen, gedeckelt

    # ------------------------------------------------------------------
    def _row_cells(self, zeile) -> tuple[list[str], bool]:
        """Zellen einer Zeile; ``number-columns-repeated`` beruecksichtigt."""
        zellen: list[str] = []
        gedeckelt = False
        for zelle in zeile:
            if zelle.tag not in (f"{_TABLE_NS}table-cell",
                                 f"{_TABLE_NS}covered-table-cell"):
                continue
            if zelle.tag == f"{_TABLE_NS}covered-table-cell":
                # Von einer verbundenen Zelle ueberdeckt -> bleibt leer
                wert = ""
            else:
                wert = self._cell_text(zelle)
            wiederholungen = _int_attr(zelle, f"{_TABLE_NS}number-columns-repeated")
            rest = _MAX_COLUMNS - len(zellen)
            # Gemeldet wird nur, wenn dabei wirklich Inhalt verloren geht.
            # Zusammengefasste Leerspalten sind der Normalfall, keine Kuerzung.
            if rest <= 0:
                gedeckelt = gedeckelt or bool(wert)
                break
            if wiederholungen > rest:
                wiederholungen = rest
                gedeckelt = gedeckelt or bool(wert)
            zellen.extend([wert] * wiederholungen)
        while zellen and not zellen[-1]:
            zellen.pop()
        return zellen, gedeckelt

    # ------------------------------------------------------------------
    @staticmethod
    def _cell_text(zelle) -> str:
        """Zellwert bestimmen -- Rohwert schlaegt formatierten Anzeigetext."""
        datum = zelle.get(f"{_OFFICE_NS}date-value")
        if datum:
            return _date_to_text(datum)
        wert = zelle.get(f"{_OFFICE_NS}value")
        if wert is not None and wert != "":
            return _number_to_text(wert)
        wahrheitswert = zelle.get(f"{_OFFICE_NS}boolean-value")
        if wahrheitswert:
            return "ja" if wahrheitswert.lower() == "true" else "nein"
        zeit = zelle.get(f"{_OFFICE_NS}time-value")
        if zeit:
            return zeit

        # Fallback: der angezeigte Text.  Mehrere Absaetze in einer Zelle
        # werden mit Leerzeichen verbunden, damit keine Zeile zerfaellt.
        absaetze = [
            _clean("".join(absatz.itertext()))
            for absatz in zelle.iter(f"{_TEXT_NS}p")
        ]
        gefuellt = [a for a in absaetze if a]
        if gefuellt:
            return " ".join(gefuellt)
        return _clean("".join(zelle.itertext()))
