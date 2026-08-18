"""Klauseln: wenn der Preis stimmt und trotzdem nicht der Preis ist.

    "Dichtring NBR ... 2,95 EUR/ST"
    "Preise freibleibend, zzgl. tagesaktuellem Legierungszuschlag."

2,95 ist korrekt abgelesen und trotzdem nicht, was der Einkauf zahlt.
Wandert die Zahl ohne ihre Klausel in den Infosatz, steht dort eine
Wahrheit, die keine ist -- und man sieht es der Zahl nicht an.

Zwei Dinge stehen hier fest:

1. Klauseln werden gefunden -- auch hinter Abkuerzungen ("zzgl."), was
   der erste Anlauf nicht konnte.
2. Sie werden NICHT eingerechnet.  Ein Legierungszuschlag ist
   tagesabhaengig, ein Skonto haengt vom Zahlungsverhalten ab.  Ein daraus
   gerechneter "effektiver Preis" stuende in keinem Beleg und waere fuer
   niemanden nachvollziehbar.

Der grosse Teil der Tests prueft, was NICHT anschlagen darf.  Ein
Fehlalarm bei jedem zweiten Angebot fuehrt dazu, dass die Hinweise
weggeklickt werden -- dann wirkt auch der echte nicht mehr.
"""

from __future__ import annotations

import unittest

from app.services.extraction.clauses import (
    KIND_ALLOY,
    KIND_CASH_DISCOUNT,
    KIND_ESCALATION,
    KIND_FREIGHT,
    KIND_INCOTERM,
    KIND_NON_BINDING,
    KIND_SMALL_QTY,
    KIND_SURCHARGE,
    KIND_VALIDITY,
    clause_summary,
    find_clauses,
)


class ErkennungTest(unittest.TestCase):

    def _arten(self, text: str) -> set[str]:
        return {klausel.kind for klausel in find_clauses(text)}

    # -- Preisgleitung --------------------------------------------------
    def test_preisgleitklausel(self):
        self.assertIn(KIND_ESCALATION,
                      self._arten("Es gilt unsere Preisgleitklausel."))

    def test_stoffpreisgleitklausel(self):
        self.assertIn(KIND_ESCALATION,
                      self._arten("Die Stoffpreisgleitklausel gemaess AGB gilt."))

    def test_legierungszuschlag(self):
        self.assertIn(KIND_ALLOY,
                      self._arten("Zzgl. tagesaktuellem Legierungszuschlag."))

    def test_alloy_surcharge_englisch(self):
        self.assertIn(KIND_ALLOY,
                      self._arten("Plus alloy surcharge as per daily quotation."))

    def test_kupferzuschlag(self):
        self.assertIn(KIND_SURCHARGE,
                      self._arten("Zzgl. Kupferzuschlag nach Tagesnotierung."))

    def test_energiezuschlag(self):
        self.assertIn(KIND_SURCHARGE,
                      self._arten("Wir berechnen einen Energiezuschlag."))

    # -- Mengen und Fristen ---------------------------------------------
    def test_mindermengenzuschlag(self):
        arten = self._arten(
            "Bei Abnahme unter 100 Stueck faellt ein Mindermengenzuschlag an.")
        self.assertIn(KIND_SMALL_QTY, arten)

    def test_mindermenge_mit_wert(self):
        klauseln = find_clauses(
            "Bei Abnahme unter 100 Stueck faellt ein Mindermengenzuschlag an.")
        treffer = [k for k in klauseln if k.kind == KIND_SMALL_QTY]
        self.assertTrue(treffer[0].value, "Die Grenze gehoert in den Hinweis")

    def test_freibleibend(self):
        self.assertIn(KIND_NON_BINDING, self._arten("Preise freibleibend."))

    def test_subject_to_change(self):
        self.assertIn(KIND_NON_BINDING,
                      self._arten("Prices are subject to change without notice."))

    def test_bindefrist_als_datum(self):
        klauseln = find_clauses("Dieses Angebot ist gueltig bis 30.09.2026.")
        treffer = [k for k in klauseln if k.kind == KIND_VALIDITY]
        self.assertTrue(treffer)
        self.assertIn("30.09.2026", treffer[0].value)

    def test_bindefrist_in_tagen(self):
        klauseln = find_clauses("Bindefrist: 30 Tage.")
        treffer = [k for k in klauseln if k.kind == KIND_VALIDITY]
        self.assertIn("30", treffer[0].value)

    # -- Kaufmaennisches ------------------------------------------------
    def test_skonto_mit_satz_und_frist(self):
        klauseln = find_clauses(
            "Zahlungsbedingungen: 2 % Skonto bei Zahlung innerhalb 14 Tagen, "
            "30 Tage netto.")
        treffer = [k for k in klauseln if k.kind == KIND_CASH_DISCOUNT]
        self.assertTrue(treffer)
        self.assertIn("2", treffer[0].value)
        self.assertIn("14", treffer[0].value)

    def test_skonto_wird_nicht_eingerechnet(self):
        """Der Hinweis muss das ausdruecklich sagen."""
        klauseln = find_clauses("2 % Skonto bei Zahlung innerhalb 14 Tagen.")
        treffer = [k for k in klauseln if k.kind == KIND_CASH_DISCOUNT]
        self.assertIn("NICHT", treffer[0].detail)

    def test_incoterm(self):
        self.assertIn(KIND_INCOTERM,
                      self._arten("Lieferung DDP 12345 Musterstadt."))

    def test_fracht_hinter_abkuerzung(self):
        """Der Fall, an dem der erste Anlauf scheiterte."""
        self.assertIn(KIND_FREIGHT,
                      self._arten("Alle Preise zzgl. Fracht und Verpackung."))

    def test_abkuerzung_zerschneidet_den_satz_nicht(self):
        """"zzgl." endet auf einen Punkt, beendet aber keinen Satz."""
        text = ("Lieferung erfolgt ca. 6 Wochen nach Auftrag, zzgl. "
                "Mindermengenzuschlag unter 50 Stueck.")
        self.assertIn(KIND_SMALL_QTY, self._arten(text))


