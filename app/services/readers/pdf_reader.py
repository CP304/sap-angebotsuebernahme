"""PDF-Leser mit layoutbewusster Tabellenrekonstruktion (PyMuPDF).

``page.get_text()`` allein reicht fuer Angebote nicht: Spalten verschmelzen,
Zahlen rutschen in die Beschreibung.  Deshalb werden die *Wortkoordinaten*
ausgewertet -- und zwar in zwei Verfahren, die klar unterschieden werden:

**1. Linienverfahren ("lattice", ``origin="pdf-lattice"``)**
   Hat der Lieferant seine Tabelle mit gezeichneten Linien versehen, sind
   diese Linien die verlaesslichste Spaltengrenze, die es gibt -- sie sind
   nicht geschaetzt, sondern im Dokument vorhanden.  Die Vektorzeichnungen der
   Seite (``page.get_drawings()``) liefern senkrechte und waagerechte Linien;
   schmale Rechtecke zaehlen ebenfalls als Linie, weil viele Erzeuger Linien
   so ausgeben.  Senkrechte Linien werden zu Spaltengrenzen, waagerechte zu
   zusaetzlichen Zeilentrennern (damit ein umbrochener Beschreibungstext in
   *einer* Zelle bleibt).

**2. Koordinatenverfahren ("stream", ``origin="pdf-layout"``)**
   Fallback ohne Linien:

   1. Woerter mit Rechteck holen (``page.get_text("words")``)
   2. Zeilen ueber die y-Mitte clustern (Toleranz aus der Zeilenhoehe)
   3. Spaltengrenzen ueber eine Haeufigkeitsanalyse der x-Startpunkte finden
   4. Woerter den Spalten zuordnen und daraus :class:`TableBlock` bauen

Welches Verfahren gegriffen hat, steht im ``origin`` des Blocks und damit
spaeter im Protokoll -- ohne diese Angabe waere nicht nachvollziehbar, warum
eine Spalte so und nicht anders geschnitten wurde.

Zusaetzlich wird ``page.find_tables()`` versucht (existiert erst ab neueren
PyMuPDF-Versionen, deshalb defensiv gekapselt).

Gescannte PDFs ohne Textebene werden erkannt.  Ist eine Texterkennung
verfuegbar (siehe ``app/services/ocr``), wird die Seite gerendert, erkannt und
durch **dieselbe** Tabellenrekonstruktion geschickt -- der entstehende Block
traegt dann ``origin="pdf-ocr"``, damit ueberall nachvollziehbar bleibt, dass
die Werte aus einer Erkennung stammen und nicht aus dem Dokument.  Ist keine
Erkennung verfuegbar, bleibt es bei der bisherigen klaren Meldung; geraten wird
nach wie vor nichts.

Die Entscheidung faellt **je Seite**: Ein Angebot, dessen erste Seite ein
Text-PDF und dessen zweite Seite ein eingeklebter Scan ist, wird auf Seite 1
regulaer gelesen und nur auf Seite 2 erkannt.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ...config.settings import ExtractionSettings
from ...models.enums import SourceKind
from ..ocr import (
    NO_BACKEND_HINT,
    best_backend,
    cell_confidences,
    confidence_percent,
    ocr_settings_of,
    ocr_warning,
    uncertain_cells,
)
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = [
    "PdfReader",
    "find_column_split",
    "words_to_tables",
    "lattice_tables",
    "page_rulings",
    "tolerances_for",
    "make_ruling",
    "make_word",
    "render_page_png",
    "ocr_words_to_words",
    "ocr_page_into_document",
    "SCAN_WARNING",
    "SCAN_WARNING_WITH_HINT",
    "OCR_ORIGIN",
    "OCR_ASK_HINT",
]

SCAN_WARNING = ("PDF enthaelt keinen durchsuchbaren Text (vermutlich ein Scan). "
                "OCR ist nicht eingebaut -- bitte ein Text-PDF anfordern oder die "
                "Positionen manuell erfassen.")

#: Dieselbe Meldung, aber mit dem konkreten Installationshinweis.  Sie wird
#: verwendet, sobald feststeht, dass eine Erkennung *grundsaetzlich* helfen
#: wuerde, auf diesem Rechner aber keine installiert ist.
SCAN_WARNING_WITH_HINT = (
    "PDF enthaelt keinen durchsuchbaren Text (vermutlich ein Scan).  Eine "
    "Texterkennung koennte hier helfen, ist aber nicht installiert.\n"
    + NO_BACKEND_HINT
    + "\nAlternativ ein Text-PDF anfordern oder die Positionen manuell erfassen."
)

#: Hinweis, wenn OCR moeglich waere, der Anwender aber erst zustimmen soll
OCR_ASK_HINT = (
    "PDF enthaelt keinen durchsuchbaren Text (vermutlich ein Scan).  Eine "
    "Texterkennung ist verfuegbar, wurde aber nicht gestartet -- sie dauert "
    "einige Sekunden je Seite und muss deshalb bestaetigt werden "
    "(Einstellung 'ask_before_ocr')."
)

#: Herkunftskennzeichen aller aus einer Texterkennung gewonnenen Bloecke
OCR_ORIGIN = "pdf-ocr"

#: Ab wie vielen Zeichen je Seite gilt eine Seite als "hat Text"
_MIN_CHARS_PER_PAGE = 40

#: Bis zu dieser Dicke gilt ein Rechteck als gezeichnete Linie (PDF-Punkte)
_LINE_THICKNESS = 2.5

#: Kuerzere Striche sind Kaestchen, Haken oder Schmuck -- keine Tabellenlinie
_MIN_LINE_LENGTH = 8.0

#: Senkrechte Linien, die naeher als das beieinander liegen, sind dieselbe
#: Spaltengrenze (doppelt gezeichnete Rahmen)
_LINE_MERGE_TOLERANCE = 2.0


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


@dataclass(slots=True)
class _Ruling:
    """Eine gezeichnete Linie (senkrecht oder waagerecht)."""

    #: Feste Achse: x bei senkrechten, y bei waagerechten Linien
    position: float
    #: Ausdehnung entlang der anderen Achse
    start: float
    end: float

    @property
    def length(self) -> float:
        return abs(self.end - self.start)


def tolerances_for(settings: object | None = None,
                   profile: object | None = None) -> tuple[float, float]:
    """Zeilen-/Spaltentoleranz aus Einstellungen und Lieferantenprofil.

    Reihenfolge: Standardwerte aus :class:`ExtractionSettings`, danach --
    falls vorhanden -- die Werte aus ``VendorProfile.table_hints``
    (``y_tolerance_factor``, ``x_bin``).  So bleibt der Standard fuer alle
    gueltig und ein Lieferant mit auffaelligem Zeilenabstand bekommt trotzdem
    seine eigene Einstellung.
    """
    extraction = getattr(settings, "extraction", settings) or ExtractionSettings()
    y_tolerance = _positive(getattr(extraction, "pdf_y_tolerance_factor", None), 0.6)
    x_bin = _positive(getattr(extraction, "pdf_x_bin", None), 4.0)

    hints = getattr(profile, "table_hints", None) or {}
    if isinstance(hints, dict):
        y_tolerance = _positive(hints.get("y_tolerance_factor"), y_tolerance)
        x_bin = _positive(hints.get("x_bin"), x_bin)
    return y_tolerance, x_bin


def _positive(value: object, fallback: float) -> float:
    """Zahl uebernehmen -- unbrauchbare Angaben aendern nichts."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


