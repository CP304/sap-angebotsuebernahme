"""Mail UND Anhang bilden EIN Angebot.

Der Alltagsfall im Einkauf: die Preistabelle steckt im Anhang, das
Entscheidende steht im Mailtext ("gilt ab 01.09.", "Position 30 entfaellt",
"der Preis im Anhang ist ueberholt").  Wer nur eines von beidem liest, kauft
falsch ein.

Geprueft wird deshalb beides -- die Zusammenfuehrung selbst und ihre
Nachvollziehbarkeit:

* Kopfdaten aus der Mail ergaenzen den Anhang, bei Widerspruch gewinnt die
  Mail -- aber nur mit Protokolleintrag
* Positionsangaben (Preis, Mindestmenge, Lieferzeit, Gueltigkeit) werden
  ergaenzt, Streichungen fuehren zum *Abwaehlen*, nie zum Loeschen
* Jede Uebernahme steht in ``Offer.extraction_notes`` und ist am
  ``source_hint`` der Position als Zweitquelle erkennbar
* Alles laesst sich ueber die Einstellungen abschalten
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_mailmerge_")
os.environ.setdefault("SAP_ANGEBOT_HOME", _TEMP_HOME)

from app.config.settings import Settings                                # noqa: E402
from app.models.enums import FieldOrigin, SourceKind                    # noqa: E402
from app.models.offer import EmailContext, Offer                        # noqa: E402
from app.models.offer_position import OfferPosition                     # noqa: E402
from app.services.extraction.email_merge import (                       # noqa: E402
    MAIL_SOURCE_MARK,
    apply_email_header,
    apply_email_supplements,
    mail_segments,
)
from app.services.extraction.header_rules import extract_header_fields  # noqa: E402
from app.services.extraction.profiles import InMemoryProfileStore       # noqa: E402
from app.services.offer_import_service import OfferImportService        # noqa: E402

#: Der Anhang, wie ihn der Lieferant mitschickt
ANHANG_CSV = (
    "Pos;Material;Bezeichnung;Menge;Preis\n"
    "10;47110001;Dichtring NBR 40x52x7;500;12,85\n"
    "20;47110002;O-Ring Viton 25x3;200;8,90\n"
    "30;47110003;Flachdichtung 100x60x2;100;3,40\n"
)

#: Die Mail dazu -- sie enthaelt die eigentlich wichtigen Angaben
MAIL_TEXT = (
    "Sehr geehrte Damen und Herren,\n\n"
    "anbei unsere Preisliste. Die Preise gelten ab 01.09.2026, "
    "Zahlungsziel 30 Tage netto.\n"
    "Position 30 entfaellt, dafuer neu: 47110009 zu 4,20 EUR.\n"
    "Fuer Artikel 47110001 gilt abweichend eine Mindestmenge von 500 Stueck.\n"
    "Der Preis fuer 47110002 im Anhang ist ueberholt, es gilt 9,10 EUR.\n"
)


# ==========================================================================
# Hilfsmittel
# ==========================================================================

def anhang_angebot() -> Offer:
    """Angebot, wie es *nur* aus dem Anhang entstanden waere."""
    offer = Offer()
    offer.email = EmailContext(from_address="vertrieb@muster-dichtung.de",
                               subject="Preisliste")
    daten = (("10", "47110001", "Dichtring NBR 40x52x7", "12.85"),
             ("20", "47110002", "O-Ring Viton 25x3", "8.90"),
             ("30", "47110003", "Flachdichtung 100x60x2", "3.40"))
    for nummer, material, text, preis in daten:
        position = OfferPosition()
        position.source_kind = SourceKind.EMAIL_ATTACHMENT
        position.source_hint = "Anhang: preise.csv, Zeile 2"
        position.set_field("position_number", nummer, FieldOrigin.EXTRACTED)
        position.set_field("material_number", material, FieldOrigin.EXTRACTED)
        position.set_field("description", text, FieldOrigin.EXTRACTED)
        position.set_field("price", Decimal(preis), FieldOrigin.EXTRACTED)
        position.set_field("currency", "EUR", FieldOrigin.EXTRACTED)
        offer.positions.append(position)
    return offer


def position_mit(offer: Offer, material: str) -> OfferPosition:
    for position in offer.positions:
        if position.material_number == material:
            return position
    raise AssertionError(f"Position {material} fehlt: "
                         f"{[p.material_number for p in offer.positions]}")


class MailAnhangCase(unittest.TestCase):
    """Basisklasse mit temporaerem Verzeichnis und Standardeinstellungen."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mailmerge_")
        self.settings = Settings()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def eml(self, body: str = MAIL_TEXT, anhaenge: int = 1,
            inhalt: str = ANHANG_CSV, name: str = "preise.csv") -> str:
        message = EmailMessage()
        message["From"] = "T. Wagner <vertrieb@muster-dichtung.de>"
        message["To"] = "einkauf@technotrans.de"
        message["Subject"] = "Preisliste 2026"
        message["Date"] = "Mon, 17 Aug 2026 09:00:00 +0200"
        message.set_content(body)
        for index in range(anhaenge):
            stem, suffix = Path(name).stem, Path(name).suffix
            filename = name if index == 0 else f"{stem}_{index + 1}{suffix}"
            message.add_attachment(inhalt.encode("utf-8"), maintype="text",
                                   subtype="csv", filename=filename)
        path = Path(self.tmp) / "angebot.eml"
        path.write_bytes(message.as_bytes())
        return str(path)

    def importiere(self, **kwargs) -> Offer:
        service = OfferImportService(self.settings, InMemoryProfileStore())
        return service.import_file(self.eml(**kwargs))


