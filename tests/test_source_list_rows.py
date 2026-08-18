"""Orderbuch: welche Zeile wird gepflegt, wenn es mehrere gibt?

Der Fall aus der Praxis
-----------------------
Ein Lieferant steht im Orderbuch selten nur einmal.  Ueber die Jahre
sammeln sich Zeilen mit unterschiedlichen Gueltigkeitszeitraeumen an --
2022 bis 2023, dann 2024 bis 2025, und so weiter.

Wer blind die erste Zeile mit passender Lieferantennummer nimmt, pflegt
womoeglich einen historischen Zeitraum und wundert sich, warum die
Disposition den Lieferanten weiterhin nicht zieht: die heute gueltige
Zeile ist naemlich unangetastet geblieben.  Genau dieser Fehler war hier
angelegt -- die Anzeige waehlte gueltigkeitsbewusst aus, der Schreibweg
nicht.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_sl_rows_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.config.settings import Settings                        # noqa: E402
from app.models.offer_position import OfferPosition             # noqa: E402
from app.models.sap_source_list import SourceListEntry          # noqa: E402
from app.sap.source_list_service import SapSourceListService    # noqa: E402
from app.sap.interfaces import WriteContext                     # noqa: E402

HEUTE = date(2026, 8, 18)


def _dienst() -> SapSourceListService:
    """Nur die Auswahllogik pruefen -- ohne SAP-Verbindung."""
    return SapSourceListService.__new__(SapSourceListService)


def _zeile(nummer: str, von: date | None, bis: date | None, index: int) -> SourceListEntry:
    return SourceListEntry(vendor_number=nummer, plant="1000", purchasing_org="1000",
                           valid_from=von, valid_to=bis, row_index=index)


def _position(nummer: str = "0000100234") -> OfferPosition:
    return OfferPosition(material_number="47110001", vendor_number=nummer,
                         purchasing_org="1000", plant="1000")


def _kontext(stichtag: date | None = HEUTE) -> WriteContext:
    return WriteContext(dry_run=False, settings=Settings(),
                        valid_from=stichtag, valid_to=date(2099, 12, 31))


class ZeileAuswaehlen(unittest.TestCase):

    def test_einzige_gueltige_zeile(self):
        zeilen = [_zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 0)]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertIsNotNone(treffer)
        self.assertEqual(treffer.row_index, 0)

    def test_gueltige_schlaegt_abgelaufene(self):
        zeilen = [
            _zeile("0000100234", date(2022, 1, 1), date(2023, 12, 31), 0),  # alt
            _zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 1),  # gueltig
        ]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertEqual(treffer.row_index, 1,
                         "Die historische Zeile darf nicht ueberschrieben werden")

    def test_reihenfolge_egal(self):
        # Dieselbe Lage, nur andersherum sortiert -- das Ergebnis muss gleich sein.
        zeilen = [
            _zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 0),
            _zeile("0000100234", date(2022, 1, 1), date(2023, 12, 31), 1),
        ]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertEqual(treffer.row_index, 0)

    def test_nur_abgelaufene_juengste_wird_verlaengert(self):
        zeilen = [
            _zeile("0000100234", date(2020, 1, 1), date(2021, 12, 31), 0),
            _zeile("0000100234", date(2022, 1, 1), date(2023, 12, 31), 1),  # juengste
            _zeile("0000100234", date(2018, 1, 1), date(2019, 12, 31), 2),
        ]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertEqual(treffer.row_index, 1)

    def test_kuenftige_zeile_gilt_heute_nicht(self):
        # Ab 2028 gueltig -- fuer einen Preis ab heute hilft das nicht.
        zeilen = [_zeile("0000100234", date(2028, 1, 1), date(2029, 12, 31), 0)]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertEqual(treffer.row_index, 0,
                         "Es gibt keine gueltige Zeile, also wird die vorhandene gepflegt")

    def test_anderer_lieferant_bekommt_neue_zeile(self):
        zeilen = [_zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 0)]
        treffer = _dienst()._row_for_vendor(zeilen, _position("0000100987"), _kontext())
        self.assertIsNone(treffer, "Ein anderer Lieferant braucht eine eigene Zeile")

    def test_leeres_orderbuch(self):
        self.assertIsNone(_dienst()._row_for_vendor([], _position(), _kontext()))

    def test_fuehrende_nullen_egal(self):
        # SAP zeigt mal "100234", mal "0000100234" -- beides ist derselbe Lieferant.
        zeilen = [_zeile("100234", date(2026, 1, 1), date(2027, 12, 31), 3)]
        treffer = _dienst()._row_for_vendor(zeilen, _position("0000100234"), _kontext())
        self.assertEqual(treffer.row_index, 3)

    def test_offene_gueltigkeit_zaehlt_als_gueltig(self):
        zeilen = [_zeile("0000100234", None, None, 0)]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext())
        self.assertEqual(treffer.row_index, 0)

    def test_stichtag_kommt_aus_dem_kontext(self):
        """Gilt der Preis erst ab 2028, zaehlt die dann gueltige Zeile."""
        zeilen = [
            _zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 0),
            _zeile("0000100234", date(2028, 1, 1), date(2029, 12, 31), 1),
        ]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext(date(2028, 6, 1)))
        self.assertEqual(treffer.row_index, 1)

    def test_ohne_stichtag_gilt_heute(self):
        zeilen = [
            _zeile("0000100234", date(2020, 1, 1), date(2021, 12, 31), 0),
            _zeile("0000100234", date(2026, 1, 1), date(2027, 12, 31), 1),
        ]
        treffer = _dienst()._row_for_vendor(zeilen, _position(), _kontext(None))
        self.assertEqual(treffer.row_index, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
