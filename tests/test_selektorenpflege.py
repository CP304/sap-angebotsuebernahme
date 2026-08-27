"""Die Seite "SAP-Feld-IDs" -- Zuordnen ohne sich selbst zu zerschiessen.

Der Fehler, um den es geht
--------------------------
Die Zuordnung einer Aufzeichnung lief ueber den technischen Feldnamen am
Ende der ID (``EINA-LIFNR``).  Fuer Eingabefelder ist das richtig.  Nur:
Schaltflaechen haben keinen Feldnamen.  Bei ``wnd[0]/tbar[0]/btn[3]``
bleibt nach Praefix und Index **nichts** uebrig -- und dieses Nichts galt
als Name.

Damit war jede Schaltflaeche mit jeder anderen identisch.  Ein einziger
Zurueck-Knopf in einer Aufzeichnung schrieb sich in fuenfzehn Felder:
Sichern, Abbrechen, Beenden, Konditionen, Materialsichten,
Bestellpruefung, Anhang-Dialog.  Danach haette "Sichern" auf "Zurueck"
gedrueckt -- und der Anwender haette einen Bestaetigungsdialog gesehen,
der voellig plausibel aussah.

Ausserdem galt die Zuordnung ueber alle Bildschirme hinweg: eine
Aufzeichnung aus ME11 konnte Kontraktfelder ueberschreiben.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_selpflege_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.sap.selectors import SelectorRegistry  # noqa: E402

ME11_MIT_KNOPF = (
    'session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"\n'
    'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"\n'
    'session.findById("wnd[0]/tbar[0]/btn[3]").press\n'
    'session.findById("wnd[0]/tbar[0]/btn[11]").press\n'
)

#: Dieselbe Transaktion, aber die Einkaufsorganisation sitzt in dieser
#: Anlage auf einem Subscreen -- der Fall, fuer den es die Seite gibt.
ME11_ABWEICHEND = (
    'session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"\n'
    'session.findById("wnd[0]/usr/subSUB0:SAPLMEGUI:0030/ctxtEINE-EKORG").text = "1000"\n'
    'session.findById("wnd[0]/tbar[0]/btn[11]").press\n'
)


class ZuordnungAusAufzeichnungTest(unittest.TestCase):
    """suggest_mapping -- was zugeordnet wird und was ausdruecklich nicht."""

    def setUp(self):
        self.registry = SelectorRegistry()

    def _vorschlag(self, vbs: str, transaktion: str = ""):
        ids = self.registry.ids_from_vbs(vbs)
        return self.registry.suggest_mapping(ids, transaktion)

    def test_schaltflaechen_werden_nie_zugeordnet(self):
        """Der Fehler, der die Anwendung unbrauchbar gemacht haette."""
        self.assertEqual(self._vorschlag(ME11_MIT_KNOPF), {})
        self.assertEqual(self._vorschlag(ME11_MIT_KNOPF, "ME11"), {})

    def test_sichern_bleibt_sichern(self):
        """Die Probe aufs Exempel."""
        vorher = self.registry.get("common", "save").id
        for (screen_key, element_key), neu in self._vorschlag(ME11_MIT_KNOPF).items():
            self.registry.set_id(screen_key, element_key, neu)
        self.assertEqual(self.registry.get("common", "save").id, vorher)

    def test_echte_abweichung_wird_erkannt(self):
        """Sonst waere die Seite nutzlos."""
        mapping = self._vorschlag(ME11_ABWEICHEND, "ME11")
        self.assertEqual(
            mapping.get(("info_record_initial", "purchasing_org")),
            "wnd[0]/usr/subSUB0:SAPLMEGUI:0030/ctxtEINE-EKORG")

    def test_nur_abweichungen_kommen_zurueck(self):
        """Was bereits stimmt, ist keine Aenderung."""
        vbs = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME11"\n'
               'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"\n')
        self.assertEqual(self._vorschlag(vbs, "ME11"), {})

    def test_fremde_transaktion_wird_nicht_angefasst(self):
        """Eine ME01-Aufzeichnung enthaelt keine Infosatzfelder."""
        vbs = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME01"\n'
               'session.findById("wnd[0]/usr/subX/ctxtEINE-NETPR").text = "9,99"\n')
        self.assertEqual(self._vorschlag(vbs, "ME01"), {})

    def test_mehrdeutiges_wird_ohne_transaktion_nicht_geraten(self):
        """RM06E-LIFNR gibt es im Kontrakt-Einstieg und in der Suche.

        Ist unbekannt, woher die Aufzeichnung stammt, verraet nichts,
        welches der beiden gemeint ist -- also wird geschwiegen und der
        Anwender entscheidet.  Ein falscher Vorschlag, den jemand
        ungeprueft bestaetigt, schreibt spaeter in das falsche Feld.
        """
        vbs = 'session.findById("wnd[0]/usr/subZ/ctxtRM06E-LIFNR").text = "100234"'
        mapping = self._vorschlag(vbs)
        self.assertNotIn(("contract_initial", "vendor"), mapping)
        self.assertNotIn(("contract_search", "vendor"), mapping)

    def test_die_transaktion_macht_mehrdeutiges_eindeutig(self):
        """Und genau das ist der Gewinn der Eingrenzung.

        Die Kontraktsuche gehoert zu ME33K/ME3L, nicht zu ME31K.  Steht
        die Transaktion fest, bleibt nur ein Feld uebrig -- und aus einem
        Fall, in dem geschwiegen werden musste, wird ein Vorschlag.
        """
        vbs = ('session.findById("wnd[0]/tbar[0]/okcd").text = "/nME31K"\n'
               'session.findById("wnd[0]/usr/subZ/ctxtRM06E-LIFNR").text = "100234"\n')
        mapping = self._vorschlag(vbs, "ME31K")
        self.assertEqual(mapping.get(("contract_initial", "vendor")),
                         "wnd[0]/usr/subZ/ctxtRM06E-LIFNR")
        self.assertNotIn(("contract_search", "vendor"), mapping)

    def test_ohne_transaktion_bleibt_alles_moeglich(self):
        """Ist die Herkunft unbekannt, wird nicht kuenstlich eingeschraenkt."""
        mapping = self._vorschlag(ME11_ABWEICHEND)
        self.assertIn(("info_record_initial", "purchasing_org"), mapping)

    def test_leere_aufzeichnung(self):
        self.assertEqual(self.registry.suggest_mapping([]), {})
        self.assertEqual(self.registry.suggest_mapping([], "ME11"), {})


class TransaktionAufBildschirmeTest(unittest.TestCase):
    """screens_fuer_transaktion -- die Eingrenzung selbst."""

    def setUp(self):
        self.registry = SelectorRegistry()

    def test_jede_transaktion_trifft_ihre_bildschirme(self):
        erwartet = {
            "ME11": "info_record_initial",
            "ME12": "info_record_purchasing",
            "ME01": "source_list_initial",
            "ME31K": "contract_initial",
            "ME21N": "purchase_order",
            "MM03": "material_display",
            "XK03": "vendor_display",
        }
        for code, screen in erwartet.items():
            with self.subTest(transaktion=code):
                self.assertIn(screen, self.registry.screens_fuer_transaktion(code))

    def test_fremde_bildschirme_bleiben_draussen(self):
        me11 = self.registry.screens_fuer_transaktion("ME11")
        self.assertNotIn("contract_initial", me11)
        self.assertNotIn("purchase_order", me11)
        self.assertNotIn("source_list_initial", me11)

    def test_gemeinsame_elemente_sind_immer_dabei(self):
        """Kommandofeld, Sichern und Meldungszeile gibt es ueberall."""
        for code in ("ME11", "ME31K", "ME21N", "MM03"):
            with self.subTest(transaktion=code):
                self.assertIn("common",
                              self.registry.screens_fuer_transaktion(code))

    def test_unbekannte_transaktion_sperrt_niemanden_aus(self):
        for code in ("", "ZZ99", "   "):
            with self.subTest(code=repr(code)):
                self.assertEqual(len(self.registry.screens_fuer_transaktion(code)),
                                 len(self.registry.screens))


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class PflegeMaskeTest(unittest.TestCase):
    """Die Seite selbst: was der Anwender sieht und aendern kann."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _seite(self):
        from app.config.settings import Settings
        from app.gui.selector_view import SelectorView

        einstellungen = Settings()
        einstellungen.ensure_dirs()
        return SelectorView(SelectorRegistry(), einstellungen)

    def _erstes_kind(self, seite):
        for i in range(seite.tree.topLevelItemCount()):
            top = seite.tree.topLevelItem(i)
            if top.childCount():
                return top.child(0)
        raise AssertionError("Kein Feld in der Tabelle")

    def test_jedes_feld_zeigt_seinen_klartext(self):
        """Beim Eintragen von Hand die eigentliche Hilfe."""
        seite = self._seite()
        seite.search_edit.setText("LIFNR")
        gefunden = []
        for i in range(seite.tree.topLevelItemCount()):
            top = seite.tree.topLevelItem(i)
            for j in range(top.childCount()):
                gefunden.append(top.child(j).text(3))
        self.assertTrue(gefunden)
        for klartext in gefunden:
            self.assertIn("Lieferantennummer", klartext)

    def test_geaenderte_id_zieht_den_klartext_mit(self):
        """So faellt sofort auf, wenn die neue ID ein anderes Feld meint."""
        from PySide6.QtCore import Qt

        seite = self._seite()
        kind = self._erstes_kind(seite)
        kind.setText(2, "wnd[0]/usr/ctxtEINA-IDNLF")
        self.assertIn("Materialnummer des Lieferanten", kind.text(3))
        self.assertEqual(kind.checkState(5), Qt.CheckState.Unchecked)

    def test_geaenderte_id_gilt_wieder_als_ungeprueft(self):
        """Sonst schriebe die Anwendung mit einer unkontrollierten ID."""
        from PySide6.QtCore import Qt

        seite = self._seite()
        kind = self._erstes_kind(seite)
        kind.setCheckState(5, Qt.CheckState.Checked)
        screen_key, element_key = kind.data(0, Qt.ItemDataRole.UserRole)
        self.assertTrue(seite.registry.get(screen_key, element_key).verified)

        kind.setText(2, "wnd[0]/usr/ctxtEINA-ANDERS")
        self.assertFalse(seite.registry.get(screen_key, element_key).verified)

    def test_spaltenkopf(self):
        seite = self._seite()
        kopf = [seite.tree.headerItem().text(i) for i in range(6)]
        self.assertEqual(kopf[2], "SAP-GUI-ID")
        self.assertEqual(kopf[3], "So heisst das Feld in SAP")
        self.assertEqual(kopf[5], "Geprueft")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class SeiteIstErreichbarTest(unittest.TestCase):
    """Eine gebaute Seite, die in keinem Menue steht, gibt es nicht.

    Der Fehler, um den es geht
    --------------------------
    Die Pflegeseite wurde beim Start erzeugt, ihr Aenderungssignal war
    verdrahtet -- nur in die Liste der Verwaltungsseiten eingetragen war
    sie nie.  Damit war sie ueber kein Menue zu oeffnen, obwohl mehrere
    Meldungen ausdruecklich auf sie verweisen ("bitte pruefen Sie die
    SAP-Feld-IDs auf der gleichnamigen Seite").  Wer dem folgte, suchte
    eine Seite, die es im Menue nicht gab.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app.bootstrap import build_services
        from app.config.settings import Settings
        from app.gui.main_window import MainWindow

        cls.app = QApplication.instance() or QApplication([])
        einstellungen = Settings()
        einstellungen.use_mock_sap = True
        einstellungen.dry_run = True
        einstellungen.ensure_dirs()
        cls.fenster = MainWindow(einstellungen,
                                 build_services(einstellungen).as_dict())

    def _seiten(self) -> dict:
        return {name: widget for name, widget in self.fenster._admin_pages}

    def test_beide_seiten_stehen_im_menue(self):
        from app.gui.selector_view import SelectorView
        from app.gui.vbs_importer import VbsImporterWidget

        seiten = self._seiten()
        self.assertIsInstance(seiten.get("SAP-Feld-IDs"), SelectorView)
        self.assertIsInstance(seiten.get("Aufzeichnung einlesen (.vbs)"),
                              VbsImporterWidget)

    def test_erst_einlesen_dann_pruefen(self):
        """Die Reihenfolge im Menue ist die Reihenfolge der Arbeit."""
        namen = [name for name, _ in self.fenster._admin_pages]
        self.assertLess(namen.index("Aufzeichnung einlesen (.vbs)"),
                        namen.index("SAP-Feld-IDs"))

    def test_jede_gebaute_verwaltungsseite_ist_erreichbar(self):
        """Damit dasselbe nicht der naechsten Seite passiert."""
        eingetragen = {id(widget) for _name, widget in self.fenster._admin_pages}
        for attribut in ("history_view", "mapping_view", "selector_view",
                         "settings_view", "diagnosis_view", "vbs_importer"):
            with self.subTest(seite=attribut):
                widget = getattr(self.fenster, attribut, None)
                self.assertIsNotNone(widget, attribut)
                self.assertIn(id(widget), eingetragen,
                              f"{attribut} ist gebaut, steht aber in keinem Menue")

    def test_die_seite_laesst_sich_oeffnen(self):
        self.fenster.open_admin("SAP-Feld-IDs")
        self.assertIsNotNone(self.fenster._admin_window)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
