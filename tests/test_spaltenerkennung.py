"""Regressionsnetz fuer die Spaltenerkennung echter Lieferantenangebote.

Grundlage sind die Kopfzeilen aus 22 tatsaechlich eingegangenen Angeboten
(anonymisiert, ohne echte Preise).  Genau an diesen Kopfzeilen ist die
Zuordnung frueher gescheitert -- am schlimmsten dort, wo die PREISSPALTE
verloren ging: eine Position ohne Preis ist fuer den Einkauf wertlos.

Geprueft wird deshalb je Kopfzeile: Menge UND Preis muessen zugeordnet
werden.  Dazu kommen gezielte Einzelfaelle (Trennstrich im Spaltenkopf,
Incoterm-Preisspalten, Zeilensumme, Rabatt, Liefertermin) und -- ebenso
wichtig -- Negativtests: echte Bindestrich-Woerter wie "E-Preis" oder
"Art.-Nr." duerfen durch die Trennstrich-Normalisierung NICHT kaputtgehen.
"""

from __future__ import annotations

import unittest

from app.config.settings import Settings
from app.services.extraction.table_extractor import (
    PSEUDO_ALT_QTY,
    PSEUDO_DELIVERY,
    PSEUDO_EXTRA,
    PSEUDO_IGNORE,
    PSEUDO_TOTAL,
    TableExtractor,
    dehyphenate_header,
    incoterm_price_header,
)
from app.services.readers.base import TableBlock


def _extractor() -> TableExtractor:
    return TableExtractor(Settings())


def _analyse(header: list[str], *rows: list[str]):
    """Kopfzeile plus Datenzeilen analysieren."""
    block = TableBlock(rows=[header, *rows], origin="excel")
    return _extractor().analyze(block.normalized())


def _felder(analysis) -> dict[str, str]:
    """Feld -> Ueberschrift (nur echte Felder, keine Pseudofelder)."""
    return {a.field: a.header for a in analysis.columns.values()
            if not a.field.startswith("_")}


def _pseudo(analysis) -> dict[str, str]:
    """Pseudofeld -> Ueberschrift."""
    return {a.field: a.header for a in analysis.columns.values()
            if a.field.startswith("_")}


# ----------------------------------------------------------------------
# Die echten Kopfzeilen aus den 22 Angeboten
# ----------------------------------------------------------------------

#: (Bezeichnung, Kopfzeile, Datenzeile)
ECHTE_KOPFZEILEN: list[tuple[str, list[str], list[str]]] = [
    (
        "Nr 8 -- Mengenstaffel ueber Spalten, Preis mit 'pro/WE/PE'",
        ["Pos.", "Bezeichnung", "Menge1", "Menge2", "Menge3", "ME", "ME", "ME",
         "Artikelpreis in", "pro", "WE", "PE", "Summe in EUR"],
        ["10", "Dichtring NBR 40x52x7", "100", "250", "500", "ST", "ST", "ST",
         "4,55", "1", "ST", "1", "455,00"],
    ),
    (
        "Nr 15 -- 'EP /ME' und 'GP'",
        ["Pos.", "Bezeichnung", "Menge RS", "EP /ME", "GP"],
        ["10", "Passfeder A 8x7x40", "250", "1,25", "312,50"],
    ),
    (
        "Nr 17 -- englischer Kopf mit Incoterm-Preisspalte",
        ["Pos.", "Article / Drawing / Description", "Quantity (pcs)",
         "DDP 12345 Musterstadt (EUR/ST)", "Subtotal (EUR)"],
        ["10", "Bracket 12-4455-A", "500", "3,80", "1900,00"],
    ),
    (
        "L-Termin",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "L-Termin"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "12.09.2026"],
    ),
    (
        "Leistungsdatum",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Leistungsdatum"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "12.09.2026"],
    ),
    (
        "Lieferwoche",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Lieferwoche"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "KW 38"],
    ),
    (
        "Liefer-woche (Trennstrich aus dem PDF)",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Liefer-woche"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "KW 38"],
    ),
    (
        "erwart. Versanddatum",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "erwart. Versanddatum"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "12.09.2026"],
    ),
    (
        "Rabattspalte '%'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "%"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "3"],
    ),
    (
        "Rabattspalte 'Rab.'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Rab."],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "3"],
    ),
    (
        "Rabattspalte 'P'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "P"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "3"],
    ),
    (
        "Zeilensumme 'PosWert'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "PosWert"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "236,00"],
    ),
    (
        "Zeilensumme 'Betrag'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Betrag"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "236,00"],
    ),
    (
        "Zeilensumme 'Wert EUR'",
        ["Pos.", "Bezeichnung", "Menge", "ME", "Preis", "Wert EUR"],
        ["10", "Kugellager 6203", "40", "ST", "5,90", "236,00"],
    ),
    (
        "Positionsnummer 'Pos.Nr.'",
        ["Pos.Nr.", "Bezeichnung", "Menge", "ME", "Preis"],
        ["10", "Kugellager 6203", "40", "ST", "5,90"],
    ),
    (
        "Zusatzangaben ohne SAP-Bezug (Werkstoff, Oberflaeche, Gewicht, Rahmenvertrag)",
        ["Pos.", "Bezeichnung", "Werkstoff", "Ober-flaeche", "Menge", "ME",
         "E-Preis", "Rahmen-vertrag / Stck.", "Netto-gewicht pro Stck. / Kg"],
        ["10", "Winkel 40x40", "1.4301", "gebeizt", "80", "ST", "2,15",
         "nein", "0,340"],
    ),
]


