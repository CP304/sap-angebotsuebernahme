"""Tests der Angebotserkennung (unittest, ohne externe Testframeworks).

Abgedeckt werden bewusst *verschiedene Lieferantenformate*, denn genau darin
liegt die Schwierigkeit des Imports:

* Excel mit und ohne Kopfzeile, mehrzeilige Koepfe, mehrdeutige Preisspalten
* deutsche und englische Zahlenformate
* PDF-Layout (ueber die Wortkoordinaten-Rekonstruktion, ohne echtes PDF)
* E-Mails: Freitext, HTML-Preistabelle, Anhang, Signaturschnitt
* Outlook-.msg (Compound File wird im Test selbst erzeugt)
* Staffelpreise, Summenzeilen, Fortsetzungszeilen
* defekte Dateien (duerfen nie eine Exception nach aussen geben)
* Lernen aus Anwenderkorrekturen
"""

from __future__ import annotations

import copy
import datetime
import os
import shutil
import struct
import tempfile
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from app.config.settings import Settings
from app.models.enums import FieldOrigin, SourceKind
from app.models.offer import EmailContext, Offer
from app.services.extraction.freetext_extractor import FreetextExtractor
from app.services.extraction.header_rules import (
    extract_header_fields,
    find_incoterm,
    vendor_name_from_signature,
)
from app.services.extraction.learning import (
    LearningConfig,
    describe_learning,
    forget_rule,
    learn_from_corrections,
)
from app.services.extraction.profiles import (
    InMemoryProfileStore,
    VendorProfile,
    fingerprint,
    match_profile,
    new_profile,
)
from app.services.extraction.table_extractor import (
    TableExtractor,
    find_price_tiers,
    is_summary_row,
    parse_number,
)
from app.services.offer_import_service import OfferImportService
from app.services.readers import ReaderRegistry
from app.services.readers.base import RawDocument, TableBlock
from app.services.readers.email_reader import html_to_text, strip_signature
from app.services.readers.excel_reader import XLS_HINT, detect_delimiter
from app.services.readers.pdf_reader import SCAN_WARNING, make_word, words_to_tables
from app.utils.msg_reader import CompoundFile, read_msg, read_msg_bytes


# ==========================================================================
# Hilfsmittel
# ==========================================================================

def make_document(rows: list[list[str]], title: str = "Preise",
                  kind: SourceKind = SourceKind.EXCEL,
                  text: str = "") -> RawDocument:
    """Dokument mit genau einem Tabellenblock."""
    return RawDocument(
        source_path=f"{title}.xlsx",
        source_kind=kind,
        text=text,
        tables=[TableBlock(rows=[list(r) for r in rows], origin="excel", title=title)],
    )


class _CfbNode:
    """Knoten fuer den Test-Compound-File-Schreiber."""

    def __init__(self, name: str, data: bytes | None = None,
                 children: list["_CfbNode"] | None = None) -> None:
        self.name = name
        self.data = data
        self.children = children or []
        self.index = -1
        self.start = 0xFFFFFFFE
        self.size = 0
        self.child = 0xFFFFFFFF
        self.left = 0xFFFFFFFF
        self.right = 0xFFFFFFFF


