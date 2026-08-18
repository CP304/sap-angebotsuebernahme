"""Sonderpositionen muessen SICHTBAR sein, nicht nur unangehakt.

Der leere Haken allein genuegt nicht: In einem Angebot mit vierzig Zeilen
-- und genau solche gibt es im Bestand -- faellt eine fehlende Markierung
nicht auf.  Ausgerechnet dort ist der Fehler am teuersten: ein
Werkzeugkostenanteil, der als Materialpreis in den Infosatz wandert, faellt
erst beim naechsten Abruf auf.

Deshalb drei voneinander unabhaengige Wege, dieselbe Information zu sehen:
Farbe (schnell), Kuerzel im Text (ueberlebt Ausdruck und Farbsehschwaeche)
und ein Filter (zum gezielten Abarbeiten).

Zusaetzlich: unverstandene Kuerzel in einem Spaltenkopf.  "Menge RS" --
"Menge" ist eindeutig, "RS" nicht.  Es koennte eine Rahmenmenge statt einer
Bestellmenge sein, und das erkennt niemand am Zahlenwert.  Die Spalte wird
uebernommen, aber gelb.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_sonder_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from app.config.settings import Settings                            # noqa: E402
from app.models.enums import FieldOrigin                            # noqa: E402
from app.models.offer import Offer                                  # noqa: E402
from app.models.offer_position import OfferPosition                 # noqa: E402
from app.services.extraction.table_extractor import TableExtractor   # noqa: E402


class UnverstandenesKuerzelTest(unittest.TestCase):
    """"Menge RS" -- uebernehmen, aber gelb."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.extractor = TableExtractor(Settings())

    def _pruefe(self, kopf: str) -> str:
        feld, _konfidenz, _grund = self.extractor._match_header_text(kopf)
        return self.extractor.unexplained_qualifier(kopf, feld)

    def test_menge_rs_ist_unsicher(self):
        self.assertEqual(self._pruefe("Menge RS"), "RS")

    def test_blosse_menge_ist_sicher(self):
        self.assertEqual(self._pruefe("Menge"), "")

    def test_ausgeschriebener_zusatz_ist_harmlos(self):
        self.assertEqual(self._pruefe("Menge gesamt"), "",
                         "Ausgeschriebenes erklaert sich selbst")

    def test_bekannte_einheit_ist_harmlos(self):
        for kopf in ("Preis EUR", "Menge ST", "Preis PE", "Menge ME"):
            self.assertEqual(self._pruefe(kopf), "", kopf)

    def test_bezeichnungsspalte_bleibt_unberuehrt(self):
        """Bei einem Freitextfeld verschiebt ein Kuerzel nichts."""
        self.assertEqual(self._pruefe("Bezeichnung XY"), "")

    def test_ep_je_me_bleibt_sicher(self):
        self.assertEqual(self._pruefe("EP /ME"), "",
                         "Die Bezugsgroesse ist erklaert, nicht unbekannt")

    def test_unbekanntes_kuerzel_am_preis(self):
        # "AB" waere kein guter Testfall: "ab" ist der Alias fuer "gueltig ab".
        self.assertEqual(self._pruefe("Preis XY"), "XY")


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class TabelleZeigtDieArtTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        from app.gui.offer_table import OfferFilterProxy, OfferTableModel

        cls.app = QApplication.instance() or QApplication([])
        cls.OfferTableModel = OfferTableModel
        cls.OfferFilterProxy = OfferFilterProxy

    def _modell(self):
        angebot = Offer()
        for nummer, art, text in (
                ("10", "material", "Dichtring NBR"),
                ("20", "one_time_cost", "Werkzeugkostenanteil"),
                ("30", "alternative", "Dichtring NBR Alternativpreis"),
                ("40", "subtotal", "Zwischensumme"),
        ):
            position = OfferPosition(position_number=nummer, description=text)
            position.position_kind = art
            position.selected = (art == "material")
            angebot.positions.append(position)
        modell = self.OfferTableModel()
        modell.set_offer(angebot)
        return modell, angebot

    def _spalte(self, modell, key: str) -> int:
        from app.gui.offer_table import COLUMNS

        return next(i for i, s in enumerate(COLUMNS) if s.key == key)

    def test_beschreibung_nennt_die_art(self):
        modell, _ = self._modell()
        spalte = self._spalte(modell, "description")
        texte = [modell.data(modell.index(zeile, spalte),
                             Qt.ItemDataRole.DisplayRole)
                 for zeile in range(modell.rowCount())]
        self.assertNotIn("[", texte[0], "Materialpositionen bleiben unbeschriftet")
        self.assertIn("Einmalkosten", texte[1])
        self.assertIn("Alternativposition", texte[2])
        self.assertIn("Zwischensumme", texte[3])

    def test_beschreibungstext_bleibt_erhalten(self):
        modell, _ = self._modell()
        spalte = self._spalte(modell, "description")
        text = modell.data(modell.index(1, spalte), Qt.ItemDataRole.DisplayRole)
        self.assertIn("Werkzeugkostenanteil", text,
                      "Das Kuerzel darf den Text nicht ersetzen")

    def test_bearbeiten_zeigt_den_reinen_text(self):
        """Sonst schreibt der Anwender das Kuerzel in die Bezeichnung.

        Qt fuellt das Eingabefeld aus der EditRole.  Stuende dort "[Einmal-
        kosten] Werkzeug...", wuerde beim naechsten Speichern genau das im
        Feld landen -- und beim uebernaechsten Mal doppelt.
        """
        modell, _ = self._modell()
        spalte = self._spalte(modell, "description")
        bearbeiten = modell.data(modell.index(1, spalte),
                                 Qt.ItemDataRole.EditRole)
        self.assertEqual(bearbeiten, "Werkzeugkostenanteil")
        self.assertNotIn("[", str(bearbeiten))

    def test_sonderzeilen_sind_eingefaerbt(self):
        modell, _ = self._modell()
        spalte = self._spalte(modell, "description")
        farben = [modell.data(modell.index(zeile, spalte),
                              Qt.ItemDataRole.BackgroundRole)
                  for zeile in range(modell.rowCount())]
        self.assertIsNone(farben[0], "Materialpositionen bleiben unbunt")
        for zeile in (1, 2, 3):
            self.assertIsNotNone(farben[zeile], f"Zeile {zeile} nicht markiert")

    def test_tooltip_erklaert_die_zeile(self):
        modell, _ = self._modell()
        spalte = self._spalte(modell, "description")
        hinweis = modell.data(modell.index(1, spalte), Qt.ItemDataRole.ToolTipRole)
        self.assertIn("Einmalkosten", hinweis)
        self.assertIn("pruefen", hinweis.lower())

    def test_unsicheres_feld_bleibt_gelb(self):
        """Die neue Einfaerbung darf die Gelbmarkierung nicht verdraengen."""
        modell, angebot = self._modell()
        position = angebot.positions[1]
        position.set_field("description", position.description,
                           FieldOrigin.UNCERTAIN)
        modell.refresh_all()
        spalte = self._spalte(modell, "description")
        farbe = modell.data(modell.index(1, spalte), Qt.ItemDataRole.BackgroundRole)
        self.assertIn("fff5e0", farbe.color().name().lower(),
                      "Unsichere Werte muessen gelb bleiben, nicht blau werden")

    # -- Filter --------------------------------------------------------
    def test_filter_zeigt_nur_sonderpositionen(self):
        modell, _ = self._modell()
        proxy = self.OfferFilterProxy()
        proxy.setSourceModel(modell)
        self.assertEqual(proxy.rowCount(), 4)
        proxy.set_only_special(True)
        self.assertEqual(proxy.rowCount(), 3, "Nur die drei Sonderzeilen")
        proxy.set_only_special(False)
        self.assertEqual(proxy.rowCount(), 4)

    def test_filter_ohne_sonderpositionen(self):
        angebot = Offer()
        angebot.positions.append(OfferPosition(position_number="10",
                                               description="Dichtring"))
        modell = self.OfferTableModel()
        modell.set_offer(angebot)
        proxy = self.OfferFilterProxy()
        proxy.setSourceModel(modell)
        proxy.set_only_special(True)
        self.assertEqual(proxy.rowCount(), 0,
                         "Kein Treffer ist ein gueltiges Ergebnis, kein Fehler")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
