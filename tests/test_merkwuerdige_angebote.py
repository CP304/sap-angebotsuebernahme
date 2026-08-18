"""Tests fuer die "merkwuerdigen" Angebote -- die Sonderfaelle des Alltags.

Je Fall mindestens vier Tests: Gutfall, Grenzfall, darf-nicht-anschlagen und
das Zusammenspiel ueber den kompletten Import (Beispieldatei aus
``sample_data/erzeuge_beispiele.py``).

Abgedeckt:
 1. Mengenstaffel als Matrix ("ab 100 | ab 500 | ab 1000" als Spalten)
 2. Zwischenueberschriften (Artikelgruppen-Zeilen) in Preislisten
 3. Preisangaben im Fusstext (Waehrung, Preiseinheit je 100 Stueck)
 4. Verbundene Zellen (Excel merged cells, Word vMerge)
 5. HTML-Mails mit Angebotstabelle direkt im Body (inkl. colspan)
 6. Quer gedrehte PDF-Seiten (rotation 90/270)
 7. Englische/gemischte Belege mit mehrdeutigem Datumsformat
 8. "auf Anfrage"-Positionen inkl. Teilpruefung der Belegsumme
 9. Zweispaltige PDF-Layouts (zwei Angebotsbloecke nebeneinander)
10. Preisspannen-Zeilen ohne Kopfzeile (fruehere bekannte Schwaeche)
"""

from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_merkwuerdig_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import FieldOrigin                                # noqa: E402
from app.services.extraction.price_parsing import (                     # noqa: E402
    footer_price_unit,
    is_on_request,
)
from app.services.extraction.table_extractor import TableExtractor      # noqa: E402
from app.services.offer_import_service import OfferImportService        # noqa: E402
from app.services.readers.base import RawDocument, TableBlock           # noqa: E402
from app.services.readers.email_reader import html_to_text              # noqa: E402
from app.services.readers.office_reader import WordReader               # noqa: E402
from app.services.readers.pdf_reader import find_column_split, make_word  # noqa: E402