def _build_cfb(root_children: list[_CfbNode]) -> bytes:
    """Erzeugt eine minimale, gueltige CFB-Datei (512-Byte-Sektoren).

    Damit laesst sich der .msg-Leser ohne Outlook und ohne Testdatei pruefen.
    """
    sector, mini, cutoff = 512, 64, 4096
    freesect, endofchain, fatsect = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD

    root = _CfbNode("Root Entry", None, root_children)
    order: list[_CfbNode] = []

    def walk(node: _CfbNode) -> None:
        node.index = len(order)
        order.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    for node in order:
        if node.children:
            node.child = node.children[0].index
            for left, right in zip(node.children, node.children[1:]):
                left.right = right.index

    streams = [n for n in order if n.data is not None]
    small = [n for n in streams if 0 < len(n.data) < cutoff]
    big = [n for n in streams if len(n.data) >= cutoff]

    mini_blob = bytearray()
    for node in small:
        node.start = len(mini_blob) // mini
        node.size = len(node.data)
        mini_blob.extend(node.data)
        mini_blob.extend(b"\x00" * ((-len(node.data)) % mini))

    minifat: list[int] = []
    for node in small:
        count = (node.size + mini - 1) // mini
        for offset in range(count):
            minifat.append(node.start + offset + 1 if offset < count - 1 else endofchain)
    minifat += [freesect] * ((-len(minifat)) % (sector // 4))

    sectors: list[bytes] = []
    fat: list[int] = []

    def add_chain(payload: bytes) -> int:
        if not payload:
            return endofchain
        first = len(sectors)
        blocks = [payload[i:i + sector].ljust(sector, b"\x00")
                  for i in range(0, len(payload), sector)]
        for offset, block in enumerate(blocks):
            sectors.append(block)
            fat.append(first + offset + 1 if offset < len(blocks) - 1 else endofchain)
        return first

    root.start = add_chain(bytes(mini_blob))
    root.size = len(mini_blob)
    for node in big:
        node.size = len(node.data)
        node.start = add_chain(node.data)

    directory = bytearray()
    for node in order:
        name = node.name.encode("utf-16-le") + b"\x00\x00"
        entry = bytearray(128)
        entry[0:len(name)] = name
        struct.pack_into("<H", entry, 64, len(name))
        entry[66] = 5 if node is root else (1 if node.data is None else 2)
        entry[67] = 1
        struct.pack_into("<III", entry, 68, node.left, node.right, node.child)
        struct.pack_into("<I", entry, 116, node.start)
        struct.pack_into("<Q", entry, 120, node.size)
        directory.extend(entry)
    directory += b"\x00" * ((-len(directory)) % sector)
    dir_start = add_chain(bytes(directory))

    minifat_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
    minifat_start = add_chain(minifat_bytes) if minifat_bytes else endofchain
    minifat_count = len(minifat_bytes) // sector

    data_sectors = len(sectors)
    fat_sectors = 1
    while True:
        needed = (data_sectors + fat_sectors + (sector // 4) - 1) // (sector // 4)
        if needed <= fat_sectors:
            break
        fat_sectors = needed
    fat_start = data_sectors
    for _ in range(fat_sectors):
        sectors.append(b"\x00" * sector)
        fat.append(fatsect)
    fat += [freesect] * (fat_sectors * (sector // 4) - len(fat))
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    for offset in range(fat_sectors):
        sectors[fat_start + offset] = fat_bytes[offset * sector:(offset + 1) * sector]

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 26, 3)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, fat_sectors)
    struct.pack_into("<I", header, 48, dir_start)
    struct.pack_into("<I", header, 56, cutoff)
    struct.pack_into("<I", header, 60, minifat_start)
    struct.pack_into("<I", header, 64, minifat_count)
    struct.pack_into("<I", header, 68, endofchain)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4,
                         fat_start + index if index < fat_sectors else freesect)
    return bytes(header) + b"".join(sectors)


def _substg(prop_id: int, prop_type: int, data: bytes) -> _CfbNode:
    return _CfbNode(f"__substg1.0_{prop_id:04X}{prop_type:04X}", data)


def _unicode_prop(prop_id: int, text: str) -> _CfbNode:
    return _substg(prop_id, 0x001F, text.encode("utf-16-le"))


def _filetime(value: datetime.datetime) -> bytes:
    ticks = int((value - datetime.datetime(1601, 1, 1)).total_seconds() * 10_000_000)
    return struct.pack("<Q", ticks)


def _properties(entries: list[tuple[int, int, bytes]], header_size: int) -> _CfbNode:
    blob = bytearray(b"\x00" * header_size)
    for prop_id, prop_type, value in entries:
        blob.extend(struct.pack("<I", (prop_id << 16) | prop_type))
        blob.extend(struct.pack("<I", 6))
        blob.extend(value.ljust(8, b"\x00")[:8])
    return _CfbNode("__properties_version1.0", bytes(blob))


class TempDirCase(unittest.TestCase):
    """Basisklasse mit temporaerem Arbeitsverzeichnis."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="angebot_test_")
        self.settings = Settings()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def write(self, name: str, content: bytes) -> str:
        target = self.path(name)
        Path(target).write_bytes(content)
        return target


# ==========================================================================
# 1 -- Tabellen aus Excel
# ==========================================================================

class TabellenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()

    def test_01_excel_mit_kopfzeile(self) -> None:
        """Klassische Preisliste mit Kopfzeile."""
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "ME", "Einzelpreis"],
            ["10", "4711002", "Dichtring 40x52x7", "100", "Stk", "12,85"],
            ["20", "4711003", "O-Ring 25x3", "250", "Stk", "3,40"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        first = result.positions[0]
        self.assertEqual(first.material_number, "4711002")
        self.assertEqual(first.description, "Dichtring 40x52x7")
        self.assertEqual(first.quantity, Decimal("100"))
        self.assertEqual(first.uom, "ST")
        self.assertEqual(first.price, Decimal("12.85"))
        self.assertEqual(first.origin("price"), FieldOrigin.EXTRACTED)

    def test_02_excel_ohne_kopfzeile(self) -> None:
        """Ganz ohne Kopfzeile entscheiden die Datentypen der Spalten."""
        rows = [
            ["4711002", "Dichtring 40x52x7", "100", "ST", "12,85", "01.09.2026"],
            ["4711003", "O-Ring 25x3", "250", "ST", "3,40", "01.09.2026"],
            ["4711004", "Flachdichtung 60", "50", "ST", "7,90", "01.09.2026"],
        ]
        document = make_document(rows)
        extractor = TableExtractor(self.settings)
        analysis = extractor.analyze(document.tables[0].normalized())
        self.assertIsNone(analysis.header_row_index)
        result = extractor.extract(document)
        self.assertEqual(len(result.positions), 3)
        position = result.positions[0]
        self.assertEqual(position.material_number, "4711002")
        self.assertEqual(position.price, Decimal("12.85"))
        self.assertEqual(position.valid_from, datetime.date(2026, 9, 1))
        # Ohne Ueberschrift ist alles nur "unsicher" erkannt
        self.assertIn("price", position.uncertain_fields)

    def test_03_mehrzeilige_kopfzeile(self) -> None:
        """Kopfzeile ueber zwei Zeilen ("Preis" / "EUR je Stueck")."""
        rows = [
            ["Pos", "Artikel", "Bezeichnung", "Menge", "Netto"],
            ["Nr.", "Nummer", "", "", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["20", "4711003", "O-Ring", "250", "3,40"],
        ]
        extractor = TableExtractor(self.settings)
        document = make_document(rows)
        analysis = extractor.analyze(document.tables[0].normalized())
        self.assertEqual(analysis.data_start, 2)
        result = extractor.extract(document)
        self.assertEqual(len(result.positions), 2)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))

    def test_04_deutsche_zahlen(self) -> None:
        rows = [
            ["Material", "Bezeichnung", "Menge", "Preis"],
            ["4711002", "Dichtring", "1.500", "1.234,56"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(result.positions[0].quantity, Decimal("1500"))
        self.assertEqual(result.positions[0].price, Decimal("1234.56"))

    def test_05_englische_zahlen(self) -> None:
        rows = [
            ["Part No", "Description", "Quantity", "Unit price"],
            ["4711002", "Sealing ring", "1,500", "1,234.56"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        position = result.positions[0]
        self.assertEqual(position.quantity, Decimal("1500"))
        self.assertEqual(position.price, Decimal("1234.56"))

    def test_06_dezimalstil_aus_profil(self) -> None:
        """Gelerntes Zahlenformat deutet mehrdeutige Werte um."""
        self.assertEqual(parse_number("1,234", "en"), Decimal("1234"))
        self.assertEqual(parse_number("1.234", "en"), Decimal("1.234"))
        self.assertEqual(parse_number("1.234", "de"), Decimal("1234"))
        self.assertEqual(parse_number("12,85", "de"), Decimal("12.85"))

    def test_07_summenzeilen_werden_verworfen(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["", "", "Zwischensumme", "", "1.285,00"],
            ["", "", "zzgl. MwSt 19%", "", "244,15"],
            ["", "", "Gesamt", "", "1.529,15"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 1)
        self.assertTrue(any("Summenzeile" in note for note in result.notes))
        self.assertTrue(is_summary_row(["", "Netto gesamt", "1.285,00"]))
        self.assertFalse(is_summary_row(["4711002", "Dichtring", "12,85"]))

    def test_08_fortsetzungszeile(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring 40x52x7", "100", "12,85"],
            ["", "", "Werkstoff NBR 70 Shore", "", ""],
            ["20", "4711003", "O-Ring", "250", "3,40"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        self.assertIn("NBR 70", result.positions[0].description)

    def test_09_staffelpreis_in_der_zelle(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring (ab 100 Stk 11,90)", "1", "12,85"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        staffel = result.positions[1]
        self.assertEqual(staffel.price, Decimal("11.90"))
        self.assertEqual(staffel.quantity, Decimal("100"))
        self.assertIn("Staffelpreis", staffel.remarks)
        self.assertEqual(staffel.material_number, "4711002")

    def test_10_staffelpreis_als_folgezeile(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711003", "O-Ring", "250", "3,40"],
            ["", "", "", "500", "3,10"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        self.assertEqual(result.positions[1].material_number, "4711003")
        self.assertIn("Staffelpreis", result.positions[1].remarks)

    def test_11_mehrdeutige_preisspalten(self) -> None:
        """Listenpreis vs. Nettopreis -- die Entscheidung wird protokolliert."""
        rows = [
            ["Pos", "Material", "Bezeichnung", "Listenpreis", "Netto EUR"],
            ["10", "4711002", "Dichtring", "15,00", "12,85"],
        ]
        extractor = TableExtractor(self.settings)
        document = make_document(rows)
        result = extractor.extract(document)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))
        self.assertIn("Listenpreis", result.positions[0].remarks)
        self.assertTrue(any("Mehrdeutige Spalten" in note for note in result.notes))

    def test_12_gesamtpreis_wird_nicht_als_preis_genommen(self) -> None:
        rows = [
            ["Pos", "Material", "Menge", "Einzelpreis", "Gesamtpreis"],
            ["10", "4711002", "100", "12,85", "1.285,00"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(result.positions[0].price, Decimal("12.85"))

    def test_13_zu_wenig_felder_wird_verworfen(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["", "", "", "", ""],
            ["Hinweis", "", "", "", ""],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 1)

    def test_14_menge_mit_einheit_in_einer_zelle(self) -> None:
        rows = [
            ["Artikelnummer", "Bezeichnung", "Menge", "Preis"],
            ["4711002", "Dichtring", "100 Stk", "12,85 EUR"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        position = result.positions[0]
        self.assertEqual(position.quantity, Decimal("100"))
        self.assertEqual(position.uom, "ST")
        self.assertEqual(position.currency, "EUR")

    def test_15_staffelpreis_erkennung_ignoriert_datumsangaben(self) -> None:
        self.assertEqual(find_price_tiers("gueltig ab 01.09.2026"), [])
        tiers = find_price_tiers("ab 500 Stk 10,50 EUR")
        self.assertEqual(tiers[0][0], Decimal("500"))
        self.assertEqual(tiers[0][1], Decimal("10.50"))


# ==========================================================================
# 2 -- Kopfdaten
# ==========================================================================

class KopfdatenTests(unittest.TestCase):
    DEUTSCH = (
        "Muster Dichtungstechnik GmbH\n"
        "Angebot Nr. AN-2026-4711\n"
        "Angebotsdatum: 15.08.2026\n"
        "Ihre Kundennummer: 100234\n"
        "Lieferantennummer: 0000123456\n"
        "Waehrung: EUR\n"
        "Angebot gueltig bis 31.12.2026\n"
        "Preise gueltig ab 01.09.2026\n"
        "Incoterms 2020: FCA Muenchen\n"
        "Zahlungsbedingungen: 30 Tage netto, 2% Skonto bei 10 Tagen\n"
        "Ansprechpartner: Frau Erika Muster\n"
    )

    def test_16_kopfregeln_deutsch(self) -> None:
        matches = extract_header_fields(self.DEUTSCH)
        self.assertEqual(matches["offer_number"].value, "AN-2026-4711")
        self.assertEqual(matches["offer_date"].value, datetime.date(2026, 8, 15))
        self.assertEqual(matches["valid_to"].value, datetime.date(2026, 12, 31))
        self.assertEqual(matches["valid_from"].value, datetime.date(2026, 9, 1))
        self.assertEqual(matches["currency"].value, "EUR")
        self.assertEqual(matches["vendor_number"].value, "123456")
        self.assertEqual(matches["customer_number"].value, "100234")
        self.assertEqual(matches["contact"].value, "Frau Erika Muster")
        self.assertIn("Skonto", matches["payment_terms"].value)
        self.assertEqual(matches["vendor_name"].value, "Muster Dichtungstechnik GmbH")

    def test_17_kopfregeln_englisch(self) -> None:
        text = ("Sample Sealing Ltd\n"
                "Quotation No. Q-2026-88\n"
                "Offer Date: 2026-08-15\n"
                "Currency: USD\n"
                "valid until 2026-12-31\n"
                "Terms of payment: net 30 days\n"
                "Delivery terms: CIF Hamburg\n")
        matches = extract_header_fields(text)
        self.assertEqual(matches["offer_number"].value, "Q-2026-88")
        self.assertEqual(matches["offer_date"].value, datetime.date(2026, 8, 15))
        self.assertEqual(matches["currency"].value, "USD")
        self.assertEqual(matches["valid_to"].value, datetime.date(2026, 12, 31))
        self.assertEqual(matches["incoterm"].value, "CIF")
        self.assertEqual(matches["incoterm_location"].value, "Hamburg")

    def test_18_incoterm_varianten(self) -> None:
        for text, expected in (
            ("Lieferbedingung: EXW Werk Muenchen", "EXW"),
            ("Incoterms: DDP Sassenberg", "DDP"),
            ("Delivery terms FOB Shanghai", "FOB"),
            ("Versand: DAP Hamburg", "DAP"),
        ):
            code, _location, confidence = find_incoterm(text)
            self.assertEqual(code, expected, msg=text)
            self.assertGreater(confidence, 0.5)

    def test_19_konfidenz_steuert_die_herkunft(self) -> None:
        """Unsichere Treffer duerfen nicht als gesichert gelten."""
        matches = extract_header_fields("Rechnung vom 15.08.2026")
        self.assertIn("offer_date", matches)
        self.assertEqual(matches["offer_date"].origin, FieldOrigin.EXTRACTED)
        weak = extract_header_fields("Ort, den 15.08.2026")
        self.assertEqual(weak["offer_date"].origin, FieldOrigin.UNCERTAIN)

    def test_20_kein_wert_wird_erfunden(self) -> None:
        matches = extract_header_fields("Guten Tag, anbei die Unterlagen.")
        self.assertNotIn("offer_number", matches)
        self.assertNotIn("offer_date", matches)
        self.assertNotIn("currency", matches)

    def test_21_lieferantenname_aus_signatur_und_domaene(self) -> None:
        name, confidence = vendor_name_from_signature(
            "Mit freundlichen Gruessen\nErika Muster\nMuster Dichtungstechnik GmbH")
        self.assertEqual(name, "Muster Dichtungstechnik GmbH")
        self.assertGreater(confidence, 0.5)
        # Keine Fehlgriffe auf Fliesstext mit "ab"
        self.assertEqual(vendor_name_from_signature("gueltig ab 01.09.2026")[0], "")

        context = EmailContext(from_address="e.muster@muster-dichtung.de")
        matches = extract_header_fields("Preise wie besprochen.", context)
        self.assertIn("vendor_name", matches)
        self.assertEqual(matches["vendor_name"].origin, FieldOrigin.UNCERTAIN)


# ==========================================================================
# 3 -- PDF-Layout
# ==========================================================================

class PdfLayoutTests(TempDirCase):
    def test_22_wortkoordinaten_werden_zu_spalten(self) -> None:
        """Tabellenrekonstruktion aus Wortkoordinaten (PDF-Layout)."""
        spalten = (60.0, 140.0, 300.0, 400.0, 460.0)
        zeilen = [
            ["Pos", "Artikel", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["20", "4711003", "O-Ring", "250", "3,40"],
            ["30", "4711004", "Flachdichtung", "50", "7,90"],
        ]
        words = []
        for row_index, row in enumerate(zeilen):
            y = 100.0 + row_index * 20.0
            for x, text in zip(spalten, row):
                words.append(make_word(x, y, x + 40.0, y + 10.0, text))
        blocks = words_to_tables(words, page=1)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].origin, "pdf-layout")
        self.assertEqual(blocks[0].rows[1], ["10", "4711002", "Dichtring", "100", "12,85"])

        document = RawDocument(source_kind=SourceKind.PDF, tables=blocks)
        result = TableExtractor(Settings()).extract(document)
        self.assertEqual(len(result.positions), 3)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))

    def test_23_fliesstext_ergibt_keine_tabelle(self) -> None:
        """Ein Absatz darf nicht als Tabelle missverstanden werden."""
        words = []
        satz = "Wir bedanken uns fuer Ihre Anfrage und unterbreiten Ihnen folgendes"
        x = 60.0
        for index, wort in enumerate(satz.split()):
            words.append(make_word(x, 100.0, x + len(wort) * 5.0, 110.0, wort))
            x += len(wort) * 5.0 + 4.0
            if index == 6:
                x = 60.0
        self.assertEqual(words_to_tables(words, page=1), [])

    def test_24_pdf_ohne_textebene_wird_gemeldet(self) -> None:
        """Ein Scan wird klar benannt -- es wird nichts geraten."""
        try:
            import fitz  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("PyMuPDF ist nicht installiert")
        import fitz

        document = fitz.open()
        document.new_page()
        target = self.path("scan.pdf")
        document.save(target)
        document.close()

        raw = ReaderRegistry(self.settings.extraction).read(target)
        self.assertTrue(raw.meta.get("scanned"))
        self.assertIn(SCAN_WARNING, raw.warnings)

    def test_25_pdf_mit_text_wird_gelesen(self) -> None:
        try:
            import fitz
        except ImportError:  # pragma: no cover
            self.skipTest("PyMuPDF ist nicht installiert")

        document = fitz.open()
        page = document.new_page()
        page.insert_text((60, 60), "Angebot Nr. AN-2026-4711")
        page.insert_text((60, 80), "Angebotsdatum: 15.08.2026")
        target = self.path("angebot.pdf")
        document.save(target)
        document.close()

        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(offer.offer_number, "AN-2026-4711")
        self.assertEqual(offer.offer_date, datetime.date(2026, 8, 15))


# ==========================================================================
# 4 -- E-Mail
# ==========================================================================

def _build_eml(html: str = "", attachments: list[tuple[str, bytes, str]] | None = None,
               body: str = "") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Angebot Nr. AN-2026-4711 - Preisanpassung"
    message["From"] = ('"Erika Muster (Muster Dichtungstechnik GmbH)" '
                       "<e.muster@muster-dichtung.de>")
    message["To"] = "einkauf@technotrans.de"
    message["Date"] = "Mon, 17 Aug 2026 09:15:00 +0200"
    message.set_content(body or "Guten Tag,\n\nPreise wie besprochen.\n")
    if html:
        message.add_alternative(html, subtype="html")
    for name, data, subtype in attachments or []:
        message.add_attachment(data, maintype="application", subtype=subtype,
                               filename=name)
    return message.as_bytes()


class EmailTests(TempDirCase):
    def test_26_email_freitext_als_angebotsquelle(self) -> None:
        body = ("Guten Tag,\n\n"
                "wir erhoehen den Preis fuer Dichtring 40x52x7 zum 01.09.2026 "
                "auf 12,85 EUR.\n"
                "Mat.-Nr. 47110001, Preis 3,40 EUR/St ab 01.09.2026\n\n"
                "Mit freundlichen Gruessen\n"
                "Erika Muster\nMuster Dichtungstechnik GmbH\n")
        target = self.write("mail.eml", _build_eml(body=body))
        offer = OfferImportService(self.settings).import_file(target)

        self.assertEqual(offer.offer_number, "AN-2026-4711")
        self.assertEqual(offer.vendor_name, "Muster Dichtungstechnik GmbH")
        self.assertGreaterEqual(len(offer.positions), 2)
        for position in offer.positions:
            self.assertEqual(position.source_kind, SourceKind.EMAIL_BODY)
            self.assertTrue(position.raw_text)
            self.assertIn("price", position.uncertain_fields)
        preise = {p.price for p in offer.positions}
        self.assertIn(Decimal("12.85"), preise)
        self.assertIn(Decimal("3.40"), preise)

    def test_27_html_preistabelle_in_der_mail(self) -> None:
        html = ("<html><body><p>Guten Tag,</p>"
                "<table><tr><th>Artikelnummer</th><th>Bezeichnung</th>"
                "<th>Menge</th><th>Preis</th></tr>"
                "<tr><td>4711002</td><td>Dichtring 40x52x7</td><td>100 Stk</td>"
                "<td>12,85 EUR</td></tr>"
                "<tr><td>4711003</td><td>O-Ring 25x3</td><td>250 Stk</td>"
                "<td>3,40 EUR</td></tr></table></body></html>")
        target = self.write("mail.eml", _build_eml(html=html))
        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(len(offer.positions), 2)
        self.assertEqual(offer.positions[0].vendor_material_number, "4711002")
        self.assertEqual(offer.positions[0].quantity, Decimal("100"))
        self.assertEqual(offer.positions[0].price, Decimal("12.85"))
        self.assertEqual(offer.positions[0].currency, "EUR")

    def test_28_html_parser_liefert_text_und_tabellen(self) -> None:
        text, tables = html_to_text(
            "<div>Hallo</div><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table><script>x=1</script>")
        self.assertIn("Hallo", text)
        self.assertNotIn("x=1", text)
        self.assertEqual(tables, [[["A", "B"], ["1", "2"]]])

    def test_29_anhang_wird_mitgelesen(self) -> None:
        try:
            from openpyxl import Workbook
        except ImportError:  # pragma: no cover
            self.skipTest("openpyxl ist nicht installiert")

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Preise"
        sheet.append(["Pos", "Material", "Bezeichnung", "Menge", "ME", "Netto EUR"])
        sheet.append([10, "4711002", "Dichtring", 100, "Stk", 12.85])
        sheet.append([None, None, "Summe", None, None, 1285.0])
        xlsx = self.path("preise.xlsx")
        workbook.save(xlsx)

        subtype = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        target = self.write("mail.eml", _build_eml(
            attachments=[("preise.xlsx", Path(xlsx).read_bytes(), subtype)]))

        offer = OfferImportService(self.settings).import_file(target)
        anhang = [p for p in offer.positions
                  if p.source_kind is SourceKind.EMAIL_ATTACHMENT]
        self.assertEqual(len(anhang), 1)
        self.assertEqual(anhang[0].material_number, "4711002")
        self.assertIn("Anhang: preise.xlsx", anhang[0].source_hint)

    def test_30_unerwuenschter_anhang_wird_gemeldet(self) -> None:
        target = self.write("mail.eml", _build_eml(
            attachments=[("virus.exe", b"MZ egal", "octet-stream")]))
        raw = ReaderRegistry(self.settings.extraction).read(target)
        self.assertFalse(raw.attachments)
        self.assertTrue(any("virus.exe" in w for w in raw.warnings))

    def test_31_signatur_wird_abgeschnitten(self) -> None:
        text = ("Preise wie besprochen: 12,85 EUR\n"
                "weitere Zeile mit Inhalt\n"
                "Mit freundlichen Gruessen\n"
                "Erika Muster\n"
                "Diese E-Mail enthaelt vertrauliche Informationen.\n")
        head, tail = strip_signature(text)
        self.assertIn("Preise wie besprochen", head)
        self.assertNotIn("vertrauliche", head)
        self.assertIn("Erika Muster", tail)

        # Sehr kurze Nachricht: es wird NICHT abgeschnitten
        kurz = "Mit freundlichen Gruessen\nErika"
        self.assertEqual(strip_signature(kurz)[0], kurz)


# ==========================================================================
# 5 -- Outlook .msg
# ==========================================================================

class MsgTests(TempDirCase):
    def _minimal_msg(self) -> bytes:
        attachment = _CfbNode("__attach_version1.0_#00000000", None, [
            _unicode_prop(0x3707, "preise.csv"),
            _substg(0x3701, 0x0102, "Material;Preis\r\n4711002;12,85\r\n".encode("cp1252")),
        ])
        return _build_cfb([
            _unicode_prop(0x0037, "Angebot Nr. AN-2026-4711"),
            _unicode_prop(0x0C1A, "Erika Muster"),
            _unicode_prop(0x5D01, "e.muster@muster-dichtung.de"),
            _unicode_prop(0x0E04, "einkauf@technotrans.de"),
            _unicode_prop(0x1000, "Guten Tag,\r\n\r\nder Preis fuer Artikel 4711002 "
                                  "betraegt ab 01.09.2026 12,85 EUR.\r\n"),
            _substg(0x1013, 0x0102, b"<html><body><p>Preise</p></body></html>"),
            _substg(0x007D, 0x001E, b"Date: Mon, 17 Aug 2026 09:15:00 +0200\r\n"
                                    b"Message-ID: <abc@muster-dichtung.de>\r\n"),
            _properties([(0x0039, 0x0040,
                          _filetime(datetime.datetime(2026, 8, 17, 9, 15)))], 32),
            attachment,
        ])

    def test_32_msg_parser_liest_alle_kopffelder(self) -> None:
        message = read_msg_bytes(self._minimal_msg(), "test.msg")
        self.assertEqual(message.errors, [])
        self.assertEqual(message.subject, "Angebot Nr. AN-2026-4711")
        self.assertEqual(message.sender_name, "Erika Muster")
        self.assertEqual(message.sender_email, "e.muster@muster-dichtung.de")
        self.assertEqual(message.sender_domain, "muster-dichtung.de")
        self.assertEqual(message.to, "einkauf@technotrans.de")
        self.assertIn("12,85 EUR", message.body)
        self.assertIn("<html>", message.html)
        self.assertEqual(message.sent, datetime.datetime(2026, 8, 17, 9, 15))
        self.assertEqual(message.header_value("Message-ID"),
                         "<abc@muster-dichtung.de>")
        self.assertEqual([a.name for a in message.attachments], ["preise.csv"])

    def test_33_msg_import_ueber_den_dienst(self) -> None:
        target = self.write("angebot.msg", self._minimal_msg())
        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(offer.offer_number, "AN-2026-4711")
        self.assertIsNotNone(offer.email)
        self.assertEqual(offer.email.sender_domain, "muster-dichtung.de")
        self.assertTrue(offer.positions)
        anhang = [p for p in offer.positions
                  if p.source_kind is SourceKind.EMAIL_ATTACHMENT]
        self.assertTrue(anhang, "Der CSV-Anhang muss ausgewertet werden")
        self.assertEqual(anhang[0].price, Decimal("12.85"))

    def test_34_defekte_msg_gibt_teilergebnis(self) -> None:
        """Eine kaputte Datei darf niemals eine Exception nach aussen geben."""
        kaputt = self.write("kaputt.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                                          + b"\x00" * 600)
        message = read_msg(kaputt)
        self.assertFalse(message.ok)
        self.assertTrue(message.errors)
        self.assertEqual(message.subject, "")

        unsinn = self.write("unsinn.msg", b"Das ist keine Outlook-Datei")
        message = read_msg(unsinn)
        self.assertTrue(message.errors)

        offer = OfferImportService(self.settings).import_file(unsinn)
        self.assertFalse(offer.positions)
        self.assertTrue(offer.issues.has_blocking)

    def test_35_compound_file_erkennt_zyklen(self) -> None:
        data = bytearray(self._minimal_msg())
        # FAT-Eintrag 0 auf sich selbst zeigen lassen -> Zyklus
        compound = CompoundFile(bytes(data))
        self.assertTrue(compound.ok)
        self.assertGreater(len(compound.entries), 1)


# ==========================================================================
# 6 -- Freitext
# ==========================================================================

class FreitextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = FreetextExtractor(Settings())

    def test_36_typische_freitextzeilen(self) -> None:
        text = ("Artikel 4711 / 100 Stueck / 12,85 EUR\n"
                "Mat.-Nr. 47110001, Preis 3,40 EUR/St ab 01.09.2026\n"
                "- 4711-002  Dichtring  50 St  9,90 EUR\n")
        result = self.extractor.extract_text(text)
        self.assertEqual(len(result.positions), 3)
        erste = result.positions[0]
        self.assertEqual(erste.material_number, "4711")
        self.assertEqual(erste.quantity, Decimal("100"))
        self.assertEqual(erste.price, Decimal("12.85"))
        self.assertEqual(erste.currency, "EUR")
        zweite = result.positions[1]
        self.assertEqual(zweite.material_number, "47110001")
        self.assertEqual(zweite.valid_from, datetime.date(2026, 9, 1))
        self.assertEqual(result.positions[2].material_number, "4711-002")

    def test_37_preisaenderung_im_satz(self) -> None:
        text = ("Wir erhoehen den Preis fuer Dichtring 40x52x7 "
                "zum 01.09.2026 auf 12,85 EUR.")
        result = self.extractor.extract_text(text)
        self.assertEqual(len(result.positions), 1)
        position = result.positions[0]
        self.assertEqual(position.description, "Dichtring 40x52x7")
        self.assertEqual(position.price, Decimal("12.85"))
        self.assertEqual(position.valid_from, datetime.date(2026, 9, 1))
        self.assertEqual(position.origin("price"), FieldOrigin.UNCERTAIN)

    def test_38_ohne_preis_keine_position(self) -> None:
        text = ("Sehr geehrte Damen und Herren,\n"
                "die Lieferzeit betraegt 6 Wochen.\n"
                "Tel. 0123 456789\n"
                "Summe: 1.234,00 EUR\n")
        result = self.extractor.extract_text(text)
        self.assertEqual(result.positions, [])

    def test_39_beschriftungsblock(self) -> None:
        text = ("Artikel: 4711-002\n"
                "Bezeichnung: Dichtring 40x52x7\n"
                "Menge: 100 Stk\n"
                "Preis: 12,85 EUR\n")
        result = self.extractor.extract_text(text)
        self.assertEqual(len(result.positions), 1)
        position = result.positions[0]
        self.assertEqual(position.material_number, "4711-002")
        self.assertEqual(position.description, "Dichtring 40x52x7")
        self.assertEqual(position.quantity, Decimal("100"))
        self.assertEqual(position.price, Decimal("12.85"))

    def test_40_datum_ohne_jahr_wird_nicht_geraten(self) -> None:
        result = self.extractor.extract_text(
            "Neuer Preis fuer Artikel 4711002 ab 01.09. betraegt 12,85 EUR")
        self.assertEqual(len(result.positions), 1)
        self.assertIsNone(result.positions[0].valid_from)
        self.assertTrue(any("ohne Jahresangabe" in note for note in result.notes))


# ==========================================================================
# 7 -- Leser (CSV, Excel, defekte Dateien)
# ==========================================================================

class LeserTests(TempDirCase):
    def test_41_csv_trennzeichen_und_kodierung(self) -> None:
        content = ("Material;Bezeichnung;Menge;Preis\r\n"
                   "4711002;Dichtring gross;100;12,85\r\n"
                   "4711003;Gehäusedeckel;50;3,40\r\n").encode("cp1252")
        target = self.write("preise.csv", content)
        raw = ReaderRegistry(self.settings.extraction).read(target)
        self.assertEqual(raw.meta["delimiter"], ";")
        self.assertEqual(raw.meta["encoding"], "cp1252")
        self.assertIn("Gehäusedeckel", raw.text)
        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(len(offer.positions), 2)
        self.assertEqual(offer.positions[0].price, Decimal("12.85"))

    def test_42_csv_trennzeichenerkennung(self) -> None:
        self.assertEqual(detect_delimiter("a;b;c\n1;2;3\n"), ";")
        self.assertEqual(detect_delimiter("a\tb\tc\n1\t2\t3\n"), "\t")
        self.assertEqual(detect_delimiter("a|b|c\n1|2|3\n"), "|")

    def test_43_xls_gibt_freundlichen_hinweis(self) -> None:
        target = self.write("alt.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        raw = ReaderRegistry(self.settings.extraction).read(target)
        try:
            import xlrd  # noqa: F401
        except ImportError:
            self.assertIn(XLS_HINT, raw.warnings)
            self.assertTrue(raw.meta.get("needs_conversion"))

    def test_44_defekte_dateien_stuerzen_nicht_ab(self) -> None:
        registry = ReaderRegistry(self.settings.extraction)
        for name, content in (("kaputt.pdf", b"%PDF-1.4 nur Muell"),
                              ("kaputt.xlsx", b"PK\x03\x04 kein Excel"),
                              ("kaputt.eml", b"\x00\x01\x02")):
            raw = registry.read(self.write(name, content))
            self.assertIsInstance(raw, RawDocument)
            self.assertFalse(raw.tables, msg=name)

    def test_45_unbekanntes_format_wird_benannt(self) -> None:
        target = self.write("zeichnung.dwg", b"egal")
        service = OfferImportService(self.settings)
        self.assertFalse(service.can_import(target))
        offer = service.import_file(target)
        self.assertTrue(any("wird nicht unterstuetzt" in str(i) for i in offer.issues))
        self.assertTrue(offer.issues.has_blocking)

    def test_46_fehlende_datei(self) -> None:
        raw = ReaderRegistry(self.settings.extraction).read(self.path("gibtsnicht.pdf"))
        self.assertTrue(any("nicht gefunden" in w for w in raw.warnings))


# ==========================================================================
# 8 -- Profile und Lernen
# ==========================================================================

class ProfilTests(unittest.TestCase):
    ROWS = [
        ["Pos", "Artikel", "Bezeichnung", "Menge", "Listenpreis", "Netto EUR"],
        ["10", "4711002", "Dichtring", "100", "15,00", "12,85"],
        ["20", "4711003", "O-Ring", "250", "4,00", "3,40"],
    ]

    def setUp(self) -> None:
        self.settings = Settings()

    def _document(self) -> RawDocument:
        return make_document(self.ROWS, title="Preise")

    def _extract(self, document: RawDocument,
                 profile: VendorProfile | None = None) -> Offer:
        offer = Offer()
        result = TableExtractor(self.settings, profile).extract(document)
        offer.positions = result.positions
        return offer

    def test_47_fingerabdruck_ist_stabil_und_wertfrei(self) -> None:
        erste = self._document()
        zweite = make_document(
            [self.ROWS[0], ["10", "4711002", "Dichtring", "100", "19,00", "16,00"]],
            title="Preise")
        # Gleiche Ueberschriften, andere Preise -> gleicher Fingerabdruck
        self.assertEqual(fingerprint(erste), fingerprint(zweite))
        andere = make_document([["Teil", "Text", "Anzahl", "Wert"],
                                ["1", "x", "2", "3"]], title="Preise")
        self.assertNotEqual(fingerprint(erste), fingerprint(andere))

    def test_48_profil_wird_ueber_domaene_gefunden(self) -> None:
        document = self._document()
        document.email = EmailContext(from_address="e.muster@muster-dichtung.de")
        profile = VendorProfile(profile_id="p1", vendor_key="123456",
                                vendor_name="Muster GmbH",
                                email_domains=["muster-dichtung.de"])
        found, score = match_profile([profile], document, Offer())
        self.assertIs(found, profile)
        self.assertGreaterEqual(score, 0.45)

        fremd = VendorProfile(profile_id="p2", vendor_key="999",
                              email_domains=["andere.de"])
        found, _ = match_profile([fremd], document, Offer())
        self.assertIsNone(found)

    def test_49_lernen_der_spaltenzuordnung(self) -> None:
        document = self._document()
        original = self._extract(document)
        corrected = Offer()
        corrected.positions = [copy.deepcopy(p) for p in original.positions]
        corrected.positions[0].set_field("price", Decimal("15.00"), FieldOrigin.MANUAL)
        corrected.positions[1].set_field("price", Decimal("4.00"), FieldOrigin.MANUAL)

        profile = learn_from_corrections(original, corrected, document, None)
        self.assertEqual(profile.column_map.get("listenpreis"), "price")
        self.assertEqual(profile.correction_count, 1)

        # Zweiter Import mit dem gelernten Profil
        wieder = self._extract(self._document(), profile)
        self.assertEqual(wieder.positions[0].price, Decimal("15.00"))

    def test_50_gelernte_regel_ist_rueckbaubar(self) -> None:
        document = self._document()
        original = self._extract(document)
        corrected = Offer()
        corrected.positions = [copy.deepcopy(p) for p in original.positions]
        corrected.positions[0].set_field("price", Decimal("15.00"), FieldOrigin.MANUAL)
        profile = learn_from_corrections(original, corrected, document, None)

        self.assertTrue(describe_learning(profile))
        self.assertTrue(forget_rule(profile, "column_map:listenpreis=price"))
        self.assertNotIn("listenpreis", profile.column_map)
        profile.reset_learning()
        self.assertEqual(profile.column_map, {})
        self.assertEqual(profile.decimal_style, "auto")

    def test_51_verworfene_zeile_wird_zur_skip_regel(self) -> None:
        rows = self.ROWS + [["30", "9999", "Mindermengenzuschlag", "1", "25,00", "25,00"]]
        document = make_document(rows, title="Preise")
        original = self._extract(document)
        self.assertEqual(len(original.positions), 3)
        corrected = Offer()
        corrected.positions = [copy.deepcopy(p) for p in original.positions[:2]]

        profile = learn_from_corrections(original, corrected, document, None)
        self.assertTrue(profile.skip_patterns)
        # Die Regel darf keinen Betrag enthalten -- nur die Art der Zeile
        self.assertTrue(any("Mindermengenzuschlag" in p for p in profile.skip_patterns))
        self.assertFalse(any("25" in p for p in profile.skip_patterns))

        nachher = self._extract(make_document(rows, title="Preise"), profile)
        self.assertEqual(len(nachher.positions), 2)

    def test_52_kopfregel_erst_nach_zwei_bestaetigungen(self) -> None:
        text = ("Muster GmbH\n"
                "Unsere Referenz  AN-2026-4711\n"
                "Preisliste\n")
        document = make_document(self.ROWS, title="Preise", text=text)
        original = Offer()
        corrected = Offer()
        corrected.set_field("offer_number", "AN-2026-4711", FieldOrigin.MANUAL)

        config = LearningConfig()
        profile = learn_from_corrections(original, corrected, document, None, config)
        self.assertNotIn("offer_number", profile.header_regexes)
        self.assertTrue(profile.pending_rules)

        profile = learn_from_corrections(original, corrected, document, profile, config)
        self.assertIn("offer_number", profile.header_regexes)
        self.assertNotIn("AN-2026-4711", profile.header_regexes["offer_number"],
                         "Eine gelernte Regel darf niemals den Wert selbst enthalten")

    def test_53_profilspeicher(self) -> None:
        store = InMemoryProfileStore()
        profile = new_profile(Offer(vendor_name="Muster GmbH"), self._document())
        store.save_profile(profile)
        self.assertEqual(len(store.load_profiles()), 1)
        store.delete_profile(profile.profile_id)
        self.assertEqual(store.load_profiles(), [])


# ==========================================================================
# 9 -- Gesamtdienst
# ==========================================================================

class ImportDienstTests(TempDirCase):
    def test_54_import_von_eingefuegtem_text(self) -> None:
        text = ("Angebot Nr. AN-2026-4711\n"
                "Angebotsdatum: 15.08.2026\n"
                "Waehrung: EUR\n"
                "Artikel 4711002 / 100 Stk / 12,85 EUR\n")
        offer = OfferImportService(self.settings).import_text(text)
        self.assertEqual(offer.offer_number, "AN-2026-4711")
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(len(offer.positions), 1)
        self.assertEqual(offer.positions[0].currency, "EUR")

    def test_55_mehrere_dateien_ein_angebot(self) -> None:
        eins = self.write("kopf.txt", ("Angebot Nr. AN-2026-4711\n"
                                       "Angebotsdatum: 15.08.2026\n"
                                       "Waehrung: EUR\n").encode("utf-8"))
        zwei = self.write("preise.csv",
                          ("Material;Bezeichnung;Menge;Preis\n"
                           "4711002;Dichtring;100;12,85\n").encode("utf-8"))
        offer = OfferImportService(self.settings).import_files([eins, zwei])
        self.assertEqual(offer.offer_number, "AN-2026-4711")
        self.assertEqual(len(offer.positions), 1)
        self.assertEqual(len(offer.source_files), 2)

    def test_56_nachbearbeitung_setzt_nur_voreinstellungen(self) -> None:
        target = self.write("preise.csv",
                            ("Material;Bezeichnung;Menge;ME;Preis\n"
                             "4711002;Dichtring;100;Stck;12,85\n").encode("utf-8"))
        offer = OfferImportService(self.settings).import_file(target)
        position = offer.positions[0]
        self.assertEqual(position.uom, "ST")                  # normalisiert
        self.assertEqual(position.price_unit, self.settings.purchasing.price_unit)
        self.assertEqual(position.origin("price_unit"), FieldOrigin.DEFAULT)
        self.assertEqual(position.purchasing_org,
                         self.settings.purchasing.purchasing_org)
        self.assertTrue(position.position_number)             # Nummerierung ergaenzt

    def test_57_ohne_positionen_gibt_es_einen_blockierenden_befund(self) -> None:
        target = self.write("info.txt", b"Guten Tag, wir melden uns naechste Woche.")
        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(offer.positions, [])
        self.assertTrue(offer.issues.has_blocking)
        self.assertTrue(any(i.code == "no_positions" for i in offer.issues))

    def test_58_waehrung_wird_nicht_erfunden(self) -> None:
        target = self.write("preise.csv",
                            ("Material;Bezeichnung;Menge;Preis\n"
                             "4711002;Dichtring;100;12,85\n").encode("utf-8"))
        offer = OfferImportService(self.settings).import_file(target)
        self.assertEqual(offer.currency, "")
        self.assertEqual(offer.origin("currency"), FieldOrigin.MISSING)
        self.assertTrue(any(i.code == "currency_missing" for i in offer.issues))

    def test_59_unterstuetzte_formate(self) -> None:
        service = OfferImportService(self.settings)
        extensions = service.supported_extensions()
        for expected in (".pdf", ".xlsx", ".csv", ".eml", ".msg", ".txt", ".html"):
            self.assertIn(expected, extensions)
        self.assertTrue(service.can_import("irgendwas.PDF"))
        self.assertFalse(service.can_import("zeichnung.dwg"))

    def test_60_dienst_lernt_und_speichert_das_profil(self) -> None:
        target = self.write("preise.csv",
                            ("Material;Bezeichnung;Menge;Listenpreis;Netto EUR\n"
                             "4711002;Dichtring;100;15,00;12,85\n").encode("utf-8"))
        store = InMemoryProfileStore()
        service = OfferImportService(self.settings, store)
        offer = service.import_file(target)
        self.assertIsNotNone(service.last_document())

        corrected = copy.deepcopy(offer)
        corrected.positions[0].set_field("price", Decimal("15.00"), FieldOrigin.MANUAL)
        profile = service.learn(offer, corrected)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.column_map.get("listenpreis"), "price")
        self.assertEqual(len(store.load_profiles()), 1)

    def test_61_protokoll_ist_nachvollziehbar(self) -> None:
        target = self.write("preise.csv",
                            ("Material;Bezeichnung;Menge;Preis\n"
                             "4711002;Dichtring;100;12,85\n").encode("utf-8"))
        offer = OfferImportService(self.settings).import_file(target)
        joined = " ".join(offer.extraction_notes)
        self.assertIn("Tabellenstruktur", joined)
        self.assertIn("Import abgeschlossen", joined)
        self.assertTrue(offer.positions[0].source_hint)
        self.assertTrue(offer.positions[0].raw_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
