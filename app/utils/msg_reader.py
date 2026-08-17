"""Outlook-``.msg``-Leser -- ausschliesslich mit der Standardbibliothek.

Eine ``.msg``-Datei ist ein "Compound File Binary" (CFB, frueher OLE2):
ein kleines Dateisystem in der Datei mit Sektoren, einer FAT, einer MiniFAT
fuer kleine Streams und einem Verzeichnisbaum.  Die eigentlichen
MAPI-Eigenschaften liegen als Streams ``__substg1.0_<TAG><TYP>`` in diesem
Verzeichnis, Anhaenge in Unterordnern ``__attach_version1.0_#00000000``.

Grundsatz des Projekts: **niemals raten und niemals abstuerzen**.  Alle
Parserfehler werden gesammelt (``MsgFile.errors``) und als Teilergebnis
zurueckgegeben -- eine kaputte Mail darf die Anwendung nicht anhalten.

Referenz: [MS-CFB] und [MS-OXMSG] (oeffentliche Microsoft-Spezifikationen).
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "MsgAttachment",
    "MsgFile",
    "CompoundFile",
    "read_msg",
    "is_msg_file",
]


# ---------------------------------------------------------------------------
# Konstanten aus [MS-CFB]
# ---------------------------------------------------------------------------

CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAXREGSECT = 0xFFFFFFFA
DIFSECT = 0xFFFFFFFC
FATSECT = 0xFFFFFFFD
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF

#: Objekttypen eines Verzeichniseintrags
OBJ_EMPTY = 0
OBJ_STORAGE = 1
OBJ_STREAM = 2
OBJ_ROOT = 5

#: Sicherheitsgrenzen gegen Endlosschleifen bei defekten Dateien
_MAX_SECTORS = 4_000_000
_MAX_DIR_ENTRIES = 100_000

#: MAPI-Eigenschaftstypen, die hier interessieren
PT_UNICODE = 0x001F
PT_STRING8 = 0x001E
PT_BINARY = 0x0102
PT_SYSTIME = 0x0040

#: Eigenschafts-IDs (siehe [MS-OXPROPS])
PID_SUBJECT = 0x0037
PID_BODY = 0x1000
PID_HTML = 0x1013
PID_RTF_COMPRESSED = 0x1009
PID_SENDER_NAME = 0x0C1A
PID_SENDER_EMAIL = 0x0C1F
PID_SENDER_SMTP = 0x5D01
PID_SENT_REPRESENTING_SMTP = 0x5D02
PID_SMTP_ADDRESS = 0x39FE
PID_DISPLAY_TO = 0x0E04
PID_DISPLAY_CC = 0x0E03
PID_TRANSPORT_HEADERS = 0x007D
PID_CLIENT_SUBMIT_TIME = 0x0039
PID_DELIVERY_TIME = 0x0E06
PID_LAST_MODIFICATION = 0x3008
PID_ATTACH_LONG_FILENAME = 0x3707
PID_ATTACH_FILENAME = 0x3704
PID_ATTACH_EXTENSION = 0x3703
PID_ATTACH_DATA = 0x3701
PID_ATTACH_MIME_TAG = 0x370E
PID_INTERNET_CPID = 0x3FDE
PID_MESSAGE_CODEPAGE = 0x3FFD

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

#: Codepage-Nummer -> Python-Codec (nur die im Einkauf realistischen)
_CODEPAGES = {
    1252: "cp1252", 1250: "cp1250", 1251: "cp1251", 1253: "cp1253",
    1254: "cp1254", 1257: "cp1257", 28591: "latin-1", 28599: "iso8859-9",
    20127: "ascii", 65001: "utf-8", 850: "cp850", 437: "cp437",
}


# ---------------------------------------------------------------------------
# Verzeichniseintrag
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DirEntry:
    """Ein Eintrag im CFB-Verzeichnisbaum (Ordner oder Stream)."""

    index: int
    name: str
    kind: int
    left: int
    right: int
    child: int
    start_sector: int
    size: int

    @property
    def is_stream(self) -> bool:
        return self.kind == OBJ_STREAM

    @property
    def is_storage(self) -> bool:
        return self.kind in (OBJ_STORAGE, OBJ_ROOT)


# ---------------------------------------------------------------------------
# Compound-File-Parser
# ---------------------------------------------------------------------------

class CompoundFile:
    """Minimaler, defensiver Leser fuer OLE/CFB-Dateien.

    Alle Fehler landen in :attr:`errors`; nach aussen fliegt keine Exception.
    ``self.ok`` sagt, ob ueberhaupt ein Verzeichnis gelesen werden konnte.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.errors: list[str] = []
        self.ok = False
        self.sector_size = 512
        self.mini_sector_size = 64
        self.mini_cutoff = 4096
        self.fat: list[int] = []
        self.minifat: list[int] = []
        self.entries: list[DirEntry] = []
        self._mini_stream: bytes = b""
        self._children_cache: dict[int, list[DirEntry]] = {}
        try:
            self._parse()
            self.ok = bool(self.entries)
        except Exception as exc:  # noqa: BLE001 -- defekte Datei darf nicht killen
            self.errors.append(f"Compound-File konnte nicht gelesen werden: {exc}")
            logger.warning("CFB-Parserfehler: %s", exc, exc_info=True)

    # -- Header / FAT ----------------------------------------------------
    def _parse(self) -> None:
        data = self.data
        if len(data) < 512:
            raise ValueError("Datei ist zu klein fuer ein Compound-File")
        if data[:8] != CFB_SIGNATURE:
            raise ValueError("Kein Compound-File (Signatur fehlt)")

        sector_shift = struct.unpack_from("<H", data, 30)[0]
        mini_shift = struct.unpack_from("<H", data, 32)[0]
        if sector_shift not in (9, 12):
            self.errors.append(f"Ungewoehnliche Sektorgroesse 2^{sector_shift}, nehme 512 Byte an")
            sector_shift = 9
        if mini_shift != 6:
            mini_shift = 6
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift

        num_fat_sectors = struct.unpack_from("<I", data, 44)[0]
        first_dir_sector = struct.unpack_from("<I", data, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 56)[0] or 4096
        first_minifat = struct.unpack_from("<I", data, 60)[0]
        num_minifat = struct.unpack_from("<I", data, 64)[0]
        first_difat = struct.unpack_from("<I", data, 68)[0]
        num_difat = struct.unpack_from("<I", data, 72)[0]

        fat_sectors = self._read_difat(first_difat, num_difat, num_fat_sectors)
        self.fat = self._read_fat(fat_sectors)
        self.minifat = self._read_chain_as_uint32(first_minifat, limit=num_minifat or None)

        self.entries = self._read_directory(first_dir_sector)
        if self.entries:
            root = self.entries[0]
            self._mini_stream = self._read_sector_chain(root.start_sector, root.size)

    def _sector_offset(self, sector: int) -> int:
        return (sector + 1) * self.sector_size

    def _read_sector(self, sector: int) -> bytes:
        start = self._sector_offset(sector)
        chunk = self.data[start:start + self.sector_size]
        if len(chunk) < self.sector_size:
            # Abgeschnittene Datei -- auffuellen statt abbrechen
            chunk = chunk + b"\x00" * (self.sector_size - len(chunk))
        return chunk

    def _read_difat(self, first_difat: int, num_difat: int, num_fat_sectors: int) -> list[int]:
        """Liste der Sektoren, die die FAT enthalten."""
        sectors: list[int] = []
        for i in range(109):
            value = struct.unpack_from("<I", self.data, 76 + i * 4)[0]
            if value > MAXREGSECT:
                break
            sectors.append(value)

        sector = first_difat
        guard = 0
        per_sector = self.sector_size // 4 - 1
        while sector <= MAXREGSECT and guard < _MAX_SECTORS:
            guard += 1
            block = self._read_sector(sector)
            for i in range(per_sector):
                value = struct.unpack_from("<I", block, i * 4)[0]
                if value <= MAXREGSECT:
                    sectors.append(value)
            sector = struct.unpack_from("<I", block, per_sector * 4)[0]
        if num_fat_sectors and len(sectors) > num_fat_sectors:
            sectors = sectors[:num_fat_sectors]
        if num_difat and guard > num_difat + 1:
            self.errors.append("DIFAT-Kette laenger als im Kopf angegeben")
        return sectors

    def _read_fat(self, fat_sectors: list[int]) -> list[int]:
        fat: list[int] = []
        per_sector = self.sector_size // 4
        for sector in fat_sectors:
            block = self._read_sector(sector)
            fat.extend(struct.unpack_from(f"<{per_sector}I", block, 0))
        return fat

    def _next_sector(self, sector: int) -> int:
        if 0 <= sector < len(self.fat):
            return self.fat[sector]
        return ENDOFCHAIN

    def _read_sector_chain(self, start: int, size: int | None = None) -> bytes:
        """Folgt einer FAT-Kette und liefert die Rohdaten."""
        out = bytearray()
        sector = start
        seen: set[int] = set()
        while sector <= MAXREGSECT and len(seen) < _MAX_SECTORS:
            if sector in seen:
                self.errors.append("Zyklus in der Sektorkette erkannt -- Rest wird ignoriert")
                break
            seen.add(sector)
            out.extend(self._read_sector(sector))
            if size is not None and len(out) >= size:
                break
            sector = self._next_sector(sector)
        if size is not None:
            return bytes(out[:size])
        return bytes(out)

    def _read_chain_as_uint32(self, start: int, limit: int | None = None) -> list[int]:
        raw = self._read_sector_chain(start)
        if limit is not None:
            raw = raw[: limit * self.sector_size]
        count = len(raw) // 4
        if not count:
            return []
        return list(struct.unpack_from(f"<{count}I", raw, 0))

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        sector = start
        seen: set[int] = set()
        while sector <= MAXREGSECT and len(out) < size and len(seen) < _MAX_SECTORS:
            if sector in seen:
                self.errors.append("Zyklus in der MiniFAT-Kette erkannt")
                break
            seen.add(sector)
            offset = sector * self.mini_sector_size
            out.extend(self._mini_stream[offset:offset + self.mini_sector_size])
            sector = self.minifat[sector] if 0 <= sector < len(self.minifat) else ENDOFCHAIN
        return bytes(out[:size])

    # -- Verzeichnis -----------------------------------------------------
    def _read_directory(self, first_sector: int) -> list[DirEntry]:
        raw = self._read_sector_chain(first_sector)
        entries: list[DirEntry] = []
        count = min(len(raw) // 128, _MAX_DIR_ENTRIES)
        for index in range(count):
            base = index * 128
            try:
                name_len = struct.unpack_from("<H", raw, base + 64)[0]
                name_len = max(0, min(name_len, 64))
                name = raw[base:base + name_len].decode("utf-16-le", "replace").rstrip("\x00")
                kind = raw[base + 66]
                left, right, child = struct.unpack_from("<III", raw, base + 68)
                start_sector = struct.unpack_from("<I", raw, base + 116)[0]
                size = struct.unpack_from("<Q", raw, base + 120)[0]
            except struct.error:
                self.errors.append(f"Verzeichniseintrag {index} unvollstaendig")
                break
            if kind == OBJ_EMPTY:
                entries.append(DirEntry(index, "", kind, left, right, child, start_sector, 0))
                continue
            if size > len(self.data) * 8:  # offensichtlich unsinnige Groesse
                size = min(size, len(self.data))
            entries.append(DirEntry(index, name, kind, left, right, child, start_sector, int(size)))
        return entries

    def children(self, entry: DirEntry) -> list[DirEntry]:
        """Direkte Kinder eines Ordners (Rot-Schwarz-Baum wird abgelaufen)."""
        if entry.index in self._children_cache:
            return self._children_cache[entry.index]
        result: list[DirEntry] = []
        stack = [entry.child]
        seen: set[int] = set()
        while stack:
            index = stack.pop()
            if index > MAXREGSECT or index in seen or index >= len(self.entries):
                continue
            seen.add(index)
            node = self.entries[index]
            if node.kind != OBJ_EMPTY:
                result.append(node)
            # Nur Geschwister verfolgen -- Kinder werden erst auf Anfrage gelesen
            stack.append(node.left)
            stack.append(node.right)
        self._children_cache[entry.index] = result
        return result

    @property
    def root(self) -> DirEntry | None:
        return self.entries[0] if self.entries else None

    def read_stream(self, entry: DirEntry) -> bytes:
        """Inhalt eines Streams (automatisch Mini- oder Normalsektoren)."""
        if not entry.is_stream or entry.size <= 0:
            return b""
        try:
            if entry.size < self.mini_cutoff:
                return self._read_mini_chain(entry.start_sector, entry.size)
            return self._read_sector_chain(entry.start_sector, entry.size)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"Stream '{entry.name}' nicht lesbar: {exc}")
            return b""