def _beispiele():
    """``sample_data/erzeuge_beispiele.py`` als Modul laden (kein Paket)."""
    import importlib.util

    pfad = Path(__file__).resolve().parent.parent / "sample_data" / "erzeuge_beispiele.py"
    spec = importlib.util.spec_from_file_location("erzeuge_beispiele_mw", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _extract(rows: list[list[str]], **block_kwargs):
    """Tabellenzeilen durch den TableExtractor schicken."""
    extractor = TableExtractor(Settings())
    document = RawDocument()
    document.tables.append(TableBlock(rows=rows, **block_kwargs))
    return extractor.extract(document)


def _import(path: Path):
    service = OfferImportService(Settings())
    return service.import_file(str(path))


class TempSampleCase(unittest.TestCase):
    """Basisklasse: erzeugt Beispieldateien in ein Temp-Verzeichnis."""

    generator = None

    @classmethod
    def setUpClass(cls):
        cls.generator = _beispiele()
        cls.tempdir = Path(tempfile.mkdtemp(prefix="sap_mw_samples_"))

    def sample(self, name: str, funktion_name: str) -> Path:
        ziel = self.tempdir / name
        if not ziel.exists():
            getattr(self.generator, funktion_name)(ziel)
        self.assertTrue(ziel.exists(), f"Beispieldatei {name} wurde nicht erzeugt")
        return ziel


# ==========================================================================
# 1. Mengenstaffel als Matrix
# ==========================================================================

class StaffelMatrixTest(TempSampleCase):
    ROWS = [
        ["Material", "Bezeichnung", "ab 100", "ab 500", "ab 1000"],
        ["47110001", "Dichtring NBR 40x52x7", "13,20", "12,85", "12,40"],
        ["47110002", "O-Ring Viton 25x3", "0,95", "0,89", "0,84"],
    ]

    def test_matrix_wird_zu_staffelpositionen(self):
        result = _extract(self.ROWS)
        erste = [p for p in result.positions if p.material_number == "47110001"]
        self.assertEqual(len(erste), 3)
        self.assertEqual(erste[0].price, Decimal("13.20"))
        self.assertEqual(erste[0].min_order_qty, Decimal("100"))
        self.assertEqual(erste[1].price, Decimal("12.85"))
        self.assertEqual(erste[1].min_order_qty, Decimal("500"))
        self.assertIn("Staffelpreis", erste[1].remarks)
        self.assertEqual(erste[2].price, Decimal("12.40"))

    def test_leere_matrixzelle_erzeugt_keine_stufe(self):
        rows = [self.ROWS[0],
                ["47110001", "Dichtring NBR 40x52x7", "13,20", "", "12,40"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)   # nur 2 Stufen mit Preis
        mengen = {p.min_order_qty for p in result.positions}
        self.assertEqual(mengen, {Decimal("100"), Decimal("1000")})

    def test_einzelne_ab_spalte_ist_keine_matrix(self):
        rows = [["Material", "Bezeichnung", "Menge", "Preis", "ab 100"],
                ["47110001", "Dichtring", "500", "12,85", "x"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 1)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Angebot_Staffel_Matrix.xlsx",
                                    "excel_staffel_matrix"))
        self.assertEqual(len(offer.positions), 9)   # 3 Materialien x 3 Stufen
        self.assertEqual(offer.currency, "EUR")
        staffeln = [p for p in offer.positions if "Staffelpreis" in (p.remarks or "")]
        self.assertEqual(len(staffeln), 6)


# ==========================================================================
# 2. Zwischenueberschriften
# ==========================================================================

class ZwischenueberschriftenTest(TempSampleCase):
    def test_gruppenzeile_wird_uebersprungen(self):
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"],
                ["Dichtungen:", "", "", "", "", ""],
                ["10", "47110001", "Dichtring NBR", "500", "St", "12,85"],
                ["20", "47110002", "O-Ring Viton", "200", "St", "8,90"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)
        self.assertTrue(any("Zwischenueberschrift" in n for n in result.notes))

    def test_dekorierte_gruppenzeile(self):
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"],
                ["10", "47110001", "Dichtring NBR", "500", "St", "12,85"],
                ["", "", "-- Gruppe B: Lager --", "", "", ""],
                ["20", "47110005", "Kugellager 6204", "1000", "St", "4,55"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)
        # Die Gruppenzeile darf NICHT an die Vorposition angehaengt worden sein
        self.assertNotIn("Gruppe B", result.positions[0].description)

    def test_fortsetzungszeile_bleibt_fortsetzung(self):
        # Text in der Beschreibungsspalte ohne Dekoration ist KEINE Gruppe,
        # sondern gehoert zur Vorposition.
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"],
                ["10", "47110001", "Sonderdichtung", "20", "St", "145,00"],
                ["", "", "nach Zeichnung gefertigt", "", "", ""]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 1)
        self.assertIn("nach Zeichnung gefertigt", result.positions[0].description)

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Preisliste_Zwischenueberschriften.csv",
                                    "csv_zwischenueberschriften"))
        self.assertEqual(len(offer.positions), 3)
        beschreibungen = " | ".join(p.description for p in offer.positions)
        self.assertNotIn("Gruppe B", beschreibungen)
        self.assertNotIn("Dichtungen:", beschreibungen)


# ==========================================================================
# 3. Preisangaben im Fusstext
# ==========================================================================