class KeinFehlalarmTest(unittest.TestCase):
    """Wichtiger als jede Erkennung: nicht bei Harmlosem anschlagen."""

    def _keine(self, text: str) -> None:
        klauseln = find_clauses(text)
        self.assertEqual(klauseln, [],
                         f"Fehlalarm bei: {text!r} -> "
                         f"{[k.label for k in klauseln]}")

    def test_legierungsstahl_ist_ein_werkstoff(self):
        self._keine("Dichtring NBR 40x52x7 aus Legierungsstahl 1.2312.")

    def test_frei_haus_ist_keine_klausel(self):
        self._keine("Wir liefern frei Haus.")

    def test_gewoehnliche_position(self):
        self._keine("Menge 100 Stueck, Preis 2,95 EUR.")

    def test_lieferzeit_ist_keine_bindefrist(self):
        self._keine("Lieferzeit ca. 4 Wochen.")

    def test_firmenbeschreibung(self):
        self._keine("Unser Werk in Musterstadt fertigt seit 1970.")

    def test_leerer_text(self):
        self.assertEqual(find_clauses(""), [])
        self.assertEqual(find_clauses("   \n  "), [])

    def test_sehr_langer_absatz_wird_uebersprungen(self):
        """Zusammengelaufene Tabellenzeilen ergeben kein brauchbares Zitat."""
        lang = "Wort " * 200 + "Legierungszuschlag."
        self.assertEqual(find_clauses(lang), [])


class BewertungTest(unittest.TestCase):
    """Welche Klausel macht den Preis unvollstaendig?"""

    def _erste(self, text: str):
        klauseln = find_clauses(text)
        self.assertTrue(klauseln, text)
        return klauseln[0]

    def test_legierungszuschlag_beruehrt_den_preis(self):
        self.assertTrue(self._erste("Zzgl. Legierungszuschlag.").affects_price)

    def test_preisgleitklausel_beruehrt_den_preis(self):
        self.assertTrue(self._erste("Es gilt die Preisgleitklausel.").affects_price)

    def test_incoterm_beruehrt_den_stueckpreis_nicht(self):
        klausel = self._erste("Lieferung DDP 12345 Musterstadt.")
        self.assertFalse(klausel.affects_price)

    def test_skonto_beruehrt_den_stueckpreis_nicht(self):
        """Der Infosatz fuehrt den Bruttopreis -- Skonto ist eigene Kondition."""
        klausel = self._erste("2 % Skonto bei Zahlung innerhalb 14 Tagen.")
        self.assertFalse(klausel.affects_price)

    def test_zitat_stammt_aus_dem_beleg(self):
        """Der Anwender muss nachlesen koennen, nicht glauben muessen."""
        satz = "Preise freibleibend, zzgl. tagesaktuellem Legierungszuschlag."
        klausel = self._erste(satz)
        self.assertIn("freibleibend", klausel.quote.lower())


class ZusammenfassungTest(unittest.TestCase):

    def test_leer_bei_keinen_klauseln(self):
        self.assertEqual(clause_summary([]), "")

    def test_nennt_die_preisrelevanten_zuerst(self):
        text = ("Preise freibleibend, zzgl. Legierungszuschlag. "
                "Lieferung DDP 12345 Musterstadt.")
        zusammenfassung = clause_summary(find_clauses(text))
        self.assertIn("unvollstaendig", zusammenfassung)
        self.assertIn("Legierungszuschlag", zusammenfassung)

    def test_sagt_dass_nicht_gerechnet_wird(self):
        zusammenfassung = clause_summary(find_clauses("Zzgl. Legierungszuschlag."))
        self.assertIn("NICHT", zusammenfassung)

    def test_jede_klausel_nur_einmal(self):
        text = ("Zzgl. Legierungszuschlag. Der Legierungszuschlag wird "
                "monatlich angepasst.")
        klauseln = find_clauses(text)
        arten = [k.kind for k in klauseln if k.kind == KIND_ALLOY]
        self.assertLessEqual(len(arten), 2)
        zusammenfassung = clause_summary(klauseln)
        self.assertEqual(zusammenfassung.count("Legierungszuschlag"), 1,
                         "In der Zusammenfassung darf nichts doppelt stehen")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
