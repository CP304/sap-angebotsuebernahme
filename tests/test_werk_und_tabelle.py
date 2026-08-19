"""Werksspezifische Sichten und einfache Tabellenbedienung.

Werk und Einkaufsorganisation
-----------------------------
Derselbe Artikel beim selben Lieferanten hat je Werk eine EIGENE
Infosatz-Sicht mit eigenem Preis.  Welche geschrieben wird, entscheiden
Einkaufsorganisation und Werk -- deshalb stehen beide als eigene Spalte
in der Tabelle und nicht nur im Tooltip.  Wer die Spalte nicht sieht,
merkt eine Verwechslung erst, wenn der Preis im falschen Werk steht.

Gilt ein Angebot fuer mehrere Werke, muessen die Positionen mehrfach
vorkommen -- einmal je Werk.  Beim Vervielfaeltigen darf NICHT alles
mitkopiert werden: der gelesene Infosatz gehoert zum Ursprungswerk, und
ein Ergebnis aus einem frueheren Lauf waere hier schlicht falsch.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_werk_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.models.enums import FieldOrigin, PositionStatus          # noqa: E402
from app.models.offer import Offer                                # noqa: E402
from app.models.offer_position import OfferPosition               # noqa: E402


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class SpaltenTest(unittest.TestCase):
    """EKorg und Werk muessen sichtbar UND bearbeitbar sein."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _spalte(self, key: str):
        from app.gui.offer_table import COLUMNS

        treffer = [(i, s) for i, s in enumerate(COLUMNS) if s.key == key]
        self.assertTrue(treffer, f"Spalte {key} fehlt")
        return treffer[0]

    def test_werk_ist_eine_spalte(self):
        _index, spec = self._spalte("plant")
        self.assertEqual(spec.title, "Werk")

    def test_ekorg_ist_eine_spalte(self):
        _index, spec = self._spalte("purchasing_org")
        self.assertEqual(spec.title, "EKorg")

    def test_beide_sind_bearbeitbar(self):
        for key in ("plant", "purchasing_org"):
            _index, spec = self._spalte(key)
            self.assertTrue(spec.editable, f"{key} muss editierbar sein")

    def test_werk_laesst_sich_eintippen(self):
        from app.gui.offer_table import OfferTableModel

        angebot = Offer()
        angebot.positions.append(OfferPosition(position_number="10"))
        modell = OfferTableModel()
        modell.set_offer(angebot)

        spalte, _spec = self._spalte("plant")
        index = modell.index(0, spalte)
        self.assertTrue(modell.setData(index, "2000", Qt.ItemDataRole.EditRole))
        self.assertEqual(angebot.positions[0].plant, "2000")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class KopieFuerWerkTest(unittest.TestCase):
    """Was beim Vervielfaeltigen mitgeht -- und was ausdruecklich nicht."""

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

    def _position(self):
        from datetime import datetime

        from app.models.sap_info_record import SapInfoRecord

        position = OfferPosition(position_number="10",
                                 material_number="4711001",
                                 description="Dichtring NBR")
        position.price = Decimal("2.95")
        position.quantity = Decimal("500")
        position.plant = "1000"
        satz = SapInfoRecord()
        satz.net_price = Decimal("2.50")
        satz.read_at = datetime.now()
        position.sap_info_record = satz
        position.status = PositionStatus.DONE
        position.result_text = "Infosatz 5300001234 angelegt"
        return position

    def test_angebotsdaten_gehen_mit(self):
        kopie = self.window._kopie_fuer_werk(self._position(), "2000")
        self.assertEqual(kopie.material_number, "4711001")
        self.assertEqual(kopie.price, Decimal("2.95"))
        self.assertEqual(kopie.quantity, Decimal("500"))

    def test_werk_wird_gesetzt(self):
        kopie = self.window._kopie_fuer_werk(self._position(), "2000")
        self.assertEqual(kopie.plant, "2000")
        self.assertEqual(kopie.origin("plant"), FieldOrigin.MANUAL)

    def test_sap_daten_gehen_NICHT_mit(self):
        """Der gelesene Infosatz gehoert zum Ursprungswerk."""
        kopie = self.window._kopie_fuer_werk(self._position(), "2000")
        self.assertIsNone(kopie.sap_info_record,
                          "Der Preis aus Werk 1000 darf nicht als "
                          "'alter Preis' von Werk 2000 erscheinen")

    def test_ergebnis_geht_NICHT_mit(self):
        """Sonst stuende an der neuen Zeile eine Infosatznummer, die zu
        einem anderen Werk gehoert."""
        kopie = self.window._kopie_fuer_werk(self._position(), "2000")
        self.assertNotEqual(kopie.status, PositionStatus.DONE,
                            "Die Kopie ist nicht verarbeitet worden")
        self.assertEqual(kopie.result_text, "")

    def test_eigene_kennung(self):
        original = self._position()
        kopie = self.window._kopie_fuer_werk(original, "2000")
        self.assertNotEqual(kopie.uid, original.uid,
                            "Zwei Zeilen mit derselben Kennung waeren "
                            "spaeter nicht auseinanderzuhalten")

    def test_original_bleibt_unberuehrt(self):
        original = self._position()
        self.window._kopie_fuer_werk(original, "2000")
        self.assertEqual(original.plant, "1000")
        self.assertIsNotNone(original.sap_info_record)

    def test_vermerk_erklaert_die_herkunft(self):
        kopie = self.window._kopie_fuer_werk(self._position(), "2000")
        vermerk = " ".join(kopie.confidence_reasons)
        self.assertIn("2000", vermerk)
        self.assertIn("Werk", vermerk)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class ZielpositionenTest(unittest.TestCase):
    """Ohne Markierung sind die angehakten Positionen gemeint."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ohne_markierung_zaehlen_die_haken(self):
        from app.bootstrap import build_services
        from app.config.settings import Settings
        from app.gui.main_window import MainWindow

        settings = Settings()
        settings.use_mock_sap = True
        settings.dry_run = True
        settings.ensure_dirs()
        services = build_services(settings)
        fenster = MainWindow(settings, services.as_dict())
        try:
            angebot = Offer()
            for nummer, angehakt in (("10", True), ("20", False), ("30", True)):
                position = OfferPosition(position_number=nummer)
                position.price = Decimal("1.00")
                position.selected = angehakt
                angebot.positions.append(position)
            fenster.offer = angebot
            fenster.table_model.set_offer(angebot)

            ziel = fenster._zielpositionen()
            self.assertEqual([p.position_number for p in ziel], ["10", "30"])
        finally:
            fenster.close()
            if services.repository is not None:
                services.repository.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
