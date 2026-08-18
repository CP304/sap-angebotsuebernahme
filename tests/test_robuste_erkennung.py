"""Robuste Erkennung: die fuenf Achsen, an denen echte Angebote scheitern.

Grundlage ist eine Messung ueber 600 kombinatorisch erzeugte Belege
(Trennzeichen x Zahlenformat x Spaltennamen x Reihenfolge x Vorspann x
Sonderfaelle).  Fuenf Achsen fielen dort deutlich ab:

    Komma als Trennzeichen        28 %
    "auf Anfrage"-Positionen      35 %
    ohne Kopfzeile                43 %
    Kopfzeile mitten in Tabelle   62 %
    Menge mit angehaengter ME     68 %

Dieses Modul haelt fuer jede Achse fest, was seitdem gilt.  Je Achse gibt
es drei Sorten Test:

    * Gutfall      -- der Normalfall muss richtig herauskommen
    * Grenzfall    -- der schwierige Fall muss richtig ODER gemeldet sein
    * darf-nicht-anschlagen -- die Regel darf heile Belege nicht verderben

Die letzte Sorte ist die wichtigste.  Eine hoehere Trefferquote, die
stille Fehler einfuehrt, waere eine Verschlechterung: lieber ein Befund
im Klartext als ein falscher Preis ohne Warnung.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_robust_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                            # noqa: E402
from app.models.enums import FieldOrigin                            # noqa: E402
from app.services.offer_import_service import OfferImportService    # noqa: E402
from app.services.readers.excel_reader import (                     # noqa: E402
    detect_delimiter,
    repair_decimal_split_rows,
)


class _ImportHilfe(unittest.TestCase):
    """Gemeinsames Geruest: Text als Datei ablegen und einlesen."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dienst = OfferImportService(Settings())
        cls.ordner = Path(tempfile.mkdtemp(prefix="robust_belege_"))

    def _import(self, name: str, inhalt: str, endung: str = ".csv"):
        pfad = self.ordner / f"{self.__class__.__name__}_{name}{endung}"
        pfad.write_text(inhalt, encoding="utf-8")
        return self.dienst.import_file(str(pfad))

    # -- kleine Lesehilfen ---------------------------------------------
    @staticmethod
    def _preise(angebot) -> list[Decimal | None]:
        return [p.price for p in angebot.positions]

    @staticmethod
    def _mengen(angebot) -> list[Decimal | None]:
        return [p.quantity for p in angebot.positions]

    @staticmethod
    def _materialien(angebot) -> list[str]:
        return [(p.material_number or "").lstrip("0") for p in angebot.positions]

    @staticmethod
    def _alle_meldungen(angebot) -> str:
        teile = [f"{i.code} {i.message}" for i in angebot.issues]
        for position in angebot.positions:
            teile += [f"{i.code} {i.message}" for i in position.issues]
        teile += list(angebot.extraction_notes)
        return " ".join(teile).lower()

    def _kein_stiller_fehler(self, angebot, erwartet: dict[str, Decimal]) -> None:
        """Jeder abweichende Preis MUSS gemeldet oder als unsicher markiert sein.

        Das ist die Bedingung, die ueber allem steht.  Falsch sein darf die
        Erkennung -- still falsch sein darf sie nicht.
        """
        meldungen = self._alle_meldungen(angebot)
        for position in angebot.positions:
            nummer = (position.material_number or "").lstrip("0")
            soll = erwartet.get(nummer)
            if soll is None or position.price == soll:
                continue
            unsicher = position.field_origins.get("price") in (
                FieldOrigin.UNCERTAIN, FieldOrigin.MISSING)
            self.assertTrue(
                unsicher or meldungen,
                f"Position {nummer}: Preis {position.price} statt {soll} -- "
                "ohne Befund und ohne Markierung als unsicher")


# ==========================================================================
# 1. Komma als Trennzeichen
# ==========================================================================

