"""Rechenregeln der Zeiterfassung.

Hier steht die gesamte Fachlogik -- ohne Oberflaeche und ohne Datenbank,
damit sie vollstaendig testbar bleibt.

Grundlagen
----------
* Wochenarbeitszeit 38 Stunden, verteilt auf Montag bis Freitag (7,6 h/Tag).
  Fuehrend ist das Tagesziel, zusaetzlich wird der Wochenrest ausgewiesen.
* Pflichtpausen nach Arbeitszeitgesetz (Paragraph 4 ArbZG):
  ab mehr als 6 Stunden Arbeitszeit 30 Minuten, ab mehr als 9 Stunden
  45 Minuten.  Bereits erfasste Pausen werden angerechnet, nur die Differenz
  wird zusaetzlich abgezogen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Einstellungen
from .storage import ABWESEND, ARBEIT, PAUSE, Luecke, Sitzung

# Schwellen der Pflichtpause: (Arbeitszeit ueber ..., Pause mindestens ...)
PFLICHTPAUSEN = (
    (timedelta(hours=9), timedelta(minutes=45)),
    (timedelta(hours=6), timedelta(minutes=30)),
)


def pflichtpause(arbeitszeit: timedelta) -> timedelta:
    """Mindestpause nach ArbZG fuer die geleistete Arbeitszeit."""
    for schwelle, pause in PFLICHTPAUSEN:
        if arbeitszeit > schwelle:
            return pause
    return timedelta(0)


def _zuschnitt(beginn: datetime, ende: datetime, von: datetime, bis: datetime) -> timedelta:
    """Anteil eines Zeitraums, der in das Fenster [von, bis) faellt."""
    start = max(beginn, von)
    schluss = min(ende, bis)
    return max(timedelta(0), schluss - start)


def tagesfenster(tag: date) -> tuple[datetime, datetime]:
    beginn = datetime.combine(tag, time.min)
    return beginn, beginn + timedelta(days=1)


@dataclass
class Tageswerte:
    """Ausgewertete Kennzahlen eines einzelnen Tages."""

    tag: date
    erste_anmeldung: datetime | None
    letzter_kontakt: datetime | None
    anwesenheit: timedelta          # Einschaltzeit + als Arbeit gebuchte Luecken
    erfasste_pause: timedelta       # Luecken als Pause oder Abwesenheit
    pausenabzug: timedelta          # zusaetzlicher Abzug nach ArbZG
    arbeitszeit: timedelta          # das, was zaehlt
    soll: timedelta
    laeuft: bool                    # Rechner laeuft gerade (Sitzung offen)
    offene_luecken: int = 0

    @property
    def rest(self) -> timedelta:
        return max(timedelta(0), self.soll - self.arbeitszeit)

    @property
    def saldo(self) -> timedelta:
        return self.arbeitszeit - self.soll

    @property
    def feierabend(self) -> datetime | None:
        """Voraussichtlicher Feierabend, wenn ab jetzt durchgearbeitet wird."""
        if self.letzter_kontakt is None:
            return None
        rest = self.rest
        if rest <= timedelta(0):
            return self.letzter_kontakt
        # Kippt die verbleibende Arbeit ueber eine Pausenschwelle, faellt
        # dort weitere Pausenzeit an -- die muss man mit absitzen.
        ziel = self.letzter_kontakt + rest
        zusatz = pflichtpause(self.arbeitszeit + rest) - (self.erfasste_pause + self.pausenabzug)
        if zusatz > timedelta(0):
            ziel += zusatz
        return ziel


def tag_auswerten(
    tag: date,
    sitzungen: list[Sitzung],
    luecken: list[Luecke],
    einstellungen: Einstellungen,
    jetzt: datetime | None = None,
) -> Tageswerte:
    """Rechnet Sitzungen und eingeordnete Luecken zu Tageskennzahlen zusammen."""
    von, bis = tagesfenster(tag)
    jetzt = jetzt or datetime.now()

    anwesenheit = timedelta(0)
    erste: datetime | None = None
    letzter: datetime | None = None
    laeuft = False

    for sitzung in sitzungen:
        # Eine laufende Sitzung reicht bis jetzt -- der letzte Herzschlag
        # liegt je nach Takt bis zu einer Minute zurueck.
        ende = max(sitzung.ende, jetzt) if sitzung.laeuft else sitzung.ende
        anteil = _zuschnitt(sitzung.beginn, ende, von, bis)
        if anteil <= timedelta(0) and not (von <= sitzung.beginn < bis):
            continue
        anwesenheit += anteil
        beginn_im_tag = max(sitzung.beginn, von)
        ende_im_tag = min(ende, bis)
        erste = beginn_im_tag if erste is None else min(erste, beginn_im_tag)
        letzter = ende_im_tag if letzter is None else max(letzter, ende_im_tag)
        laeuft = laeuft or sitzung.laeuft

    erfasste_pause = timedelta(0)
    for luecke in luecken:
        anteil = _zuschnitt(luecke.beginn, luecke.ende, von, bis)
        if anteil <= timedelta(0):
            continue
        if luecke.art == ARBEIT:
            anwesenheit += anteil
            letzter = max(letzter, min(luecke.ende, bis)) if letzter else min(luecke.ende, bis)
            erste = min(erste, max(luecke.beginn, von)) if erste else max(luecke.beginn, von)
        elif luecke.art in (PAUSE, ABWESEND):
            erfasste_pause += anteil

    if einstellungen.pausen_automatik:
        # Zweistufig: erst mit der Bruttozeit pruefen, dann mit der bereits
        # gekuerzten Zeit -- so kippt niemand faelschlich ueber die Schwelle.
        abzug = max(timedelta(0), pflichtpause(anwesenheit) - erfasste_pause)
        abzug = max(timedelta(0), pflichtpause(anwesenheit - abzug) - erfasste_pause)
    else:
        abzug = timedelta(0)

    arbeitszeit = max(timedelta(0), anwesenheit - abzug)
    soll = timedelta(hours=einstellungen.soll_stunden(tag.weekday()))

    return Tageswerte(
        tag=tag,
        erste_anmeldung=erste,
        letzter_kontakt=letzter,
        anwesenheit=anwesenheit,
        erfasste_pause=erfasste_pause,
        pausenabzug=abzug,
        arbeitszeit=arbeitszeit,
        soll=soll,
        laeuft=laeuft,
    )


@dataclass
class Wochenwerte:
    """Zusammenfassung einer Kalenderwoche."""

    montag: date
    tage: list[Tageswerte]

    @property
    def arbeitszeit(self) -> timedelta:
        return sum((t.arbeitszeit for t in self.tage), timedelta(0))

    @property
    def soll(self) -> timedelta:
        return sum((t.soll for t in self.tage), timedelta(0))

    @property
    def rest(self) -> timedelta:
        return max(timedelta(0), self.soll - self.arbeitszeit)

    @property
    def saldo(self) -> timedelta:
        return self.arbeitszeit - self.soll

    @property
    def kalenderwoche(self) -> int:
        return self.montag.isocalendar().week


def wochenbeginn(tag: date) -> date:
    return tag - timedelta(days=tag.weekday())


# -- Darstellung ------------------------------------------------------------
def als_stunden(dauer: timedelta) -> str:
    """``timedelta`` als ``7:36 h`` -- auch negativ (Minusstunden)."""
    vorzeichen = "-" if dauer < timedelta(0) else ""
    minuten = int(round(abs(dauer).total_seconds() / 60))
    return f"{vorzeichen}{minuten // 60}:{minuten % 60:02d} h"


def als_dezimal(dauer: timedelta) -> float:
    """Dauer in Industriestunden, auf zwei Stellen gerundet (fuer Excel)."""
    return round(dauer.total_seconds() / 3600, 2)


def als_uhrzeit(zeitpunkt: datetime | None) -> str:
    return zeitpunkt.strftime("%H:%M") if zeitpunkt else "--:--"
