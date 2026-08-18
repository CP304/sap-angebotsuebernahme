"""Eigene Angebote pruefen, bevor man sich auf das Werkzeug verlaesst.

Wozu
----
Die Frage "liest er meine Belege ueberhaupt?" laesst sich nicht durch
Zusicherungen beantworten, sondern nur, indem man es an den eigenen
Belegen ausprobiert -- und zwar an allen auf einmal, bevor der erste
Preis nach SAP geht.

Diese Pruefung liest einen ganzen Ordner und beantwortet je Datei:
wie viele Positionen kommen heraus, sind Preis und Menge vollstaendig,
und was hat gefehlt.  Sie schreibt nichts, weder nach SAP noch in die
Historie.

Warum das mehr wert ist als jede Zusage
---------------------------------------
Danach weiss der Anwender, woran er ist: welche Lieferanten laufen
durch, welche nicht, und wie viel Nacharbeit auf ihn zukommt.  Und die
Dateien, die durchfallen, sind genau die, gegen die nachgehaertet werden
muss -- damit ist die Liste keine Ausrede, sondern eine Aufgabenliste.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["FileResult", "StockCheck", "check_folder", "report_text"]

#: Endungen, die ueberhaupt in Frage kommen
_SAMMELN = (".pdf", ".xlsx", ".xls", ".csv", ".txt", ".docx", ".doc",
            ".ods", ".odt", ".rtf", ".eml", ".msg", ".zip", ".htm", ".html")


@dataclass
class FileResult:
    """Was bei einer Datei herauskam."""

    name: str
    positions: int = 0
    #: Positionen, bei denen sowohl Preis als auch Menge stehen
    complete: int = 0
    #: Positionen mit unsicher erkannten Werten (gelb in der Tabelle)
    uncertain: int = 0
    #: Kopffelder, die gefunden wurden (Lieferant, Nummer, Datum, Waehrung)
    header_found: int = 0
    header_total: int = 4
    clauses: int = 0
    findings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verdict(self) -> str:
        """Kurzurteil in Klartext."""
        if self.error:
            return "Fehler"
        if not self.positions:
            return "nichts erkannt"
        if self.complete == self.positions and not self.uncertain:
            return "vollstaendig"
        if self.complete:
            return "teilweise"
        return "ohne Preise"

    @property
    def ok(self) -> bool:
        return self.verdict == "vollstaendig"

    @property
    def needs_work(self) -> bool:
        """Datei, gegen die nachgehaertet werden sollte."""
        return self.verdict in ("nichts erkannt", "Fehler", "ohne Preise")


@dataclass
class StockCheck:
    """Ergebnis ueber einen ganzen Ordner."""

    results: list[FileResult] = field(default_factory=list)
    folder: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def positions(self) -> int:
        return sum(r.positions for r in self.results)

    def count(self, verdict: str) -> int:
        return sum(1 for r in self.results if r.verdict == verdict)

    @property
    def problem_files(self) -> list[FileResult]:
        return [r for r in self.results if r.needs_work]

    def summary(self) -> str:
        if not self.results:
            return "Keine lesbaren Dateien gefunden."
        gut = self.count("vollstaendig")
        return (f"{self.total} Datei(en), {self.positions} Position(en). "
                f"{gut} vollstaendig, {len(self.problem_files)} mit Nachholbedarf.")


def _sammle(folder: Path, recursive: bool) -> list[Path]:
    muster = "**/*" if recursive else "*"
    dateien = [p for p in sorted(folder.glob(muster))
               if p.is_file() and p.suffix.lower() in _SAMMELN]
    return dateien


def check_folder(settings, folder: str | Path, recursive: bool = False,
                 limit: int = 200, progress=None) -> StockCheck:
    """Einen Ordner mit Angeboten durchpruefen.

    ``progress`` wird, falls angegeben, mit (fertig, gesamt, dateiname)
    aufgerufen -- damit die Oberflaeche nicht einfriert wirkt.
    """
    from .offer_import_service import OfferImportService

    ordner = Path(folder)
    ergebnis = StockCheck(folder=str(ordner))
    if not ordner.is_dir():
        logger.warning("Kein Ordner: %s", ordner)
        return ergebnis

    dateien = _sammle(ordner, recursive)[:limit]
    dienst = OfferImportService(settings)

    for nummer, pfad in enumerate(dateien, start=1):
        if progress is not None:
            progress(nummer, len(dateien), pfad.name)
        eintrag = FileResult(name=pfad.name)
        try:
            angebot = dienst.import_file(str(pfad))
        except Exception as fehler:  # noqa: BLE001
            # Eine kaputte Datei darf die Pruefung der uebrigen nicht
            # abbrechen -- gerade sie ist ja der interessante Fall.
            eintrag.error = str(fehler)[:200]
            ergebnis.results.append(eintrag)
            logger.exception("Pruefung fehlgeschlagen: %s", pfad.name)
            continue

        eintrag.positions = len(angebot.positions)
        for position in angebot.positions:
            if position.price is not None and position.quantity is not None:
                eintrag.complete += 1
            if _hat_unsicheres(position):
                eintrag.uncertain += 1

        eintrag.header_found = sum(1 for wert in (
            angebot.vendor_name, angebot.offer_number,
            angebot.offer_date, angebot.currency) if wert)
        eintrag.clauses = len(getattr(angebot, "clauses", []) or [])
        eintrag.findings = [str(befund.message)[:160]
                            for befund in angebot.issues][:4]
        ergebnis.results.append(eintrag)

    logger.info("Bestandspruefung: %s", ergebnis.summary())
    return ergebnis


def _hat_unsicheres(position) -> bool:
    from ..models.enums import FieldOrigin

    herkunft = getattr(position, "field_origins", {}) or {}
    return any(wert is FieldOrigin.UNCERTAIN for wert in herkunft.values())


def report_text(check: StockCheck) -> str:
    """Bericht zum Mitlesen und Weitergeben."""
    zeilen = [
        "Pruefung eigener Angebote",
        "=" * 72,
        f"Ordner: {check.folder}",
        check.summary(),
        "",
    ]
    if not check.results:
        zeilen.append("In diesem Ordner wurde keine lesbare Datei gefunden.")
        return "\n".join(zeilen)

    zeilen.append(f"{'Datei':<44}{'Pos.':>5}{'davon vollst.':>14}  Urteil")
    zeilen.append("-" * 82)
    for eintrag in check.results:
        name = eintrag.name if len(eintrag.name) <= 43 else eintrag.name[:40] + "..."
        zeilen.append(f"{name:<44}{eintrag.positions:>5}{eintrag.complete:>14}  "
                      f"{eintrag.verdict}")

    problem = check.problem_files
    if problem:
        zeilen += ["", "=" * 72,
                   "Diese Dateien brauchen Nacharbeit:", ""]
        for eintrag in problem:
            zeilen.append(f"  {eintrag.name} -- {eintrag.verdict}")
            if eintrag.error:
                zeilen.append(f"      Fehler: {eintrag.error}")
            for befund in eintrag.findings:
                zeilen.append(f"      {befund}")
        zeilen += [
            "",
            "Diese Dateien sind die wertvollste Grundlage fuer die naechste",
            "Verbesserung. Sie liegen samt Protokoll im Ordner 'nicht_erkannt'",
            "und koennen gesammelt weitergegeben werden.",
        ]
    else:
        zeilen += ["", "Aus allen Dateien wurden Positionen erkannt."]

    return "\n".join(zeilen)
