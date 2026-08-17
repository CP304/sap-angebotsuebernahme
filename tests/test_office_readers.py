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
from unittest import mock

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_office_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                          # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402
from app.services.readers import ReaderRegistry                   # noqa: E402
from app.services.readers import archive_reader                   # noqa: E402
from app.services.readers.archive_reader import ArchiveReader     # noqa: E402
from app.services.readers.office_reader import (                  # noqa: E402
    OpenDocumentReader,
    RichTextReader,
    WordReader,
)
from app.services.readers.spreadsheet_reader import OdsReader     # noqa: E402

WURZEL = Path(__file__).resolve().parent.parent
BEISPIELE = WURZEL / "sample_data" / "erzeugt"
sys.path.insert(0, str(WURZEL / "sample_data"))


def _erzeuge(name: str, funktion_name: str) -> Path:
    """Beispieldatei ueber den Generator erzeugen (nicht duplizieren)."""
    import erzeuge_beispiele as generator

    ziel = Path(tempfile.mkdtemp(prefix="sap_office_datei_")) / name
    getattr(generator, funktion_name)(ziel)
    return ziel


def _erzeuge_zip() -> Path:
    """Beispielarchiv aus zwei erzeugten Einzelbeispielen bauen."""
    import erzeuge_beispiele as generator

    ordner = Path(tempfile.mkdtemp(prefix="sap_office_archiv_"))
    excel = ordner / "Angebot_Muster_Dichtungstechnik.xlsx"
    generator.excel_mit_kopfzeile(excel)
    csv_datei = ordner / "Preisliste_Muster.csv"
    generator.csv_datei(csv_datei)
    ziel = ordner / "Angebot_Sammlung.zip"
    generator.zip_sammlung(ziel, excel, csv_datei)
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


class _OdsBau:
    """Kleiner Baukasten fuer ODS-Testdateien.

    Die Tests brauchen sehr gezielte Sonderfaelle (riesige Wiederholungen,
    Rohwert gegen Anzeigetext).  Die per Hand gebaute content.xml ist dafuer
    ehrlicher als eine fertige Beispieldatei.
    """

    T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    TB = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    O = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"

    @staticmethod
    def zelle(text: str = "", wiederholt: int = 1, wert: str | None = None,
              datum: str | None = None, typ: str = "string") -> str:
        teile = ["<table:table-cell"]
        if wiederholt > 1:
            teile.append(f' table:number-columns-repeated="{wiederholt}"')
        if datum is not None:
            teile.append(f' office:value-type="date" office:date-value="{datum}"')
        elif wert is not None:
            teile.append(f' office:value-type="{typ}" office:value="{wert}"')
        elif text:
            teile.append(' office:value-type="string"')
        if not text:
            teile.append("/>")
            return "".join(teile)
        teile.append(f"><text:p>{text}</text:p></table:table-cell>")
        return "".join(teile)

    @staticmethod
    def zeile(*zellen: str, wiederholt: int = 1) -> str:
        wdh = f' table:number-rows-repeated="{wiederholt}"' if wiederholt > 1 else ""
        return f"<table:table-row{wdh}>" + "".join(zellen) + "</table:table-row>"

    @classmethod
    def blatt(cls, name: str, *zeilen: str) -> str:
        return (f'<table:table table:name="{name}">' + "".join(zeilen)
                + "</table:table>")

    @classmethod
    def datei(cls, pfad: Path, *blaetter: str) -> Path:
        inhalt = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-content xmlns:office="{cls.O}" '
            f'xmlns:text="{cls.T}" xmlns:table="{cls.TB}">'
            f"<office:body><office:spreadsheet>{''.join(blaetter)}"
            f"</office:spreadsheet></office:body></office:document-content>")
        with zipfile.ZipFile(pfad, "w", zipfile.ZIP_DEFLATED) as archiv:
            archiv.writestr("mimetype",
                            "application/vnd.oasis.opendocument.spreadsheet")
            archiv.writestr("content.xml", inhalt)
        return pfad


