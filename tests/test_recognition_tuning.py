"""Feinschliff der Angebotserkennung (unittest, ohne externe Testframeworks).

Geprueft werden fuenf Verbesserungen, die alle demselben Grundsatz folgen:
**lieber nachfragen als stillschweigend falsch liegen.**

1. *Linien-basierte Spaltenerkennung im PDF.*  Hat ein PDF gezeichnete
   Tabellenlinien, sind diese die verlaesslichste Spaltengrenze -- sie stehen
   im Dokument und sind nicht geschaetzt.  Fehlen sie, wird auf das bisherige
   Koordinatenverfahren zurueckgefallen; welches griff, steht im ``origin``.
2. *Nicht zugeordnete Aussagen aus dem Mailtext.*  Ein Satz des Lieferanten,
   der eine Nummer, einen Preis, eine Menge oder ein Datum nennt und den
   niemand aufgegriffen hat, wird als Warnung ausgewiesen.
3. *Ausschlussmerkmale im Lieferantenprofil.*  Ein zu generisches Merkmal darf
   nicht dazu fuehren, dass ein Profil auf ein fremdes Dokument passt.
4. *Datumsreihenfolge je Lieferant.*  Tag/Monat werden gelernt -- und dort, wo
   ein Wert > 12 auftritt, ist die Reihenfolge ohnehin bewiesen.
5. *Toleranzen als Profilparameter.*  Zeilen-/Spaltentoleranz des PDF-Lesers
   kommen aus den Einstellungen und sind je Lieferant ueberschreibbar.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_tuning_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.config.settings import ExtractionSettings, Settings            # noqa: E402
from app.models.enums import FieldOrigin, IssueSeverity, SourceKind     # noqa: E402
from app.models.offer import EmailContext, Offer                        # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.services.extraction.email_merge import (                       # noqa: E402
    UNMATCHED_ISSUE_CODE,
    apply_email_supplements,
    is_statement,
)
from app.services.extraction.learning import (                          # noqa: E402
    LearningConfig,
    describe_learning,
    forget_rule,
    learn_from_corrections,
    suggest_exclude_keyword,
)
from app.services.extraction.profiles import (                          # noqa: E402
    VendorProfile,
    match_profile,
)
from app.services.extraction.table_extractor import (                   # noqa: E402
    TableExtractor,
    detect_day_first,
    parse_date_ordered,
)
from app.services.readers.base import RawDocument, TableBlock           # noqa: E402
from app.services.readers.pdf_reader import (                           # noqa: E402
    PdfReader,
    lattice_tables,
    make_ruling,
    make_word,
    page_rulings,
    tolerances_for,
    words_to_tables,
)

try:  # PyMuPDF ist Pflicht fuer die PDF-Tests, aber nicht fuer den Rest
    import fitz
except ImportError:  # pragma: no cover
    fitz = None


# ==========================================================================
# Hilfsmittel
# ==========================================================================

def worte(zeilen: list[tuple[float, list[tuple[float, str]]]]) -> list:
    """Woerter aus (y, [(x, Text), ...]) erzeugen -- kurze Schreibweise."""
    out = []
    for y, eintraege in zeilen:
        for x, text in eintraege:
            out.append(make_word(x, y, x + 6.0 * len(text), y + 9.0, text))
    return out


#: Eine kleine Preistabelle: drei Spalten, vier Zeilen
TABELLE = [
    (100.0, [(30.0, "Artikel"), (150.0, "Bezeichnung"), (330.0, "Preis")]),
    (120.0, [(30.0, "47110001"), (150.0, "Dichtring"), (330.0, "12,85")]),
    (140.0, [(30.0, "47110002"), (150.0, "O-Ring"), (330.0, "8,90")]),
    (160.0, [(30.0, "47110003"), (150.0, "Flachdichtung"), (330.0, "3,40")]),
]

#: Senkrechte Linien passend zu TABELLE (links, zwei Trenner, rechts)
SENKRECHT = [make_ruling(20.0, 95.0, 175.0), make_ruling(140.0, 95.0, 175.0),
             make_ruling(320.0, 95.0, 175.0), make_ruling(420.0, 95.0, 175.0)]


def angebot_mit_positionen() -> Offer:
    offer = Offer()
    for nummer, preis in (("47110001", "12,85"), ("47110002", "8,90")):
        position = OfferPosition()
        position.source_kind = SourceKind.EMAIL_ATTACHMENT
        position.set_field("material_number", nummer, FieldOrigin.EXTRACTED)
        position.set_field("price", Decimal(preis.replace(",", ".")),
                           FieldOrigin.EXTRACTED)
        offer.positions.append(position)
    return offer


def dokument_mit_text(text: str) -> RawDocument:
    document = RawDocument(source_path="angebot.pdf", source_kind=SourceKind.PDF)
    document.text = text
    return document


# ==========================================================================
# 1. Linienverfahren im PDF
# ==========================================================================

class LinienerkennungTests(unittest.TestCase):
    """Gezeichnete Linien schlagen jede Koordinatenschaetzung."""

    def test_01_linien_liefern_lattice_block(self) -> None:
        bloecke = lattice_tables(worte(TABELLE), SENKRECHT, [], page=1)
        self.assertEqual(len(bloecke), 1)
        self.assertEqual(bloecke[0].origin, "pdf-lattice")

    def test_02_spalten_werden_an_den_linien_geschnitten(self) -> None:
        block = lattice_tables(worte(TABELLE), SENKRECHT, [], page=1)[0].normalized()
        self.assertEqual(block.rows[1], ["47110001", "Dichtring", "12,85"])

    def test_03_ohne_linien_kein_lattice(self) -> None:
        self.assertEqual(lattice_tables(worte(TABELLE), [], [], page=1), [])

    def test_04_zu_wenige_linien_kein_lattice(self) -> None:
        self.assertEqual(
            lattice_tables(worte(TABELLE), SENKRECHT[:2], [], page=1), [])

    def test_05_fallback_kennzeichnet_sich_als_pdf_layout(self) -> None:
        bloecke = words_to_tables(worte(TABELLE), page=1)
        self.assertTrue(bloecke)
        self.assertEqual(bloecke[0].origin, "pdf-layout")

    def test_06_kurze_striche_zaehlen_nicht_als_spaltengrenze(self) -> None:
        kurz = [make_ruling(20.0, 118.0, 124.0), make_ruling(140.0, 118.0, 124.0),
                make_ruling(320.0, 118.0, 124.0)]
        self.assertEqual(lattice_tables(worte(TABELLE), kurz, [], page=1), [])

    def test_07_waagerechte_linien_halten_umbrochenen_text_zusammen(self) -> None:
        zeilen = list(TABELLE)
        zeilen.insert(2, (131.0, [(150.0, "NBR 40x52x7")]))
        waagerecht = [make_ruling(y, 20.0, 420.0)
                      for y in (95.0, 112.0, 136.0, 152.0, 172.0)]
        block = lattice_tables(worte(zeilen), SENKRECHT, waagerecht,
                               page=1)[0].normalized()
        treffer = [r for r in block.rows if r[0] == "47110001"]
        self.assertEqual(len(treffer), 1)
        self.assertIn("NBR 40x52x7", treffer[0][1])

    def test_08_ohne_waagerechte_linien_bleibt_jede_zeile_eine_zeile(self) -> None:
        zeilen = list(TABELLE)
        zeilen.insert(2, (131.0, [(150.0, "NBR 40x52x7")]))
        block = lattice_tables(worte(zeilen), SENKRECHT, [], page=1)[0].normalized()
        self.assertEqual(len(block.rows), 5)

    def test_09_rechtecke_gelten_als_linien(self) -> None:
        zeichnung = [{"items": [
            ("re", (19.0, 95.0, 20.5, 175.0)),
            ("re", (139.0, 95.0, 140.5, 175.0)),
            ("re", (319.0, 95.0, 320.5, 175.0)),
        ]}]
        senkrecht, waagerecht = page_rulings(zeichnung)
        self.assertEqual(len(senkrecht), 3)
        self.assertEqual(waagerecht, [])

    def test_10_strecken_werden_nach_richtung_getrennt(self) -> None:
        zeichnung = [{"items": [
            ("l", (20.0, 95.0), (20.0, 175.0)),
            ("l", (20.0, 95.0), (420.0, 95.0)),
            ("l", (20.0, 95.0), (300.0, 175.0)),        # schraeg -> egal
        ]}]
        senkrecht, waagerecht = page_rulings(zeichnung)
        self.assertEqual(len(senkrecht), 1)
        self.assertEqual(len(waagerecht), 1)

    def test_11_doppelt_gezeichnete_linien_werden_verschmolzen(self) -> None:
        zeichnung = [{"items": [
            ("l", (20.0, 95.0), (20.0, 175.0)),
            ("l", (20.6, 95.0), (20.6, 175.0)),
        ]}]
        senkrecht, _ = page_rulings(zeichnung)
        self.assertEqual(len(senkrecht), 1)

    def test_12_ein_rahmen_liefert_vier_kanten(self) -> None:
        senkrecht, waagerecht = page_rulings([{"items": [("re", (20.0, 95.0,
                                                                420.0, 175.0))]}])
        self.assertEqual(len(senkrecht), 2)
        self.assertEqual(len(waagerecht), 2)

    def test_13_kaputte_zeichnungsdaten_werfen_nicht(self) -> None:
        self.assertEqual(page_rulings([{"items": [("l", None, None), ("x",)]}]),
                         ([], []))

    def test_14_leere_eingabe_liefert_leere_liste(self) -> None:
        self.assertEqual(lattice_tables([], SENKRECHT, [], page=1), [])


@unittest.skipIf(fitz is None, "PyMuPDF nicht installiert")
class PdfMitGezeichnetenLinienTests(unittest.TestCase):
    """Ein echtes PDF -- einmal mit Liniengitter, einmal ohne."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ordner = Path(tempfile.mkdtemp(prefix="sap_lattice_"))
        cls.mit_linien = cls.ordner / "mit_linien.pdf"
        cls.ohne_linien = cls.ordner / "ohne_linien.pdf"
        cls._schreibe(cls.mit_linien, linien=True)
        cls._schreibe(cls.ohne_linien, linien=False)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.ordner, ignore_errors=True)

    @staticmethod
    def _schreibe(ziel: Path, linien: bool) -> None:
        dokument = fitz.open()
        seite = dokument.new_page()
        spalten = (40.0, 160.0, 340.0)
        kopf = ("Artikel", "Bezeichnung", "Preis")
        daten = [("47110001", "Dichtring NBR", "12,85"),
                 ("47110002", "O-Ring Viton", "8,90"),
                 ("47110003", "Flachdichtung", "3,40")]
        y = 100.0
        for index, text in enumerate(kopf):
            seite.insert_text((spalten[index], y), text, fontsize=10)
        for zeile in daten:
            y += 20.0
            for index, text in enumerate(zeile):
                seite.insert_text((spalten[index], y), text, fontsize=10)
        if linien:
            for x in (30.0, 150.0, 330.0, 460.0):
                seite.draw_line(fitz.Point(x, 88.0), fitz.Point(x, y + 8.0))
            for waagerecht in (88.0, 106.0, 126.0, 146.0, y + 8.0):
                seite.draw_line(fitz.Point(30.0, waagerecht),
                                fitz.Point(460.0, waagerecht))
        dokument.save(str(ziel))
        dokument.close()

    def test_15_pdf_mit_linien_wird_ueber_das_gitter_gelesen(self) -> None:
        document = PdfReader().read(str(self.mit_linien))
        herkunft = {t.origin for t in document.tables}
        self.assertIn("pdf-lattice", herkunft)

    def test_16_pdf_ohne_linien_faellt_auf_das_koordinatenverfahren(self) -> None:
        document = PdfReader().read(str(self.ohne_linien))
        herkunft = {t.origin for t in document.tables}
        self.assertNotIn("pdf-lattice", herkunft)
        self.assertIn("pdf-layout", herkunft)

    def test_17_lattice_abschaltbar(self) -> None:
        settings = Settings()
        settings.extraction.pdf_use_lattice = False
        document = PdfReader(settings=settings).read(str(self.mit_linien))
        self.assertNotIn("pdf-lattice", {t.origin for t in document.tables})

    def test_18_inhalt_bleibt_vollstaendig(self) -> None:
        document = PdfReader().read(str(self.mit_linien))
        block = [t for t in document.tables if t.origin == "pdf-lattice"][0]
        text = block.as_text()
        for erwartet in ("47110001", "Dichtring NBR", "12,85"):
            self.assertIn(erwartet, text)


