"""Selbstdiagnose: laeuft auf dem Arbeits-PC alles, was laufen soll?

Warum es diese Pruefung gibt
----------------------------
Die Anwendung laeuft am Ende auf einem Rechner ohne Internet und ohne
direkten Support.  Wenn dort die Texterkennung fehlt oder ein optionales
Paket nicht installiert ist, aeussert sich das als "erkennt nichts" -- und
niemand kann von aussen nachsehen.  Diese Diagnose beantwortet die Frage
"ist meine Installation vollstaendig?" direkt in der Anwendung, ohne
Konsole und ohne fremde Hilfe.

Der Dienst ist bewusst GUI-frei: die Verwaltungsseite zeigt nur an, was
hier ermittelt wird, und der Bericht laesst sich als Text kopieren --
z. B. um ihn per Mail zu schicken, wenn doch einmal Hilfe noetig ist.
"""

from __future__ import annotations

import importlib
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["CheckResult", "SelfCheck", "OK", "WARN", "FAIL"]

OK = "ok"        #: funktioniert
WARN = "warn"    #: funktioniert eingeschraenkt / optionales Extra fehlt
FAIL = "fail"    #: Kernfunktion betroffen

_STATUS_LABEL = {OK: "OK", WARN: "Hinweis", FAIL: "Problem"}


@dataclass
class CheckResult:
    """Ein einzelner Befund der Selbstdiagnose."""

    name: str
    status: str
    text: str
    detail: str = ""

    @property
    def status_label(self) -> str:
        return _STATUS_LABEL.get(self.status, self.status)

    def display(self) -> str:
        zeile = f"[{self.status_label:>7}] {self.name}: {self.text}"
        if self.detail:
            eingerueckt = "\n".join(f"          {z}" for z in self.detail.splitlines())
            zeile += f"\n{eingerueckt}"
        return zeile


#: Pflichtpakete: ohne sie startet die Anwendung nicht oder verliert
#: Kernfunktionen.  (name, Einfuhrname, wofuer)
_PFLICHT = (
    ("PySide6", "PySide6", "Oberflaeche"),
    ("pandas", "pandas", "Tabellenverarbeitung"),
    ("openpyxl", "openpyxl", "Excel-Dateien (.xlsx)"),
    ("PyMuPDF", "fitz", "PDF-Angebote"),
)

#: Optionale Pakete: ihr Fehlen schraenkt ein, bricht aber nichts.
_OPTIONAL = (
    ("pywin32", "win32com.client", "SAP GUI Scripting (nur Echtbetrieb)",
     "pip install pywin32"),
    ("odfpy", "odf.opendocument", "OpenDocument-Dateien (.ods/.odt)",
     "pip install odfpy"),
    ("python-docx", "docx", "Word-Angebote (.docx)",
     "pip install python-docx"),
    ("extract-msg", "extract_msg", "Outlook-Mails (.msg); ohne das Paket "
     "greift der eingebaute Leser", ""),
)