class KommaTrennzeichenTest(_ImportHilfe):
    """Dezimalkomma gegen Feldtrenner -- der schwierigste Fall."""

    DEUTSCH_MIT_KOMMA = (
        "Artikel,Bezeichnung,Menge,ME,Einzelpreis,Gesamt\n"
        "4711000,Dichtring Typ 0,100,ST,127,00,12.700,00\n"
        "4711001,Dichtring Typ 1,10,ST,0,85,8,50\n"
        "4711002,Dichtring Typ 2,1000,ST,38,50,38.500,00\n"
    )

    ENGLISCH_SAUBER = (
        "Item,Description,Quantity,Unit,Unit Price\n"
        "4711000,Sealing ring,500,PC,2.95\n"
        "4711001,O-ring,250,PC,1.15\n"
    )

    # -- Gutfall -------------------------------------------------------
    def test_deutsche_liste_mit_komma_wird_geheilt(self):
        """"127" und "00" gehoeren zusammen -- 127,00, nicht 127."""
        angebot = self._import("de_komma", self.DEUTSCH_MIT_KOMMA)
        self.assertEqual(self._preise(angebot),
                         [Decimal("127.00"), Decimal("0.85"), Decimal("38.50")])

    def test_deutsche_liste_mit_komma_behaelt_die_mengen(self):
        angebot = self._import("de_komma_menge", self.DEUTSCH_MIT_KOMMA)
        self.assertEqual(self._mengen(angebot),
                         [Decimal("100"), Decimal("10"), Decimal("1000")])

    def test_geheilte_zeilen_werden_gemeldet(self):
        """Zusammenfuegen ist eine Entscheidung -- sie gehoert ins Protokoll."""
        angebot = self._import("de_komma_befund", self.DEUTSCH_MIT_KOMMA)
        self.assertIn("trennzeichen", self._alle_meldungen(angebot))

    # -- darf nicht anschlagen ------------------------------------------
    def test_englische_liste_bleibt_unangetastet(self):
        angebot = self._import("en_sauber", self.ENGLISCH_SAUBER)
        self.assertEqual(self._preise(angebot),
                         [Decimal("2.95"), Decimal("1.15")])

    def test_englische_liste_ohne_reparaturmeldung(self):
        """Eine heile Datei darf keine Trennzeichenwarnung ausloesen."""
        angebot = self._import("en_still", self.ENGLISCH_SAUBER)
        self.assertNotIn("zerrissene betraege", self._alle_meldungen(angebot))

    def test_gleich_breite_zeilen_werden_nicht_angefasst(self):
        rows = [["Artikel", "Preis"], ["4711000", "2.95"], ["4711001", "1.15"]]
        geheilt = repair_decimal_split_rows([list(r) for r in rows])
        self.assertEqual(geheilt[0], rows)
        self.assertEqual((geheilt[1], geheilt[2]), (0, 0))

    def test_einzelne_ziffer_gilt_nicht_als_cent_stelle(self):
        """"...,50,0,..." ist Menge und Rabatt -- nicht "50,0"."""
        rows = [["Artikel", "Menge", "Rabatt", "Preis"],
                ["4711000", "50", "0", "2", "95"],
                ["4711001", "10", "0", "1", "15"]]
        geheilt, anzahl, _offen = repair_decimal_split_rows(rows)
        self.assertEqual(geheilt[1], ["4711000", "50", "0", "2,95"])
        self.assertEqual(anzahl, 2)

    def test_mehrdeutige_zeile_bleibt_stehen(self):
        """Zwei moegliche Deutungen -> nichts zusammenfuegen, melden."""
        rows = [["a", "b", "c"],
                ["1", "1", "1", "1"]]
        geheilt, anzahl, offen = repair_decimal_split_rows(rows)
        self.assertEqual(geheilt[1], ["1", "1", "1", "1"])
        self.assertEqual((anzahl, offen), (0, 1))

    # -- Grenzfall -----------------------------------------------------
    def test_englische_tausendertrennung_wird_gemeldet_nicht_geraten(self):
        """"1,250.00" bei Komma als Trenner ist echt mehrdeutig."""
        text = ("Item,Description,Quantity,Unit Price\n"
                "4711000,Sealing ring,10,1,250.00\n"
                "4711001,O-ring,20,1,250.00\n")
        angebot = self._import("en_tausender", text)
        self.assertIn("trennzeichen", self._alle_meldungen(angebot))

    def test_komma_liste_ohne_stillen_fehler(self):
        angebot = self._import("de_komma_still", self.DEUTSCH_MIT_KOMMA)
        self._kein_stiller_fehler(angebot, {
            "4711000": Decimal("127.00"),
            "4711001": Decimal("0.85"),
            "4711002": Decimal("38.50")})

    def test_stellen_werden_aus_eindeutigen_zeilen_gelernt(self):
        """Die Preisspalte liegt in jeder Zeile an derselben Stelle.

        Zeile 2 waere fuer sich genommen mehrdeutig ("0,12" saehe genauso
        aus wie "12,40").  Die uebrigen Zeilen zeigen aber, wo der Betrag
        steht -- damit ist auch sie aufloesbar.
        """
        rows = [["Rabatt", "Preis", "Text", "Menge"],
                ["0", "2", "95", "Dichtring", "500"],
                ["0", "12", "40", "Dichtring", "100"],
                ["0", "0", "85", "Dichtring", "50"]]
        geheilt, anzahl, offen = repair_decimal_split_rows(rows)
        self.assertEqual([r[1] for r in geheilt[1:]], ["2,95", "12,40", "0,85"])
        self.assertEqual((anzahl, offen), (3, 0))


