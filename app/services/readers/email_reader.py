"""Leser fuer E-Mails (.eml/.msg), Textdateien und eingefuegten Text.

Im Einkaufsalltag ist die E-Mail eine *vollwertige* Angebotsquelle: sehr viele
Preise kommen als formlose Mail ("wir erhoehen zum 01.09. auf 12,85 EUR") oder
als HTML-Tabelle direkt im Nachrichtentext -- ohne jedes PDF.  Dieser Leser
behandelt die Mail deshalb gleichrangig zu einem Angebotsdokument:

* Kopfdaten (Absender, Betreff, Datum) landen in :class:`EmailContext`
* Der Textkoerper wird -- optional -- von Signatur, Disclaimer und Zitatbloecken
  befreit, damit dort keine alten Preise mitgelesen werden
* HTML-Tabellen werden als :class:`TableBlock` rekonstruiert
* Anhaenge werden (nach Endung und Groesse gefiltert) in ein temporaeres
  Verzeichnis geschrieben und rekursiv eingelesen
"""

from __future__ import annotations

import logging
import re
import tempfile
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from ...config.settings import ExtractionSettings
from ...models.enums import SourceKind
from ...models.offer import EmailContext
from ...utils.msg_reader import read_msg
from .base import DocumentReader, RawDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["EmailReader", "TextReader", "HtmlTableParser", "html_to_text",
           "strip_signature", "text_tables", "SIGNATURE_MARKERS"]

#: Zeilenanfaenge, ab denen der Rest einer Mail typischerweise nur noch
#: Signatur, Disclaimer oder ein zitierter aelterer Verlauf ist.
SIGNATURE_MARKERS: tuple[str, ...] = (
    "-- ",
    "--\n",
    "mit freundlichen gruessen",
    "mit freundlichen grüßen",
    "mit freundlichem gruss",
    "freundliche gruesse",
    "freundliche grüße",
    "viele gruesse",
    "viele grüße",
    "beste gruesse",
    "beste grüße",
    "best regards",
    "kind regards",
    "yours sincerely",
    "sincerely yours",
    "diese e-mail enthaelt vertrauliche",
    "diese e-mail enthält vertrauliche",
    "diese nachricht enthaelt vertrauliche",
    "diese nachricht enthält vertrauliche",
    "this e-mail contains confidential",
    "this email and any files",
    "the information contained in this",
    "vertraulichkeitshinweis",
    "disclaimer:",
    "-----urspruengliche nachricht-----",
    "-----ursprüngliche nachricht-----",
    "-----original message-----",
    "________________________________",
)

#: Beginn eines zitierten Nachrichtenblocks ("Von: ... Gesendet: ...")
_QUOTE_BLOCK = re.compile(
    r"^\s*(?:von|from|gesendet von|am .{0,40}schrieb)\s*:?\s",
    re.I,
)

_MAX_TEMP_NAME = 120


# ---------------------------------------------------------------------------
# HTML -> Text und HTML -> Tabellen
# ---------------------------------------------------------------------------

