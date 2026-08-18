"""Tests der Angebotserkennung (unittest, ohne externe Testframeworks).

Abgedeckt werden bewusst *verschiedene Lieferantenformate*, denn genau darin
liegt die Schwierigkeit des Imports:

* die beiden Artikelnummern und ihre mehrdeutigen Spaltenueberschriften
  ("Artikelnummer" kann unsere oder seine Nummer sein)
* Excel mit und ohne Kopfzeile, mehrzeilige Koepfe, Beschriftung/Wert-Paare
* deutsche und englische Zahlenformate, Waehrungszeichen
* Summenzeilen, Fortsetzungszeilen, Staffelpreise
* E-Mails: Freitext, HTML-Preistabelle, Anhang, Signaturschnitt
* Outlook-.msg (Compound File wird im Test selbst erzeugt)
* Falsch-Treffer in den Kopfregeln ("lautet" ist keine Angebotsnummer)
* Lernen aus Anwenderkorrekturen
* defekte Dateien (duerfen nie eine Exception nach aussen geben)

Leitgedanke aller Tests: **lieber leer als falsch.**  Ein nicht erkanntes Feld
ist in Ordnung, ein stillschweigend falsches nicht.
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

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_extraction_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import FieldOrigin, SourceKind                    # noqa: E402
from app.models.offer import EmailContext, Offer                        # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.services.extraction.freetext_extractor import FreetextExtractor  # noqa: E402
from app.services.extraction.header_rules import (                      # noqa: E402
    extract_header_fields,
    find_incoterm,
    label_value_pairs,
    label_value_text,
    vendor_name_from_signature,
)
from app.services.extraction.learning import (                          # noqa: E402
    describe_learning,
    forget_rule,
)
from app.services.extraction.material_roles import (                    # noqa: E402
    compile_own_pattern,
    find_labelled_material_numbers,
    header_role,
    matches_own_pattern,
    own_ratio,
    resolve_position_roles,
)
from app.services.extraction.plausibility import (                      # noqa: E402
    CODE_DOCUMENT_TOTAL_MISMATCH,
    CODE_LINE_TOTAL_MISMATCH,
    CODE_POSITION_GAP,
    CODE_PRICE_OUTLIER,
)
from app.services.extraction.profiles import (                          # noqa: E402
    InMemoryProfileStore,
    VendorProfile,
    match_profile,
)
from app.services.extraction.table_extractor import (                   # noqa: E402
    TableExtractor,
    find_price_tiers,
    is_summary_row,
    parse_number,
)
from app.services.offer_import_service import OfferImportService        # noqa: E402
from app.services.readers.base import RawDocument, TableBlock           # noqa: E402
from app.services.readers.email_reader import html_to_text, strip_signature  # noqa: E402
from app.utils.msg_reader import read_msg                               # noqa: E402


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


def first_position(rows: list[list[str]], settings: Settings | None = None,
                   profile: VendorProfile | None = None) -> OfferPosition:
    """Erste Position aus einer Tabelle -- Kurzform fuer viele Tests."""
    settings = settings or Settings()
    result = TableExtractor(settings, profile).extract(make_document(rows))
    if not result.positions:
        raise AssertionError(f"keine Position erkannt (Notizen: {result.notes})")
    return result.positions[0]


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


def _unicode_prop(prop_id: int, text: str) -> _CfbNode:
    return _CfbNode(f"__substg1.0_{prop_id:04X}001F", text.encode("utf-16-le"))


class TempDirCase(unittest.TestCase):
    """Basisklasse mit temporaerem Arbeitsverzeichnis."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="angebot_test_")
        self.settings = Settings()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, data: bytes) -> str:
        path = Path(self.tmp) / name
        path.write_bytes(data)
        return str(path)


# ==========================================================================
# 1. Die beiden Artikelnummern -- der Kern der Aufgabe
# ==========================================================================

