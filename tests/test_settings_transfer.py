"""Einstellungen weitergeben -- ohne dabei Unerwuenschtes mitzugeben.

Wer die Einrichtung einmal gemacht hat, soll sie weitergeben koennen.
Eine Einstellungsdatei ist aber keine Kopie des Rechners, und die Tests
hier bestehen groesstenteils darauf, was NICHT mitwandern darf:

* Pfade -- beim Empfaenger zeigen sie ins Leere oder auf dessen falsche
  Datenbank.
* Die Liste zuletzt geoeffneter Dateien -- sie verraet, an welchen
  Lieferanten und Vorgaengen jemand gearbeitet hat.
* Probelauf und Testsystem -- diese beiden Schalter entscheiden, ob
  wirklich in SAP geschrieben wird.  Eine importierte Datei darf beim
  Empfaenger nicht unbemerkt den Echtbetrieb einschalten.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_transfer_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                        # noqa: E402
from app.services.settings_transfer import (                    # noqa: E402
    export_settings,
    import_settings,
)


class ExportTest(unittest.TestCase):

    def setUp(self) -> None:
        self.ziel = Path(_TEMP_HOME) / f"export_{id(self)}.json"
        self.settings = Settings()
        self.settings.purchasing.purchasing_org = "2000"
        self.settings.purchasing.plant = "3000"
        self.settings.sap_field_ids = {
            "wnd[0]/usr/ctxtEINA-LIFNR": "vendor_number"}
        self.settings.database_path = r"C:\Users\Chef\privat\meine.sqlite3"
        self.settings.log_path = r"C:\Users\Chef\logs"
        self.settings.ui.recent_files = [
            "C:/vorgaenge/Angebot_Lieferant_Mueller.pdf"]
        self.settings.dry_run = False
        self.settings.use_mock_sap = False

    def _export(self) -> tuple[str, dict]:
        export_settings(self.settings, self.ziel)
        roh = self.ziel.read_text(encoding="utf-8")
        return roh, json.loads(roh)["einstellungen"]

    # -- Was mitgehen soll ---------------------------------------------
    def test_einkaufsdaten_gehen_mit(self):
        _roh, daten = self._export()
        self.assertEqual(daten["purchasing"]["purchasing_org"], "2000")
        self.assertEqual(daten["purchasing"]["plant"], "3000")

    def test_feld_ids_gehen_mit(self):
        """Das ist der eigentliche Zweck: die muehsame Aufzeichnung teilen."""
        _roh, daten = self._export()
        self.assertIn("wnd[0]/usr/ctxtEINA-LIFNR", daten["sap_field_ids"])

    def test_erkennungseinstellungen_gehen_mit(self):
        _roh, daten = self._export()
        self.assertIn("extraction", daten)

    # -- Was NICHT mitgehen darf ---------------------------------------
    def test_kein_datenbankpfad(self):
        roh, daten = self._export()
        self.assertNotIn("database_path", daten)
        self.assertNotIn("meine.sqlite3", roh)

    def test_kein_benutzername_im_klartext(self):
        roh, _daten = self._export()
        self.assertNotIn("Chef", roh,
                         "Aus den Pfaden darf kein Benutzername durchsickern")

    def test_keine_vorgangsliste(self):
        """Sie verraet, an welchen Lieferanten jemand gearbeitet hat."""
        roh, daten = self._export()
        self.assertNotIn("recent_files", daten.get("ui", {}))
        self.assertNotIn("Mueller", roh)

    def test_keine_betriebsart(self):
        _roh, daten = self._export()
        self.assertNotIn("dry_run", daten)
        self.assertNotIn("use_mock_sap", daten)

    def test_datei_benennt_die_luecken(self):
        """Der Empfaenger soll sehen, was bewusst fehlt."""
        _roh, _daten = self._export()
        inhalt = json.loads(self.ziel.read_text(encoding="utf-8"))
        self.assertIn("dry_run", inhalt["enthaelt_nicht"])
        self.assertIn("ui.recent_files", inhalt["enthaelt_nicht"])


class ImportTest(unittest.TestCase):

    def setUp(self) -> None:
        self.datei = Path(_TEMP_HOME) / f"import_{id(self)}.json"
        absender = Settings()
        absender.purchasing.purchasing_org = "2000"
        absender.purchasing.plant = "3000"
        absender.thresholds.price_warn_percent = Decimal("15")
        absender.sap_field_ids = {"wnd[0]/usr/ctxtEINA-LIFNR": "vendor_number"}
        export_settings(absender, self.datei)

        self.empfaenger = Settings()
        self.empfaenger.dry_run = True
        self.empfaenger.use_mock_sap = True
        self.empfaenger.database_path = r"D:\eigene\daten.sqlite3"

    def test_werte_kommen_an(self):
        import_settings(self.empfaenger, self.datei)
        self.assertEqual(self.empfaenger.purchasing.purchasing_org, "2000")
        self.assertEqual(self.empfaenger.purchasing.plant, "3000")

    def test_feld_ids_kommen_an(self):
        import_settings(self.empfaenger, self.datei)
        self.assertEqual(len(self.empfaenger.sap_field_ids), 1)

    def test_dezimalwerte_bleiben_dezimal(self):
        """Sonst rechnet die Pruefung spaeter mit einem Text."""
        import_settings(self.empfaenger, self.datei)
        wert = self.empfaenger.thresholds.price_warn_percent
        self.assertIsInstance(wert, Decimal)
        self.assertEqual(wert, Decimal("15"))

    def test_eigener_datenbankpfad_bleibt(self):
        import_settings(self.empfaenger, self.datei)
        self.assertEqual(self.empfaenger.database_path,
                         r"D:\eigene\daten.sqlite3")

    def test_probelauf_bleibt_an(self):
        """Der wichtigste Test: keine Datei schaltet den Echtbetrieb ein."""
        import_settings(self.empfaenger, self.datei)
        self.assertTrue(self.empfaenger.dry_run)
        self.assertTrue(self.empfaenger.use_mock_sap)

    def test_betriebsart_auch_dann_nicht_wenn_sie_in_der_datei_steht(self):
        """Eine von Hand veraenderte Datei darf das nicht aushebeln."""
        inhalt = json.loads(self.datei.read_text(encoding="utf-8"))
        inhalt["einstellungen"]["dry_run"] = False
        inhalt["einstellungen"]["use_mock_sap"] = False
        self.datei.write_text(json.dumps(inhalt), encoding="utf-8")

        ergebnis = import_settings(self.empfaenger, self.datei)
        self.assertTrue(self.empfaenger.dry_run,
                        "Der Probelauf darf niemals per Datei abgeschaltet werden")
        self.assertTrue(self.empfaenger.use_mock_sap)
        self.assertIn("dry_run", ergebnis.skipped)

    def test_unbekanntes_wird_gemeldet_nicht_verschluckt(self):
        inhalt = json.loads(self.datei.read_text(encoding="utf-8"))
        inhalt["einstellungen"]["gibtesnicht"] = 42
        self.datei.write_text(json.dumps(inhalt), encoding="utf-8")

        ergebnis = import_settings(self.empfaenger, self.datei)
        self.assertIn("gibtesnicht", ergebnis.unknown)
        self.assertTrue(ergebnis.ok, "Unbekanntes ist kein Fehler")

    def test_fehlende_werte_bleiben_unveraendert(self):
        """Eine aeltere Datei darf nichts zuruecksetzen."""
        self.empfaenger.purchasing.purchasing_group = "999"
        inhalt = json.loads(self.datei.read_text(encoding="utf-8"))
        inhalt["einstellungen"]["purchasing"].pop("purchasing_group", None)
        self.datei.write_text(json.dumps(inhalt), encoding="utf-8")

        import_settings(self.empfaenger, self.datei)
        self.assertEqual(self.empfaenger.purchasing.purchasing_group, "999")

    # -- Fehlerfaelle ---------------------------------------------------
    def test_kaputte_datei_meldet_sich(self):
        kaputt = Path(_TEMP_HOME) / "kaputt.json"
        kaputt.write_text("{ das ist kein JSON", encoding="utf-8")
        ergebnis = import_settings(self.empfaenger, kaputt)
        self.assertFalse(ergebnis.ok)
        self.assertTrue(ergebnis.errors)

    def test_fehlende_datei_meldet_sich(self):
        ergebnis = import_settings(self.empfaenger,
                                   Path(_TEMP_HOME) / "gibtesnicht.json")
        self.assertFalse(ergebnis.ok)

    def test_fremde_json_datei_meldet_sich(self):
        fremd = Path(_TEMP_HOME) / "fremd.json"
        fremd.write_text('{"irgendwas": 1}', encoding="utf-8")
        ergebnis = import_settings(self.empfaenger, fremd)
        self.assertFalse(ergebnis.ok)
        self.assertIn("einstellungen", " ".join(ergebnis.errors))

    def test_neueres_format_wird_abgelehnt(self):
        """Lieber nichts uebernehmen als die Haelfte falsch."""
        inhalt = json.loads(self.datei.read_text(encoding="utf-8"))
        inhalt["format"] = 99
        self.datei.write_text(json.dumps(inhalt), encoding="utf-8")

        ergebnis = import_settings(self.empfaenger, self.datei)
        self.assertFalse(ergebnis.ok)
        self.assertIn("neueren", " ".join(ergebnis.errors))

    def test_unbrauchbarer_wert_bricht_nicht_alles_ab(self):
        inhalt = json.loads(self.datei.read_text(encoding="utf-8"))
        inhalt["einstellungen"]["thresholds"]["price_warn_percent"] = "viel"
        self.datei.write_text(json.dumps(inhalt), encoding="utf-8")

        ergebnis = import_settings(self.empfaenger, self.datei)
        self.assertTrue(ergebnis.errors, "Der Fehler muss gemeldet werden")
        self.assertEqual(self.empfaenger.purchasing.purchasing_org, "2000",
                         "Der Rest muss trotzdem angekommen sein")

    def test_rundlauf(self):
        """Export, Import, erneuter Export ergibt dasselbe."""
        import_settings(self.empfaenger, self.datei)
        zweite = Path(_TEMP_HOME) / "zweite.json"
        export_settings(self.empfaenger, zweite)

        a = json.loads(self.datei.read_text(encoding="utf-8"))["einstellungen"]
        b = json.loads(zweite.read_text(encoding="utf-8"))["einstellungen"]
        self.assertEqual(a, b)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