class TrennzeichenWahlTest(unittest.TestCase):
    """Welches Zeichen trennt die Spalten?  Probeweise zerlegen statt zaehlen."""

    TABELLE = (
        "Pos;Artikelnummer;Bezeichnung;Menge;ME;Einzelpreis;Waehrung\n"
        "10;4711001;Dichtring NBR 40x52x7;500;ST;2,95;EUR\n"
        "20;4711002;Wellendichtring;250;ST;12,40;EUR\n"
    )
    FUSSTEXT_MIT_KOMMA = (
        "\nZahlungsbedingungen: 2 % Skonto bei Zahlung innerhalb 14 Tagen, "
        "30 Tage netto.\n"
        "Lieferung DDP 12345 Musterstadt, alle Preise zzgl. Verpackung.\n"
    )
    FUSSTEXT_OHNE_KOMMA = (
        "\nZahlungsbedingungen: 30 Tage netto.\n"
        "Lieferung DDP 12345 Musterstadt.\n"
    )

    # -- Gutfall -------------------------------------------------------
    def test_nur_tabelle(self):
        self.assertEqual(detect_delimiter(self.TABELLE), ";")

    def test_fusstext_ohne_komma(self):
        self.assertEqual(
            detect_delimiter(self.TABELLE + self.FUSSTEXT_OHNE_KOMMA), ";")

    # -- der eigentliche Ausloeser --------------------------------------
    def test_fusstext_mit_komma_kippt_die_wahl_nicht(self):
        """Zahlungsbedingungen stehen unter fast jedem Angebot.

        Deutsche Prosa ist voller Kommas und enthaelt keine Semikolons.
        Beim blossen Zaehlen gewann dadurch das Komma -- die Tabelle blieb
        ungetrennt, "2,95" zerfiel obendrein, und der Beleg ergab null
        Positionen, ohne dass jemand ein Trennzeichenproblem sah.
        """
        self.assertEqual(
            detect_delimiter(self.TABELLE + self.FUSSTEXT_MIT_KOMMA), ";",
            "Fliesstext im Fussbereich darf die Trennzeichenwahl nicht kippen")

    def test_fusstext_mit_komma_liefert_weiter_positionen(self):
        dienst = OfferImportService(Settings())
        pfad = Path(_TEMP_HOME) / "fusstext.csv"
        pfad.write_text(self.TABELLE + self.FUSSTEXT_MIT_KOMMA, encoding="utf-8")
        angebot = dienst.import_file(str(pfad))
        self.assertEqual(len(angebot.positions), 2)
        self.assertEqual(angebot.positions[0].price, Decimal("2.95"))

    def test_briefkopf_und_fusstext_zusammen(self):
        text = ("Angebot ANG-2026-9001 vom 18.08.2026\nBeispiel GmbH\n\n"
                + self.TABELLE + self.FUSSTEXT_MIT_KOMMA)
        self.assertEqual(detect_delimiter(text), ";")

    # -- darf nicht anschlagen ------------------------------------------
    def test_echte_komma_datei_gewinnt_weiter(self):
        text = ("Item,Description,Quantity,Unit,Price\n"
                "10,Sealing ring,500,PC,2.95\n"
                "20,O-ring,250,PC,1.15\n")
        self.assertEqual(detect_delimiter(text), ",")

    def test_echte_komma_datei_mit_fusstext(self):
        text = ("Item,Description,Quantity,Unit,Price\n"
                "10,Sealing ring,500,PC,2.95\n"
                "20,O-ring,250,PC,1.15\n"
                "\nPayment: 30 days net, delivery DDP.\n")
        self.assertEqual(detect_delimiter(text), ",")

    def test_tabulator_mit_fusstext_mit_komma(self):
        text = self.TABELLE.replace(";", "\t") + self.FUSSTEXT_MIT_KOMMA
        self.assertEqual(detect_delimiter(text), "\t")

    def test_pipe_mit_fusstext_mit_komma(self):
        text = self.TABELLE.replace(";", "|") + self.FUSSTEXT_MIT_KOMMA
        self.assertEqual(detect_delimiter(text), "|")

    def test_trennzeichen_im_text_zaehlt_nicht_mit(self):
        """In Anfuehrungszeichen ist ein Komma kein Trenner."""
        text = ('Artikel;Bezeichnung;Preis\n'
                '4711000;"Dichtring, gross";2,95\n'
                '4711001;"O-Ring, klein";1,15\n')
        self.assertEqual(detect_delimiter(text), ";")

    def test_reiner_fliesstext_stuerzt_nicht_ab(self):
        text = ("Sehr geehrte Damen und Herren,\n"
                "anbei unser Angebot, wie besprochen.\n")
        self.assertIn(detect_delimiter(text), (";", ",", "\t", "|"))