class FusstextTest(TempSampleCase):
    def test_footer_price_unit_deutsch(self):
        self.assertEqual(footer_price_unit("Preise je 100 Stueck."), 100)
        self.assertEqual(footer_price_unit("Alle Preise pro 1000 Stk"), 1000)

    def test_footer_price_unit_englisch(self):
        self.assertEqual(footer_price_unit("All prices per 100 pcs."), 100)

    def test_footer_price_unit_schlaegt_nicht_falsch_an(self):
        self.assertIsNone(footer_price_unit("Preise je Stueck"))
        self.assertIsNone(footer_price_unit("Preise je 1 Stueck"))
        self.assertIsNone(footer_price_unit("Die Lieferzeit betraegt 14 Tage."))

    def test_eigene_preiseinheit_hat_vorrang(self):
        service = OfferImportService(Settings())
        text = ("Pos;Material;Bezeichnung;Menge;ME;Preis;PE\n"
                "10;47110001;Dichtring;500;St;12,85;10\n"
                "\nPreise je 100 Stueck.\n")
        offer = service.import_text(text)
        self.assertEqual(len(offer.positions), 1)
        self.assertEqual(offer.positions[0].price_unit, 10)

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Angebot_Fusstext_Preisangaben.txt",
                                    "text_fusstext_preisangaben"))
        self.assertEqual(len(offer.positions), 2)
        self.assertEqual(offer.currency, "EUR")
        for position in offer.positions:
            self.assertEqual(position.price_unit, 100)
            self.assertIs(position.origin("price_unit"), FieldOrigin.EXTRACTED)
        self.assertTrue(any("Fusstext" in n for n in offer.extraction_notes))


# ==========================================================================
# 4. Verbundene Zellen (Excel/Word)
# ==========================================================================

class VerbundeneZellenTest(TempSampleCase):
    ROWS = [["Materialnummer", "Bezeichnung", "Menge", "ME", "Preis"],
            ["47110001", "Dichtring NBR", "500", "St", "12,85"],
            ["47110001", "Dichtring FKM", "500", "St", "14,20"]]

    def test_verbundene_materialnummer_wird_unsicher(self):
        result = _extract(self.ROWS, merged_cells={(2, 0)})
        self.assertEqual(len(result.positions), 2)
        self.assertEqual(result.positions[1].material_number, "47110001")
        self.assertIs(result.positions[1].origin("material_number"),
                      FieldOrigin.UNCERTAIN)
        # Die Ursprungszeile des Verbunds bleibt eine normale Erkennung
        self.assertIsNot(result.positions[0].origin("material_number"),
                         FieldOrigin.UNCERTAIN)

    def test_echte_leere_materialnummer_bleibt_leer(self):
        rows = [self.ROWS[0], self.ROWS[1],
                ["", "Sonderteil nach Zeichnung", "20", "St", "145,00"]]
        result = _extract(rows)     # keine merged_cells!
        self.assertEqual(result.positions[1].material_number, "")

    def test_word_leser_liest_vmerge(self):
        pfad = self.sample("Angebot_verbundene_Zellen.docx", "word_verbundene_zellen")
        document = WordReader().read(str(pfad))
        self.assertEqual(len(document.tables), 1)
        block = document.tables[0]
        self.assertTrue(block.merged_cells)
        # Die Folgezeilen tragen die uebernommene Materialnummer
        self.assertEqual(block.rows[2][0], "47110001")
        self.assertEqual(block.rows[3][0], "47110001")

    def test_normalized_erhaelt_verbundkoordinaten(self):
        block = TableBlock(rows=[["", "A", "B"], ["", "A", "B"]],
                           merged_cells={(1, 1)})
        normalized = block.normalized()
        # Spalte 0 ist leer und faellt weg -- die Koordinate wandert mit
        self.assertEqual(normalized.merged_cells, {(1, 0)})

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Angebot_verbundene_Zellen.docx",
                                    "word_verbundene_zellen"))
        self.assertEqual(len(offer.positions), 5)
        varianten = [p for p in offer.positions
                     if p.origin("material_number") is FieldOrigin.UNCERTAIN]
        self.assertEqual(len(varianten), 3)
        self.assertTrue(all(p.material_number for p in offer.positions))


# ==========================================================================
# 5. HTML-Mails mit Tabelle im Body
# ==========================================================================