class OdsLeserTest(unittest.TestCase):
    """.ods -- die LibreOffice-Entsprechung zu Excel."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ordner = Path(tempfile.mkdtemp(prefix="sap_ods_"))
        cls.datei = _erzeuge("preisliste.ods", "ods_preisliste")
        cls.dokument = OdsReader().read(str(cls.datei))

    def test_endungen(self) -> None:
        leser = OdsReader()
        self.assertTrue(leser.can_read("x.ods"))
        self.assertTrue(leser.can_read("X.OTS"))
        self.assertFalse(leser.can_read("x.odt"))

    def test_zwei_blaetter_werden_gelesen(self) -> None:
        self.assertEqual(len(self.dokument.tables), 2)

    def test_blattname_landet_im_ergebnis(self) -> None:
        titel = [t.title for t in self.dokument.tables]
        self.assertEqual(titel, ["Anschreiben", "Positionen"])
        self.assertEqual(self.dokument.meta["sheet_names"],
                         ["Anschreiben", "Positionen"])

    def test_herkunft_ist_gekennzeichnet(self) -> None:
        for tabelle in self.dokument.tables:
            self.assertEqual(tabelle.origin, "ods-table")

    def test_keine_warnung_bei_gueltiger_datei(self) -> None:
        self.assertEqual(self.dokument.warnings, [])

    def test_leerspalten_werden_abgeschnitten(self) -> None:
        """1014 zusammengefasste Leerspalten duerfen nicht uebrig bleiben."""
        positionen = self.dokument.tables[1]
        self.assertEqual(positionen.column_count, 6)

    def test_leerzeilen_werden_abgeschnitten(self) -> None:
        """Die Schlusszeile mit 1.048.000 Wiederholungen faellt weg."""
        self.assertEqual(self.dokument.tables[1].row_count, 4)
        self.assertEqual(self.dokument.tables[0].row_count, 5)

    def test_wiederholte_spalten_werden_expandiert(self) -> None:
        pfad = _OdsBau.datei(
            self.ordner / "wiederholung.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("A"),
                                        _OdsBau.zelle("B", wiederholt=3),
                                        _OdsBau.zelle("C"))))
        tabelle = OdsReader().read(str(pfad)).tables[0]
        self.assertEqual(tabelle.rows[0], ["A", "B", "B", "B", "C"])

    def test_wiederholte_zeilen_werden_expandiert(self) -> None:
        pfad = _OdsBau.datei(
            self.ordner / "zeilen.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("X"), wiederholt=3),
                          _OdsBau.zeile(_OdsBau.zelle("Ende"))))
        tabelle = OdsReader().read(str(pfad)).tables[0]
        self.assertEqual([z[0] for z in tabelle.rows], ["X", "X", "X", "Ende"])

    def test_riesige_spaltenwiederholung_wird_gedeckelt(self) -> None:
        """Sonst entstehen aus einer Zeile Millionen Zellen."""
        pfad = _OdsBau.datei(
            self.ordner / "riesig.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("X", wiederholt=100000))))
        dokument = OdsReader().read(str(pfad))
        self.assertEqual(dokument.tables[0].column_count, 256)
        self.assertTrue(any("abgeschnitten" in w for w in dokument.warnings))

    def test_riesige_zeilenwiederholung_wird_gedeckelt(self) -> None:
        pfad = _OdsBau.datei(
            self.ordner / "riesig_zeilen.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("X"), wiederholt=500000)))
        dokument = OdsReader().read(str(pfad))
        self.assertEqual(dokument.tables[0].row_count, 10000)
        self.assertTrue(any("abgeschnitten" in w for w in dokument.warnings))

    def test_leere_wiederholung_erzeugt_keine_warnung(self) -> None:
        """Zusammengefasste Leerbereiche sind der Normalfall, kein Verlust."""
        pfad = _OdsBau.datei(
            self.ordner / "leerwiederholung.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("A"),
                                        _OdsBau.zelle(wiederholt=16000)),
                          _OdsBau.zeile(_OdsBau.zelle(), wiederholt=1048000)))
        dokument = OdsReader().read(str(pfad))
        self.assertEqual(dokument.warnings, [])
        self.assertEqual(dokument.tables[0].rows, [["A"]])

    def test_kaputte_wiederholung_wird_ignoriert(self) -> None:
        pfad = self.ordner / "kaputte_zahl.ods"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("content.xml",
                            f'<office:document-content xmlns:office="{_OdsBau.O}" '
                            f'xmlns:text="{_OdsBau.T}" xmlns:table="{_OdsBau.TB}">'
                            f'<table:table table:name="T"><table:table-row>'
                            f'<table:table-cell table:number-columns-repeated="viele">'
                            f"<text:p>A</text:p></table:table-cell>"
                            f"</table:table-row></table:table>"
                            f"</office:document-content>")
        tabelle = OdsReader().read(str(pfad)).tables[0]
        self.assertEqual(tabelle.rows, [["A"]])

    def test_rohwert_schlaegt_formatierten_text(self) -> None:
        """'1.234,50 EUR' ist Anzeige -- gerechnet wird mit office:value."""
        pfad = _OdsBau.datei(
            self.ordner / "rohwert.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(
                              _OdsBau.zelle("1.234,50 EUR", wert="1234.5",
                                            typ="float"),
                              _OdsBau.zelle("4,55 EUR", wert="4.5500",
                                            typ="float"),
                              _OdsBau.zelle("1.000", wert="1000",
                                            typ="float"))))
        tabelle = OdsReader().read(str(pfad)).tables[0]
        self.assertEqual(tabelle.rows[0], ["1234.5", "4.55", "1000"])

    def test_text_wird_genommen_wenn_kein_rohwert_da_ist(self) -> None:
        pfad = _OdsBau.datei(
            self.ordner / "text.ods",
            _OdsBau.blatt("Test", _OdsBau.zeile(_OdsBau.zelle("47110001"))))
        self.assertEqual(OdsReader().read(str(pfad)).tables[0].rows, [["47110001"]])

    def test_datumswert_wird_deutsch_formatiert(self) -> None:
        pfad = _OdsBau.datei(
            self.ordner / "datum.ods",
            _OdsBau.blatt("Test",
                          _OdsBau.zeile(_OdsBau.zelle("2026-09-01",
                                                      datum="2026-09-01"))))
        self.assertEqual(OdsReader().read(str(pfad)).tables[0].rows,
                         [["01.09.2026"]])

    def test_erkennung_liefert_positionen(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        angebot = OfferImportService(einstellungen).import_file(str(self.datei))
        self.assertEqual(angebot.offer_number, "ANG-2026-5501")
        self.assertEqual(angebot.currency, "EUR")
        materialien = [p.material_number for p in angebot.positions]
        for erwartet in ("47110001", "47110005", "49900010"):
            self.assertIn(erwartet, materialien)

    def test_einheiten_und_preise_aus_dem_rohwert(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        angebot = OfferImportService(einstellungen).import_file(str(self.datei))
        position = next(p for p in angebot.positions
                        if p.material_number == "47110005")
        self.assertEqual(position.price, Decimal("4.55"))
        self.assertEqual(position.quantity, Decimal("1000"))
        self.assertEqual(position.uom, "ST")

    def test_beschaedigte_datei(self) -> None:
        pfad = self.ordner / "kaputt.ods"
        pfad.write_bytes(b"das ist kein ZIP")
        dokument = OdsReader().read(str(pfad))
        self.assertTrue(dokument.warnings)
        self.assertEqual(dokument.tables, [])

    def test_leere_datei(self) -> None:
        pfad = self.ordner / "leer.ods"
        pfad.write_bytes(b"")
        dokument = OdsReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_zip_ohne_content_xml(self) -> None:
        pfad = self.ordner / "ohne_content.ods"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("mimetype", "irgendwas")
        dokument = OdsReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_kaputtes_xml(self) -> None:
        pfad = self.ordner / "kaputtes_xml.ods"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("content.xml", "<office:document nicht geschlossen")
        dokument = OdsReader().read(str(pfad))
        self.assertTrue(dokument.warnings)

    def test_tabelle_ganz_ohne_zellen(self) -> None:
        pfad = _OdsBau.datei(self.ordner / "leeres_blatt.ods",
                             _OdsBau.blatt("Leer", _OdsBau.zeile()))
        dokument = OdsReader().read(str(pfad))
        self.assertEqual(dokument.tables, [])
        self.assertTrue(dokument.warnings)


class ArchivLeserTest(unittest.TestCase):
    """ZIP -- Lieferanten packen PDF und Excel gern zusammen."""

    def setUp(self) -> None:
        self.ordner = Path(tempfile.mkdtemp(prefix="sap_zip_"))
        self.registry = ReaderRegistry()
        self.leser = self.registry.archive_reader

    def _zip(self, name: str, dateien: dict) -> Path:
        pfad = self.ordner / name
        with zipfile.ZipFile(pfad, "w", zipfile.ZIP_DEFLATED) as archiv:
            for eintrag, inhalt in dateien.items():
                archiv.writestr(eintrag, inhalt)
        return pfad

    def _csv(self, marker: str = "47110001") -> str:
        return ("Pos;Material;Bezeichnung;Menge;ME;Preis\r\n"
                f"10;{marker};Dichtring 52x72x10;25;ST;12,35\r\n")

    # -- Normalfall -----------------------------------------------------
    def test_endungen(self) -> None:
        self.assertTrue(self.leser.can_read("x.zip"))
        self.assertTrue(self.leser.can_read("X.ZIP"))
        self.assertFalse(self.leser.can_read("x.rar"))

    def test_mehrere_dateien_werden_gelesen(self) -> None:
        pfad = self._zip("zwei.zip", {"a.csv": self._csv("47110001"),
                                      "b.txt": "Angebot ANG-2026-1234"})
        dokument = self.leser.read(str(pfad))
        self.assertEqual(len(dokument.attachments), 2)
        namen = [a.meta.get("attachment_name") for a in dokument.attachments]
        self.assertEqual(sorted(namen), ["a.csv", "b.txt"])

    def test_inhalt_der_dateien_bleibt_erhalten(self) -> None:
        """Der Temp-Ordner ist danach weg -- die Daten muessen es nicht sein."""
        pfad = self._zip("inhalt.zip", {"a.csv": self._csv("47110001")})
        dokument = self.leser.read(str(pfad))
        self.assertIn("47110001", dokument.attachments[0].all_text())

    def test_anhaenge_sind_als_anhang_gekennzeichnet(self) -> None:
        from app.models.enums import SourceKind

        pfad = self._zip("kind.zip", {"a.csv": self._csv()})
        dokument = self.leser.read(str(pfad))
        self.assertEqual(dokument.attachments[0].source_kind,
                         SourceKind.EMAIL_ATTACHMENT)

    def test_unbekannte_endung_wird_uebersprungen(self) -> None:
        pfad = self._zip("mitbild.zip", {"a.csv": self._csv(),
                                         "logo.png": "\x89PNG-Attrappe"})
        dokument = self.leser.read(str(pfad))
        self.assertEqual(len(dokument.attachments), 1)
        self.assertEqual(dokument.meta["archiv_uebersprungen"], ["logo.png"])
        self.assertTrue(any("logo.png" in w for w in dokument.warnings))

    def test_ordner_stoeren_nicht(self) -> None:
        pfad = self.ordner / "mitordner.zip"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("unterordner/", "")
            archiv.writestr("unterordner/a.csv", self._csv())
        dokument = self.leser.read(str(pfad))
        self.assertEqual(len(dokument.attachments), 1)

    def test_erkennung_liefert_positionen(self) -> None:
        einstellungen = Settings()
        einstellungen.ensure_dirs()
        pfad = self._zip("angebot.zip", {"preise.csv": self._csv("47110001")})
        angebot = OfferImportService(einstellungen).import_file(str(pfad))
        self.assertIn("47110001", [p.material_number for p in angebot.positions])

    # -- Sicherheit -----------------------------------------------------
    def test_zip_slip_wird_abgewehrt(self) -> None:
        pfad = self._zip("slip.zip", {"../boese.csv": self._csv(),
                                      "gut.csv": self._csv()})
        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("verworfen" in w for w in dokument.warnings))
        self.assertEqual(len(dokument.attachments), 1)
        self.assertFalse((self.ordner.parent / "boese.csv").exists())

    def test_absoluter_pfad_wird_abgewehrt(self) -> None:
        pfad = self._zip("absolut.zip", {"/etc/boese.csv": self._csv()})
        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("verworfen" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_laufwerksbuchstabe_wird_abgewehrt(self) -> None:
        pfad = self._zip("laufwerk.zip", {"C:/temp/boese.csv": self._csv()})
        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("verworfen" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_zu_grosses_archiv_wird_abgewehrt(self) -> None:
        """Zip-Bombe: viel Luft, wenig Datei."""
        pfad = self._zip("bombe.zip", {"gross.csv": "A" * 200_000})
        with mock.patch.object(archive_reader, "_MAX_TOTAL_BYTES", 50_000):
            dokument = self.leser.read(str(pfad))
        self.assertTrue(any("Sicherheitsgruenden" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_zu_viele_eintraege_werden_abgewehrt(self) -> None:
        inhalte = {f"datei_{i}.txt": "x" for i in range(201)}
        pfad = self._zip("viele.zip", inhalte)
        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("zulaessigen" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_kennwortgeschuetztes_archiv(self) -> None:
        pfad = self.ordner / "kennwort.zip"
        with zipfile.ZipFile(pfad, "w") as archiv:
            info = zipfile.ZipInfo("geheim.csv")
            archiv.writestr(info, self._csv())
        # Verschluesselungsbit nachtraeglich setzen (die Standardbibliothek
        # kann selbst nicht verschluesseln)
        rohdaten = bytearray(pfad.read_bytes())
        stelle = rohdaten.find(b"PK\x03\x04")
        rohdaten[stelle + 6] |= 0x01
        stelle = rohdaten.find(b"PK\x01\x02")
        rohdaten[stelle + 8] |= 0x01
        pfad.write_bytes(bytes(rohdaten))

        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("kennwortgeschuetzt" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_verschachteltes_archiv_eine_ebene(self) -> None:
        innen = self._zip("innen.zip", {"a.csv": self._csv("47110005")})
        aussen = self._zip("aussen.zip", {"innen.zip": innen.read_bytes()})
        dokument = self.leser.read(str(aussen))
        self.assertEqual(len(dokument.attachments), 1)
        kind = dokument.attachments[0]
        self.assertEqual(len(kind.attachments), 1)
        self.assertIn("47110005", kind.attachments[0].all_text())

    def test_zu_tiefe_verschachtelung_wird_gestoppt(self) -> None:
        innen = self._zip("tief1.zip", {"a.csv": self._csv()})
        mitte = self._zip("tief2.zip", {"tief1.zip": innen.read_bytes()})
        aussen = self._zip("tief3.zip", {"tief2.zip": mitte.read_bytes()})
        dokument = self.leser.read(str(aussen))
        alle = " ".join(w for d in dokument.iter_documents() for w in d.warnings)
        self.assertIn("verschachtelt", alle)

    def test_leeres_archiv(self) -> None:
        pfad = self._zip("leer.zip", {})
        dokument = self.leser.read(str(pfad))
        self.assertTrue(any("keine Dateien" in w for w in dokument.warnings))
        self.assertEqual(dokument.attachments, [])

    def test_beschaedigtes_archiv(self) -> None:
        pfad = self.ordner / "kaputt.zip"
        pfad.write_bytes(b"PK das ist nichts")
        dokument = self.leser.read(str(pfad))
        self.assertTrue(dokument.warnings)
        self.assertEqual(dokument.attachments, [])

    def test_temp_verzeichnis_wird_aufgeraeumt(self) -> None:
        gemerkt: list[str] = []
        echtes_mkdtemp = tempfile.mkdtemp

        def merken(*args, **kwargs):
            pfad = echtes_mkdtemp(*args, **kwargs)
            gemerkt.append(pfad)
            return pfad

        pfad = self._zip("aufraeumen.zip", {"a.csv": self._csv()})
        with mock.patch.object(archive_reader.tempfile, "mkdtemp", merken):
            dokument = self.leser.read(str(pfad))
        self.assertEqual(len(dokument.attachments), 1)
        self.assertTrue(gemerkt, "es wurde kein Temp-Verzeichnis angelegt")
        for ordner in gemerkt:
            self.assertFalse(Path(ordner).exists(),
                             f"Temp-Verzeichnis blieb zurueck: {ordner}")

    def test_ohne_registry_klare_meldung(self) -> None:
        """Ein alleinstehender Leser kann Inhalte nicht auswerten."""
        pfad = self._zip("allein.zip", {"a.csv": self._csv()})
        dokument = archive_reader.ArchiveReader().read(str(pfad))
        self.assertEqual(len(dokument.attachments), 1)
        self.assertTrue(dokument.attachments[0].warnings)

    def test_beispieldatei_wird_gelesen(self) -> None:
        pfad = _erzeuge_zip()
        dokument = self.registry.read(str(pfad))
        self.assertEqual(dokument.meta["archiv_uebersprungen"], ["Firmenlogo.png"])
        self.assertEqual(len(dokument.attachments), 2)


class RegistrierungTest(unittest.TestCase):
    """Die neuen Formate muessen im Leserverzeichnis auftauchen."""

    def setUp(self) -> None:
        self.registry = ReaderRegistry()

    def test_endungen_sind_bekannt(self) -> None:
        endungen = self.registry.supported_extensions()
        for endung in (".docx", ".odt", ".rtf", ".ods", ".ots", ".zip"):
            self.assertIn(endung, endungen)

    def test_richtiger_leser_wird_gewaehlt(self) -> None:
        self.assertIsInstance(self.registry.reader_for("x.docx"), WordReader)
        self.assertIsInstance(self.registry.reader_for("x.odt"), OpenDocumentReader)
        self.assertIsInstance(self.registry.reader_for("x.rtf"), RichTextReader)
        self.assertIsInstance(self.registry.reader_for("x.ods"), OdsReader)
        self.assertIsInstance(self.registry.reader_for("x.zip"), ArchiveReader)

    def test_ods_und_odt_werden_nicht_verwechselt(self) -> None:
        """Beides ist OpenDocument -- aber Text und Tabelle sind verschieden."""
        self.assertIsInstance(self.registry.reader_for("preise.ods"), OdsReader)
        self.assertIsInstance(self.registry.reader_for("preise.odt"),
                              OpenDocumentReader)

    def test_archivleser_ist_mit_der_registry_verdrahtet(self) -> None:
        """Ohne Rueckruf koennte das Archiv seinen Inhalt nicht lesen."""
        self.assertIsNotNone(self.registry.archive_reader.attachment_reader)
        self.assertIsNotNone(self.registry.archive_reader.can_read_entry)

    def test_textleser_faengt_rtf_nicht_ab(self) -> None:
        """Reihenfolge im Verzeichnis: RTF vor dem allgemeinen Textleser."""
        leser = self.registry.reader_for("angebot.rtf")
        self.assertIsInstance(leser, RichTextReader)

    def test_unbekannte_endung(self) -> None:
        self.assertIsNone(self.registry.reader_for("x.xyz"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