# ==========================================================================
# 2. Uebergangene Aussagen aus dem Mailtext
# ==========================================================================

class UnzugeordneteAussagenTests(unittest.TestCase):
    """Was niemand aufgegriffen hat, muss der Anwender zu sehen bekommen."""

    def setUp(self) -> None:
        self.settings = Settings()
        self.offer = angebot_mit_positionen()

    def _anwenden(self, text: str) -> list[str]:
        return apply_email_supplements(self.offer, text, self.settings)

    def test_20_unbekannte_materialnummer_wird_gemeldet(self) -> None:
        notizen = self._anwenden("Fuer 99887766 gilt ein Preis von 4,20 EUR.")
        self.assertTrue(any("konnte keiner Position" in n for n in notizen), notizen)

    def test_21_hinweis_ist_eine_warnung_und_nicht_blockierend(self) -> None:
        self._anwenden("Fuer 99887766 gilt ein Preis von 4,20 EUR.")
        befunde = [i for i in self.offer.issues if i.code == UNMATCHED_ISSUE_CODE]
        self.assertEqual(len(befunde), 1)
        self.assertIs(befunde[0].severity, IssueSeverity.WARNING)
        self.assertFalse(befunde[0].blocking)

    def test_22_satz_steht_im_klartext_im_befund(self) -> None:
        self._anwenden("Fuer 99887766 gilt ein Preis von 4,20 EUR.")
        befund = [i for i in self.offer.issues if i.code == UNMATCHED_ISSUE_CODE][0]
        self.assertIn("99887766", befund.detail)

    def test_23_langer_satz_wird_gekuerzt(self) -> None:
        satz = ("Fuer 99887766 gilt ab sofort ein Sonderpreis von 4,20 EUR "
                + "und weitere Bedingungen " * 10)
        notizen = self._anwenden(satz)
        hinweis = [n for n in notizen if "konnte keiner Position" in n][0]
        self.assertIn("...", hinweis)
        self.assertLess(len(hinweis), 260)

    def test_24_floskel_ist_keine_aussage(self) -> None:
        self.assertFalse(is_statement("Mit freundlichen Gruessen, Max Muster"))
        self.assertFalse(is_statement("Sehr geehrte Damen und Herren"))

    def test_25_signatur_und_rechtstext_zaehlen_nicht(self) -> None:
        for zeile in ("Handelsregister HRB 12345 Amtsgericht Muenster",
                      "Tel. 02871 123456, Fax 02871 123457",
                      "Es gelten unsere Allgemeinen Geschaeftsbedingungen",
                      "Diese E-Mail ist vertraulich zu behandeln"):
            self.assertFalse(is_statement(zeile), zeile)

    def test_26_aussage_mit_datum_zaehlt(self) -> None:
        self.assertTrue(is_statement("Die neue Staffel greift ab 01.10.2026"))

    def test_27_aussage_mit_menge_zaehlt(self) -> None:
        self.assertTrue(is_statement("Wir liefern nur noch in Gebinden zu 500 Stueck"))

    def test_28_floskel_erzeugt_keinen_hinweis(self) -> None:
        notizen = self._anwenden("Mit freundlichen Gruessen\nMax Muster")
        self.assertEqual(notizen, [])
        self.assertEqual(len(list(self.offer.issues)), 0)

    def test_29_abschaltbar_ueber_die_einstellung(self) -> None:
        self.settings.extraction.warn_on_unmatched_email_statements = False
        notizen = self._anwenden("Fuer 99887766 gilt ein Preis von 4,20 EUR.")
        self.assertEqual(notizen, [])
        self.assertEqual(len(list(self.offer.issues)), 0)

    def test_30_uebernommene_aussage_erzeugt_keinen_hinweis(self) -> None:
        notizen = self._anwenden(
            "Der Preis fuer 47110001 ist ueberholt, es gilt 13,50 EUR.")
        self.assertTrue(notizen)
        self.assertFalse(any("konnte keiner Position" in n for n in notizen), notizen)

    def test_31_bereits_bekannte_kopfangabe_erzeugt_keinen_hinweis(self) -> None:
        self.offer.set_field("valid_from", datetime.date(2026, 9, 1),
                             FieldOrigin.EXTRACTED)
        notizen = self._anwenden("Die Preise gelten ab 01.09.2026.")
        self.assertEqual(notizen, [])

    def test_32_mehrere_aussagen_landen_in_einem_befund(self) -> None:
        self._anwenden("Fuer 99887766 gilt 4,20 EUR.\n"
                       "Artikel 11223344 wird ab 01.12.2026 anders verpackt.")
        befunde = [i for i in self.offer.issues if i.code == UNMATCHED_ISSUE_CODE]
        self.assertEqual(len(befunde), 1)
        self.assertEqual(len(befunde[0].detail.splitlines()), 2)