class PdfReader(DocumentReader):
    """Liest PDF-Angebote inklusive Tabellenstruktur."""

    extensions = (".pdf",)

    def __init__(self, y_tolerance_factor: float | None = None,
                 x_bin: float | None = None, settings: object | None = None,
                 profile: object | None = None, ocr_settings: object | None = None,
                 ocr_backend: object | None = None,
                 ocr_confirmed: bool | None = None) -> None:
        default_y, default_x = tolerances_for(settings, profile)
        #: Anteil der Zeilenhoehe, bis zu dem Woerter noch als eine Zeile gelten
        self.y_tolerance_factor = _positive(y_tolerance_factor, default_y)
        #: Rasterbreite fuer die Haeufigkeitsanalyse der Spaltenstarts
        self.x_bin = _positive(x_bin, default_x)

        extraction = getattr(settings, "extraction", settings) or ExtractionSettings()
        #: Linienverfahren ueberhaupt versuchen
        self.use_lattice = bool(getattr(extraction, "pdf_use_lattice", True))
        #: So viele senkrechte Linien muessen es mindestens sein
        self.min_vertical_lines = max(
            2, int(getattr(extraction, "pdf_min_vertical_lines", 3) or 3))
        hints = getattr(profile, "table_hints", None) or {}
        if isinstance(hints, dict) and "use_lattice" in hints:
            self.use_lattice = bool(hints["use_lattice"])

        # -- Texterkennung ------------------------------------------------
        #: Einstellungen der Texterkennung (auch aus einer Gesamtkonfiguration)
        self.ocr_settings = ocr_settings_of(ocr_settings if ocr_settings is not None
                                            else settings)
        #: Fest vorgegebenes Backend (Tests, oder wenn die Oberflaeche eines
        #: ausgewaehlt hat).  ``None`` = beim Lesen selbst suchen.
        self.ocr_backend = ocr_backend
        #: Hat der Anwender der Erkennung zugestimmt?  Bei
        #: ``ask_before_ocr`` bleibt OCR ohne ein ``True`` bewusst aus.
        self.ocr_confirmed = ocr_confirmed

    # ------------------------------------------------------------------
    def resolve_ocr_backend(self):
        """Das zu verwendende Backend bestimmen (oder ``None``).

        Reihenfolge: fest vorgegebenes Backend, sonst das erste verfuegbare
        gemaess Einstellungen.  Liefert ``None``, wenn OCR abgeschaltet ist
        oder nichts installiert ist -- der Aufrufer muss nichts weiter pruefen.
        """
        if self.ocr_backend is not None:
            if not bool(getattr(self.ocr_settings, "enabled", True)):
                return None
            return self.ocr_backend
        return best_backend(self.ocr_settings)

    def _ocr_allowed(self) -> bool:
        """Darf ohne Rueckfrage erkannt werden?"""
        if self.ocr_confirmed is True:
            return True
        if self.ocr_confirmed is False:
            return False
        return not bool(getattr(self.ocr_settings, "ask_before_ocr", True))

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

        # Texterkennung nur einmal je Dokument aufloesen -- die
        # Verfuegbarkeitspruefung kostet Zeit.
        backend = self.resolve_ocr_backend()
        allowed = self._ocr_allowed()
        ocr_pages = 0
        max_pages = max(0, int(getattr(self.ocr_settings, "max_pages", 20) or 0))
        scan_pages: list[int] = []

        for index in range(handle.page_count):
            page_number = index + 1
            try:
                page = handle.load_page(index)
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Seite {page_number} nicht lesbar: {exc}")
                document.pages.append("")
                continue

            # Quer gedrehte Seiten (rotation 90/270): PyMuPDF liefert die
            # Wortkoordinaten bereits im AUFGERICHTETEN Anzeigeraum -- die
            # Tabellenrekonstruktion arbeitet also auf der normalisierten
            # Seite.  Die Drehung wird trotzdem gemeldet, damit der Anwender
            # weiss, warum das Ergebnis besonders zu pruefen ist.
            rotation = int(getattr(page, "rotation", 0) or 0)
            if rotation in (90, 180, 270):
                document.meta.setdefault("rotated_pages", []).append(page_number)
                document.add_warning(
                    f"Seite {page_number} ist um {rotation} Grad gedreht "
                    "gespeichert und wurde fuer die Auswertung aufgerichtet -- "
                    "bitte das Ergebnis pruefen.")

            try:
                text = page.get_text("text") or ""
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Text der Seite {page_number} nicht lesbar: {exc}")
                text = ""
            document.pages.append(text)

            words = self._page_words(page, page_number, document)
            if words:
                for block in self._page_tables(page, words, page_number, document):
                    document.tables.append(block)

            for block in self._native_tables(page, page_number, document):
                document.tables.append(block)

            # -- Seite ohne Textebene: hier greift die Texterkennung --------
            if len(text.strip()) >= _MIN_CHARS_PER_PAGE:
                continue
            scan_pages.append(page_number)
            if backend is None or not allowed:
                continue
            if ocr_pages >= max_pages:
                document.add_warning(
                    f"Texterkennung wurde nach {max_pages} Seiten abgebrochen "
                    f"(Einstellung 'max_pages').  Seite {page_number} und "
                    f"folgende wurden nicht erkannt.")
                continue
            ocr_pages += 1
            self._ocr_page(page, page_number, document, backend)

        if scan_pages:
            document.meta["scan_pages"] = list(scan_pages)

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

    def _page_tables(self, page, words: list[_Word], page_number: int,
                     document: RawDocument) -> list[TableBlock]:
        """Erst das Linienverfahren, sonst das Koordinaten-Clustering."""
        # Zweispaltiges Seitenlayout (zwei Angebotsbloecke nebeneinander):
        # wird es erkannt, werden die Spalten getrennt ausgewertet und der
        # Anwender bekommt einen klaren Befund.
        split = find_column_split(words)
        if split is not None:
            blocks: list[TableBlock] = []
            for part, name in ((
                    [w for w in words if w.x1 <= split], "linke Spalte"), (
                    [w for w in words if w.x0 >= split], "rechte Spalte")):
                for block in words_to_tables(part, page_number,
                                             y_tolerance_factor=self.y_tolerance_factor,
                                             x_bin=self.x_bin):
                    block.title = f"{name} (zweispaltiges Layout)"
                    blocks.append(block)
            document.add_warning(
                f"Seite {page_number}: zweispaltiges Layout erkannt (zwei "
                "Angebotsbloecke nebeneinander).  Die Spalten wurden getrennt "
                "ausgewertet -- bitte das Ergebnis pruefen.")
            document.meta.setdefault("two_column_pages", []).append(page_number)
            if blocks:
                return blocks
        if self.use_lattice:
            verticals, horizontals = self._page_rulings(page, page_number, document)
            blocks = lattice_tables(words, verticals, horizontals, page_number,
                                    y_tolerance_factor=self.y_tolerance_factor,
                                    min_vertical_lines=self.min_vertical_lines)
            if blocks:
                logger.info("Seite %d: Spalten ueber %d gezeichnete Linien bestimmt",
                            page_number, len(verticals))
                return blocks
            logger.debug("Seite %d: zu wenige Tabellenlinien (%d) -- es wird ueber "
                         "die Wortkoordinaten gerechnet", page_number, len(verticals))
        return words_to_tables(words, page_number,
                               y_tolerance_factor=self.y_tolerance_factor,
                               x_bin=self.x_bin)

    def _page_rulings(self, page, page_number: int,
                      document: RawDocument) -> tuple[list[_Ruling], list[_Ruling]]:
        """Gezeichnete Linien der Seite holen (defensiv -- API variiert)."""
        try:
            drawings = page.get_drawings() or []
        except Exception as exc:  # noqa: BLE001 -- aeltere/andere PyMuPDF-Version
            logger.debug("Vektorzeichnungen der Seite %d nicht lesbar: %s",
                         page_number, exc)
            return [], []
        return page_rulings(drawings)

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
    def _ocr_page(self, page, page_number: int, document: RawDocument,
                  backend) -> None:
        """Eine Seite rendern, erkennen und wie ein Text-PDF auswerten."""
        dpi = _dpi_of(self.ocr_settings)
        preprocess = bool(getattr(self.ocr_settings, "preprocess", True))
        try:
            image = render_page_png(page, dpi=dpi, preprocess=preprocess)
        except Exception as exc:  # noqa: BLE001 -- Renderfehler nie durchlassen
            document.add_warning(
                f"Seite {page_number} konnte fuer die Texterkennung nicht "
                f"gerendert werden: {exc}")
            logger.warning("Rendern der Seite %d fehlgeschlagen: %s",
                           page_number, exc, exc_info=True)
            return
        if not image:
            document.add_warning(
                f"Seite {page_number} lieferte kein Bild fuer die Texterkennung.")
            return

        language = str(getattr(self.ocr_settings, "language", "de") or "de")
        try:
            result = backend.recognize(image, language)
        except Exception as exc:  # noqa: BLE001 -- fremder Code, doppelter Boden
            document.add_warning(
                f"Texterkennung auf Seite {page_number} fehlgeschlagen: {exc}")
            logger.warning("OCR auf Seite %d fehlgeschlagen: %s",
                           page_number, exc, exc_info=True)
            return

        ocr_page_into_document(
            document, result, page_number,
            ocr_settings=self.ocr_settings,
            y_tolerance_factor=self.y_tolerance_factor,
            x_bin=self.x_bin,
            scale=72.0 / float(dpi),
            origin=OCR_ORIGIN,
        )

    # ------------------------------------------------------------------
    def _check_for_scan(self, document: RawDocument) -> None:
        """Scan melden -- je nachdem, ob die Erkennung gegriffen hat.

        Drei Faelle, drei verschiedene Meldungen.  Es waere falsch, hier
        pauschal zu warnen: Wurde erkannt, ist die Warnung eine *andere*
        (naemlich die ueber die Unsicherheit des Erkannten).
        """
        pages = document.pages or [""]
        chars = sum(len(p.strip()) for p in pages)
        if chars >= _MIN_CHARS_PER_PAGE * len(pages) and not document.meta.get("scan_pages"):
            return

        document.meta["scanned"] = True
        ocr_meta = document.meta.get("ocr") or {}
        if ocr_meta.get("pages"):
            # Erkannt -- die Unsicherheitswarnung steht bereits im Dokument
            logger.info("PDF ohne Textebene, Texterkennung hat gegriffen: %s",
                        document.source_path)
            return

        backend = self.resolve_ocr_backend()
        if backend is not None and not self._ocr_allowed():
            document.add_warning(OCR_ASK_HINT)
        elif backend is None:
            document.add_warning(SCAN_WARNING_WITH_HINT)
        else:
            document.add_warning(SCAN_WARNING)
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


