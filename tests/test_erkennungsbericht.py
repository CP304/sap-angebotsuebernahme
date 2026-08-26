"""Der Erkennungsbericht darf nichts verraten.

Wozu es das Werkzeug gibt
-------------------------
Um die Belegerkennung zu verbessern, muss man wissen, woran sie
scheitert.  Dafuer braucht es echte Angebote -- und die duerfen das Haus
nicht verlassen.  Beides zugleich geht nur, wenn die Messung dorthin
kommt, wo die Belege liegen, und von dort ausschliesslich Kennzahlen
zurueckbringt.

Damit steht und faellt alles mit einer Zusicherung: im Bericht steht
kein Lieferant, keine Angebotsnummer, keine Materialnummer, keine
Bezeichnung, kein Preis, kein Dateiname.  Wer sich darauf verlaesst und
hereinfaellt, gibt Geschaeftsdaten heraus, ohne es zu merken.

Genau das ist beim Bauen einmal passiert: die Erkennungsnotizen der
Anwendung nennen ihre Treffer im Klartext ("Kopf erkannt -- vendor_name:
'Muster GmbH'"), und sie standen zunaechst wortwoertlich im Bericht --
unter einer Ueberschrift, die das Gegenteil versprach.  Deshalb wird
seither ueber eine Positivliste ausgewaehlt: was dort nicht steht, wird
gezaehlt, aber nicht wiedergegeben.

Dieser Test ist die Gegenprobe dazu.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_bericht_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

_WURZEL = Path(__file__).resolve().parent.parent
_BEISPIELE = _WURZEL / "sample_data" / "erzeugt"
_WERKZEUG = _WURZEL / "tools" / "erkennungsbericht.py"

_ENDUNGEN = {".pdf", ".xlsx", ".csv", ".txt", ".eml", ".docx", ".odt",
             ".ods", ".rtf"}


def _erzeuge_bericht(ziel: Path, *zusatz: str) -> str:
    umgebung = dict(os.environ, PYTHONPATH=str(_WURZEL))
    ergebnis = subprocess.run(
        [sys.executable, str(_WERKZEUG), str(_BEISPIELE), "-o", str(ziel),
         *zusatz],
        capture_output=True, text=True, env=umgebung, timeout=600)
    if ergebnis.returncode != 0:  # pragma: no cover -- soll nicht vorkommen
        raise AssertionError(f"Werkzeug fehlgeschlagen: {ergebnis.stderr}")
    return ziel.read_text(encoding="utf-8")


@unittest.skipUnless(_BEISPIELE.is_dir(), "Beispieldateien fehlen")
class BerichtVerraetNichtsTest(unittest.TestCase):
    """Die Zusicherung, auf der alles beruht."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ordner = Path(tempfile.mkdtemp(prefix="bericht_"))
        cls.bericht = _erzeuge_bericht(cls.ordner / "bericht.txt")

        from app.config.settings import Settings
        from app.services.offer_import_service import OfferImportService

        dienst = OfferImportService(Settings())
        cls.geheim: list[tuple[str, str]] = []
        for pfad in sorted(_BEISPIELE.iterdir()):
            if pfad.suffix.lower() not in _ENDUNGEN:
                continue
            angebot = dienst.import_file(pfad)
            werte = [angebot.vendor_name, angebot.offer_number]
            for position in angebot.positions:
                werte += [position.material_number, position.description,
                          str(position.price) if position.price else ""]
            for wert in werte:
                wert = (wert or "").strip()
                if len(wert) >= 5:
                    cls.geheim.append((pfad.name, wert))

    def test_kein_erkannter_wert_steht_im_bericht(self):
        """Lieferant, Angebotsnummer, Material, Bezeichnung, Preis."""
        gefunden = [(datei, wert) for datei, wert in self.geheim
                    if wert in self.bericht]
        self.assertEqual(gefunden, [], f"Im Bericht steht: {gefunden[:5]}")

    def test_kein_dateiname_steht_im_bericht(self):
        """In Dateinamen steht oft der Lieferant."""
        drin = [pfad.name for pfad in _BEISPIELE.iterdir()
                if pfad.is_file() and pfad.name in self.bericht]
        self.assertEqual(drin, [], f"Dateinamen im Bericht: {drin[:5]}")

    def test_der_bericht_sagt_selbst_was_er_enthaelt(self):
        """Wer ihn weitergibt, soll es nachlesen koennen."""
        self.assertIn("AUSSCHLIESSLICH Kennzahlen", self.bericht)
        self.assertIn("durchlesen", self.bericht)

    def test_der_bericht_ist_trotzdem_brauchbar(self):
        """Eine leere Zusicherung waere leicht zu erfuellen."""
        self.assertIn("Trefferquote je Positionsfeld", self.bericht)
        self.assertIn("Materialnummer", self.bericht)     # als Feldname
        self.assertIn("erkannte Positionen", self.bericht)
        self.assertIn("ZUSAMMENFASSUNG", self.bericht)

    def test_strukturmerkmale_stehen_drin(self):
        """Ohne sie liesse sich die Ursache nicht eingrenzen."""
        self.assertIn("Seite(n)", self.bericht)
        self.assertTrue("Linien" in self.bericht or "Rahmen" in self.bericht)
        self.assertIn("Verfahren:", self.bericht)

    def test_mit_dateinamen_warnt_im_kopf(self):
        """Der Schalter ist fuer die eigene Analyse -- das muss dastehen."""
        bericht = _erzeuge_bericht(self.ordner / "mit_namen.txt",
                                   "--mit-dateinamen")
        self.assertIn("ACHTUNG", bericht)
        self.assertIn("Dateinamen", bericht)


class WerkzeugRandfaelleTest(unittest.TestCase):
    """Was passiert, wenn der Ordner nicht das ist, was er sein soll."""

    def _lauf(self, *argumente: str):
        umgebung = dict(os.environ, PYTHONPATH=str(_WURZEL))
        return subprocess.run([sys.executable, str(_WERKZEUG), *argumente],
                              capture_output=True, text=True, env=umgebung,
                              timeout=120)

    def test_ordner_gibt_es_nicht(self):
        ergebnis = self._lauf("/gibt/es/nicht")
        self.assertEqual(ergebnis.returncode, 2)
        self.assertIn("Kein Ordner", ergebnis.stderr)

    def test_leerer_ordner(self):
        leer = tempfile.mkdtemp(prefix="leer_")
        ergebnis = self._lauf(leer)
        self.assertEqual(ergebnis.returncode, 1)
        self.assertIn("Keine lesbaren Angebote", ergebnis.stderr)

    def test_unlesbare_datei_bricht_den_lauf_nicht_ab(self):
        """Eine kaputte Datei darf die Messung nicht beenden."""
        ordner = Path(tempfile.mkdtemp(prefix="kaputt_"))
        (ordner / "kaputt.pdf").write_bytes(b"das ist kein PDF")
        (ordner / "leer.csv").write_bytes(b"")
        ziel = ordner / "bericht.txt"
        ergebnis = self._lauf(str(ordner), "-o", str(ziel))
        self.assertEqual(ergebnis.returncode, 0, ergebnis.stderr)
        self.assertTrue(ziel.exists())
        self.assertIn("ZUSAMMENFASSUNG", ziel.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
