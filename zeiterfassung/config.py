"""Einstellungen der Zeiterfassung.

Die Einstellungen liegen als JSON im Benutzerprofil, damit mehrere Benutzer
am selben Rechner getrennte Daten haben.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import APP_NAME

# Wochenarbeitszeit laut Vorgabe.
WOCHEN_SOLL_STUNDEN = 38.0

# Vorgabe: gleichmaessig auf Montag bis Freitag (38 / 5 = 7,6 h = 7:36).
STANDARD_TAGES_SOLL: dict[str, float] = {
    "mo": 7.6, "di": 7.6, "mi": 7.6, "do": 7.6, "fr": 7.6, "sa": 0.0, "so": 0.0,
}

WOCHENTAG_SCHLUESSEL = ("mo", "di", "mi", "do", "fr", "sa", "so")


def datenverzeichnis() -> Path:
    """Verzeichnis fuer Datenbank, Einstellungen und Protokoll."""
    basis = os.environ.get("ZEITERFASSUNG_HOME")
    if basis:
        pfad = Path(basis)
    elif os.name == "nt":
        pfad = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        pfad = Path.home() / ".local" / "share" / "zeiterfassung"
    pfad.mkdir(parents=True, exist_ok=True)
    return pfad


@dataclass
class Einstellungen:
    """Alle veraenderbaren Vorgaben an einer Stelle."""

    wochen_soll: float = WOCHEN_SOLL_STUNDEN
    tages_soll: dict[str, float] = field(default_factory=lambda: dict(STANDARD_TAGES_SOLL))
    pausen_automatik: bool = True          # Pflichtpausen nach ArbZG abziehen
    hotkey: str = "Strg+Shift+Z"           # Mini-Fenster solange gedrueckt
    autostart: bool = True
    herzschlag_sekunden: int = 60
    # Ab dieser Luecke wird nachgefragt (kurze Aussetzer sind Arbeit).
    luecke_ab_minuten: int = 5
    export_verzeichnis: str = ""

    # -- Laden und Speichern ------------------------------------------------
    @classmethod
    def pfad(cls) -> Path:
        return datenverzeichnis() / "einstellungen.json"

    @classmethod
    def laden(cls) -> "Einstellungen":
        pfad = cls.pfad()
        if not pfad.exists():
            return cls()
        try:
            roh = json.loads(pfad.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        bekannt = {f: roh[f] for f in cls().__dict__ if f in roh}
        einstellungen = cls(**bekannt)
        # Fehlende Wochentage auffuellen, damit kein Schluessel fehlt.
        for tag in WOCHENTAG_SCHLUESSEL:
            einstellungen.tages_soll.setdefault(tag, STANDARD_TAGES_SOLL[tag])
        return einstellungen

    def speichern(self) -> None:
        self.pfad().write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # -- Abgeleitete Werte --------------------------------------------------
    def soll_stunden(self, wochentag: int) -> float:
        """Sollstunden fuer einen Wochentag (0 = Montag ... 6 = Sonntag)."""
        return float(self.tages_soll.get(WOCHENTAG_SCHLUESSEL[wochentag], 0.0))