class HtmlMailTest(TempSampleCase):
    def test_html_tabelle_wird_struktur(self):
        text, tables = html_to_text(
            "<table><tr><th>Material</th><th>Preis</th></tr>"
            "<tr><td>47110001</td><td>12,85</td></tr></table>")
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0][1], ["47110001", "12,85"])

    def test_colspan_verschiebt_keine_spalten(self):
        _, tables = html_to_text(
            '<table><tr><th colspan="2">Artikel</th><th>Preis</th></tr>'
            "<tr><td>47110001</td><td>Dichtring</td><td>12,85</td></tr></table>")
        self.assertEqual(len(tables[0][0]), 3)      # Kopf ist aufgefuellt
        self.assertEqual(tables[0][0][2], "Preis")

    def test_html_ohne_tabelle_liefert_keine(self):
        _, tables = html_to_text("<p>Nur Fliesstext, kein Angebot.</p>")
        self.assertEqual(tables, [])

    def test_kaputtes_html_wirft_nicht(self):
        text, tables = html_to_text(
            "<table><tr><td>47110001<td>12,85</tr><tr><td>x")
        self.assertIsInstance(text, str)            # kein Absturz

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Angebot_HTML_Tabelle_im_Body.eml",
                                    "mail_html_tabelle"))
        self.assertEqual(len(offer.positions), 2)
        materialien = {p.material_number for p in offer.positions}
        self.assertEqual(materialien, {"47110001", "47110005"})
        self.assertEqual(offer.positions[0].price, Decimal("12.85"))


# ==========================================================================
# 6. Quer gedrehte PDF-Seiten
# ==========================================================================

class GedrehteSeitenTest(TempSampleCase):
    def test_gedrehte_seite_liefert_positionen(self):
        offer = _import(self.sample("Angebot_gedrehte_Seite.pdf",
                                    "pdf_gedrehte_seite"))
        self.assertEqual(len(offer.positions), 2)
        materialien = {p.material_number for p in offer.positions}
        self.assertEqual(materialien, {"48200110", "48200111"})

    def test_drehung_wird_gemeldet(self):
        pfad = self.sample("Angebot_gedrehte_Seite.pdf", "pdf_gedrehte_seite")
        from app.services.readers.pdf_reader import PdfReader
        document = PdfReader().read(str(pfad))
        self.assertEqual(document.meta.get("rotated_pages"), [1])
        self.assertTrue(any("gedreht" in w for w in document.warnings))

    def test_ungedrehte_seite_meldet_nichts(self):
        pfad = self.sample("Angebot_Pumpen_Weber.pdf", "pdf_angebot")
        from app.services.readers.pdf_reader import PdfReader
        document = PdfReader().read(str(pfad))
        self.assertNotIn("rotated_pages", document.meta)
        self.assertFalse(any("gedreht" in w for w in document.warnings))

    def test_180_grad_wird_ebenfalls_gemeldet(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF nicht installiert")
        pfad = self.tempdir / "gedreht_180.pdf"
        dokument = fitz.open()
        seite = dokument.new_page()
        seite.insert_text((50, 60), "Material  Preis", fontsize=10)
        seite.insert_text((50, 80), "47110001  12,85", fontsize=10)
        seite.set_rotation(180)
        dokument.save(pfad)
        dokument.close()
        from app.services.readers.pdf_reader import PdfReader
        document = PdfReader().read(str(pfad))
        self.assertEqual(document.meta.get("rotated_pages"), [1])


# ==========================================================================
# 7. Englische und gemischte Belege
# ==========================================================================

class EnglischGemischtTest(TempSampleCase):
    def test_mehrdeutiges_slash_datum_wird_unsicher(self):
        rows = [["Item", "Material", "Description", "Qty", "Unit price", "Valid from"],
                ["10", "47110001", "Sealing ring", "500", "12.85", "03/04/2026"],
                ["20", "47110005", "Ball bearing", "1000", "4.55", "05/06/2026"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)
        for position in result.positions:
            self.assertIs(position.origin("valid_from"), FieldOrigin.UNCERTAIN)
        self.assertTrue(any("mehrdeutig" in n for n in result.notes))

    def test_beweisbares_datum_bleibt_sicher(self):
        rows = [["Item", "Material", "Description", "Qty", "Unit price", "Valid from"],
                ["10", "47110001", "Sealing ring", "500", "12.85", "13/04/2026"],
                ["20", "47110005", "Ball bearing", "1000", "4.55", "05/06/2026"]]
        result = _extract(rows)
        # 13/04 beweist Tag-zuerst -- die Spalte ist nicht mehr mehrdeutig
        self.assertEqual(result.positions[0].valid_from,
                         datetime.date(2026, 4, 13))
        self.assertIsNot(result.positions[0].origin("valid_from"),
                         FieldOrigin.UNCERTAIN)

    def test_deutsches_punktdatum_bleibt_sicher(self):
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "Preis", "Gueltig ab"],
                ["10", "47110001", "Dichtring", "500", "12,85", "01.09.2026"],
                ["20", "47110005", "Kugellager", "1000", "4,55", "01.09.2026"]]
        result = _extract(rows)
        self.assertEqual(result.positions[0].valid_from,
                         datetime.date(2026, 9, 1))
        self.assertIsNot(result.positions[0].origin("valid_from"),
                         FieldOrigin.UNCERTAIN)

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Quotation_englisch_gemischt.csv",
                                    "csv_englisch_gemischt"))
        self.assertEqual(len(offer.positions), 2)
        self.assertEqual(offer.positions[0].price, Decimal("12.85"))
        self.assertEqual(offer.positions[1].quantity, Decimal("1000"))
        for position in offer.positions:
            self.assertIs(position.origin("valid_from"), FieldOrigin.UNCERTAIN)