# ==========================================================================
# 1. Textzerlegung
# ==========================================================================

class TextzerlegungTests(unittest.TestCase):

    def test_01_saetze_werden_getrennt(self) -> None:
        segmente = mail_segments("Erster Satz. Zweiter Satz.\nDritte Zeile.")
        self.assertEqual(segmente, ["Erster Satz", "Zweiter Satz", "Dritte Zeile"])

    def test_02_datum_bleibt_ganz(self) -> None:
        """'01.09.2026' darf nicht in drei Bruchstuecke zerfallen."""
        segmente = mail_segments("Die Preise gelten ab 01.09.2026, Zahlungsziel "
                                 "30 Tage netto.")
        self.assertEqual(len(segmente), 1)
        self.assertIn("01.09.2026", segmente[0])

    def test_03_leerer_text_liefert_nichts(self) -> None:
        self.assertEqual(mail_segments(""), [])
        self.assertEqual(mail_segments("   \n\n  "), [])


# ==========================================================================
# 2. Kopfdaten aus dem Mailtext
# ==========================================================================

class KopfdatenAusDerMailTests(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = Settings()

    def _anwenden(self, offer: Offer, text: str) -> list[str]:
        return apply_email_header(offer, extract_header_fields(text), self.settings)

    def test_10_gueltig_ab_wird_ergaenzt(self) -> None:
        offer = anhang_angebot()
        self._anwenden(offer, "Die Preise gelten ab 01.09.2026.")
        self.assertEqual(offer.valid_from.isoformat(), "2026-09-01")

    def test_11_zahlungsbedingungen_werden_ergaenzt(self) -> None:
        offer = anhang_angebot()
        self._anwenden(offer, "Zahlungsziel 30 Tage netto.")
        self.assertIn("30 Tage netto", offer.payment_terms)

    def test_12_widerspruch_die_mail_gewinnt(self) -> None:
        offer = anhang_angebot()
        offer.set_field("payment_terms", "60 Tage netto", FieldOrigin.EXTRACTED)
        notizen = self._anwenden(offer, "Zahlungsziel: 30 Tage netto")
        self.assertIn("30 Tage netto", offer.payment_terms)
        self.assertTrue(any("Widerspruch im Kopf" in n for n in notizen), notizen)

    def test_13_widerspruch_wird_als_unsicher_markiert(self) -> None:
        offer = anhang_angebot()
        offer.set_field("payment_terms", "60 Tage netto", FieldOrigin.EXTRACTED)
        self._anwenden(offer, "Zahlungsziel: 30 Tage netto")
        self.assertIs(offer.origin("payment_terms"), FieldOrigin.UNCERTAIN)

    def test_14_ohne_vorrang_bleibt_der_anhang_stehen(self) -> None:
        self.settings.extraction.email_header_wins_over_attachment = False
        offer = anhang_angebot()
        offer.set_field("payment_terms", "60 Tage netto", FieldOrigin.EXTRACTED)
        self._anwenden(offer, "Zahlungsziel: 30 Tage netto")
        self.assertEqual(offer.payment_terms, "60 Tage netto")

    def test_15_ohne_unsicherheitsmarkierung_bleibt_es_extrahiert(self) -> None:
        self.settings.extraction.conflict_marks_uncertain = False
        offer = anhang_angebot()
        offer.set_field("payment_terms", "60 Tage netto", FieldOrigin.EXTRACTED)
        self._anwenden(offer, "Zahlungsziel: 30 Tage netto")
        self.assertIn("30 Tage netto", offer.payment_terms)
        self.assertIsNot(offer.origin("payment_terms"), FieldOrigin.UNCERTAIN)


# ==========================================================================
# 3. Positionsergaenzungen aus dem Mailtext
# ==========================================================================

class ErgaenzungenTests(unittest.TestCase):

    def setUp(self) -> None:
        self.settings = Settings()
        self.offer = anhang_angebot()

    def _anwenden(self, text: str) -> list[str]:
        return apply_email_supplements(self.offer, text, self.settings)

    # -- Mindestmenge ---------------------------------------------------
    def test_20_mindestmenge_wird_ergaenzt(self) -> None:
        self._anwenden("Fuer Artikel 47110001 gilt abweichend eine Mindestmenge "
                       "von 500 Stueck.")
        self.assertEqual(position_mit(self.offer, "47110001").min_order_qty,
                         Decimal("500"))

    def test_21_mindestmenge_ist_nachvollziehbar(self) -> None:
        notizen = self._anwenden("Fuer Artikel 47110001 gilt abweichend eine "
                                 "Mindestmenge von 500 Stueck.")
        text = " ".join(notizen)
        self.assertIn("Mindestmenge", text)
        self.assertIn("47110001", text)
        self.assertIn("Mailtext", text)

    def test_22_ergaenzte_position_zeigt_zwei_quellen(self) -> None:
        self._anwenden("Fuer Artikel 47110001 gilt abweichend eine Mindestmenge "
                       "von 500 Stueck.")
        hinweis = position_mit(self.offer, "47110001").source_hint
        self.assertIn("Anhang", hinweis)
        self.assertIn(MAIL_SOURCE_MARK, hinweis)

    def test_23_andere_positionen_bleiben_unberuehrt(self) -> None:
        self._anwenden("Fuer Artikel 47110001 gilt eine Mindestmenge von 500 Stueck.")
        self.assertIsNone(position_mit(self.offer, "47110002").min_order_qty)

    # -- Preis ----------------------------------------------------------
    def test_24_ueberholter_preis_wird_gesetzt(self) -> None:
        self._anwenden("Der Preis fuer 47110002 im Anhang ist ueberholt, "
                       "es gilt 9,10 EUR.")
        self.assertEqual(position_mit(self.offer, "47110002").price, Decimal("9.10"))

    def test_25_widersprochener_preis_ist_unsicher(self) -> None:
        self._anwenden("Der Preis fuer 47110002 im Anhang ist ueberholt, "
                       "es gilt 9,10 EUR.")
        self.assertIn("price", position_mit(self.offer, "47110002").uncertain_fields)

    def test_26_preiswiderspruch_steht_im_protokoll(self) -> None:
        notizen = self._anwenden("Der Preis fuer 47110002 im Anhang ist ueberholt, "
                                 "es gilt 9,10 EUR.")
        text = " ".join(notizen)
        self.assertIn("Widerspruch", text)
        self.assertIn("8,90", text, "der verdraengte Wert muss nachlesbar bleiben")
        self.assertIn("9,10", text)

    def test_27_ohne_unsicherheitsmarkierung_bleibt_es_extrahiert(self) -> None:
        self.settings.extraction.conflict_marks_uncertain = False
        self._anwenden("Der Preis fuer 47110002 ist ueberholt, es gilt 9,10 EUR.")
        position = position_mit(self.offer, "47110002")
        self.assertEqual(position.price, Decimal("9.10"))
        self.assertEqual(position.uncertain_fields, [])

    def test_28_preis_ohne_widerspruchswort_wird_nicht_uebernommen(self) -> None:
        """Ein blosser Betrag im Fliesstext aendert nichts am Anhang."""
        self._anwenden("Artikel 47110002 haben wir 2025 fuer 9,10 EUR geliefert.")
        self.assertEqual(position_mit(self.offer, "47110002").price, Decimal("8.90"))

    def test_29_preis_ohne_waehrung_wird_nicht_geraten(self) -> None:
        self._anwenden("Der Preis fuer 47110002 ist ueberholt, es gilt 9,10.")
        self.assertEqual(position_mit(self.offer, "47110002").price, Decimal("8.90"))

    # -- Streichung ------------------------------------------------------
    def test_30_gestrichene_position_wird_abgewaehlt(self) -> None:
        self._anwenden("Position 30 entfaellt.")
        self.assertFalse(position_mit(self.offer, "47110003").selected)

    def test_31_gestrichene_position_bleibt_erhalten(self) -> None:
        self._anwenden("Position 30 entfaellt.")
        self.assertEqual(len(self.offer.positions), 3,
                         "nichts wird stillschweigend geloescht")

    def test_32_streichung_hinterlaesst_eine_bemerkung(self) -> None:
        self._anwenden("Position 30 entfaellt.")
        self.assertIn("gestrichen", position_mit(self.offer, "47110003").remarks)

    def test_33_streichung_ueber_die_artikelnummer(self) -> None:
        self._anwenden("Der Artikel 47110003 entfaellt ersatzlos.")
        self.assertFalse(position_mit(self.offer, "47110003").selected)

    def test_34_streichung_nimmt_die_haken_zurueck(self) -> None:
        self._anwenden("Position 30 entfaellt.")
        position = position_mit(self.offer, "47110003")
        self.assertFalse(position.do_info_record)
        self.assertFalse(position.is_processable)

    # -- Zusatzposition ---------------------------------------------------
    def test_35_neue_position_kommt_dazu(self) -> None:
        self._anwenden("Position 30 entfaellt, dafuer neu: 47110009 zu 4,20 EUR.")
        neue = position_mit(self.offer, "47110009")
        self.assertEqual(neue.price, Decimal("4.20"))
        self.assertIs(neue.source_kind, SourceKind.EMAIL_BODY)

    def test_36_neue_position_ist_als_mailherkunft_erkennbar(self) -> None:
        self._anwenden("dafuer neu: 47110009 zu 4,20 EUR")
        self.assertIn("Mailtext", position_mit(self.offer, "47110009").source_hint)

    def test_37_bekannte_nummer_wird_nicht_doppelt_angelegt(self) -> None:
        self._anwenden("neu: 47110001 zu 4,20 EUR")
        nummern = [p.material_number for p in self.offer.positions]
        self.assertEqual(nummern.count("47110001"), 1)

    def test_38_fremde_nummer_landet_beim_lieferantenmaterial(self) -> None:
        """'DR-40527' passt nicht auf unseren Nummernkreis."""
        self._anwenden("dafuer neu: DR-40527 zu 4,20 EUR")
        neue = [p for p in self.offer.positions
                if p.vendor_material_number == "DR-40527"]
        self.assertEqual(len(neue), 1, [p.material_number for p in self.offer.positions])
        self.assertEqual(neue[0].material_number, "")

    # -- weitere Angaben ---------------------------------------------------
    def test_39_lieferzeit_wird_ergaenzt(self) -> None:
        self._anwenden("Fuer 47110001 betraegt die Lieferzeit 21 Tage.")
        self.assertEqual(position_mit(self.offer, "47110001").lead_time_days, 21)

    def test_40_gueltig_ab_je_artikel(self) -> None:
        self._anwenden("Fuer Artikel 47110002 gilt ab 01.10.2026 der neue Preis.")
        self.assertEqual(
            position_mit(self.offer, "47110002").valid_from.isoformat(), "2026-10-01")

    def test_41_ohne_artikelbezug_passiert_nichts(self) -> None:
        notizen = self._anwenden("Die Lieferzeit betraegt 21 Tage.")
        self.assertEqual(notizen, [])
        self.assertIsNone(position_mit(self.offer, "47110001").lead_time_days)

    def test_42_leerer_mailtext_aendert_nichts(self) -> None:
        self.assertEqual(self._anwenden(""), [])
        self.assertEqual(len(self.offer.positions), 3)

    def test_43_jede_uebernahme_erzeugt_genau_eine_notiz(self) -> None:
        notizen = self._anwenden(MAIL_TEXT)
        uebernahmen = [n for n in notizen if "konnte keiner Position" not in n]
        self.assertEqual(len(uebernahmen), 4, notizen)

    def test_44_kopfsatz_ohne_positionsbezug_wird_als_hinweis_gemeldet(self) -> None:
        """Der Satz zu Gueltigkeit/Zahlungsziel betrifft keine Position.

        Im vollstaendigen Ablauf wird er als *Kopfangabe* ausgewertet; hier
        steht die Erkennung fuer sich, und dann zaehlt nur eines: der Satz darf
        nicht stillschweigend verschwinden, solange ihn niemand aufgegriffen hat.
        """
        notizen = self._anwenden(MAIL_TEXT)
        hinweise = [n for n in notizen if "konnte keiner Position" in n]
        self.assertEqual(len(hinweise), 1, notizen)
        self.assertIn("01.09.2026", hinweise[0])


# ==========================================================================
# 4. Der ganze Weg: .eml mit Anhang
# ==========================================================================

class DurchgaengigerImportTests(MailAnhangCase):

    def test_50_kopf_aus_der_mail_positionen_aus_dem_anhang(self) -> None:
        offer = self.importiere()
        self.assertEqual(offer.valid_from.isoformat(), "2026-09-01")
        self.assertIn("30 Tage netto", offer.payment_terms)
        self.assertTrue(any(p.source_kind is SourceKind.EMAIL_ATTACHMENT
                            for p in offer.positions))

    def test_51_alle_vier_beispielfaelle_greifen(self) -> None:
        offer = self.importiere()
        self.assertEqual(position_mit(offer, "47110001").min_order_qty,
                         Decimal("500"))
        self.assertEqual(position_mit(offer, "47110002").price, Decimal("9.10"))
        self.assertFalse(position_mit(offer, "47110003").selected)
        self.assertEqual(position_mit(offer, "47110009").price, Decimal("4.20"))

    def test_52_keine_position_geht_verloren(self) -> None:
        offer = self.importiere()
        self.assertEqual(len(offer.positions), 4,
                         "3 aus dem Anhang + 1 aus dem Mailtext")

    def test_53_mailtext_erzeugt_keine_doppelten_positionen(self) -> None:
        offer = self.importiere()
        nummern = [p.material_number for p in offer.positions]
        self.assertEqual(len(nummern), len(set(nummern)), nummern)

    def test_54_zusammenfuehrung_steht_im_protokoll(self) -> None:
        offer = self.importiere()
        self.assertTrue(any("zusammengefuehrt" in n for n in offer.extraction_notes),
                        offer.extraction_notes)

    def test_55_mail_ohne_anhang_liefert_positionen_aus_dem_text(self) -> None:
        offer = self.importiere(
            body="Artikel 47110001, Dichtring NBR: 12,85 EUR je Stueck\n",
            anhaenge=0)
        self.assertTrue(offer.positions)
        self.assertTrue(any(p.source_kind is SourceKind.EMAIL_BODY
                            for p in offer.positions))

    def test_56_anhang_ohne_mailtext(self) -> None:
        offer = self.importiere(body="")
        self.assertEqual(len(offer.positions), 3)
        self.assertTrue(all(p.selected for p in offer.positions))

    def test_57_mehrere_anhaenge(self) -> None:
        offer = self.importiere(body="Position 30 entfaellt.\n", anhaenge=2)
        self.assertEqual(len(offer.positions), 6, "beide Anhaenge werden gelesen")
        gestrichen = [p for p in offer.positions if not p.selected]
        self.assertEqual(len(gestrichen), 2, "die Streichung trifft beide Anhaenge")

    def test_58_anhang_ohne_positionen_faellt_auf_den_mailtext_zurueck(self) -> None:
        offer = self.importiere(
            body="Artikel 47110001, Dichtring NBR: 12,85 EUR je Stueck\n",
            inhalt="Pos;Material;Preis\n")
        self.assertTrue(offer.positions, offer.extraction_notes)
        self.assertTrue(any(p.source_kind is SourceKind.EMAIL_BODY
                            for p in offer.positions))

    def test_59_zusammenfuehrung_laesst_sich_abschalten(self) -> None:
        self.settings.extraction.merge_email_and_attachment = False
        offer = self.importiere()
        self.assertTrue(position_mit(offer, "47110003").selected,
                        "ohne Zusammenfuehrung wird nichts abgewaehlt")
        self.assertFalse(any("zusammengefuehrt" in n
                             for n in offer.extraction_notes))

    def test_60_ergaenzungen_lassen_sich_einzeln_abschalten(self) -> None:
        self.settings.extraction.apply_email_supplements = False
        offer = self.importiere()
        self.assertEqual(position_mit(offer, "47110002").price, Decimal("8.90"),
                         "der Anhangwert bleibt unangetastet")
        self.assertEqual(offer.valid_from.isoformat(), "2026-09-01",
                         "die Kopfdaten der Mail gelten weiterhin")

    def test_61_import_stuerzt_bei_wirrem_mailtext_nicht_ab(self) -> None:
        offer = self.importiere(body="entfaellt neu: zu gilt ab ueberholt ;;; 4,20\n")
        self.assertEqual(len(offer.positions), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