# ---------------------------------------------------------------------------
# MAPI-Ebene
# ---------------------------------------------------------------------------

_SUBSTG = re.compile(r"^__substg1\.0_([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})$")
_ATTACH_DIR = re.compile(r"^__attach_version1\.0_", re.I)


@dataclass(slots=True)
class MsgAttachment:
    """Ein Dateianhang der Nachricht."""

    name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def extension(self) -> str:
        return Path(self.name).suffix.lower()


@dataclass
class MsgFile:
    """Ergebnis des .msg-Lesens -- immer ein Teilergebnis, nie eine Exception."""

    path: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    to: str = ""
    cc: str = ""
    body: str = ""
    html: str = ""
    sent: datetime | None = None
    headers: str = ""
    attachments: list[MsgAttachment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Wurde ueberhaupt etwas Verwertbares gefunden?"""
        return bool(self.subject or self.body or self.html or self.attachments)

    @property
    def sender_domain(self) -> str:
        if "@" not in self.sender_email:
            return ""
        return self.sender_email.rsplit("@", 1)[1].lower().strip(">").strip()

    def header_value(self, name: str) -> str:
        """Einen Wert aus den Transport-Headern lesen (z. B. ``Date``)."""
        if not self.headers:
            return ""
        pattern = re.compile(rf"^{re.escape(name)}\s*:\s*(.*(?:\n[ \t].*)*)", re.I | re.M)
        match = pattern.search(self.headers)
        return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


class _MsgProperties:
    """Zugriff auf die MAPI-Eigenschaften eines Storage (Nachricht/Anhang)."""

    def __init__(self, cfb: CompoundFile, storage: DirEntry, header_size: int) -> None:
        self.cfb = cfb
        self.storage = storage
        self.streams: dict[tuple[int, int], DirEntry] = {}
        self.storages: list[DirEntry] = []
        self.fixed: dict[int, bytes] = {}
        self.codepage: str = ""
        for child in cfb.children(storage):
            if child.is_storage:
                self.storages.append(child)
                continue
            match = _SUBSTG.match(child.name)
            if match:
                prop_id = int(match.group(1), 16)
                prop_type = int(match.group(2), 16)
                self.streams[(prop_id, prop_type)] = child
            elif child.name.startswith("__properties_version1.0"):
                self._read_properties(child, header_size)
        self._resolve_codepage()

    # -- Eigenschaften mit fester Laenge (Datum, Zahlen) ------------------
    def _read_properties(self, entry: DirEntry, header_size: int) -> None:
        raw = self.cfb.read_stream(entry)
        offset = header_size
        while offset + 16 <= len(raw):
            tag = struct.unpack_from("<I", raw, offset)[0]
            value = raw[offset + 8:offset + 16]
            self.fixed[tag] = value
            offset += 16

    def _resolve_codepage(self) -> None:
        for pid in (PID_MESSAGE_CODEPAGE, PID_INTERNET_CPID):
            for prop_type in (0x0003,):
                raw = self.fixed.get((pid << 16) | prop_type)
                if raw:
                    number = struct.unpack_from("<I", raw, 0)[0]
                    self.codepage = _CODEPAGES.get(number, "")
                    if self.codepage:
                        return

    # -- Stringzugriff ---------------------------------------------------
    def string(self, prop_id: int) -> str:
        entry = self.streams.get((prop_id, PT_UNICODE))
        if entry is not None:
            return self.cfb.read_stream(entry).decode("utf-16-le", "replace").rstrip("\x00")
        entry = self.streams.get((prop_id, PT_STRING8))
        if entry is not None:
            raw = self.cfb.read_stream(entry)
            return _decode_bytes(raw, self.codepage or "cp1252").rstrip("\x00")
        return ""

    def binary(self, prop_id: int) -> bytes:
        entry = self.streams.get((prop_id, PT_BINARY))
        return self.cfb.read_stream(entry) if entry is not None else b""

    def any_stream(self, prop_id: int) -> bytes:
        for (pid, _ptype), entry in self.streams.items():
            if pid == prop_id:
                return self.cfb.read_stream(entry)
        return b""

    def filetime(self, prop_id: int) -> datetime | None:
        raw = self.fixed.get((prop_id << 16) | PT_SYSTIME)
        if not raw or len(raw) < 8:
            return None
        ticks = struct.unpack_from("<Q", raw, 0)[0]
        if not ticks:
            return None
        try:
            return (_FILETIME_EPOCH + timedelta(microseconds=ticks / 10)).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None


def _decode_bytes(raw: bytes, preferred: str = "") -> str:
    """Bytes moeglichst verlustfrei in Text wandeln (nie Exception)."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16", "replace")
        except (UnicodeDecodeError, LookupError):
            pass
    candidates = [c for c in (preferred, "utf-8", "cp1252", "latin-1") if c]
    for codec in candidates:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def _decode_html(raw: bytes) -> str:
    """HTML-Body dekodieren; das Charset steht ggf. im Dokument selbst."""
    if not raw:
        return ""
    match = re.search(rb"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", raw[:4096], re.I)
    preferred = match.group(1).decode("ascii", "ignore").lower() if match else ""
    if preferred in ("utf8", "utf_8"):
        preferred = "utf-8"
    return _decode_bytes(raw, preferred)