#: Kopfbegriffe, die in einem Angebotsblock vorkommen.  Tauchen mehrere davon
#: LINKS UND RECHTS eines breiten Leerstreifens auf, stehen zwei vollstaendige
#: Bloecke nebeneinander -- eine gewoehnliche Tabelle nennt "Preis" nur einmal.
_TWO_COLUMN_KEYWORDS = ("pos", "material", "artikel", "bezeichnung", "menge",
                        "preis", "price", "qty", "item", "description")

#: So breit muss der Leerstreifen zwischen zwei Layoutspalten mindestens sein
_MIN_COLUMN_GAP = 30.0


def find_column_split(words: list[_Word]) -> float | None:
    """Trennlinie eines zweispaltigen Seitenlayouts finden (oder ``None``).

    Bewusst streng, damit gewoehnliche Tabellen nie faelschlich zerschnitten
    werden:  Es muss einen durchgehend leeren senkrechten Streifen im mittleren
    Seitenbereich geben, beide Haelften muessen substanziellen Inhalt tragen,
    und mindestens zwei typische Spaltenkopf-Begriffe ("Pos", "Preis", ...)
    muessen auf BEIDEN Seiten auftauchen.
    """
    if len(words) < 16:
        return None
    x_min = min(w.x0 for w in words)
    x_max = max(w.x1 for w in words)
    span = x_max - x_min
    if span < 250:
        return None

    # Belegte x-Bereiche ueber die ganze Seite zusammenfassen
    intervals: list[tuple[float, float]] = []
    for x0, x1 in sorted((w.x0, w.x1) for w in words):
        if intervals and x0 <= intervals[-1][1] + 1.0:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], x1))
        else:
            intervals.append((x0, x1))

    best: tuple[float, float] | None = None       # (Luecke, Mitte)
    for (_, left_end), (right_start, _) in zip(intervals, intervals[1:]):
        gap = right_start - left_end
        center = (left_end + right_start) / 2.0
        if gap < _MIN_COLUMN_GAP:
            continue
        if not (x_min + span * 0.25 <= center <= x_min + span * 0.75):
            continue
        if best is None or gap > best[0]:
            best = (gap, center)
    if best is None:
        return None
    split = best[1]

    left = [w for w in words if w.x1 <= split]
    right = [w for w in words if w.x0 >= split]
    if len(left) < 8 or len(right) < 8:
        return None

    def _keywords(part: list[_Word]) -> set[str]:
        text = " ".join(w.text.lower() for w in part)
        return {k for k in _TWO_COLUMN_KEYWORDS if k in text}

    if len(_keywords(left) & _keywords(right)) < 2:
        return None
    return split


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


