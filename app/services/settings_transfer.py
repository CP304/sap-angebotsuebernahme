"""Einstellungen weitergeben -- an Kollegen, nicht an ein anderes Ich.

Wozu
----
Wer die Einrichtung einmal gemacht hat (Einkaufsorganisation, Werk,
Konditionsarten, aufgezeichnete SAP-Feld-IDs), soll sie nicht jedem
Kollegen einzeln erklaeren muessen.  Eine Datei weitergeben genuegt.

Was NICHT mitgeht
-----------------
Eine Einstellungsdatei ist keine Kopie des Rechners.  Drei Gruppen
bleiben bewusst zurueck:

* **Rechnergebundenes** -- Datenbankpfad, Protokollverzeichnis,
  Fenstergroesse.  Beim Empfaenger zeigen diese Pfade ins Leere oder,
  schlimmer, auf dessen falsche Datenbank.
* **Persoenliches** -- die Liste zuletzt geoeffneter Dateien.  Sie
  verraet, an welchen Lieferanten und Vorgaengen jemand gearbeitet hat;
  das gehoert niemandem sonst.
* **Betriebsart** -- Probelauf und Testsystem.  Diese beiden Schalter
  entscheiden, ob wirklich in SAP geschrieben wird.  Wuerden sie
  mitwandern, koennte eine importierte Datei beim Empfaenger unbemerkt
  den Echtbetrieb einschalten.  Jeder stellt das selbst ein.

Grundsatz beim Einlesen: ergaenzen, nicht ersetzen.  Was die Datei nicht
nennt, bleibt beim Empfaenger unveraendert -- eine aeltere Datei darf
keine neu hinzugekommene Einstellung auf den Auslieferungszustand
zuruecksetzen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["export_settings", "import_settings", "TransferResult",
           "EXCLUDED_TOP_LEVEL", "EXCLUDED_NESTED"]

#: Dateiformat -- steht mit in der Datei, damit ein spaeterer Wechsel
#: erkennbar ist, statt stillschweigend Unsinn einzulesen.
FORMAT_VERSION = 1

#: Schluessel der obersten Ebene, die nicht mitgehen.  Siehe Modulkopf.
EXCLUDED_TOP_LEVEL: frozenset[str] = frozenset({
    "database_path",      # zeigt beim Empfaenger ins Leere
    "log_path",
    "selectors_path",
    "dry_run",            # Betriebsart: jeder stellt sie selbst ein
    "use_mock_sap",
})

#: Einzelne Felder innerhalb eines Teilbereichs, die nicht mitgehen.
EXCLUDED_NESTED: dict[str, frozenset[str]] = {
    "ui": frozenset({
        "recent_files",       # verraet die eigene Vorgangsliste
        "max_recent_files",
        "remember_window_state",
    }),
}


class TransferResult:
    """Was beim Einlesen tatsaechlich passiert ist.

    Bewusst gespraechig: Der Anwender soll nachlesen koennen, was sich
    geaendert hat, statt einer Datei blind zu vertrauen.
    """

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.skipped: list[str] = []
        self.unknown: list[str] = []
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        teile = [f"{len(self.applied)} Einstellung(en) uebernommen"]
        if self.skipped:
            teile.append(f"{len(self.skipped)} bewusst ausgelassen")
        if self.unknown:
            teile.append(f"{len(self.unknown)} unbekannt")
        if self.errors:
            teile.append(f"{len(self.errors)} Fehler")
        return ", ".join(teile)

    def details(self) -> str:
        zeilen: list[str] = []
        if self.applied:
            zeilen.append("Uebernommen:")
            zeilen += [f"  {name}" for name in sorted(self.applied)]
        if self.skipped:
            zeilen.append("")
            zeilen.append("Nicht uebernommen (rechnergebunden oder persoenlich):")
            zeilen += [f"  {name}" for name in sorted(self.skipped)]
        if self.unknown:
            zeilen.append("")
            zeilen.append("Unbekannt -- stammt die Datei aus einer neueren Fassung?")
            zeilen += [f"  {name}" for name in sorted(self.unknown)]
        if self.errors:
            zeilen.append("")
            zeilen.append("Fehler:")
            zeilen += [f"  {name}" for name in self.errors]
        return "\n".join(zeilen)


def _json_default(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Nicht serialisierbar: {type(value)!r}")


def export_settings(settings: Any, path: str | Path,
                    note: str = "") -> Path:
    """Einstellungen in eine weitergebbare Datei schreiben."""
    ziel = Path(path)
    nutzdaten: dict[str, Any] = {}

    for feld in fields(settings):
        name = feld.name
        if name in EXCLUDED_TOP_LEVEL:
            continue
        wert = getattr(settings, name)
        if is_dataclass(wert) and not isinstance(wert, type):
            teilbereich = asdict(wert)
            for verboten in EXCLUDED_NESTED.get(name, frozenset()):
                teilbereich.pop(verboten, None)
            nutzdaten[name] = teilbereich
        else:
            nutzdaten[name] = wert

    inhalt = {
        "format": FORMAT_VERSION,
        "hinweis": note or "Einstellungen zur Weitergabe an Kollegen.",
        "enthaelt_nicht": sorted(EXCLUDED_TOP_LEVEL)
                          + [f"ui.{f}" for f in sorted(EXCLUDED_NESTED["ui"])],
        "einstellungen": nutzdaten,
    }

    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(
        json.dumps(inhalt, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8")
    logger.info("Einstellungen exportiert: %s (%d Bereiche)", ziel, len(nutzdaten))
    return ziel


def _coerce_like(vorlage: Any, wert: Any) -> Any:
    """Einen eingelesenen Wert auf den Typ des Bestandswertes bringen."""
    from decimal import Decimal, InvalidOperation

    if isinstance(vorlage, Decimal):
        try:
            return Decimal(str(wert))
        except (InvalidOperation, ValueError):
            raise ValueError(f"keine Zahl: {wert!r}")
    if isinstance(vorlage, bool):
        if isinstance(wert, str):
            return wert.strip().lower() in ("1", "true", "ja", "yes", "on")
        return bool(wert)
    if isinstance(vorlage, int) and not isinstance(vorlage, bool):
        return int(wert)
    if isinstance(vorlage, float):
        return float(wert)
    if isinstance(vorlage, str):
        return str(wert)
    return wert


def import_settings(settings: Any, path: str | Path) -> TransferResult:
    """Einstellungen aus einer Datei uebernehmen -- ergaenzend.

    Der Aufrufer entscheidet, ob danach gespeichert wird; diese Funktion
    aendert nur das uebergebene Objekt.
    """
    ergebnis = TransferResult()
    quelle = Path(path)

    try:
        rohtext = quelle.read_text(encoding="utf-8")
    except OSError as fehler:
        ergebnis.errors.append(f"Datei nicht lesbar: {fehler}")
        return ergebnis

    try:
        inhalt = json.loads(rohtext)
    except json.JSONDecodeError as fehler:
        ergebnis.errors.append(
            f"Die Datei ist keine gueltige Einstellungsdatei (Zeile "
            f"{fehler.lineno}): {fehler.msg}")
        return ergebnis

    if not isinstance(inhalt, dict):
        ergebnis.errors.append("Die Datei enthaelt keine Einstellungen.")
        return ergebnis

    version = inhalt.get("format")
    if version is not None and version > FORMAT_VERSION:
        ergebnis.errors.append(
            f"Die Datei stammt aus einer neueren Fassung (Format {version}, "
            f"dieses Programm kennt {FORMAT_VERSION}). Bitte das Programm "
            "aktualisieren.")
        return ergebnis

    daten = inhalt.get("einstellungen")
    if not isinstance(daten, dict):
        ergebnis.errors.append("Die Datei enthaelt keinen Abschnitt "
                               "'einstellungen'.")
        return ergebnis

    bekannte = {f.name for f in fields(settings)}

    for name, wert in daten.items():
        if name in EXCLUDED_TOP_LEVEL:
            # Auch wenn es in der Datei steht: nicht uebernehmen.  Eine
            # fremde Datei darf die Betriebsart nicht umschalten.
            ergebnis.skipped.append(name)
            continue
        if name not in bekannte:
            ergebnis.unknown.append(name)
            continue

        bestand = getattr(settings, name)

        if is_dataclass(bestand) and not isinstance(bestand, type):
            if not isinstance(wert, dict):
                ergebnis.errors.append(f"{name}: erwartet wurde ein Abschnitt")
                continue
            unterfelder = {f.name for f in fields(bestand)}
            gesperrt = EXCLUDED_NESTED.get(name, frozenset())
            for untername, unterwert in wert.items():
                voll = f"{name}.{untername}"
                if untername in gesperrt:
                    ergebnis.skipped.append(voll)
                    continue
                if untername not in unterfelder:
                    ergebnis.unknown.append(voll)
                    continue
                try:
                    vorlage = getattr(bestand, untername)
                    setattr(bestand, untername,
                            _coerce_like(vorlage, unterwert))
                    ergebnis.applied.append(voll)
                except (ValueError, TypeError) as fehler:
                    ergebnis.errors.append(f"{voll}: {fehler}")
        else:
            try:
                setattr(settings, name, _coerce_like(bestand, wert))
                ergebnis.applied.append(name)
            except (ValueError, TypeError) as fehler:
                ergebnis.errors.append(f"{name}: {fehler}")

    logger.info("Einstellungen eingelesen aus %s: %s", quelle, ergebnis.summary())
    return ergebnis
