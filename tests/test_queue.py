"""Tests des Arbeitsvorrats (mehrere Angebote nacheinander).

Wichtig ist vor allem das Verhalten in den unangenehmen Faellen: Eine nicht
lesbare Datei darf den Stapel nicht anhalten, ein uebersprungenes Angebot darf
nicht als verarbeitet gelten, und ein bereits gelesenes Angebot darf beim
Zurueckblaettern nicht erneut eingelesen werden -- sonst waeren die
Korrekturen des Anwenders weg.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_queue_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.models.enums import ResultState                    # noqa: E402
from app.models.offer import Offer                          # noqa: E402
from app.models.offer_position import OfferPosition         # noqa: E402
from app.models.results import BatchSummary, PositionResult  # noqa: E402
from app.services.queue_service import (                    # noqa: E402
    OfferQueue,
    QueueEntry,
    QueueState,
)

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def _angebot(name: str, positionen: int = 2) -> Offer:
    offer = Offer(vendor_name=name, offer_number=f"ANG-{name}")
    for index in range(positionen):
        offer.positions.append(OfferPosition(material_number=f"4711000{index}"))
    return offer


def _ergebnis(erfolgreich: int = 2, fehlgeschlagen: int = 0) -> BatchSummary:
    summary = BatchSummary()
    for _ in range(erfolgreich):
        ergebnis = PositionResult(position_uid=0, label="ok")
        from app.models.results import ActionResult
        ergebnis.actions.append(ActionResult("info_record", ResultState.SUCCESS, "ok"))
        summary.results.append(ergebnis)
    for _ in range(fehlgeschlagen):
        ergebnis = PositionResult(position_uid=0, label="fehler")
        from app.models.results import ActionResult
        ergebnis.actions.append(ActionResult("info_record", ResultState.FAILED, "nein"))
        summary.results.append(ergebnis)
    return summary


class ArbeitsvorratTest(unittest.TestCase):
    """Grundverhalten der Liste."""

    def setUp(self) -> None:
        self.queue = OfferQueue()

    def test_leer_am_anfang(self) -> None:
        self.assertTrue(self.queue.is_empty)
        self.assertEqual(self.queue.total, 0)
        self.assertFalse(self.queue.is_finished)
        self.assertEqual(self.queue.summary_line(), "")

    def test_dateien_aufnehmen(self) -> None:
        neu = self.queue.add_paths(["a.pdf", "b.xlsx", "c.eml"])
        self.assertEqual(neu, 3)
        self.assertEqual(self.queue.total, 3)
        self.assertEqual(self.queue.pending, 3)

    def test_doppelte_werden_nicht_aufgenommen(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        neu = self.queue.add_paths(["b.pdf", "c.pdf"])
        self.assertEqual(neu, 1)
        self.assertEqual(self.queue.total, 3)

    def test_naechstes_offenes(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.assertEqual(self.queue.next_pending_index(), 0)
        self.queue.mark_skipped(0)
        self.assertEqual(self.queue.next_pending_index(), 1)
        self.queue.mark_skipped(1)
        self.assertEqual(self.queue.next_pending_index(), -1)

    def test_auswahl(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        eintrag = self.queue.select(1)
        self.assertIsNotNone(eintrag)
        self.assertEqual(self.queue.current_index, 1)
        self.assertIs(self.queue.current, eintrag)

    def test_ungueltige_auswahl(self) -> None:
        self.queue.add_paths(["a.pdf"])
        self.assertIsNone(self.queue.select(5))
        self.assertIsNone(self.queue.select(-1))

    def test_entfernen_verschiebt_den_zeiger(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf", "c.pdf"])
        self.queue.select(2)
        self.queue.remove(0)
        self.assertEqual(self.queue.current_index, 1)
        self.assertEqual(self.queue.entries[1].name, "c.pdf")

    def test_entfernen_des_aktuellen(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.queue.select(1)
        self.queue.remove(1)
        self.assertEqual(self.queue.current_index, -1)

    def test_leeren(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.queue.select(0)
        self.queue.clear()
        self.assertTrue(self.queue.is_empty)
        self.assertEqual(self.queue.current_index, -1)


class StandTest(unittest.TestCase):
    """Fortschreiben des Bearbeitungsstands."""

    def setUp(self) -> None:
        self.queue = OfferQueue()
        self.queue.add_paths(["a.pdf", "b.pdf", "c.pdf"])

    def test_geladen(self) -> None:
        self.queue.mark_loaded(0, _angebot("Muster", 3))
        eintrag = self.queue.entries[0]
        self.assertIs(eintrag.state, QueueState.LOADED)
        self.assertEqual(eintrag.positions, 3)
        self.assertIn("3 Position", eintrag.result_text())

    def test_verarbeitet(self) -> None:
        self.queue.mark_loaded(0, _angebot("Muster"))
        self.queue.mark_processed(0, _ergebnis(2, 1))
        eintrag = self.queue.entries[0]
        self.assertIs(eintrag.state, QueueState.PROCESSED)
        self.assertTrue(eintrag.state.is_done)
        self.assertIsNotNone(eintrag.finished_at)
        self.assertIn("2 erfolgreich", eintrag.result_text())
        self.assertIn("1 fehlgeschlagen", eintrag.result_text())

    def test_nicht_lesbar_haelt_den_stapel_nicht_an(self) -> None:
        """Der wichtigste Fall: eine kaputte Datei blockiert nicht."""
        self.queue.mark_import_failed(0, "Die Datei ist beschaedigt.")
        eintrag = self.queue.entries[0]
        self.assertIs(eintrag.state, QueueState.FAILED_IMPORT)
        self.assertTrue(eintrag.state.is_done)
        self.assertEqual(self.queue.next_pending_index(), 1)
        self.assertIn("beschaedigt", eintrag.result_text())

    def test_uebersprungen_gilt_nicht_als_verarbeitet(self) -> None:
        self.queue.mark_skipped(0, "vom Anwender uebersprungen")
        eintrag = self.queue.entries[0]
        self.assertIs(eintrag.state, QueueState.SKIPPED)
        self.assertIsNone(eintrag.summary)
        self.assertNotIn("erfolgreich", eintrag.result_text())

    def test_geladenes_angebot_bleibt_erhalten(self) -> None:
        """Zurueckblaettern darf die Korrekturen des Anwenders nicht verwerfen."""
        angebot = _angebot("Muster")
        angebot.positions[0].description = "vom Anwender korrigiert"
        self.queue.mark_loaded(0, angebot)
        self.queue.select(1)
        wieder = self.queue.select(0)
        self.assertIsNotNone(wieder.offer)
        self.assertEqual(wieder.offer.positions[0].description,
                         "vom Anwender korrigiert")

    def test_zaehler(self) -> None:
        self.queue.mark_processed(0, _ergebnis())
        self.queue.mark_import_failed(1, "kaputt")
        self.assertEqual(self.queue.done, 2)
        self.assertEqual(self.queue.pending, 1)
        self.assertFalse(self.queue.is_finished)
        self.queue.mark_skipped(2)
        self.assertTrue(self.queue.is_finished)

    def test_ungueltiger_index_wird_ignoriert(self) -> None:
        self.queue.mark_processed(99, _ergebnis())
        self.queue.mark_import_failed(-1, "x")
        self.queue.mark_skipped(42)
        self.assertEqual(self.queue.done, 0)


class ZusammenfassungTest(unittest.TestCase):
    """Texte fuer Statusleiste und Abschlussmeldung."""

    def setUp(self) -> None:
        self.queue = OfferQueue()
        self.queue.add_paths(["a.pdf", "b.pdf", "c.pdf", "d.pdf"])

    def test_statuszeile(self) -> None:
        self.queue.mark_processed(0, _ergebnis())
        zeile = self.queue.summary_line()
        self.assertIn("1/4", zeile)
        self.assertIn("verarbeitet", zeile)
        self.assertIn("offen", zeile)

    def test_abschlussmeldung(self) -> None:
        self.queue.mark_processed(0, _ergebnis(3, 0))
        self.queue.mark_processed(1, _ergebnis(2, 1))
        self.queue.mark_import_failed(2, "kaputt")
        self.queue.mark_skipped(3)
        text = self.queue.overall_result()
        self.assertIn("2 von 4", text)
        self.assertIn("5 Position(en) erfolgreich", text)
        self.assertIn("1 Position(en) fehlgeschlagen", text)
        self.assertIn("nicht lesbar", text)
        self.assertIn("uebersprungen", text)

    def test_dateiname_statt_pfad(self) -> None:
        eintrag = QueueEntry(path=str(Path("C:/tmp/unterordner/Angebot.pdf")))
        self.assertEqual(eintrag.name, "Angebot.pdf")

    def test_zustandsbeschriftungen(self) -> None:
        for zustand in QueueState:
            self.assertTrue(zustand.label)


@unittest.skipUnless(HAS_QT, "PySide6 ist nicht installiert")
class LeisteTest(unittest.TestCase):
    """Die Leiste erscheint nur, wenn sie gebraucht wird."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from app.gui.queue_bar import QueueBar

        self.bar = QueueBar()
        self.queue = OfferQueue()

    def test_unsichtbar_bei_einem_angebot(self) -> None:
        self.queue.add_paths(["a.pdf"])
        self.bar.bind(self.queue)
        self.assertFalse(self.bar.isVisible())

    def test_sichtbar_ab_zwei_angeboten(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.bar.bind(self.queue)
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar.selector.count(), 2)

    def test_fortschritt(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf", "c.pdf"])
        self.queue.mark_processed(0, _ergebnis())
        self.bar.bind(self.queue)
        self.assertEqual(self.bar.progress.maximum(), 3)
        self.assertEqual(self.bar.progress.value(), 1)

    def test_weiter_schaltflaeche(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.bar.bind(self.queue)
        self.assertTrue(self.bar.next_button.isEnabled())
        self.queue.mark_processed(0, _ergebnis())
        self.queue.mark_processed(1, _ergebnis())
        self.bar.refresh()
        self.assertFalse(self.bar.next_button.isEnabled())
        self.assertIn("Alle bearbeitet", self.bar.next_button.text())

    def test_auswahl_sendet_signal(self) -> None:
        self.queue.add_paths(["a.pdf", "b.pdf"])
        self.bar.bind(self.queue)
        empfangen: list[int] = []
        self.bar.entrySelected.connect(empfangen.append)
        self.bar.selector.setCurrentIndex(1)
        self.assertEqual(empfangen, [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