# ==========================================================================
# 3. Ausschlussmerkmale im Lieferantenprofil
# ==========================================================================

class AusschlussmerkmalTests(unittest.TestCase):
    """Ein zu generisches Merkmal darf kein fremdes Angebot einfangen."""

    def setUp(self) -> None:
        self.profile = VendorProfile(profile_id="p1", vendor_key="4711",
                                     vendor_name="Dichtungswerk Nord")
        self.profile.email_domains = ["sammelpostfach.de"]
        self.document = dokument_mit_text(
            "Angebot der Pumpen Weber GmbH\nArtikel 47110001 Dichtring")
        self.document.email = EmailContext(from_address="info@sammelpostfach.de")

    def test_40_ohne_ausschluss_greift_das_profil(self) -> None:
        treffer, score = match_profile([self.profile], self.document)
        self.assertIs(treffer, self.profile)
        self.assertGreater(score, 0.5)

    def test_41_ausschlussmerkmal_verwirft_das_profil(self) -> None:
        self.profile.exclude_keywords = ["pumpen weber"]
        treffer, _ = match_profile([self.profile], self.document)
        self.assertIsNone(treffer)

    def test_42_ausschluss_wirkt_auch_bei_hohem_score(self) -> None:
        self.profile.add_fingerprint("fp1:egal")
        self.profile.exclude_keywords = ["pumpen weber"]
        offer = Offer(vendor_number="4711")
        self.assertIsNone(match_profile([self.profile], self.document, offer)[0])

    def test_43_ausschluss_wird_protokolliert(self) -> None:
        self.profile.exclude_keywords = ["pumpen weber"]
        offer = Offer()
        match_profile([self.profile], self.document, offer)
        self.assertTrue(any("Ausschlussmerkmal" in n for n in offer.extraction_notes),
                        offer.extraction_notes)

    def test_44_ein_anderes_profil_kann_weiterhin_greifen(self) -> None:
        self.profile.exclude_keywords = ["pumpen weber"]
        anderes = VendorProfile(profile_id="p2", vendor_name="Pumpen Weber")
        anderes.email_domains = ["sammelpostfach.de"]
        treffer, _ = match_profile([self.profile, anderes], self.document)
        self.assertIs(treffer, anderes)

    def test_45_gross_klein_ist_egal(self) -> None:
        self.profile.add_exclude_keyword("PUMPEN Weber")
        self.assertEqual(self.profile.excluded_by("Angebot der pumpen weber GmbH"),
                         "pumpen weber")

    def test_46_serialisierung_haelt_die_merkmale(self) -> None:
        self.profile.add_exclude_keyword("pumpen weber")
        wieder = VendorProfile.from_dict(self.profile.to_dict())
        self.assertEqual(wieder.exclude_keywords, ["pumpen weber"])

    def test_47_altes_profil_ohne_feld_bleibt_lesbar(self) -> None:
        daten = self.profile.to_dict()
        daten.pop("exclude_keywords")
        daten.pop("date_order")
        wieder = VendorProfile.from_dict(daten)
        self.assertEqual(wieder.exclude_keywords, [])
        self.assertEqual(wieder.date_order, "auto")

    def test_48_vorschlag_stammt_aus_dem_dokument(self) -> None:
        vorschlag = suggest_exclude_keyword(self.document, self.profile)
        self.assertIn("pumpen weber", vorschlag)

    def test_49_verworfenes_profil_merkt_das_merkmal_erst_vor(self) -> None:
        original, korrigiert = Offer(), Offer()
        learn_from_corrections(original, korrigiert, self.document, self.profile,
                               profile_rejected=True)
        self.assertEqual(self.profile.exclude_keywords, [])
        self.assertTrue(any(k.startswith("exclude_keyword:")
                            for k in self.profile.pending_rules),
                        self.profile.pending_rules)

    def test_50_zweiter_fehltreffer_schaltet_scharf(self) -> None:
        original, korrigiert = Offer(), Offer()
        for _ in range(2):
            learn_from_corrections(original, korrigiert, self.document, self.profile,
                                   profile_rejected=True)
        self.assertTrue(self.profile.exclude_keywords, describe_learning(self.profile))

    def test_51_merkmal_laesst_sich_wieder_vergessen(self) -> None:
        self.profile.add_exclude_keyword("pumpen weber")
        self.assertTrue(forget_rule(self.profile, "exclude_keyword:pumpen weber"))
        self.assertEqual(self.profile.exclude_keywords, [])

    def test_52_ohne_fehltreffer_wird_nichts_vorgeschlagen(self) -> None:
        original, korrigiert = Offer(), Offer()
        learn_from_corrections(original, korrigiert, self.document, self.profile)
        self.assertFalse(any(k.startswith("exclude_keyword:")
                             for k in self.profile.pending_rules))

    def test_53_zuruecksetzen_loescht_die_merkmale(self) -> None:
        self.profile.add_exclude_keyword("pumpen weber")
        self.profile.reset_learning()
        self.assertEqual(self.profile.exclude_keywords, [])


