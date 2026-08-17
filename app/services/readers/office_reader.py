"""Leser fuer Textverarbeitungsformate: Word, OpenDocument, RTF.

Warum ohne Zusatzbibliothek?
----------------------------
``.docx`` und ``.odt`` sind ZIP-Archive mit XML darin.  Beides laesst sich mit
der Standardbibliothek (``zipfile`` + ``xml.etree``) zuverlaessig lesen.  Das
spart eine weitere Abhaengigkeit -- und Abhaengigkeiten sind in einer
Einkaufsumgebung, in der Installationen genehmigt werden muessen, teuer.

Was gelesen wird
----------------
* **Tabellen** -- der wichtigste Teil.  Viele Lieferanten schicken ihr Angebot
  als Word-Datei mit einer Preistabelle.  Diese wird als ``TableBlock``
  uebergeben und laeuft danach durch dieselbe Spaltenerkennung wie eine
  Excel-Tabelle.
* **Fliesstext** -- Absaetze ausserhalb von Tabellen, fuer Kopfdaten und die
  Freitexterkennung.
* **Kopf- und Fusszeilen** von Word-Dateien: Dort steht ueberraschend oft die
  Angebotsnummer.

Nicht unterstuetzt
------------------
Das alte Binaerformat ``.doc`` (vor Word 2007).  Dafuer waere ein kompletter
OLE-Streamparser noetig, dessen Textextraktion trotzdem unzuverlaessig bliebe.
Statt zu raten, gibt es eine klare Meldung mit dem Hinweis, die Datei als
``.docx`` zu speichern.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from ...models.enums import SourceKind
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["WordReader", "OpenDocumentReader", "RichTextReader"]

# -- Namensraeume ----------------------------------------------------------
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"

#: Mehr als so viele Zeilen liest niemand mehr sinnvoll ein -- Schutz gegen
#: aufgeblaehte Dateien
_MAX_ROWS = 5000


def _clean(text: str) -> str:
    """Weiche Trennzeichen und Mehrfach-Leerzeichen entfernen."""
    return re.sub(r"[ \t\u00a0]+", " ", text.replace("\u00ad", "")).strip()


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------

class WordReader(DocumentReader):
    """Liest Word-Dateien im Open-XML-Format."""

    extensions = (".docx", ".dotx", ".docm")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
        try:
            with zipfile.ZipFile(path) as archiv:
                namen = set(archiv.namelist())
                if "word/document.xml" not in namen:
                    return self._empty(
                        path, SourceKind.TEXT,
                        "Die Datei sieht nicht wie ein Word-Dokument aus "
                        "(word/document.xml fehlt).")
                haupt = archiv.read("word/document.xml")
                zusatz = [archiv.read(name) for name in sorted(namen)
                          if re.fullmatch(r"word/(header|footer)\d*\.xml", name)]
        except zipfile.BadZipFile:
            return self._empty(
                path, SourceKind.TEXT,
                "Die Word-Datei ist beschaedigt oder es handelt sich um das alte "
                "Format (.doc). Bitte in Word als .docx speichern.")
        except OSError as fehler:
            return self._empty(path, SourceKind.TEXT,
                               f"Die Datei konnte nicht gelesen werden: {fehler}")

        try:
            wurzel = ElementTree.fromstring(haupt)
        except ElementTree.ParseError as fehler:
            return self._empty(path, SourceKind.TEXT,
                               f"Der Inhalt der Word-Datei ist unlesbar: {fehler}")

        absaetze: list[str] = []
        tabellen: list[TableBlock] = []
        self._walk_body(wurzel, absaetze, tabellen, path)

        # Kopf-/Fusszeilen: dort steht oft die Angebotsnummer
        kopfzeilen: list[str] = []
        for rohdaten in zusatz:
            try:
                teil = ElementTree.fromstring(rohdaten)
            except ElementTree.ParseError:
                continue
            for absatz in teil.iter(f"{_W}p"):
                text = _clean(self._paragraph_text(absatz))
                if text:
                    kopfzeilen.append(text)

        alle_zeilen = kopfzeilen + absaetze
        document.text = "\n".join(alle_zeilen)
        document.pages = [document.text]
        document.tables = tabellen
        document.meta.update({
            "leser": "word",
            "absaetze": len(absaetze),
            "tabellen": len(tabellen),
            "kopf_fusszeilen": len(kopfzeilen),
        })
        if not document.text.strip() and not tabellen:
            document.add_warning(
                "Das Word-Dokument enthaelt keinen auswertbaren Text. Besteht es "
                "nur aus Bildern, ist keine Erkennung moeglich (kein OCR).")
        logger.info("Word gelesen: %s (%d Absaetze, %d Tabelle(n))",
                    Path(path).name, len(absaetze), len(tabellen))
        return document

    # ------------------------------------------------------------------
    def _walk_body(self, wurzel, absaetze: list[str], tabellen: list[TableBlock],
                   path: str) -> None:
        """Absaetze und Tabellen in Dokumentreihenfolge einsammeln.

        Die Reihenfolge ist wichtig: Steht ueber der Tabelle "Preise gueltig ab
        01.09.2026", gehoert das zusammen.
        """
        koerper = wurzel.find(f"{_W}body")
        if koerper is None:
            koerper = wurzel
        for element in koerper:
            if element.tag == f"{_W}p":
                text = _clean(self._paragraph_text(element))
                if text:
                    absaetze.append(text)
            elif element.tag == f"{_W}tbl":
                block = self._table(element, len(tabellen))
                if block is not None:
                    tabellen.append(block)
                    # Tabelleninhalt zusaetzlich als Text, damit die
                    # Kopfdatenregeln ihn ebenfalls sehen
                    for zeile in block.rows:
                        absaetze.append("\t".join(zeile))

    @staticmethod
    def _paragraph_text(absatz) -> str:
        teile: list[str] = []
        for knoten in absatz.iter():
            if knoten.tag == f"{_W}t" and knoten.text:
                teile.append(knoten.text)
            elif knoten.tag == f"{_W}tab":
                teile.append("\t")
            elif knoten.tag in (f"{_W}br", f"{_W}cr"):
                teile.append(" ")
        return "".join(teile)

    def _table(self, tabelle, index: int) -> TableBlock | None:
        zeilen: list[list[str]] = []
        for zeile in tabelle.findall(f"{_W}tr"):
            zellen: list[str] = []
            for zelle in zeile.findall(f"{_W}tc"):
                absaetze = [self._paragraph_text(a) for a in zelle.findall(f"{_W}p")]
                zellen.append(_clean(" ".join(absaetze)))
                # Verbundene Zellen: der Inhalt gilt fuer alle Spalten
                span = zelle.find(f"{_W}tcPr/{_W}gridSpan")
                if span is not None:
                    try:
                        weitere = int(span.get(f"{_W}val", "1")) - 1
                    except ValueError:
                        weitere = 0
                    zellen.extend([""] * max(0, weitere))
            if any(zellen):
                zeilen.append(zellen)
            if len(zeilen) >= _MAX_ROWS:
                break
        if not zeilen:
            return None
        breite = max(len(z) for z in zeilen)
        zeilen = [z + [""] * (breite - len(z)) for z in zeilen]
        return TableBlock(rows=zeilen, page=0, origin="word-table",
                          header_row_index=None)


# ---------------------------------------------------------------------------
# OpenDocument (.odt)
# ---------------------------------------------------------------------------

class OpenDocumentReader(DocumentReader):
    """Liest OpenDocument-Texte (LibreOffice, OpenOffice)."""

    extensions = (".odt", ".ott")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
        try:
            with zipfile.ZipFile(path) as archiv:
                if "content.xml" not in archiv.namelist():
                    return self._empty(path, SourceKind.TEXT,
                                       "Die Datei sieht nicht wie ein OpenDocument aus "
                                       "(content.xml fehlt).")
                rohdaten = archiv.read("content.xml")
        except zipfile.BadZipFile:
            return self._empty(path, SourceKind.TEXT,
                               "Die OpenDocument-Datei ist beschaedigt.")
        except OSError as fehler:
            return self._empty(path, SourceKind.TEXT,
                               f"Die Datei konnte nicht gelesen werden: {fehler}")

        try:
            wurzel = ElementTree.fromstring(rohdaten)
        except ElementTree.ParseError as fehler:
            return self._empty(path, SourceKind.TEXT,
                               f"Der Inhalt ist unlesbar: {fehler}")

        absaetze: list[str] = []
        tabellen: list[TableBlock] = []
        for tabelle in wurzel.iter(f"{_TABLE_NS}table"):
            block = self._table(tabelle)
            if block is not None:
                tabellen.append(block)
                for zeile in block.rows:
                    absaetze.append("\t".join(zeile))

        for absatz in wurzel.iter():
            if absatz.tag in (f"{_TEXT_NS}p", f"{_TEXT_NS}h"):
                text = _clean("".join(absatz.itertext()))
                if text and text not in absaetze:
                    absaetze.append(text)

        document.text = "\n".join(absaetze)
        document.pages = [document.text]
        document.tables = tabellen
        document.meta.update({"leser": "opendocument", "tabellen": len(tabellen)})
        if not document.text.strip():
            document.add_warning("Das OpenDocument enthaelt keinen auswertbaren Text.")
        logger.info("OpenDocument gelesen: %s (%d Tabelle(n))",
                    Path(path).name, len(tabellen))
        return document

    @staticmethod
    def _table(tabelle) -> TableBlock | None:
        zeilen: list[list[str]] = []
        for zeile in tabelle.iter(f"{_TABLE_NS}table-row"):
            zellen: list[str] = []
            for zelle in zeile.findall(f"{_TABLE_NS}table-cell"):
                zellen.append(_clean("".join(zelle.itertext())))
                # Wiederholte Spalten sind in ODF ueblich (leere Auffueller)
                wiederholt = zelle.get(f"{_TABLE_NS}number-columns-repeated")
                if wiederholt:
                    try:
                        anzahl = min(int(wiederholt), 64) - 1
                    except ValueError:
                        anzahl = 0
                    zellen.extend([zellen[-1]] * max(0, anzahl))
            if any(zellen):
                zeilen.append(zellen)
            if len(zeilen) >= _MAX_ROWS:
                break
        if not zeilen:
            return None
        breite = max(len(z) for z in zeilen)
        zeilen = [z + [""] * (breite - len(z)) for z in zeilen]
        return TableBlock(rows=zeilen, page=0, origin="odt-table",
                          header_row_index=None)


# ---------------------------------------------------------------------------
# Rich Text (.rtf)
# ---------------------------------------------------------------------------

_RTF_STEUERWORT = re.compile(r"\\([a-zA-Z]+)(-?\d+)?[ ]?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")


class RichTextReader(DocumentReader):
    """Liest RTF-Dateien als Text.

    RTF-Tabellen bestehen aus Steuerworten (``\\cell``, ``\\row``) statt aus
    einer Struktur.  Daraus laesst sich eine Tabelle rekonstruieren, aber nur
    grob -- Spaltenbreiten sind Zeichnungsangaben, keine Semantik.  Deshalb
    wird der Inhalt zusaetzlich als Text uebergeben, damit die
    Freitexterkennung greifen kann.
    """

    extensions = (".rtf",)

    def read(self, path: str) -> RawDocument:
        try:
            rohdaten = Path(path).read_bytes()
        except OSError as fehler:
            return self._empty(path, SourceKind.TEXT,
                               f"Die Datei konnte nicht gelesen werden: {fehler}")
        if not rohdaten.lstrip().startswith(b"{\\rtf"):
            return self._empty(path, SourceKind.TEXT,
                               "Die Datei ist keine gueltige RTF-Datei.")

        text = rohdaten.decode("cp1252", errors="replace")
        zeilen, tabellenzeilen = self._entpacken(text)

        document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
        document.text = "\n".join(zeilen)
        document.pages = [document.text]
        if tabellenzeilen:
            breite = max(len(z) for z in tabellenzeilen)
            gefuellt = [z + [""] * (breite - len(z)) for z in tabellenzeilen]
            document.tables = [TableBlock(rows=gefuellt, page=0, origin="rtf-table",
                                          header_row_index=None)]
        document.meta.update({"leser": "rtf", "tabellenzeilen": len(tabellenzeilen)})
        if not document.text.strip():
            document.add_warning("Die RTF-Datei enthaelt keinen auswertbaren Text.")
        logger.info("RTF gelesen: %s (%d Zeile(n), %d Tabellenzeile(n))",
                    Path(path).name, len(zeilen), len(tabellenzeilen))
        return document

    @staticmethod
    def _entpacken(text: str) -> tuple[list[str], list[list[str]]]:
        """Steuerworte entfernen und dabei Zeilen-/Zellengrenzen merken."""
        zeilen: list[str] = []
        tabellenzeilen: list[list[str]] = []
        puffer: list[str] = []
        zellen: list[str] = []
        tiefe_ueberspringen = 0

        # Sonderzeichen zuerst aufloesen
        text = _RTF_HEX.sub(lambda m: bytes([int(m.group(1), 16)]).decode(
            "cp1252", errors="replace"), text)

        index = 0
        laenge = len(text)
        while index < laenge:
            zeichen = text[index]
            if zeichen == "\\":
                treffer = _RTF_STEUERWORT.match(text, index)
                if treffer is None:
                    index += 2          # maskiertes Zeichen (\{ \} \\)
                    if index - 1 < laenge:
                        puffer.append(text[index - 1])
                    continue
                wort = treffer.group(1)
                index = treffer.end()
                if wort in ("par", "line"):
                    fertig = _clean("".join(puffer))
                    if fertig:
                        zeilen.append(fertig)
                    puffer = []
                elif wort == "cell":
                    zellen.append(_clean("".join(puffer)))
                    puffer = []
                elif wort == "row":
                    if any(zellen):
                        tabellenzeilen.append(zellen)
                        zeilen.append("\t".join(zellen))
                    zellen = []
                elif wort in ("fonttbl", "colortbl", "stylesheet", "info",
                              "pict", "object", "themedata", "datastore"):
                    tiefe_ueberspringen = 1   # zugehoerige Gruppe verwerfen
                continue
            if zeichen == "{":
                if tiefe_ueberspringen:
                    tiefe_ueberspringen += 1
                index += 1
                continue
            if zeichen == "}":
                if tiefe_ueberspringen:
                    tiefe_ueberspringen -= 1
                index += 1
                continue
            if not tiefe_ueberspringen and zeichen not in "\r\n":
                puffer.append(zeichen)
            index += 1

        rest = _clean("".join(puffer))
        if rest:
            zeilen.append(rest)
        return zeilen, tabellenzeilen
