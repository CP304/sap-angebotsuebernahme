"""Tests der Texterkennung fuer gescannte Belege.

Die eigentlichen Engines (Windows-Bordmittel, Tesseract) sind auf einem
Entwickler- oder Testrechner haeufig gar nicht installiert.  Die Tests duerfen
deshalb nicht davon abhaengen, dass sie da sind -- sie muessen trotzdem etwas
aussagen.  Zwei Wege werden gegangen:

1. Ein :class:`FakeBackend` liefert kontrollierte Ergebnisse (samt niedriger
   Konfidenzen und typischer OCR-Verwechslungen).  Damit laeuft die *ganze*
   Kette vom Bild ueber die Erkennung bis zum ``TableBlock`` unter Kontrolle.
2. Die echten Backends werden nur auf ihre Verfuegbarkeitsmeldung geprueft:
   Sie duerfen "nicht verfuegbar" sagen -- sie duerfen dabei aber nicht
   abstuerzen und muessen erklaeren, was zu installieren waere.

Der Grundsatz, der hier abgesichert wird: Aus OCR stammende Werte sind
durchgaengig als unsicher gekennzeichnet, und es wird nie ein ``O`` in eine
``0`` "korrigiert" -- der Verdacht wird gemeldet.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_ocr_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import OcrSettings, Settings           # noqa: E402
from app.services.ocr import (                                  # noqa: E402
    NO_BACKEND_HINT,
    OcrResult,
    OcrWord,
    TesseractOcrBackend,
    WindowsOcrBackend,
    available_backends,
    backend_by_name,
    best_backend,
    cell_confidences,
    confidence_percent,
    ocr_settings_of,
    ocr_status_text,
    ocr_warning,
    suspicious_number,
    uncertain_cells,
)
from app.services.ocr.base import mean_of                       # noqa: E402
from app.services.ocr.tesseract_ocr import (                    # noqa: E402
    _confidence,
    _words_from_data,
    tesseract_language,
)
from app.services.readers import ReaderRegistry                 # noqa: E402
from app.services.readers.image_reader import (                 # noqa: E402
    IMAGE_EXTENSIONS,
    ImageReader,
)
from app.services.readers.pdf_reader import (                   # noqa: E402
    OCR_ASK_HINT,
    OCR_ORIGIN,
    SCAN_WARNING,
    SCAN_WARNING_WITH_HINT,
    PdfReader,
    ocr_words_to_words,
    render_page_png,
)

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover -- PyMuPDF ist Pflichtabhaengigkeit
    fitz = None


# ---------------------------------------------------------------------------
# Hilfsmittel
# ---------------------------------------------------------------------------

#: Spaltenpositionen in Bildpixeln (bei 300 dpi ergibt das brauchbare Abstaende)
_COLUMN_X = (100.0, 500.0, 900.0)
_ROW_Y = (100.0, 220.0, 340.0)
_WORD_WIDTH = 260.0
_WORD_HEIGHT = 40.0


def fake_words(rows: list[list[str]],
               confidences: dict[str, float] | None = None) -> list[OcrWord]:
    """Aus einer Tabellenvorlage Woerter mit Koordinaten bauen.

    Die Koordinaten sind Bildpixel -- genau das, was ein echtes Backend
    liefert.  So laeuft im Test dieselbe Umrechnung wie im Betrieb.
    """
    confidences = confidences or {}
    words: list[OcrWord] = []
    for row_index, row in enumerate(rows):
        y0 = _ROW_Y[row_index] if row_index < len(_ROW_Y) else 100.0 + row_index * 120.0
        for column_index, cell in enumerate(row):
            text = (cell or "").strip()
            if not text:
                continue
            x0 = (_COLUMN_X[column_index] if column_index < len(_COLUMN_X)
                  else 100.0 + column_index * 400.0)
            words.append(OcrWord(text=text, x0=x0, y0=y0,
                                 x1=x0 + _WORD_WIDTH, y1=y0 + _WORD_HEIGHT,
                                 confidence=confidences.get(text)))
    return words


class FakeBackend:
    """Kontrolliertes Backend fuer die Tests."""

    name = "fake"
    label = "Test-Backend"
    install_hint = "nichts zu tun -- nur fuer Tests"

    def __init__(self, rows: list[list[str]] | None = None,
                 confidences: dict[str, float] | None = None,
                 available: bool = True, text: str | None = None,
                 raises: bool = False) -> None:
        self.rows = rows if rows is not None else [
            ["Pos", "Bezeichnung", "Preis"],
            ["10", "Dichtring", "12,50"],
            ["20", "Kugellager", "89,00"],
        ]
        self.confidences = confidences or {}
        self.available = available
        self.raises = raises
        self._text = text
        #: Protokoll aller Aufrufe: (Bildgroesse in Bytes, Sprache)
        self.calls: list[tuple[int, str]] = []
        self.images: list[bytes] = []

    def is_available(self) -> bool:
        return self.available

    def recognize(self, image_bytes: bytes, language: str = "de") -> OcrResult:
        self.calls.append((len(image_bytes or b""), language))
        self.images.append(image_bytes)
        if self.raises:
            raise RuntimeError("Backend absichtlich kaputt")
        words = fake_words(self.rows, self.confidences)
        text = self._text if self._text is not None else "\n".join(
            " ".join(cell for cell in row if cell) for row in self.rows)
        result = OcrResult(text=text, words=words, backend_name=self.name)
        result.recompute_mean()
        return result


def ocr_settings(**overrides) -> OcrSettings:
    """OCR-Einstellungen fuer Tests: ohne Rueckfrage, kleine Aufloesung."""
    values = {"ask_before_ocr": False, "dpi": 150}
    values.update(overrides)
    settings = OcrSettings()
    for key, value in values.items():
        setattr(settings, key, value)
    return settings


def _text_pixmap(text: str = "Angebot Position 10 Dichtring 12,50 EUR",
                 dpi: int = 110):
    """Eine Seite mit Text rendern und als Pixmap zurueckgeben."""
    helper = fitz.open()
    page = helper.new_page(width=595, height=842)
    for index, line in enumerate(text.split("\n")):
        page.insert_text((60, 100 + index * 30), line, fontsize=14)
    pixmap = page.get_pixmap(dpi=dpi)
    helper.close()
    return pixmap


def make_image_pdf(path: Path, pages: int = 1) -> Path:
    """Ein PDF *ohne* Textebene erzeugen: gerenderte Bilder als Seiten."""
    pixmap = _text_pixmap()
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pixmap)
    document.save(str(path))
    document.close()
    return path


def make_mixed_pdf(path: Path) -> Path:
    """Seite 1 mit echter Textebene, Seite 2 als Bild (Scan)."""
    pixmap = _text_pixmap()
    document = fitz.open()
    first = document.new_page(width=595, height=842)
    for index in range(6):
        first.insert_text((60, 100 + index * 24),
                          f"Zeile {index}: Angebot ueber Ersatzteile, Seite eins.",
                          fontsize=12)
    second = document.new_page(width=595, height=842)
    second.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pixmap)
    document.save(str(path))
    document.close()
    return path


def make_image_file(path: Path) -> Path:
    """Eine echte Bilddatei (PNG) mit gerendertem Text schreiben."""
    pixmap = _text_pixmap()
    pixmap.save(str(path))
    return path


# ---------------------------------------------------------------------------
# 1. Datenstrukturen
# ---------------------------------------------------------------------------

class OcrDataStructureTests(unittest.TestCase):
    """Die Bausteine muessen "unbekannt" von "schlecht" unterscheiden."""

    def test_word_ohne_konfidenz_gilt_nicht_als_niedrig(self):
        word = OcrWord(text="12,50", confidence=None)
        self.assertFalse(word.is_low_confidence)
        self.assertFalse(word.below(0.9))

    def test_word_mit_niedriger_konfidenz(self):
        word = OcrWord(text="12,50", confidence=0.4)
        self.assertTrue(word.below(0.6))
        self.assertTrue(word.is_low_confidence)

    def test_mean_of_ignoriert_unbekannte_werte(self):
        self.assertAlmostEqual(mean_of([0.4, None, 0.6]), 0.5)

    def test_mean_of_ohne_werte_ist_none(self):
        self.assertIsNone(mean_of([None, None]))

    def test_result_recompute_mean(self):
        result = OcrResult(words=[OcrWord("a", confidence=0.8),
                                  OcrWord("b", confidence=0.6)])
        result.recompute_mean()
        self.assertAlmostEqual(result.mean_confidence, 0.7)

    def test_result_ohne_konfidenz_meldet_none(self):
        result = OcrResult(words=[OcrWord("a"), OcrWord("b")])
        result.recompute_mean()
        self.assertIsNone(result.mean_confidence)
        self.assertFalse(result.has_confidence)

    def test_warnungen_ohne_dubletten(self):
        result = OcrResult()
        result.add_warning("Hinweis")
        result.add_warning("Hinweis")
        self.assertEqual(result.warnings, ["Hinweis"])

    def test_low_confidence_words(self):
        result = OcrResult(words=[OcrWord("a", confidence=0.2),
                                  OcrWord("b", confidence=0.9),
                                  OcrWord("c", confidence=None)])
        self.assertEqual([w.text for w in result.low_confidence_words(0.6)], ["a"])


# ---------------------------------------------------------------------------
# 2. Ehrlichkeit: Konfidenz und Ziffernverwechslung
# ---------------------------------------------------------------------------

class QualityTests(unittest.TestCase):
    """Der wichtigste Teil: OCR liefert immer etwas -- was davon ist gut?"""

    def test_verwechselte_null_wird_gemeldet(self):
        note = suspicious_number("1.2O0")
        self.assertTrue(note)
        self.assertIn("O", note)
        self.assertIn("NICHT automatisch", note)

    def test_verwechselte_eins_wird_gemeldet(self):
        self.assertTrue(suspicious_number("l2,50"))

    def test_verwechselte_fuenf_und_acht(self):
        self.assertTrue(suspicious_number("S8"))
        self.assertTrue(suspicious_number("B9,00"))

    def test_verwechseltes_g_wird_gemeldet(self):
        self.assertTrue(suspicious_number("G4"))

    def test_echter_text_wird_nicht_gemeldet(self):
        for text in ("Dichtring", "Schraube M8", "Kugellager 6203", "Stueck"):
            with self.subTest(text=text):
                self.assertEqual(suspicious_number(text), "")

    def test_saubere_zahl_wird_nicht_gemeldet(self):
        for text in ("12,50", "1.200", "89,00", "-5,00"):
            with self.subTest(text=text):
                self.assertEqual(suspicious_number(text), "")

    def test_einzelnes_zeichen_loest_nichts_aus(self):
        self.assertEqual(suspicious_number("S"), "")
        self.assertEqual(suspicious_number("O"), "")

    def test_es_wird_nichts_ersetzt(self):
        """Der Text bleibt unveraendert -- gemeldet wird nur der Verdacht."""
        original = "1.2O0"
        suspicious_number(original)
        self.assertEqual(original, "1.2O0")

    def test_konfidenzmatrix_nimmt_den_schlechtesten_wert(self):
        words = [OcrWord("Dicht", confidence=0.9), OcrWord("ring", confidence=0.3)]
        matrix = cell_confidences([["Dicht ring"]], words)
        self.assertAlmostEqual(matrix[0][0], 0.3)

    def test_konfidenzmatrix_unbekannt_bleibt_none(self):
        matrix = cell_confidences([["Unbekannt"]], [OcrWord("Anderes", confidence=0.9)])
        self.assertIsNone(matrix[0][0])

    def test_konfidenzmatrix_hat_dieselbe_form(self):
        rows = [["a", "b", "c"], ["d", "e", "f"]]
        matrix = cell_confidences(rows, [])
        self.assertEqual([len(r) for r in matrix], [3, 3])

    def test_uncertain_cells_findet_niedrige_konfidenz(self):
        rows = [["Dichtring", "12,50"]]
        matrix = [[0.95, 0.30]]
        findings = uncertain_cells(rows, matrix, 0.6, page=2)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["reason"], "konfidenz")
        self.assertEqual(findings[0]["column"], 1)
        self.assertEqual(findings[0]["page"], 2)

    def test_uncertain_cells_findet_ziffernverdacht(self):
        findings = uncertain_cells([["1.2O0"]], [[0.99]], 0.6)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["reason"], "ziffern")

    def test_uncertain_cells_ignoriert_leere_zellen(self):
        self.assertEqual(uncertain_cells([["", "  "]], [[None, None]], 0.6), [])

    def test_confidence_percent_ist_ehrlich(self):
        self.assertEqual(confidence_percent(None), "unbekannt")
        self.assertEqual(confidence_percent(0.87), "87 %")

    def test_ocr_warnung_nennt_pruefpflicht(self):
        text = ocr_warning(0.87, "tesseract", 3)
        self.assertIn("Texterkennung", text)
        self.assertIn("87 %", text)
        self.assertIn("bitte alle Werte pruefen", text)

    def test_ocr_warnung_ohne_konfidenz(self):
        self.assertIn("unbekannt", ocr_warning(None, "windows"))


# ---------------------------------------------------------------------------
# 3. Verfuegbarkeit der echten Backends
# ---------------------------------------------------------------------------

class BackendAvailabilityTests(unittest.TestCase):
    """Fehlende Zusatzpakete duerfen niemals einen Absturz ausloesen."""

    def test_windows_backend_meldet_sauber(self):
        backend = WindowsOcrBackend()
        self.assertIsInstance(backend.is_available(), bool)

    def test_tesseract_backend_meldet_sauber(self):
        backend = TesseractOcrBackend()
        self.assertIsInstance(backend.is_available(), bool)

    def test_backends_ohne_installation_liefern_warnung_statt_ausnahme(self):
        for backend in (WindowsOcrBackend(), TesseractOcrBackend()):
            with self.subTest(backend=backend.name):
                if backend.is_available():
                    continue
                result = backend.recognize(b"nicht wirklich ein bild", "de")
                self.assertEqual(result.words, [])
                self.assertTrue(result.warnings)

    def test_leeres_bild_wird_gemeldet(self):
        for backend in (WindowsOcrBackend(), TesseractOcrBackend()):
            with self.subTest(backend=backend.name):
                result = backend.recognize(b"", "de")
                self.assertTrue(result.warnings)

    def test_installationshinweis_nennt_beide_wege(self):
        self.assertIn("winsdk", NO_BACKEND_HINT)
        self.assertIn("pytesseract", NO_BACKEND_HINT)
        self.assertIn("Programm", NO_BACKEND_HINT)

    def test_status_text_ist_verstaendlich(self):
        text = ocr_status_text(Settings())
        self.assertTrue(text.strip())
        if not available_backends(Settings()):
            self.assertIn("keine Engine installiert", text)

    def test_status_text_bei_abgeschalteter_ocr(self):
        text = ocr_status_text(ocr_settings(enabled=False))
        self.assertIn("abgeschaltet", text)

    def test_backend_by_name(self):
        self.assertIsInstance(backend_by_name("windows"), WindowsOcrBackend)
        self.assertIsInstance(backend_by_name("tesseract"), TesseractOcrBackend)
        self.assertIsNone(backend_by_name("gibtsnicht"))

    def test_best_backend_ist_none_wenn_abgeschaltet(self):
        self.assertIsNone(best_backend(ocr_settings(enabled=False)))

    def test_unbekannte_reihenfolge_faellt_auf_standard_zurueck(self):
        settings = ocr_settings(backend_order=["quatsch"])
        # darf nicht werfen und muss trotzdem die bekannten Backends kennen
        self.assertIsInstance(available_backends(settings), list)

    def test_ocr_settings_of_akzeptiert_verschiedene_quellen(self):
        self.assertIsInstance(ocr_settings_of(None), OcrSettings)
        self.assertIsInstance(ocr_settings_of(Settings()), OcrSettings)
        eigen = OcrSettings()
        self.assertIs(ocr_settings_of(eigen), eigen)

    def test_tesseract_sprachcodes(self):
        self.assertEqual(tesseract_language("de"), "deu")
        self.assertEqual(tesseract_language("en"), "eng")
        self.assertEqual(tesseract_language("deu+eng"), "deu+eng")
        self.assertEqual(tesseract_language(""), "deu")

    def test_tesseract_konfidenz_minus_eins_ist_unbekannt(self):
        self.assertIsNone(_confidence([-1], 0))
        self.assertAlmostEqual(_confidence([87], 0), 0.87)
        self.assertIsNone(_confidence([], 0))

    def test_tesseract_worte_aus_datenwoerterbuch(self):
        data = {
            "text": ["", "Dichtring", "12,50"],
            "left": [0, 10, 200], "top": [0, 20, 20],
            "width": [0, 90, 40], "height": [0, 15, 15],
            "conf": [-1, 95, 42],
        }
        words = _words_from_data(data)
        self.assertEqual([w.text for w in words], ["Dichtring", "12,50"])
        self.assertAlmostEqual(words[1].confidence, 0.42)


# ---------------------------------------------------------------------------
# 4. Einstellungen
# ---------------------------------------------------------------------------

class OcrSettingsTests(unittest.TestCase):

    def test_standardwerte(self):
        settings = OcrSettings()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.ask_before_ocr)
        self.assertEqual(settings.backend_order, ["windows", "tesseract"])
        self.assertEqual(settings.language, "de")
        self.assertEqual(settings.dpi, 300)
        self.assertAlmostEqual(settings.min_confidence, 0.60)
        self.assertEqual(settings.max_pages, 20)
        self.assertTrue(settings.preprocess)

    def test_settings_enthaelt_ocr_bereich(self):
        self.assertIsInstance(Settings().ocr, OcrSettings)

    def test_ocr_einstellungen_ueberleben_speichern_und_laden(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "settings.json"
            settings = Settings()
            settings.ocr.dpi = 250
            settings.ocr.enabled = False
            settings.ocr.backend_order = ["tesseract"]
            settings.save(target)
            geladen = Settings.load(target)
        self.assertEqual(geladen.ocr.dpi, 250)
        self.assertFalse(geladen.ocr.enabled)
        self.assertEqual(geladen.ocr.backend_order, ["tesseract"])


# ---------------------------------------------------------------------------
# 5. PDF-Leser mit Texterkennung
# ---------------------------------------------------------------------------

@unittest.skipIf(fitz is None, "PyMuPDF nicht installiert")
class PdfOcrTests(unittest.TestCase):
    """Die ganze Kette: Bild-PDF -> Rendern -> Erkennen -> TableBlock."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="sap_ocr_pdf_")

    def _pdf(self, name: str = "scan.pdf", pages: int = 1) -> str:
        return str(make_image_pdf(Path(self.folder) / name, pages=pages))

    # -- ohne Backend ---------------------------------------------------
    def test_scan_ohne_backend_behaelt_meldung_mit_hinweis(self):
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=None)
        if reader.resolve_ocr_backend() is not None:
            self.skipTest("Auf diesem Rechner ist eine echte OCR installiert")
        document = reader.read(self._pdf())
        warnungen = "\n".join(document.warnings)
        self.assertIn("keinen durchsuchbaren Text", warnungen)
        self.assertIn("winsdk", warnungen)
        self.assertTrue(document.meta.get("scanned"))

    def test_hinweistext_enthaelt_ursprungsmeldung(self):
        self.assertIn("keinen durchsuchbaren Text", SCAN_WARNING)
        self.assertIn("keinen durchsuchbaren Text", SCAN_WARNING_WITH_HINT)

    # -- mit Backend ----------------------------------------------------
    def test_ocr_erzeugt_block_mit_origin_pdf_ocr(self):
        backend = FakeBackend()
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        origins = [t.origin for t in document.tables]
        self.assertIn(OCR_ORIGIN, origins)

    def test_ocr_setzt_deutliche_warnung(self):
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(self._pdf())
        self.assertTrue(any("Texterkennung" in w and "pruefen" in w
                            for w in document.warnings))

    def test_ocr_uebernimmt_den_text_der_seite(self):
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(self._pdf())
        self.assertIn("Dichtring", document.text)

    def test_meta_haelt_backend_und_konfidenz_fest(self):
        backend = FakeBackend(confidences={"12,50": 0.9, "Dichtring": 0.8})
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        meta = document.meta.get("ocr") or {}
        self.assertEqual(meta.get("backend"), "fake")
        self.assertEqual(len(meta.get("pages") or []), 1)
        self.assertIn("mean_confidence", meta["pages"][0])

    def test_meta_zaehlt_woerter_unter_der_schwelle(self):
        backend = FakeBackend(confidences={"12,50": 0.2, "89,00": 0.1,
                                           "Dichtring": 0.95})
        reader = PdfReader(ocr_settings=ocr_settings(min_confidence=0.6),
                           ocr_backend=backend)
        document = reader.read(self._pdf())
        self.assertEqual(document.meta["ocr"]["pages"][0]["low_confidence_words"], 2)

    def test_unsichere_zellen_stehen_in_meta_und_als_warnung(self):
        backend = FakeBackend(confidences={"12,50": 0.2})
        reader = PdfReader(ocr_settings=ocr_settings(min_confidence=0.6),
                           ocr_backend=backend)
        document = reader.read(self._pdf())
        unsicher = document.meta["ocr"]["uncertain_cells"]
        self.assertTrue(any(f["text"] == "12,50" for f in unsicher))
        self.assertTrue(any("Unsicher erkannt" in w for w in document.warnings))

    def test_konfidenzmatrix_liegt_parallel_zum_block(self):
        backend = FakeBackend(confidences={"Dichtring": 0.55})
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        eintraege = document.meta["ocr"]["cell_confidence"]
        self.assertTrue(eintraege)
        block = [t for t in document.tables if t.origin == OCR_ORIGIN][0]
        matrix = eintraege[0]["rows"]
        self.assertEqual(len(matrix), len(block.rows))

    def test_ziffernverwechslung_schlaegt_an(self):
        backend = FakeBackend(rows=[
            ["Pos", "Bezeichnung", "Preis"],
            ["10", "Dichtring", "1.2O0"],
            ["20", "Kugellager", "89,00"],
        ])
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        unsicher = document.meta["ocr"]["uncertain_cells"]
        self.assertTrue(any(f["reason"] == "ziffern" for f in unsicher))

    def test_ziffernverwechslung_wird_nicht_korrigiert(self):
        backend = FakeBackend(rows=[
            ["Pos", "Bezeichnung", "Preis"],
            ["10", "Dichtring", "1.2O0"],
            ["20", "Kugellager", "89,00"],
        ])
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        block = [t for t in document.tables if t.origin == OCR_ORIGIN][0]
        self.assertIn("1.2O0", block.as_text())
        self.assertNotIn("1.200", block.as_text())

    # -- Steuerung ------------------------------------------------------
    def test_ocr_abgeschaltet_ergibt_alte_meldung(self):
        reader = PdfReader(ocr_settings=ocr_settings(enabled=False),
                           ocr_backend=FakeBackend())
        document = reader.read(self._pdf())
        self.assertEqual([t for t in document.tables if t.origin == OCR_ORIGIN], [])
        self.assertTrue(any("keinen durchsuchbaren Text" in w
                            for w in document.warnings))

    def test_rueckfrage_verhindert_ocr(self):
        backend = FakeBackend()
        reader = PdfReader(ocr_settings=ocr_settings(ask_before_ocr=True),
                           ocr_backend=backend)
        document = reader.read(self._pdf())
        self.assertEqual(backend.calls, [])
        self.assertIn(OCR_ASK_HINT, document.warnings)

    def test_bestaetigung_startet_ocr_trotz_rueckfrage(self):
        backend = FakeBackend()
        reader = PdfReader(ocr_settings=ocr_settings(ask_before_ocr=True),
                           ocr_backend=backend, ocr_confirmed=True)
        reader.read(self._pdf())
        self.assertEqual(len(backend.calls), 1)

    def test_max_pages_begrenzt_die_erkennung(self):
        backend = FakeBackend()
        reader = PdfReader(ocr_settings=ocr_settings(max_pages=2),
                           ocr_backend=backend)
        document = reader.read(self._pdf("viele.pdf", pages=4))
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue(any("max_pages" in w for w in document.warnings))

    def test_dpi_wird_durchgereicht(self):
        klein = FakeBackend()
        gross = FakeBackend()
        PdfReader(ocr_settings=ocr_settings(dpi=100), ocr_backend=klein).read(self._pdf())
        PdfReader(ocr_settings=ocr_settings(dpi=200), ocr_backend=gross).read(self._pdf())
        self.assertGreater(gross.calls[0][0], klein.calls[0][0])

    def test_sprache_wird_durchgereicht(self):
        backend = FakeBackend()
        PdfReader(ocr_settings=ocr_settings(language="en"),
                  ocr_backend=backend).read(self._pdf())
        self.assertEqual(backend.calls[0][1], "en")

    def test_backend_ausnahme_wird_zur_warnung(self):
        backend = FakeBackend(raises=True)
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        self.assertTrue(any("fehlgeschlagen" in w for w in document.warnings))

    def test_backend_ohne_treffer_meldet_das(self):
        backend = FakeBackend(rows=[], text="")
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(self._pdf())
        self.assertTrue(any("nichts gefunden" in w for w in document.warnings))

    # -- Teilweise gescannt ---------------------------------------------
    def test_teilweise_gescanntes_pdf(self):
        backend = FakeBackend()
        path = str(make_mixed_pdf(Path(self.folder) / "gemischt.pdf"))
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        document = reader.read(path)
        # Nur die Bildseite wurde erkannt
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(document.meta["ocr"]["pages"][0]["page"], 2)
        # Der echte Text der ersten Seite ist unangetastet
        self.assertIn("Seite eins", document.pages[0])
        self.assertIn("Dichtring", document.pages[1])

    def test_text_pdf_loest_keine_ocr_aus(self):
        backend = FakeBackend()
        document = fitz.open()
        page = document.new_page()
        for index in range(8):
            page.insert_text((50, 60 + index * 20),
                             f"Position {index}: Ersatzteil, Preis 12,50 EUR")
        path = str(Path(self.folder) / "text.pdf")
        document.save(path)
        document.close()
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=backend)
        gelesen = reader.read(path)
        self.assertEqual(backend.calls, [])
        self.assertNotIn("ocr", gelesen.meta)

    def test_kaputtes_pdf_wirft_nicht(self):
        path = Path(self.folder) / "kaputt.pdf"
        path.write_bytes(b"%PDF-1.4 das ist kein PDF")
        reader = PdfReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(str(path))
        self.assertTrue(document.warnings)