# ==========================================================================
# 4. Datumsreihenfolge
# ==========================================================================

class DatumsreihenfolgeTests(unittest.TestCase):
    """Tag oder Monat zuerst -- geraten wird nichts, belegt wird alles."""

    def test_60_tag_groesser_zwoelf_beweist_tag_zuerst(self) -> None:
        self.assertIs(detect_day_first(["13.04.2026", "03.04.2026"]), True)

    def test_61_zweiter_wert_groesser_zwoelf_beweist_monat_zuerst(self) -> None:
        self.assertIs(detect_day_first(["04/13/2026", "04/03/2026"]), False)

    def test_62_ohne_beweis_wird_nichts_entschieden(self) -> None:
        self.assertIsNone(detect_day_first(["03.04.2026", "05.06.2026"]))

    def test_63_widerspruch_entscheidet_nichts(self) -> None:
        self.assertIsNone(detect_day_first(["13.04.2026", "04.13.2026"]))

    def test_64_monat_zuerst_dreht_ein_punktdatum(self) -> None:
        self.assertEqual(parse_date_ordered("03.04.2026", day_first=False),
                         datetime.date(2026, 3, 4))

    def test_65_tag_zuerst_laesst_ein_punktdatum_stehen(self) -> None:
        self.assertEqual(parse_date_ordered("03.04.2026", day_first=True),
                         datetime.date(2026, 4, 3))

    def test_66_eindeutiger_wert_schlaegt_die_vorgabe(self) -> None:
        self.assertEqual(parse_date_ordered("13.04.2026", day_first=False),
                         datetime.date(2026, 4, 13))

    def test_67_ohne_vorgabe_bleibt_das_bisherige_verhalten(self) -> None:
        self.assertEqual(parse_date_ordered("03.04.2026"),
                         datetime.date(2026, 4, 3))

    def _block(self) -> TableBlock:
        return TableBlock(rows=[
            ["Artikel", "Bezeichnung", "Preis", "Gueltig ab"],
            ["47110001", "Dichtring", "12,85", "03.04.2026"],
            ["47110002", "O-Ring", "8,90", "13.04.2026"],
        ], origin="excel")

    def test_68_spaltenweise_erkennung_ohne_profil(self) -> None:
        extractor = TableExtractor(Settings())
        analyse = extractor.analyze(self._block())
        spalte = [i for i, a in analyse.columns.items() if a.field == "valid_from"]
        self.assertTrue(spalte, analyse.columns)
        self.assertIs(analyse.date_day_first.get(spalte[0]), True)

    def test_69_profil_reicht_die_reihenfolge_durch(self) -> None:
        profile = VendorProfile(profile_id="p1", date_order="month_first")
        extractor = TableExtractor(Settings(), profile=profile)
        block = TableBlock(rows=[
            ["Artikel", "Bezeichnung", "Preis", "Gueltig ab"],
            ["47110001", "Dichtring", "12,85", "03.04.2026"],
            ["47110002", "O-Ring", "8,90", "05.06.2026"],
        ], origin="excel")
        document = RawDocument(source_kind=SourceKind.EXCEL)
        document.tables.append(block)
        ergebnis = extractor.extract(document)
        self.assertEqual(ergebnis.positions[0].valid_from,
                         datetime.date(2026, 3, 4))

    def test_70_belegte_spalte_schlaegt_das_profil(self) -> None:
        profile = VendorProfile(profile_id="p1", date_order="month_first")
        extractor = TableExtractor(Settings(), profile=profile)
        document = RawDocument(source_kind=SourceKind.EXCEL)
        document.tables.append(self._block())
        ergebnis = extractor.extract(document)
        self.assertEqual(ergebnis.positions[0].valid_from,
                         datetime.date(2026, 4, 3))

    def test_71_vertauschte_korrektur_wird_gelernt(self) -> None:
        original = Offer(offer_date=datetime.date(2026, 4, 3))
        korrigiert = Offer(offer_date=datetime.date(2026, 3, 4))
        profile = VendorProfile(profile_id="p1")
        learn_from_corrections(original, korrigiert, dokument_mit_text("Angebot"),
                               profile)
        self.assertEqual(profile.date_order, "month_first")

    def test_72_andere_korrektur_lernt_keine_reihenfolge(self) -> None:
        original = Offer(offer_date=datetime.date(2026, 4, 3))
        korrigiert = Offer(offer_date=datetime.date(2026, 5, 20))
        profile = VendorProfile(profile_id="p1")
        learn_from_corrections(original, korrigiert, dokument_mit_text("Angebot"),
                               profile)
        self.assertEqual(profile.date_order, "auto")

    def test_73_reihenfolge_ist_serialisierbar(self) -> None:
        profile = VendorProfile(profile_id="p1", date_order="month_first")
        self.assertEqual(VendorProfile.from_dict(profile.to_dict()).date_order,
                         "month_first")

    def test_74_reihenfolge_laesst_sich_vergessen(self) -> None:
        profile = VendorProfile(profile_id="p1", date_order="month_first")
        self.assertTrue(forget_rule(profile, "date_order:month_first"))
        self.assertEqual(profile.date_order, "auto")

    def test_75_gelerntes_wird_im_klartext_beschrieben(self) -> None:
        profile = VendorProfile(profile_id="p1", date_order="month_first")
        self.assertTrue(any("Monat vor Tag" in z for z in describe_learning(profile)))

    def test_76_day_first_flag_des_profils(self) -> None:
        self.assertIsNone(VendorProfile().day_first)
        self.assertIs(VendorProfile(date_order="day_first").day_first, True)
        self.assertIs(VendorProfile(date_order="month_first").day_first, False)