# ---------------------------------------------------------------------------
# Linienverfahren (lattice)
# ---------------------------------------------------------------------------

def _point(value: object) -> tuple[float, float] | None:
    """(x, y) aus einem PyMuPDF-Point oder einem einfachen Paar."""
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if x is not None and y is not None:
        return float(x), float(y)
    try:
        return float(value[0]), float(value[1])       # type: ignore[index]
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _rectangle(value: object) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) aus einem PyMuPDF-Rect oder einem 4er-Tupel."""
    if all(hasattr(value, name) for name in ("x0", "y0", "x1", "y1")):
        return (float(value.x0), float(value.y0),      # type: ignore[attr-defined]
                float(value.x1), float(value.y1))      # type: ignore[attr-defined]
    try:
        x0, y0, x1, y1 = (float(v) for v in value)     # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def _add_segment(x0: float, y0: float, x1: float, y1: float,
                 verticals: list[_Ruling], horizontals: list[_Ruling]) -> None:
    """Eine Strecke einsortieren -- schraege Striche bleiben unbeachtet."""
    if abs(x1 - x0) <= _LINE_THICKNESS and abs(y1 - y0) >= _MIN_LINE_LENGTH:
        verticals.append(_Ruling((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
    elif abs(y1 - y0) <= _LINE_THICKNESS and abs(x1 - x0) >= _MIN_LINE_LENGTH:
        horizontals.append(_Ruling((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))


def page_rulings(drawings) -> tuple[list[_Ruling], list[_Ruling]]:
    """Aus den Vektorzeichnungen einer Seite die Tabellenlinien gewinnen.

    Beruecksichtigt Strecken (``"l"``) und schmale Rechtecke (``"re"``) --
    viele PDF-Erzeuger geben eine Linie als duennes gefuelltes Rechteck aus.
    Rueckgabe: (senkrechte Linien, waagerechte Linien), jeweils zusammengefasst.
    """
    verticals: list[_Ruling] = []
    horizontals: list[_Ruling] = []
    for drawing in drawings or []:
        if isinstance(drawing, dict):
            items = drawing.get("items") or []
        else:
            items = getattr(drawing, "items", None) or []
        for item in items:
            try:
                kind = item[0]
            except (TypeError, IndexError):
                continue
            if kind == "l" and len(item) >= 3:
                start, end = _point(item[1]), _point(item[2])
                if start is None or end is None:
                    continue
                _add_segment(start[0], start[1], end[0], end[1], verticals, horizontals)
            elif kind == "re" and len(item) >= 2:
                rect = _rectangle(item[1])
                if rect is None:
                    continue
                x0, y0, x1, y1 = rect
                width, height = abs(x1 - x0), abs(y1 - y0)
                if width <= _LINE_THICKNESS and height >= _MIN_LINE_LENGTH:
                    verticals.append(_Ruling((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
                elif height <= _LINE_THICKNESS and width >= _MIN_LINE_LENGTH:
                    horizontals.append(_Ruling((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
                elif width >= _MIN_LINE_LENGTH and height >= _MIN_LINE_LENGTH:
                    # Ein echter Rahmen: seine vier Kanten sind ebenfalls Linien
                    verticals.append(_Ruling(x0, min(y0, y1), max(y0, y1)))
                    verticals.append(_Ruling(x1, min(y0, y1), max(y0, y1)))
                    horizontals.append(_Ruling(y0, min(x0, x1), max(x0, x1)))
                    horizontals.append(_Ruling(y1, min(x0, x1), max(x0, x1)))
    return _merge_rulings(verticals), _merge_rulings(horizontals)


def _merge_rulings(rulings: list[_Ruling],
                   tolerance: float = _LINE_MERGE_TOLERANCE) -> list[_Ruling]:
    """Dicht beieinander liegende Linien zu einer zusammenfassen."""
    if not rulings:
        return []
    merged: list[_Ruling] = []
    for ruling in sorted(rulings, key=lambda r: r.position):
        if merged and abs(ruling.position - merged[-1].position) <= tolerance:
            previous = merged[-1]
            merged[-1] = _Ruling((previous.position + ruling.position) / 2.0,
                                 min(previous.start, ruling.start),
                                 max(previous.end, ruling.end))
        else:
            merged.append(ruling)
    return merged


def _line_center(line: list[_Word]) -> float:
    return sum(w.y_center for w in line) / len(line)


def _column_index(x: float, bounds: list[float]) -> int:
    """In welche Spalte faellt eine x-Position (Grenzen sind aufsteigend)?"""
    index = 0
    for bound in bounds:
        if x >= bound:
            index += 1
        else:
            break
    return index


def lattice_tables(words: list[_Word], verticals: list[_Ruling],
                   horizontals: list[_Ruling], page: int,
                   y_tolerance_factor: float = 0.6,
                   min_vertical_lines: int = 3) -> list[TableBlock]:
    """Tabelle ueber gezeichnete Linien schneiden.

    Verwendet werden nur senkrechte Linien, die sich ueber mehrere Textzeilen
    erstrecken -- ein Strich neben einem Logo ist keine Spaltengrenze.  Sind es
    weniger als ``min_vertical_lines``, liefert die Funktion eine leere Liste;
    der Aufrufer faellt dann auf das Koordinatenverfahren zurueck.
    """
    lines = _cluster_lines(words, y_tolerance_factor)
    if len(lines) < 2:
        return []
    centers = [_line_center(line) for line in lines]

    useful: list[_Ruling] = []
    for ruling in verticals:
        covered = sum(1 for c in centers if ruling.start - 2.0 <= c <= ruling.end + 2.0)
        if covered >= 2:
            useful.append(ruling)
    useful = _merge_rulings(useful)
    if len(useful) < max(2, min_vertical_lines):
        return []

    bounds = sorted(r.position for r in useful)
    band_edges = sorted(r.position for r in horizontals if r.length >= _MIN_LINE_LENGTH)
    use_bands = len(band_edges) >= 3

    rows: list[list[str]] = []
    previous_band: int | None = None
    for line, center in zip(lines, centers):
        cells: list[list[str]] = [[] for _ in range(len(bounds) + 1)]
        for word in line:
            index = _column_index((word.x0 + word.x1) / 2.0, bounds)
            cells[index].append(word.text)
        texts = [" ".join(cell).strip() for cell in cells]
        band = sum(1 for edge in band_edges if edge < center) if use_bands else None

        if use_bands and rows and band == previous_band:
            # Innerhalb derselben Zeile des Liniengitters: umbrochener Text
            # gehoert in dieselbe Zelle und darf keine neue Zeile werden.
            rows[-1] = [" ".join(part for part in (old, new) if part).strip()
                        for old, new in zip(rows[-1], texts)]
        else:
            rows.append(texts)
        previous_band = band

    rows = [row for row in rows if any(cell for cell in row)]
    multi = sum(1 for row in rows if sum(1 for c in row if c) >= 2)
    if len(rows) < 2 or multi < 2:
        return []
    return [TableBlock(rows=rows, page=page, origin="pdf-lattice")]


def make_ruling(position: float, start: float, end: float) -> _Ruling:
    """Hilfsfunktion fuer Tests: eine Linie erzeugen."""
    return _Ruling(position, start, end)


def make_word(x0: float, y0: float, x1: float, y1: float, text: str) -> _Word:
    """Hilfsfunktion fuer Tests: ein Wort mit Koordinaten erzeugen."""
    return _Word(x0, y0, x1, y1, text)


# ---------------------------------------------------------------------------
# Texterkennung (OCR)
#
# Bewusst als freie Funktionen: Der Bildleser (image_reader.py) verwendet
# genau dieselben Bausteine.  Es gibt nur EINE Tabellenrekonstruktion --
# ob die Woerter aus der Textebene eines PDFs oder aus einer Erkennung
# stammen, aendert daran nichts.
# ---------------------------------------------------------------------------

#: Unterhalb dieser Aufloesung wird OCR unbrauchbar, oberhalb nur langsamer
_MIN_DPI = 72
_MAX_DPI = 600


def _dpi_of(ocr_settings: object | None) -> int:
    """Renderaufloesung aus den Einstellungen -- auf sinnvolle Werte begrenzt."""
    try:
        dpi = int(getattr(ocr_settings, "dpi", 300) or 300)
    except (TypeError, ValueError):
        dpi = 300
    return max(_MIN_DPI, min(_MAX_DPI, dpi))


def render_page_png(page, dpi: int = 300, preprocess: bool = True) -> bytes:
    """Eine PyMuPDF-Seite als PNG-Bytes rendern.

    ``preprocess`` rendert in Graustufen.  Das ist keine Kosmetik: Farbrauschen
    aus einem Fotoscan kostet die Erkennung sonst spuerbar Genauigkeit -- und
    die Datenmenge sinkt nebenbei auf ein Drittel.
    """
    import fitz  # PyMuPDF

    kwargs: dict = {"dpi": int(dpi)}
    if preprocess:
        kwargs["colorspace"] = fitz.csGRAY
    try:
        pixmap = page.get_pixmap(**kwargs)
    except TypeError:
        # Aeltere PyMuPDF-Versionen kennen 'dpi' noch nicht -- ueber die
        # Matrix skalieren (72 dpi ist die Grundaufloesung eines PDFs).
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pixmap = page.get_pixmap(matrix=matrix)
    return bytes(pixmap.tobytes("png"))


def ocr_words_to_words(ocr_words, scale: float = 1.0) -> list[_Word]:
    """Erkannte Woerter in die interne Wortstruktur des Layoutverfahrens ueberfuehren.

    ``scale`` rechnet Bildpixel in PDF-Punkte um (``72 / dpi``).  Damit gelten
    fuer erkannte Seiten dieselben Toleranzen wie fuer echte Text-PDFs -- sonst
    muesste die Zeilen- und Spaltenerkennung zweimal gepflegt werden.
    """
    factor = float(scale) if scale and scale > 0 else 1.0
    words: list[_Word] = []
    for word in ocr_words or []:
        text = str(getattr(word, "text", "") or "").strip()
        if not text:
            continue
        x0 = float(getattr(word, "x0", 0.0)) * factor
        y0 = float(getattr(word, "y0", 0.0)) * factor
        x1 = float(getattr(word, "x1", 0.0)) * factor
        y1 = float(getattr(word, "y1", 0.0)) * factor
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        if y1 - y0 < 1.0:
            # Ohne brauchbare Hoehe funktioniert das Zeilen-Clustering nicht
            y1 = y0 + 1.0
        words.append(_Word(x0, y0, x1, y1, text))
    return words


def ocr_page_into_document(document: RawDocument, result, page_number: int,
                           ocr_settings: object | None = None,
                           y_tolerance_factor: float = 0.6, x_bin: float = 4.0,
                           scale: float = 1.0,
                           origin: str = OCR_ORIGIN) -> bool:
    """Ein Erkennungsergebnis in ein :class:`RawDocument` einarbeiten.

    Erledigt alles, was zur Ehrlichkeit gehoert:

    * Seitentext setzen (bisher war die Seite leer)
    * Tabelle ueber dieselbe Layoutanalyse rekonstruieren, ``origin`` setzen
    * Konfidenz je Zelle als *parallele Matrix* in ``meta["ocr"]`` ablegen --
      der :class:`TableBlock` selbst bleibt unveraendert, damit andere Module
      nichts anpassen muessen
    * unsichere Zellen und Ziffernverdachtsfaelle auflisten
    * eine deutliche Warnung setzen

    Rueckgabe: ``True``, wenn ein Tabellenblock entstanden ist.
    """
    min_confidence = _min_confidence_of(ocr_settings)
    text = str(getattr(result, "text", "") or "")
    warnings = list(getattr(result, "warnings", None) or [])
    ocr_words = list(getattr(result, "words", None) or [])
    backend_name = str(getattr(result, "backend_name", "") or "")
    mean_confidence = getattr(result, "mean_confidence", None)

    # -- Seitentext hinterlegen ----------------------------------------
    if text:
        while len(document.pages) < page_number:
            document.pages.append("")
        document.pages[page_number - 1] = text

    # -- Tabelle ueber die vorhandene Layoutanalyse ---------------------
    blocks = words_to_tables(ocr_words_to_words(ocr_words, scale), page_number,
                             y_tolerance_factor=y_tolerance_factor, x_bin=x_bin)
    findings: list[dict] = []
    matrices: list[dict] = []
    for block in blocks:
        block.origin = origin
        block.title = (f"Texterkennung, mittlere Sicherheit "
                       f"{confidence_percent(mean_confidence)}")
        matrix = cell_confidences(block.rows, ocr_words)
        matrices.append({"page": page_number, "origin": origin, "rows": matrix})
        findings.extend(uncertain_cells(block.rows, matrix, min_confidence,
                                        page=page_number))
        document.tables.append(block)

    # -- Buchfuehrung in den Metadaten ---------------------------------
    low_words = [w for w in ocr_words
                 if getattr(w, "confidence", None) is not None
                 and w.confidence < min_confidence]
    meta = document.meta.setdefault("ocr", {})
    meta.setdefault("backend", backend_name)
    meta.setdefault("min_confidence", min_confidence)
    meta.setdefault("pages", [])
    meta.setdefault("cell_confidence", [])
    meta.setdefault("uncertain_cells", [])
    meta["pages"].append({
        "page": page_number,
        "backend": backend_name,
        "mean_confidence": mean_confidence,
        "word_count": len(ocr_words),
        "low_confidence_words": len(low_words),
        "tables": len(blocks),
    })
    meta["cell_confidence"].extend(matrices)
    meta["uncertain_cells"].extend(findings)

    # -- Warnungen ------------------------------------------------------
    for note in warnings:
        document.add_warning(f"Texterkennung Seite {page_number}: {note}")
    if ocr_words or text:
        document.add_warning(ocr_warning(mean_confidence, backend_name,
                                         len(low_words)))
    else:
        document.add_warning(
            f"Die Texterkennung hat auf Seite {page_number} nichts gefunden.  "
            f"Die Vorlage ist vermutlich zu schwach aufgeloest, zu dunkel oder "
            f"zu schief eingescannt.")
    for finding in findings:
        document.add_warning(
            f"Unsicher erkannt (Seite {finding['page']}, Zeile "
            f"{finding['row'] + 1}, Spalte {finding['column'] + 1}): "
            f"{finding['hinweis']}")
    return bool(blocks)


def _min_confidence_of(ocr_settings: object | None) -> float:
    """Schwelle fuer "unsicher" -- unbrauchbare Angaben ergeben 0.60."""
    try:
        value = float(getattr(ocr_settings, "min_confidence", 0.60))
    except (TypeError, ValueError):
        return 0.60
    return value if 0.0 <= value <= 1.0 else 0.60
