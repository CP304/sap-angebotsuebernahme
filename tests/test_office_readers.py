"""Tests der Textverarbeitungsformate: Word, OpenDocument, RTF.

Alle drei werden mit Bordmitteln gelesen (ZIP + XML bzw. Steuerwortanalyse).
Geprueft wird nicht nur, dass Text herauskommt, sondern dass die
Angebotserkennung darauf dasselbe leistet wie bei Excel: Kopfdaten, Positionen,
normalisierte Einheiten, richtige Spaltenrollen -- und dass beschaedigte
Dateien nie eine Ausnahme nach aussen durchlassen.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_office_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                          # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402
from app.services.readers import ReaderRegistry                   # noqa: E402
from app.services.readers.office_reader import (                  # noqa: E402
    OpenDocumentReader,
    RichTextReader,
    WordReader,
)

WURZEL = Path(__file__).resolve().parent.parent
BEISPIELE = WURZEL / "sample_data" / "erzeugt"
sys.path.insert(0, str(WURZEL / "sample_data"))


def _erzeuge(name: str, funktion_name: str) -> Path:
    """Beispieldatei ueber den Generator erzeugen (nicht duplizieren)."""
    import erzeuge_beispiele as generator

    ziel = Path(tempfile.mkdtemp(prefix="sap_office_datei_")) / name
    getattr(generator, funktion_name)(ziel)
    return ziel


class WordLeserTest(unittest.TestCase):
    """.docx -- das mit Abstand haeufigste Textverarbeitungsformat."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.datei = _erzeuge("angebot.docx", "word_angebot")
        cls.dokument = WordReader().read(str(cls.datei))

    def test_endungen(self) -> None:
        leser = WordReader()
        self.assertTrue(leser.can_read("x.docx"))
        self.assertTrue(leser.can_read("X.DOCX"))
        self.assertFalse(leser.can_read("x.doc"))

    def test_text_wird_gelesen(self) -> None:
        self.assertIn("Schmidt", self.dokument.text)
        self.assertIn("ANG-2026-7788", self.dokument.text)

    def test_tabelle_wird_erkannt(self) -> None:
        self.assertEqual(len(self.dokument.tables), 1)
        tabelle = self.dokument.tables[0]
        self.assertEqual(tabelle.origin, "word-table")
        self.assertGreaterEqual(len(tabelle.rows), 5)

    def test_tabellenzeilen_sind_gleich_breit(self) -> None:
        tabelle = self.dokument.tables[0]
        breiten = {len(zeile) for zeile in tabelle.rows}
        self.assertEqual(len(breiten), 1, f"unterschiedliche Zeilenbreiten: {breiten}")

    def test_keine_warnung_bei_gueltiger_datei(self) -> None:
        self.assertEqual(self.dokument.warnings, [])


class WordErkennungTest(unittest.TestCase):
    """Die ganze Kette: Word-Datei -> fertige Angebotspositionen."""

    @classmethod
    def setUpClass(cls) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        cls.datei = _erzeuge("angebot.docx", "word_angebot")
        cls.angebot = OfferImportService(einstellungen).import_file(str(cls.datei))

    def test_kopfdaten(self) -> None:
        self.assertIn("Schmidt", self.angebot.vendor_name)
        self.assertEqual(self.angebot.offer_number, "ANG-2026-7788")
        self.assertEqual(self.angebot.currency, "EUR")
        self.assertIsNotNone(self.angebot.offer_date)

    def test_zahlungsbedingungen_aus_dem_fliesstext(self) -> None:
        self.assertIn("30", self.angebot.payment_terms)

    def test_positionen(self) -> None:
        materialien = [p.material_number for p in self.angebot.positions]
        for erwartet in ("47110001", "47110005", "49900010"):
            self.assertIn(erwartet, materialien)

    def test_ihre_artikelnummer_ist_unser_material(self) -> None:
        """'Ihre Artikelnummer' = unsere Nummer, 'Unsere Art.-Nr.' = deren."""
        position = next(p for p in self.angebot.positions
                        if p.material_number == "47110001")
        self.assertEqual(position.vendor_material_number, "SP-DR-4052")

    def test_einheiten_werden_normalisiert(self) -> None:
        einheiten = {p.material_number: p.uom for p in self.angebot.positions}
        self.assertEqual(einheiten.get("47110001"), "ST")     # aus "Stk."
        self.assertEqual(einheiten.get("49900010"), "M")      # aus "Meter"

    def test_deutsche_zahlen(self) -> None:
        position = next(p for p in self.angebot.positions
                        if p.material_number == "47110005")
        self.assertEqual(position.quantity, Decimal("1000"))  # aus "1.000"
        self.assertEqual(position.price, Decimal("4.45"))

    def test_auf_anfrage_erzeugt_keinen_preis(self) -> None:
        """Grundsatz: lieber leer als geraten."""
        treffer = [p for p in self.angebot.positions if p.material_number == "47110004"]
        for position in treffer:
            self.assertIsNone(position.price)