class ArtikelnummerSpaltenTests(unittest.TestCase):
    """Welche Spalte ist UNSERE Materialnummer, welche die des Lieferanten?

    Der Lieferant beschriftet aus *seiner* Sicht: unsere SAP-Nummer heisst bei
    ihm "Kundenartikelnummer"/"Ihre Art.-Nr.", seine eigene schlicht
    "Artikelnummer".  Dieselbe Ueberschrift kann also je nach Lieferant das
    eine oder das andere bedeuten.
    """

    def setUp(self) -> None:
        self.settings = Settings()

    def _analyse(self, rows: list[list[str]], profile: VendorProfile | None = None):
        extractor = TableExtractor(self.settings, profile)
        analysis = extractor.analyze(make_document(rows).tables[0].normalized())
        return analysis, {a.field: i for i, a in analysis.columns.items()}

    # -- Fall A: "Artikel-Nr." (seine) + "Ihre Artikelnummer" (unsere) ------
    def test_01_fall_a_deutsch_beide_spalten(self) -> None:
        rows = [
            ["Pos", "Artikel-Nr.", "Ihre Artikelnummer", "Bezeichnung", "Menge", "Preis"],
            ["10", "DR-40527-NBR", "47110001", "Dichtring", "500", "12,85"],
        ]
        _, felder = self._analyse(rows)
        self.assertEqual(felder["material_number"], 2)
        self.assertEqual(felder["vendor_material_number"], 1)
        position = first_position(rows, self.settings)
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.vendor_material_number, "DR-40527-NBR")

    # -- Fall B: "Unsere Art.-Nr." (seine) + "Kundenartikelnummer" (unsere) -
    def test_02_fall_b_unsere_art_nr_ist_die_des_lieferanten(self) -> None:
        rows = [
            ["Pos", "Unsere Art.-Nr.", "Kundenartikelnummer", "Bezeichnung",
             "Menge", "Preis"],
            ["10", "DR-40527-NBR", "47110001", "Dichtring", "500", "12,85"],
        ]
        _, felder = self._analyse(rows)
        self.assertEqual(felder["vendor_material_number"], 1,
                         "'Unsere Art.-Nr.' ist die Nummer des Lieferanten")
        self.assertEqual(felder["material_number"], 2)
        position = first_position(rows, self.settings)
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.vendor_material_number, "DR-40527-NBR")

    # -- Fall C: nur "Artikelnummer", Inhalt ist unsere Nummer -------------
    def test_03_fall_c_neutrale_ueberschrift_inhalt_entscheidet(self) -> None:
        rows = [
            ["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
            ["20", "47110002", "O-Ring", "200", "8,90"],
        ]
        analysis, felder = self._analyse(rows)
        self.assertIn("material_number", felder,
                      "8-stellig numerisch -> das ist unsere Materialnummer")
        self.assertNotIn("vendor_material_number", felder)
        self.assertTrue(any("sieht nach unserer Materialnummer aus" in n
                            for n in analysis.notes), analysis.notes)

    def test_04_fall_c_wird_als_unsicher_gekennzeichnet(self) -> None:
        rows = [
            ["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
        ]
        position = first_position(rows, self.settings)
        self.assertEqual(position.material_number, "47110001")
        self.assertIn("material_number", position.uncertain_fields,
                      "Inhaltsbasierte Zuordnung muss geprueft werden")

    def test_05_fall_c_notiz_landet_im_angebot(self) -> None:
        service = OfferImportService(self.settings, InMemoryProfileStore())
        offer = service.import_text(
            "Pos;Artikelnummer;Bezeichnung;Menge;Preis\n"
            "10;47110001;Dichtring;500;12,85\n", "artikel.csv")
        self.assertTrue(any("sieht nach unserer Materialnummer aus" in n
                            for n in offer.extraction_notes),
                        offer.extraction_notes)

    # -- Fall D: englische Ueberschriften ----------------------------------
    def test_06_fall_d_englisch(self) -> None:
        rows = [
            ["Item", "Part No", "Customer part no", "Description", "Qty", "Price"],
            ["10", "DR-40527-NBR", "47110001", "Sealing ring", "500", "12.85"],
        ]
        _, felder = self._analyse(rows)
        self.assertEqual(felder["material_number"], 2)
        self.assertEqual(felder["vendor_material_number"], 1)
        self.assertEqual(felder["position_number"], 0)

    def test_07_weitere_englische_schreibweisen(self) -> None:
        for kunde, lieferant in (("Your part number", "Supplier part no"),
                                 ("Customer material", "Our part number"),
                                 ("Your ref.", "Manufacturer part no")):
            with self.subTest(kunde=kunde):
                rows = [["Pos", lieferant, kunde, "Description", "Qty", "Price"],
                        ["10", "AB-99", "47110001", "Ring", "5", "12.85"]]
                _, felder = self._analyse(rows)
                self.assertEqual(felder.get("material_number"), 2, felder)
                self.assertEqual(felder.get("vendor_material_number"), 1, felder)

    def test_08_weitere_deutsche_schreibweisen(self) -> None:
        for kunde in ("Ihre Art.-Nr.", "Kd-Art-Nr.", "Ihre Materialnummer",
                      "Kundenmaterial", "Ihre Bestellnummer", "Bestellnummer Kunde"):
            with self.subTest(kunde=kunde):
                rows = [["Pos", "Sachnummer", kunde, "Bezeichnung", "Menge", "Preis"],
                        ["10", "AB-99", "47110001", "Ring", "5", "12,85"]]
                _, felder = self._analyse(rows)
                self.assertEqual(felder.get("material_number"), 2, felder)
                self.assertEqual(felder.get("vendor_material_number"), 1, felder)

    def test_09_lieferantenmarker_in_der_ueberschrift(self) -> None:
        for lieferant in ("Unsere Artikelnummer", "Hersteller-Nr.", "Typ",
                          "Lieferantenmaterial", "Supplier part no"):
            with self.subTest(lieferant=lieferant):
                rows = [["Pos", "Materialnummer", lieferant, "Bezeichnung",
                         "Menge", "Preis"],
                        ["10", "47110001", "AB-99", "Ring", "5", "12,85"]]
                _, felder = self._analyse(rows)
                self.assertEqual(felder.get("vendor_material_number"), 2, felder)

    # -- Tauscherkennung ---------------------------------------------------
    def test_10_swap_erkennung_tauscht_vertauschte_nummern(self) -> None:
        """Inhalt schlaegt Ueberschrift: hier hat der Lieferant verwechselt."""
        position = OfferPosition()
        position.set_field("material_number", "DR-40527-NBR", FieldOrigin.EXTRACTED)
        position.set_field("vendor_material_number", "47110001", FieldOrigin.EXTRACTED)
        note = resolve_position_roles(position, compile_own_pattern(r"^\d{6,18}$"))
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.vendor_material_number, "DR-40527-NBR")
        self.assertIn("getauscht", note)

    def test_11_swap_setzt_beide_felder_auf_unsicher(self) -> None:
        position = OfferPosition()
        position.set_field("material_number", "DR-40527-NBR", FieldOrigin.EXTRACTED)
        position.set_field("vendor_material_number", "47110001", FieldOrigin.EXTRACTED)
        resolve_position_roles(position, compile_own_pattern(r"^\d{6,18}$"))
        self.assertIn("material_number", position.uncertain_fields)
        self.assertIn("vendor_material_number", position.uncertain_fields)

    def test_12_swap_ueber_den_importdienst(self) -> None:
        service = OfferImportService(self.settings, InMemoryProfileStore())
        offer = service.import_text(
            "Pos;Materialnummer;Lieferantenmaterial;Bezeichnung;Menge;Preis\n"
            "10;DR-40527-NBR;47110001;Dichtring;500;12,85\n", "swap.csv")
        position = offer.positions[0]
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.vendor_material_number, "DR-40527-NBR")
        self.assertTrue(any("getauscht" in n for n in offer.extraction_notes),
                        offer.extraction_notes)

    def test_13_kein_tausch_wenn_alles_stimmt(self) -> None:
        position = OfferPosition()
        position.set_field("material_number", "47110001", FieldOrigin.EXTRACTED)
        position.set_field("vendor_material_number", "DR-40527-NBR",
                           FieldOrigin.EXTRACTED)
        note = resolve_position_roles(position, compile_own_pattern(r"^\d{6,18}$"))
        self.assertEqual(note, "")
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.uncertain_fields, [])

    def test_14_kein_tausch_wenn_beide_unpassend(self) -> None:
        """Passt keine der beiden Nummern, wird nichts umsortiert."""
        position = OfferPosition()
        position.set_field("material_number", "AB-1", FieldOrigin.EXTRACTED)
        position.set_field("vendor_material_number", "CD-2", FieldOrigin.EXTRACTED)
        self.assertEqual(resolve_position_roles(
            position, compile_own_pattern(r"^\d{6,18}$")), "")
        self.assertEqual(position.material_number, "AB-1")

    def test_15_swap_abschaltbar(self) -> None:
        self.settings.extraction.swap_detection = False
        service = OfferImportService(self.settings, InMemoryProfileStore())
        offer = service.import_text(
            "Pos;Materialnummer;Lieferantenmaterial;Bezeichnung;Menge;Preis\n"
            "10;DR-40527-NBR;47110001;Dichtring;500;12,85\n", "swap.csv")
        self.assertEqual(offer.positions[0].material_number, "DR-40527-NBR")

    # -- Muster und Konfiguration ------------------------------------------
    def test_16_eigenes_nummernkreismuster_ist_konfigurierbar(self) -> None:
        """Der Kunde muss sein Schema eintragen koennen (hier: 'M-' + 5 Ziffern)."""
        self.settings.extraction.own_material_pattern = r"^M-\d{5}$"
        rows = [["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
                ["10", "M-40527", "Dichtring", "500", "12,85"]]
        _, felder = self._analyse(rows)
        self.assertIn("material_number", felder)

    def test_17_inhaltserkennung_abschaltbar(self) -> None:
        self.settings.extraction.own_material_pattern_active = False
        rows = [["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
                ["10", "47110001", "Dichtring", "500", "12,85"]]
        _, felder = self._analyse(rows)
        self.assertIn("vendor_material_number", felder,
                      "ohne Inhaltspruefung zaehlt allein die Ueberschrift")

    def test_18_kaputtes_muster_faellt_auf_den_standard_zurueck(self) -> None:
        regex = compile_own_pattern(r"^\d{6,18")       # Klammer fehlt
        self.assertTrue(matches_own_pattern("47110001", regex))

    def test_19_muster_greift_nicht_bei_zu_kurzen_nummern(self) -> None:
        regex = compile_own_pattern(r"^\d{6,18}$")
        self.assertTrue(matches_own_pattern("47110001", regex))
        self.assertFalse(matches_own_pattern("4711", regex))
        self.assertFalse(matches_own_pattern("DR-40527-NBR", regex))

    def test_20_anteil_passender_werte_einer_spalte(self) -> None:
        regex = compile_own_pattern(r"^\d{6,18}$")
        self.assertEqual(own_ratio(["47110001", "47110002"], regex), 1.0)
        self.assertEqual(own_ratio(["47110001", "AB-1"], regex), 0.5)
        self.assertEqual(own_ratio([], regex), 0.0)

    def test_21_ueberschriftenmarker(self) -> None:
        self.assertEqual(header_role("Ihre Artikelnummer"), "material_number")
        self.assertEqual(header_role("Customer part no"), "material_number")
        self.assertEqual(header_role("Unsere Art.-Nr."), "vendor_material_number")
        self.assertEqual(header_role("Supplier part no"), "vendor_material_number")
        self.assertEqual(header_role("Artikelnummer"), "",
                         "neutrale Ueberschrift entscheidet nichts")

    def test_22_gemischte_spalte_wird_nicht_umgetragen(self) -> None:
        """Unter 70 % passender Werte bleibt es bei der Ueberschrift."""
        rows = [
            ["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
            ["10", "AB-1234", "Dichtring", "500", "12,85"],
            ["20", "CD-5678", "O-Ring", "200", "8,90"],
            ["30", "EF-9012", "Flachdichtung", "100", "3,40"],
        ]
        _, felder = self._analyse(rows)
        self.assertIn("vendor_material_number", felder)
        self.assertNotIn("material_number", felder)

    # -- Freitext ----------------------------------------------------------
    def test_23_freitext_kundenbeschriftung_ist_unsere_nummer(self) -> None:
        for text in ("Ihre Materialnummer 47110001",
                     "Kundenartikelnummer: 47110001",
                     "unter Ihrer Art.-Nr. 47110001",
                     "Customer part 47110001"):
            with self.subTest(text=text):
                eigen, fremd = find_labelled_material_numbers(text)
                self.assertEqual(eigen, "47110001", f"{text!r} -> {eigen!r}")
                self.assertEqual(fremd, "")

    def test_24_freitext_lieferantenbeschriftung(self) -> None:
        for text in ("unsere Artikelnummer DR-40527",
                     "our part no. DR-40527",
                     "Hersteller-Nr. DR-40527"):
            with self.subTest(text=text):
                eigen, fremd = find_labelled_material_numbers(text)
                self.assertEqual(fremd, "DR-40527", f"{text!r} -> {fremd!r}")
                self.assertEqual(eigen, "")

    def test_25_freitextposition_mit_kundenbeschriftung(self) -> None:
        extractor = FreetextExtractor(self.settings)
        result = extractor.extract_text(
            "Dichtring, Ihre Materialnummer 47110001, 12,85 EUR je Stueck")
        self.assertTrue(result.positions)
        self.assertEqual(result.positions[0].material_number, "47110001")

    def test_26_freitextposition_mit_lieferantenbeschriftung(self) -> None:
        service = OfferImportService(self.settings, InMemoryProfileStore())
        offer = service.import_text(
            "Dichtring, unsere Artikelnummer DR-40527, 12,85 EUR je Stueck",
            "mail.txt")
        self.assertTrue(offer.positions)
        self.assertEqual(offer.positions[0].vendor_material_number, "DR-40527")

    # -- Gelernte Rolle ----------------------------------------------------
    def test_27_gelernte_spaltenrolle_schlaegt_die_heuristik(self) -> None:
        """Hat der Anwender korrigiert, gilt das beim naechsten Angebot."""
        profile = VendorProfile(profile_id="p1", vendor_key="123456")
        profile.material_column_role["artikelnummer"] = "vendor_material_number"
        rows = [["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
                ["10", "47110001", "Dichtring", "500", "12,85"]]
        _, felder = self._analyse(rows, profile)
        self.assertIn("vendor_material_number", felder,
                      "die bestaetigte Rolle darf nicht ueberstimmt werden")

    def test_28_gelernte_rolle_traegt_spalte_um(self) -> None:
        profile = VendorProfile(profile_id="p1", vendor_key="123456")
        profile.material_column_role["sachnummer"] = "material_number"
        rows = [["Pos", "Sachnummer", "Bezeichnung", "Menge", "Preis"],
                ["10", "AB-4711", "Dichtring", "500", "12,85"]]
        analysis, felder = self._analyse(rows, profile)
        self.assertIn("material_number", felder)
        self.assertTrue(any("Lieferantenprofil" in n for n in analysis.notes),
                        analysis.notes)


# ==========================================================================
# 2. Tabellenstruktur
# ==========================================================================

class TabellenTests(unittest.TestCase):
    """Kopfzeilen, Zahlenformate, Summen-, Fortsetzungs- und Staffelzeilen."""

    def setUp(self) -> None:
        self.settings = Settings()

    def test_30_excel_mit_kopfzeile(self) -> None:
        rows = [
            ["Pos", "Materialnummer", "Bezeichnung", "Menge", "ME", "Preis"],
            ["10", "47110001", "Dichtring NBR", "500", "St", "12,85"],
            ["20", "47110002", "O-Ring Viton", "200", "St", "8,90"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        position = result.positions[0]
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.price, Decimal("12.85"))
        self.assertEqual(position.uom, "ST")
        self.assertEqual(position.quantity, Decimal("500"))

    def test_31_excel_ohne_kopfzeile(self) -> None:
        """Ohne Ueberschriften muss die Spaltenart aus den Daten kommen."""
        rows = [
            ["47110005", "Kugellager 6204-2RS", "1000", "ST", "4,55", "01.09.2026"],
            ["49900010", "Hydraulikschlauch DN12", "300", "M", "7,20", "01.09.2026"],
            ["48200111", "Gleitringdichtung KP-40", "40", "ST", "289,00", "01.09.2026"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 3)
        self.assertEqual(result.positions[0].material_number, "47110005")
        self.assertEqual(result.positions[0].price, Decimal("4.55"))
        self.assertIn("price", result.positions[0].uncertain_fields,
                      "ohne Ueberschrift ist alles nur unsicher erkannt")

    def test_32_mehrzeilige_kopfzeile(self) -> None:
        """Kopfzeile ueber zwei Zeilen ("Netto" / "Preis")."""
        rows = [
            ["Pos", "Artikel", "Bezeichnung", "Menge", "Netto"],
            ["Nr.", "Nummer", "", "", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["20", "4711003", "O-Ring", "250", "3,40"],
        ]
        extractor = TableExtractor(self.settings)
        analysis = extractor.analyze(make_document(rows).tables[0].normalized())
        self.assertEqual(analysis.data_start, 2)
        result = extractor.extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))

    def test_33_datenzeile_wird_nicht_als_kopffortsetzung_verschmolzen(self) -> None:
        rows = [
            ["Material", "Bezeichnung", "Menge", "Preis"],
            ["4711002", "Dichtring", "100", "12,85"],
        ]
        extractor = TableExtractor(self.settings)
        analysis = extractor.analyze(make_document(rows).tables[0].normalized())
        self.assertEqual(analysis.data_start, 1)

    def test_34_deutsche_zahlen(self) -> None:
        rows = [
            ["Material", "Bezeichnung", "Menge", "Preis"],
            ["4711002", "Dichtring", "1.500", "1.234,56"],
        ]
        position = first_position(rows, self.settings)
        self.assertEqual(position.quantity, Decimal("1500"))
        self.assertEqual(position.price, Decimal("1234.56"))

    def test_35_englische_zahlen(self) -> None:
        rows = [
            ["Part No", "Description", "Quantity", "Unit price"],
            ["4711002", "Sealing ring", "1,500", "1,234.56"],
        ]
        position = first_position(rows, self.settings)
        self.assertEqual(position.quantity, Decimal("1500"))
        self.assertEqual(position.price, Decimal("1234.56"))

    def test_36_dezimalstil_deutet_mehrdeutige_werte_um(self) -> None:
        """Ein gelerntes Zahlenformat loest '1.234' eindeutig auf."""
        self.assertEqual(parse_number("1,234", "en"), Decimal("1234"))
        self.assertEqual(parse_number("1.234", "en"), Decimal("1.234"))
        self.assertEqual(parse_number("1.234", "de"), Decimal("1234"))
        self.assertEqual(parse_number("1,234", "de"), Decimal("1.234"))
        self.assertEqual(parse_number("12,85", "de"), Decimal("12.85"))
        self.assertEqual(parse_number("12.85", "en"), Decimal("12.85"))

    def test_37_waehrungszeichen_am_preis(self) -> None:
        for zelle, waehrung in (("12,85 EUR", "EUR"), ("€ 12,85", "EUR"),
                                ("12.85 USD", "USD")):
            with self.subTest(zelle=zelle):
                rows = [["Material", "Bezeichnung", "Menge", "Preis"],
                        ["4711002", "Dichtring", "100", zelle]]
                position = first_position(rows, self.settings)
                self.assertEqual(position.price, Decimal("12.85"))
                self.assertEqual(position.currency, waehrung)

    def test_38_summenzeilen_werden_verworfen(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring", "100", "12,85"],
            ["", "", "Zwischensumme", "", "1.285,00"],
            ["", "", "MwSt 19 %", "", "244,15"],
            ["", "", "Gesamtbetrag", "", "1.529,15"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        texte = " ".join(p.description.lower() for p in result.positions)
        self.assertNotIn("zwischensumme", texte)
        self.assertNotIn("gesamtbetrag", texte)
        self.assertEqual(len(result.positions), 1)

    def test_39_summenzeilen_erkennung_direkt(self) -> None:
        self.assertTrue(is_summary_row(["", "", "Zwischensumme", "", "1.285,00"]))
        self.assertTrue(is_summary_row(["", "", "Total", "", "100"]))
        self.assertTrue(is_summary_row(["", "", "VAT 19 %", "", "19"]))
        self.assertFalse(is_summary_row(["10", "4711002", "Dichtring", "100", "12,85"]))

    def test_40_fortsetzungszeilen_werden_angehaengt(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "4711002", "Dichtring NBR", "100", "12,85"],
            ["", "", "40x52x7, lebensmittelecht", "", ""],
            ["20", "4711003", "O-Ring", "250", "3,40"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2)
        self.assertIn("lebensmittelecht", result.positions[0].description)

    def test_41_staffelpreise_werden_eigene_positionen(self) -> None:
        rows = [
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis", "Bemerkung"],
            ["10", "4711002", "Dichtring", "100", "12,85", "ab 500 Stk 11,90 EUR"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 2,
                         "Staffel darf nicht stillschweigend zusammengefasst werden")
        staffel = result.positions[1]
        self.assertEqual(staffel.price, Decimal("11.90"))
        self.assertEqual(staffel.quantity, Decimal("500"))
        self.assertIn("Staffel", staffel.remarks)

    def test_42_staffelpreise_im_text_finden(self) -> None:
        treffer = find_price_tiers("ab 100 Stk 11,90 EUR, ab 500: 10,50 EUR")
        self.assertEqual(len(treffer), 2)
        self.assertEqual(treffer[0][0], Decimal("100"))
        self.assertEqual(treffer[0][1], Decimal("11.90"))

    def test_43_datum_ist_kein_staffelpreis(self) -> None:
        self.assertEqual(find_price_tiers("gueltig ab 01.09.2026"), [])

    def test_44_mehrdeutige_preisspalten_werden_aufgeloest(self) -> None:
        rows = [
            ["Material", "Bezeichnung", "Listenpreis", "Nettopreis"],
            ["4711002", "Dichtring", "15,00", "12,85"],
        ]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(result.positions[0].price, Decimal("12.85"),
                         "der Nettopreis ist der massgebliche")
        self.assertTrue(any("Mehrdeutige Spalten" in n for n in result.notes),
                        result.notes)

    def test_45_gesamtpreisspalte_wird_nicht_als_preis_genommen(self) -> None:
        rows = [
            ["Material", "Bezeichnung", "Menge", "Einzelpreis", "Gesamtpreis"],
            ["4711002", "Dichtring", "100", "12,85", "1285,00"],
        ]
        position = first_position(rows, self.settings)
        self.assertEqual(position.price, Decimal("12.85"))


# ==========================================================================
# 3. Kopfdaten
# ==========================================================================

class KopfdatenTests(unittest.TestCase):
    """Kopffelder aus Fliesstext und aus Beschriftung/Wert-Zellen."""

    def setUp(self) -> None:
        self.settings = Settings()

    # -- Beschriftung links, Wert rechts daneben ---------------------------
    def test_50_label_wert_paare_aus_nachbarzellen(self) -> None:
        rows = [
            ["Muster Dichtungstechnik GmbH", "", "", ""],
            ["Angebot Nr.:", "ANG-2026-04711", "Zahlungsbedingungen:",
             "30 Tage netto"],
            ["Angebotsdatum:", "17.08.2026", "Incoterm:", "FCA Bielefeld"],
        ]
        paare = dict(label_value_pairs(rows))
        self.assertEqual(paare["Angebot Nr."], "ANG-2026-04711")
        self.assertEqual(paare["Angebotsdatum"], "17.08.2026")
        self.assertEqual(paare["Zahlungsbedingungen"], "30 Tage netto")

    def test_51_wert_steht_in_der_zelle_darunter(self) -> None:
        rows = [["Waehrung:", ""], ["EUR", ""]]
        paare = dict(label_value_pairs(rows))
        self.assertEqual(paare.get("Waehrung"), "EUR")

    def test_52_beschriftung_ohne_wert_liefert_nichts(self) -> None:
        rows = [["Angebot Nr.:", ""], ["", ""]]
        self.assertEqual(label_value_pairs(rows), [])

    def test_53_beschriftung_folgt_keiner_beschriftung(self) -> None:
        rows = [["Angebot Nr.:", "Angebotsdatum:"], ["", ""]]
        self.assertEqual(label_value_pairs(rows), [])

    def test_54_kopffelder_aus_label_wert_paaren(self) -> None:
        rows = [
            ["Angebot Nr.:", "ANG-2026-04711"],
            ["Angebotsdatum:", "17.08.2026"],
            ["Gueltig bis:", "15.11.2026"],
            ["Waehrung:", "EUR"],
        ]
        treffer = extract_header_fields(label_value_text(
            [TableBlock(rows=rows, origin="excel")]))
        self.assertEqual(treffer["offer_number"].value, "ANG-2026-04711")
        self.assertEqual(treffer["offer_date"].value, datetime.date(2026, 8, 17))
        self.assertEqual(treffer["valid_to"].value, datetime.date(2026, 11, 15))
        self.assertEqual(treffer["currency"].value, "EUR")

    def test_55_spaltenkopfzeile_wird_nicht_als_beschriftung_gedeutet(self) -> None:
        """'Gueltig ab' als Spaltenueberschrift ist kein Kopfdatenfeld."""
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "Preis", "Gueltig ab"],
                ["10", "4711002", "Dichtring", "100", "12,85", "01.09.2026"]]
        self.assertEqual(label_value_pairs(rows), [])

    # -- Falsch-Treffer ----------------------------------------------------
    def test_56_fuellwort_ist_keine_angebotsnummer(self) -> None:
        """"Unsere Angebotsnummer lautet ANG-2026-04712" -- nicht 'lautet'."""
        treffer = extract_header_fields(
            "Unsere Angebotsnummer lautet ANG-2026-04712, das Angebot ist "
            "60 Tage gueltig.")
        self.assertIn("offer_number", treffer)
        self.assertEqual(treffer["offer_number"].value, "ANG-2026-04712")

    def test_57_weitere_fuellwoerter(self) -> None:
        for satz, erwartet in (
                ("Die Angebotsnummer ist ANG-2026-04712", "ANG-2026-04712"),
                ("Unsere Angebotsnummer war AG-1188", "AG-1188"),
                ("Angebots-Nr.: ANG-2026-04711", "ANG-2026-04711")):
            with self.subTest(satz=satz):
                treffer = extract_header_fields(satz)
                self.assertIn("offer_number", treffer)
                self.assertEqual(treffer["offer_number"].value, erwartet)

    def test_58_kein_kopffeld_enthaelt_ein_fuellwort(self) -> None:
        """Systematisch: kein Kopfwert darf ein blosses Fuellwort sein."""
        fuellwoerter = {"lautet", "ist", "war", "folgende", "folgender", "nummer",
                        "nr", "no", "is", "reads", "sind", "waren"}
        saetze = (
            "Unsere Angebotsnummer lautet ANG-2026-04712",
            "Die Lieferantennummer ist 100234",
            "Ihre Kundennummer lautet 47110",
            "Angebotsdatum ist der 17.08.2026",
            "Zahlungsbedingungen sind 30 Tage netto",
            "Our offer number is Q-2026-8842",
            "Die Waehrung ist EUR",
        )
        for satz in saetze:
            with self.subTest(satz=satz):
                for feld, treffer in extract_header_fields(satz).items():
                    text = str(treffer.value).strip().lower()
                    self.assertNotIn(text, fuellwoerter,
                                     f"{feld} wurde zu '{treffer.value}' geraten")

    def test_59_ohne_erkennbaren_wert_bleibt_das_feld_leer(self) -> None:
        """Lieber leer als falsch."""
        treffer = extract_header_fields("Die Angebotsnummer teilen wir spaeter mit.")
        wert = treffer.get("offer_number")
        self.assertTrue(wert is None or any(c.isdigit() for c in str(wert.value)),
                        f"unplausibler Fund: {wert}")

    def test_60_belegnummer_braucht_ziffer_oder_bindestrich(self) -> None:
        treffer = extract_header_fields("Angebots-Nr.: AB-CDEF")
        self.assertEqual(treffer["offer_number"].value, "AB-CDEF")
        treffer = extract_header_fields("Angebots-Nr.: siehe")
        self.assertNotIn("offer_number", treffer)

    # -- uebrige Kopfregeln ------------------------------------------------
    def test_61_kopffelder_aus_fliesstext(self) -> None:
        text = (
            "Pumpen Weber GmbH & Co. KG\n"
            "Angebots-Nr.:  AG-2026-1188\n"
            "Datum:  17.08.2026\n"
            "Freibleibend gueltig bis:  16.10.2026\n"
            "Kundennummer:  47110\n"
            "Zahlungsziel:  60 Tage netto\n"
            "Lieferbedingung:  CPT Werk\n"
            "Waehrung:  EUR\n"
        )
        treffer = extract_header_fields(text)
        self.assertEqual(treffer["offer_number"].value, "AG-2026-1188")
        self.assertEqual(treffer["offer_date"].value, datetime.date(2026, 8, 17))
        self.assertEqual(treffer["valid_to"].value, datetime.date(2026, 10, 16))
        self.assertEqual(treffer["currency"].value, "EUR")
        self.assertEqual(treffer["incoterm"].value, "CPT")

    def test_62_incoterm_mit_ort(self) -> None:
        code, ort, konfidenz = find_incoterm("Incoterms 2020: FCA Bielefeld")
        self.assertEqual(code, "FCA")
        self.assertEqual(ort, "Bielefeld")
        self.assertGreater(konfidenz, 0.5)

    def test_63_lieferantenname_aus_signatur(self) -> None:
        name, konfidenz = vendor_name_from_signature(
            "Mit freundlichen Gruessen\nThomas Wagner\nVertrieb\n"
            "Muster Dichtungstechnik GmbH\n")
        self.assertEqual(name, "Muster Dichtungstechnik GmbH")
        self.assertGreater(konfidenz, 0.5)

    def test_64_kein_firmenname_aus_einer_datumszeile(self) -> None:
        name, _ = vendor_name_from_signature("gueltig ab 01.09.2026")
        self.assertEqual(name, "", "'AB' darf keinen Firmennamen erzeugen")

    def test_65_unsichere_treffer_werden_markiert(self) -> None:
        treffer = extract_header_fields("Bielefeld, den 17.08.2026")
        self.assertEqual(treffer["offer_date"].origin, FieldOrigin.UNCERTAIN)


# ==========================================================================
# 4. E-Mail und .msg
# ==========================================================================

class EmailTests(TempDirCase):
    """E-Mails: Freitext, HTML-Tabelle, Anhang, Signaturschnitt."""

    def _eml(self, name: str, body: str, html: str = "",
             attachment: tuple[str, bytes] | None = None) -> str:
        message = EmailMessage()
        message["From"] = "T. Wagner (Muster GmbH) <vertrieb@muster-dichtung.de>"
        message["To"] = "einkauf@technotrans.de"
        message["Subject"] = "Preisanpassung zum 01.09.2026"
        message["Date"] = "Mon, 17 Aug 2026 09:00:00 +0200"
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        if attachment:
            filename, data = attachment
            message.add_attachment(data, maintype="text", subtype="csv",
                                   filename=filename)
        return self.write(name, message.as_bytes())

    def test_70_eml_freitext(self) -> None:
        path = self._eml("preis.eml",
                         "Sehr geehrte Damen und Herren,\n\n"
                         "wir passen unsere Preise wie folgt an:\n"
                         "  - Artikel 47110001, Dichtring NBR: 12,85 EUR je Stueck\n"
                         "  - Artikel 47110002, O-Ring Viton: 8,90 EUR je Stueck\n\n"
                         "Unsere Angebotsnummer lautet ANG-2026-04712.\n")
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        self.assertIsNotNone(offer.email)
        self.assertEqual(offer.email.sender_domain, "muster-dichtung.de")
        self.assertGreaterEqual(len(offer.positions), 2)
        self.assertEqual(offer.offer_number, "ANG-2026-04712")

    def test_71_eml_angebotsnummer_ist_kein_fuellwort(self) -> None:
        path = self._eml("preis.eml",
                         "Unsere Angebotsnummer lautet ANG-2026-04712.\n"
                         "Artikel 47110001: 12,85 EUR je Stueck\n")
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        self.assertNotEqual(offer.offer_number, "lautet")
        self.assertEqual(offer.offer_number, "ANG-2026-04712")

    def test_72_html_tabelle_in_der_mail(self) -> None:
        html = ("<html><body><p>Unsere Preise:</p><table>"
                "<tr><th>Material</th><th>Bezeichnung</th><th>Menge</th>"
                "<th>Preis</th></tr>"
                "<tr><td>47110001</td><td>Dichtring NBR</td><td>500</td>"
                "<td>12,85</td></tr>"
                "<tr><td>47110002</td><td>O-Ring Viton</td><td>200</td>"
                "<td>8,90</td></tr>"
                "</table></body></html>")
        path = self._eml("html.eml", "Bitte die Tabelle beachten.", html=html)
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        materialien = {p.material_number for p in offer.positions}
        self.assertIn("47110001", materialien, offer.extraction_notes)

    def test_73_html_wird_zu_text_und_tabellen(self) -> None:
        text, tabellen = html_to_text(
            "<p>Hallo</p><table><tr><td>A</td><td>B</td></tr></table>")
        self.assertIn("Hallo", text)
        self.assertEqual(tabellen[0][0], ["A", "B"])

    def test_74_mail_mit_anhang(self) -> None:
        csv = ("Material;Bezeichnung;Menge;Preis\n"
               "47110001;Dichtring;500;12,85\n"
               "47110002;O-Ring;200;8,90\n").encode("utf-8")
        path = self._eml("anhang.eml", "Preise siehe Anhang.",
                         attachment=("preise.csv", csv))
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        self.assertGreaterEqual(len(offer.positions), 2)
        self.assertTrue(any(p.source_kind is SourceKind.EMAIL_ATTACHMENT
                            for p in offer.positions))

    def test_75_signatur_wird_abgeschnitten(self) -> None:
        text, signatur = strip_signature(
            "Wir passen die Preise an.\n"
            "Artikel 47110001: 12,85 EUR\n"
            "Die Lieferzeit betraegt 14 Tage.\n\n"
            "Mit freundlichen Gruessen\n"
            "Thomas Wagner\n"
            "Muster Dichtungstechnik GmbH\n"
            "Telefon 0521 555-120\n")
        self.assertIn("12,85", text)
        self.assertNotIn("Telefon", text)
        self.assertIn("Wagner", signatur)

    def test_76_signatur_bleibt_fuer_die_kopfdaten_nutzbar(self) -> None:
        path = self._eml("sig.eml",
                         "Artikel 47110001: 12,85 EUR je Stueck\n"
                         "Die Lieferzeit betraegt 14 Tage.\n\n"
                         "Mit freundlichen Gruessen\n"
                         "Thomas Wagner\n"
                         "Muster Dichtungstechnik GmbH\n")
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        self.assertTrue(offer.vendor_name, "Lieferant muss aus der Signatur kommen")

    def test_77_leere_mail_liefert_keine_position(self) -> None:
        path = self._eml("leer.eml", "Guten Tag,\n\nvielen Dank.\n")
        offer = OfferImportService(self.settings, InMemoryProfileStore()).import_file(path)
        self.assertEqual(len(offer.positions), 0)


class MsgTests(TempDirCase):
    """Outlook-.msg -- selbst erzeugte Minimaldatei und Schrottdatei."""

    def test_80_minimale_msg_wird_gelesen(self) -> None:
        data = _build_cfb([
            _unicode_prop(0x0037, "Preisanpassung zum 01.09.2026"),
            _unicode_prop(0x1000, "Artikel 47110001: 12,85 EUR je Stueck"),
            _unicode_prop(0x0C1A, "Thomas Wagner"),
        ])
        path = self.write("angebot.msg", data)
        msg = read_msg(path)
        self.assertTrue(msg.ok, f"nichts gelesen: {msg.errors}")
        self.assertEqual(msg.subject, "Preisanpassung zum 01.09.2026")
        self.assertIn("12,85", msg.body)

    def test_81_kaputte_msg_wirft_nicht(self) -> None:
        path = self.write("kaputt.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                          + b"\x00" * 100)
        try:
            msg = read_msg(path)
        except Exception as exc:  # noqa: BLE001 -- genau das darf nicht passieren
            self.fail(f"read_msg ist abgestuerzt: {type(exc).__name__}: {exc}")
        self.assertTrue(msg.errors, "eine kaputte Datei muss einen Hinweis liefern")

    def test_82_msg_ueber_den_importdienst(self) -> None:
        path = self.write("kaputt.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                          + b"\x00" * 100)
        service = OfferImportService(self.settings, InMemoryProfileStore())
        try:
            offer = service.import_file(path)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"Import einer kaputten .msg stuerzte ab: {exc}")
        self.assertEqual(len(offer.positions), 0)


# ==========================================================================
# 5. Profile und Lernen
# ==========================================================================

class ProfilLernenTests(TempDirCase):
    """Gelernt wird, WO ein Wert steht -- nie, WELCHER Wert es ist."""

    def _import(self, inhalt: str, store: InMemoryProfileStore
                ) -> tuple[OfferImportService, Offer]:
        path = self.write("preise.csv", inhalt.encode("utf-8"))
        service = OfferImportService(self.settings, store)
        return service, service.import_file(path)

    def test_90_profil_wird_ueber_die_domaene_gefunden(self) -> None:
        document = make_document([["Material", "Preis"], ["4711002", "12,85"]])
        document.email = EmailContext(from_address="e.muster@muster-dichtung.de")
        profile = VendorProfile(profile_id="p1", vendor_key="123456",
                                vendor_name="Muster GmbH",
                                email_domains=["muster-dichtung.de"])
        gefunden, score = match_profile([profile], document, Offer())
        self.assertIs(gefunden, profile)
        self.assertGreaterEqual(score, 0.55)

    def test_91_fremdes_profil_wird_nicht_genommen(self) -> None:
        document = make_document([["Material", "Preis"], ["4711002", "12,85"]])
        document.email = EmailContext(from_address="e.muster@muster-dichtung.de")
        fremd = VendorProfile(profile_id="p2", vendor_key="999",
                              email_domains=["andere.de"])
        gefunden, _ = match_profile([fremd], document, Offer())
        self.assertIsNone(gefunden)

    def test_92_dienst_lernt_und_speichert_das_profil(self) -> None:
        store = InMemoryProfileStore()
        service, offer = self._import(
            "Material;Bezeichnung;Menge;Listenpreis;Netto EUR\n"
            "4711002;Dichtring;100;15,00;12,85\n", store)
        self.assertIsNotNone(service.last_document())

        corrected = copy.deepcopy(offer)
        corrected.positions[0].set_field("price", Decimal("15.00"), FieldOrigin.MANUAL)
        profile = service.learn(offer, corrected)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.column_map.get("listenpreis"), "price")
        self.assertEqual(len(store.load_profiles()), 1,
                         "das uebergebene (leere) Profillager muss benutzt werden")

    def test_93_leeres_profillager_wird_nicht_ersetzt(self) -> None:
        store = InMemoryProfileStore()
        service = OfferImportService(self.settings, store)
        self.assertIs(service.profile_store, store)

    def test_94_korrigierte_spaltenrolle_wird_gelernt(self) -> None:
        """Der Anwender stellt richtig, welche Spalte unsere Nummer ist."""
        store = InMemoryProfileStore()
        service, offer = self._import(
            "Pos;Artikelnummer;Bezeichnung;Menge;Preis\n"
            "10;AB-4711;Dichtring;100;12,85\n", store)
        corrected = copy.deepcopy(offer)
        corrected.positions[0].set_field("vendor_material_number", "AB-4711",
                                         FieldOrigin.MANUAL)
        profile = service.learn(offer, corrected)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.material_column_role.get("artikelnummer"),
                         "vendor_material_number", describe_learning(profile))

    def test_95_gelernte_rolle_wirkt_beim_naechsten_angebot(self) -> None:
        profile = VendorProfile(profile_id="p1", vendor_key="123456")
        profile.material_column_role["artikelnummer"] = "vendor_material_number"
        rows = [["Pos", "Artikelnummer", "Bezeichnung", "Menge", "Preis"],
                ["10", "47110001", "Dichtring", "500", "12,85"]]
        position = first_position(rows, self.settings, profile)
        self.assertEqual(position.vendor_material_number, "47110001")
        self.assertEqual(position.material_number, "")

    def test_96_gelernte_rolle_laesst_sich_verwerfen(self) -> None:
        profile = VendorProfile(profile_id="p1", vendor_key="123456")
        profile.column_map["artikelnummer"] = "vendor_material_number"
        profile.material_column_role["artikelnummer"] = "vendor_material_number"
        self.assertTrue(forget_rule(
            profile, "column_map:artikelnummer=vendor_material_number"))
        self.assertNotIn("artikelnummer", profile.material_column_role)
        self.assertNotIn("artikelnummer", profile.column_map)

    def test_97_reset_verwirft_alles_gelernte(self) -> None:
        profile = VendorProfile(profile_id="p1")
        profile.column_map["netto eur"] = "price"
        profile.material_column_role["artikelnummer"] = "material_number"
        profile.decimal_style = "de"
        profile.reset_learning()
        self.assertEqual(profile.column_map, {})
        self.assertEqual(profile.material_column_role, {})
        self.assertEqual(profile.decimal_style, "auto")

    def test_98_gelerntes_ist_im_klartext_nachlesbar(self) -> None:
        profile = VendorProfile(profile_id="p1")
        profile.material_column_role["artikelnummer"] = "material_number"
        zeilen = " ".join(describe_learning(profile))
        self.assertIn("artikelnummer", zeilen)
        self.assertIn("unsere Materialnummer", zeilen)

    def test_99_gelernt_wird_nie_ein_wert(self) -> None:
        """Ein Profil darf keinen Preis und keine Bezeichnung enthalten."""
        store = InMemoryProfileStore()
        service, offer = self._import(
            "Material;Bezeichnung;Menge;Listenpreis;Netto EUR\n"
            "4711002;Dichtring;100;15,00;12,85\n", store)
        corrected = copy.deepcopy(offer)
        corrected.positions[0].set_field("price", Decimal("15.00"), FieldOrigin.MANUAL)
        profile = service.learn(offer, corrected)
        blob = str(profile.to_dict())
        for wert in ("15.00", "15,00", "12,85", "Dichtring"):
            self.assertNotIn(wert, blob, f"'{wert}' darf nicht im Profil stehen")


# ==========================================================================
# 6. Robustheit
# ==========================================================================

class RobustheitTests(TempDirCase):
    """Kaputte Eingaben duerfen nie eine Exception nach aussen geben."""

    def _pruefe(self, name: str, daten: bytes) -> Offer:
        path = self.write(name, daten)
        service = OfferImportService(self.settings, InMemoryProfileStore())
        try:
            offer = service.import_file(path)
        except Exception as exc:  # noqa: BLE001 -- genau das darf nicht passieren
            self.fail(f"{name} fuehrte zum Absturz: {type(exc).__name__}: {exc}")
        self.assertEqual(len(offer.positions), 0,
                         f"{name} lieferte erfundene Positionen")
        return offer

    def test_A1_leere_excel(self) -> None:
        self._pruefe("leer.xlsx", b"")

    def test_A2_kaputtes_pdf(self) -> None:
        self._pruefe("kaputt.pdf", b"%PDF-1.4\nJUNK JUNK JUNK")

    def test_A3_csv_nur_kopfzeile(self) -> None:
        self._pruefe("nur_kopf.csv", "Pos;Material;Preis\n".encode("utf-8"))

    def test_A4_kaputte_mail(self) -> None:
        self._pruefe("kaputt.eml", b"Das ist keine gueltige Mail\x00\xff")

    def test_A5_leerer_text(self) -> None:
        service = OfferImportService(self.settings, InMemoryProfileStore())
        for text in ("", "   ", "Hallo Welt"):
            with self.subTest(text=text):
                offer = service.import_text(text, "Fuzz")
                self.assertEqual(len(offer.positions), 0)

    def test_A6_tabelle_ohne_verwertbare_struktur(self) -> None:
        rows = [["A", "B", "C"], ["x", "y", "z"], ["1", "2", "3"]]
        result = TableExtractor(self.settings).extract(make_document(rows))
        self.assertEqual(len(result.positions), 0)
        self.assertTrue(result.notes)

    def test_A7_zeilen_ohne_preis_erzeugen_keine_position(self) -> None:
        extractor = FreetextExtractor(self.settings)
        result = extractor.extract_text("Artikel 47110001, Dichtring NBR 40x52x7")
        self.assertEqual(len(result.positions), 0,
                         "ohne Preis darf keine Position entstehen")

    def test_A8_datum_ohne_jahr_wird_nicht_geraten(self) -> None:
        extractor = FreetextExtractor(self.settings)
        result = extractor.extract_text(
            "Artikel 47110001: 12,85 EUR je Stueck ab 01.09.")
        for position in result.positions:
            self.assertIsNone(position.valid_from,
                              "ein fehlendes Jahr darf nicht ergaenzt werden")


# ==========================================================================
# 7. Die schwierigen Beispieldateien
# ==========================================================================

def _beispiele():
    """``sample_data/erzeuge_beispiele.py`` als Modul laden (kein Paket)."""
    import importlib.util

    pfad = Path(__file__).resolve().parent.parent / "sample_data" / "erzeuge_beispiele.py"
    spec = importlib.util.spec_from_file_location("erzeuge_beispiele", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


class SchwierigeBeispieleTests(TempDirCase):
    """Die harten Faelle aus ``sample_data`` -- ehrlich geprueft.

    Diese Tests halten fest, was die Erkennung bei realistisch schwierigen
    Angeboten wirklich leistet.  Sie behaupten bewusst *nicht*, dass alles
    perfekt erkannt wird -- sie sichern, dass nichts erfunden und nichts
    stillschweigend verschluckt wird.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.beispiele = _beispiele()

    def _erzeuge(self, funktion, name: str) -> str:
        pfad = Path(self.tmp) / name
        funktion(pfad)
        if not pfad.exists():
            self.skipTest(f"{name} konnte nicht erzeugt werden (fehlende Bibliothek)")
        return str(pfad)

    def _import(self, funktion, name: str) -> Offer:
        pfad = self._erzeuge(funktion, name)
        service = OfferImportService(self.settings, InMemoryProfileStore())
        return service.import_file(pfad)

    # -- 1. PDF im Fliesstextstil ---------------------------------------
    def test_B1_fliesstext_pdf_kopfdaten(self) -> None:
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        self.assertEqual(offer.offer_number, "AG-2026-3355")
        self.assertEqual(offer.currency, "EUR")
        self.assertIn("30 Tage netto", offer.payment_terms)

    def test_B2_fliesstext_pdf_position_aus_dem_satz(self) -> None:
        """"Fuer den Dichtring ..., Ihre Materialnummer 47110001, ... 12,85 EUR"."""
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        treffer = [p for p in offer.positions if p.material_number == "47110001"]
        self.assertEqual(len(treffer), 1, [p.raw_text for p in offer.positions])
        position = treffer[0]
        self.assertEqual(position.price, Decimal("12.85"))
        self.assertEqual(position.min_order_qty, Decimal("50"))
        self.assertEqual(position.lead_time_days, 14)
        self.assertIn("Dichtring", position.description)

    def test_B3_fliesstext_pdf_auf_anfrage_erzeugt_keinen_preis(self) -> None:
        """Ohne Betrag darf keine Position mit erfundenem Preis entstehen."""
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        for position in offer.positions:
            if position.material_number == "48200111":
                self.assertIsNone(position.price)

    def test_B4_fliesstext_werte_sind_als_unsicher_gekennzeichnet(self) -> None:
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        self.assertTrue(offer.positions)
        for position in offer.positions:
            self.assertIn("price", position.uncertain_fields,
                          "aus Fliesstext abgeleitete Preise sind zu pruefen")

    # -- 2. Excel mit quer verteilten Kopfdaten -------------------------
    def test_B5_kopfdaten_quer_verteilt(self) -> None:
        offer = self._import(self.beispiele.excel_kopfdaten_quer, "quer.xlsx")
        self.assertEqual(offer.offer_number, "ANG-2026-7788",
                         "Angebotsnummer steht ganz rechts in H2")
        self.assertEqual(offer.offer_date, datetime.date.today(),
                         "Datum steht UNTER der Tabelle")
        self.assertEqual(offer.currency, "EUR", "Waehrung nur in der Fusszeile")

    def test_B6_kopfzeile_erst_in_zeile_12(self) -> None:
        offer = self._import(self.beispiele.excel_kopfdaten_quer, "quer.xlsx")
        self.assertEqual(len(offer.positions), 2, offer.extraction_notes)
        self.assertEqual(offer.positions[0].material_number, "47110001")

    def test_B7_umlaute_bleiben_erhalten(self) -> None:
        offer = self._import(self.beispiele.excel_kopfdaten_quer, "quer.xlsx")
        texte = " ".join(p.description for p in offer.positions)
        self.assertIn("Öldichtring", texte)
        self.assertIn("Meßstab", texte)

    def test_B8_mengeneinheiten_werden_normalisiert(self) -> None:
        """'Stk.' und 'Meter' muessen zu SAP-Einheiten werden."""
        offer = self._import(self.beispiele.excel_kopfdaten_quer, "quer.xlsx")
        einheiten = [p.uom for p in offer.positions]
        self.assertIn("ST", einheiten)
        self.assertIn("M", einheiten)

    # -- 3. Staffelpreise ueber zwei Seiten -----------------------------
    def test_B9_staffel_pdf_positionen_beider_seiten(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffel.pdf")
        nummern = {p.material_number for p in offer.positions}
        self.assertIn("47110005", nummern, "Seite 1")
        self.assertIn("47110004", nummern, "Seite 2")

    def test_B10_staffelstufen_werden_eigene_positionen(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffel.pdf")
        staffeln = [p for p in offer.positions if "Staffel" in p.remarks]
        self.assertGreaterEqual(len(staffeln), 3,
                                "Staffeln duerfen nicht stillschweigend "
                                "zusammengefasst werden")
        preise = {p.price for p in offer.positions if p.material_number == "47110005"}
        self.assertIn(Decimal("4.35"), preise)
        self.assertIn(Decimal("4.10"), preise)

    def test_B11_uebertrag_und_summe_sind_keine_positionen(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffel.pdf")
        texte = " ".join(p.description.lower() + p.raw_text.lower()
                         for p in offer.positions)
        self.assertNotIn("gesamtsumme", texte)
        for position in offer.positions:
            self.assertNotEqual(position.price, Decimal("15402.50"))

    # -- 4. Vertauschte Spalten, englische Ueberschriften ---------------
    def test_B12_englische_spalten_werden_richtig_zugeordnet(self) -> None:
        """'Part No' ist SEINE Nummer, 'Customer Material' UNSERE."""
        offer = self._import(self.beispiele.excel_vertauschte_spalten,
                             "vertauscht.xlsx")
        position = offer.positions[0]
        self.assertEqual(position.material_number, "47110001")
        self.assertEqual(position.vendor_material_number, "DR-40527-NBR")

    def test_B13_gemischte_zahlenformate_in_einer_datei(self) -> None:
        """1,234.56 (englisch) und 1.234,56 (deutsch) meinen dasselbe."""
        offer = self._import(self.beispiele.excel_vertauschte_spalten,
                             "vertauscht.xlsx")
        mengen = [p.quantity for p in offer.positions[:2]]
        self.assertEqual(mengen, [Decimal("1234.56"), Decimal("1234.56")])

    def test_B14_auf_anfrage_bleibt_ohne_preis(self) -> None:
        offer = self._import(self.beispiele.excel_vertauschte_spalten,
                             "vertauscht.xlsx")
        position = [p for p in offer.positions
                    if p.material_number == "48200111"][0]
        self.assertIsNone(position.price, "'auf Anfrage' darf keinen Preis erfinden")
        self.assertIs(position.origin("price"), FieldOrigin.MISSING)

    def test_B15_englische_einheiten_werden_normalisiert(self) -> None:
        offer = self._import(self.beispiele.excel_vertauschte_spalten,
                             "vertauscht.xlsx")
        einheiten = [p.uom for p in offer.positions]
        self.assertEqual(einheiten.count("ST"), 3, einheiten)
        self.assertIn("M", einheiten)

    # -- 5. Alle Beispieldateien entstehen ------------------------------
    def test_B16_alle_beispieldateien_werden_erzeugt(self) -> None:
        ziel = Path(self.tmp) / "erzeugt"
        ziel.mkdir()
        original = self.beispiele.ZIEL
        try:
            self.beispiele.ZIEL = ziel
            self.beispiele.main()
        finally:
            self.beispiele.ZIEL = original
        erwartet = (
            "Angebot_Muster_Dichtungstechnik.xlsx",
            "Preisliste_ohne_Kopfzeile.xlsx",
            "Quotation_Nordtec_mit_Stoerfaellen.xlsx",
            "Preisanpassung_Muster.eml",
            "Preismitteilung.txt",
            "Preisliste_Muster.csv",
            "Angebot_Nordtec_mit_Anhang.eml",
            "Angebot_Kopfdaten_quer_verteilt.xlsx",
            "Mail_mit_Ergaenzungen_im_Text.eml",
            "Quotation_vertauschte_Spalten.xlsx",
        )
        fehlend = [name for name in erwartet if not (ziel / name).exists()]
        self.assertEqual(fehlend, [], f"nicht erzeugt: {fehlend}")


class KreuzpruefungAufBeispielenTests(TempDirCase):
    """Schlagen die Kreuzpruefungen auf den echten Beispieldateien an?

    Wichtiger noch als das Anschlagen ist das *Schweigen*: eine Warnung, die
    ohne Anlass kommt, kostet den Einkaeufer genauso viel Zeit wie ein
    uebersehener Lesefehler.  Deshalb steht hier beides nebeneinander.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.beispiele = _beispiele()

    def _erzeuge(self, funktion, name: str) -> str:
        pfad = Path(self.tmp) / name
        funktion(pfad)
        if not pfad.exists():
            self.skipTest(f"{name} konnte nicht erzeugt werden (fehlende Bibliothek)")
        return str(pfad)

    def _import(self, funktion, name: str) -> Offer:
        pfad = self._erzeuge(funktion, name)
        return OfferImportService(self.settings, InMemoryProfileStore()).import_file(pfad)

    @staticmethod
    def _codes(offer: Offer) -> set[str]:
        codes = {issue.code for issue in offer.issues}
        for position in offer.positions:
            codes |= {issue.code for issue in position.issues}
        return codes

    # -- Staffeln ueber zwei Seiten -------------------------------------
    def test_B17_uebertragszeile_wird_keine_position(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffeln.pdf")
        self.assertTrue(any("Uebertrag" in note for note in offer.extraction_notes),
                        offer.extraction_notes)
        for position in offer.positions:
            self.assertNotIn("Uebertrag", position.raw_text)

    def test_B18_belegsumme_deckt_die_abweichung_auf(self) -> None:
        """Die Gesamtsumme des Belegs passt nicht zu den Positionen.

        Das ist genau der Fall, den die Pruefung finden soll -- ob nun eine
        Position fehlt oder der Beleg selbst nicht stimmt, entscheidet der
        Anwender.
        """
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffeln.pdf")
        self.assertIn(CODE_DOCUMENT_TOTAL_MISMATCH, self._codes(offer))
        treffer = [n for n in offer.extraction_notes if "Differenz" in n]
        self.assertTrue(treffer, offer.extraction_notes)
        self.assertIn("15.402,50", treffer[0])

    def test_B19_ohne_gesamtspalte_keine_zeilenpruefung(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffeln.pdf")
        self.assertNotIn(CODE_LINE_TOTAL_MISMATCH, self._codes(offer))
        for position in offer.positions:
            self.assertIsNone(position.line_total)

    def test_B20_staffelstufen_bleiben_zu_pruefen(self) -> None:
        offer = self._import(self.beispiele.pdf_staffelpreise_zwei_seiten,
                             "staffeln.pdf")
        staffeln = [p for p in offer.positions if "Staffel" in p.remarks]
        self.assertTrue(staffeln)
        for position in staffeln:
            self.assertNotEqual(position.confidence_label(), "sicher")

    # -- PDF im Fliesstextstil ------------------------------------------
    def test_B21_fliesstext_loest_keine_falschmeldung_aus(self) -> None:
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        for code in (CODE_LINE_TOTAL_MISMATCH, CODE_DOCUMENT_TOTAL_MISMATCH,
                     CODE_POSITION_GAP, CODE_PRICE_OUTLIER):
            self.assertNotIn(code, self._codes(offer))

    def test_B22_fliesstext_hat_die_niedrigste_konfidenz(self) -> None:
        offer = self._import(self.beispiele.pdf_fliesstext, "fliesstext.pdf")
        self.assertTrue(offer.positions)
        for position in offer.positions:
            self.assertLess(position.confidence, 0.5)
            self.assertEqual(position.confidence_label(), "unsicher")
            self.assertTrue(position.confidence_reasons)

    # -- Vertauschte Spalten --------------------------------------------
    def test_B23_vertauschte_spalten_ohne_falschmeldung(self) -> None:
        offer = self._import(self.beispiele.excel_vertauschte_spalten,
                             "vertauscht.xlsx")
        for code in (CODE_LINE_TOTAL_MISMATCH, CODE_DOCUMENT_TOTAL_MISMATCH,
                     CODE_POSITION_GAP, CODE_PRICE_OUTLIER):
            self.assertNotIn(code, self._codes(offer))

    def test_B24_saubere_tabelle_ist_sicher(self) -> None:
        offer = self._import(self.beispiele.excel_mit_kopfzeile, "muster.xlsx")
        self.assertTrue(offer.positions)
        for position in offer.positions:
            self.assertEqual(position.confidence_label(), "sicher")
        self.assertEqual(self._codes(offer), set())

    def test_B25_lueckenlose_nummernfolge_bleibt_still(self) -> None:
        offer = self._import(self.beispiele.excel_mit_kopfzeile, "muster.xlsx")
        nummern = [p.position_number for p in offer.positions]
        self.assertEqual(nummern, ["10", "20", "30", "40", "50"])
        self.assertNotIn(CODE_POSITION_GAP, self._codes(offer))


if __name__ == "__main__":
    unittest.main(verbosity=2)