# ---------------------------------------------------------------------------
# 6. Rendern und Koordinatenumrechnung
# ---------------------------------------------------------------------------

@unittest.skipIf(fitz is None, "PyMuPDF nicht installiert")
class RenderTests(unittest.TestCase):

    def test_render_liefert_png(self):
        document = fitz.open()
        page = document.new_page()
        data = render_page_png(page, dpi=100, preprocess=True)
        document.close()
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_hoehere_aufloesung_ergibt_mehr_daten(self):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((50, 50), "Angebot 12,50 EUR")
        klein = render_page_png(page, dpi=72)
        gross = render_page_png(page, dpi=200)
        document.close()
        self.assertGreater(len(gross), len(klein))

    def test_koordinaten_werden_in_pdf_punkte_umgerechnet(self):
        words = ocr_words_to_words([OcrWord("Test", 300.0, 600.0, 400.0, 640.0)],
                                   scale=72.0 / 300.0)
        self.assertAlmostEqual(words[0].x0, 72.0)
        self.assertAlmostEqual(words[0].y0, 144.0)

    def test_leere_worte_fallen_weg(self):
        self.assertEqual(ocr_words_to_words([OcrWord("  ")]), [])

    def test_wort_ohne_hoehe_bekommt_mindesthoehe(self):
        words = ocr_words_to_words([OcrWord("X", 0.0, 10.0, 5.0, 10.0)])
        self.assertGreaterEqual(words[0].height, 1.0)


