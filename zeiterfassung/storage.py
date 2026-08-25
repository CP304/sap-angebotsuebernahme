"""Datenhaltung der Zeiterfassung (SQLite).

Drei Tabellen genuegen:

``sitzungen``  Eine Zeile je Einschaltzeitraum des Rechners.  ``ende`` wird
               fortlaufend mit dem Herzschlag nachgezogen, damit nach einem
               Stromausfall der letzte bekannte Stand erhalten bleibt.
``luecken``    Die Zeit zwischen zwei Sitzungen, sobald der Benutzer sie
               eingeordnet hat (Arbeit, Pause oder Abwesenheit).
``tagesarten`` Ganze oder halbe Tage, an denen nicht gearbeitet wird --
               Urlaub, Krankheit, Feiertag, Gleittag, Dienstreise.
``protokoll``  Das Aenderungsprotokoll: wer hat wann was von Hand geaendert
               und mit welcher Begruendung.  Es wird nur ergaenzt, nie
               ueberschrieben -- daraus wird der Bericht belastbar.

Jeder Zeitstempel traegt seine Herkunft (``quelle``).  So laesst sich in
jedem Bericht ablesen, ob eine Zeit vom Rechner selbst kommt, aus dem
letzten Herzschlag geschaetzt ist oder von Hand erfasst wurde -- und im
letzten Fall mit welcher Begruendung.

Alle Zeitstempel sind lokale Zeit im Format ``YYYY-MM-DD HH:MM:SS`` -- die
Zeiterfassung bezieht sich immer auf den Arbeitstag vor Ort.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import datenverzeichnis

ZEITFORMAT = "%Y-%m-%d %H:%M:%S"

# Herkunft eines Zeitstempels -- der Kern der Nachweisbarkeit.
AUTOMATISCH = "automatisch"    # Rechner lief, vom Programm selbst gemessen
GESCHAETZT = "geschaetzt"      # Ende aus dem letzten Herzschlag (harter Aus)
RUECKFRAGE = "rueckfrage"      # Luecke, vom Benutzer auf Nachfrage eingeordnet
MANUELL = "manuell"            # von Hand erfasst oder korrigiert

QUELLE_TEXT = {
    AUTOMATISCH: "automatisch gemessen",
    GESCHAETZT: "Ende aus letztem Herzschlag",
    RUECKFRAGE: "auf Rueckfrage eingeordnet",
    MANUELL: "von Hand erfasst",
}

# Diese Herkuenfte verlangen eine Begruendung.
BEGRUENDUNGSPFLICHT = (RUECKFRAGE, MANUELL)

# Aktionen im Aenderungsprotokoll.
ANGELEGT = "angelegt"
GEAENDERT = "geaendert"
GELOESCHT = "geloescht"

# Einordnung einer Luecke.
ARBEIT = "arbeit"
PAUSE = "pause"
ABWESEND = "abwesend"

# Tagesarten.  ``ARBEITSTAG`` ist der Normalfall und wird nicht gespeichert.
ARBEITSTAG = "arbeitstag"
URLAUB = "urlaub"
KRANK = "krank"
FEIERTAG = "feiertag"
GLEITTAG = "gleittag"
DIENSTREISE = "dienstreise"

# Wie eine Tagesart auf das Sollkonto wirkt:
#   True  -> das Tagessoll gilt als erfuellt (Gutschrift)
#   False -> kein Soll und keine Gutschrift (unbezahlt frei)
GUTSCHRIFT_TAGESARTEN = {
    URLAUB: True,
    KRANK: True,
    FEIERTAG: True,
    GLEITTAG: True,       # Zeitausgleich: Soll gilt als erfuellt, aus dem Saldo
    DIENSTREISE: True,    # unterwegs gearbeitet, aber ohne Rechnerlaufzeit
}

TAGESART_TEXT = {
    ARBEITSTAG: "Arbeitstag",
    URLAUB: "Urlaub",
    KRANK: "Krank",
    FEIERTAG: "Feiertag",
    GLEITTAG: "Gleittag",
    DIENSTREISE: "Dienstreise",
}


def als_text(zeitpunkt: datetime) -> str:
    return zeitpunkt.strftime(ZEITFORMAT)


def als_zeit(text: str) -> datetime:
    return datetime.strptime(text, ZEITFORMAT)


def als_tag(text: str) -> date:
    return date.fromisoformat(text)


def _pflicht(begruendung: str) -> str:
    """Stellt sicher, dass eine Begruendung wirklich etwas aussagt."""
    begruendung = (begruendung or "").strip()
    if len(begruendung) < 3:
        raise ValueError(
            "Bitte eine Begruendung angeben -- sie steht spaeter im Bericht "
            "und macht die Zeit nachvollziehbar."
        )
    return begruendung


def benutzer() -> str:
    """Angemeldeter Benutzer -- steht in jedem Protokolleintrag."""
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unbekannt"


def rechnername() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


@dataclass
class Sitzung:
    """Ein Zeitraum, in dem der Rechner nachweislich lief."""

    id: int
    beginn: datetime
    ende: datetime
    ende_geschaetzt: bool
    laeuft: bool
    manuell: bool = False
    quelle: str = AUTOMATISCH
    begruendung: str = ""
    erfasst_am: str = ""
    geaendert_am: str = ""

    @property
    def dauer(self) -> timedelta:
        return max(timedelta(0), self.ende - self.beginn)

    @property
    def herkunft(self) -> str:
        """Ein Satz, der die Zeitstempel dieser Sitzung belegt."""
        if self.quelle == MANUELL:
            grund = self.begruendung or "ohne Begruendung"
            wann = f" am {self.geaendert_am or self.erfasst_am}" if (self.geaendert_am or self.erfasst_am) else ""
            return f"von Hand erfasst{wann}: {grund}"
        if self.laeuft:
            return "automatisch gemessen -- Sitzung laeuft noch"
        if self.ende_geschaetzt:
            return "Beginn automatisch, Ende aus dem letzten Herzschlag (Rechner hart aus)"
        return "automatisch gemessen (Rechner an bis aus)"


@dataclass
class Tagesart:
    """Urlaub, Krankheit und aehnliches -- ganz- oder halbtags."""

    tag: date
    art: str
    anteil: float = 1.0   # 1.0 = ganzer Tag, 0.5 = halber Tag
    notiz: str = ""

    @property
    def beschriftung(self) -> str:
        text = TAGESART_TEXT.get(self.art, self.art)
        return text if self.anteil >= 1.0 else f"{text} (halbtags)"


@dataclass
class Luecke:
    """Eine bereits eingeordnete Pause zwischen zwei Sitzungen."""

    id: int
    beginn: datetime
    ende: datetime
    art: str
    notiz: str = ""
    quelle: str = RUECKFRAGE
    erfasst_am: str = ""

    @property
    def dauer(self) -> timedelta:
        return max(timedelta(0), self.ende - self.beginn)

    @property
    def herkunft(self) -> str:
        grund = self.notiz or "ohne Begruendung"
        wann = f" am {self.erfasst_am}" if self.erfasst_am else ""
        if self.quelle == MANUELL:
            return f"von Hand erfasst{wann}: {grund}"
        return f"Rechner war aus, auf Rueckfrage eingeordnet{wann}: {grund}"


@dataclass
class Protokolleintrag:
    """Eine Zeile des Aenderungsprotokolls."""

    id: int
    zeitpunkt: datetime
    benutzer: str
    rechner: str
    aktion: str
    gegenstand: str
    vorher: str
    nachher: str
    begruendung: str


class Datenbank:
    """Schmale Huelle um SQLite -- bewusst ohne ORM."""

    def __init__(self, pfad: Path | str | None = None) -> None:
        self.pfad = Path(pfad) if pfad else datenverzeichnis() / "zeiterfassung.db"
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        self._verbindung = sqlite3.connect(self.pfad, isolation_level=None)
        self._verbindung.row_factory = sqlite3.Row
        self._anlegen()

    def _anlegen(self) -> None:
        self._verbindung.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sitzungen (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                beginn           TEXT NOT NULL,
                ende             TEXT NOT NULL,
                ende_geschaetzt  INTEGER NOT NULL DEFAULT 0,
                laeuft           INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS protokoll (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                zeitpunkt    TEXT NOT NULL,
                benutzer     TEXT NOT NULL,
                rechner      TEXT NOT NULL,
                aktion       TEXT NOT NULL,
                gegenstand   TEXT NOT NULL,
                vorher       TEXT NOT NULL DEFAULT '',
                nachher      TEXT NOT NULL DEFAULT '',
                begruendung  TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS luecken (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                beginn  TEXT NOT NULL,
                ende    TEXT NOT NULL,
                art     TEXT NOT NULL,
                notiz   TEXT NOT NULL DEFAULT '',
                UNIQUE(beginn, ende)
            );
            CREATE TABLE IF NOT EXISTS tagesarten (
                tag     TEXT PRIMARY KEY,
                art     TEXT NOT NULL,
                anteil  REAL NOT NULL DEFAULT 1.0,
                notiz   TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_sitzungen_beginn ON sitzungen(beginn);
            CREATE INDEX IF NOT EXISTS idx_luecken_beginn   ON luecken(beginn);
            CREATE INDEX IF NOT EXISTS idx_protokoll_zeit    ON protokoll(zeitpunkt);
            """
        )

        self._nachruesten()

    def _nachruesten(self) -> None:
        """Ergaenzt Spalten aelterer Datenbestaende (schonende Migration)."""
        def spalten(tabelle: str) -> set[str]:
            return {
                zeile["name"]
                for zeile in self._verbindung.execute(f"PRAGMA table_info({tabelle})").fetchall()
            }

        nachzuruesten = {
            "sitzungen": (
                ("manuell", "INTEGER NOT NULL DEFAULT 0"),
                ("quelle", f"TEXT NOT NULL DEFAULT '{AUTOMATISCH}'"),
                ("begruendung", "TEXT NOT NULL DEFAULT ''"),
                ("erfasst_am", "TEXT NOT NULL DEFAULT ''"),
                ("geaendert_am", "TEXT NOT NULL DEFAULT ''"),
            ),
            "luecken": (
                ("quelle", f"TEXT NOT NULL DEFAULT '{RUECKFRAGE}'"),
                ("erfasst_am", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        for tabelle, spaltenliste in nachzuruesten.items():
            bekannt = spalten(tabelle)
            for name, beschreibung in spaltenliste:
                if name not in bekannt:
                    self._verbindung.execute(
                        f"ALTER TABLE {tabelle} ADD COLUMN {name} {beschreibung}"
                    )

    def schliessen(self) -> None:
        self._verbindung.close()

    # -- Sitzungen ----------------------------------------------------------
    def sitzung_beginnen(self, zeitpunkt: datetime) -> int:
        """Legt eine laufende Sitzung an und liefert deren Kennung."""
        cursor = self._verbindung.execute(
            "INSERT INTO sitzungen (beginn, ende, ende_geschaetzt, laeuft, quelle, erfasst_am) "
            "VALUES (?,?,0,1,?,?)",
            (als_text(zeitpunkt), als_text(zeitpunkt), AUTOMATISCH, als_text(zeitpunkt)),
        )
        return int(cursor.lastrowid)

    def herzschlag(self, sitzung_id: int, zeitpunkt: datetime) -> None:
        """Zieht das Ende der laufenden Sitzung nach."""
        self._verbindung.execute(
            "UPDATE sitzungen SET ende = ? WHERE id = ?", (als_text(zeitpunkt), sitzung_id)
        )

    def sitzung_beenden(self, sitzung_id: int, zeitpunkt: datetime, geschaetzt: bool = False) -> None:
        self._verbindung.execute(
            "UPDATE sitzungen SET ende = ?, ende_geschaetzt = ?, laeuft = 0 WHERE id = ?",
            (als_text(zeitpunkt), int(geschaetzt), sitzung_id),
        )

    def offene_sitzungen_abschliessen(self) -> None:  # noqa: D401
        """Nach einem harten Ausschalten stehen Sitzungen noch auf 'laeuft'.

        Das zuletzt geschriebene Ende ist der letzte Herzschlag -- also die
        letzte Zeit, zu der der Rechner nachweislich lief.
        """
        self._verbindung.execute(
            "UPDATE sitzungen SET laeuft = 0, ende_geschaetzt = 1, quelle = ? WHERE laeuft = 1",
            (GESCHAETZT,),
        )

    def sitzungen(self, von: datetime, bis: datetime) -> list[Sitzung]:
        """Alle Sitzungen, die den Zeitraum beruehren."""
        zeilen = self._verbindung.execute(
            "SELECT * FROM sitzungen WHERE ende >= ? AND beginn <= ? ORDER BY beginn",
            (als_text(von), als_text(bis)),
        ).fetchall()
        return [self._sitzung(z) for z in zeilen]

    @staticmethod
    def _sitzung(zeile) -> Sitzung:
        vorhanden = zeile.keys()

        def wert(name: str, ersatz: str = "") -> str:
            return str(zeile[name]) if name in vorhanden and zeile[name] is not None else ersatz

        return Sitzung(
            id=int(zeile["id"]),
            beginn=als_zeit(zeile["beginn"]),
            ende=als_zeit(zeile["ende"]),
            ende_geschaetzt=bool(zeile["ende_geschaetzt"]),
            laeuft=bool(zeile["laeuft"]),
            manuell=bool(zeile["manuell"]) if "manuell" in vorhanden else False,
            quelle=wert("quelle", AUTOMATISCH),
            begruendung=wert("begruendung"),
            erfasst_am=wert("erfasst_am"),
            geaendert_am=wert("geaendert_am"),
        )

    def letzte_sitzung_vor(self, zeitpunkt: datetime) -> Sitzung | None:
        zeile = self._verbindung.execute(
            "SELECT * FROM sitzungen WHERE beginn < ? ORDER BY beginn DESC LIMIT 1",
            (als_text(zeitpunkt),),
        ).fetchone()
        if zeile is None:
            return None
        return self._sitzung(zeile)

    # -- Manuelle Korrektur von Sitzungen -----------------------------------
    def sitzung_anlegen(
        self, beginn: datetime, ende: datetime, begruendung: str, jetzt: datetime | None = None
    ) -> int:
        """Von Hand nachgetragener Zeitraum (z. B. vergessener Start).

        Ohne Begruendung geht das nicht -- ein Bericht, der nicht sagt, warum
        eine Zeit von Hand entstanden ist, ist wertlos.
        """
        begruendung = _pflicht(begruendung)
        jetzt = jetzt or datetime.now()
        cursor = self._verbindung.execute(
            "INSERT INTO sitzungen (beginn, ende, ende_geschaetzt, laeuft, manuell, quelle, "
            "begruendung, erfasst_am) VALUES (?,?,0,0,1,?,?,?)",
            (als_text(beginn), als_text(ende), MANUELL, begruendung, als_text(jetzt)),
        )
        self.protokollieren(
            ANGELEGT,
            f"Arbeitszeit {beginn:%d.%m.%Y}",
            "",
            f"{beginn:%H:%M} bis {ende:%H:%M}",
            begruendung,
            jetzt,
        )
        return int(cursor.lastrowid)

    def sitzung_aendern(
        self,
        sitzung_id: int,
        beginn: datetime,
        ende: datetime,
        begruendung: str,
        jetzt: datetime | None = None,
    ) -> None:
        """Korrigiert die Zeiten einer Sitzung und schreibt das Protokoll fort."""
        begruendung = _pflicht(begruendung)
        jetzt = jetzt or datetime.now()
        vorher = self.sitzung(sitzung_id)
        self._verbindung.execute(
            "UPDATE sitzungen SET beginn = ?, ende = ?, ende_geschaetzt = 0, laeuft = 0, "
            "manuell = 1, quelle = ?, begruendung = ?, geaendert_am = ? WHERE id = ?",
            (als_text(beginn), als_text(ende), MANUELL, begruendung, als_text(jetzt), sitzung_id),
        )
        self.protokollieren(
            GEAENDERT,
            f"Arbeitszeit {beginn:%d.%m.%Y}",
            f"{vorher.beginn:%H:%M} bis {vorher.ende:%H:%M}" if vorher else "",
            f"{beginn:%H:%M} bis {ende:%H:%M}",
            begruendung,
            jetzt,
        )

    def sitzung_loeschen(
        self, sitzung_id: int, begruendung: str, jetzt: datetime | None = None
    ) -> None:
        begruendung = _pflicht(begruendung)
        vorher = self.sitzung(sitzung_id)
        self._verbindung.execute("DELETE FROM sitzungen WHERE id = ?", (sitzung_id,))
        if vorher is not None:
            self.protokollieren(
                GELOESCHT,
                f"Arbeitszeit {vorher.beginn:%d.%m.%Y}",
                f"{vorher.beginn:%H:%M} bis {vorher.ende:%H:%M}",
                "",
                begruendung,
                jetzt,
            )

    def sitzung(self, sitzung_id: int) -> Sitzung | None:
        zeile = self._verbindung.execute(
            "SELECT * FROM sitzungen WHERE id = ?", (sitzung_id,)
        ).fetchone()
        return self._sitzung(zeile) if zeile else None

    # -- Luecken ------------------------------------------------------------
    def luecke_eintragen(
        self,
        beginn: datetime,
        ende: datetime,
        art: str,
        notiz: str = "",
        quelle: str = RUECKFRAGE,
        jetzt: datetime | None = None,
        protokollieren: bool = True,
    ) -> None:
        """Ordnet eine Zeit ohne Rechnerbetrieb ein.

        ``quelle`` haelt fest, ob die Einordnung aus der Rueckfrage beim Start
        stammt oder spaeter von Hand nachgetragen wurde.
        """
        jetzt = jetzt or datetime.now()
        self._verbindung.execute(
            "INSERT OR REPLACE INTO luecken (beginn, ende, art, notiz, quelle, erfasst_am) "
            "VALUES (?,?,?,?,?,?)",
            (als_text(beginn), als_text(ende), art, notiz, quelle, als_text(jetzt)),
        )
        if protokollieren:
            self.protokollieren(
                ANGELEGT,
                f"{art.capitalize()} {beginn:%d.%m.%Y}",
                "",
                f"{beginn:%H:%M} bis {ende:%H:%M}",
                notiz or QUELLE_TEXT.get(quelle, quelle),
                jetzt,
            )

    def luecken(self, von: datetime, bis: datetime) -> list[Luecke]:
        zeilen = self._verbindung.execute(
            "SELECT * FROM luecken WHERE ende >= ? AND beginn <= ? ORDER BY beginn",
            (als_text(von), als_text(bis)),
        ).fetchall()
        return [self._luecke(z) for z in zeilen]

    @staticmethod
    def _luecke(zeile) -> Luecke:
        vorhanden = zeile.keys()
        return Luecke(
            id=int(zeile["id"]),
            beginn=als_zeit(zeile["beginn"]),
            ende=als_zeit(zeile["ende"]),
            art=str(zeile["art"]),
            notiz=str(zeile["notiz"]),
            quelle=str(zeile["quelle"]) if "quelle" in vorhanden else RUECKFRAGE,
            erfasst_am=str(zeile["erfasst_am"]) if "erfasst_am" in vorhanden else "",
        )

    def ist_eingeordnet(self, beginn: datetime, ende: datetime) -> bool:
        zeile = self._verbindung.execute(
            "SELECT 1 FROM luecken WHERE beginn = ? AND ende = ?",
            (als_text(beginn), als_text(ende)),
        ).fetchone()
        return zeile is not None

    def luecke_aendern(
        self,
        luecke_id: int,
        beginn: datetime,
        ende: datetime,
        art: str,
        notiz: str = "",
        jetzt: datetime | None = None,
    ) -> None:
        notiz = _pflicht(notiz)
        jetzt = jetzt or datetime.now()
        vorher = self.luecke(luecke_id)
        self._verbindung.execute(
            "UPDATE luecken SET beginn = ?, ende = ?, art = ?, notiz = ?, quelle = ?, "
            "erfasst_am = ? WHERE id = ?",
            (als_text(beginn), als_text(ende), art, notiz, MANUELL, als_text(jetzt), luecke_id),
        )
        self.protokollieren(
            GEAENDERT,
            f"{art.capitalize()} {beginn:%d.%m.%Y}",
            f"{vorher.beginn:%H:%M} bis {vorher.ende:%H:%M} ({vorher.art})" if vorher else "",
            f"{beginn:%H:%M} bis {ende:%H:%M} ({art})",
            notiz,
            jetzt,
        )

    def luecke_loeschen(self, luecke_id: int, begruendung: str, jetzt: datetime | None = None) -> None:
        begruendung = _pflicht(begruendung)
        vorher = self.luecke(luecke_id)
        self._verbindung.execute("DELETE FROM luecken WHERE id = ?", (luecke_id,))
        if vorher is not None:
            self.protokollieren(
                GELOESCHT,
                f"{vorher.art.capitalize()} {vorher.beginn:%d.%m.%Y}",
                f"{vorher.beginn:%H:%M} bis {vorher.ende:%H:%M}",
                "",
                begruendung,
                jetzt,
            )

    def luecke(self, luecke_id: int) -> Luecke | None:
        zeile = self._verbindung.execute(
            "SELECT * FROM luecken WHERE id = ?", (luecke_id,)
        ).fetchone()
        return self._luecke(zeile) if zeile else None

    # -- Tagesarten (Urlaub, Krank, Feiertag ...) ---------------------------
    def tagesart_setzen(
        self,
        tag: date,
        art: str,
        anteil: float = 1.0,
        notiz: str = "",
        jetzt: datetime | None = None,
    ) -> None:
        """Setzt die Tagesart.  ``ARBEITSTAG`` loescht den Eintrag wieder."""
        if art == ARBEITSTAG:
            self.tagesart_loeschen(tag, notiz, jetzt)
            return
        vorher = self.tagesart(tag)
        self._verbindung.execute(
            "INSERT OR REPLACE INTO tagesarten (tag, art, anteil, notiz) VALUES (?,?,?,?)",
            (tag.isoformat(), art, float(anteil), notiz),
        )
        self.protokollieren(
            GEAENDERT if vorher else ANGELEGT,
            f"Tagesart {tag:%d.%m.%Y}",
            vorher.beschriftung if vorher else "Arbeitstag",
            TAGESART_TEXT.get(art, art) + ("" if anteil >= 1.0 else " (halbtags)"),
            notiz or TAGESART_TEXT.get(art, art),
            jetzt,
        )

    def tagesart_loeschen(
        self, tag: date, begruendung: str = "", jetzt: datetime | None = None
    ) -> None:
        vorher = self.tagesart(tag)
        self._verbindung.execute("DELETE FROM tagesarten WHERE tag = ?", (tag.isoformat(),))
        if vorher is not None:
            self.protokollieren(
                GELOESCHT,
                f"Tagesart {tag:%d.%m.%Y}",
                vorher.beschriftung,
                "Arbeitstag",
                begruendung or "auf Arbeitstag zurueckgesetzt",
                jetzt,
            )

    def tagesart(self, tag: date) -> Tagesart | None:
        zeile = self._verbindung.execute(
            "SELECT * FROM tagesarten WHERE tag = ?", (tag.isoformat(),)
        ).fetchone()
        return self._tagesart(zeile) if zeile else None

    def tagesarten(self, von: date, bis: date) -> dict[date, Tagesart]:
        zeilen = self._verbindung.execute(
            "SELECT * FROM tagesarten WHERE tag BETWEEN ? AND ? ORDER BY tag",
            (von.isoformat(), bis.isoformat()),
        ).fetchall()
        return {als_tag(z["tag"]): self._tagesart(z) for z in zeilen}

    @staticmethod
    def _tagesart(zeile) -> Tagesart:
        return Tagesart(
            tag=als_tag(zeile["tag"]),
            art=str(zeile["art"]),
            anteil=float(zeile["anteil"]),
            notiz=str(zeile["notiz"]),
        )

    # -- Aenderungsprotokoll ------------------------------------------------
    def protokollieren(
        self,
        aktion: str,
        gegenstand: str,
        vorher: str = "",
        nachher: str = "",
        begruendung: str = "",
        jetzt: datetime | None = None,
    ) -> None:
        """Schreibt einen Eintrag fort -- das Protokoll wird nie geaendert."""
        self._verbindung.execute(
            "INSERT INTO protokoll (zeitpunkt, benutzer, rechner, aktion, gegenstand, vorher, "
            "nachher, begruendung) VALUES (?,?,?,?,?,?,?,?)",
            (
                als_text(jetzt or datetime.now()),
                benutzer(),
                rechnername(),
                aktion,
                gegenstand,
                vorher,
                nachher,
                begruendung,
            ),
        )

    def protokoll(self, von: datetime | None = None, bis: datetime | None = None) -> list[Protokolleintrag]:
        if von is None and bis is None:
            zeilen = self._verbindung.execute(
                "SELECT * FROM protokoll ORDER BY zeitpunkt, id"
            ).fetchall()
        else:
            zeilen = self._verbindung.execute(
                "SELECT * FROM protokoll WHERE zeitpunkt BETWEEN ? AND ? ORDER BY zeitpunkt, id",
                (als_text(von or datetime.min), als_text(bis or datetime.max.replace(microsecond=0))),
            ).fetchall()
        return [
            Protokolleintrag(
                id=int(z["id"]),
                zeitpunkt=als_zeit(z["zeitpunkt"]),
                benutzer=str(z["benutzer"]),
                rechner=str(z["rechner"]),
                aktion=str(z["aktion"]),
                gegenstand=str(z["gegenstand"]),
                vorher=str(z["vorher"]),
                nachher=str(z["nachher"]),
                begruendung=str(z["begruendung"]),
            )
            for z in zeilen
        ]

    # -- Auswertung ---------------------------------------------------------
    def erster_tag(self) -> date | None:
        zeile = self._verbindung.execute("SELECT MIN(beginn) AS m FROM sitzungen").fetchone()
        if zeile is None or zeile["m"] is None:
            return None
        return als_zeit(zeile["m"]).date()
