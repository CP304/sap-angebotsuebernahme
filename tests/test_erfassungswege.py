"""Die drei Wege, auf denen der Anwender selbst erfasst -- Ende zu Ende.

Warum diese Datei
-----------------
Bisher waren die Bausteine dieser Wege einzeln geprueft: der Parser der
Aufzeichnung, die Zerlegung eingefuegten Textes, die Kodierungserkennung.
Was fehlte, war der Weg selbst -- die Maske, die der Anwender vor sich
hat, mit den Werten, die am Ende in der Tabelle stehen.

Genau daran haengt der praktische Nutzen des Werkzeugs:

1. **Aufzeichnung (.vbs)** -- ohne die Feld-IDs aus der eigenen Anlage
   schreibt die Anwendung nichts nach SAP.
2. **Tabelle einfuegen oder laden** -- der Ausweg, wenn die automatische
   Erkennung einmal nicht greift.
3. **Schnellerfassung** -- fuer die formlose Preismitteilung, aus der
   eine einzige Position wird.
4. **Direkt in der Tabelle tippen** -- der kuerzeste Weg von allen:
   Zeile anlegen, hineinschreiben, fertig.

Geprueft wird jeweils bis zum sichtbaren Ergebnis: was in der Tabelle
steht, was vorgeschlagen wird, was als Position herauskommt.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_erfassung_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.config.settings import Settings  # noqa: E402
from app.utils.textkodierung import decode_bytes  # noqa: E402

#: Eine Aufzeichnung, wie der SAP-GUI-Recorder sie fuer ME11 hinterlaesst.
AUFZEICHNUNG_ME11 = '''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
session.findById("wnd[0]").maximize
session.findById("wnd[0]/tbar[0]/okcd").text = "/nme11"
session.findById("wnd[0]").sendVKey 0
session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
session.findById("wnd[0]/usr/ctxtEINE-EKORG").text = "1000"
session.findById("wnd[0]/usr/ctxtEINE-WERKS").text = "1010"
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"
session.findById("wnd[0]/usr/ctxtEINE-WAERS").text = "EUR"
session.findById("wnd[0]/tbar[0]/btn[11]").press
'''

#: Ein aus Excel kopierter Bereich.
TABELLE = ("Pos\tMaterial\tBezeichnung\tMenge\tME\tPreis\tWaehrung\n"
           "10\t4711001\tDichtring 40x52\t100\tST\t2,95\tEUR\n"
           "20\t4711002\tFlanschdichtung DN50\t50\tST\t7,40\tEUR\n")


def wie_aus_falschem_editor(text: str) -> str:
    """Text so verfaelschen, wie ein Editor es tut, der eine UTF-16-Datei
    als Windows-1252 oeffnet."""
    return (b"\xff\xfe" + text.encode("utf-16-le")).decode("cp1252")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class AufzeichnungEinlesenTest(unittest.TestCase):
    """Weg 1: die .vbs aus dem SAP GUI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _maske_mit_datei(self, rohdaten: bytes):
        from app.gui.vbs_importer import VbsImporterWidget

        ordner = Path(tempfile.mkdtemp(prefix="sap_vbs_datei_"))
        pfad = ordner / "me11_anlegen.vbs"
        pfad.write_bytes(rohdaten)
        maske = VbsImporterWidget(Settings())
        # Genau das, was "Datei oeffnen ..." tut -- ohne den Dateidialog.
        inhalt, _kodierung, warnung = decode_bytes(pfad.read_bytes())
        maske.input.setPlainText(inhalt)
        maske._kodierungshinweis = warnung
        maske.parse_input()
        return maske

    def _zuordnungen(self, maske) -> dict[str, str]:
        """Was in der Tabelle steht: Feldname -> vorgeschlagene Bedeutung."""
        ergebnis = {}
        for zeile in range(maske.table.rowCount()):
            auswahl = maske.table.cellWidget(zeile, 3)
            ergebnis[maske.table.item(zeile, 0).text()] = (
                auswahl.currentText() if isinstance(auswahl, QComboBox) else "")
        return ergebnis

    def test_utf16_aufzeichnung_fuellt_die_maske(self):
        """So schreibt der Recorder: UTF-16 mit Byte-Order-Mark."""
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))

        self.assertIn("ME11", maske.transaction_label.text())
        self.assertIn("infosatz", maske.transaction_label.text().lower())
        self.assertEqual(maske.table.rowCount(), 6)

        zuordnung = self._zuordnungen(maske)
        # Vorgeschlagen wird das Feld eines Bildschirms dieser Transaktion,
        # nicht eine allgemeine Bedeutung.
        self.assertIn("Lieferant", zuordnung["EINA-LIFNR"])
        self.assertIn("Einstiegsbild", zuordnung["EINA-LIFNR"])
        self.assertIn("Material", zuordnung["EINA-MATNR"])
        self.assertIn("Einkaufsorganisation", zuordnung["EINE-EKORG"])
        self.assertIn("Werk", zuordnung["EINE-WERKS"])
        self.assertIn("Nettopreis", zuordnung["EINE-NETPR"])
        self.assertIn("hrung", zuordnung["EINE-WAERS"])

        # Kommandofeld und Werkzeugleiste sind Navigation, keine Daten.
        self.assertNotIn("okcd", zuordnung)

    def test_werte_stehen_im_klartext_in_der_tabelle(self):
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))
        werte = {maske.table.item(z, 0).text(): maske.table.item(z, 1).text()
                 for z in range(maske.table.rowCount())}
        self.assertEqual(werte["EINA-LIFNR"], "100234")
        self.assertEqual(werte["EINE-NETPR"], "2,95")

    def test_jede_kodierung_ergibt_dieselbe_maske(self):
        varianten = (
            ("utf-16 mit Marke",
             b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le")),
            ("utf-16 ohne Marke", AUFZEICHNUNG_ME11.encode("utf-16-le")),
            ("utf-8", AUFZEICHNUNG_ME11.encode("utf-8")),
            ("cp1252", AUFZEICHNUNG_ME11.encode("cp1252")),
        )
        for name, rohdaten in varianten:
            with self.subTest(kodierung=name):
                maske = self._maske_mit_datei(rohdaten)
                self.assertEqual(maske.table.rowCount(), 6)
                self.assertIn("ME11", maske.transaction_label.text())

    def test_gespeicherte_zuordnung_landet_dort_wo_geschrieben_wird(self):
        """In der Selektorenablage, aus der die Schreibschicht ihre IDs holt.

        Frueher landete sie in einer flachen Liste neben den
        Einstellungen, die niemand las: der Anwender ordnete zu, bekam
        eine Erfolgsmeldung, und beim Schreiben fehlten die IDs trotzdem.
        """
        maske = self._maske_mit_datei(
            b"\xff\xfe" + AUFZEICHNUNG_ME11.encode("utf-16-le"))
        maske.save_mapping()
        self.assertEqual(
            maske.registry.get("info_record_initial", "vendor").id,
            "wnd[0]/usr/ctxtEINA-LIFNR")
        self.assertEqual(
            maske.registry.get("info_record_purchasing", "net_price").id,
            "wnd[0]/usr/txtEINE-NETPR")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class ZuordnungIstTransaktionsspezifischTest(unittest.TestCase):
    """Weg 1c: dieselbe Bedeutung in mehreren Transaktionen.

    Der Fehler, um den es geht
    --------------------------
    Die Maske bot eine feste Liste allgemeiner Bedeutungen an
    ("Lieferantennummer", "Preis") und legte das Ergebnis in einer flachen
    Liste neben den Einstellungen ab.  Beides passte nicht zur Sache:

    * Eine Lieferantennummer gibt es im Infosatz, im Kontrakt und in der
      Bestellung -- drei Bildschirme, drei verschiedene Feld-IDs.  Flach
      gespeichert ueberschrieb die zweite Aufzeichnung die erste, oder die
      Dublettenpruefung warnte vor etwas voellig Richtigem.
    * Gelesen wurde diese Liste ohnehin nirgends.  Die Schreibschicht holt
      ihre IDs aus der Selektorenablage, je Bildschirm und Element.  Der
      Anwender ordnete also zu, bestaetigte, bekam eine Erfolgsmeldung --
      und beim Schreiben nach SAP fehlten die IDs trotzdem.

    Angeboten werden jetzt die Felder der Bildschirme, die zur erkannten
    Transaktion gehoeren, und gespeichert wird dorthin, wo geschrieben
    wird.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _maske(self, vbs: str, registry=None):
        from app.gui.vbs_importer import VbsImporterWidget
        from app.sap.selectors import SelectorRegistry

        maske = VbsImporterWidget(Settings(),
                                  registry or SelectorRegistry())
        maske.input.setPlainText(vbs)
        maske.parse_input()
        return maske

    ME11 = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"\n'
            'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"\n'
            'session.findById("wnd[0]/usr/txtEINE-NETPR").text = "2,95"\n')
    ME31K = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME31K"\n'
             'session.findById("wnd[0]/usr/ctxtRM06E-LIFNR").text = "100234"\n')
    ME01 = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME01"\n'
            'session.findById("wnd[0]/usr/ctxtEORD-MATNR").text = "4711001"\n')

    def test_die_auswahl_beschraenkt_sich_auf_die_transaktion(self):
        """Alles anzubieten macht die Liste lang und die Wahl unsicher."""
        from PySide6.QtWidgets import QComboBox

        # Die Masken in Variablen halten: sonst raeumt Python sie weg,
        # bevor die Auswahlfelder ausgelesen sind, und Qt meldet ein
        # bereits geloeschtes C++-Objekt.
        maske_me11 = self._maske(self.ME11)
        maske_me01 = self._maske(self.ME01)
        auswahl_me11 = maske_me11.table.cellWidget(0, 3)
        auswahl_me01 = maske_me01.table.cellWidget(0, 3)
        self.assertIsInstance(auswahl_me11, QComboBox)
        beschriftungen_me11 = [auswahl_me11.itemText(i)
                               for i in range(auswahl_me11.count())]
        beschriftungen_me01 = [auswahl_me01.itemText(i)
                               for i in range(auswahl_me01.count())]
        self.assertTrue(any("Infosatz" in t for t in beschriftungen_me11))
        self.assertFalse(any("Orderbuch" in t for t in beschriftungen_me11))
        self.assertTrue(any("Orderbuch" in t for t in beschriftungen_me01))
        self.assertFalse(any("Infosatz" in t for t in beschriftungen_me01))

    def test_lieferant_im_infosatz_und_im_kontrakt_koennen_nebeneinander(self):
        """Der Fall, an dem die flache Liste scheiterte."""
        from app.sap.selectors import SelectorRegistry

        registry = SelectorRegistry()
        self._maske(self.ME11, registry).save_mapping()
        self._maske(self.ME31K, registry).save_mapping()

        self.assertEqual(registry.get("info_record_initial", "vendor").id,
                         "wnd[0]/usr/ctxtEINA-LIFNR")
        self.assertEqual(registry.get("contract_initial", "vendor").id,
                         "wnd[0]/usr/ctxtRM06E-LIFNR")

    def test_drei_aufzeichnungen_nacheinander(self):
        """Die Vorgaenge werden nach und nach aufgezeichnet."""
        from app.sap.selectors import SelectorRegistry

        registry = SelectorRegistry()
        for vbs in (self.ME11, self.ME31K, self.ME01):
            self._maske(vbs, registry).save_mapping()

        self.assertEqual(registry.get("info_record_initial", "vendor").id,
                         "wnd[0]/usr/ctxtEINA-LIFNR")
        self.assertEqual(registry.get("info_record_purchasing", "net_price").id,
                         "wnd[0]/usr/txtEINE-NETPR")
        self.assertEqual(registry.get("contract_initial", "vendor").id,
                         "wnd[0]/usr/ctxtRM06E-LIFNR")
        self.assertEqual(registry.get("source_list_initial", "material").id,
                         "wnd[0]/usr/ctxtEORD-MATNR")

    def test_abweichender_bildaufbau_bekommt_trotzdem_einen_vorschlag(self):
        """Nicht jede Anlage baut ihre Bilder gleich auf.

        Stimmt nur der Feldname (``LIFNR``) und nicht die Bildstruktur
        davor (``EKKO`` statt ``RM06E``), ist das ein guter Hinweis --
        solange er eindeutig ist.
        """
        vbs = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME31K"\n'
               'session.findById("wnd[0]/usr/ctxtEKKO-LIFNR").text = "100234"\n')
        maske = self._maske(vbs)
        auswahl = maske.table.cellWidget(0, 3)
        self.assertIn("Lieferant", auswahl.currentText())

    def test_unbekannte_transaktion_stellt_alles_zur_wahl(self):
        """Lieber alles anbieten als den Anwender aussperren."""
        vbs = 'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"'
        maske = self._maske(vbs)
        self.assertEqual(maske.transaction, "")
        auswahl = maske.table.cellWidget(0, 3)
        beschriftungen = [auswahl.itemText(i) for i in range(auswahl.count())]
        self.assertTrue(any("Infosatz" in t for t in beschriftungen))
        self.assertTrue(any("Kontrakt" in t for t in beschriftungen))

    def test_uebernommene_ids_gelten_als_ungeprueft(self):
        """Solange sie das sind, schreibt die Anwendung damit nicht."""
        from app.sap.selectors import SelectorRegistry

        registry = SelectorRegistry()
        self._maske(self.ME11, registry).save_mapping()
        self.assertFalse(registry.get("info_record_initial", "vendor").verified)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class EingefuegteAufzeichnungTest(unittest.TestCase):
    """Weg 1b: die Aufzeichnung wird nicht geoeffnet, sondern eingefuegt.

    Der Fehler, um den es geht
    --------------------------
    Wer die .vbs ueber einen Editor kopiert, der sie falsch geoeffnet
    hat, fuegt Text mit einem Nullzeichen zwischen je zwei Buchstaben
    ein.  Auf dem Bildschirm ist das ein Rechteck, dann ein Buchstabe,
    dann wieder ein Rechteck.

    Ausgewertet wurde so ein Text auch bisher schon richtig -- die
    *Anzeige* blieb aber Zeichensalat.  Wer darauf blickt, haelt das
    Werkzeug fuer kaputt und sieht gar nicht, dass die Tabelle darunter
    stimmt.  Deshalb wird die Anzeige mitgezogen.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _maske_mit_einfuegung(self, salat: str):
        from PySide6.QtCore import QMimeData
        from app.gui.vbs_importer import VbsImporterWidget

        maske = VbsImporterWidget(Settings())
        daten = QMimeData()
        daten.setText(salat)
        maske.input.insertFromMimeData(daten)      # genau wie Strg+V
        maske.parse_input()
        return maske

    def test_beide_byte_reihenfolgen(self):
        """LE ergibt "Buchstabe, Rechteck", BE "Rechteck, Buchstabe"."""
        for name, kodierung in (("Buchstabe zuerst", "utf-16-le"),
                                ("Rechteck zuerst", "utf-16-be")):
            with self.subTest(muster=name):
                salat = AUFZEICHNUNG_ME11.encode(kodierung).decode("cp1252")
                maske = self._maske_mit_einfuegung(salat)

                # Die Anzeige ist lesbar -- kein Nullzeichen mehr.
                self.assertNotIn("\x00", maske.input.toPlainText())
                self.assertTrue(
                    maske.input.toPlainText().lstrip().startswith("If Not"))
                self.assertIn('findById("wnd[0]/usr/ctxtEINA-LIFNR")',
                              maske.input.toPlainText())
                # Und ausgewertet ist es auch.
                self.assertIn("ME11", maske.transaction_label.text())
                self.assertEqual(maske.table.rowCount(), 6)

    def test_mit_byte_order_mark_im_eingefuegten_text(self):
        salat = wie_aus_falschem_editor(AUFZEICHNUNG_ME11)
        maske = self._maske_mit_einfuegung(salat)
        self.assertNotIn("\x00", maske.input.toPlainText())
        self.assertEqual(maske.table.rowCount(), 6)

    def test_der_anwender_erfaehrt_davon(self):
        """Stillschweigend geradeziehen waere auch wieder falsch."""
        salat = wie_aus_falschem_editor(AUFZEICHNUNG_ME11)
        maske = self._maske_mit_einfuegung(salat)
        self.assertIn("Datei oeffnen", maske.status_label.text())

    def test_sauberer_text_wird_nicht_angefasst(self):
        maske = self._maske_mit_einfuegung(AUFZEICHNUNG_ME11)
        self.assertEqual(maske.input.toPlainText().replace("\r\n", "\n"),
                         AUFZEICHNUNG_ME11)
        self.assertEqual(maske.table.rowCount(), 6)

    def test_auch_ohne_zwischenablage_bleibt_die_anzeige_sauber(self):
        """Etwa beim Ziehen und Ablegen -- da greift insertFromMimeData nicht."""
        from app.gui.vbs_importer import VbsImporterWidget

        maske = VbsImporterWidget(Settings())
        maske.input.setPlainText(wie_aus_falschem_editor(AUFZEICHNUNG_ME11))
        maske.parse_input()
        self.assertNotIn("\x00", maske.input.toPlainText())
        self.assertEqual(maske.table.rowCount(), 6)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class TabelleUebernehmenTest(unittest.TestCase):
    """Weg 2: Bereich aus Excel einfuegen oder Datei laden."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog_mit_text(self, text: str):
        from app.gui.table_import_dialog import TableImportDialog

        dialog = TableImportDialog(Settings(), "Nordtec GmbH")
        dialog.set_grid(TableImportDialog._parse_text(text))
        return dialog

    def _steckbrief(self, positionen):
        return [(p.material_number, p.description, str(p.quantity), p.uom,
                 str(p.price), p.currency) for p in positionen]

    ERWARTET = [
        ("4711001", "Dichtring 40x52", "100", "ST", "2.95", "EUR"),
        ("4711002", "Flanschdichtung DN50", "50", "ST", "7.40", "EUR"),
    ]

    def test_eingefuegter_bereich_wird_zu_positionen(self):
        dialog = self._dialog_mit_text(TABELLE)
        self.assertEqual(dialog.table.rowCount(), 3)   # Kopf + 2 Datenzeilen
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)

    def test_spalten_werden_von_selbst_vorgeschlagen(self):
        """Der Anwender soll bestaetigen, nicht von vorn zuordnen."""
        dialog = self._dialog_mit_text(TABELLE)
        self.assertTrue(dialog.result_data.column_map or dialog.build_positions())

    def test_semikolon_statt_tabulator(self):
        dialog = self._dialog_mit_text(TABELLE.replace("\t", ";"))
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)

    def test_datei_laden_in_jeder_kodierung(self):
        from app.gui.table_import_dialog import TableImportDialog

        ordner = Path(tempfile.mkdtemp(prefix="sap_tabelle_datei_"))
        varianten = (
            ("utf-8", TABELLE.encode("utf-8")),
            ("utf-16 mit Marke", b"\xff\xfe" + TABELLE.encode("utf-16-le")),
            ("utf-16 ohne Marke", TABELLE.encode("utf-16-le")),
            ("cp1252", TABELLE.encode("cp1252")),
        )
        for name, rohdaten in varianten:
            with self.subTest(kodierung=name):
                pfad = ordner / f"listexport_{len(name)}_{name[:4]}.txt"
                pfad.write_bytes(rohdaten)
                raster = TableImportDialog._read_file(str(pfad))
                self.assertEqual(len(raster), 3)
                self.assertEqual(raster[1][2], "Dichtring 40x52")

    def test_zeichensalat_ergibt_lesbare_zellen(self):
        dialog = self._dialog_mit_text(wie_aus_falschem_editor(TABELLE))
        self.assertEqual(self._steckbrief(dialog.build_positions()),
                         self.ERWARTET)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class SchnellerfassungTest(unittest.TestCase):
    """Weg 3: von Hand tippen -- eine Zeile, Enter, fertig."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _leiste(self):
        from app.gui.quick_entry import QuickEntryBar

        leiste = QuickEntryBar()
        erfasst: list = []
        leiste.positionEntered.connect(erfasst.append)
        return leiste, erfasst

    def test_getippte_zeile_wird_zur_position(self):
        leiste, erfasst = self._leiste()
        for schluessel, wert in (("material_number", "4711003"),
                                 ("description", "Kegelstück grün"),
                                 ("quantity", "25"), ("uom", "ST"),
                                 ("price", "3,10"), ("price_unit", "1"),
                                 ("currency", "EUR")):
            leiste.edits[schluessel].setText(wert)
        leiste.commit()

        self.assertEqual(len(erfasst), 1)
        position = erfasst[0]
        self.assertEqual(position.material_number, "4711003")
        self.assertEqual(position.description, "Kegelstück grün")
        self.assertEqual(str(position.price), "3.10")
        self.assertEqual(position.currency, "EUR")

    def test_felder_sind_nach_enter_wieder_leer(self):
        """Sonst wandert der Wert der letzten Zeile in die naechste."""
        leiste, _erfasst = self._leiste()
        leiste.edits["material_number"].setText("4711003")
        leiste.edits["price"].setText("3,10")
        leiste.commit()
        self.assertTrue(all(not edit.text() for edit in leiste.edits.values()))

    def test_kopierte_excel_zeile_verteilt_sich(self):
        leiste, _erfasst = self._leiste()
        leiste._distribute("4711004\tO-Ring 25x3\t500\tST\t0,45\t1\tEUR"
                           .split("\t"))
        self.assertEqual(leiste.edits["material_number"].text(), "4711004")
        self.assertEqual(leiste.edits["description"].text(), "O-Ring 25x3")
        self.assertEqual(leiste.edits["quantity"].text(), "500")
        self.assertEqual(leiste.edits["price"].text(), "0,45")
        self.assertEqual(leiste.edits["currency"].text(), "EUR")

    def test_zeichensalat_verteilt_sich_lesbar(self):
        from app.gui.quick_entry import split_pasted_row

        leiste, _erfasst = self._leiste()
        leiste._distribute(split_pasted_row(wie_aus_falschem_editor(
            "4711005\tDichtring 60x80\t10\tST\t9,90\t1\tEUR")))
        self.assertEqual(leiste.edits["material_number"].text(), "4711005")
        self.assertEqual(leiste.edits["description"].text(), "Dichtring 60x80")
        self.assertEqual(leiste.edits["price"].text(), "9,90")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class DirektInDerTabelleTest(unittest.TestCase):
    """Weg 4: einfach in die Tabelle schreiben, die schon da ist.

    Der Fehler, um den es geht
    --------------------------
    Der direkteste Weg ueberhaupt -- Anwendung starten, Zeile anlegen,
    tippen -- ging nicht.  Ohne geladenes Angebot gab das Tabellenmodell
    keine Zeile heraus, und der Menuepunkt "Position ergaenzen" tat
    schlicht nichts: keine Zeile, keine Meldung, kein Hinweis worauf man
    wartet.  Wer ohne Datei anfangen wollte, stand vor einer leeren
    Tabelle, in die er nicht hineinkam.

    Ausserdem entstand die Zeile auf einem anderen Weg als eine schnell
    erfasste und blieb deshalb ohne Vorbelegung -- Einkaufsorganisation,
    Werk, Mengeneinheit und Preiseinheit leer.  Zwei Wege, dasselbe zu
    tun, mit verschiedenen Ergebnissen: genau das faellt spaeter
    niemandem auf.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app.bootstrap import build_services
        from app.gui.main_window import MainWindow

        cls.app = QApplication.instance() or QApplication([])
        cls.settings = Settings()
        cls.settings.use_mock_sap = True
        cls.settings.dry_run = True
        cls.settings.ensure_dirs()
        cls.services = build_services(cls.settings)
        cls.Fenster = MainWindow

    def _fenster(self):
        return self.Fenster(self.settings, self.services.as_dict())

    @staticmethod
    def _spalte(schluessel: str) -> int:
        from app.gui.offer_table import COLUMNS

        return next(i for i, spec in enumerate(COLUMNS)
                    if spec.key == schluessel)

    def _tippe(self, fenster, zeile: int, schluessel: str, wert: str) -> bool:
        from PySide6.QtCore import Qt

        index = fenster.table_model.index(zeile, self._spalte(schluessel))
        return fenster.table_model.setData(index, wert, Qt.ItemDataRole.EditRole)

    def test_beim_start_steht_schon_eine_zeile_bereit(self):
        """Ohne sie stuende der Anwender vor einer Tabelle ohne Zellen."""
        fenster = self._fenster()
        self.assertIsNotNone(fenster.offer)
        self.assertEqual(fenster.table_model.rowCount(), 1)
        self.assertTrue(
            fenster.offer.positions[0].ist_leere_erfassungszeile)

    def test_die_bereitgestellte_zeile_ist_kein_fehler(self):
        """Eine Einladung zum Tippen darf nicht rot begruesst werden."""
        from app.models.enums import PositionStatus

        fenster = self._fenster()
        position = fenster.offer.positions[0]
        self.assertEqual(len(list(position.issues)), 0)
        self.assertEqual(position.status, PositionStatus.NOT_SELECTED)
        self.assertFalse(position.selected)
        self.assertFalse(position.is_processable)
        self.assertIn("0 mit Fehler", fenster.counter_label.text())

    def test_die_zeile_erwacht_beim_ersten_wert(self):
        """Sonst muesste sie noch von Hand angehakt werden."""
        fenster = self._fenster()
        position = fenster.offer.positions[0]
        self.assertFalse(position.selected)
        self._tippe(fenster, 0, "material_number", "4711001")
        self.assertTrue(position.selected)
        self.assertFalse(position.ist_leere_erfassungszeile)

    def test_die_neue_zeile_ist_vorbelegt(self):
        """Wie eine schnell erfasste -- sonst haetten zwei Wege zwei
        Ergebnisse."""
        fenster = self._fenster()
        position = fenster.offer.positions[0]
        self.assertTrue(position.purchasing_org)
        self.assertTrue(position.plant)
        self.assertTrue(position.uom)
        self.assertTrue(position.currency)
        self.assertIsNotNone(position.price_unit)

    def test_der_cursor_steht_schon_im_ersten_feld(self):
        """Sonst muesste die neue Zeile erst gesucht und angeklickt werden."""
        from app.gui.offer_table import COLUMNS

        fenster = self._fenster()
        fenster._add_position()          # zweite Zeile, Cursor hinein
        index = fenster.table.currentIndex()
        self.assertTrue(index.isValid())
        quelle = fenster.proxy.mapToSource(index)
        self.assertEqual(COLUMNS[quelle.column()].key, "material_number")

    def test_getippte_werte_landen_in_der_position(self):
        fenster = self._fenster()
        for schluessel, wert in (("material_number", "4711001"),
                                 ("description", "Dichtring 40x52"),
                                 ("quantity", "100"),
                                 ("uom", "ST"),
                                 ("price", "2,95"),
                                 ("currency", "EUR")):
            with self.subTest(feld=schluessel):
                self.assertTrue(self._tippe(fenster, 0, schluessel, wert))

        position = fenster.offer.positions[0]
        self.assertEqual(position.material_number, "4711001")
        self.assertEqual(position.description, "Dichtring 40x52")
        self.assertEqual(str(position.quantity), "100")
        self.assertEqual(str(position.price), "2.95")
        self.assertEqual(position.currency, "EUR")

    def test_zweite_zeile_wird_weitergezaehlt(self):
        fenster = self._fenster()
        self._tippe(fenster, 0, "material_number", "4711001")
        fenster._add_position()
        self.assertEqual(fenster.table_model.rowCount(), 2)
        self.assertEqual([p.position_number for p in fenster.offer.positions],
                         ["10", "20"])

    def test_menuepunkt_legt_eine_weitere_zeile_an(self):
        """Er war immer anklickbar -- er tat nur nichts.  Jetzt beides."""
        fenster = self._fenster()
        self.assertTrue(fenster.add_position_action.isEnabled())
        self.assertEqual(fenster.add_position_action.shortcut().toString(),
                         "Ins")
        vorher = fenster.table_model.rowCount()
        fenster.add_position_action.trigger()
        self.assertEqual(fenster.table_model.rowCount(), vorher + 1)

    def test_echte_positionen_raeumen_die_leerzeile_weg(self):
        """Sonst stuende sie zwischen den uebernommenen Positionen herum."""
        from app.models.enums import SourceKind
        from app.models.offer_position import OfferPosition

        fenster = self._fenster()
        self.assertEqual(fenster.table_model.rowCount(), 1)
        echte = OfferPosition(source_kind=SourceKind.EXCEL,
                              material_number="4711001",
                              description="Dichtring 40x52")
        fenster._add_positions([echte], "Tabelle")
        self.assertEqual(fenster.table_model.rowCount(), 1)
        self.assertEqual(fenster.offer.positions[0].material_number, "4711001")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