# ==========================================================================
# 2. Tabellen ohne Kopfzeile
# ==========================================================================

class OhneKopfzeileTest(_ImportHilfe):
    """Zuordnung ueber den Spalteninhalt statt ueber Ueberschriften."""

    OHNE_KOPF = (
        "4711000\tDichtring Typ 0\t100\tST\t38,50\n"
        "4711001\tDichtring Typ 1\t250\tST\t1,15\n"
        "4711002\tDichtring Typ 2\t500\tST\t12,40\n"
    )

    # -- Gutfall -------------------------------------------------------
    def test_positionen_entstehen(self):
        angebot = self._import("ohne_kopf", self.OHNE_KOPF)
        self.assertEqual(len(angebot.positions), 3)

    def test_preise_stimmen(self):
        angebot = self._import("ohne_kopf_preis", self.OHNE_KOPF)
        self.assertEqual(self._preise(angebot),
                         [Decimal("38.50"), Decimal("1.15"), Decimal("12.40")])

    def test_mengen_stimmen(self):
        angebot = self._import("ohne_kopf_menge", self.OHNE_KOPF)
        self.assertEqual(self._mengen(angebot),
                         [Decimal("100"), Decimal("250"), Decimal("500")])

    def test_materialnummern_stimmen(self):
        angebot = self._import("ohne_kopf_mat", self.OHNE_KOPF)
        self.assertEqual(self._materialien(angebot),
                         ["4711000", "4711001", "4711002"])

    def test_ohne_kopfzeile_wird_gemeldet(self):
        """Ueber den Inhalt zugeordnet heisst: bitte einmal hinsehen."""
        angebot = self._import("ohne_kopf_befund", self.OHNE_KOPF)
        meldungen = self._alle_meldungen(angebot)
        self.assertIn("keine kopfzeile erkannt", meldungen)
        self.assertIn("ueber den datentyp als 'price' erkannt", meldungen)

    # -- Grenzfall -----------------------------------------------------
    def test_briefkopf_ueber_kopfloser_tabelle(self):
        angebot = self._import("ohne_kopf_brief",
                               "Preisliste 2026\n" + self.OHNE_KOPF)
        self.assertEqual(len(angebot.positions), 3)

    def test_summenzeile_unter_kopfloser_tabelle(self):
        """Die Summenzeile darf nicht als Kopfzeile durchgehen.

        Genau das ist passiert: "Gesamtsumme" steht als Zeilensumme im
        Aliaskatalog, die Zeile gewann die Kopfzeilensuche, und der
        Datenbeginn rutschte hinter das Tabellenende -- null Positionen.
        """
        text = self.OHNE_KOPF + "Gesamtsumme\t\t\t\t9.717,50\n"
        angebot = self._import("ohne_kopf_summe", text)
        self.assertEqual(len(angebot.positions), 3)

    def test_summenzeile_wird_nicht_zur_position(self):
        text = self.OHNE_KOPF + "Gesamtsumme\t\t\t\t9.717,50\n"
        angebot = self._import("ohne_kopf_summe2", text)
        self.assertNotIn(Decimal("9717.50"), self._preise(angebot))

    def test_ohne_kopf_ohne_stillen_fehler(self):
        angebot = self._import("ohne_kopf_still", self.OHNE_KOPF)
        self._kein_stiller_fehler(angebot, {
            "4711000": Decimal("38.50"),
            "4711001": Decimal("1.15"),
            "4711002": Decimal("12.40")})

    # -- darf nicht anschlagen ------------------------------------------
    def test_tabelle_mit_kopfzeile_bleibt_ueber_kopfzeile_zugeordnet(self):
        text = ("Artikel\tBezeichnung\tMenge\tME\tEinzelpreis\n" + self.OHNE_KOPF)
        angebot = self._import("mit_kopf", text)
        self.assertEqual(len(angebot.positions), 3)
        self.assertEqual(self._preise(angebot),
                         [Decimal("38.50"), Decimal("1.15"), Decimal("12.40")])


