"""Zeiterfassung -- Arbeitszeit anhand der Einschaltzeiten des Rechners.

Das Werkzeug laeuft im Hintergrund (Infobereich der Taskleiste), schreibt
fortlaufend Herzschlaege in eine SQLite-Datei und erkennt daran, wann der
Rechner lief.  Luecken innerhalb eines Tages (Neustart, Standby, Abmeldung)
werden beim naechsten Start nachgefragt und als Arbeit, Pause oder
Abwesenheit verbucht.
"""

from __future__ import annotations

__version__ = "1.0.0"
APP_NAME = "Zeiterfassung"
