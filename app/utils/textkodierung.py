"""Bytes zu Text -- mit verlaesslicher Erkennung der Kodierung.

Der Fehler, um den es hier geht
-------------------------------
Windows und SAP liefern Textdateien haeufig als UTF-16.  Das betrifft
genau die Wege, auf denen Angebote und Aufzeichnungen ins Werkzeug
kommen:

* Excel: "Unicode Text (*.txt)" schreibt UTF-16LE mit Byte-Order-Mark.
* SAP-Listexport (Datei -> Lokale Datei) ebenso.
* Die Skript-Aufzeichnung des SAP GUI schreibt ihre .vbs in UTF-16LE.

Solche Dateien beginnen mit den Bytes ``FF FE``, und jedes Zeichen belegt
zwei Bytes -- bei lateinischer Schrift ist das zweite davon ``00``.

Die frueher uebliche Kandidatenliste ("utf-8-sig", "utf-8", "cp1252",
"latin-1") faellt darauf herein: ``utf-8`` scheitert zwar an ``FF FE``,
aber ``cp1252`` nimmt *jedes* Byte an und meldet keinen Fehler.  Das
Ergebnis ist Text der Form::

    'ÿþA\\x00n\\x00g\\x00e\\x00b\\x00o\\x00t\\x00'

Auf dem Bildschirm sind das die bekannten leeren Rechtecke und
Zeichensalat.  Schlimmer als die Anzeige ist die Folge: die
Tabellenerkennung findet in so einem Text keine Spalte und keine Zahl,
das Angebot ergibt null Positionen, und die .vbs-Aufzeichnung ergibt null
Felder -- ohne dass irgendwo ein Kodierungsproblem sichtbar wuerde.  Der
Anwender sieht nur "nichts erkannt" und hat keinen Anhaltspunkt, woran es
lag.

Deshalb wird hier zuerst die Byte-Order-Mark ausgewertet, danach eine
Zweibyte-Kodierung ohne Marke erkannt, und erst zuletzt die uebliche
Liste durchprobiert.  Als Sicherung gilt: ein Ergebnis, in dem noch
Nullzeichen stehen, ist nicht richtig dekodiert -- egal wie fehlerfrei
der Codec sich gab.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["decode_bytes", "entferne_nullzeichen", "UEBLICHE_KODIERUNGEN"]

#: Kodierungen ohne Erkennungsmarke, in der Reihenfolge ihrer
#: Wahrscheinlichkeit fuer deutsche Belege.  ``latin-1`` steht zuletzt,
#: weil es jedes Byte annimmt und deshalb nie scheitert.
UEBLICHE_KODIERUNGEN = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: Byte-Order-Marks.  Die vier Bytes langen Marken stehen zuerst, denn die
#: UTF-32LE-Marke ``FF FE 00 00`` beginnt mit der UTF-16LE-Marke ``FF FE``
#: -- in umgekehrter Reihenfolge wuerde UTF-32 nie erkannt.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

#: So viele Bytes werden fuer die Erkennung ohne Marke betrachtet.
_PROBE = 4096

#: Ab diesem Anteil an Nullbytes in der Probe wird eine Zweibyte-Kodierung
#: angenommen.  Lateinische Schrift in UTF-16 liegt bei knapp der Haelfte;
#: ein Drittel laesst Luft fuer Umlaute und gemischte Inhalte, ohne dass
#: eine gewoehnliche Textdatei mit einem verirrten Nullbyte hereinfaellt.
_NULL_ANTEIL = 0.30

#: Ab diesem Anteil an Nullzeichen gilt ein dekodiertes Ergebnis als
#: falsch geraten.  Echter Text enthaelt keine Nullzeichen.
_NULL_ANTEIL_TEXT = 0.05


def _kodierung_aus_bom(data: bytes) -> str:
    """Welche Kodierung meldet die Byte-Order-Mark -- falls eine dasteht?"""
    for marke, kodierung in _BOMS:
        if data.startswith(marke):
            return kodierung
    return ""


def _zweibyte_ohne_bom(data: bytes) -> str:
    """UTF-16 ohne Erkennungsmarke -- an der Lage der Nullbytes.

    Nicht jede UTF-16-Datei traegt eine Marke; SAP-Exporte lassen sie
    gelegentlich weg.  Bei lateinischer Schrift steht dann jedes zweite
    Byte auf Null, und *welches* der beiden verraet die Bytereihenfolge:
    bei UTF-16LE ("A" -> ``41 00``) sind es die ungeraden Stellen, bei
    UTF-16BE ("A" -> ``00 41``) die geraden.

    Verlangt wird ein deutliches Verhaeltnis.  Ist die Lage uneindeutig,
    wird nichts behauptet -- dann entscheidet die uebliche Liste.
    """
    probe = data[:_PROBE]
    if len(probe) < 4:
        return ""
    gerade = sum(1 for i in range(0, len(probe), 2) if probe[i] == 0)
    ungerade = sum(1 for i in range(1, len(probe), 2) if probe[i] == 0)
    if (gerade + ungerade) < len(probe) * _NULL_ANTEIL:
        return ""
    if ungerade > gerade * 4:
        return "utf-16-le"
    if gerade > ungerade * 4:
        return "utf-16-be"
    return ""


def _wirkt_unentschluesselt(text: str) -> bool:
    """Stehen im Ergebnis noch Nullzeichen?

    Dann hat ein Codec zwar keinen Fehler gemeldet, aber trotzdem das
    Falsche geliefert -- ``cp1252`` und ``latin-1`` nehmen jedes Byte an,
    auch die Fuellbytes einer UTF-16-Datei.
    """
    if not text:
        return False
    return text.count("\x00") > len(text) * _NULL_ANTEIL_TEXT


def decode_bytes(data: bytes) -> tuple[str, str, str]:
    """Bytes in Text wandeln und dabei die Kodierung bestimmen.

    Liefert ``(text, kodierung, warnung)``.  Die Warnung ist leer, solange
    die Kodierung zweifelsfrei war; sonst nennt sie in einem Satz, was
    unsicher ist.  Es wird nie eine Ausnahme ausgeloest: notfalls wird
    ``latin-1`` mit Ersatzzeichen verwendet, denn ein teilweise lesbarer
    Text ist immer noch besser als ein abgebrochener Import.
    """
    if not data:
        return "", "utf-8", ""

    # 1. Die Datei sagt selbst, was sie ist.
    kodierung = _kodierung_aus_bom(data)
    if kodierung:
        try:
            text = data.decode(kodierung)
        except UnicodeDecodeError:
            # Marke da, Inhalt passt nicht dazu: die Datei ist beschaedigt
            # oder zusammenkopiert.  Weiter mit der ueblichen Liste.
            logger.warning("Byte-Order-Mark meldet %s, der Inhalt passt "
                           "nicht dazu", kodierung)
        else:
            return text, kodierung, ""

    # 2. Zweibyte-Kodierung ohne Marke.
    kodierung = _zweibyte_ohne_bom(data)
    if kodierung:
        try:
            text = data.decode(kodierung)
        except UnicodeDecodeError:
            pass
        else:
            return text, kodierung, ""

    # 3. Die ueblichen Kodierungen -- mit Gegenprobe.
    notloesung: tuple[str, str] | None = None
    for kandidat in UEBLICHE_KODIERUNGEN:
        try:
            text = data.decode(kandidat)
        except UnicodeDecodeError:
            continue
        if _wirkt_unentschluesselt(text):
            # Angenommen, aber sichtbar falsch.  Gemerkt als letzte
            # Zuflucht, die Suche geht weiter.
            if notloesung is None:
                notloesung = (text, kandidat)
            continue
        return text, kandidat, ""

    if notloesung is not None:
        text, kandidat = notloesung
        return (entferne_nullzeichen(text), kandidat,
                "Die Kodierung der Datei liess sich nicht bestimmen -- der "
                "Text kann unvollstaendig sein. Bitte die Datei als UTF-8 "
                "oder Excel-Arbeitsmappe speichern und erneut einlesen.")

    return (data.decode("latin-1", "replace"), "latin-1",
            "Kodierung konnte nicht sicher bestimmt werden -- Umlaute pruefen.")


def entferne_nullzeichen(text: str) -> str:
    """Nullzeichen aus bereits falsch dekodiertem Text entfernen.

    Rettungsanker fuer Text, der nicht mehr als Bytes vorliegt -- etwa
    weil er aus einem Editor in ein Eingabefeld kopiert wurde, der die
    UTF-16-Datei seinerseits falsch geoeffnet hat.  Aus
    ``'ÿþs\\x00e\\x00s\\x00s\\x00'`` wird wieder ``'sess'``; die
    fuehrende Marke faellt mit weg.

    Das stellt Umlaute nicht wieder her -- dafuer muessten die Bytes
    vorliegen.  Es macht die Zeilen aber wieder auswertbar, und das ist
    der Unterschied zwischen "nichts erkannt" und "fast alles erkannt".
    """
    if "\x00" not in text:
        return text
    bereinigt = text.replace("\x00", "")
    # Die als Text durchgereichte Byte-Order-Mark, in beiden Lesarten.
    for marke in ("﻿", "ÿþ", "þÿ"):
        if bereinigt.startswith(marke):
            bereinigt = bereinigt[len(marke):]
    return bereinigt