class HtmlTableParser(HTMLParser):
    """Wandelt HTML in Klartext und sammelt dabei alle Tabellen.

    Bewusst mit der Standardbibliothek: keine externe Abhaengigkeit, und der
    Parser ist fehlertolerant genug fuer das ueblicherweise sehr wilde
    Outlook-/Newsletter-HTML.
    """

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5",
                   "h6", "table", "blockquote", "pre"}
    _SKIP_TAGS = {"script", "style", "head", "title", "meta"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._row_stack: list[list[str]] = []
        self._cell: list[str] | None = None
        self._skip_depth = 0

    # -- HTMLParser-Schnittstelle ---------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self._table_stack.append([])
            self._row_stack.append([])
        elif tag == "tr":
            if self._table_stack:
                self._flush_row()
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "li":
            self.text_parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("td", "th"):
            if self._cell is not None and self._row_stack:
                cell_text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
                self._row_stack[-1].append(cell_text)
                self.text_parts.append(cell_text + "\t")
            self._cell = None
        elif tag == "tr":
            self._flush_row()
            self.text_parts.append("\n")
        elif tag == "table":
            self._flush_row()
            if self._table_stack:
                rows = self._table_stack.pop()
                if self._row_stack:
                    self._row_stack.pop()
                if rows:
                    self.tables.append(rows)
            self.text_parts.append("\n")
        elif tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:  # noqa: D102
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self.text_parts.append(data)

    # -- intern ----------------------------------------------------------
    def _flush_row(self) -> None:
        if not self._table_stack or not self._row_stack:
            return
        row = self._row_stack[-1]
        if any(cell for cell in row):
            self._table_stack[-1].append(list(row))
        self._row_stack[-1] = []

    def close(self) -> None:  # noqa: D102
        super().close()
        while self._table_stack:
            self._flush_row()
            rows = self._table_stack.pop()
            if self._row_stack:
                self._row_stack.pop()
            if rows:
                self.tables.append(rows)

    # -- Ergebnis --------------------------------------------------------
    @property
    def text(self) -> str:
        raw = "".join(self.text_parts)
        raw = raw.replace("\xa0", " ").replace("\t", "  ")
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> tuple[str, list[list[list[str]]]]:
    """HTML in (Text, Tabellen) zerlegen.  Wirft nie."""
    if not html:
        return "", []
    parser = HtmlTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 -- kaputtes HTML ist der Normalfall
        logger.debug("HTML nur teilweise ausgewertet: %s", exc)
    text = parser.text
    if not text:
        # Notfalls Tags stumpf entfernen -- besser als gar kein Text
        text = unescape(re.sub(r"<[^>]+>", " ", html))
        text = re.sub(r"\s+", " ", text).strip()
    return text, parser.tables


# ---------------------------------------------------------------------------
# Klartext -> Tabellen ("text-grid")
# ---------------------------------------------------------------------------

#: Trennzeichen, die im Alltag als Spaltentrenner auftauchen.  Das Komma fehlt
#: bewusst: in deutschen Preisangaben ist es der Dezimaltrenner, eine Zeile wie
#: "Dichtring, 12,85 EUR" waere sonst eine dreispaltige Tabelle.
_GRID_DELIMITERS: tuple[str, ...] = ("\t", ";", "|")

#: So viele Zeilen muss ein Block mindestens haben (Kopf + eine Datenzeile)
_MIN_GRID_ROWS = 2


def text_tables(text: str) -> list[TableBlock]:
    """Trennzeichen-Tabellen in Klartext finden ("text-grid").

    Eingefuegter Text aus der Zwischenablage und schlicht gespeicherte
    Preislisten kommen haeufig als Semikolon- oder Tabulatorliste an -- ohne
    Dateiendung, die den CSV-Leser aktivieren wuerde.  Diese Funktion holt
    daraus wieder eine Tabellenstruktur heraus, damit die Spaltenerkennung
    greifen kann.

    Erkannt wird nur, was eindeutig ist: mindestens zwei aufeinanderfolgende
    Zeilen mit demselben Trennzeichen und derselben Spaltenzahl.  Alles andere
    bleibt Fliesstext -- lieber keine Tabelle als eine falsche.
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    best: list[TableBlock] = []
    best_cells = 0
    for delimiter in _GRID_DELIMITERS:
        blocks = _grid_blocks(lines, delimiter)
        cells = sum(len(b.rows) * b.column_count for b in blocks)
        if cells > best_cells:
            best, best_cells = blocks, cells
    return best


def _grid_blocks(lines: list[str], delimiter: str) -> list[TableBlock]:
    """Zusammenhaengende Bloecke gleich breiter Trennzeichenzeilen."""
    blocks: list[TableBlock] = []
    current: list[list[str]] = []

    def flush() -> None:
        if len(current) >= _MIN_GRID_ROWS:
            blocks.append(TableBlock(rows=[list(r) for r in current],
                                     origin="text-grid",
                                     title="Textliste mit Trennzeichen"))
        current.clear()

    for line in lines:
        if delimiter not in line:
            flush()
            continue
        cells = [c.strip() for c in line.split(delimiter)]
        if len(cells) < 2 or not any(cells):
            flush()
            continue
        if current and len(cells) != len(current[0]):
            # Andere Spaltenzahl: das ist ein neuer Block, kein Fortsetzen --
            # sonst entstuenden aus Fliesstext krumme Pseudotabellen.
            flush()
        current.append(cells)
    flush()
    return blocks


# ---------------------------------------------------------------------------
# Signatur/Disclaimer
# ---------------------------------------------------------------------------

def strip_signature(text: str) -> tuple[str, str]:
    """Trennt den Nachrichtentext von Signatur/Disclaimer/Zitat.

    Rueckgabe ``(text, abgeschnittener_teil)``.  Es wird nur abgeschnitten,
    wenn davor noch genug Text uebrig bleibt -- sonst wuerde bei einer sehr
    kurzen Preismitteilung der eigentliche Inhalt verschwinden.
    """
    if not text:
        return "", ""
    lines = text.splitlines()
    cut_at: int | None = None
    for index, line in enumerate(lines):
        lowered = line.strip().lower()
        if not lowered:
            continue
        if line.rstrip() == "--" or line.startswith("-- "):
            cut_at = index
            break
        if any(lowered.startswith(marker.strip()) for marker in SIGNATURE_MARKERS if marker.strip()):
            cut_at = index
            break
        if _QUOTE_BLOCK.match(line) and index > 0:
            cut_at = index
            break
    if cut_at is None:
        return text, ""
    head = "\n".join(lines[:cut_at]).strip()
    tail = "\n".join(lines[cut_at:]).strip()
    if len(head) < 20:
        # Zu wenig uebrig -- lieber alles behalten als Inhalt verlieren
        return text, ""
    return head, tail


# ---------------------------------------------------------------------------
# E-Mail-Leser
# ---------------------------------------------------------------------------

class EmailReader(DocumentReader):
    """Liest ``.eml`` (stdlib ``email``) und ``.msg`` (eigener OLE-Parser)."""

    extensions = (".eml", ".msg")

    def __init__(self, settings: ExtractionSettings | None = None,
                 attachment_reader: Callable[[str], RawDocument] | None = None,
                 temp_dir: str | None = None) -> None:
        self.settings = settings or ExtractionSettings()
        #: Wird von der Registry gesetzt, damit Anhaenge rekursiv gelesen werden
        self.attachment_reader = attachment_reader
        self.temp_dir = temp_dir

    # ------------------------------------------------------------------
    def read(self, path: str) -> RawDocument:
        suffix = Path(path).suffix.lower()
        if suffix == ".msg":
            return self._read_msg(path)
        return self._read_eml(path)

    # -- .eml ------------------------------------------------------------
    def _read_eml(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.EMAIL_BODY)
        try:
            with open(path, "rb") as handle:
                message = BytesParser(policy=policy.default).parse(handle)
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"E-Mail konnte nicht gelesen werden: {exc}")
            logger.warning("EML nicht lesbar (%s): %s", path, exc)
            return document

        context = EmailContext()
        try:
            context.subject = _header(message, "Subject")
            sender = _header(message, "From")
            context.from_name, context.from_address = _split_address(sender)
            context.to = _header(message, "To")
            context.message_id = _header(message, "Message-ID")
            date_header = _header(message, "Date")
            if date_header:
                try:
                    parsed = parsedate_to_datetime(date_header)
                    context.sent = parsed.replace(tzinfo=None) if parsed else None
                except (TypeError, ValueError):
                    document.add_warning(f"Datum der E-Mail unlesbar: {date_header}")
            document.meta["headers"] = "\n".join(f"{k}: {v}" for k, v in message.items())
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Kopfdaten der E-Mail unvollstaendig: {exc}")

        plain, html, attachments = self._walk_eml(message, document)
        self._finish(document, context, plain, html, attachments)
        logger.info("E-Mail gelesen: %s (Betreff=%r, %d Anhaenge)",
                    Path(path).name, context.subject, len(document.attachments))
        return document

    def _walk_eml(self, message, document: RawDocument
                  ) -> tuple[str, str, list[tuple[str, bytes]]]:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[tuple[str, bytes]] = []
        try:
            parts = message.walk()
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Nachrichtenstruktur nicht lesbar: {exc}")
            return "", "", []

        # Teile einer eingebetteten Nachricht duerfen nicht zusaetzlich als
        # Text des aeusseren Briefes gelten, sonst steht alles doppelt da.
        uebersprungen: set[int] = set()

        for part in parts:
            try:
                if id(part) in uebersprungen:
                    continue
                content_type = part.get_content_type()

                # Weitergeleitete Mail: Der Anhang ist selbst eine Nachricht.
                # Sie ist mehrteilig (wuerde also gleich uebersprungen) und
                # get_payload(decode=True) liefert dafuer None -- deshalb hier
                # eigens behandeln und serialisiert weiterreichen.
                if content_type == "message/rfc822":
                    eingebettet = part.get_payload()
                    if isinstance(eingebettet, list):
                        eingebettet = eingebettet[0] if eingebettet else None

                    # Ist die eingebettete Nachricht base64-kodiert (das ist bei
                    # Weiterleitungen die Regel), liefert nur decode=True die
                    # tatsaechlichen Bytes.  Ohne diesen Schritt reicht man den
                    # Base64-Text weiter, und die Erkennung findet erwartungs-
                    # gemaess nichts.
                    rohdaten = b""
                    try:
                        rohdaten = part.get_payload(decode=True) or b""
                    except Exception:  # noqa: BLE001
                        rohdaten = b""
                    if not rohdaten and eingebettet is not None and                             hasattr(eingebettet, "as_bytes"):
                        rohdaten = eingebettet.as_bytes()

                    rohdaten = _unwrap_base64(rohdaten)
                    if rohdaten:
                        if eingebettet is not None and hasattr(eingebettet, "walk"):
                            for teil in eingebettet.walk():
                                uebersprungen.add(id(teil))
                        name = part.get_filename() or ""
                        if not name and eingebettet is not None and                                 hasattr(eingebettet, "get"):
                            name = _safe_filename(
                                str(eingebettet.get("Subject") or ""))[:60]
                        if not name:
                            name = "Weitergeleitete Nachricht"
                        if not name.lower().endswith((".eml", ".msg")):
                            name += ".eml"
                        attachments.append((name, rohdaten))
                    continue

                if part.is_multipart():
                    continue
                disposition = (part.get_content_disposition() or "").lower()
                filename = part.get_filename() or ""
                if disposition == "attachment" or filename:
                    payload = part.get_payload(decode=True) or b""
                    attachments.append((filename or "anhang.bin", payload))
                    continue
                if content_type == "text/plain":
                    plain_parts.append(_part_text(part))
                elif content_type == "text/html":
                    html_parts.append(_part_text(part))
            except Exception as exc:  # noqa: BLE001
                document.add_warning(f"Teil der E-Mail nicht lesbar: {exc}")
        return "\n".join(plain_parts).strip(), "\n".join(html_parts).strip(), attachments

    # -- .msg ------------------------------------------------------------
    def _read_msg(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.EMAIL_BODY)
        msg = read_msg(path)
        for error in msg.errors:
            document.add_warning(error)
        if not msg.ok and msg.errors:
            document.add_warning("Die .msg-Datei konnte nicht ausgewertet werden. "
                                 "Bitte die Mail als .eml oder den Anhang direkt "
                                 "speichern und erneut importieren.")
            return document

        context = EmailContext(
            from_name=msg.sender_name,
            from_address=msg.sender_email,
            to=msg.to,
            subject=msg.subject,
            sent=msg.sent,
            message_id=msg.header_value("Message-ID"),
        )
        document.meta["headers"] = msg.headers
        attachments = [(a.name, a.data) for a in msg.attachments]
        self._finish(document, context, msg.body, msg.html, attachments)
        logger.info("MSG gelesen: %s (Betreff=%r, %d Anhaenge)",
                    Path(path).name, msg.subject, len(document.attachments))
        return document

    # -- gemeinsamer Abschluss -------------------------------------------
    def _finish(self, document: RawDocument, context: EmailContext,
                plain: str, html: str, attachments: list[tuple[str, bytes]]) -> None:
        body = plain.strip()
        html_tables: list[list[list[str]]] = []
        if html:
            html_text, html_tables = html_to_text(html)
            if len(html_text) > len(body):
                body = html_text
        body = body.replace("\r\n", "\n").replace("\r", "\n")

        stripped = ""
        if self.settings.strip_email_signature:
            body, stripped = strip_signature(body)
            if stripped:
                document.meta["stripped_signature"] = stripped

        context.body_text = body
        context.attachment_names = [name for name, _ in attachments]
        document.email = context
        document.text = _compose_email_text(context, body)
        document.pages = [document.text]

        for rows in html_tables:
            cleaned = [[cell for cell in row] for row in rows if any(c.strip() for c in row)]
            if len(cleaned) >= 2 and max(len(r) for r in cleaned) >= 2:
                document.tables.append(TableBlock(rows=cleaned, origin="html",
                                                 title="HTML-Tabelle aus der E-Mail"))

        self._process_attachments(document, attachments)

    # ------------------------------------------------------------------
    def _process_attachments(self, document: RawDocument,
                             attachments: list[tuple[str, bytes]]) -> None:
        if not attachments:
            return
        if not self.settings.process_email_attachments:
            document.add_warning(
                f"{len(attachments)} Anhang/Anhaenge wurden laut Einstellung nicht "
                "mitgelesen."
            )
            return

        allowed = {e.lower() for e in self.settings.attachment_extensions}
        limit = max(1, self.settings.max_attachment_mb) * 1024 * 1024
        target_dir: Path | None = None

        for name, data in attachments:
            safe_name = _safe_filename(name)
            suffix = Path(safe_name).suffix.lower()
            if suffix not in allowed:
                document.add_warning(
                    f"Anhang '{name}' ({suffix or 'ohne Endung'}) wird nicht "
                    "ausgewertet -- nicht in der Liste der Angebotsformate."
                )
                continue
            if len(data) > limit:
                document.add_warning(
                    f"Anhang '{name}' ist mit {len(data) / 1024 / 1024:.1f} MB groesser "
                    f"als das Limit von {self.settings.max_attachment_mb} MB."
                )
                continue
            if not data:
                document.add_warning(f"Anhang '{name}' ist leer.")
                continue

            if target_dir is None:
                base = self.temp_dir or tempfile.mkdtemp(prefix="angebot_anhang_")
                target_dir = Path(base)
                target_dir.mkdir(parents=True, exist_ok=True)
            file_path = target_dir / safe_name
            try:
                file_path.write_bytes(data)
            except OSError as exc:
                document.add_warning(f"Anhang '{name}' konnte nicht zwischengespeichert "
                                     f"werden: {exc}")
                continue

            child = self._read_attachment(str(file_path))
            child.meta["attachment_name"] = name
            child.source_kind = SourceKind.EMAIL_ATTACHMENT
            document.attachments.append(child)
            for warning in child.warnings:
                document.add_warning(f"Anhang '{name}': {warning}")

    def _read_attachment(self, path: str) -> RawDocument:
        if self.attachment_reader is None:
            document = RawDocument(source_path=path,
                                   source_kind=SourceKind.EMAIL_ATTACHMENT)
            document.add_warning("Anhaenge koennen in dieser Konfiguration nicht "
                                 "gelesen werden.")
            return document
        try:
            return self.attachment_reader(path)
        except Exception as exc:  # noqa: BLE001
            document = RawDocument(source_path=path,
                                   source_kind=SourceKind.EMAIL_ATTACHMENT)
            document.add_warning(f"Anhang nicht lesbar: {exc}")
            logger.warning("Anhang %s nicht lesbar: %s", path, exc, exc_info=True)
            return document


class TextReader(DocumentReader):
    """Reine Text- und HTML-Dateien sowie eingefuegter Text."""

    extensions = (".txt", ".text", ".html", ".htm", ".md")

    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            document.add_warning(f"Datei konnte nicht gelesen werden: {exc}")
            return document
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = data.decode(encoding)
                document.meta["encoding"] = encoding
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover -- latin-1 schlaegt praktisch nie fehl
            text = data.decode("latin-1", "replace")

        if Path(path).suffix.lower() in (".html", ".htm") or "<html" in text[:2000].lower():
            text, tables = html_to_text(text)
            for rows in tables:
                cleaned = [row for row in rows if any(c.strip() for c in row)]
                if len(cleaned) >= 2 and max(len(r) for r in cleaned) >= 2:
                    document.tables.append(TableBlock(rows=cleaned, origin="html"))
        document.text = text.replace("\r\n", "\n")
        document.pages = [document.text]
        if not document.tables:
            document.tables.extend(text_tables(document.text))
        return document

    # ------------------------------------------------------------------
    @staticmethod
    def from_text(text: str, source_name: str = "Eingefuegter Text") -> RawDocument:
        """Dokument direkt aus eingefuegtem Text bauen (Zwischenablage)."""
        document = RawDocument(source_path="", source_kind=SourceKind.TEXT)
        document.meta["source_name"] = source_name
        content = text or ""
        if "<table" in content.lower() or "<html" in content.lower():
            plain, tables = html_to_text(content)
            for rows in tables:
                cleaned = [row for row in rows if any(c.strip() for c in row)]
                if len(cleaned) >= 2 and max(len(r) for r in cleaned) >= 2:
                    document.tables.append(TableBlock(rows=cleaned, origin="html"))
            content = plain
        document.text = content.replace("\r\n", "\n")
        document.pages = [document.text]
        if not document.tables:
            document.tables.extend(text_tables(document.text))
        return document


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _header(message, name: str) -> str:
    try:
        value = message.get(name, "")
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"\s+", " ", str(value)).strip() if value else ""


def _split_address(value: str) -> tuple[str, str]:
    """``"Max Muster" <max@lieferant.de>`` -> ("Max Muster", "max@lieferant.de")."""
    if not value:
        return "", ""
    try:
        pairs = getaddresses([value])
    except Exception:  # noqa: BLE001
        pairs = []
    if pairs:
        name, address = pairs[0]
        return name.strip().strip('"'), address.strip()
    return value.strip(), ""


def _part_text(part) -> str:
    """Textteil einer Mail dekodieren, ohne an der Kodierung zu scheitern."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:  # noqa: BLE001
        pass
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001
        return ""
    charset = part.get_content_charset() or "utf-8"
    for encoding in (charset, "utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("latin-1", "replace")


def _compose_email_text(context: EmailContext, body: str) -> str:
    """Kopfzeilen mit in den Text nehmen -- dort stehen oft Angebotsnummern."""
    header_lines = []
    if context.subject:
        header_lines.append(f"Betreff: {context.subject}")
    if context.from_name or context.from_address:
        header_lines.append(f"Von: {context.from_name} <{context.from_address}>".strip())
    if context.to:
        header_lines.append(f"An: {context.to}")
    if context.sent:
        header_lines.append(f"Gesendet: {context.sent.strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(header_lines + ["", body]).strip()


def _looks_like_message(data: bytes) -> bool:
    """Sieht das nach einem Briefkopf aus (``Header: Wert``)?"""
    kopf = data[:200].lstrip()
    if not kopf:
        return False
    erste = kopf.split(bytes([10]), 1)[0]
    return b":" in erste and not erste.startswith(b"--")


def _unwrap_base64(data: bytes) -> bytes:
    """Doppelt verpackte Nachrichten auspacken.

    Manche Programme betten eine weitergeleitete Nachricht base64-kodiert
    ein.  Wird das nicht ausgepackt, reicht man Buchstabensalat weiter und
    die Erkennung findet erwartungsgemaess nichts.
    """
    import base64
    import binascii

    if _looks_like_message(data):
        return data
    try:
        entpackt = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return data
    return entpackt if _looks_like_message(entpackt) else data


def _safe_filename(name: str) -> str:
    """Dateinamen fuer das temporaere Verzeichnis entschaerfen."""
    cleaned = re.sub(r"[^\w.\- ]+", "_", Path(name or "anhang.bin").name, flags=re.UNICODE)
    cleaned = cleaned.strip() or "anhang.bin"
    if len(cleaned) > _MAX_TEMP_NAME:
        stem, suffix = Path(cleaned).stem, Path(cleaned).suffix
        cleaned = stem[: _MAX_TEMP_NAME - len(suffix)] + suffix
    return cleaned
