"""Kreuzpruefungen, geharteter Preisparser und Konfidenz (unittest).

Der Leitgedanke ist ueberall derselbe wie im uebrigen Projekt: **lieber
weniger erkennen als falsch erkennen.**  Entsprechend pruefen diese Tests
nicht nur, ob eine Warnung *kommt*, sondern mindestens ebenso oft, ob sie
korrekt *ausbleibt* -- eine Falschmeldung kostet den Einkaeufer genauso viel
Zeit wie ein uebersehener Fehler.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_plausi_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import FieldOrigin, IssueSeverity, SourceKind     # noqa: E402
from app.models.offer import Offer                                      # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.services.extraction.confidence import (                        # noqa: E402
    BASE_CONFIDENCE,
    PATH_FREETEXT,
    PATH_TABLE_HEADER,
    PATH_TABLE_NO_HEADER,
    apply_confidence,
    compute_confidence,
)
from app.services.extraction.plausibility import (                      # noqa: E402
    CODE_DOCUMENT_TOTAL_MISMATCH,
    CODE_LINE_TOTAL_MISMATCH,
    CODE_POSITION_DUPLICATE,
    CODE_POSITION_GAP,
    CODE_PRICE_DERIVED,
    CODE_PRICE_OUTLIER,
    CODE_VALUE_DERIVABLE,
    check_document_total,
    check_line_total,
    check_line_totals,
    check_position_numbers,
    check_price_outliers,
    expected_line_total,
    find_document_total,
    position_value,
    run_checks,
)
from app.services.extraction.price_parsing import (                     # noqa: E402
    is_carryover_row,
    is_page_noise_row,
    parse_price_text,
)
from app.services.extraction.table_extractor import TableExtractor      # noqa: E402
from app.services.readers.base import RawDocument, TableBlock           # noqa: E402


# ==========================================================================
# Hilfsmittel
# ==========================================================================

def dez(text: str | int) -> Decimal:
    return Decimal(str(text))


def position(nummer: str = "10", menge: str | None = "100",
             preis: str | None = "12,85", zeilensumme: str | None = None,
             preiseinheit: int | None = 1, material: str = "47110001",
             waehrung: str = "EUR", quelle: str = "Tabelle 'Preise'",
             ) -> OfferPosition:
    """Position von Hand bauen -- so wie es die uebrigen Tests auch tun."""
    p = OfferPosition()
    p.source_kind = SourceKind.EXCEL
    p.source_hint = f"{quelle}, Zeile 5"
    p.extraction_path = PATH_TABLE_HEADER
    if nummer:
        p.set_field("position_number", nummer, FieldOrigin.EXTRACTED)
    if material:
        p.set_field("material_number", material, FieldOrigin.EXTRACTED)
    if menge is not None:
        p.set_field("quantity", dez(menge.replace(",", ".")), FieldOrigin.EXTRACTED)
    if preis is not None:
        p.set_field("price", dez(preis.replace(".", "").replace(",", ".")),
                    FieldOrigin.EXTRACTED)
    if preiseinheit is not None:
        p.set_field("price_unit", preiseinheit, FieldOrigin.EXTRACTED)
    if waehrung:
        p.set_field("currency", waehrung, FieldOrigin.EXTRACTED)
    if zeilensumme is not None:
        p.line_total = dez(zeilensumme.replace(".", "").replace(",", "."))
    return p


def angebot(*positionen: OfferPosition, text: str = "") -> Offer:
    offer = Offer()
    offer.currency = "EUR"
    offer.raw_text = text
    offer.positions.extend(positionen)
    return offer


def codes(traeger) -> set[str]:
    return {issue.code for issue in traeger.issues}


def tabelle(zeilen: list[list[str]], titel: str = "Preise") -> RawDocument:
    return RawDocument(
        source_path=f"{titel}.xlsx",
        source_kind=SourceKind.EXCEL,
        tables=[TableBlock(rows=[list(z) for z in zeilen], origin="excel",
                           title=titel)],
    )


def erste_position(zeilen: list[list[str]]) -> OfferPosition:
    ergebnis = TableExtractor(Settings()).extract(tabelle(zeilen))
    if not ergebnis.positions:
        raise AssertionError(f"keine Position erkannt: {ergebnis.notes}")
    return ergebnis.positions[0]


# ==========================================================================
# a) Zeilensumme gegen Menge x Preis
# ==========================================================================

class ZeilensummeTest(unittest.TestCase):
    """Die Gesamtpreisspalte ist der beste Zeuge fuer Menge und Preis."""

    def test_rechnung_geht_auf(self):
        p = position(menge="100", preis="12,85", zeilensumme="1.285,00")
        self.assertEqual(check_line_total(p), [])
        self.assertNotIn(CODE_LINE_TOTAL_MISMATCH, codes(p))

    def test_bestaetigung_wird_als_grund_vermerkt(self):
        p = position(menge="100", preis="12,85", zeilensumme="1.285,00")
        check_line_total(p)
        self.assertTrue(any("Zeilensumme bestaetigt" in g
                            for g in p.confidence_reasons))

    def test_kleine_rundung_bleibt_still(self):
        # 3 x 3,3333 = 9,9999 -- der Beleg rundet auf 10,00
        p = position(menge="3", preis="3,3333", zeilensumme="10,00")
        self.assertEqual(check_line_total(p), [])

    def test_abweichung_wird_gemeldet(self):
        p = position(nummer="20", menge="500", preis="12,85",
                     zeilensumme="642,50")
        notizen = check_line_total(p)
        self.assertEqual(len(notizen), 1)
        self.assertIn(CODE_LINE_TOTAL_MISMATCH, codes(p))

    def test_meldung_nennt_beide_betraege(self):
        p = position(nummer="20", menge="500", preis="12,85",
                     zeilensumme="642,50")
        text = check_line_total(p)[0]
        self.assertIn("Position 20", text)
        self.assertIn("6.425,00", text)
        self.assertIn("642,50", text)

    def test_abweichung_macht_menge_und_preis_unsicher(self):
        p = position(menge="500", preis="12,85", zeilensumme="642,50")
        check_line_total(p)
        self.assertEqual(p.origin("quantity"), FieldOrigin.UNCERTAIN)
        self.assertEqual(p.origin("price"), FieldOrigin.UNCERTAIN)

    def test_abweichung_nennt_vorschlag_ohne_zu_aendern(self):
        p = position(menge="500", preis="12,85", zeilensumme="642,50")
        text = check_line_total(p)[0]
        self.assertIn("Vorschlag", text)
        # nichts wurde angefasst
        self.assertEqual(p.price, dez("12.85"))
        self.assertEqual(p.quantity, dez("500"))

    def test_befund_ist_nicht_blockierend(self):
        p = position(menge="500", preis="12,85", zeilensumme="642,50")
        check_line_total(p)
        befund = next(i for i in p.issues if i.code == CODE_LINE_TOTAL_MISMATCH)
        self.assertFalse(befund.blocking)
        self.assertIs(befund.severity, IssueSeverity.WARNING)

    def test_ohne_gesamtspalte_keine_pruefung(self):
        p = position(menge="500", preis="12,85", zeilensumme=None)
        self.assertEqual(check_line_total(p), [])
        self.assertEqual(codes(p), set())

    def test_preiseinheit_wird_beruecksichtigt(self):
        # 500 St zu 12,85 je 100 St = 64,25
        p = position(menge="500", preis="12,85", preiseinheit=100,
                     zeilensumme="64,25")
        self.assertEqual(check_line_total(p), [])

    def test_uebersehene_preiseinheit_faellt_auf(self):
        p = position(menge="500", preis="12,85", preiseinheit=1,
                     zeilensumme="64,25")
        check_line_total(p)
        self.assertIn(CODE_LINE_TOTAL_MISMATCH, codes(p))

    def test_toleranzgrenze_gerade_noch_in_ordnung(self):
        # 1 % von 1000,00 plus 2 Cent
        p = position(menge="100", preis="10,00", zeilensumme="1.010,00")
        self.assertEqual(check_line_total(p), [])

    def test_knapp_ueber_der_toleranz_schlaegt_an(self):
        p = position(menge="100", preis="10,00", zeilensumme="1.100,00")
        check_line_total(p)
        self.assertIn(CODE_LINE_TOTAL_MISMATCH, codes(p))

    def test_fehlender_preis_wird_gerechnet(self):
        p = position(menge="100", preis=None, zeilensumme="1.285,00")
        notizen = check_line_total(p)
        self.assertEqual(p.price, dez("12.8500"))
        self.assertEqual(len(notizen), 1)

    def test_gerechneter_preis_ist_unsicher(self):
        p = position(menge="100", preis=None, zeilensumme="1.285,00")
        check_line_total(p)
        self.assertEqual(p.origin("price"), FieldOrigin.UNCERTAIN)
        self.assertIn(CODE_PRICE_DERIVED, codes(p))

    def test_gerechneter_preis_begruendet_sich(self):
        p = position(menge="100", preis=None, zeilensumme="1.285,00")
        text = check_line_total(p)[0]
        self.assertIn("gerechnet, nicht geraten", text)

    def test_gerechneter_preis_mit_preiseinheit(self):
        p = position(menge="500", preis=None, preiseinheit=100,
                     zeilensumme="64,25")
        check_line_total(p)
        self.assertEqual(p.price, dez("12.8500"))

    def test_fehlende_menge_wird_nur_vorgeschlagen(self):
        p = position(menge=None, preis="12,85", zeilensumme="1.285,00")
        notizen = check_line_total(p)
        self.assertIsNone(p.quantity)          # nichts erfunden
        self.assertIn("100", notizen[0])
        self.assertIn(CODE_VALUE_DERIVABLE, codes(p))

    def test_menge_und_preis_fehlen_keine_aussage(self):
        p = position(menge=None, preis=None, zeilensumme="1.285,00")
        self.assertEqual(check_line_total(p), [])
        self.assertEqual(codes(p), set())

    def test_menge_null_stuerzt_nicht_ab(self):
        p = position(menge="0", preis=None, zeilensumme="1.285,00")
        self.assertEqual(check_line_total(p), [])

    def test_alle_positionen_werden_geprueft(self):
        offer = angebot(position(nummer="10", zeilensumme="1.285,00"),
                        position(nummer="20", menge="500", preis="12,85",
                                 zeilensumme="642,50"))
        notizen = check_line_totals(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn("Position 20", notizen[0])

    def test_erwartete_zeilensumme(self):
        p = position(menge="500", preis="12,85", preiseinheit=100)
        self.assertEqual(expected_line_total(p), dez("64.25"))
        self.assertIsNone(expected_line_total(position(menge=None, preis=None)))


# ==========================================================================
# b) Belegsumme gegen Summe der Positionen
# ==========================================================================

class BelegsummeTest(unittest.TestCase):
    """Die wertvollste Pruefung: sie findet *fehlende* Positionen."""

    def test_summe_stimmt(self):
        offer = angebot(position(nummer="10", zeilensumme="1.285,00"),
                        position(nummer="20", menge="10", preis="20,00",
                                 zeilensumme="200,00"),
                        text="Gesamtsumme: 1.485,00 EUR")
        self.assertEqual(check_document_total(offer), [])
        self.assertEqual(codes(offer), set())

    def test_fehlende_position_faellt_auf(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        position(nummer="20", menge="10", preis="321,00"),
                        text="Gesamtsumme: 4.855,00 EUR")
        notizen = check_document_total(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn(CODE_DOCUMENT_TOTAL_MISMATCH, codes(offer))

    def test_meldung_nennt_differenz_und_vermutung(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        position(nummer="20", menge="10", preis="321,00"),
                        text="Gesamtsumme: 4.855,00 EUR")
        text = check_document_total(offer)[0]
        self.assertIn("4.210,00", text)
        self.assertIn("4.855,00", text)
        self.assertIn("645,00", text)
        self.assertIn("uebersehen", text)

    def test_zu_viel_erkannt_wird_anders_formuliert(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        position(nummer="20", menge="10", preis="100,00"),
                        text="Gesamtsumme: 1.000,00 EUR")
        text = check_document_total(offer)[0]
        self.assertIn("zu viel erkannt", text)

    def test_ohne_belegsumme_keine_pruefung(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        text="Vielen Dank fuer Ihre Anfrage.")
        self.assertEqual(check_document_total(offer), [])
        self.assertEqual(codes(offer), set())

    def test_zeilensumme_des_belegs_hat_vorrang(self):
        # Die Zeilensumme steht im Beleg -- sie zaehlt, nicht die Rechnung.
        p = position(nummer="10", menge="10", preis="100,00",
                     zeilensumme="1.100,00")
        offer = angebot(p, text="Gesamtsumme: 1.100,00 EUR")
        self.assertEqual(check_document_total(offer), [])

    def test_position_ohne_wert_fuehrt_zur_teilpruefung(self):
        # Frueher wurde die Pruefung komplett uebersprungen -- jetzt werden
        # die uebrigen Positionen trotzdem gegen die Belegsumme gehalten.
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        position(nummer="20", menge=None, preis=None),
                        text="Gesamtsumme: 1.000,00 EUR")
        notizen = check_document_total(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn("teilweise geprueft", notizen[0])
        self.assertNotIn(CODE_DOCUMENT_TOTAL_MISMATCH, codes(offer))

    def test_teilsumme_ueber_belegsumme_ist_ein_befund(self):
        # Schon die Positionen MIT Preis uebersteigen die Belegsumme -- das
        # muss trotz fehlender Preise als Widerspruch gemeldet werden.
        offer = angebot(position(nummer="10", menge="10", preis="200,00"),
                        position(nummer="20", menge=None, preis=None),
                        text="Gesamtsumme: 1.000,00 EUR")
        notizen = check_document_total(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn("UEBER", notizen[0])
        self.assertIn(CODE_DOCUMENT_TOTAL_MISMATCH, codes(offer))

    def test_staffelzeilen_zaehlen_nicht_doppelt(self):
        staffel = position(nummer="10", menge="500", preis="10,00")
        staffel.set_field("remarks", "Staffelpreis zur vorherigen Position",
                          FieldOrigin.EXTRACTED)
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        staffel, text="Gesamtsumme: 1.000,00 EUR")
        self.assertEqual(check_document_total(offer), [])

    def test_toleranz_bei_der_belegsumme(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"),
                        text="Gesamtsumme: 1.005,00 EUR")
        self.assertEqual(check_document_total(offer), [])

    def test_bestaetigte_summe_wird_als_grund_vermerkt(self):
        p = position(nummer="10", menge="10", preis="100,00")
        offer = angebot(p, text="Nettosumme 1.000,00 EUR")
        check_document_total(offer)
        self.assertTrue(any("Belegsumme bestaetigt" in g
                            for g in p.confidence_reasons))

    def test_text_kann_uebergeben_werden(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00"))
        self.assertEqual(check_document_total(offer, "Gesamtsumme 1.000,00"), [])


class BelegsummeFindenTest(unittest.TestCase):
    """Welche Zahl im Text ist ueberhaupt die Belegsumme?"""

    def test_gesamtsumme(self):
        self.assertEqual(find_document_total("Gesamtsumme: 4.855,00 EUR")[0],
                         dez("4855.00"))

    def test_nettosumme(self):
        self.assertEqual(find_document_total("Nettosumme  1.234,56")[0],
                         dez("1234.56"))

    def test_auftragswert(self):
        self.assertEqual(find_document_total("Auftragswert EUR 900,00")[0],
                         dez("900.00"))

    def test_englische_beschriftung(self):
        self.assertEqual(find_document_total("Total amount: 1,234.56 USD")[0],
                         dez("1234.56"))

    def test_netto_gewinnt_gegen_brutto(self):
        text = "Zwischensumme 1.000,00\nNetto gesamt 950,00\nBrutto 1.130,50"
        betrag, _ = find_document_total(text)
        self.assertEqual(betrag, dez("950.00"))

    def test_ohne_treffer_nichts(self):
        self.assertIsNone(find_document_total("Wir danken fuer Ihre Anfrage."))
        self.assertIsNone(find_document_total(""))

    def test_beschriftung_wird_mitgeliefert(self):
        _, label = find_document_total("Gesamtsumme: 100,00")
        self.assertIn("gesamtsumme", label.lower())

    def test_positionswert(self):
        p = position(menge="10", preis="5,00")
        self.assertEqual(position_value(p), dez("50.00"))
        p.line_total = dez("60")
        self.assertEqual(position_value(p), dez("60"))


# ==========================================================================
# c) Positionsnummern-Folge
# ==========================================================================

class PositionsnummernTest(unittest.TestCase):

    def test_luecke_wird_gemeldet(self):
        offer = angebot(position(nummer="10"), position(nummer="20"),
                        position(nummer="40"))
        notizen = check_position_numbers(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn("30", notizen[0])
        self.assertIn(CODE_POSITION_GAP, codes(offer))

    def test_lueckenlose_folge_bleibt_still(self):
        offer = angebot(position(nummer="10"), position(nummer="20"),
                        position(nummer="30"), position(nummer="40"))
        self.assertEqual(check_position_numbers(offer), [])

    def test_nichts_wird_ergaenzt(self):
        offer = angebot(position(nummer="10"), position(nummer="20"),
                        position(nummer="40"))
        check_position_numbers(offer)
        self.assertEqual(len(offer.positions), 3)

    def test_zwei_positionen_sind_kein_muster(self):
        offer = angebot(position(nummer="10"), position(nummer="30"))
        self.assertEqual(check_position_numbers(offer), [])

    def test_unregelmaessige_nummern_erzeugen_keine_falschmeldung(self):
        offer = angebot(position(nummer="1"), position(nummer="2"),
                        position(nummer="7"))
        self.assertEqual(check_position_numbers(offer), [])

    def test_selbst_vergebene_nummern_zaehlen_nicht(self):
        offer = angebot(position(nummer="10"), position(nummer="20"),
                        position(nummer="40"))
        for p in offer.positions:
            p.field_origins["position_number"] = FieldOrigin.DEFAULT
        self.assertEqual(check_position_numbers(offer), [])

    def test_dublette_wird_gemeldet(self):
        offer = angebot(position(nummer="10"), position(nummer="10"),
                        position(nummer="20"))
        notizen = check_position_numbers(offer)
        self.assertTrue(any(CODE_POSITION_DUPLICATE == i.code for i in offer.issues))
        self.assertIn("10", notizen[0])

    def test_staffel_mit_gleicher_nummer_ist_keine_dublette(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="10", preis="11,90"),
                        position(nummer="20"))
        self.assertNotIn(CODE_POSITION_DUPLICATE, codes(offer))

    def test_getrennte_quellen_stoeren_sich_nicht(self):
        # Mail und Anhang fangen beide bei 10 an -- das ist keine Dublette.
        offer = angebot(position(nummer="10", quelle="Mailtext"),
                        position(nummer="20", quelle="Mailtext"),
                        position(nummer="10", quelle="Anhang"),
                        position(nummer="20", quelle="Anhang"))
        self.assertEqual(check_position_numbers(offer), [])

    def test_luecke_senkt_die_konfidenz(self):
        offer = angebot(position(nummer="10"), position(nummer="20"),
                        position(nummer="40"))
        check_position_numbers(offer)
        self.assertTrue(any("Luecke" in g
                            for g in offer.positions[0].confidence_reasons))

    def test_nicht_numerische_nummern_werden_ignoriert(self):
        offer = angebot(position(nummer="10a"), position(nummer="20b"),
                        position(nummer="40c"))
        self.assertEqual(check_position_numbers(offer), [])


# ==========================================================================
# d) Preisgroessenordnung
# ==========================================================================

class PreisausreisserTest(unittest.TestCase):

    def test_ausreisser_nach_oben(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="11,90"),
                        position(nummer="30", preis="13,40"),
                        position(nummer="40", preis="1.890,00"))
        notizen = check_price_outliers(offer)
        self.assertEqual(len(notizen), 1)
        self.assertIn(CODE_PRICE_OUTLIER, codes(offer.positions[3]))

    def test_ausreisser_wird_unsicher_markiert(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="11,90"),
                        position(nummer="30", preis="13,40"),
                        position(nummer="40", preis="1.890,00"))
        check_price_outliers(offer)
        self.assertEqual(offer.positions[3].origin("price"),
                         FieldOrigin.UNCERTAIN)
        self.assertEqual(offer.positions[0].origin("price"),
                         FieldOrigin.EXTRACTED)

    def test_ausreisser_wird_nicht_korrigiert(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="11,90"),
                        position(nummer="30", preis="13,40"),
                        position(nummer="40", preis="1.890,00"))
        check_price_outliers(offer)
        self.assertEqual(offer.positions[3].price, dez("1890.00"))

    def test_ausreisser_nach_unten(self):
        offer = angebot(position(nummer="10", preis="1285,00"),
                        position(nummer="20", preis="1190,00"),
                        position(nummer="30", preis="1340,00"),
                        position(nummer="40", preis="0,50"))
        check_price_outliers(offer)
        self.assertIn(CODE_PRICE_OUTLIER, codes(offer.positions[3]))

    def test_normale_streuung_bleibt_still(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="289,00"),
                        position(nummer="30", preis="3,40"),
                        position(nummer="40", preis="450,00"))
        self.assertEqual(check_price_outliers(offer), [])

    def test_zu_wenige_positionen_keine_aussage(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="9000,00"))
        self.assertEqual(check_price_outliers(offer), [])

    def test_preiseinheit_wird_vor_dem_vergleich_bereinigt(self):
        # 1285,00 je 100 St = 12,85 je St -> kein Ausreisser
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="11,90"),
                        position(nummer="30", preis="13,40"),
                        position(nummer="40", preis="1.890,00", preiseinheit=100))
        self.assertEqual(check_price_outliers(offer), [])

    def test_meldung_nennt_den_verdacht(self):
        offer = angebot(position(nummer="10", preis="12,85"),
                        position(nummer="20", preis="11,90"),
                        position(nummer="30", preis="13,40"),
                        position(nummer="40", preis="1.890,00"))
        text = check_price_outliers(offer)[0]
        self.assertIn("Dezimaltrenner", text)


# ==========================================================================
# Gesamtlauf
# ==========================================================================

class GesamtlaufTest(unittest.TestCase):

    def test_run_checks_schreibt_notizen_ins_angebot(self):
        offer = angebot(position(nummer="10", menge="500", preis="12,85",
                                 zeilensumme="642,50"),
                        text="Gesamtsumme 9.999,00 EUR")
        notizen = run_checks(offer)
        self.assertTrue(notizen)
        for note in notizen:
            self.assertIn(note, offer.extraction_notes)

    def test_run_checks_bleibt_bei_sauberem_beleg_still(self):
        offer = angebot(position(nummer="10", menge="10", preis="100,00",
                                 zeilensumme="1.000,00"),
                        position(nummer="20", menge="5", preis="200,00",
                                 zeilensumme="1.000,00"),
                        text="Gesamtsumme 2.000,00 EUR")
        self.assertEqual(run_checks(offer), [])

    def test_run_checks_blockiert_nie(self):
        offer = angebot(position(nummer="10", menge="500", preis="12,85",
                                 zeilensumme="642,50"),
                        text="Gesamtsumme 9.999,00 EUR")
        run_checks(offer)
        self.assertFalse(offer.issues.has_blocking)
        self.assertFalse(any(p.issues.has_blocking for p in offer.positions))

    def test_run_checks_ohne_positionen(self):
        self.assertEqual(run_checks(Offer()), [])


# ==========================================================================
# Aufgabe 2: geharteter Preisparser
# ==========================================================================

class PreisParserTest(unittest.TestCase):

    def test_preis_je_hundert_stueck(self):
        info = parse_price_text("12,85 EUR/100 St")
        self.assertEqual(info.price, dez("12.85"))
        self.assertEqual(info.price_unit, 100)
        self.assertEqual(info.currency, "EUR")
        self.assertEqual(info.uom, "ST")

    def test_englische_schreibweise(self):
        info = parse_price_text("EUR 4.50 per 1000 pcs")
        self.assertEqual(info.price, dez("4.50"))
        self.assertEqual(info.price_unit, 1000)

    def test_einheit_vor_dem_preis(self):
        info = parse_price_text("je 100 Stueck 12,85")
        self.assertEqual(info.price, dez("12.85"))
        self.assertEqual(info.price_unit, 100)

    def test_preis_je_meter(self):
        info = parse_price_text("0,50 EUR / 10 m")
        self.assertEqual(info.price, dez("0.50"))
        self.assertEqual(info.price_unit, 10)
        self.assertEqual(info.uom, "M")

    def test_schlichter_preis(self):
        info = parse_price_text("1.234,56")
        self.assertEqual(info.price, dez("1234.56"))
        self.assertIsNone(info.price_unit)
        self.assertFalse(info.is_range)

    def test_waehrung_je_position(self):
        info = parse_price_text("12,85 USD")
        self.assertEqual(info.currency, "USD")
        self.assertEqual(info.price, dez("12.85"))

    def test_preisspanne_liefert_keinen_preis(self):
        info = parse_price_text("12,00 - 14,00 EUR")
        self.assertTrue(info.is_range)
        self.assertIsNone(info.price)
        self.assertEqual(info.range_low, dez("12.00"))
        self.assertEqual(info.range_high, dez("14.00"))

    def test_preisspanne_erklaert_sich(self):
        info = parse_price_text("12,00 - 14,00 EUR")
        self.assertTrue(info.notes)
        self.assertIn("Preisspanne", info.notes[0])

    def test_preisspanne_ausgeschrieben(self):
        info = parse_price_text("zwischen 12,00 und 14,00 EUR")
        self.assertTrue(info.is_range)

    def test_abmessung_ist_keine_preisspanne(self):
        info = parse_price_text("40-52-7")
        self.assertFalse(info.is_range)

    def test_leere_zelle(self):
        info = parse_price_text("")
        self.assertFalse(info.usable)
        self.assertFalse(info.is_range)

    def test_text_ohne_zahl(self):
        self.assertFalse(parse_price_text("auf Anfrage").usable)


class ZeilenfilterTest(unittest.TestCase):

    def test_seitenzahl_ist_keine_position(self):
        self.assertTrue(is_page_noise_row(["Seite 2 von 3", ""]))
        self.assertTrue(is_page_noise_row(["Blatt 2"]))
        self.assertTrue(is_page_noise_row(["Page 2 of 3"]))

    def test_echte_zeile_ist_keine_fusszeile(self):
        self.assertFalse(is_page_noise_row(["10", "4711", "Dichtring", "12,85"]))

    def test_uebertrag_erkannt(self):
        self.assertTrue(is_carryover_row(["Uebertrag", "", "1.200,00"]))
        self.assertTrue(is_carryover_row(["Uebertrag von Seite 1", "500,00"]))

    def test_kein_uebertrag(self):
        self.assertFalse(is_carryover_row(["10", "Dichtring", "12,85"]))


class TabellenIntegrationTest(unittest.TestCase):
    """Die neuen Regeln muessen auch im Tabellenextraktor greifen."""

    def test_preiseinheit_aus_der_preiszelle(self):
        p = erste_position([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85 EUR/100 St"],
        ])
        self.assertEqual(p.price, dez("12.85"))
        self.assertEqual(p.price_unit, 100)

    def test_preisspanne_bleibt_leer(self):
        p = erste_position([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,00 - 14,00 EUR"],
        ])
        self.assertIsNone(p.price)
        self.assertEqual(p.origin("price"), FieldOrigin.UNCERTAIN)

    def test_eigene_waehrung_je_position(self):
        ergebnis = TableExtractor(Settings()).extract(tabelle([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85 EUR"],
            ["20", "47110002", "O-Ring", "100", "8,90 USD"],
        ]))
        self.assertEqual(ergebnis.positions[0].currency, "EUR")
        self.assertEqual(ergebnis.positions[1].currency, "USD")

    def test_mehrzeilige_kopfzeile_wird_zusammengefuehrt(self):
        # Der Extraktor kann das bereits -- hier festgeschrieben.
        p = erste_position([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["", "", "", "St", "EUR/100 St"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
        ])
        self.assertEqual(p.material_number, "47110001")
        self.assertEqual(p.price, dez("12.85"))
        # Die zweite Kopfzeile traegt die Preiseinheit -- sie darf nicht
        # verloren gehen, sonst ist der Preis um Faktor 100 falsch.
        self.assertEqual(p.price_unit, 100)

    def test_wiederholte_kopfzeile_wird_uebersprungen(self):
        ergebnis = TableExtractor(Settings()).extract(tabelle([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["20", "47110002", "O-Ring", "100", "8,90"],
        ]))
        self.assertEqual(len(ergebnis.positions), 2)

    def test_seitenfusszeile_wird_uebersprungen(self):
        ergebnis = TableExtractor(Settings()).extract(tabelle([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
            ["Seite 2 von 3", "", "", "", ""],
            ["20", "47110002", "O-Ring", "100", "8,90"],
        ]))
        self.assertEqual(len(ergebnis.positions), 2)
        self.assertTrue(any("Fusszeile" in n for n in ergebnis.notes))

    def test_uebertragszeile_wird_uebersprungen(self):
        ergebnis = TableExtractor(Settings()).extract(tabelle([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis"],
            ["10", "47110001", "Dichtring", "500", "12,85"],
            ["Uebertrag", "", "", "", "6.425,00"],
            ["20", "47110002", "O-Ring", "100", "8,90"],
        ]))
        self.assertEqual(len(ergebnis.positions), 2)

    def test_gesamtspalte_landet_in_der_zeilensumme(self):
        p = erste_position([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis", "Gesamt"],
            ["10", "47110001", "Dichtring", "500", "12,85", "6.425,00"],
        ])
        self.assertEqual(p.line_total, dez("6425.00"))

    def test_gesamtspalte_deckt_lesefehler_auf(self):
        ergebnis = TableExtractor(Settings()).extract(tabelle([
            ["Pos", "Material", "Bezeichnung", "Menge", "Preis", "Gesamt"],
            ["10", "47110001", "Dichtring", "500", "12,85", "642,50"],
        ]))
        offer = angebot(*ergebnis.positions)
        self.assertTrue(check_line_totals(offer))


# ==========================================================================
# Aufgabe 3: Konfidenz
# ==========================================================================

class KonfidenzTest(unittest.TestCase):

    def test_tabelle_mit_kopfzeile_ist_am_hoechsten(self):
        p = position()
        self.assertEqual(compute_confidence(p, PATH_TABLE_HEADER),
                         BASE_CONFIDENCE[PATH_TABLE_HEADER])

    def test_tabelle_ohne_kopfzeile_liegt_darunter(self):
        mit = compute_confidence(position(), PATH_TABLE_HEADER)
        ohne = compute_confidence(position(), PATH_TABLE_NO_HEADER)
        self.assertLess(ohne, mit)

    def test_fliesstext_liegt_am_niedrigsten(self):
        ohne = compute_confidence(position(), PATH_TABLE_NO_HEADER)
        frei = compute_confidence(position(), PATH_FREETEXT)
        self.assertLess(frei, ohne)

    def test_unsicheres_feld_kostet(self):
        p = position()
        p.field_origins["price"] = FieldOrigin.UNCERTAIN
        self.assertLess(compute_confidence(p), BASE_CONFIDENCE[PATH_TABLE_HEADER])

    def test_mehr_unsichere_felder_kosten_mehr(self):
        eins = position()
        eins.field_origins["price"] = FieldOrigin.UNCERTAIN
        zwei = position()
        zwei.field_origins["price"] = FieldOrigin.UNCERTAIN
        zwei.field_origins["quantity"] = FieldOrigin.UNCERTAIN
        self.assertLess(compute_confidence(zwei), compute_confidence(eins))

    def test_fehlgeschlagene_kreuzpruefung_kostet(self):
        p = position(menge="500", preis="12,85", zeilensumme="642,50")
        check_line_total(p)
        self.assertLess(compute_confidence(p), 0.6)

    def test_bestaetigte_zeilensumme_bringt_zuschlag(self):
        ohne = compute_confidence(position(preis="12,85"))
        p = position(menge="100", preis="12,85", zeilensumme="1.285,00")
        check_line_total(p)
        self.assertGreater(compute_confidence(p), ohne)

    def test_fehlender_preis_kostet(self):
        self.assertLess(compute_confidence(position(preis=None)),
                        compute_confidence(position()))

    def test_fehlende_artikelangabe_kostet(self):
        p = position(material="")
        p.set_field("description", "", FieldOrigin.MISSING)
        self.assertLess(compute_confidence(p), compute_confidence(position()))

    def test_fehlende_waehrung_kostet(self):
        self.assertLess(compute_confidence(position(waehrung="")),
                        compute_confidence(position()))

    def test_gruende_werden_gefuehrt(self):
        p = position()
        compute_confidence(p)
        self.assertTrue(p.confidence_reasons)
        self.assertIn("Erkennungsweg", p.confidence_reasons[0])

    def test_gruende_nennen_das_unsichere_feld(self):
        p = position()
        p.field_origins["price"] = FieldOrigin.UNCERTAIN
        compute_confidence(p)
        self.assertTrue(any("price" in g for g in p.confidence_reasons))

    def test_wert_bleibt_zwischen_null_und_eins(self):
        p = position(menge=None, preis=None, material="", waehrung="")
        p.extraction_path = PATH_FREETEXT
        p.field_origins["quantity"] = FieldOrigin.UNCERTAIN
        p.field_origins["uom"] = FieldOrigin.UNCERTAIN
        wert = compute_confidence(p)
        self.assertGreaterEqual(wert, 0.0)
        self.assertLessEqual(wert, 1.0)

    def test_unbekannter_weg_bekommt_mittelwert(self):
        p = position()
        p.extraction_path = ""
        self.assertLess(compute_confidence(p), BASE_CONFIDENCE[PATH_TABLE_HEADER])

    def test_label_sicher(self):
        p = position()
        compute_confidence(p, PATH_TABLE_HEADER)
        self.assertEqual(p.confidence_label(), "sicher")

    def test_label_pruefen(self):
        p = position()
        p.confidence = 0.6
        self.assertEqual(p.confidence_label(), "pruefen")

    def test_label_unsicher(self):
        p = position()
        p.confidence = 0.2
        self.assertEqual(p.confidence_label(), "unsicher")

    def test_apply_confidence_fuer_alle_positionen(self):
        offer = angebot(position(nummer="10"), position(nummer="20"))
        apply_confidence(offer)
        self.assertTrue(all(p.confidence > 0 for p in offer.positions))

    def test_erneute_berechnung_verdoppelt_die_gruende_nicht(self):
        p = position()
        compute_confidence(p)
        erste = len(p.confidence_reasons)
        compute_confidence(p)
        self.assertEqual(len(p.confidence_reasons), erste)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