class EchteKopfzeilenTest(unittest.TestCase):
    """Fuer JEDE echte Kopfzeile muessen Menge und Preis ankommen."""

    def test_alle_kopfzeilen_liefern_menge_und_preis(self) -> None:
        for name, header, row in ECHTE_KOPFZEILEN:
            with self.subTest(kopfzeile=name):
                felder = _felder(_analyse(header, row))
                self.assertIn("quantity", felder,
                              f"Mengenspalte fehlt bei: {name}")
                self.assertIn("price", felder,
                              f"Preisspalte fehlt bei: {name}")

    def test_alle_kopfzeilen_sind_verwertbar(self) -> None:
        for name, header, row in ECHTE_KOPFZEILEN:
            with self.subTest(kopfzeile=name):
                self.assertTrue(_analyse(header, row).usable,
                                f"Struktur nicht verwertbar bei: {name}")

    def test_alle_kopfzeilen_finden_eine_beschreibung(self) -> None:
        for name, header, row in ECHTE_KOPFZEILEN:
            with self.subTest(kopfzeile=name):
                self.assertIn("description", _felder(_analyse(header, row)),
                              f"Bezeichnungsspalte fehlt bei: {name}")


class Angebot08Test(unittest.TestCase):
    """Nr 8: 'Menge1|Menge2|Menge3', dreimal 'ME', 'Artikelpreis in|pro|WE|PE'."""

    def setUp(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[0]
        self.analysis = _analyse(header, row)
        self.felder = _felder(self.analysis)

    def test_preisspalte_ist_artikelpreis(self) -> None:
        self.assertEqual(self.felder.get("price"), "Artikelpreis in")

    def test_artikelpreis_ist_kein_artikelnummernfeld(self) -> None:
        self.assertNotEqual(self.felder.get("vendor_material_number"),
                            "Artikelpreis in")

    def test_erste_mengenspalte_wird_uebernommen(self) -> None:
        self.assertEqual(self.felder.get("quantity"), "Menge1")

    def test_weitere_mengenspalten_gelten_als_staffel(self) -> None:
        self.assertIn(PSEUDO_ALT_QTY, _pseudo(self.analysis))

    def test_klartext_befund_zu_mehreren_mengenspalten(self) -> None:
        self.assertTrue(
            any("Mehrere Mengenspalten erkannt" in n for n in self.analysis.notes),
            f"Befund fehlt, Notizen: {self.analysis.notes}")

    def test_menge_bleibt_unsicher(self) -> None:
        spalte = next(a for a in self.analysis.columns.values()
                      if a.field == "quantity")
        self.assertEqual(spalte.origin.name, "UNCERTAIN")

    def test_summe_in_eur_ist_zeilensumme(self) -> None:
        self.assertEqual(_pseudo(self.analysis).get(PSEUDO_TOTAL), "Summe in EUR")


class Angebot15Test(unittest.TestCase):
    """Nr 15: 'EP /ME' ist der Preis, 'GP' die Zeilensumme."""

    def setUp(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[1]
        self.analysis = _analyse(header, row)
        self.felder = _felder(self.analysis)

    def test_ep_je_me_ist_der_preis(self) -> None:
        self.assertEqual(self.felder.get("price"), "EP /ME")

    def test_ep_je_me_ist_keine_mengeneinheit(self) -> None:
        self.assertNotEqual(self.felder.get("uom"), "EP /ME")

    def test_gp_ist_zeilensumme(self) -> None:
        self.assertEqual(_pseudo(self.analysis).get(PSEUDO_TOTAL), "GP")

    def test_menge_rs_ist_die_menge(self) -> None:
        self.assertEqual(self.felder.get("quantity"), "Menge RS")


class PreisJeMengeneinheitTest(unittest.TestCase):
    """Alle Schreibweisen von "Preis je Mengeneinheit" muessen Preis sein."""

    SCHREIBWEISEN = ("EP /ME", "EP/ME", "ep/me", "Preis/ME", "Preis je ME",
                     "Einzelpreis / Stk", "Preis pro Stueck", "Preis / Einheit")

    def test_alle_schreibweisen_sind_preis(self) -> None:
        extractor = _extractor()
        for text in self.SCHREIBWEISEN:
            with self.subTest(ueberschrift=text):
                feld, konfidenz, _ = extractor._match_header_text(text)
                self.assertEqual(feld, "price", f"'{text}' -> {feld}")
                self.assertGreaterEqual(konfidenz, 0.6)

    def test_reine_mengeneinheit_bleibt_mengeneinheit(self) -> None:
        feld, _, _ = _extractor()._match_header_text("ME")
        self.assertEqual(feld, "uom")

    def test_preiseinheit_bleibt_preiseinheit(self) -> None:
        feld, _, _ = _extractor()._match_header_text("Preiseinheit")
        self.assertEqual(feld, "price_unit")


class IncotermPreisspalteTest(unittest.TestCase):
    """Incoterm-Kuerzel plus Waehrung in Klammern = Preisspalte."""

    def test_ddp_mit_waehrung_und_einheit(self) -> None:
        treffer = incoterm_price_header("DDP 12345 Musterstadt (EUR/ST)")
        self.assertIsNotNone(treffer)
        assert treffer is not None
        incoterm, currency, uom = treffer
        self.assertEqual(incoterm, "DDP")
        self.assertEqual(currency, "EUR")
        self.assertEqual(uom, "ST")

    def test_fca_werk(self) -> None:
        treffer = incoterm_price_header("FCA Werk (EUR/ST)")
        self.assertIsNotNone(treffer)
        assert treffer is not None
        self.assertEqual(treffer[0], "FCA")

    def test_exw_nur_waehrung(self) -> None:
        treffer = incoterm_price_header("EXW (EUR)")
        self.assertIsNotNone(treffer)
        assert treffer is not None
        self.assertEqual((treffer[0], treffer[1]), ("EXW", "EUR"))

    def test_incoterm_ohne_waehrung_ist_keine_preisspalte(self) -> None:
        self.assertIsNone(incoterm_price_header("Lieferbedingung DDP"))

    def test_waehrung_ohne_incoterm_ist_keine_preisspalte(self) -> None:
        self.assertIsNone(incoterm_price_header("Subtotal (EUR)"))

    def test_incoterm_spalte_wird_als_preis_zugeordnet(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        felder = _felder(_analyse(header, row))
        self.assertEqual(felder.get("price"), "DDP 12345 Musterstadt (EUR/ST)")

    def test_incoterm_wird_im_kopf_vermerkt(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        self.assertEqual(_analyse(header, row).incoterm, "DDP")

    def test_waehrung_aus_dem_kopf_wird_uebernommen(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        analysis = _analyse(header, row)
        self.assertEqual(analysis.header_currency, "EUR")
        self.assertEqual(analysis.header_uom, "ST")

    def test_incoterm_preisspalte_bleibt_unsicher(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        analysis = _analyse(header, row)
        spalte = next(a for a in analysis.columns.values() if a.field == "price")
        self.assertEqual(spalte.origin.name, "UNCERTAIN")

    def test_incoterm_erzeugt_klartext_befund(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        analysis = _analyse(header, row)
        self.assertTrue(any("DDP" in n for n in analysis.notes),
                        f"Befund fehlt, Notizen: {analysis.notes}")

    def test_subtotal_bleibt_zeilensumme(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[2]
        self.assertEqual(_pseudo(_analyse(header, row)).get(PSEUDO_TOTAL),
                         "Subtotal (EUR)")


class TrennstrichTest(unittest.TestCase):
    """Umbruch-Bindestriche zusammenfuehren -- echte aber NICHT zerstoeren."""

    UMBRUECHE = {
        "Preis-einheit": "Preiseinheit",
        "Liefer-woche": "Lieferwoche",
        "Ober-flaeche": "Oberflaeche",
        "Netto-gewicht": "Nettogewicht",
        "Bestell-menge": "Bestellmenge",
        "Rahmen-vertrag": "Rahmenvertrag",
    }
    ECHTE_BINDESTRICHE = ("E-Preis", "G-Preis", "L-Termin", "Art.-Nr.",
                          "Mat.-Nr.", "EK-Preis")

    def test_umbrueche_werden_zusammengefuehrt(self) -> None:
        for quelle, ziel in self.UMBRUECHE.items():
            with self.subTest(ueberschrift=quelle):
                self.assertEqual(dehyphenate_header(quelle), ziel)

    def test_echte_bindestriche_bleiben_stehen(self) -> None:
        for text in self.ECHTE_BINDESTRICHE:
            with self.subTest(ueberschrift=text):
                self.assertEqual(dehyphenate_header(text), text)

    def test_e_preis_bleibt_preis(self) -> None:
        feld, _, _ = _extractor()._match_header_text("E-Preis")
        self.assertEqual(feld, "price")

    def test_g_preis_ist_zeilensumme(self) -> None:
        feld, _, _ = _extractor()._match_header_text("G-Preis")
        self.assertEqual(feld, PSEUDO_TOTAL)

    def test_l_termin_ist_liefertermin(self) -> None:
        feld, _, _ = _extractor()._match_header_text("L-Termin")
        self.assertEqual(feld, PSEUDO_DELIVERY)

    def test_art_nr_bleibt_artikelnummer(self) -> None:
        feld, _, _ = _extractor()._match_header_text("Art.-Nr.")
        self.assertEqual(feld, "vendor_material_number")

    def test_preis_einheit_wird_preiseinheit(self) -> None:
        feld, _, _ = _extractor()._match_header_text("Preis-einheit")
        self.assertEqual(feld, "price_unit")

    def test_liefer_woche_wird_liefertermin(self) -> None:
        feld, _, _ = _extractor()._match_header_text("Liefer-woche")
        self.assertEqual(feld, PSEUDO_DELIVERY)

    def test_ober_flaeche_ist_zusatzangabe(self) -> None:
        feld, _, _ = _extractor()._match_header_text("Ober-flaeche")
        self.assertEqual(feld, PSEUDO_EXTRA)

    def test_trennstrich_wird_in_der_begruendung_genannt(self) -> None:
        _, _, grund = _extractor()._match_header_text("Liefer-woche")
        self.assertIn("Trennstrich", grund)


class NeueFelderTest(unittest.TestCase):
    """Spalten ohne SAP-Bezug werden erkannt statt ignoriert."""

    def test_zeilensummen_kuerzel(self) -> None:
        extractor = _extractor()
        for text in ("GP", "G-Preis", "PosWert", "Summe in EUR", "Betrag",
                     "Wert EUR", "Subtotal", "Gesamt", "Gesamtpreis"):
            with self.subTest(ueberschrift=text):
                feld, _, _ = extractor._match_header_text(text)
                self.assertEqual(feld, PSEUDO_TOTAL, f"'{text}' -> {feld}")

    def test_rabattspalten(self) -> None:
        extractor = _extractor()
        for text in ("%", "Rab.", "Rabatt", "P", "Nachlass", "Discount"):
            with self.subTest(ueberschrift=text):
                feld, _, _ = extractor._match_header_text(text)
                self.assertEqual(feld, PSEUDO_IGNORE, f"'{text}' -> {feld}")

    def test_lieferterminspalten(self) -> None:
        extractor = _extractor()
        for text in ("L-Termin", "Leistungsdatum", "Lieferwoche", "Liefer-woche",
                     "erwart. Versanddatum", "Termin", "Liefertermin"):
            with self.subTest(ueberschrift=text):
                feld, _, _ = extractor._match_header_text(text)
                self.assertEqual(feld, PSEUDO_DELIVERY, f"'{text}' -> {feld}")

    def test_lieferzeit_bleibt_wiederbeschaffungszeit(self) -> None:
        # "Lieferzeit" ist eine Dauer in Tagen und hat ein echtes SAP-Feld --
        # sie darf NICHT zum Pseudofeld Liefertermin abrutschen.
        feld, _, _ = _extractor()._match_header_text("Lieferzeit")
        self.assertEqual(feld, "lead_time_days")

    def test_positionsnummern(self) -> None:
        extractor = _extractor()
        for text in ("Pos.Nr.", "Pos-Nr.", "Position", "Pos."):
            with self.subTest(ueberschrift=text):
                feld, _, _ = extractor._match_header_text(text)
                self.assertEqual(feld, "position_number", f"'{text}' -> {feld}")

    def test_zusatzangaben_ohne_sap_bezug(self) -> None:
        extractor = _extractor()
        for text in ("Werkstoff", "Oberflaeche", "Ober-flaeche", "Nettogewicht",
                     "Rahmenvertrag"):
            with self.subTest(ueberschrift=text):
                feld, _, _ = extractor._match_header_text(text)
                self.assertEqual(feld, PSEUDO_EXTRA, f"'{text}' -> {feld}")

    def test_werteinheit_ist_mengeneinheit(self) -> None:
        feld, _, _ = _extractor()._match_header_text("WE")
        self.assertEqual(feld, "uom")

    def test_pro_ist_bezugsgroesse(self) -> None:
        feld, _, _ = _extractor()._match_header_text("pro")
        self.assertEqual(feld, "price_unit")

    def test_pseudofelder_landen_nicht_in_der_position(self) -> None:
        _, header, row = ECHTE_KOPFZEILEN[15]
        felder = _felder(_analyse(header, row))
        for wert in felder.values():
            self.assertNotIn(wert, ("Werkstoff", "Ober-flaeche",
                                    "Rahmen-vertrag / Stck."))


class MengenmatrixNegativTest(unittest.TestCase):
    """Eine gewoehnliche Tabelle darf keine Mengen-Matrix werden."""

    GEWOEHNLICH = ["Pos.", "Artikelnummer", "Bezeichnung", "Menge", "ME",
                   "Einzelpreis", "Gesamtpreis"]
    ZEILE = ["10", "12-4455-A", "Dichtring NBR", "100", "ST", "4,55", "455,00"]

    def test_keine_tier_columns(self) -> None:
        self.assertFalse(_analyse(self.GEWOEHNLICH, self.ZEILE).tier_columns)

    def test_keine_alternativen_mengenspalten(self) -> None:
        self.assertNotIn(PSEUDO_ALT_QTY, _pseudo(_analyse(self.GEWOEHNLICH,
                                                          self.ZEILE)))

    def test_kein_staffelbefund(self) -> None:
        analysis = _analyse(self.GEWOEHNLICH, self.ZEILE)
        self.assertFalse(any("Mehrere Mengenspalten" in n for n in analysis.notes))

    def test_menge_bleibt_sicher(self) -> None:
        analysis = _analyse(self.GEWOEHNLICH, self.ZEILE)
        spalte = next(a for a in analysis.columns.values() if a.field == "quantity")
        self.assertEqual(spalte.origin.name, "EXTRACTED")

    def test_einzelne_nummerierte_mengenspalte_bleibt_menge(self) -> None:
        header = ["Pos.", "Bezeichnung", "Menge1", "ME", "Preis"]
        felder = _felder(_analyse(header, ["10", "Ring", "100", "ST", "4,55"]))
        self.assertIn("quantity", felder)

    def test_ab_mengen_matrix_bleibt_unberuehrt(self) -> None:
        header = ["Pos.", "Bezeichnung", "ab 100", "ab 500", "ab 1000"]
        analysis = _analyse(header, ["10", "Ring", "4,55", "4,20", "3,95"])
        self.assertEqual(len(analysis.tier_columns), 3)
        self.assertNotIn(PSEUDO_ALT_QTY, _pseudo(analysis))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
