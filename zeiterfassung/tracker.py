"""Aufzeichnung der Einschaltzeiten.

Ablauf beim Start:

1. Offene Sitzungen aus einem harten Ausschalten werden geschlossen -- ihr
   Ende ist der letzte Herzschlag.
2. Liegt der letzte Kontakt am selben Tag und ist die Luecke groesser als die
   eingestellte Schwelle, entsteht eine offene Frage: War der Rechner
   zwischendurch aus, weil Pause war, weil anderswo gearbeitet wurde oder
   weil Feierabend war?  Die Antwort holt die Oberflaeche ein.
3. Danach laeuft eine neue Sitzung, deren Ende jede Minute nachgezogen wird.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Einstellungen
from .rules import (
    Tageswerte,
    Wochenwerte,
    tag_auswerten,
    tagesfenster,
    wochenbeginn,
)
from .storage import (
    ABWESEND,
    ARBEIT,
    ARBEITSTAG,
    PAUSE,
    TAGESART_TEXT,
    Datenbank,
    Luecke,
    Sitzung,
)


@dataclass
class OffeneLuecke:
    """Eine Zeitspanne ohne Rechnerbetrieb, die noch eingeordnet werden muss."""

    beginn: datetime
    ende: datetime

    @property
    def dauer(self) -> timedelta:
        return self.ende - self.beginn


class Zeiterfassung:
    """Verbindet Datenbank und Rechenregeln -- ohne jede Oberflaeche."""

    def __init__(self, datenbank: Datenbank, einstellungen: Einstellungen) -> None:
        self.db = datenbank
        self.einstellungen = einstellungen
        self.sitzung_id: int | None = None

    # -- Lebenszyklus -------------------------------------------------------
    def starten(self, jetzt: datetime | None = None) -> list[OffeneLuecke]:
        """Startet die Aufzeichnung und liefert die nachzufragenden Luecken."""
        jetzt = jetzt or datetime.now()
        letzte = self.db.letzte_sitzung_vor(jetzt)
        self.db.offene_sitzungen_abschliessen()

        luecken = self._offene_luecken(letzte, jetzt)
        self.sitzung_id = self.db.sitzung_beginnen(jetzt)
        return luecken

    def _offene_luecken(self, letzte, jetzt: datetime) -> list[OffeneLuecke]:
        if letzte is None:
            return []
        # Nur Luecken innerhalb desselben Arbeitstages sind interessant --
        # ueber Nacht ist Feierabend die selbstverstaendliche Erklaerung.
        if letzte.ende.date() != jetzt.date():
            return []
        luecke = jetzt - letzte.ende
        if luecke < timedelta(minutes=self.einstellungen.luecke_ab_minuten):
            # Kurze Aussetzer gelten als durchgehende Arbeit.
            if luecke > timedelta(0):
                self.db.luecke_eintragen(letzte.ende, jetzt, ARBEIT, "automatisch: kurzer Aussetzer")
            return []
        if self.db.ist_eingeordnet(letzte.ende, jetzt):
            return []
        return [OffeneLuecke(beginn=letzte.ende, ende=jetzt)]

    def herzschlag(self, jetzt: datetime | None = None) -> None:
        if self.sitzung_id is None:
            return
        self.db.herzschlag(self.sitzung_id, jetzt or datetime.now())

    def beenden(self, jetzt: datetime | None = None) -> None:
        if self.sitzung_id is None:
            return
        self.db.sitzung_beenden(self.sitzung_id, jetzt or datetime.now(), geschaetzt=False)
        self.sitzung_id = None

    # -- Antworten auf Rueckfragen -----------------------------------------
    def luecke_einordnen(self, luecke: OffeneLuecke, art: str, notiz: str = "") -> None:
        if art not in (ARBEIT, PAUSE, ABWESEND):
            raise ValueError(f"Unbekannte Art: {art!r}")
        self.db.luecke_eintragen(luecke.beginn, luecke.ende, art, notiz)

    # -- Auswertung ---------------------------------------------------------
    def tag(self, tag: date | None = None, jetzt: datetime | None = None) -> Tageswerte:
        jetzt = jetzt or datetime.now()
        tag = tag or jetzt.date()
        von, bis = tagesfenster(tag)
        werte = tag_auswerten(
            tag,
            self.db.sitzungen(von, bis),
            self.db.luecken(von, bis),
            self.einstellungen,
            jetzt=jetzt,
            tagesart=self.db.tagesart(tag),
        )
        return werte

    def woche(self, tag: date | None = None, jetzt: datetime | None = None) -> Wochenwerte:
        jetzt = jetzt or datetime.now()
        tag = tag or jetzt.date()
        montag = wochenbeginn(tag)
        tage = [self.tag(montag + timedelta(days=i), jetzt=jetzt) for i in range(7)]
        return Wochenwerte(montag=montag, tage=tage)

    def zeitraum(self, von: date, bis: date, jetzt: datetime | None = None) -> list[Tageswerte]:
        """Alle Tage von ``von`` bis ``bis`` einschliesslich."""
        jetzt = jetzt or datetime.now()
        tage: list[Tageswerte] = []
        laufend = von
        while laufend <= bis:
            tage.append(self.tag(laufend, jetzt=jetzt))
            laufend += timedelta(days=1)
        return tage

    # -- Manuelle Korrektur -------------------------------------------------
    def buchungen(self, tag: date) -> list[Sitzung | Luecke]:
        """Alle Eintraege eines Tages, nach Uhrzeit sortiert."""
        von, bis = tagesfenster(tag)
        eintraege: list[Sitzung | Luecke] = [*self.db.sitzungen(von, bis), *self.db.luecken(von, bis)]
        return sorted(eintraege, key=lambda eintrag: eintrag.beginn)

    def sitzung_nachtragen(self, beginn: datetime, ende: datetime) -> int:
        """Traegt eine vergessene Arbeitszeit von Hand nach."""
        if ende <= beginn:
            raise ValueError("Das Ende muss nach dem Beginn liegen.")
        return self.db.sitzung_anlegen(beginn, ende)

    def sitzung_korrigieren(self, sitzung_id: int, beginn: datetime, ende: datetime) -> None:
        if ende <= beginn:
            raise ValueError("Das Ende muss nach dem Beginn liegen.")
        if self.sitzung_id == sitzung_id:
            # Die laufende Sitzung wird durch die Korrektur festgeschrieben.
            self.sitzung_id = None
        self.db.sitzung_aendern(sitzung_id, beginn, ende)

    def sitzung_verwerfen(self, sitzung_id: int) -> None:
        if self.sitzung_id == sitzung_id:
            self.sitzung_id = None
        self.db.sitzung_loeschen(sitzung_id)

    def luecke_nachtragen(self, beginn: datetime, ende: datetime, art: str, notiz: str = "") -> None:
        if ende <= beginn:
            raise ValueError("Das Ende muss nach dem Beginn liegen.")
        if art not in (ARBEIT, PAUSE, ABWESEND):
            raise ValueError(f"Unbekannte Art: {art!r}")
        self.db.luecke_eintragen(beginn, ende, art, notiz)

    def luecke_korrigieren(self, luecke_id: int, beginn: datetime, ende: datetime, art: str, notiz: str = "") -> None:
        if ende <= beginn:
            raise ValueError("Das Ende muss nach dem Beginn liegen.")
        self.db.luecke_aendern(luecke_id, beginn, ende, art, notiz)

    def luecke_verwerfen(self, luecke_id: int) -> None:
        self.db.luecke_loeschen(luecke_id)

    # -- Urlaub, Krankheit, Feiertag ---------------------------------------
    def tagesart_setzen(
        self, von: date, bis: date, art: str, anteil: float = 1.0, notiz: str = ""
    ) -> int:
        """Setzt die Tagesart fuer einen ganzen Zeitraum und liefert die Anzahl.

        Wochenenden und andere Tage ohne Soll bleiben unberuehrt -- Urlaub am
        Sonntag kostet keinen Urlaubstag.
        """
        if art != ARBEITSTAG and art not in TAGESART_TEXT:
            raise ValueError(f"Unbekannte Tagesart: {art!r}")
        gesetzt = 0
        laufend = von
        while laufend <= bis:
            if art == ARBEITSTAG:
                self.db.tagesart_loeschen(laufend)
                gesetzt += 1
            elif self.einstellungen.soll_stunden(laufend.weekday()) > 0:
                self.db.tagesart_setzen(laufend, art, anteil, notiz)
                gesetzt += 1
            laufend += timedelta(days=1)
        return gesetzt

    # -- Kennzahlen fuer das Mini-Fenster -----------------------------------
    def kennzahlen(self, jetzt: datetime | None = None) -> dict[str, object]:
        jetzt = jetzt or datetime.now()
        heute = self.tag(jetzt=jetzt)
        woche = self.woche(jetzt=jetzt)
        seit = None
        if self.sitzung_id is not None:
            sitzung = self.db.letzte_sitzung_vor(jetzt + timedelta(seconds=1))
            seit = sitzung.beginn if sitzung else None
        return {
            "tag": heute,
            "woche": woche,
            "eingestempelt_seit": heute.erste_anmeldung,
            "taetig_seit": seit,
            "jetzt": jetzt,
        }
