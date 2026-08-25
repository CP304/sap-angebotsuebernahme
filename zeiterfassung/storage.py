"""Datenhaltung der Zeiterfassung (SQLite).

Zwei Tabellen genuegen:

``sitzungen``  Eine Zeile je Einschaltzeitraum des Rechners.  ``ende`` wird
               fortlaufend mit dem Herzschlag nachgezogen, damit nach einem
               Stromausfall der letzte bekannte Stand erhalten bleibt.
``luecken``    Die Zeit zwischen zwei Sitzungen, sobald der Benutzer sie
               eingeordnet hat (Arbeit, Pause oder Abwesenheit).

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


def als_text(zeitpunkt: datetime) -> str:
    return zeitpunkt.strftime(ZEITFORMAT)


def als_zeit(text: str) -> datetime:
    return datetime.strptime(text, ZEITFORMAT)


@dataclass
class Sitzung:
    """Ein Zeitraum, in dem der Rechner nachweislich lief."""

    id: int
    beginn: datetime
    ende: datetime
    ende_geschaetzt: bool
    laeuft: bool

    @property
    def dauer(self) -> timedelta:
        return max(timedelta(0), self.ende - self.beginn)


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
            CREATE INDEX IF NOT EXISTS idx_sitzungen_beginn ON sitzungen(beginn);
            CREATE INDEX IF NOT EXISTS idx_luecken_beginn   ON luecken(beginn);
            """
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
        return [
            Sitzung(
                id=int(z["id"]),
                beginn=als_zeit(z["beginn"]),
                ende=als_zeit(z["ende"]),
                ende_geschaetzt=bool(z["ende_geschaetzt"]),
                laeuft=bool(z["laeuft"]),
            )
            for z in zeilen
        ]

    def letzte_sitzung_vor(self, zeitpunkt: datetime) -> Sitzung | None:
        zeile = self._verbindung.execute(
            "SELECT * FROM sitzungen WHERE beginn < ? ORDER BY beginn DESC LIMIT 1",
            (als_text(zeitpunkt),),
        ).fetchone()
        if zeile is None:
            return None
        return Sitzung(
            id=int(zeile["id"]),
            beginn=als_zeit(zeile["beginn"]),
            ende=als_zeit(zeile["ende"]),
            ende_geschaetzt=bool(zeile["ende_geschaetzt"]),
            laeuft=bool(zeile["laeuft"]),
        )

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

    # -- Auswertung ---------------------------------------------------------
    def erster_tag(self) -> date | None:
        zeile = self._verbindung.execute("SELECT MIN(beginn) AS m FROM sitzungen").fetchone()
        if zeile is None or zeile["m"] is None:
            return None
        return als_zeit(zeile["m"]).date()
