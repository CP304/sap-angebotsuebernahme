"""Doppelte Belege verhindern -- und dabei nicht ueber das Ziel hinausschiessen.

Warum es diesen Schutz gibt
---------------------------
SAP nimmt zu derselben Angebotsnummer beliebig viele Bestellungen an.  Wer
ein Angebot versehentlich zweimal durch das Werkzeug schickt -- weil die
Mail nochmal aufploppt, weil ein Kollege dasselbe PDF bekommen hat --, hat
zwei Bestellungen beim Lieferanten.  Das faellt oft erst beim Wareneingang
auf.

Warum aus der eigenen Historie und nicht aus SAP
------------------------------------------------
Es gaebe eine Bestelluebersicht (ME2N), die sich durchsuchen liesse.  Dafuer
braeuchte es aber Feld-IDs, die hier nicht geprueft sind -- und ein geratener
Belegbezug waere schlimmer als eine fehlende Warnung.  Die eigene Historie
weiss dagegen sicher, was dieses Werkzeug geschrieben hat.  Ihre Grenze ist
ehrlich benannt: von Hand in SAP angelegte Bestellungen sieht sie nicht.

Was hier gepruegt wird, ist vor allem das Gegenteil des Schutzes: dass er
in genau den Faellen *schweigt*, in denen eine Warnung falsch waere.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TEMP_HOME = tempfile.mkdtemp(prefix="sap_dup_guard_")
os.environ["SAP_ANGEBOT_HOME"] = _TEMP_HOME

from app.database.repository import Repository              # noqa: E402
from app.models.enums import ResultState                    # noqa: E402
from app.models.results import ActionResult                 # noqa: E402


def _repo(name: str) -> Repository:
    pfad = Path(_TEMP_HOME) / f"{name}.sqlite3"
    if pfad.exists():
        pfad.unlink()
    return Repository(pfad)


def _eintrag(repo: Repository, *, offer_number: str = "AN-2024-0815",
             material_number: str = "47110001", action: str = "purchase_order",
             document_number: str = "4500000001",
             state: ResultState = ResultState.SUCCESS,
             mode: str = "echt", dry_run: bool = False) -> None:
    """Eine Protokollzeile schreiben; Vorgaben = der uebliche Gutfall."""
    kontext = {
        "vendor_number": "0000100234",
        "vendor_name": "Muster Dichtungstechnik GmbH",
        "material_number": material_number,
        "offer_number": offer_number,
    }
    ergebnis = ActionResult(action=action, state=state,
                            transaction="ME21N",
                            document_number=document_number,
                            message="Bestellung angelegt")
    repo.log_action("lauf-1", kontext, ergebnis, mode=mode, dry_run=dry_run)


class DoppelteBelegeErkennen(unittest.TestCase):
    """Der Normalfall: derselbe Beleg wird wiedererkannt."""

    def setUp(self) -> None:
        self.repo = _repo("erkennen")

    def tearDown(self) -> None:
        self.repo.close()

    def test_bekannte_bestellung_wird_gemeldet(self):
        _eintrag(self.repo)
        treffer = self.repo.documents_already_created("AN-2024-0815", "purchase_order")
        self.assertEqual(len(treffer), 1)
        self.assertEqual(treffer[0]["document_number"], "4500000001")

    def test_belegnummer_erscheint_nur_einmal(self):
        # Ein Beleg mit mehreren Positionen erzeugt mehrere Protokollzeilen.
        _eintrag(self.repo, material_number="47110001")
        _eintrag(self.repo, material_number="47110002")
        _eintrag(self.repo, material_number="47110003")
        treffer = self.repo.documents_already_created("AN-2024-0815", "purchase_order")
        self.assertEqual(len(treffer), 1, "Eine Bestellung, also auch nur eine Meldung")

    def test_mehrere_verschiedene_bestellungen(self):
        _eintrag(self.repo, document_number="4500000001")
        _eintrag(self.repo, document_number="4500000002")
        treffer = self.repo.documents_already_created("AN-2024-0815", "purchase_order")
        self.assertEqual({z["document_number"] for z in treffer},
                         {"4500000001", "4500000002"})

    def test_einschraenkung_auf_ein_material(self):
        _eintrag(self.repo, material_number="47110001", document_number="4500000001")
        _eintrag(self.repo, material_number="47110002", document_number="4500000002")
        treffer = self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order", material_number="47110002")
        self.assertEqual(len(treffer), 1)
        self.assertEqual(treffer[0]["document_number"], "4500000002")

    def test_kontrakte_getrennt_von_bestellungen(self):
        _eintrag(self.repo, action="contract", document_number="4600000001")
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"))
        self.assertTrue(self.repo.documents_already_created(
            "AN-2024-0815", "contract"))


class WannDerSchutzSchweigenMuss(unittest.TestCase):
    """Eine falsche Warnung ist schlimmer als keine: sie kostet Vertrauen."""

    def setUp(self) -> None:
        self.repo = _repo("schweigen")

    def tearDown(self) -> None:
        self.repo.close()

    def test_probelauf_zaehlt_nicht(self):
        _eintrag(self.repo, dry_run=True)
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"),
            "Im Probelauf entsteht in SAP nichts -- davor darf nicht gewarnt werden")

    def test_testsystem_zaehlt_nicht(self):
        _eintrag(self.repo, mode="mock")
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"),
            "Mock-Belege gibt es in SAP nicht")

    def test_fehlgeschlagener_versuch_zaehlt_nicht(self):
        _eintrag(self.repo, state=ResultState.FAILED, document_number="")
        _eintrag(self.repo, state=ResultState.SKIPPED, document_number="")
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"))

    def test_erfolg_ohne_belegnummer_zaehlt_nicht(self):
        # Ohne Nummer koennte man dem Anwender nichts Nachpruefbares nennen.
        _eintrag(self.repo, document_number="")
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"))

    def test_anderes_angebot_zaehlt_nicht(self):
        _eintrag(self.repo, offer_number="AN-2024-0815")
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0999", "purchase_order"))

    def test_ohne_angebotsnummer_keine_zuordnung(self):
        _eintrag(self.repo, offer_number="")
        self.assertFalse(self.repo.documents_already_created("", "purchase_order"),
                         "Ohne Angebotsnummer laesst sich nichts sicher zuordnen")

    def test_leere_aktion_liefert_nichts(self):
        _eintrag(self.repo)
        self.assertFalse(self.repo.documents_already_created("AN-2024-0815", ""))

    def test_leere_datenbank(self):
        self.assertFalse(self.repo.documents_already_created(
            "AN-2024-0815", "purchase_order"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