# ==========================================================================
# 8. "auf Anfrage"-Positionen
# ==========================================================================

class AufAnfrageTest(TempSampleCase):
    def test_is_on_request_varianten(self):
        for text in ("auf Anfrage", "Preis auf Anfrage", "a.A.", "a. A.",
                     "on request", "upon request", "P.O.A.",
                     "nach Vereinbarung"):
            self.assertTrue(is_on_request(text), text)

    def test_is_on_request_schlaegt_nicht_falsch_an(self):
        for text in ("4,55", "12,85 EUR", "Anfrage vom 12.05.",
                     "auf Anfrage senden wir Muster zu", ""):
            self.assertFalse(is_on_request(text), text)

    def test_position_bleibt_mit_befund_erhalten(self):
        rows = [["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"],
                ["10", "47110001", "Dichtring", "500", "St", "12,85"],
                ["20", "48200111", "Gleitringdichtung", "40", "St", "auf Anfrage"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)
        anfrage = result.positions[1]
        self.assertIsNone(anfrage.price)
        self.assertIn("price_on_request", [i.code for i in anfrage.issues.items])
        self.assertIn("Preis auf Anfrage", anfrage.remarks)

    def test_zusammenspiel_teilpruefung_der_belegsumme(self):
        offer = _import(self.sample("Angebot_auf_Anfrage_mit_Summe.csv",
                                    "csv_auf_anfrage_mit_summe"))
        self.assertEqual(len(offer.positions), 3)
        anfrage = [p for p in offer.positions if p.price is None]
        self.assertEqual(len(anfrage), 1)
        # Die uebrigen Positionen wurden trotzdem gegen die Summe gehalten
        self.assertTrue(any("teilweise geprueft" in n
                            for n in offer.extraction_notes))
        self.assertNotIn("document_total_mismatch",
                         [i.code for i in offer.issues.items])


# ==========================================================================
# 9. Zweispaltige PDF-Layouts
# ==========================================================================

def _wortblock(x0: float, zeilen: list[tuple[str, ...]], y0: float = 100.0):
    woerter = []
    for zeilen_index, zeile in enumerate(zeilen):
        y = y0 + zeilen_index * 16
        x = x0
        for text in zeile:
            breite = 8.0 * max(len(text), 1)
            woerter.append(make_word(x, y, x + breite, y + 10, text))
            x += breite + 12
    return woerter


class ZweispaltigTest(TempSampleCase):
    _BLOCK = [("Pos", "Material", "Menge", "Preis"),
              ("10", "47110001", "500", "12,85"),
              ("20", "47110002", "200", "8,90"),
              ("30", "47110003", "100", "18,95")]

    def test_zwei_bloecke_werden_erkannt(self):
        woerter = _wortblock(50, self._BLOCK) + _wortblock(400, self._BLOCK)
        split = find_column_split(woerter)
        self.assertIsNotNone(split)
        self.assertTrue(300 < split < 420)

    def test_normale_tabelle_wird_nicht_zerschnitten(self):
        zeilen = [("Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis"),
                  ("10", "47110001", "Dichtring", "500", "St", "12,85"),
                  ("20", "47110002", "O-Ring", "200", "St", "8,90"),
                  ("30", "47110003", "Wellendichtring", "100", "St", "18,95")]
        self.assertIsNone(find_column_split(_wortblock(50, zeilen)))

    def test_zu_wenige_woerter_kein_split(self):
        woerter = _wortblock(50, [("Pos", "Preis")]) + _wortblock(400, [("Pos", "Preis")])
        self.assertIsNone(find_column_split(woerter))

    def test_zusammenspiel_beispieldatei(self):
        pfad = self.sample("Angebot_zweispaltig.pdf", "pdf_zweispaltig")
        offer = _import(pfad)
        self.assertEqual(len(offer.positions), 8)
        warnungen = [str(i.message) for i in offer.issues.items]
        self.assertTrue(any("zweispaltig" in w for w in warnungen))


# ==========================================================================
# 10. Preisspannen ohne Kopfzeile
# ==========================================================================

class PreisspanneOhneKopfTest(TempSampleCase):
    def test_spannenzeile_ohne_kopf_bleibt_erhalten(self):
        rows = [["47110001", "Dichtring NBR 40x52x7", "12,00 - 14,00 EUR"],
                ["47110002", "O-Ring Viton 25x3", "8,50 - 9,20 EUR"],
                ["47110005", "Kugellager 6204-2RS", "4,55 EUR"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 3)
        spannen = [p for p in result.positions if p.price is None]
        self.assertEqual(len(spannen), 2)
        for position in spannen:
            self.assertIn("price_range", [i.code for i in position.issues.items])
            self.assertIs(position.origin("price"), FieldOrigin.UNCERTAIN)

    def test_spannenzeile_mit_kopf_bekommt_befund(self):
        rows = [["Material", "Bezeichnung", "Preis"],
                ["47110001", "Dichtring NBR", "12,00 - 14,00 EUR"],
                ["47110005", "Kugellager", "4,55"]]
        result = _extract(rows)
        self.assertEqual(len(result.positions), 2)
        self.assertIsNone(result.positions[0].price)
        self.assertIn("price_range",
                      [i.code for i in result.positions[0].issues.items])
        self.assertEqual(result.positions[1].price, Decimal("4.55"))

    def test_abmessung_ist_keine_preisspanne(self):
        rows = [["Material", "Bezeichnung", "Preis"],
                ["47110001", "Dichtring 40-52-7", "12,85"],
                ["47110005", "Kugellager 6204", "4,55"]]
        result = _extract(rows)
        self.assertEqual(result.positions[0].price, Decimal("12.85"))
        self.assertNotIn("price_range",
                         [i.code for i in result.positions[0].issues.items])

    def test_zusammenspiel_beispieldatei(self):
        offer = _import(self.sample("Preisspannen_ohne_Kopfzeile.txt",
                                    "text_preisspanne_ohne_kopf"))
        self.assertEqual(len(offer.positions), 3)
        spannen = [p for p in offer.positions
                   if "price_range" in [i.code for i in p.issues.items]]
        self.assertEqual(len(spannen), 2)
        mit_preis = [p for p in offer.positions if p.price is not None]
        self.assertEqual(len(mit_preis), 1)
        self.assertEqual(mit_preis[0].price, Decimal("4.55"))


if __name__ == "__main__":
    unittest.main()
