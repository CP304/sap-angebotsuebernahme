"""Mengenstaffeln: auslesen, vergleichen, von Hand pflegen.

Was bisher fehlte
-----------------
Geschrieben wurden Staffeln schon.  Zwei Haelften fehlten:

*Auslesen.*  Der Bestandssatz kam ohne seine Staffel zurueck.  Damit sah
der Alt/Neu-Vergleich nur den Grundpreis: stand in SAP eine dreistufige
Staffel und im Angebot ein einzelner Preis, meldete die Anwendung
"unveraendert" -- und der Anwender haette die Stufen ueberschrieben, ohne
sie je gesehen zu haben.

*Pflegen.*  Staffeln entstanden nur beim Zusammenfassen mehrerer
Angebotszeilen.  Eine Staffel, die im Angebot als Fliesstext steht, eine
nachverhandelte Stufe oder eine bestehende SAP-Staffel, die angepasst
statt ueberschrieben werden soll -- dafuer gab es keinen Weg.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_staffeln_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.config.settings import Settings  # noqa: E402
from app.models.enums import SourceKind  # noqa: E402
from app.models.offer_position import OfferPosition  # noqa: E402
from app.models.sap_info_record import SapInfoRecord  # noqa: E402
from app.services.comparison_service import ComparisonService  # noqa: E402


def _position(**werte) -> OfferPosition:
    grund = dict(source_kind=SourceKind.MANUAL, material_number="4711001",
                 vendor_number="100234", price=Decimal("13.20"),
                 price_unit=1, currency="EUR", uom="ST",
                 purchasing_org="1000", plant="1000")
    grund.update(werte)
    return OfferPosition(**grund)


def _bestand(stufen=None, gelesen=True, **werte) -> SapInfoRecord:
    satz = SapInfoRecord(material_number="4711001", vendor_number="100234",
                         exists=True, price=Decimal("13.20"), price_unit=1,
                         currency="EUR", order_unit="ST", **werte)
    satz.scales = list(stufen or [])
    satz.scales_read = gelesen
    return satz


class StaffelVergleichTest(unittest.TestCase):
    """Der Vergleich muss die Staffel sehen, sonst ist er blind."""

    def setUp(self):
        self.vergleich = ComparisonService(Settings())

    def test_gleiche_staffel_ist_keine_aenderung(self):
        stufen = [(Decimal("1"), Decimal("13.20")),
                  (Decimal("500"), Decimal("12.85"))]
        position = _position()
        position.scale_quantities = list(stufen)
        self.assertFalse(self.vergleich.scales_changed(position, _bestand(stufen)))

    def test_geaenderter_stufenpreis_faellt_auf(self):
        position = _position()
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("11.99"))]
        bestand = _bestand([(Decimal("1"), Decimal("13.20")),
                            (Decimal("500"), Decimal("12.85"))])
        self.assertTrue(self.vergleich.scales_changed(position, bestand))

    def test_zusaetzliche_stufe_faellt_auf(self):
        position = _position()
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85")),
                                     (Decimal("1000"), Decimal("12.40"))]
        bestand = _bestand([(Decimal("1"), Decimal("13.20")),
                            (Decimal("500"), Decimal("12.85"))])
        self.assertTrue(self.vergleich.scales_changed(position, bestand))

    def test_angebot_ohne_staffel_bei_bestehender_sap_staffel(self):
        """Der gefaehrliche Fall: die Stufen gingen sonst stillschweigend
        verloren."""
        position = _position()
        bestand = _bestand([(Decimal("1"), Decimal("13.20")),
                            (Decimal("500"), Decimal("12.85"))])
        self.assertTrue(self.vergleich.scales_changed(position, bestand))

    def test_ungelesene_staffel_behauptet_nichts(self):
        """"Keine Staffel in SAP" und "nicht gelesen" sind zweierlei."""
        position = _position()
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85"))]
        self.assertFalse(
            self.vergleich.scales_changed(position, _bestand(gelesen=False)))

    def test_die_staffel_geht_in_die_gesamtbewertung_ein(self):
        """Sonst stuende "unveraendert" auf einem Satz, der es nicht ist."""
        from app.models.enums import InfoRecordAction

        position = _position()
        position.sap_loaded = True
        position.sap_info_record = _bestand(
            [(Decimal("1"), Decimal("13.20")), (Decimal("500"), Decimal("12.85"))],
            valid_from=None, valid_to=None)
        from datetime import datetime

        position.sap_info_record.read_at = datetime.now()
        self.vergleich.compare_position(position)
        self.assertEqual(position.info_record_action, InfoRecordAction.UPDATE)


class MockRundlaufTest(unittest.TestCase):
    """Schreiben, zuruecklesen, pruefen -- gegen das Testsystem."""

    def setUp(self):
        from app.sap.gateway import SapGateway

        self.settings = Settings()
        self.settings.use_mock_sap = True
        self.settings.dry_run = False
        self.settings.ensure_dirs()
        self.gateway = SapGateway(self.settings)
        self.gateway.reset_mock_data()
        for selektor in self.gateway.selectors.screens[
                "info_record_conditions"].elements.values():
            selektor.verified = True

    def _schreibe(self, position):
        from datetime import date

        from app.sap.interfaces import WriteContext

        kontext = WriteContext(dry_run=False, valid_from=date.today(),
                              valid_to=None, settings=self.settings)
        return self.gateway.info_records.write(position, kontext), kontext

    def test_geschriebene_staffel_kommt_zurueck(self):
        position = _position(price=Decimal("13.20"))
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85")),
                                     (Decimal("1000"), Decimal("12.40"))]
        self._schreibe(position)

        gelesen = self.gateway.info_records.read("4711001", "100234", "1000", "1000")
        self.assertTrue(gelesen.scales_read)
        self.assertEqual([(Decimal(m), Decimal(p)) for m, p in gelesen.scales],
                         position.sorted_scales())

    def test_ruecklese_pruefung_bestaetigt_die_staffel(self):
        from app.sap.info_record_service import verify_info_record_write

        position = _position(price=Decimal("13.20"))
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85"))]
        _ergebnis, kontext = self._schreibe(position)
        gelesen = self.gateway.info_records.read("4711001", "100234", "1000", "1000")

        in_ordnung, meldungen = verify_info_record_write(
            gelesen, position, kontext, self.settings)
        self.assertTrue(in_ordnung, meldungen)
        self.assertTrue(any("Mengenstaffel bestaetigt" in m for m in meldungen),
                        meldungen)

    def test_fehlende_stufe_faellt_bei_der_ruecklese_auf(self):
        """Eine Stufe, die SAP verworfen hat, darf nicht durchgehen."""
        from app.sap.info_record_service import verify_info_record_write

        position = _position(price=Decimal("13.20"))
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85"))]
        _ergebnis, kontext = self._schreibe(position)
        gelesen = self.gateway.info_records.read("4711001", "100234", "1000", "1000")
        gelesen.scales = [(Decimal("1"), Decimal("13.20"))]   # SAP hat eine verworfen

        in_ordnung, meldungen = verify_info_record_write(
            gelesen, position, kontext, self.settings)
        self.assertFalse(in_ordnung)
        self.assertIn("ab 500", " ".join(meldungen))


class StufenPruefungTest(unittest.TestCase):
    """Was der Pflegedialog annimmt und was nicht."""

    def _pruefe(self, zeilen):
        from app.gui.scale_dialog import pruefe_stufen

        return pruefe_stufen(zeilen)

    def test_saubere_staffel(self):
        stufen, fehler = self._pruefe([("1", "13,20"), ("500", "12,85"),
                                       ("1000", "12,40")])
        self.assertEqual(fehler, [])
        self.assertEqual(stufen, [(Decimal("1"), Decimal("13.20")),
                                  (Decimal("500"), Decimal("12.85")),
                                  (Decimal("1000"), Decimal("12.40"))])

    def test_wird_aufsteigend_sortiert(self):
        stufen, _fehler = self._pruefe([("1000", "12,40"), ("1", "13,20")])
        self.assertEqual([menge for menge, _preis in stufen],
                         [Decimal("1"), Decimal("1000")])

    def test_leere_zeilen_werden_uebergangen(self):
        stufen, fehler = self._pruefe([("1", "13,20"), ("", ""), ("500", "12,85"),
                                       ("", "")])
        self.assertEqual(fehler, [])
        self.assertEqual(len(stufen), 2)

    def test_halbe_zeile_wird_benannt(self):
        _stufen, fehler = self._pruefe([("500", ""), ("1000", "12,40")])
        self.assertTrue(fehler)
        self.assertIn("Zeile 1", fehler[0])
        self.assertIn("Preis", fehler[0])

    def test_text_statt_zahl(self):
        _stufen, fehler = self._pruefe([("ab 500", "12,85"), ("1000", "12,40")])
        self.assertTrue(any("keine Menge" in f for f in fehler))

    def test_doppelte_ab_menge(self):
        """In SAP waere die zweite Zeile ein Fehler -- oder schlimmer, sie
        ueberschriebe die erste."""
        _stufen, fehler = self._pruefe([("500", "12,85"), ("500", "12,40")])
        self.assertTrue(any("mehrfach" in f for f in fehler))

    def test_negative_werte(self):
        _stufen, fehler = self._pruefe([("500", "-1"), ("1000", "12,40")])
        self.assertTrue(any("negativ" in f for f in fehler))

    def test_eine_einzige_stufe_ist_keine_staffel(self):
        _stufen, fehler = self._pruefe([("500", "12,85")])
        self.assertTrue(any("zwei Stufen" in f for f in fehler))

    def test_steigende_preise_sind_erlaubt(self):
        """Kommt vor: kleine Mengen aus dem Lager, grosse aus der Fertigung."""
        _stufen, fehler = self._pruefe([("1", "10,00"), ("500", "11,00")])
        self.assertEqual(fehler, [])

    def test_leere_tabelle_ergibt_nichts(self):
        stufen, fehler = self._pruefe([("", "")] * 5)
        self.assertEqual((stufen, fehler), ([], []))


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class PflegeDialogTest(unittest.TestCase):
    """Der Dialog selbst."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, position):
        from app.gui.scale_dialog import ScaleDialog

        return ScaleDialog(position)

    def test_bestehende_staffel_steht_drin(self):
        position = _position()
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85"))]
        dialog = self._dialog(position)
        self.assertEqual(dialog.table.item(0, 0).text(), "1")
        self.assertEqual(dialog.table.item(1, 0).text(), "500")

    def test_ohne_staffel_dient_der_preis_als_erste_stufe(self):
        """Kein leeres Blatt -- die zweite Stufe tippt sich dann schneller."""
        position = _position(price=Decimal("13.20"), quantity=Decimal("1"))
        dialog = self._dialog(position)
        self.assertEqual(dialog.table.item(0, 0).text(), "1")
        self.assertEqual(dialog.table.item(0, 1).text(), "13,2")

    def test_der_sap_bestand_steht_daneben(self):
        """Damit niemand eine bestehende Staffel blind ueberschreibt."""
        position = _position()
        position.sap_info_record = _bestand(
            [(Decimal("1"), Decimal("13.50")), (Decimal("250"), Decimal("13.10"))])
        dialog = self._dialog(position)
        self.assertIn("13,50", dialog.sap_label.text())
        self.assertIn("13,10", dialog.sap_label.text())

    def test_ohne_gelesenen_bestand_wird_nichts_behauptet(self):
        position = _position()
        dialog = self._dialog(position)
        self.assertIn("noch nicht", dialog.sap_label.text().lower())

    def test_sap_staffel_uebernehmen(self):
        position = _position()
        position.sap_info_record = _bestand(
            [(Decimal("1"), Decimal("13.50")), (Decimal("250"), Decimal("13.10"))])
        dialog = self._dialog(position)
        dialog._sap_uebernehmen()
        self.assertEqual(dialog.table.item(0, 1).text(), "13,5")
        self.assertEqual(dialog.table.item(1, 0).text(), "250")

    def test_uebernehmen_setzt_staffel_und_grundpreis(self):
        position = _position(price=Decimal("99.00"))
        dialog = self._dialog(position)
        dialog._leeren()
        from PySide6.QtWidgets import QTableWidgetItem
        for zeile, (menge, preis) in enumerate((("1", "13,20"), ("500", "12,85"))):
            dialog.table.setItem(zeile, 0, QTableWidgetItem(menge))
            dialog.table.setItem(zeile, 1, QTableWidgetItem(preis))
        dialog._pruefen_und_schliessen()
        meldungen = dialog.uebernehmen()

        self.assertTrue(position.has_scales)
        self.assertEqual(position.sorted_scales(),
                         [(Decimal("1"), Decimal("13.20")),
                          (Decimal("500"), Decimal("12.85"))])
        self.assertEqual(position.price, Decimal("13.20"),
                         "Der Grundpreis ist die unterste Stufe")
        self.assertTrue(meldungen)

    def test_fehlerhafte_eingabe_schliesst_den_dialog_nicht(self):
        from PySide6.QtWidgets import QTableWidgetItem

        position = _position()
        dialog = self._dialog(position)
        dialog._leeren()
        dialog.table.setItem(0, 0, QTableWidgetItem("500"))   # ohne Preis
        dialog._pruefen_und_schliessen()
        self.assertTrue(dialog.fehler_label.text())
        self.assertEqual(dialog.stufen, [])

    def test_geleerte_tabelle_entfernt_die_staffel(self):
        position = _position()
        position.scale_quantities = [(Decimal("1"), Decimal("13.20")),
                                     (Decimal("500"), Decimal("12.85"))]
        dialog = self._dialog(position)
        dialog._leeren()
        dialog._pruefen_und_schliessen()
        meldungen = dialog.uebernehmen()
        self.assertFalse(position.has_scales)
        self.assertTrue(any("entfernt" in m for m in meldungen))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