# ==========================================================================
# 3. Kopfzeile mitten in der Tabelle (Seitenumbruch)
# ==========================================================================

class WiederholteKopfzeileTest(_ImportHilfe):
    """Beim Seitenumbruch wiederholt der Lieferant die Ueberschrift."""

    KOPF = "Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
    ZEILEN = [
        "4711000;Dichtring Typ 0;100;ST;38,50\n",
        "4711001;Dichtring Typ 1;250;ST;1,15\n",
        "4711002;Dichtring Typ 2;500;ST;12,40\n",
        "4711003;Dichtring Typ 3;50;ST;2,95\n",
    ]

    MIT_WIEDERHOLUNG = (KOPF + ZEILEN[0] + ZEILEN[1] + KOPF + ZEILEN[2] + ZEILEN[3])
    #: Kein Kopf am Anfang -- er taucht erst beim Seitenumbruch auf
    NUR_IN_DER_MITTE = (ZEILEN[0] + ZEILEN[1] + KOPF + ZEILEN[2] + ZEILEN[3])

    # -- Gutfall -------------------------------------------------------
    def test_alle_vier_positionen(self):
        angebot = self._import("wdh", self.MIT_WIEDERHOLUNG)
        self.assertEqual(len(angebot.positions), 4)

    def test_wiederholte_kopfzeile_wird_keine_position(self):
        angebot = self._import("wdh_keine_pos", self.MIT_WIEDERHOLUNG)
        self.assertNotIn("Bezeichnung",
                         [p.description or "" for p in angebot.positions])

    def test_preise_bleiben_vollstaendig(self):
        angebot = self._import("wdh_preise", self.MIT_WIEDERHOLUNG)
        self.assertEqual(
            sorted(self._preise(angebot)),
            sorted([Decimal("38.50"), Decimal("1.15"),
                    Decimal("12.40"), Decimal("2.95")]))

    # -- Grenzfall: der Kopf steht NUR in der Mitte ----------------------
    def test_kopf_nur_in_der_mitte_verliert_nichts(self):
        """Die Zeilen ueber der Ueberschrift sind die ersten Positionen.

        Ohne diese Regel wurde die Ueberschrift mitten in der Tabelle als
        *die* Kopfzeile gelesen -- und alles darueber verschwand
        stillschweigend.
        """
        angebot = self._import("wdh_mitte", self.NUR_IN_DER_MITTE)
        self.assertEqual(len(angebot.positions), 4)

    def test_kopf_nur_in_der_mitte_findet_die_ersten_materialien(self):
        angebot = self._import("wdh_mitte_mat", self.NUR_IN_DER_MITTE)
        self.assertIn("4711000", self._materialien(angebot))
        self.assertIn("4711001", self._materialien(angebot))

    def test_kopf_nur_in_der_mitte_wird_gemeldet(self):
        angebot = self._import("wdh_mitte_befund", self.NUR_IN_DER_MITTE)
        self.assertIn("steht in dieser tabelle mehrfach",
                      self._alle_meldungen(angebot))

    # -- darf nicht anschlagen ------------------------------------------
    def test_briefkopf_wird_nicht_zur_position(self):
        """Ein Briefkopf ueber der Kopfzeile bleibt aussen vor."""
        text = ("Angebot ANG-2026-9001 vom 18.08.2026\n"
                "Muster Dichtungstechnik GmbH\n\n"
                + self.KOPF + self.ZEILEN[0] + self.ZEILEN[1])
        angebot = self._import("brief_keine_pos", text)
        self.assertEqual(len(angebot.positions), 2)

    def test_gewoehnliche_tabelle_unveraendert(self):
        text = self.KOPF + "".join(self.ZEILEN)
        angebot = self._import("gewoehnlich", text)
        self.assertEqual(len(angebot.positions), 4)
        self.assertEqual(self._preise(angebot)[0], Decimal("38.50"))

    def test_wiederholung_ohne_stillen_fehler(self):
        angebot = self._import("wdh_still", self.NUR_IN_DER_MITTE)
        self._kein_stiller_fehler(angebot, {
            "4711000": Decimal("38.50"), "4711001": Decimal("1.15"),
            "4711002": Decimal("12.40"), "4711003": Decimal("2.95")})


