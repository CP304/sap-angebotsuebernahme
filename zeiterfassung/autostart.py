"""Automatischer Start beim Anmelden -- ohne Administratorrechte.

Weg 1 (Vorgabe): Eintrag unter
``HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.
Der Schluessel gehoert dem angemeldeten Benutzer, es sind keine erhoehten
Rechte noetig, und der Eintrag laesst sich jederzeit wieder entfernen.

Weg 2 (Ausweichloesung): eine Startdatei im Autostart-Ordner
(``shell:startup``), falls die Registry gesperrt ist.  Auch das ist ein
reiner Benutzerordner.

Beides ist auf einem verwalteten Arbeitsplatz zulaessig; greift eine
Gruppenrichtlinie, meldet die Funktion das ehrlich zurueck, statt es still
zu ignorieren.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

RUN_SCHLUESSEL = r"Software\Microsoft\Windows\CurrentVersion\Run"
EINTRAGSNAME = APP_NAME


def _startbefehl() -> str:
    """Befehl, der die Zeiterfassung ohne Konsolenfenster startet."""
    programm = Path(sys.executable)
    if programm.name.lower() == "python.exe":
        ohne_konsole = programm.with_name("pythonw.exe")
        if ohne_konsole.exists():
            programm = ohne_konsole
    if getattr(sys, "frozen", False):  # als .exe gepackt
        return f'"{programm}"'
    return f'"{programm}" -m zeiterfassung.main --hintergrund'


def _arbeitsverzeichnis() -> Path:
    return Path(__file__).resolve().parent.parent


def _autostart_ordner() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def _vbs_datei() -> Path:
    return _autostart_ordner() / f"{APP_NAME}.vbs"


def ist_eingerichtet() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SCHLUESSEL) as schluessel:
            winreg.QueryValueEx(schluessel, EINTRAGSNAME)
            return True
    except OSError:
        pass
    return _vbs_datei().exists()


def einrichten() -> tuple[bool, str]:
    """Traegt den Autostart ein.  Liefert (Erfolg, verstaendliche Meldung)."""
    if os.name != "nt":
        return False, "Autostart wird nur unter Windows eingerichtet."

    befehl = _startbefehl()
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_SCHLUESSEL) as schluessel:
            winreg.SetValueEx(schluessel, EINTRAGSNAME, 0, winreg.REG_SZ, befehl)
        return True, "Autostart eingerichtet (Registry des angemeldeten Benutzers)."
    except OSError as fehler:
        registry_fehler = str(fehler)

    try:
        ordner = _autostart_ordner()
        ordner.mkdir(parents=True, exist_ok=True)
        _vbs_datei().write_text(
            'Set shell = CreateObject("WScript.Shell")\r\n'
            f'shell.CurrentDirectory = "{_arbeitsverzeichnis()}"\r\n'
            f'shell.Run "{befehl.replace(chr(34), chr(34) * 2)}", 0, False\r\n',
            encoding="utf-8",
        )
        return True, "Autostart ueber den Autostart-Ordner eingerichtet."
    except OSError as fehler:
        return False, (
            "Autostart konnte nicht eingerichtet werden -- vermutlich sperrt eine "
            f"Gruppenrichtlinie beide Wege.\nRegistry: {registry_fehler}\nOrdner: {fehler}"
        )


def entfernen() -> tuple[bool, str]:
    """Nimmt den Autostart wieder heraus."""
    if os.name != "nt":
        return True, "Unter diesem Betriebssystem ist kein Autostart eingetragen."
    meldungen = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SCHLUESSEL, 0, winreg.KEY_SET_VALUE) as s:
            winreg.DeleteValue(s, EINTRAGSNAME)
        meldungen.append("Registry-Eintrag entfernt.")
    except FileNotFoundError:
        pass
    except OSError as fehler:
        return False, f"Registry-Eintrag blieb stehen: {fehler}"
    try:
        _vbs_datei().unlink(missing_ok=True)
    except OSError as fehler:
        return False, f"Startdatei blieb stehen: {fehler}"
    return True, " ".join(meldungen) or "Autostart war nicht eingetragen."