class OpenDocumentTest(unittest.TestCase):
    """.odt aus LibreOffice."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.datei = _erzeuge("angebot.odt", "opendocument_angebot")
        cls.dokument = OpenDocumentReader().read(str(cls.datei))

    def test_tabelle_wird_erkannt(self) -> None:
        self.assertTrue(self.dokument.tables)
        self.assertEqual(self.dokument.tables[0].origin, "odt-table")

    def test_text_wird_gelesen(self) -> None:
        self.assertIn("Q-2026-9911", self.dokument.text)

    def test_erkennung_liefert_positionen(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        angebot = OfferImportService(einstellungen).import_file(str(self.datei))
        materialien = [p.material_number for p in angebot.positions]
        self.assertIn("47110005", materialien)

    def test_englische_zahlen(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        angebot = OfferImportService(einstellungen).import_file(str(self.datei))
        position = next((p for p in angebot.positions
                         if p.material_number == "47110005"), None)
        self.assertIsNotNone(position)
        self.assertEqual(position.price, Decimal("4.55"))     # aus "4.55"
        self.assertEqual(position.quantity, Decimal("1000"))  # aus "1,000"


class RichTextTest(unittest.TestCase):
    """.rtf -- selten, aber es kommt vor."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.datei = _erzeuge("angebot.rtf", "rtf_angebot")
        cls.dokument = RichTextReader().read(str(cls.datei))

    def test_steuerworte_verschwinden(self) -> None:
        self.assertNotIn("\\par", self.dokument.text)
        self.assertNotIn("fonttbl", self.dokument.text)

    def test_text_bleibt_erhalten(self) -> None:
        self.assertIn("AG-2026-4455", self.dokument.text)
        self.assertIn("Pumpen Weber", self.dokument.text)

    def test_tabellenzeilen_werden_rekonstruiert(self) -> None:
        self.assertTrue(self.dokument.tables)
        zeilen = self.dokument.tables[0].rows
        self.assertGreaterEqual(len(zeilen), 3)
        inhalt = " ".join(" ".join(z) for z in zeilen)
        self.assertIn("48200110", inhalt)

    def test_erkennung_liefert_positionen(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        angebot = OfferImportService(einstellungen).import_file(str(self.datei))
        materialien = [p.material_number for p in angebot.positions]
        self.assertIn("48200110", materialien)


class RobustheitTest(unittest.TestCase):
    """Kaputte Dateien duerfen nie eine Ausnahme durchlassen."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ordner = Path(tempfile.mkdtemp(prefix="sap_office_kaputt_"))

    def _pruefe(self, name: str, daten: bytes, leser) -> None:
        pfad = self.ordner / name
        pfad.write_bytes(daten)
        try:
            dokument = leser.read(str(pfad))
        except Exception as fehler:  # noqa: BLE001 - genau das darf nicht passieren
            self.fail(f"{name}: {type(fehler).__name__}: {fehler}")
        self.assertTrue(dokument.warnings, f"{name} lieferte keine Warnung")
        self.assertEqual(dokument.tables, [])

    def test_altes_doc_format(self) -> None:
        """.doc ist ein Binaerformat -- klare Meldung statt Ratespiel."""
        self._pruefe("alt.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
                     WordReader())

    def test_leere_datei(self) -> None:
        self._pruefe("leer.docx", b"", WordReader())

    def test_zip_ohne_document_xml(self) -> None:
        pfad = self.ordner / "falsch.docx"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("egal.txt", "kein Word")
        dokument = WordReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_kaputtes_xml(self) -> None:
        pfad = self.ordner / "kaputt.docx"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("word/document.xml", "<w:document><nicht geschlossen")
        dokument = WordReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_odt_ohne_content(self) -> None:
        pfad = self.ordner / "leer.odt"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        dokument = OpenDocumentReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_rtf_ohne_kennung(self) -> None:
        self._pruefe("falsch.rtf", b"Das ist kein RTF", RichTextReader())

    def test_fehlende_datei(self) -> None:
        dokument = RichTextReader().read(str(self.ordner / "gibtsnicht.rtf"))
        self.assertTrue(dokument.warnings)


class RegistrierungTest(unittest.TestCase):
    """Die neuen Formate muessen im Leserverzeichnis auftauchen."""

    def setUp(self) -> None:
        self.registry = ReaderRegistry()

    def test_endungen_sind_bekannt(self) -> None:
        endungen = self.registry.supported_extensions()
        for endung in (".docx", ".odt", ".rtf"):
            self.assertIn(endung, endungen)

    def test_richtiger_leser_wird_gewaehlt(self) -> None:
        self.assertIsInstance(self.registry.reader_for("x.docx"), WordReader)
        self.assertIsInstance(self.registry.reader_for("x.odt"), OpenDocumentReader)
        self.assertIsInstance(self.registry.reader_for("x.rtf"), RichTextReader)

    def test_textleser_faengt_rtf_nicht_ab(self) -> None:
        """Reihenfolge im Verzeichnis: RTF vor dem allgemeinen Textleser."""
        leser = self.registry.reader_for("angebot.rtf")
        self.assertIsInstance(leser, RichTextReader)

    def test_unbekannte_endung(self) -> None:
        self.assertIsNone(self.registry.reader_for("x.xyz"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