class SelfCheck:
    """Fuehrt alle Pruefungen aus.  Jede Pruefung faengt ihre Fehler selbst."""

    def __init__(self, settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    def run_all(self) -> list[CheckResult]:
        ergebnisse: list[CheckResult] = [self._python()]
        ergebnisse.extend(self._pakete())
        ergebnisse.append(self._ocr())
        ergebnisse.append(self._datenbank())
        ergebnisse.append(self._beispieldateien())
        ergebnisse.append(self._sap_modus())
        return ergebnisse

    def report_text(self, ergebnisse: list[CheckResult] | None = None) -> str:
        """Kopierbarer Gesamtbericht -- fuer eine Mail an den Support."""
        ergebnisse = ergebnisse if ergebnisse is not None else self.run_all()
        kopf = [
            "Selbstdiagnose SAP-Angebotsuebernahme",
            f"Python {sys.version.split()[0]} auf {platform.platform()}",
            "-" * 60,
        ]
        return "\n".join(kopf + [e.display() for e in ergebnisse])

    # ------------------------------------------------------------------
    # Einzelpruefungen
    # ------------------------------------------------------------------
    def _python(self) -> CheckResult:
        version = sys.version_info
        if version >= (3, 12):
            return CheckResult("Python", OK,
                               f"{version.major}.{version.minor}.{version.micro}")
        return CheckResult("Python", FAIL,
                           f"{version.major}.{version.minor} ist zu alt -- "
                           "benoetigt wird 3.12 oder neuer.")

    def _pakete(self) -> list[CheckResult]:
        ergebnisse: list[CheckResult] = []
        for name, modul, zweck in _PFLICHT:
            if self._importierbar(modul):
                ergebnisse.append(CheckResult(name, OK, zweck))
            else:
                ergebnisse.append(CheckResult(
                    name, FAIL,
                    f"fehlt -- {zweck} funktioniert nicht.",
                    f"Abhilfe: pip install {name}"))
        for name, modul, zweck, abhilfe in _OPTIONAL:
            if self._importierbar(modul):
                ergebnisse.append(CheckResult(name, OK, zweck))
            else:
                ergebnisse.append(CheckResult(
                    name, WARN, f"nicht installiert -- {zweck}.",
                    f"Abhilfe: {abhilfe}" if abhilfe else ""))
        return ergebnisse

    @staticmethod
    def _importierbar(modul: str) -> bool:
        try:
            importlib.import_module(modul)
            return True
        except Exception:  # noqa: BLE001 - kaputt installiert == fehlt
            return False

    def _ocr(self) -> CheckResult:
        """Texterkennung: der haeufigste Grund fuer 'erkennt nichts'."""
        try:
            from .ocr import available_backends, ocr_status_text

            text = ocr_status_text(self.settings)
            status = OK if available_backends(self.settings) else WARN
            return CheckResult("Texterkennung (OCR)", status,
                               text.splitlines()[0],
                               "\n".join(text.splitlines()[1:]))
        except Exception as exc:  # noqa: BLE001
            return CheckResult("Texterkennung (OCR)", WARN,
                               f"Status nicht ermittelbar ({exc}).")

    def _datenbank(self) -> CheckResult:
        try:
            db = Path(self.settings.db_file)
            if not db.parent.exists():
                return CheckResult("Datenbank", FAIL,
                                   f"Ordner fehlt: {db.parent}")
            probe = db.parent / ".schreibprobe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            groesse = db.stat().st_size if db.exists() else 0
            return CheckResult("Datenbank", OK,
                               f"{db.name} ({groesse // 1024} KB), Ordner beschreibbar.")
        except Exception as exc:  # noqa: BLE001
            return CheckResult("Datenbank", FAIL,
                               f"Nicht beschreibbar: {exc}",
                               "Historie und Anlernen koennen nicht gespeichert werden.")

    def _beispieldateien(self) -> CheckResult:
        ordner = Path(__file__).resolve().parents[2] / "sample_data" / "erzeugt"
        if not ordner.is_dir():
            return CheckResult(
                "Beispieldateien", WARN,
                "Ordner sample_data/erzeugt fehlt -- der Selbsttest hat nichts "
                "zum Lesen.",
                "Abhilfe: python sample_data/erzeuge_beispiele.py ausfuehren.")
        anzahl = sum(1 for p in ordner.iterdir() if p.is_file())
        if anzahl == 0:
            return CheckResult("Beispieldateien", WARN, "Ordner ist leer.")
        return CheckResult("Beispieldateien", OK, f"{anzahl} Datei(en) vorhanden.")

    def _sap_modus(self) -> CheckResult:
        if getattr(self.settings, "use_mock_sap", True):
            return CheckResult("SAP-Modus", OK,
                               "Testsystem (Mock) -- es wird nichts nach SAP "
                               "geschrieben.")
        if self._importierbar("win32com.client"):
            return CheckResult("SAP-Modus", OK, "Echtbetrieb, pywin32 vorhanden.")
        return CheckResult("SAP-Modus", FAIL,
                           "Echtbetrieb eingestellt, aber pywin32 fehlt.",
                           "Abhilfe: pip install pywin32")

    # ------------------------------------------------------------------
    # Selbsttest: liest die eingebauten Beispieldateien wirklich ein
    # ------------------------------------------------------------------
    def run_import_selftest(self) -> list[CheckResult]:
        """Jede Beispieldatei importieren und Positionen zaehlen.

        Das ist die einzige Pruefung, die den kompletten Leseweg wirklich
        durchlaeuft.  Null Positionen bei einer Beispieldatei bedeutet: die
        Installation ist beschaedigt -- nicht das Angebot des Anwenders.
        """
        ordner = Path(__file__).resolve().parents[2] / "sample_data" / "erzeugt"
        if not ordner.is_dir():
            return [CheckResult("Selbsttest", WARN,
                                "Keine Beispieldateien vorhanden.",
                                "Abhilfe: python sample_data/erzeuge_beispiele.py")]

        from .offer_import_service import OfferImportService

        dienst = OfferImportService(self.settings)
        ergebnisse: list[CheckResult] = []
        for datei in sorted(p for p in ordner.iterdir() if p.is_file()):
            try:
                angebot = dienst.import_file(str(datei))
            except Exception as exc:  # noqa: BLE001
                ergebnisse.append(CheckResult(datei.name, FAIL,
                                              f"Import bricht ab: {exc}"))
                continue
            anzahl = len(angebot.positions)
            if anzahl:
                ergebnisse.append(CheckResult(datei.name, OK,
                                              f"{anzahl} Position(en) erkannt."))
            else:
                ergebnisse.append(CheckResult(
                    datei.name, FAIL, "0 Positionen -- Leseweg beschaedigt.",
                    "\n".join(angebot.extraction_notes[:3])))
        return ergebnisse
