"""Positionsarten: Einmalkosten, Alternativen, Zwischensummen (unittest).

Diese Faelle stammen aus 22 echten Angeboten.  Sie sind die *gefaehrlichste*
Fehlerklasse ueberhaupt: es bleibt nichts leer, sondern es entsteht eine
plausible falsche Zahl -- ein Werkzeugpreis von 8.500 EUR als Materialpreis im
Einkaufsinfosatz faellt erst auf, wenn jemand danach bestellt.

Entsprechend pruefen diese Tests in beide Richtungen.  Fuer jeden Fall gibt es
einen Gutfall, einen Grenzfall und mindestens einen Test, der sicherstellt,
dass die Erkennung eben **nicht** anschlaegt: "Werkzeugstahl" ist ein
Material, "Musterring" ist ein Material, "Summenscheibe" ist ein Bauteil, und
eine echte Mengenstaffel ist keine Alternative.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_poskind_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.models.enums import FieldOrigin                                 # noqa: E402
from app.models.offer import Offer                                       # noqa: E402
from app.models.offer_position import OfferPosition                      # noqa: E402
from app.services.extraction.plausibility import (                       # noqa: E402
    CODE_PRICE_OUTLIER,
    check_document_total,
    check_price_outliers,
    counts_in_document_total,
)
from app.services.extraction.position_kinds import (                     # noqa: E402
    CODE_ALTERNATIVE,
    CODE_MIN_ORDER_CONFLICT,
    CODE_MIN_ORDER_TEXT,
    CODE_ONE_TIME_COST,
    CODE_SUBTOTAL,
    KIND_ALTERNATIVE,
    KIND_MATERIAL,
    KIND_ONE_TIME_COST,
    KIND_SUBTOTAL,
    apply_document_min_order_qty,
    apply_position_kinds,
    classify_position,
    counts_as_material_price,
    counts_towards_document_total,
    detect_alternative_keyword,
    detect_one_time_cost,
    detect_subtotal,
    find_min_order_quantities,
)
from app.utils.parsing import parse_date                                 # noqa: E402


# --------------------------------------------------------------------------
# Hilfsmittel
# --------------------------------------------------------------------------

def make_position(**kwargs) -> OfferPosition:
    """Position mit sinnvollen Grundwerten; alles per Schluesselwort."""
    daten = {
        "position_number": "10",
        "description": "Drehteil",
        "quantity": Decimal("100"),
        "uom": "ST",
        "price": Decimal("12.85"),
        "price_unit": 1,
        "currency": "EUR",
    }
    daten.update(kwargs)
    return OfferPosition(**daten)


def make_offer(*positionen: OfferPosition, raw_text: str = "") -> Offer:
    offer = Offer(currency="EUR", raw_text=raw_text)
    offer.positions.extend(positionen)
    return offer


def codes(position: OfferPosition) -> set[str]:
    return {issue.code for issue in position.issues}


# --------------------------------------------------------------------------
# Aufgabe 1 -- Einmalkosten erkennen
# --------------------------------------------------------------------------

class EinmalkostenErkennung(unittest.TestCase):
    """Die Wortliste selbst -- ohne Position, ohne Angebot."""

    def test_werkzeugkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Werkzeugkosten"))

    def test_werkzeugkostenanteil_trifft(self):
        self.assertTrue(detect_one_time_cost("Werkzeugkostenanteil"))

    def test_werkzeug_allein_trifft(self):
        self.assertTrue(detect_one_time_cost("Werkzeug"))

    def test_formkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Formkosten Spritzguss"))

    def test_tooling_trifft(self):
        self.assertTrue(detect_one_time_cost("Tooling"))

    def test_tooling_cost_englisch_trifft(self):
        self.assertTrue(detect_one_time_cost("Tooling cost, one-off"))

    def test_musterkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Musterkosten"))

    def test_bemusterung_trifft(self):
        self.assertTrue(detect_one_time_cost("Bemusterung nach VDA"))

    def test_empb_trifft(self):
        self.assertTrue(detect_one_time_cost("EMPB"))

    def test_erstmusterpruefbericht_trifft(self):
        self.assertTrue(detect_one_time_cost("Erstmusterpruefbericht"))

    def test_erstmusterpruefbericht_mit_umlaut_trifft(self):
        self.assertTrue(detect_one_time_cost("Erstmusterprüfbericht"))

    def test_sample_cost_trifft(self):
        self.assertTrue(detect_one_time_cost("Sample cost"))

    def test_prototyp_trifft(self):
        self.assertTrue(detect_one_time_cost("Prototyp"))

    def test_prototypenpreise_trifft(self):
        self.assertTrue(detect_one_time_cost("Prototypenpreise"))

    def test_vorserie_trifft(self):
        self.assertTrue(detect_one_time_cost("Vorserie"))

    def test_einrichtkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Einrichtkosten"))

    def test_ruestkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Ruestkosten"))

    def test_anlaufkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Anlaufkosten"))

    def test_setup_charge_trifft(self):
        self.assertTrue(detect_one_time_cost("Setup charge"))

    def test_entwicklungskosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Entwicklungskosten"))

    def test_zeichnungskosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Zeichnungskosten"))

    def test_verpackungskosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Verpackungskosten"))

    def test_frachtkosten_trifft(self):
        self.assertTrue(detect_one_time_cost("Frachtkosten"))

    def test_gross_klein_egal(self):
        self.assertTrue(detect_one_time_cost("WERKZEUGKOSTEN"))
        self.assertTrue(detect_one_time_cost("werkzeugkosten"))

    def test_als_wortteil_im_langen_text(self):
        text = ("Position 40: anteilige Werkzeugkosten fuer die Form, "
                "einmalig faellig")
        self.assertTrue(detect_one_time_cost(text))

    def test_mit_bindestrich(self):
        self.assertTrue(detect_one_time_cost("Werkzeug-Kosten"))

    # -- DARF NICHT ANSCHLAGEN ------------------------------------------
    def test_werkzeugstahl_ist_material(self):
        self.assertEqual(detect_one_time_cost("Werkzeugstahl 1.2379"), "")

    def test_werkzeughalter_ist_material(self):
        self.assertEqual(detect_one_time_cost("Werkzeughalter VDI 30"), "")

    def test_musterring_ist_material(self):
        self.assertEqual(detect_one_time_cost("Musterring 20x3"), "")

    def test_prototypenhalterung_wort_ohne_kostenbezug(self):
        self.assertEqual(detect_one_time_cost("Musterklemme"), "")

    def test_normales_drehteil(self):
        self.assertEqual(detect_one_time_cost("Drehteil Edelstahl 1.4301"), "")

    def test_leerer_text(self):
        self.assertEqual(detect_one_time_cost(""), "")
        self.assertEqual(detect_one_time_cost(None), "")


class EinmalkostenAmAngebot(unittest.TestCase):

    def test_werkzeugposition_wird_abgewaehlt_aber_nicht_verworfen(self):
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten",
                                 quantity=Decimal("1"),
                                 price=Decimal("8500"))
        offer = make_offer(make_position(), werkzeug)
        apply_position_kinds(offer)

        self.assertEqual(len(offer.positions), 2, "nichts darf verworfen werden")
        self.assertEqual(werkzeug.position_kind, KIND_ONE_TIME_COST)
        self.assertFalse(werkzeug.selected)
        self.assertFalse(werkzeug.do_info_record)

    def test_befund_nennt_position_und_stichwort(self):
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten",
                                 price=Decimal("8500"))
        offer = make_offer(make_position(), werkzeug)
        apply_position_kinds(offer)

        self.assertIn(CODE_ONE_TIME_COST, codes(werkzeug))
        text = next(i.message for i in werkzeug.issues
                    if i.code == CODE_ONE_TIME_COST)
        self.assertIn("Position 40", text)
        self.assertIn("Einmalkosten", text)
        self.assertIn("werkzeugkosten", text.lower())

    def test_materialposition_bleibt_ausgewaehlt(self):
        material = make_position(position_number="10",
                                 description="Werkzeugstahl 1.2379")
        offer = make_offer(material)
        apply_position_kinds(offer)

        self.assertEqual(material.position_kind, KIND_MATERIAL)
        self.assertTrue(material.selected)
        self.assertEqual(codes(material), set())

    def test_einmalkosten_zaehlen_in_die_belegsumme(self):
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten",
                                 quantity=Decimal("1"),
                                 price=Decimal("500"))
        offer = make_offer(make_position(quantity=Decimal("100"),
                                         price=Decimal("10")),
                           werkzeug)
        apply_position_kinds(offer)
        self.assertTrue(counts_towards_document_total(werkzeug))
        self.assertTrue(counts_in_document_total(werkzeug))

        # 100 x 10 + 1 x 500 = 1500 -> keine Beanstandung
        notes = check_document_total(offer, "Nettosumme 1.500,00 EUR")
        self.assertEqual(notes, [])

    def test_einmalkosten_kein_preisausreisser(self):
        """Ein Werkzeug fuer 8.500 EUR neben Drehteilen zu 12 EUR ist kein
        Dezimaltrennerfehler -- die Ausreisserpruefung darf nicht anspringen."""
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten",
                                 quantity=Decimal("1"),
                                 price=Decimal("8500"))
        offer = make_offer(make_position(position_number="10"),
                           make_position(position_number="20"),
                           make_position(position_number="30"),
                           werkzeug)
        apply_position_kinds(offer)
        check_price_outliers(offer)

        self.assertNotIn(CODE_PRICE_OUTLIER, codes(werkzeug))
        self.assertFalse(counts_as_material_price(werkzeug))

    def test_ohne_einordnung_waere_es_ein_ausreisser(self):
        """Gegenprobe: dieselbe Zeile ohne Einordnung schlaegt sehr wohl an --
        der Test beweist, dass die Ausnahme wirklich greift."""
        werkzeug = make_position(position_number="40",
                                 description="Sonderteil",
                                 quantity=Decimal("1"),
                                 price=Decimal("8500"))
        offer = make_offer(make_position(position_number="10"),
                           make_position(position_number="20"),
                           make_position(position_number="30"),
                           werkzeug)
        check_price_outliers(offer)
        self.assertIn(CODE_PRICE_OUTLIER, codes(werkzeug))


# --------------------------------------------------------------------------
# Aufgabe 2 -- Alternativpositionen
# --------------------------------------------------------------------------

class AlternativeStichwort(unittest.TestCase):

    def test_alternativposition_trifft(self):
        self.assertTrue(detect_alternative_keyword("Alternativposition"))

    def test_alternativpreis_trifft(self):
        self.assertTrue(detect_alternative_keyword(
            "Drehteil, Alternativpreis bei Jahresabnahme"))

    def test_alternativmenge_trifft(self):
        self.assertTrue(detect_alternative_keyword("Alternativmenge 5.000 ST"))

    def test_alternativangebot_trifft(self):
        self.assertTrue(detect_alternative_keyword("Alternativangebot"))

    def test_optionale_position_trifft(self):
        self.assertTrue(detect_alternative_keyword("optionale Position"))

    def test_alternative_am_zeilenanfang_trifft(self):
        self.assertTrue(detect_alternative_keyword("Alternative: Ausfuehrung B"))

    def test_option_am_zeilenanfang_trifft(self):
        self.assertTrue(detect_alternative_keyword("Option 2 -- Edelstahl"))

    def test_variante_am_zeilenanfang_trifft(self):
        self.assertTrue(detect_alternative_keyword("Variante B mit Beschichtung"))

    def test_wahlweise_am_zeilenanfang_trifft(self):
        self.assertTrue(detect_alternative_keyword("wahlweise in Messing"))

    def test_oder_am_zeilenanfang_trifft(self):
        self.assertTrue(detect_alternative_keyword("oder 12,50 bei 1.000 Stueck"))

    # -- DARF NICHT ANSCHLAGEN ------------------------------------------
    def test_oder_mitten_in_der_bezeichnung_ist_keine_alternative(self):
        self.assertEqual(
            detect_alternative_keyword("Schraube M6 oder M8, verzinkt"), "")

    def test_variante_mitten_in_der_bezeichnung(self):
        self.assertEqual(
            detect_alternative_keyword("Dichtring Variante B"), "")

    def test_option_mitten_in_der_bezeichnung(self):
        self.assertEqual(
            detect_alternative_keyword("Steuergeraet mit Option Bluetooth"), "")

    def test_normales_material(self):
        self.assertEqual(detect_alternative_keyword("Drehteil Edelstahl"), "")


class AlternativeAmAngebot(unittest.TestCase):

    def test_stichwortposition_wird_abgewaehlt(self):
        alt = make_position(position_number="30",
                            description="Alternativposition: Ausfuehrung B")
        offer = make_offer(make_position(), alt)
        apply_position_kinds(offer)

        self.assertEqual(alt.position_kind, KIND_ALTERNATIVE)
        self.assertFalse(alt.selected)
        self.assertIn(CODE_ALTERNATIVE, codes(alt))

    def test_gleiches_material_verschiedener_preis(self):
        a = make_position(position_number="20", material_number="4711",
                          description="Drehteil", quantity=Decimal("1000"),
                          price=Decimal("12.85"))
        b = make_position(position_number="30", material_number="4711",
                          description="Drehteil", quantity=Decimal("100"),
                          price=Decimal("14.50"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)

        self.assertTrue(a.selected, "die ERSTE bleibt vorausgewaehlt")
        self.assertEqual(a.position_kind, KIND_MATERIAL)
        self.assertFalse(b.selected)
        self.assertEqual(b.position_kind, KIND_ALTERNATIVE)

    def test_es_wird_nicht_die_guenstigste_gewaehlt(self):
        """Ausdruecklich: es wird nicht bewertet.  Steht die teure Zeile
        zuerst, bleibt die teure Zeile vorausgewaehlt."""
        teuer = make_position(position_number="20", material_number="4711",
                              price=Decimal("99.00"))
        billig = make_position(position_number="30", material_number="4711",
                               price=Decimal("9.00"))
        offer = make_offer(teuer, billig)
        apply_position_kinds(offer)

        self.assertTrue(teuer.selected)
        self.assertFalse(billig.selected)

    def test_befund_nennt_beide_positionen(self):
        a = make_position(position_number="20", material_number="4711",
                          price=Decimal("12.85"))
        b = make_position(position_number="30", material_number="4711",
                          price=Decimal("14.50"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)

        text = next(i.message for i in b.issues if i.code == CODE_ALTERNATIVE)
        self.assertIn("Position 20 und 30", text)
        self.assertIn("nur die erste", text)

    def test_auch_die_erste_bekommt_den_hinweis(self):
        a = make_position(position_number="20", material_number="4711",
                          price=Decimal("12.85"))
        b = make_position(position_number="30", material_number="4711",
                          price=Decimal("14.50"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertIn(CODE_ALTERNATIVE, codes(a))

    def test_lieferantenmaterialnummer_zaehlt_auch(self):
        a = make_position(position_number="20", vendor_material_number="AB-1",
                          price=Decimal("12.85"))
        b = make_position(position_number="30", vendor_material_number="AB-1",
                          price=Decimal("14.50"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertEqual(b.position_kind, KIND_ALTERNATIVE)

    # -- DARF NICHT ANSCHLAGEN ------------------------------------------
    def test_echte_mengenstaffel_ist_keine_alternative(self):
        basis = make_position(position_number="20", material_number="4711",
                              quantity=Decimal("100"), price=Decimal("14.50"),
                              min_order_qty=Decimal("100"))
        stufe = make_position(position_number="20", material_number="4711",
                              quantity=Decimal("1000"), price=Decimal("12.85"),
                              min_order_qty=Decimal("1000"),
                              remarks="Staffelpreis: ab 1000 = 12,85")
        offer = make_offer(basis, stufe)
        apply_position_kinds(offer)

        self.assertEqual(stufe.position_kind, KIND_MATERIAL)
        self.assertTrue(stufe.selected)
        self.assertTrue(basis.selected)

    def test_staffel_ueber_scale_quantities_ist_keine_alternative(self):
        basis = make_position(position_number="20", material_number="4711",
                              price=Decimal("14.50"))
        basis.scale_quantities = [(Decimal("100"), Decimal("14.50")),
                                  (Decimal("1000"), Decimal("12.85"))]
        zweite = make_position(position_number="30", material_number="4711",
                               price=Decimal("12.85"))
        zweite.scale_quantities = list(basis.scale_quantities)
        offer = make_offer(basis, zweite)
        apply_position_kinds(offer)

        self.assertTrue(basis.selected)
        self.assertTrue(zweite.selected)

    def test_gleiches_material_gleicher_preis_ist_keine_alternative(self):
        """Zweimal dasselbe zum selben Preis ist eine Dublette -- die meldet
        die Kreuzpruefung, nicht dieses Modul."""
        a = make_position(position_number="20", material_number="4711",
                          price=Decimal("12.85"))
        b = make_position(position_number="30", material_number="4711",
                          price=Decimal("12.85"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)

        self.assertTrue(a.selected)
        self.assertTrue(b.selected)
        self.assertEqual(b.position_kind, KIND_MATERIAL)

    def test_preiseinheit_wird_beruecksichtigt(self):
        """12,85 je 1 und 1.285,00 je 100 sind derselbe Preis -- keine
        Alternative."""
        a = make_position(position_number="20", material_number="4711",
                          price=Decimal("12.85"), price_unit=1)
        b = make_position(position_number="30", material_number="4711",
                          price=Decimal("1285.00"), price_unit=100)
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertTrue(b.selected)

    def test_verschiedene_materialien_bleiben_unberuehrt(self):
        a = make_position(position_number="20", material_number="4711",
                          price=Decimal("12.85"))
        b = make_position(position_number="30", material_number="4712",
                          price=Decimal("14.50"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertTrue(a.selected)
        self.assertTrue(b.selected)

    def test_ohne_materialnummer_keine_dublettenaussage(self):
        """Zwei Zeilen "Dichtring" muessen nicht dasselbe Teil sein -- allein
        die Bezeichnung begruendet keine Alternative."""
        a = make_position(position_number="20", description="Dichtring",
                          price=Decimal("1.20"))
        b = make_position(position_number="30", description="Dichtring",
                          price=Decimal("1.40"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertTrue(a.selected)
        self.assertTrue(b.selected)

    def test_alternative_faellt_aus_der_belegsumme(self):
        a = make_position(position_number="20", material_number="4711",
                          quantity=Decimal("100"), price=Decimal("10"))
        b = make_position(position_number="30", material_number="4711",
                          quantity=Decimal("100"), price=Decimal("12"))
        offer = make_offer(a, b)
        apply_position_kinds(offer)
        self.assertFalse(counts_in_document_total(b))
        self.assertEqual(check_document_total(offer, "Nettosumme 1.000,00"), [])


# --------------------------------------------------------------------------
# Aufgabe 3 -- Zwischensummenzeilen
# --------------------------------------------------------------------------

class ZwischensummeErkennung(unittest.TestCase):

    def test_zwischensumme_trifft(self):
        self.assertTrue(detect_subtotal("Zwischensumme"))

    def test_summe_trifft(self):
        self.assertTrue(detect_subtotal("Summe Baugruppe A"))

    def test_uebertrag_trifft(self):
        self.assertTrue(detect_subtotal("Uebertrag von Seite 1"))

    def test_uebertrag_mit_umlaut_trifft(self):
        self.assertTrue(detect_subtotal("Übertrag"))

    def test_gesamtsumme_trifft(self):
        self.assertTrue(detect_subtotal("Gesamtsumme"))

    def test_nettosumme_trifft(self):
        self.assertTrue(detect_subtotal("Nettosumme"))

    def test_total_trifft(self):
        self.assertTrue(detect_subtotal("Total"))

    def test_mit_vorangestellter_nummer(self):
        self.assertTrue(detect_subtotal("90 Zwischensumme"))

    # -- DARF NICHT ANSCHLAGEN ------------------------------------------
    def test_summenscheibe_ist_material(self):
        self.assertEqual(detect_subtotal("Summenscheibe 40x2"), "")

    def test_summe_mitten_im_satz(self):
        self.assertEqual(detect_subtotal("Blech, Summe der Kanten 400 mm"), "")

    def test_totalisator_ist_material(self):
        self.assertEqual(detect_subtotal("Totalisator Anzeige"), "")


class ZwischensummeAmAngebot(unittest.TestCase):

    def test_summenzeile_wird_abgewaehlt(self):
        summe = make_position(position_number="90",
                              description="Zwischensumme",
                              quantity=None, price=Decimal("1284.00"))
        offer = make_offer(make_position(), summe)
        apply_position_kinds(offer)

        self.assertEqual(summe.position_kind, KIND_SUBTOTAL)
        self.assertFalse(summe.selected)
        self.assertIn(CODE_SUBTOTAL, codes(summe))

    def test_summenzeile_faellt_aus_der_belegsumme(self):
        """Sonst waere jeder Betrag doppelt und die Pruefung meldete Unsinn."""
        summe = make_position(position_number="90", description="Zwischensumme",
                              quantity=Decimal("1"), price=Decimal("1000"))
        offer = make_offer(
            make_position(position_number="10", quantity=Decimal("100"),
                          price=Decimal("10")),
            summe)
        apply_position_kinds(offer)
        self.assertFalse(counts_in_document_total(summe))
        self.assertEqual(check_document_total(offer, "Nettosumme 1.000,00"), [])

    def test_summe_schlaegt_einmalkosten(self):
        """"Zwischensumme Werkzeugkosten" ist zuerst eine Summenzeile."""
        zeile = make_position(description="Zwischensumme Werkzeugkosten")
        kind, _ = classify_position(zeile)
        self.assertEqual(kind, KIND_SUBTOTAL)


# --------------------------------------------------------------------------
# Aufgabe 4 -- Mindestbestellmenge aus dem Fliesstext
# --------------------------------------------------------------------------

class MindestmengeAusText(unittest.TestCase):

    def test_mindestbestellmenge_deutsch(self):
        treffer = find_min_order_quantities("Die Mindestbestellmenge 500 Stueck.")
        self.assertEqual([m for m, _ in treffer], [Decimal("500")])

    def test_mindestabnahme_mit_doppelpunkt(self):
        treffer = find_min_order_quantities("Mindestabnahme: 250 ST")
        self.assertEqual([m for m, _ in treffer], [Decimal("250")])

    def test_moq_englisch(self):
        treffer = find_min_order_quantities("MOQ 1000 pcs")
        self.assertEqual([m for m, _ in treffer], [Decimal("1000")])

    def test_ab_stueck_lieferbar(self):
        treffer = find_min_order_quantities("Lieferung ab 500 Stueck lieferbar")
        self.assertIn(Decimal("500"), [m for m, _ in treffer])

    def test_tausenderpunkt(self):
        treffer = find_min_order_quantities("Mindestbestellmenge 1.000 Stueck")
        self.assertEqual([m for m, _ in treffer], [Decimal("1000")])

    def test_kein_treffer_ohne_angabe(self):
        self.assertEqual(find_min_order_quantities("Preise freibleibend."), [])

    def test_uebernahme_auf_positionen(self):
        text = "Bitte beachten Sie: Mindestbestellmenge 500 Stueck."
        offer = make_offer(make_position(), make_position(position_number="20"),
                           raw_text=text)
        apply_document_min_order_qty(offer)

        for position in offer.positions:
            self.assertEqual(position.min_order_qty, Decimal("500"))
            self.assertIs(position.origin("min_order_qty"), FieldOrigin.EXTRACTED)
            self.assertIn(CODE_MIN_ORDER_TEXT, codes(position))

    def test_eigene_angabe_hat_vorrang(self):
        eigene = make_position(min_order_qty=Decimal("50"))
        offer = make_offer(eigene, raw_text="Mindestbestellmenge 500 Stueck")
        apply_document_min_order_qty(offer)
        self.assertEqual(eigene.min_order_qty, Decimal("50"))

    def test_widerspruechliche_angaben_werden_nicht_geraten(self):
        text = ("Mindestbestellmenge 500 Stueck. Fuer Sonderteile gilt eine "
                "Mindestabnahmemenge 1000 Stueck.")
        position = make_position()
        offer = make_offer(position, raw_text=text)
        apply_document_min_order_qty(offer)

        self.assertIsNone(position.min_order_qty)
        self.assertIn(CODE_MIN_ORDER_CONFLICT,
                      {i.code for i in offer.issues})

    def test_einmalkostenzeile_bekommt_keine_mindestmenge(self):
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten")
        offer = make_offer(werkzeug, raw_text="Mindestbestellmenge 500 Stueck")
        apply_position_kinds(offer)
        self.assertIsNone(werkzeug.min_order_qty)


# --------------------------------------------------------------------------
# Aufgabe 5 -- Datumsformat JJJJ/M/T
# --------------------------------------------------------------------------

class DatumJahrZuerst(unittest.TestCase):

    def test_einstellige_monate_und_tage(self):
        self.assertEqual(parse_date("2026/8/18"), date(2026, 8, 18))

    def test_zweistellig(self):
        self.assertEqual(parse_date("2026/08/18"), date(2026, 8, 18))

    def test_jahresende(self):
        self.assertEqual(parse_date("2026/12/31"), date(2026, 12, 31))

    def test_ungueltiger_tag_bleibt_none(self):
        self.assertIsNone(parse_date("2026/02/30"))

    def test_kurzes_jahr_bleibt_mehrdeutig_wie_bisher(self):
        """Die bereits behandelte Mehrdeutigkeit tt/mm gegen mm/tt darf sich
        nicht veraendert haben."""
        self.assertEqual(parse_date("08/09/2026"), date(2026, 9, 8))
        self.assertEqual(parse_date("08/09/2026", day_first=False),
                         date(2026, 8, 9))

    def test_iso_mit_bindestrich_unveraendert(self):
        self.assertEqual(parse_date("2026-08-18"), date(2026, 8, 18))

    def test_deutsches_datum_unveraendert(self):
        self.assertEqual(parse_date("18.08.2026"), date(2026, 8, 18))


# --------------------------------------------------------------------------
# Zusammenspiel
# --------------------------------------------------------------------------

class Zusammenspiel(unittest.TestCase):

    def test_vollstaendiges_angebot(self):
        """Ein Angebot mit allen vier Fallgruppen auf einmal."""
        p10 = make_position(position_number="10", material_number="4711",
                            description="Drehteil", quantity=Decimal("1000"),
                            price=Decimal("12.85"))
        p20 = make_position(position_number="20", material_number="4711",
                            description="Drehteil, Einzelabruf",
                            quantity=Decimal("100"), price=Decimal("14.50"))
        p30 = make_position(position_number="30", material_number="4712",
                            description="Dichtring", quantity=Decimal("500"),
                            price=Decimal("0.80"))
        p40 = make_position(position_number="40",
                            description="Werkzeugkostenanteil",
                            quantity=Decimal("1"), price=Decimal("8500"))
        p90 = make_position(position_number="90", description="Zwischensumme",
                            quantity=Decimal("1"), price=Decimal("21750"))
        offer = make_offer(p10, p20, p30, p40, p90,
                           raw_text="Mindestbestellmenge 500 Stueck")
        apply_position_kinds(offer)

        self.assertEqual(p10.position_kind, KIND_MATERIAL)
        self.assertEqual(p20.position_kind, KIND_ALTERNATIVE)
        self.assertEqual(p30.position_kind, KIND_MATERIAL)
        self.assertEqual(p40.position_kind, KIND_ONE_TIME_COST)
        self.assertEqual(p90.position_kind, KIND_SUBTOTAL)

        self.assertEqual([p.selected for p in offer.positions],
                         [True, False, True, False, False])
        self.assertEqual(len(offer.positions), 5, "nichts wurde verworfen")

        # Mindestmenge nur auf die beiden echten Materialpositionen
        self.assertEqual(p10.min_order_qty, Decimal("500"))
        self.assertEqual(p30.min_order_qty, Decimal("500"))
        self.assertIsNone(p40.min_order_qty)
        self.assertIsNone(p90.min_order_qty)

    def test_zweiter_lauf_aendert_nichts(self):
        """Die Einordnung muss wiederholbar sein, ohne sich aufzuschaukeln."""
        werkzeug = make_position(position_number="40",
                                 description="Werkzeugkosten")
        offer = make_offer(make_position(), werkzeug)
        apply_position_kinds(offer)
        erste = [(p.position_kind, p.selected) for p in offer.positions]
        apply_position_kinds(offer)
        zweite = [(p.position_kind, p.selected) for p in offer.positions]
        self.assertEqual(erste, zweite)

    def test_leeres_angebot_stuerzt_nicht(self):
        offer = make_offer()
        self.assertEqual(apply_position_kinds(offer), [])

    def test_standardwert_ist_material(self):
        self.assertEqual(OfferPosition().position_kind, KIND_MATERIAL)

    def test_position_kind_landet_in_to_dict(self):
        werkzeug = make_position(description="Werkzeugkosten")
        offer = make_offer(werkzeug)
        apply_position_kinds(offer)
        self.assertEqual(werkzeug.to_dict()["position_kind"], KIND_ONE_TIME_COST)


if __name__ == "__main__":
    unittest.main()
