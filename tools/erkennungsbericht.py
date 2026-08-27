"""Erkennungsqualitaet an den EIGENEN Angeboten messen -- ohne sie herzugeben.

    python tools/erkennungsbericht.py ORDNER [-o bericht.txt] [--mit-dateinamen]

Wozu
----
Um die Belegerkennung zu verbessern, muss man wissen, woran sie scheitert.
Dafuer braucht es echte Angebote -- und die duerfen das Haus nicht
verlassen.  Beides zugleich geht, wenn die Messung dorthin kommt, wo die
Belege liegen, statt umgekehrt.

Dieses Werkzeug liest einen Ordner mit Angeboten, laesst die normale
Erkennung darueberlaufen und schreibt einen Bericht aus **Kennzahlen und
Strukturmerkmalen**.  Der Bericht ist eine gewoehnliche Textdatei: vor dem
Weitergeben in Ruhe durchlesen.

Was im Bericht steht
--------------------
* je Datei: Format, Groessenklasse, Seitenzahl, Anzahl erkannter Positionen
* welche Felder gefuellt blieben und welche leer
* Strukturmerkmale, die die Erkennung steuern: hat das PDF gezeichnete
  Tabellenlinien?  Enthaelt es ueberhaupt Text oder ist es ein Scan?
  Wie viele Spalten hat die erkannte Tabelle?  Welches Verfahren griff?
* die Kennungen der Befunde (``material_missing``), nicht deren Wortlaut
* am Ende die Trefferquote je Feld ueber alle Dateien

Was NICHT im Bericht steht
--------------------------
Keine Materialnummern, keine Preise, keine Mengen, keine Lieferanten,
keine Angebotsnummern, keine Beschreibungen, kein Rohtext -- und ohne
``--mit-dateinamen`` auch keine Dateinamen, weil in denen oft der
Lieferant steht.  Stattdessen "Datei 01".

Die Zuordnung Nummer -> Dateiname sehen nur Sie, mit
``--mit-dateinamen`` auf dem eigenen Rechner.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import Settings                       # noqa: E402
from app.services.offer_import_service import OfferImportService  # noqa: E402

#: Positionsfelder, auf die es beim Schreiben nach SAP ankommt.
_POSITIONSFELDER = (
    ("material_number", "Materialnummer"),
    ("description", "Bezeichnung"),
    ("quantity", "Menge"),
    ("uom", "Mengeneinheit"),
    ("price", "Preis"),
    ("price_unit", "Preiseinheit"),
    ("currency", "Waehrung"),
)

#: Kopffelder des Angebots.
_KOPFFELDER = (
    ("vendor_name", "Lieferant"),
    ("offer_number", "Angebotsnummer"),
    ("offer_date", "Angebotsdatum"),
    ("currency", "Waehrung"),
)

#: Endungen, die eingelesen werden.
_ENDUNGEN = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".eml", ".msg",
             ".docx", ".odt", ".ods", ".rtf", ".zip", ".htm", ".html", ".tsv"}


def _groessenklasse(pfad: Path) -> str:
    """Groessenordnung statt exakter Groesse -- die verraet sonst zu viel."""
    try:
        kb = pfad.stat().st_size / 1024
    except OSError:
        return "?"
    if kb < 50:
        return "< 50 KB"
    if kb < 250:
        return "50-250 KB"
    if kb < 1024:
        return "250 KB - 1 MB"
    return "> 1 MB"


def _pdf_merkmale(pfad: Path) -> list[str]:
    """Was an einem PDF die Erkennung steuert -- ohne seinen Inhalt."""
    merkmale: list[str] = []
    try:
        import fitz
    except ImportError:
        return ["PyMuPDF fehlt -- keine PDF-Merkmale"]
    try:
        with fitz.open(pfad) as dokument:
            merkmale.append(f"{dokument.page_count} Seite(n)")
            zeichen = 0
            linien = 0
            gedreht = 0
            for seite in dokument:
                zeichen += len(seite.get_text() or "")
                try:
                    linien += len(seite.get_drawings() or [])
                except Exception:      # noqa: BLE001 -- Merkmal, kein Muss
                    pass
                if getattr(seite, "rotation", 0):
                    gedreht += 1
            if zeichen < 20 * dokument.page_count:
                merkmale.append("KEIN Text -- vermutlich Scan (braucht OCR)")
            else:
                merkmale.append(f"Text vorhanden (~{zeichen // dokument.page_count} "
                                "Zeichen je Seite)")
            merkmale.append("gezeichnete Linien vorhanden" if linien > 12
                            else "kaum gezeichnete Linien (Tabelle ohne Rahmen)")
            if gedreht:
                merkmale.append(f"{gedreht} gedrehte Seite(n)")
    except Exception as fehler:        # noqa: BLE001 -- Bericht statt Absturz
        merkmale.append(f"nicht lesbar: {type(fehler).__name__}")
    return merkmale


#: Woerter, an denen eine Erkennungsnotiz ihre Art verraet.  Nur die Art
#: wird berichtet -- der Wortlaut der Notiz nie, denn er enthaelt die
#: erkannten Werte im Klartext.
_NOTIZARTEN = (
    ("Kopf erkannt", "Kopffeld aus dem Briefkopf erkannt"),
    ("Tabellenstruktur", "Positionen aus einer Tabellenstruktur"),
    ("Fliesstext", "Positionen aus Fliesstext"),
    ("Freitext", "Positionen aus Freitext"),
    ("zweispalt", "zweispaltiges Layout erkannt"),
    ("gedreht", "gedrehte Seite verarbeitet"),
    ("Profil", "Lieferantenprofil angewendet"),
    ("Anhang", "Anhang mitgelesen"),
    ("Staffel", "Staffelhinweis verarbeitet"),
    ("Preiseinheit", "Preiseinheit aus dem Text uebernommen"),
    ("OCR", "Texterkennung (OCR) verwendet"),
    ("Kodierung", "Kodierungshinweis"),
    ("Trennzeichen", "Trennzeichen bestimmt"),
    ("Spalte", "Spaltenzuordnung"),
)


def _dokumentmerkmale(angebot) -> list[str]:
    """Welche VERFAHREN gegriffen haben -- nie, was sie gefunden haben.

    Die Erkennungsnotizen der Anwendung nennen ihre Treffer im Klartext
    ("Kopf erkannt -- vendor_name: 'Muster GmbH'").  Sie gehoeren deshalb
    nicht in einen Bericht, der das Haus verlassen soll.

    Berichtet wird stattdessen nur, welcher ART eine Notiz war und wie
    oft.  Ausgewaehlt wird ueber eine Positivliste: was hier nicht
    ausdruecklich aufgefuehrt ist, wird als "sonstige" gezaehlt und nicht
    wiedergegeben.  Eine Sperrliste waere die falsche Richtung -- sie
    liesse durch, woran niemand gedacht hat.
    """
    arten: Counter = Counter()
    for notiz in (angebot.extraction_notes or []):
        text = str(notiz)
        for merkmal, klartext in _NOTIZARTEN:
            if merkmal.lower() in text.lower():
                arten[klartext] += 1
                break
        else:
            arten["sonstige Notiz"] += 1
    return [f"{klartext} ({anzahl}x)" if anzahl > 1 else klartext
            for klartext, anzahl in arten.most_common()]


def untersuche(pfad: Path, dienst: OfferImportService, nummer: int,
               mit_dateinamen: bool) -> tuple[list[str], Counter, Counter, int]:
    """Eine Datei einlesen und beschreiben.  Liefert (Zeilen, gefuellt, leer, n)."""
    bezeichnung = (pfad.name if mit_dateinamen
                   else f"Datei {nummer:02d}{pfad.suffix.lower()}")
    zeilen = [f"{bezeichnung}", f"    Groesse: {_groessenklasse(pfad)}"]

    if pfad.suffix.lower() == ".pdf":
        for merkmal in _pdf_merkmale(pfad):
            zeilen.append(f"    {merkmal}")

    gefuellt: Counter = Counter()
    leer: Counter = Counter()
    try:
        angebot = dienst.import_file(pfad)
    except Exception as fehler:        # noqa: BLE001 -- Bericht statt Absturz
        zeilen.append(f"    ABBRUCH beim Einlesen: {type(fehler).__name__}")
        return zeilen, gefuellt, leer, 0

    anzahl = len(angebot.positions)
    zeilen.append(f"    erkannte Positionen: {anzahl}")

    fehlende_kopffelder = [klartext for feld, klartext in _KOPFFELDER
                           if not getattr(angebot, feld, None)]
    zeilen.append("    Kopfdaten fehlen: "
                  + (", ".join(fehlende_kopffelder) if fehlende_kopffelder
                     else "keine"))

    for position in angebot.positions:
        for feld, klartext in _POSITIONSFELDER:
            wert = getattr(position, feld, None)
            if wert in (None, ""):
                leer[klartext] += 1
            else:
                gefuellt[klartext] += 1

    if anzahl:
        luecken = [f"{klartext} ({leer[klartext]}x)"
                   for _feld, klartext in _POSITIONSFELDER if leer[klartext]]
        zeilen.append("    Positionsfelder leer: "
                      + (", ".join(luecken) if luecken else "keine"))

    kennungen = Counter(befund.code for befund in angebot.issues)
    if kennungen:
        zeilen.append("    Befunde: "
                      + ", ".join(f"{code} ({anzahl_})"
                                  for code, anzahl_ in kennungen.most_common(6)))

    verfahren = _dokumentmerkmale(angebot)
    if verfahren:
        zeilen.append("    Verfahren: " + ", ".join(verfahren))

    return zeilen, gefuellt, leer, anzahl


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erkennungsqualitaet an eigenen Angeboten messen, "
                    "ohne die Angebote herauszugeben.")
    parser.add_argument("ordner", type=Path, help="Ordner mit Angeboten")
    parser.add_argument("-o", "--ausgabe", type=Path,
                        default=Path("erkennungsbericht.txt"),
                        help="Zieldatei (Standard: erkennungsbericht.txt)")
    parser.add_argument("--mit-dateinamen", action="store_true",
                        help="Dateinamen in den Bericht schreiben. NUR fuer "
                             "die eigene Analyse -- in Dateinamen steht oft "
                             "der Lieferant.")
    argumente = parser.parse_args()

    if not argumente.ordner.is_dir():
        print(f"Kein Ordner: {argumente.ordner}", file=sys.stderr)
        return 2

    logging.disable(logging.CRITICAL)
    dienst = OfferImportService(Settings())

    dateien = sorted(p for p in argumente.ordner.rglob("*")
                     if p.is_file() and p.suffix.lower() in _ENDUNGEN)
    if not dateien:
        print(f"Keine lesbaren Angebote in {argumente.ordner}", file=sys.stderr)
        return 1

    kopf = [
        "Erkennungsbericht der SAP-Angebotsuebernahme",
        "=" * 66,
        "",
        "Dieser Bericht enthaelt AUSSCHLIESSLICH Kennzahlen und",
        "Strukturmerkmale. Keine Materialnummern, Preise, Mengen,",
        "Lieferanten, Angebotsnummern, Beschreibungen oder Rohtexte.",
        "Bitte trotzdem vor dem Weitergeben durchlesen.",
        "",
        f"Untersucht: {len(dateien)} Datei(en)",
        "",
        "-" * 66,
        "",
    ]
    if argumente.mit_dateinamen:
        kopf.insert(7, "ACHTUNG: Mit --mit-dateinamen erzeugt -- die "
                       "Dateinamen stehen darin.")

    zeilen: list[str] = list(kopf)
    gefuellt_gesamt: Counter = Counter()
    leer_gesamt: Counter = Counter()
    positionen_gesamt = 0
    formate: Counter = Counter()

    for nummer, pfad in enumerate(dateien, start=1):
        datei_zeilen, gefuellt, leer, anzahl = untersuche(
            pfad, dienst, nummer, argumente.mit_dateinamen)
        zeilen.extend(datei_zeilen)
        zeilen.append("")
        gefuellt_gesamt.update(gefuellt)
        leer_gesamt.update(leer)
        positionen_gesamt += anzahl
        formate[pfad.suffix.lower()] += 1
        print(f"  {nummer:>3}/{len(dateien)}  {pfad.suffix.lower():<6} "
              f"{anzahl:>4} Position(en)")

    zeilen.extend(["-" * 66, "", "ZUSAMMENFASSUNG", ""])
    zeilen.append(f"Dateien: {len(dateien)}, Positionen insgesamt: "
                  f"{positionen_gesamt}")
    zeilen.append("Formate: " + ", ".join(f"{endung} {anzahl}"
                                          for endung, anzahl in formate.most_common()))
    zeilen.append("")
    zeilen.append("Trefferquote je Positionsfeld:")
    for _feld, klartext in _POSITIONSFELDER:
        treffer = gefuellt_gesamt[klartext]
        summe = treffer + leer_gesamt[klartext]
        if not summe:
            continue
        anteil = treffer / summe * 100
        balken = "#" * int(anteil / 4)
        zeilen.append(f"   {klartext:<16} {treffer:>4}/{summe:<4} "
                      f"{anteil:5.1f}%  {balken}")
    zeilen.append("")
    zeilen.append("Dateien ohne eine einzige Position sind der wichtigste")
    zeilen.append("Hinweis -- dort greift die Erkennung gar nicht.")
    zeilen.append("")

    argumente.ausgabe.write_text("\n".join(zeilen), encoding="utf-8")
    print()
    print(f"Bericht geschrieben: {argumente.ausgabe}")
    print("Bitte durchlesen, bevor Sie ihn weitergeben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
