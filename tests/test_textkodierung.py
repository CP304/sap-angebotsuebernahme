"""UTF-16 einlesen -- SAP-Aufzeichnungen und Excel-"Unicode Text".

Der Fehler, um den es geht
--------------------------
Windows und SAP schreiben Textdateien oft als UTF-16LE mit
Byte-Order-Mark: die .vbs der SAP-GUI-Skriptaufzeichnung, der
SAP-Listexport und Excels "Unicode Text (*.txt)".

Die frueher benutzte Kandidatenliste ("utf-8-sig", "utf-8", "cp1252",
"latin-1") erkannte davon nichts.  ``utf-8`` scheiterte an ``FF FE``,
aber ``cp1252`` nahm jedes Byte an und meldete keinen Fehler -- heraus
kam ``'ÿþA\\x00n\\x00g\\x00e\\x00b\\x00o\\x00t\\x00'``.

Das Tueckische war nicht die Anzeige (leere Rechtecke), sondern die
Stille danach: die Tabellenerkennung fand in solchem Text keine Spalte,
das Angebot ergab null Positionen, die Aufzeichnung null Felder -- und
nirgends stand, dass es an der Kodierung lag.

Deshalb stehen diese Faelle hier fest.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_kodierung_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings  # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402
from app.services.vbs_parser import (  # noqa: E402
    detect_transaction,
    parse_vbs_recording,
)
from app.utils.textkodierung import decode_bytes, entferne_nullzeichen  # noqa: E402

#: Eine Aufzeichnung, wie das SAP GUI sie fuer ME11 hinterlaesst.
VBS_AUFZEICHNUNG = (
    'If Not IsObject(application) Then\n'
    '   Set SapGuiAuto = GetObject("SAPGUI")\n'
    'End If\n'
    'session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"\n'
    'session.findById("wnd[0]").sendVKey 0\n'
    'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"\n'
    'session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"\n'
    'session.findById("wnd[0]/usr/ctxtEINE-EKORG").text = "1000"\n'
    'session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"\n'
)

#: Ein Angebot mit Tabulatoren, wie Excel es als "Unicode Text" ablegt.
ANGEBOT_TABELLE = (
    "Angebot Nr. 4711\tLieferant: Nordtec GmbH\n"
    "Position\tMaterial\tBezeichnung\tMenge\tEinheit\tPreis\tWaehrung\n"
    "10\t4711001\tDichtring 40x52\t100\tST\t2,95\tEUR\n"
    "20\t4711002\tFlanschdichtung DN50\t50\tST\t7,40\tEUR\n"
)


class KodierungErkennenTest(unittest.TestCase):
    """Aus Bytes wird Text -- und zwar der richtige."""

    def test_utf16le_mit_marke(self):
        text, kodierung, warnung = decode_bytes(
            b"\xff\xfe" + "Dichtring 40x52".encode("utf-16-le"))
        self.assertEqual(text, "Dichtring 40x52")
        self.assertEqual(kodierung, "utf-16")
        self.assertEqual(warnung, "")

    def test_utf16be_mit_marke(self):
        text, _kodierung, _warnung = decode_bytes(
            b"\xfe\xff" + "Dichtring für Pumpe".encode("utf-16-be"))
        self.assertEqual(text, "Dichtring für Pumpe")

    def test_utf32_wird_nicht_als_utf16_gelesen(self):
        """Die UTF-32LE-Marke beginnt mit der UTF-16LE-Marke.

        ``FF FE 00 00`` ist beides zugleich.  Wird in der falschen
        Reihenfolge geprueft, liest UTF-16 den Rest als Nullzeichen.
        """
        text, _kodierung, _warnung = decode_bytes(
            b"\xff\xfe\x00\x00" + "Preis".encode("utf-32-le"))
        self.assertEqual(text, "Preis")

    def test_utf16_ohne_marke(self):
        """SAP-Exporte lassen die Marke gelegentlich weg."""
        text, kodierung, _warnung = decode_bytes(
            ANGEBOT_TABELLE.encode("utf-16-le"))
        self.assertEqual(kodierung, "utf-16-le")
        self.assertIn("Dichtring 40x52", text)

    def test_umlaute_bleiben_erhalten(self):
        for kodierung in ("utf-8", "utf-16", "cp1252", "latin-1"):
            with self.subTest(kodierung=kodierung):
                text, _erkannt, _warnung = decode_bytes(
                    "Grösse: 40 Stück à 2,95 €".replace("€", "EUR")
                    .encode(kodierung))
                self.assertIn("Grösse", text)
                self.assertIn("Stück", text)

    def test_bisherige_kodierungen_unveraendert(self):
        """Was vorher richtig gelesen wurde, wird es weiterhin."""
        faelle = (
            (b"\xef\xbb\xbf" + "Angebot".encode("utf-8"), "utf-8-sig"),
            ("Angebot".encode("utf-8"), "utf-8-sig"),
            ("Angebot für".encode("cp1252"), "cp1252"),
        )
        for rohdaten, erwartet in faelle:
            with self.subTest(erwartet=erwartet):
                text, kodierung, warnung = decode_bytes(rohdaten)
                self.assertEqual(kodierung, erwartet)
                self.assertTrue(text.startswith("Angebot"))
                self.assertEqual(warnung, "")

    def test_leere_datei(self):
        text, _kodierung, warnung = decode_bytes(b"")
        self.assertEqual(text, "")
        self.assertEqual(warnung, "")

    def test_kein_ergebnis_mit_nullzeichen(self):
        """Ein Ergebnis voller Nullzeichen ist nie das richtige.

        Die Gegenprobe ist die eigentliche Absicherung: ``cp1252`` und
        ``latin-1`` melden nie einen Fehler, liefern aber Zeichensalat.
        """
        text, _kodierung, _warnung = decode_bytes(
            b"\xff\xfe" + ANGEBOT_TABELLE.encode("utf-16-le"))
        self.assertNotIn("\x00", text)

    def test_unklare_zweibytedatei_warnt_statt_zu_schweigen(self):
        """Nullzeichen ohne erkennbares Muster -- Rat statt Zeichensalat.

        Liegen die Nullbytes weder auf den geraden noch auf den ungeraden
        Stellen, laesst sich die Bytereihenfolge nicht bestimmen, und es
        wird auch nichts behauptet.  Was der Anwender dann bekommt, ist
        Text ohne Nullzeichen UND ein Satz dazu, was zu tun ist -- nicht
        stillschweigend eine Datei, in der nichts gefunden wird.
        """
        text, _kodierung, warnung = decode_bytes(b"AB\x00\x00" * 200)
        self.assertNotIn("\x00", text)
        self.assertTrue(warnung)
        self.assertIn("UTF-8", warnung)


class FalscheByteReihenfolgeTest(unittest.TestCase):
    """Wenn die Byte-Order-Mark luegt.

    Eine Marke kann falsch sein -- etwa weil ein Werkzeug den Kopf
    kopiert und den Rumpf unveraendert durchgereicht hat.  Kein Codec
    meldet dabei einen Fehler: aus einer Preisliste wird ostasiatischer
    Zeichensalat, und die Tabellenerkennung findet nichts.  Das ist an
    der Schrift erkennbar und wird nicht stillschweigend hingenommen.
    """

    def test_marke_sagt_be_inhalt_ist_le(self):
        text, kodierung, warnung = decode_bytes(
            b"\xfe\xff" + ANGEBOT_TABELLE.encode("utf-16-le"))
        self.assertIn("Dichtring 40x52", text)
        self.assertEqual(kodierung, "utf-16-le")
        self.assertTrue(warnung)

    def test_marke_sagt_le_inhalt_ist_be(self):
        text, kodierung, warnung = decode_bytes(
            b"\xff\xfe" + ANGEBOT_TABELLE.encode("utf-16-be"))
        self.assertIn("Dichtring 40x52", text)
        self.assertEqual(kodierung, "utf-16-be")
        self.assertTrue(warnung)

    def test_ehrliche_marke_wird_nicht_umgedreht(self):
        """Was stimmt, bleibt -- und ohne Warnung."""
        for marke, kodierung in ((b"\xff\xfe", "utf-16-le"),
                                 (b"\xfe\xff", "utf-16-be")):
            with self.subTest(kodierung=kodierung):
                text, erkannt, warnung = decode_bytes(
                    marke + ANGEBOT_TABELLE.encode(kodierung))
                self.assertIn("Dichtring 40x52", text)
                self.assertEqual(erkannt, "utf-16")
                self.assertEqual(warnung, "")

    def test_angebot_mit_falscher_marke_ergibt_positionen(self):
        """Der ganze Weg, nicht nur die Kodierung."""
        ordner = Path(tempfile.mkdtemp(prefix="sap_bom_luegt_"))
        pfad = ordner / "Angebot.txt"
        pfad.write_bytes(b"\xfe\xff" + ANGEBOT_TABELLE.encode("utf-16-le"))
        angebot = OfferImportService(Settings()).import_file(pfad)
        self.assertEqual(len(angebot.positions), 2)
        # Repariert ist nicht verschwiegen: der Anwender wird darauf
        # hingewiesen, dass die Datei einen falschen Kopf traegt.
        self.assertTrue(any("Bytereihenfolge" in befund.message
                            for befund in angebot.issues))


class NullzeichenEntfernenTest(unittest.TestCase):
    """Rettungsanker fuer bereits falsch dekodierten Text."""

    def test_zeichensalat_wird_wieder_lesbar(self):
        salat = "ÿþ" + "".join(f"{z}\x00" for z in "session.findById")
        self.assertEqual(entferne_nullzeichen(salat), "session.findById")

    def test_sauberer_text_bleibt_unangetastet(self):
        sauber = 'session.findById("wnd[0]").text = "100234"'
        self.assertEqual(entferne_nullzeichen(sauber), sauber)


class VbsAufzeichnungTest(unittest.TestCase):
    """Der Weg, auf dem die .vbs tatsaechlich hereinkommt."""

    def test_utf16_aufzeichnung_wird_vollstaendig_gelesen(self):
        rohdaten = b"\xff\xfe" + VBS_AUFZEICHNUNG.encode("utf-16-le")
        text, _kodierung, _warnung = decode_bytes(rohdaten)
        self.assertEqual(detect_transaction(text), "ME11")
        felder = {f.short_id(): f.value for f in parse_vbs_recording(text)}
        self.assertEqual(felder["EINA-LIFNR"], "100234")
        self.assertEqual(felder["EINA-MATNR"], "4711001")
        self.assertEqual(felder["EINE-EKORG"], "1000")
        self.assertEqual(felder["EINE-NETPR"], "2,95")

    def test_eingefuegter_zeichensalat_wird_trotzdem_ausgewertet(self):
        """Kopiert jemand aus einem Editor, der die .vbs falsch oeffnet.

        Dann liegen keine Bytes mehr vor, an denen sich die Kodierung
        bestimmen liesse -- die Nullzeichen stehen aber noch zwischen den
        Buchstaben, und die Feld-IDs bestehen nur aus ASCII.
        """
        salat = "ÿþ" + "".join(f"{z}\x00" for z in VBS_AUFZEICHNUNG)
        self.assertEqual(detect_transaction(salat), "ME11")
        felder = parse_vbs_recording(salat)
        self.assertEqual(len(felder), 4)


class SelektorenAusAufzeichnungTest(unittest.TestCase):
    """Der zweite Weg fuer .vbs: die Selektorenpflege.

    Dieselbe Datei wird an zwei Stellen eingelesen -- im Uebernahme-
    Assistenten und in der Selektorenpflege.  Beide muessen mit UTF-16
    umgehen, sonst funktioniert die Aufzeichnung je nach Einstiegspunkt
    oder eben nicht.
    """

    def setUp(self):
        from app.sap.selectors import SelectorRegistry
        self.registry = SelectorRegistry()

    def test_utf16_datei(self):
        rohdaten = b"\xff\xfe" + VBS_AUFZEICHNUNG.encode("utf-16-le")
        text, _kodierung, _warnung = decode_bytes(rohdaten)
        ids = self.registry.ids_from_vbs(text)
        self.assertIn("wnd[0]/usr/ctxtEINA-LIFNR", ids)
        self.assertIn("wnd[0]/usr/txtEINE-NETPR", ids)

    def test_zeichensalat_wird_aufgefangen(self):
        salat = "ÿþ" + "".join(f"{z}\x00" for z in VBS_AUFZEICHNUNG)
        ids = self.registry.ids_from_vbs(salat)
        self.assertIn("wnd[0]/usr/ctxtEINA-LIFNR", ids)


class AngebotAlsUnicodeTextTest(unittest.TestCase):
    """Ein Angebot als UTF-16-Textdatei ergibt Positionen, keine Befunde."""

    def setUp(self):
        self.ordner = Path(tempfile.mkdtemp(prefix="sap_angebot_utf16_"))
        self.dienst = OfferImportService(Settings())

    def _importiere(self, name: str, rohdaten: bytes):
        pfad = self.ordner / name
        pfad.write_bytes(rohdaten)
        return self.dienst.import_file(pfad)

    def test_utf16_mit_marke(self):
        angebot = self._importiere(
            "Angebot_Unicode.txt",
            b"\xff\xfe" + ANGEBOT_TABELLE.encode("utf-16-le"))
        self.assertEqual(len(angebot.positions), 2)
        erste = angebot.positions[0]
        self.assertEqual(erste.material_number, "4711001")
        self.assertEqual(erste.description, "Dichtring 40x52")
        self.assertEqual(str(erste.price), "2.95")

    def test_utf16_ohne_marke(self):
        angebot = self._importiere(
            "Listexport.txt", ANGEBOT_TABELLE.encode("utf-16-le"))
        self.assertEqual(len(angebot.positions), 2)

    def test_utf16_csv(self):
        inhalt = ANGEBOT_TABELLE.replace("\t", ";")
        angebot = self._importiere(
            "Preisliste.csv", b"\xff\xfe" + inhalt.encode("utf-16-le"))
        self.assertEqual(len(angebot.positions), 2)

    def test_eingefuegter_zeichensalat_ergibt_trotzdem_positionen(self):
        """Kopiert aus einem Editor, der die UTF-16-Datei falsch oeffnet.

        Als Text liegen keine Bytes mehr vor.  Die Nullzeichen stehen aber
        noch zwischen den Buchstaben -- und weil ``cp1252`` die Umlaut-
        Bytes richtig getroffen hat, ueberlebt sogar "Kegelstück".
        """
        inhalt = ANGEBOT_TABELLE.replace(
            "Flanschdichtung DN50", "Kegelstück grün")
        salat = (b"\xff\xfe" + inhalt.encode("utf-16-le")).decode("cp1252")
        angebot = self.dienst.import_text(salat)
        self.assertEqual(len(angebot.positions), 2)
        self.assertIn("Kegelstück grün",
                      [p.description for p in angebot.positions])

    def test_umlaute_im_angebot_kommen_richtig_an(self):
        inhalt = ANGEBOT_TABELLE.replace(
            "Flanschdichtung DN50", "Kegelstück grün")
        angebot = self._importiere(
            "Angebot_Umlaute.txt", b"\xff\xfe" + inhalt.encode("utf-16-le"))
        beschreibungen = [p.description for p in angebot.positions]
        self.assertIn("Kegelstück grün", beschreibungen)


class ContainerTest(unittest.TestCase):
    """UTF-16 in der Mail und im Archiv.

    Angebote kommen selten als blanke Datei -- sie haengen an einer Mail
    oder liegen in einem ZIP.  Die Kodierungserkennung muss deshalb auch
    dort greifen, wo die Datei erst ausgepackt wird.
    """

    def setUp(self):
        self.ordner = Path(tempfile.mkdtemp(prefix="sap_container_"))
        self.dienst = OfferImportService(Settings())

    def test_mail_mit_utf16_anhang(self):
        from email.message import EmailMessage
        for endung, trenner in ((".txt", "\t"), (".csv", ";")):
            with self.subTest(endung=endung):
                inhalt = ANGEBOT_TABELLE.replace("\t", trenner)
                nachricht = EmailMessage()
                nachricht["From"] = "vertrieb@nordtec.example"
                nachricht["Subject"] = "Angebot 4711"
                nachricht.set_content("Guten Tag, anbei unser Angebot.")
                nachricht.add_attachment(
                    b"\xff\xfe" + inhalt.encode("utf-16-le"),
                    maintype="text", subtype="plain",
                    filename=f"angebot{endung}")
                pfad = self.ordner / f"mail{endung}.eml"
                pfad.write_bytes(nachricht.as_bytes())
                angebot = self.dienst.import_file(pfad)
                self.assertEqual(len(angebot.positions), 2)

    def test_zip_mit_utf16_datei(self):
        import zipfile
        pfad = self.ordner / "sammlung.zip"
        with zipfile.ZipFile(pfad, "w") as archiv:
            archiv.writestr("angebot.txt",
                            b"\xff\xfe" + ANGEBOT_TABELLE.encode("utf-16-le"))
        angebot = self.dienst.import_file(pfad)
        self.assertEqual(len(angebot.positions), 2)


class UnauffaelligeRandfaelleTest(unittest.TestCase):
    """Was nicht gehen kann, darf wenigstens nicht abstuerzen."""

    def setUp(self):
        self.ordner = Path(tempfile.mkdtemp(prefix="sap_rand_"))
        self.dienst = OfferImportService(Settings())

    def _positionen(self, name: str, rohdaten: bytes) -> int:
        pfad = self.ordner / name
        pfad.write_bytes(rohdaten)
        return len(self.dienst.import_file(pfad).positions)

    def test_leere_und_unvollstaendige_dateien(self):
        faelle = (
            ("leer.txt", b""),
            ("nur_leerraum.txt", b"   \n\n  \n"),
            ("nur_marke.txt", b"\xff\xfe"),
            ("halbe_marke.txt", b"\xff"),
        )
        for name, rohdaten in faelle:
            with self.subTest(name=name):
                self.assertEqual(self._positionen(name, rohdaten), 0)

    def test_ungerade_byte_zahl_bei_utf16(self):
        """Ein abgeschnittenes letztes Zeichen kostet nicht die Datei."""
        rohdaten = (b"\xff\xfe" + ANGEBOT_TABELLE.encode("utf-16-le")
                    + b"\x41")
        self.assertEqual(self._positionen("abgeschnitten.txt", rohdaten), 2)

    def test_vereinzeltes_nullbyte_in_utf8(self):
        rohdaten = ANGEBOT_TABELLE.encode("utf-8").replace(
            b"Dichtring", b"Dicht\x00ring")
        self.assertEqual(self._positionen("nullbyte.txt", rohdaten), 2)

    def test_gemischte_zeilenenden(self):
        inhalt = ("Position\tMaterial\tBezeichnung\tMenge\tEinheit\tPreis\tWaehrung\r\n"
                  "10\t4711001\tDichtring 40x52\t100\tST\t2,95\tEUR\n"
                  "20\t4711002\tFlanschdichtung DN50\t50\tST\t7,40\tEUR\r\n")
        self.assertEqual(self._positionen("gemischt.txt", inhalt.encode()), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