# ==========================================================================
# 5. Toleranzen als Profilparameter
# ==========================================================================

class ToleranzTests(unittest.TestCase):
    """Zeilen-/Spaltentoleranz: Standard aus den Einstellungen, Profil sticht."""

    def test_80_standardwerte_kommen_aus_den_einstellungen(self) -> None:
        settings = Settings()
        settings.extraction.pdf_y_tolerance_factor = 0.9
        settings.extraction.pdf_x_bin = 7.0
        self.assertEqual(tolerances_for(settings), (0.9, 7.0))

    def test_81_ohne_angabe_gelten_die_auslieferungswerte(self) -> None:
        self.assertEqual(tolerances_for(), (ExtractionSettings().pdf_y_tolerance_factor,
                                            ExtractionSettings().pdf_x_bin))

    def test_82_profil_ueberschreibt_die_toleranz(self) -> None:
        profile = VendorProfile(profile_id="p1",
                                table_hints={"y_tolerance_factor": 0.35, "x_bin": 9.0})
        self.assertEqual(tolerances_for(Settings(), profile), (0.35, 9.0))

    def test_83_unsinnige_profilwerte_aendern_nichts(self) -> None:
        profile = VendorProfile(profile_id="p1",
                                table_hints={"y_tolerance_factor": 0, "x_bin": "viel"})
        self.assertEqual(tolerances_for(Settings(), profile),
                         (ExtractionSettings().pdf_y_tolerance_factor,
                          ExtractionSettings().pdf_x_bin))

    def test_84_leser_uebernimmt_die_werte(self) -> None:
        settings = Settings()
        settings.extraction.pdf_y_tolerance_factor = 0.8
        profile = VendorProfile(profile_id="p1", table_hints={"x_bin": 6.0})
        reader = PdfReader(settings=settings, profile=profile)
        self.assertEqual((reader.y_tolerance_factor, reader.x_bin), (0.8, 6.0))

    def test_85_ausdrueckliches_argument_schlaegt_alles(self) -> None:
        reader = PdfReader(y_tolerance_factor=0.25, x_bin=3.0, settings=Settings())
        self.assertEqual((reader.y_tolerance_factor, reader.x_bin), (0.25, 3.0))

    def test_86_enge_toleranz_trennt_dicht_gesetzte_zeilen(self) -> None:
        eng = [(100.0, [(30.0, "47110001"), (150.0, "Dichtring"), (330.0, "12,85")]),
               (106.0, [(30.0, "47110002"), (150.0, "O-Ring"), (330.0, "8,90")]),
               (112.0, [(30.0, "47110003"), (150.0, "Flachdichtung"), (330.0, "3,40")])]
        weit = words_to_tables(worte(eng), page=1, y_tolerance_factor=1.5)
        streng = words_to_tables(worte(eng), page=1, y_tolerance_factor=0.2)
        self.assertTrue(streng)
        self.assertGreater(streng[0].row_count, weit[0].row_count if weit else 0)

    def test_87_profil_kann_das_linienverfahren_abschalten(self) -> None:
        profile = VendorProfile(profile_id="p1", table_hints={"use_lattice": False})
        self.assertFalse(PdfReader(settings=Settings(), profile=profile).use_lattice)

    def test_88_mindestzahl_der_linien_ist_einstellbar(self) -> None:
        settings = Settings()
        settings.extraction.pdf_min_vertical_lines = 8
        self.assertEqual(PdfReader(settings=settings).min_vertical_lines, 8)

    def test_89_toleranzen_stehen_in_der_konfigurationsdatei(self) -> None:
        ziel = Path(_TEMP_HOME) / "settings_tuning.json"
        settings = Settings()
        settings.extraction.pdf_x_bin = 5.5
        settings.save(ziel)
        self.assertEqual(Settings.load(ziel).extraction.pdf_x_bin, 5.5)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