# ==========================================================================
# 4. Menge mit angehaengter Einheit
# ==========================================================================

class MengeMitEinheitTest(_ImportHilfe):
    """"500 ST" in der Mengenspalte: Zahl und Einheit trennen."""

    OHNE_ME_SPALTE = (
        "Artikel;Bezeichnung;Menge;Einzelpreis\n"
        "4711000;Dichtring Typ 0;500 ST;2,95\n"
        "4711001;Dichtring Typ 1;250 Stk;1,15\n"
        "4711002;Dichtring Typ 2;1.000 kg;12,40\n"
    )

    # -- Gutfall -------------------------------------------------------
    def test_menge_wird_zur_zahl(self):
        angebot = self._import("einheit", self.OHNE_ME_SPALTE)
        self.assertEqual(self._mengen(angebot),
                         [Decimal("500"), Decimal("250"), Decimal("1000")])

    def test_preis_bleibt_unberuehrt(self):
        angebot = self._import("einheit_preis", self.OHNE_ME_SPALTE)
        self.assertEqual(self._preise(angebot),
                         [Decimal("2.95"), Decimal("1.15"), Decimal("12.40")])

    def test_einheit_wandert_in_die_mengeneinheit(self):
        angebot = self._import("einheit_me", self.OHNE_ME_SPALTE)
        self.assertEqual(angebot.positions[0].uom, "ST")

    def test_abweichende_schreibweise_wird_normiert(self):
        angebot = self._import("einheit_stk", self.OHNE_ME_SPALTE)
        self.assertEqual(angebot.positions[1].uom, "ST")

    def test_kilogramm_bleibt_kilogramm(self):
        angebot = self._import("einheit_kg", self.OHNE_ME_SPALTE)
        self.assertEqual(angebot.positions[2].uom, "KG")

    # -- Grenzfall: Widerspruch zur ME-Spalte ---------------------------
    def test_widerspruch_zur_me_spalte_wird_gemeldet(self):
        """"500 ST" in der Menge, "KG" in der ME-Spalte -- nicht raten."""
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;500 ST;KG;2,95\n"
                "4711001;Dichtring Typ 1;250 ST;KG;1,15\n")
        angebot = self._import("einheit_konflikt", text)
        self.assertIn("uom_conflict",
                      [i.code for p in angebot.positions for i in p.issues])

    def test_widerspruch_laesst_die_me_spalte_stehen(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;500 ST;KG;2,95\n"
                "4711001;Dichtring Typ 1;250 ST;KG;1,15\n")
        angebot = self._import("einheit_konflikt2", text)
        self.assertEqual(angebot.positions[0].uom, "KG")
        self.assertIs(angebot.positions[0].field_origins.get("uom"),
                      FieldOrigin.UNCERTAIN)

    def test_widerspruch_veraendert_die_menge_nicht(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;500 ST;KG;2,95\n"
                "4711001;Dichtring Typ 1;250 ST;KG;1,15\n")
        angebot = self._import("einheit_konflikt3", text)
        self.assertEqual(self._mengen(angebot), [Decimal("500"), Decimal("250")])

    # -- darf nicht anschlagen ------------------------------------------
    def test_gleiche_einheit_ist_kein_widerspruch(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;500 ST;ST;2,95\n"
                "4711001;Dichtring Typ 1;250 ST;ST;1,15\n")
        angebot = self._import("einheit_einig", text)
        self.assertNotIn("uom_conflict",
                         [i.code for p in angebot.positions for i in p.issues])

    def test_nackte_menge_loest_nichts_aus(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;500;ST;2,95\n"
                "4711001;Dichtring Typ 1;250;ST;1,15\n")
        angebot = self._import("einheit_nackt", text)
        self.assertNotIn("uom_conflict",
                         [i.code for p in angebot.positions for i in p.issues])
        self.assertEqual(self._mengen(angebot), [Decimal("500"), Decimal("250")])


