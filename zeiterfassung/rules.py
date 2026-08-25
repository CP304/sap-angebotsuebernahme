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
from .storage import (
    ABWESEND,
    ARBEIT,
    GUTSCHRIFT_TAGESARTEN,
    PAUSE,
    Luecke,
    Sitzung,
    Tagesart,
)

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


# -- Intervallrechnung ------------------------------------------------------
# Zeitraeume koennen sich ueberschneiden: zwei Sitzungen nach einer Korrektur,
# oder eine von Hand nachgetragene Pause mitten in einer Sitzung.  Deshalb
# wird mit Mengen von Zeitraeumen gerechnet und nicht mit blossen Summen --
# sonst zaehlt dieselbe Minute doppelt oder eine Pause bleibt wirkungslos.
Zeitraum = tuple[datetime, datetime]


def _vereinigen(zeitraeume: list[Zeitraum]) -> list[Zeitraum]:
    """Ueberschneidungen zusammenfassen -- jede Minute zaehlt genau einmal."""
    ergebnis: list[Zeitraum] = []
    for beginn, ende in sorted(z for z in zeitraeume if z[1] > z[0]):
        if ergebnis and beginn <= ergebnis[-1][1]:
            letzter = ergebnis[-1]
            ergebnis[-1] = (letzter[0], max(letzter[1], ende))
        else:
            ergebnis.append((beginn, ende))
    return ergebnis


def _abziehen(grundmenge: list[Zeitraum], abzug: list[Zeitraum]) -> list[Zeitraum]:
    """Alles aus ``abzug`` aus ``grundmenge`` herausschneiden."""
    ergebnis = list(grundmenge)
    for a_beginn, a_ende in _vereinigen(abzug):
        naechste: list[Zeitraum] = []
        for beginn, ende in ergebnis:
            if a_ende <= beginn or a_beginn >= ende:
                naechste.append((beginn, ende))
                continue
            if beginn < a_beginn:
                naechste.append((beginn, a_beginn))
            if a_ende < ende:
                naechste.append((a_ende, ende))
        ergebnis = naechste
    return ergebnis


def _summe(zeitraeume: list[Zeitraum]) -> timedelta:
    return sum((ende - beginn for beginn, ende in zeitraeume), timedelta(0))


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
    erfasste_arbeitszeit: timedelta  # tatsaechlich am Rechner geleistet
    gutschrift: timedelta           # aus Urlaub, Krankheit, Feiertag ...
    arbeitszeit: timedelta          # das, was zaehlt (erfasst + Gutschrift)
    soll: timedelta
    laeuft: bool                    # Rechner laeuft gerade (Sitzung offen)
    tagesart: Tagesart | None = None
    offene_luecken: int = 0

    @property
    def art_beschriftung(self) -> str:
        return self.tagesart.beschriftung if self.tagesart else "Arbeitstag"

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
        if self.tagesart is not None and self.tagesart.anteil >= 1.0:
            return None  # ganzer Tag Urlaub, Krankheit oder Feiertag
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
    tagesart: Tagesart | None = None,
) -> Tageswerte:
    """Rechnet Sitzungen, Luecken und die Tagesart zu Tageskennzahlen zusammen.

    Urlaub, Krankheit und Feiertag schreiben das Tagessoll gut.  Bei einem
    ganzen Tag bleibt eine trotzdem aufgezeichnete Rechnerlaufzeit
    unberuecksichtigt -- der Rechner lief dann eben mit, gearbeitet wurde
    nicht.  Bei einem halben Tag wird die halbe Gutschrift mit der wirklich
    geleisteten Zeit addiert.
    """
    von, bis = tagesfenster(tag)
    jetzt = jetzt or datetime.now()

    anwesend: list[Zeitraum] = []
    pausen: list[Zeitraum] = []
    laeuft = False

    for sitzung in sitzungen:
        # Eine laufende Sitzung reicht bis jetzt -- der letzte Herzschlag
        # liegt je nach Takt bis zu einer Minute zurueck.
        ende = max(sitzung.ende, jetzt) if sitzung.laeuft else sitzung.ende
        zeitraum = (max(sitzung.beginn, von), min(ende, bis))
        if zeitraum[1] > zeitraum[0]:
            anwesend.append(zeitraum)
            laeuft = laeuft or sitzung.laeuft

    for luecke in luecken:
        zeitraum = (max(luecke.beginn, von), min(luecke.ende, bis))
        if zeitraum[1] <= zeitraum[0]:
            continue
        if luecke.art == ARBEIT:
            anwesend.append(zeitraum)
        elif luecke.art in (PAUSE, ABWESEND):
            pausen.append(zeitraum)

    anwesend = _vereinigen(anwesend)
    pausen = _vereinigen(pausen)
    # Eine Pause innerhalb einer Sitzung muss die Anwesenheit kuerzen --
    # sonst waere sie folgenlos.
    anwesend = _abziehen(anwesend, pausen)

    anwesenheit = _summe(anwesend)
    erfasste_pause = _summe(pausen)
    erste = anwesend[0][0] if anwesend else None
    letzter = anwesend[-1][1] if anwesend else None

    if einstellungen.pausen_automatik:
        # Zweistufig: erst mit der Bruttozeit pruefen, dann mit der bereits
        # gekuerzten Zeit -- so kippt niemand faelschlich ueber die Schwelle.
        abzug = max(timedelta(0), pflichtpause(anwesenheit) - erfasste_pause)
        abzug = max(timedelta(0), pflichtpause(anwesenheit - abzug) - erfasste_pause)
    else:
        abzug = timedelta(0)

    erfasst = max(timedelta(0), anwesenheit - abzug)
    soll = timedelta(hours=einstellungen.soll_stunden(tag.weekday()))

    gutschrift = timedelta(0)
    if tagesart is not None:
        anteil = max(0.0, min(1.0, tagesart.anteil))
        if GUTSCHRIFT_TAGESARTEN.get(tagesart.art, False):
            gutschrift = soll * anteil
        else:
            # Unbezahlt frei: das Soll sinkt entsprechend.
            soll = soll * (1 - anteil)
        if anteil >= 1.0:
            erfasst = timedelta(0)
    arbeitszeit = erfasst + gutschrift

    return Tageswerte(
        tag=tag,
        erste_anmeldung=erste,
        letzter_kontakt=letzter,
        anwesenheit=anwesenheit,
        erfasste_pause=erfasste_pause,
        pausenabzug=abzug,
        erfasste_arbeitszeit=erfasst,
        gutschrift=gutschrift,
        arbeitszeit=arbeitszeit,
        soll=soll,
        laeuft=laeuft,
        tagesart=tagesart,
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

    @property
    def gutschrift(self) -> timedelta:
        return sum((t.gutschrift for t in self.tage), timedelta(0))


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