# ---------------------------------------------------------------------------
# Oeffentliche Schnittstelle
# ---------------------------------------------------------------------------

def is_msg_file(path: str | Path) -> bool:
    """Schnelltest ueber die Signatur (Endung allein ist unzuverlaessig)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == CFB_SIGNATURE
    except OSError:
        return False


def read_msg(path: str | Path) -> MsgFile:
    """Liest eine Outlook-``.msg``-Datei.

    Liefert *immer* ein :class:`MsgFile`; Probleme stehen in ``.errors``.
    """
    result = MsgFile(path=str(path))
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        result.errors.append(f"Datei konnte nicht gelesen werden: {exc}")
        logger.warning("MSG nicht lesbar (%s): %s", path, exc)
        return result

    return read_msg_bytes(data, source=str(path))


def read_msg_bytes(data: bytes, source: str = "") -> MsgFile:
    """Wie :func:`read_msg`, aber auf einem bereits geladenen Bytepuffer.

    Wird auch fuer eingebettete Nachrichten in Anhaengen verwendet.
    """
    result = MsgFile(path=source)
    cfb = CompoundFile(data)
    result.errors.extend(cfb.errors)
    if not cfb.ok or cfb.root is None:
        if not result.errors:
            result.errors.append("Keine Verzeichnisstruktur gefunden")
        return result

    try:
        props = _MsgProperties(cfb, cfb.root, header_size=32)
        result.subject = props.string(PID_SUBJECT).strip()
        result.sender_name = props.string(PID_SENDER_NAME).strip()
        result.sender_email = _pick_sender_address(props)
        result.to = props.string(PID_DISPLAY_TO).strip()
        result.cc = props.string(PID_DISPLAY_CC).strip()
        result.body = props.string(PID_BODY)
        result.headers = props.string(PID_TRANSPORT_HEADERS)

        html_raw = props.binary(PID_HTML)
        if html_raw:
            result.html = _decode_html(html_raw)
        else:
            result.html = props.string(PID_HTML)

        result.sent = (props.filetime(PID_CLIENT_SUBMIT_TIME)
                       or props.filetime(PID_DELIVERY_TIME)
                       or props.filetime(PID_LAST_MODIFICATION)
                       or _sent_from_headers(result.headers))

        result.attachments = _read_attachments(cfb, props, result.errors)
    except Exception as exc:  # noqa: BLE001 -- Teilergebnis ist besser als nichts
        result.errors.append(f"Nachricht nur teilweise lesbar: {exc}")
        logger.warning("MSG-Auswertung unvollstaendig (%s): %s", source, exc, exc_info=True)

    logger.debug("MSG gelesen: %s (Betreff=%r, Anhaenge=%d, Fehler=%d)",
                 source, result.subject, len(result.attachments), len(result.errors))
    return result


def _pick_sender_address(props: _MsgProperties) -> str:
    """SMTP-Adresse des Absenders -- in dieser Reihenfolge am zuverlaessigsten."""
    for prop_id in (PID_SENDER_SMTP, PID_SENT_REPRESENTING_SMTP,
                    PID_SMTP_ADDRESS, PID_SENDER_EMAIL):
        value = props.string(prop_id).strip()
        if "@" in value:
            return value
    return ""


def _sent_from_headers(headers: str) -> datetime | None:
    """Sendedatum aus dem ``Date:``-Header (Fallback)."""
    if not headers:
        return None
    match = re.search(r"^Date\s*:\s*(.+)$", headers, re.I | re.M)
    if not match:
        return None
    from email.utils import parsedate_to_datetime

    try:
        value = parsedate_to_datetime(match.group(1).strip())
    except (TypeError, ValueError):
        return None
    return value.replace(tzinfo=None) if value else None


def _read_attachments(cfb: CompoundFile, props: _MsgProperties,
                      errors: list[str]) -> list[MsgAttachment]:
    """Alle ``__attach_*``-Ordner auswerten."""
    attachments: list[MsgAttachment] = []
    for storage in sorted(props.storages, key=lambda e: e.name):
        if not _ATTACH_DIR.match(storage.name):
            continue
        try:
            sub = _MsgProperties(cfb, storage, header_size=8)
            name = (sub.string(PID_ATTACH_LONG_FILENAME).strip()
                    or sub.string(PID_ATTACH_FILENAME).strip())
            data = sub.binary(PID_ATTACH_DATA)
            if not data:
                # Eingebettete Nachricht liegt als Unterordner vor
                embedded = [s for s in sub.storages if s.name.startswith("__substg1.0_3701")]
                if embedded:
                    errors.append(
                        f"Anhang '{name or storage.name}' ist eine eingebettete Nachricht "
                        "und wird uebersprungen"
                    )
                    continue
            if not name:
                extension = sub.string(PID_ATTACH_EXTENSION).strip()
                name = f"{storage.name}{extension}"
            if data:
                attachments.append(MsgAttachment(name=name, data=data))
            else:
                errors.append(f"Anhang '{name}' enthaelt keine Daten")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Anhang '{storage.name}' nicht lesbar: {exc}")
            logger.warning("MSG-Anhang nicht lesbar: %s", exc, exc_info=True)
    return attachments