# ==========================================================================
# 5. "auf Anfrage"
# ==========================================================================

class AufAnfrageTest(_ImportHilfe):
    """Eine Position ohne Preis darf die uebrigen nicht beschaedigen."""

    MIT_ANFRAGE = (
        "Artikel;Bezeichnung;Menge;ME;Einzelpreis;Gesamt\n"
        "4711000;Dichtring Typ 0;100;ST;auf Anfrage;737,50\n"
        "4711001;Dichtring Typ 1;500;ST;2,95;1.475,00\n"
        "4711002;Dichtring Typ 2;100;ST;1,15;115,00\n"
        "4711003;Dichtring Typ 3;100;ST;0,85;85,00\n"
    )

    # -- Gutfall -------------------------------------------------------
    def test_alle_positionen_bleiben_erhalten(self):
        angebot = self._import("anfrage", self.MIT_ANFRAGE)
        self.assertEqual(len(angebot.positions), 4)

    def test_die_uebrigen_preise_stimmen(self):
        """Der eigentliche Hebel: eine Textzelle darf nicht alles mitreissen."""
        angebot = self._import("anfrage_rest", self.MIT_ANFRAGE)
        preise = {m: p for m, p in zip(self._materialien(angebot),
                                       self._preise(angebot))}
        self.assertEqual(preise["4711001"], Decimal("2.95"))
        self.assertEqual(preise["4711002"], Decimal("1.15"))
        self.assertEqual(preise["4711003"], Decimal("0.85"))

    def test_kein_preis_wird_erfunden(self):
        """Aus Zeilensumme geteilt durch Menge KEINEN Preis ableiten.

        Die Zeilensumme neben einer "auf Anfrage"-Zeile ist ein Restwert
        oder ein Platzhalter.  Daraus einen Preis auszurechnen widerspricht
        dem Beleg -- und sah nach einem sauber gelesenen Preis aus.
        """
        angebot = self._import("anfrage_kein_preis", self.MIT_ANFRAGE)
        preise = {m: p for m, p in zip(self._materialien(angebot),
                                       self._preise(angebot))}
        self.assertIsNone(preise["4711000"])

    def test_fehlender_preis_wird_gemeldet(self):
        angebot = self._import("anfrage_befund", self.MIT_ANFRAGE)
        codes = [i.code for p in angebot.positions for i in p.issues]
        self.assertIn("price_on_request", codes)

    def test_fehlender_preis_ist_nicht_extrahiert(self):
        angebot = self._import("anfrage_origin", self.MIT_ANFRAGE)
        for position in angebot.positions:
            if (position.material_number or "").lstrip("0") == "4711000":
                self.assertIs(position.field_origins.get("price"),
                              FieldOrigin.MISSING)

    def test_mengen_der_uebrigen_bleiben_richtig(self):
        angebot = self._import("anfrage_mengen", self.MIT_ANFRAGE)
        self.assertEqual(self._mengen(angebot),
                         [Decimal("100"), Decimal("500"),
                          Decimal("100"), Decimal("100")])

    # -- Grenzfall -----------------------------------------------------
    def test_anfrage_in_der_ersten_zeile(self):
        """Die Spaltenprofilierung darf an der Textzelle nicht scheitern."""
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;100;ST;auf Anfrage\n"
                "4711001;Dichtring Typ 1;500;ST;2,95\n"
                "4711002;Dichtring Typ 2;100;ST;1,15\n")
        angebot = self._import("anfrage_erste", text)
        self.assertEqual(len(angebot.positions), 3)
        self.assertEqual(self._preise(angebot)[1], Decimal("2.95"))

    def test_alle_zeilen_auf_anfrage(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis\n"
                "4711000;Dichtring Typ 0;100;ST;auf Anfrage\n"
                "4711001;Dichtring Typ 1;500;ST;auf Anfrage\n")
        angebot = self._import("anfrage_alle", text)
        self.assertEqual(len(angebot.positions), 2)
        self.assertEqual(self._preise(angebot), [None, None])

    def test_anfrage_ohne_stillen_fehler(self):
        angebot = self._import("anfrage_still", self.MIT_ANFRAGE)
        self._kein_stiller_fehler(angebot, {
            "4711001": Decimal("2.95"),
            "4711002": Decimal("1.15"),
            "4711003": Decimal("0.85")})

    # -- darf nicht anschlagen ------------------------------------------
    def test_ohne_anfrage_wird_weiter_abgeleitet(self):
        """Fehlt der Preis OHNE "auf Anfrage", darf weiter gerechnet werden."""
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis;Gesamt\n"
                "4711000;Dichtring Typ 0;100;ST;;295,00\n"
                "4711001;Dichtring Typ 1;500;ST;2,95;1.475,00\n")
        angebot = self._import("ohne_anfrage", text)
        codes = [i.code for p in angebot.positions for i in p.issues]
        self.assertIn("price_derived", codes)

    def test_vollstaendige_tabelle_bleibt_unberuehrt(self):
        text = ("Artikel;Bezeichnung;Menge;ME;Einzelpreis;Gesamt\n"
                "4711001;Dichtring Typ 1;500;ST;2,95;1.475,00\n"
                "4711002;Dichtring Typ 2;100;ST;1,15;115,00\n")
        angebot = self._import("vollstaendig", text)
        self.assertEqual(self._preise(angebot),
                         [Decimal("2.95"), Decimal("1.15")])
        codes = [i.code for p in angebot.positions for i in p.issues]
        self.assertNotIn("price_on_request", codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
