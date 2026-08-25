"""Datenhaltung der Zeiterfassung (SQLite).

Drei Tabellen genuegen:

``sitzungen``  Eine Zeile je Einschaltzeitraum des Rechners.  ``ende`` wird
               fortlaufend mit dem Herzschlag nachgezogen, damit nach einem
               Stromausfall der letzte bekannte Stand erhalten bleibt.
``luecken``    Die Zeit zwischen zwei Sitzungen, sobald der Benutzer sie
               eingeordnet hat (Arbeit, Pause oder Abwesenheit).
``tagesarten`` Ganze oder halbe Tage, an denen nicht gearbeitet wird --
               Urlaub, Krankheit, Feiertag, Gleittag, Dienstreise.

Alle Zeitstempel sind lokale Zeit im Format ``YYYY-MM-DD HH:MM:SS`` -- die
Zeiterfassung bezieht sich immer auf den Arbeitstag vor Ort.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import datenverzeichnis

ZEITFORMAT = "%Y-%m-%d %H:%M:%S"

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


@dataclass
class Sitzung:
    """Ein Zeitraum, in dem der Rechner nachweislich lief."""

    id: int
    beginn: datetime
    ende: datetime
    ende_geschaetzt: bool
    laeuft: bool
    manuell: bool = False

    @property
    def dauer(self) -> timedelta:
        return max(timedelta(0), self.ende - self.beginn)


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

    @property
    def dauer(self) -> timedelta:
        return max(timedelta(0), self.ende - self.beginn)


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
            """
        )

        self._nachruesten()

    def _nachruesten(self) -> None:
        """Ergaenzt Spalten aelterer Datenbestaende (schonende Migration)."""
        vorhanden = {
            zeile["name"]
            for zeile in self._verbindung.execute("PRAGMA table_info(sitzungen)").fetchall()
        }
        if "manuell" not in vorhanden:
            self._verbindung.execute(
                "ALTER TABLE sitzungen ADD COLUMN manuell INTEGER NOT NULL DEFAULT 0"
            )

    def schliessen(self) -> None:
        self._verbindung.close()

    # -- Sitzungen ----------------------------------------------------------
    def sitzung_beginnen(self, zeitpunkt: datetime) -> int:
        """Legt eine laufende Sitzung an und liefert deren Kennung."""
        cursor = self._verbindung.execute(
            "INSERT INTO sitzungen (beginn, ende, ende_geschaetzt, laeuft) VALUES (?,?,0,1)",
            (als_text(zeitpunkt), als_text(zeitpunkt)),
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

    def offene_sitzungen_abschliessen(self) -> None:
        """Nach einem harten Ausschalten stehen Sitzungen noch auf 'laeuft'.

        Das zuletzt geschriebene Ende ist der letzte Herzschlag -- also die
        letzte Zeit, zu der der Rechner nachweislich lief.
        """
        self._verbindung.execute(
            "UPDATE sitzungen SET laeuft = 0, ende_geschaetzt = 1 WHERE laeuft = 1"
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
        return Sitzung(
            id=int(zeile["id"]),
            beginn=als_zeit(zeile["beginn"]),
            ende=als_zeit(zeile["ende"]),
            ende_geschaetzt=bool(zeile["ende_geschaetzt"]),
            laeuft=bool(zeile["laeuft"]),
            manuell=bool(zeile["manuell"]) if "manuell" in zeile.keys() else False,
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
    def sitzung_anlegen(self, beginn: datetime, ende: datetime) -> int:
        """Von Hand nachgetragener Zeitraum (z. B. vergessener Start)."""
        cursor = self._verbindung.execute(
            "INSERT INTO sitzungen (beginn, ende, ende_geschaetzt, laeuft, manuell) "
            "VALUES (?,?,0,0,1)",
            (als_text(beginn), als_text(ende)),
        )
        return int(cursor.lastrowid)

    def sitzung_aendern(self, sitzung_id: int, beginn: datetime, ende: datetime) -> None:
        """Korrigiert die Zeiten einer Sitzung und merkt sich den Eingriff."""
        self._verbindung.execute(
            "UPDATE sitzungen SET beginn = ?, ende = ?, ende_geschaetzt = 0, laeuft = 0, "
            "manuell = 1 WHERE id = ?",
            (als_text(beginn), als_text(ende), sitzung_id),
        )

    def sitzung_loeschen(self, sitzung_id: int) -> None:
        self._verbindung.execute("DELETE FROM sitzungen WHERE id = ?", (sitzung_id,))

    # -- Luecken ------------------------------------------------------------
    def luecke_eintragen(self, beginn: datetime, ende: datetime, art: str, notiz: str = "") -> None:
        self._verbindung.execute(
            "INSERT OR REPLACE INTO luecken (beginn, ende, art, notiz) VALUES (?,?,?,?)",
            (als_text(beginn), als_text(ende), art, notiz),
        )

    def luecken(self, von: datetime, bis: datetime) -> list[Luecke]:
        zeilen = self._verbindung.execute(
            "SELECT * FROM luecken WHERE ende >= ? AND beginn <= ? ORDER BY beginn",
            (als_text(von), als_text(bis)),
        ).fetchall()
        return [
            Luecke(
                id=int(z["id"]),
                beginn=als_zeit(z["beginn"]),
                ende=als_zeit(z["ende"]),
                art=str(z["art"]),
                notiz=str(z["notiz"]),
            )
            for z in zeilen
        ]

    def ist_eingeordnet(self, beginn: datetime, ende: datetime) -> bool:
        zeile = self._verbindung.execute(
            "SELECT 1 FROM luecken WHERE beginn = ? AND ende = ?",
            (als_text(beginn), als_text(ende)),
        ).fetchone()
        return zeile is not None

    def luecke_aendern(self, luecke_id: int, beginn: datetime, ende: datetime, art: str, notiz: str = "") -> None:
        self._verbindung.execute(
            "UPDATE luecken SET beginn = ?, ende = ?, art = ?, notiz = ? WHERE id = ?",
            (als_text(beginn), als_text(ende), art, notiz, luecke_id),
        )

    def luecke_loeschen(self, luecke_id: int) -> None:
        self._verbindung.execute("DELETE FROM luecken WHERE id = ?", (luecke_id,))

    # -- Tagesarten (Urlaub, Krank, Feiertag ...) ---------------------------
    def tagesart_setzen(self, tag: date, art: str, anteil: float = 1.0, notiz: str = "") -> None:
        """Setzt die Tagesart.  ``ARBEITSTAG`` loescht den Eintrag wieder."""
        if art == ARBEITSTAG:
            self.tagesart_loeschen(tag)
            return
        self._verbindung.execute(
            "INSERT OR REPLACE INTO tagesarten (tag, art, anteil, notiz) VALUES (?,?,?,?)",
            (tag.isoformat(), art, float(anteil), notiz),
        )

    def tagesart_loeschen(self, tag: date) -> None:
        self._verbindung.execute("DELETE FROM tagesarten WHERE tag = ?", (tag.isoformat(),))

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

    # -- Auswertung ---------------------------------------------------------
    def erster_tag(self) -> date | None:
        zeile = self._verbindung.execute("SELECT MIN(beginn) AS m FROM sitzungen").fetchone()
        if zeile is None or zeile["m"] is None:
            return None
        return als_zeit(zeile["m"]).date()
