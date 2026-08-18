"""Schnellerfassung: eine Zeile tippen, Enter, fertig.

Der Fall, um den es geht: eine formlose Preismitteilung, aus der genau
eine Position wird.  Ueber die Tabelle ist das umstaendlich -- Zeile
anlegen, Zelle suchen, tippen, naechste Zelle suchen.  Hier steht alles
nebeneinander.

Gepruegt wird vor allem, dass die Schnellerfassung sich an dieselben
Grundsaetze haelt wie der Rest: nichts raten, nichts stillschweigend
verwerfen, und Werte tragen die Herkunft MANUAL.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_quick_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.models.enums import FieldOrigin                        # noqa: E402

if HAS_QT:
    from app.gui.quick_entry import QuickEntryBar, split_pasted_row


class ZeileZerlegenTest(unittest.TestCase):
    """Reine Textfunktion -- laeuft auch ohne Qt."""

    def setUp(self) -> None:
        if not HAS_QT:
            self.skipTest("PySide6 nicht installiert")

    def test_tabulatoren_aus_excel(self):
        werte = split_pasted_row("4711001\tDichtring\t500\tST\t2,95")
        self.assertEqual(werte, ["4711001", "Dichtring", "500", "ST", "2,95"])

    def test_semikolon(self):
        self.assertEqual(split_pasted_row("4711001;Dichtring;500"),
                         ["4711001", "Dichtring", "500"])

    def test_leerzeichen_werden_nicht_getrennt(self):
        """"Dichtring NBR 40x52x7" ist eine Bezeichnung, keine vier Werte."""
        self.assertEqual(split_pasted_row("Dichtring NBR 40x52x7"), [])

    def test_nur_erste_zeile(self):
        werte = split_pasted_row("4711001\t500\nzweite\tzeile")
        self.assertEqual(werte, ["4711001", "500"])

    def test_leerer_text(self):
        self.assertEqual(split_pasted_row(""), [])
        self.assertEqual(split_pasted_row(None), [])


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class SchnellerfassungTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.bar = QuickEntryBar()
        self.positionen = []
        self.meldungen = []
        self.bar.positionEntered.connect(self.positionen.append)
        self.bar.message.connect(self.meldungen.append)

    def _tippen(self, **werte) -> None:
        for schluessel, text in werte.items():
            self.bar.edits[schluessel].setText(text)

    # -- Gutfall -------------------------------------------------------
    def test_vollstaendige_zeile(self):
        self._tippen(material_number="4711001", description="Dichtring",
                     quantity="500", uom="st", price="2,95",
                     price_unit="1", currency="eur")
        self.bar.commit()

        self.assertEqual(len(self.positionen), 1)
        position = self.positionen[0]
        self.assertEqual(position.material_number, "4711001")
        self.assertEqual(position.quantity, Decimal("500"))
        self.assertEqual(position.price, Decimal("2.95"))
        self.assertEqual(position.price_unit, 1)

    def test_einheiten_werden_grossgeschrieben(self):
        self._tippen(material_number="4711001", uom="st", currency="eur")
        self.bar.commit()
        self.assertEqual(self.positionen[0].uom, "ST")
        self.assertEqual(self.positionen[0].currency, "EUR")

    def test_herkunft_ist_manuell(self):
        self._tippen(material_number="4711001", price="2,95")
        self.bar.commit()
        position = self.positionen[0]
        self.assertEqual(position.origin("material_number"), FieldOrigin.MANUAL)
        self.assertEqual(position.origin("price"), FieldOrigin.MANUAL)

    def test_englisches_zahlformat(self):
        self._tippen(material_number="4711001", price="2.95")
        self.bar.commit()
        self.assertEqual(self.positionen[0].price, Decimal("2.95"))

    def test_felder_werden_danach_geleert(self):
        self._tippen(material_number="4711001", price="2,95")
        self.bar.commit()
        self.assertEqual(self.bar.values()["material_number"], "")
        self.assertEqual(self.bar.values()["price"], "")

    def test_teilangabe_reicht(self):
        """Nur ein Preis ohne Material ist erlaubt -- der Rest folgt spaeter."""
        self._tippen(price="2,95")
        self.bar.commit()
        self.assertEqual(len(self.positionen), 1)

    # -- Darf nicht anschlagen ------------------------------------------
    def test_leere_eingabe_legt_nichts_an(self):
        self.bar.commit()
        self.assertEqual(self.positionen, [])
        self.assertIn("Nichts eingetragen", " ".join(self.meldungen))

    def test_unlesbare_zahl_wird_nicht_geraten(self):
        self._tippen(material_number="4711001", price="etwa drei Euro")
        self.bar.commit()
        self.assertEqual(len(self.positionen), 1)
        position = self.positionen[0]
        self.assertIsNone(position.price, "Ein unlesbarer Preis darf nicht geraten werden")
        self.assertIn("Nicht uebernommen", " ".join(self.meldungen),
                      "Das Verwerfen muss gemeldet werden, nicht stillschweigend passieren")

    def test_nur_unlesbares_legt_nichts_an(self):
        self._tippen(price="keine Ahnung")
        self.bar.commit()
        self.assertEqual(self.positionen, [])
        self.assertIn("Keine verwertbare Angabe", " ".join(self.meldungen))

    def test_unlesbare_preiseinheit(self):
        self._tippen(material_number="4711001", price_unit="hundert")
        self.bar.commit()
        self.assertEqual(len(self.positionen), 1)
        self.assertIn("Preiseinheit", " ".join(self.meldungen))

    # -- Einfuegen ------------------------------------------------------
    def test_eingefuegte_zeile_verteilt_sich(self):
        self.bar._distribute(["4711001", "Dichtring", "500", "ST", "2,95"])
        werte = self.bar.values()
        self.assertEqual(werte["material_number"], "4711001")
        self.assertEqual(werte["description"], "Dichtring")
        self.assertEqual(werte["uom"], "ST")

    def test_ueberzaehlige_spalten_werden_gemeldet(self):
        self.bar._distribute([str(i) for i in range(12)])
        self.assertIn("nicht uebernommen", " ".join(self.meldungen).lower())

    def test_kurze_zeile_laesst_rest_leer(self):
        self.bar._distribute(["4711001", "Dichtring"])
        self.assertEqual(self.bar.values()["price"], "")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class ZusammenspielMitDemFensterTest(unittest.TestCase):
    """Die Leiste muss im Fenster erreichbar sein und Positionen anhaengen."""

    @classmethod
    def setUpClass(cls) -> None:
        from app.bootstrap import build_services
        from app.config.settings import Settings
        from app.gui.main_window import MainWindow

        cls.app = QApplication.instance() or QApplication([])
        cls.settings = Settings()
        cls.settings.use_mock_sap = True
        cls.settings.dry_run = True
        cls.settings.ensure_dirs()
        cls.services = build_services(cls.settings)
        cls.window = MainWindow(cls.settings, cls.services.as_dict())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        if cls.services.repository is not None:
            cls.services.repository.close()

    def test_standardmaessig_eingeklappt(self):
        # isVisible() waere hier immer False, weil das Fenster im Test nie
        # gezeigt wird -- der Test wuerde also nichts beweisen.  isHidden()
        # fragt den gesetzten Zustand ab, unabhaengig vom Elternfenster.
        self.assertTrue(self.window.quick_entry.isHidden(),
                        "Die Leiste darf keinen Platz kosten, solange sie "
                        "nicht gebraucht wird")

    def test_umschalten(self):
        self.window.toggle_quick_entry(True)
        self.assertFalse(self.window.quick_entry.isHidden())
        self.assertTrue(self.window.quick_entry_action.isChecked(),
                        "Der Menuehaken muss den Zustand zeigen")
        self.window.toggle_quick_entry(False)
        self.assertTrue(self.window.quick_entry.isHidden())
        self.assertFalse(self.window.quick_entry_action.isChecked())

    def test_tastenkuerzel_vorhanden(self):
        self.assertEqual(self.window.quick_entry_action.shortcut().toString(),
                         "Ctrl+E")

    def test_position_landet_im_angebot(self):
        vorher = len(self.window.offer.positions) if self.window.offer else 0
        self.window.quick_entry.edits["material_number"].setText("4711001")
        self.window.quick_entry.edits["price"].setText("3,95")
        self.window.quick_entry.commit()
        self.assertEqual(len(self.window.offer.positions), vorher + 1)
        self.assertEqual(self.window.offer.positions[-1].material_number, "4711001")

    def test_position_bekommt_eine_nummer(self):
        self.window.quick_entry.edits["material_number"].setText("4711002")
        self.window.quick_entry.commit()
        self.assertTrue(self.window.offer.positions[-1].position_number,
                        "Ohne Positionsnummer faellt die Position spaeter auf")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
