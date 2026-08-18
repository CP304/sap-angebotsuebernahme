"""VBS-Aufzeichnung parsen und Feld-IDs extrahieren."""

from __future__ import annotations

import unittest

from app.services.vbs_parser import parse_vbs_recording


class VbsParserTest(unittest.TestCase):
    """Parsing von SAP GUI Scripting .vbs-Aufzeichnungen."""

    def test_keine_zeilen(self):
        self.assertEqual(parse_vbs_recording(""), [])

    def test_single_findbyid(self):
        vbs = 'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"'
        fields = parse_vbs_recording(vbs)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].field_id, "wnd[0]/usr/ctxtEINA-LIFNR")
        self.assertEqual(fields[0].value, "100234")
        self.assertEqual(fields[0].field_type, "text")

    def test_multiple_fields(self):
        vbs = """
            session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
            session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
            session.findById("wnd[0]/usr/ctxtEIKA-EKORG").text = "1000"
        """
        fields = parse_vbs_recording(vbs)
        self.assertEqual(len(fields), 3)

    def test_value_property(self):
        vbs = 'session.findById("wnd[0]/usr/ctxtEIKA-NETPR").value = "12.95"'
        fields = parse_vbs_recording(vbs)
        self.assertEqual(fields[0].value, "12.95")
        self.assertEqual(fields[0].field_type, "value")

    def test_short_id(self):
        vbs = 'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"'
        fields = parse_vbs_recording(vbs)
        self.assertEqual(fields[0].short_id(), "EINA-LIFNR")

    def test_looks_like_number(self):
        vbs1 = 'session.findById("f1").text = "100234"'
        vbs2 = 'session.findById("f2").text = "Text hier"'
        fields1 = parse_vbs_recording(vbs1)
        fields2 = parse_vbs_recording(vbs2)
        self.assertTrue(fields1[0].looks_like_number())
        self.assertFalse(fields2[0].looks_like_number())

    def test_looks_like_currency(self):
        vbs1 = 'session.findById("f1").text = "12,95"'
        vbs2 = 'session.findById("f2").text = "12.95"'
        vbs3 = 'session.findById("f3").text = "100"'
        for vbs, expected in ((vbs1, True), (vbs2, True), (vbs3, False)):
            fields = parse_vbs_recording(vbs)
            self.assertEqual(fields[0].looks_like_currency(), expected, f"Failed for {vbs}")

    def test_kommentar_und_leerhaende(self):
        """Kommentare und Leerzeilen werden ignoriert."""
        vbs = """
            ' Das ist ein Kommentar
            session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"

            ' Noch ein Kommentar
        """
        fields = parse_vbs_recording(vbs)
        self.assertEqual(len(fields), 1)

    def test_malformed_keine_anführungszeichen(self):
        """Fehlerhafte Zeilen werden ignoriert."""
        vbs = """
            session.findById(wnd[0]/usr/ctxtEINA-LIFNR).text = 100234
            session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
        """
        fields = parse_vbs_recording(vbs)
        self.assertEqual(len(fields), 1)

    def test_wert_kann_leer_sein(self):
        """Leere Werte werden verworfen (Sicherheit)."""
        vbs = 'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = ""'
        fields = parse_vbs_recording(vbs)
        self.assertEqual(len(fields), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
