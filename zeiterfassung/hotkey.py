"""Globaler Hotkey, der nur gilt, solange die Tasten gedrueckt sind.

Windows meldet ueber ``RegisterHotKey`` nur das Druecken, nicht das Loslassen.
Das Mini-Fenster soll aber genau solange stehen, wie die Tastenkombination
gehalten wird.  Deshalb wird der Tastenzustand mit ``GetAsyncKeyState``
abgefragt -- das funktioniert systemweit, auch wenn ein anderes Programm im
Vordergrund ist, und braucht keine besonderen Rechte.
"""

from __future__ import annotations

import os
from collections.abc import Callable

# Virtuelle Tastencodes (Windows).
VK = {
    "strg": 0x11, "ctrl": 0x11, "control": 0x11,
    "shift": 0x10, "umschalt": 0x10,
    "alt": 0x12,
    "leertaste": 0x20, "space": 0x20,
    "esc": 0x1B,
}
for _buchstabe in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_buchstabe.lower()] = ord(_buchstabe)
for _nummer in range(1, 13):
    VK[f"f{_nummer}"] = 0x6F + _nummer


def tastencodes(hotkey: str) -> list[int]:
    """``"Strg+Shift+Z"`` -> ``[0x11, 0x10, 0x5A]``."""
    codes = []
    for teil in hotkey.replace(" ", "").split("+"):
        if not teil:
            continue
        code = VK.get(teil.lower())
        if code is None:
            raise ValueError(f"Unbekannte Taste: {teil!r}")
        codes.append(code)
    if not codes:
        raise ValueError("Leere Tastenkombination")
    return codes


class HotkeyWaechter:
    """Fragt zyklisch ab, ob die Kombination gerade gehalten wird.

    Die Abfrage selbst kostet nichts Nennenswertes (drei Aufrufe alle 60 ms);
    aufgerufen wird sie aus einem Qt-Timer, damit alles im GUI-Faden bleibt.
    """

    def __init__(self, hotkey: str) -> None:
        self.hotkey = hotkey
        self.codes = tastencodes(hotkey)
        self._gedrueckt = False
        self._user32 = None
        if os.name == "nt":  # pragma: no cover - nur Windows
            import ctypes

            self._user32 = ctypes.windll.user32

    @property
    def verfuegbar(self) -> bool:
        return self._user32 is not None

    def gedrueckt(self) -> bool:
        """True, solange alle Tasten der Kombination unten sind."""
        if self._user32 is None:
            return False
        # Das hohe Bit zeigt an, dass die Taste im Moment gedrueckt ist.
        return all(self._user32.GetAsyncKeyState(code) & 0x8000 for code in self.codes)

    def pruefen(self, beim_druecken: Callable[[], None], beim_loslassen: Callable[[], None]) -> None:
        """Ruft die Rueckmeldungen genau bei Zustandswechseln auf."""
        jetzt_gedrueckt = self.gedrueckt()
        if jetzt_gedrueckt and not self._gedrueckt:
            self._gedrueckt = True
            beim_druecken()
        elif not jetzt_gedrueckt and self._gedrueckt:
            self._gedrueckt = False
            beim_loslassen()