# ---------------------------------------------------------------------------
# 7. Bilddateien als Angebotsquelle
# ---------------------------------------------------------------------------

@unittest.skipIf(fitz is None, "PyMuPDF nicht installiert")
class ImageReaderTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="sap_ocr_img_")

    def _image(self, name: str = "angebot.png") -> str:
        return str(make_image_file(Path(self.folder) / name))

    def test_endungen_werden_erkannt(self):
        reader = ImageReader()
        for extension in IMAGE_EXTENSIONS:
            with self.subTest(extension=extension):
                self.assertTrue(reader.can_read(f"scan{extension}"))

    def test_registry_waehlt_den_bildleser(self):
        registry = ReaderRegistry()
        self.assertIsInstance(registry.reader_for("angebot.jpg"), ImageReader)
        self.assertTrue(registry.can_read("angebot.tiff"))

    def test_ohne_backend_klare_meldung_ohne_ausnahme(self):
        reader = ImageReader(ocr_settings=ocr_settings(), ocr_backend=None)
        if reader.resolve_ocr_backend() is not None:
            self.skipTest("Auf diesem Rechner ist eine echte OCR installiert")
        document = reader.read(self._image())
        self.assertTrue(document.warnings)
        self.assertIn("Texterkennung", "\n".join(document.warnings))
        self.assertEqual(document.tables, [])

    def test_abgeschaltete_ocr_meldet_das(self):
        reader = ImageReader(ocr_settings=ocr_settings(enabled=False),
                             ocr_backend=FakeBackend())
        document = reader.read(self._image())
        self.assertTrue(any("abgeschaltet" in w for w in document.warnings))

    def test_bild_mit_backend_liefert_tabelle(self):
        reader = ImageReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(self._image())
        self.assertTrue(document.tables)
        self.assertEqual(document.tables[0].origin, "image-ocr")
        self.assertIn("Dichtring", document.text)

    def test_bild_setzt_ocr_warnung(self):
        reader = ImageReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(self._image())
        self.assertTrue(any("Texterkennung" in w and "pruefen" in w
                            for w in document.warnings))

    def test_kaputte_bilddatei_wirft_nicht(self):
        path = Path(self.folder) / "kaputt.png"
        path.write_bytes(b"kein bild")
        reader = ImageReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(str(path))
        self.assertTrue(document.warnings)
        self.assertEqual(document.tables, [])

    def test_leere_bilddatei_wirft_nicht(self):
        path = Path(self.folder) / "leer.png"
        path.write_bytes(b"")
        reader = ImageReader(ocr_settings=ocr_settings(), ocr_backend=FakeBackend())
        document = reader.read(str(path))
        self.assertTrue(document.warnings)

    def test_registry_liest_bild_ohne_ausnahme(self):
        registry = ReaderRegistry()
        document = registry.read(self._image())
        self.assertIsNotNone(document)
        self.assertTrue(document.warnings or document.tables)

    def test_bilder_in_archiven_bleiben_aussen_vor(self):
        """Ein Logo im ZIP ist kein Angebot -- und darf keine OCR ausloesen."""
        registry = ReaderRegistry()
        self.assertTrue(registry.can_read("Firmenlogo.png"))
        self.assertFalse(registry.can_read_in_archive("Firmenlogo.png"))
        self.assertTrue(registry.can_read_in_archive("Angebot.pdf"))

    def test_fehlende_datei_wird_gemeldet(self):
        registry = ReaderRegistry()
        document = registry.read(str(Path(self.folder) / "gibtsnicht.png"))
        self.assertTrue(any("nicht gefunden" in w for w in document.warnings))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
