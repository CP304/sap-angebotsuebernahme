"""Erzeugt realistische Beispielangebote zum Ausprobieren.

    python sample_data/erzeuge_beispiele.py

Die Dateien landen in ``sample_data/erzeugt/``.  Sie decken bewusst sehr
unterschiedliche Formate ab -- genau das ist im Alltag das Problem:

    1. Klassische Excel-Preisliste mit Kopfzeile
    2. Excel OHNE Kopfzeile (Spalten nur ueber Datentypen erkennbar)
    3. PDF-Angebot mit Spaltenlayout
    4. E-Mail (.eml) mit formloser Preismitteilung im Text
    5. E-Mail (.eml) mit Excel-Anhang
    6. Reiner Text (fuer "Text einfuegen")
    7. CSV mit Semikolon und deutschen Zahlen
    8. Excel mit Staffelpreisen und Summenzeile (Stoerfaelle)

Die verwendeten Materialnummern passen zum eingebauten Testsystem
(Mock-SAP), damit der Alt/Neu-Vergleich sofort etwas anzeigt.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

ZIEL = Path(__file__).resolve().parent / "erzeugt"

HEUTE = date.today()
GUELTIG_AB = (HEUTE.replace(day=1) + timedelta(days=32)).replace(day=1)


def _datum(tag: date) -> str:
    return tag.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# 1 + 2 + 8: Excel
# ---------------------------------------------------------------------------

def excel_mit_kopfzeile(pfad: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Preisliste 2026"

    blatt["A1"] = "Muster Dichtungstechnik GmbH"
    blatt["A1"].font = Font(bold=True, size=14)
    blatt["A2"] = "Industriestrasse 14, 33602 Bielefeld"
    blatt["A4"] = "Angebot Nr.:"
    blatt["B4"] = "ANG-2026-04711"
    blatt["A5"] = "Angebotsdatum:"
    blatt["B5"] = _datum(HEUTE)
    blatt["A6"] = "Gueltig bis:"
    blatt["B6"] = _datum(HEUTE + timedelta(days=90))
    blatt["A7"] = "Waehrung:"
    blatt["B7"] = "EUR"
    blatt["D4"] = "Zahlungsbedingungen:"
    blatt["E4"] = "30 Tage netto, 2 % Skonto bei 10 Tagen"
    blatt["D5"] = "Incoterm:"
    blatt["E5"] = "FCA Bielefeld"
    blatt["D6"] = "Ansprechpartner:"
    blatt["E6"] = "T. Wagner, vertrieb@muster-dichtungstechnik.de"

    kopf = ["Pos.", "Materialnummer", "Artikelnummer Lieferant", "Bezeichnung",
            "Menge", "ME", "Preis", "PE", "Mindestmenge", "Lieferzeit",
            "Gueltig ab", "Bemerkung"]
    for spalte, text in enumerate(kopf, start=1):
        zelle = blatt.cell(row=9, column=spalte, value=text)
        zelle.font = Font(bold=True)
        zelle.alignment = Alignment(horizontal="center")

    zeilen = [
        (10, "47110001", "DR-40527-NBR", "Dichtring NBR 40x52x7", 500, "St", "12,85", 1,
         50, "14 Tage", _datum(GUELTIG_AB), "Preis +3,6 %"),
        (20, "47110002", "OR-2503-FPM", "O-Ring Viton 25x3", 2000, "St", "8,90", 10,
         500, "14 Tage", _datum(GUELTIG_AB), "Preis je 10 Stueck"),
        (30, "47110003", "WDR-30477", "Wellendichtring FPM 30x47x7", 250, "St", "18,95", 1,
         25, "21 Tage", _datum(GUELTIG_AB), "unveraendert"),
        (40, "47110004", "FD-10060-2", "Flachdichtung Klingersil 100x60x2", 800, "St",
         "3,40", 1, 100, "10 Tage", _datum(GUELTIG_AB), "NEU im Sortiment"),
        (50, "", "SP-99-001", "Sonderdichtung nach Zeichnung", 20, "St", "145,00", 1,
         5, "35 Tage", _datum(GUELTIG_AB), "Materialnummer bitte ergaenzen"),
    ]
    for index, zeile in enumerate(zeilen, start=10):
        for spalte, wert in enumerate(zeile, start=1):
            blatt.cell(row=index, column=spalte, value=wert)

    for spalte, breite in zip("ABCDEFGHIJKL",
                              (6, 16, 22, 34, 8, 6, 10, 6, 12, 12, 12, 26)):
        blatt.column_dimensions[spalte].width = breite

    mappe.save(pfad)


def excel_ohne_kopfzeile(pfad: Path) -> None:
    """Preisliste ohne jede Ueberschrift -- nur ueber Datentypen erkennbar."""
    from openpyxl import Workbook

    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Preise"

    zeilen = [
        ("47110005", "Kugellager 6204-2RS", 1000, "ST", "4,55", 1, _datum(GUELTIG_AB)),
        ("49900010", "Hydraulikschlauch DN12 2SN", 300, "M", "7,20", 1, _datum(GUELTIG_AB)),
        ("48200111", "Gleitringdichtung KP-40", 40, "ST", "289,00", 1, _datum(GUELTIG_AB)),
    ]
    for index, zeile in enumerate(zeilen, start=1):
        for spalte, wert in enumerate(zeile, start=1):
            blatt.cell(row=index, column=spalte, value=wert)
    mappe.save(pfad)


def excel_mit_stoerfaellen(pfad: Path) -> None:
    """Staffelpreise, Zwischensummen, Leerzeilen, englische Zahlen."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Quotation"

    blatt["A1"] = "Nordtec Industriebedarf AG"
    blatt["A1"].font = Font(bold=True)
    blatt["A2"] = "Quotation No. Q-2026-8842"
    blatt["A3"] = f"Date: {HEUTE.strftime('%Y-%m-%d')}"
    blatt["A4"] = "Currency: EUR"

    kopf = ["Item", "Material", "Description", "Qty", "Unit", "Unit price",
            "Price unit", "Valid from"]
    for spalte, text in enumerate(kopf, start=1):
        blatt.cell(row=6, column=spalte, value=text).font = Font(bold=True)

    zeilen = [
        (10, "47110005", "Ball bearing 6204-2RS", 100, "PCS", "4.75", 1,
         GUELTIG_AB.strftime("%Y-%m-%d")),
        (10, "47110005", "Ball bearing 6204-2RS (ab 500 Stk)", 500, "PCS", "4.35", 1,
         GUELTIG_AB.strftime("%Y-%m-%d")),
        (10, "47110005", "Ball bearing 6204-2RS (ab 1000 Stk)", 1000, "PCS", "4.10", 1,
         GUELTIG_AB.strftime("%Y-%m-%d")),
        (None, None, None, None, None, None, None, None),
        (20, "49900010", "Hydraulic hose DN12 2SN", 500, "M", "6.95", 1,
         GUELTIG_AB.strftime("%Y-%m-%d")),
        (30, "47119999", "Unknown part (nicht im Materialstamm)", 10, "PCS", "99.00", 1,
         GUELTIG_AB.strftime("%Y-%m-%d")),
        (None, None, "Subtotal", None, None, "3,952.50", None, None),
        (None, None, "VAT 19 %", None, None, "750.98", None, None),
        (None, None, "Total", None, None, "4,703.48", None, None),
    ]
    for index, zeile in enumerate(zeilen, start=7):
        for spalte, wert in enumerate(zeile, start=1):
            if wert is not None:
                blatt.cell(row=index, column=spalte, value=wert)
    mappe.save(pfad)


# ---------------------------------------------------------------------------
# 3: PDF
# ---------------------------------------------------------------------------

def pdf_angebot(pfad: Path) -> None:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("  PyMuPDF fehlt – PDF-Beispiel wird uebersprungen.")
        return

    dokument = fitz.open()
    seite = dokument.new_page()

    def schreibe(x: float, y: float, text: str, groesse: float = 10,
                 fett: bool = False) -> None:
        seite.insert_text((x, y), text, fontsize=groesse,
                          fontname="helv" if not fett else "hebo")

    schreibe(50, 60, "Pumpen Weber GmbH & Co. KG", 16, True)
    schreibe(50, 78, "Werkstrasse 3  •  68199 Mannheim")
    schreibe(50, 92, "Telefon 0621 555-0  •  angebote@pumpen-weber.de")

    schreibe(50, 130, "ANGEBOT", 14, True)
    schreibe(50, 152, "Angebots-Nr.:  AG-2026-1188")
    schreibe(50, 168, f"Datum:  {_datum(HEUTE)}")
    schreibe(50, 184, f"Freibleibend gueltig bis:  {_datum(HEUTE + timedelta(days=60))}")
    schreibe(300, 152, "Kundennummer:  47110")
    schreibe(300, 168, "Zahlungsziel:  60 Tage netto")
    schreibe(300, 184, "Lieferbedingung:  CPT Werk")
    schreibe(300, 200, "Waehrung:  EUR")

    spalten = (50, 105, 175, 330, 385, 430, 490)
    ueberschriften = ("Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis", "gueltig ab")
    for x, text in zip(spalten, ueberschriften):
        schreibe(x, 240, text, 10, True)
    seite.draw_line(fitz.Point(50, 245), fitz.Point(560, 245))

    zeilen = (
        ("10", "48200110", "Kreiselpumpe Typ KP-40", "12", "ST", "1.298,00",
         _datum(GUELTIG_AB)),
        ("20", "48200111", "Gleitringdichtung KP-40", "40", "ST", "289,00",
         _datum(GUELTIG_AB)),
        ("30", "47110003", "Wellendichtring FPM 30x47x7", "100", "ST", "19,80",
         _datum(GUELTIG_AB)),
    )
    y = 265
    for zeile in zeilen:
        for x, text in zip(spalten, zeile):
            schreibe(x, y, text)
        y += 20

    schreibe(50, y + 30, "Preise verstehen sich netto ab Werk zuzueglich gesetzlicher "
                         "Mehrwertsteuer.")
    schreibe(50, y + 46, "Mindestbestellmenge Pumpen: 5 Stueck.  Lieferzeit 6 Wochen.")
    schreibe(50, y + 78, "Mit freundlichen Gruessen")
    schreibe(50, y + 94, "M. Weber, Vertriebsleitung")

    dokument.save(pfad)
    dokument.close()


# ---------------------------------------------------------------------------
# 4 + 5: E-Mails
# ---------------------------------------------------------------------------

_MAIL_TEXT = f"""Sehr geehrte Damen und Herren,

vielen Dank fuer Ihre Anfrage. Aufgrund gestiegener Rohstoffpreise muessen wir
unsere Preise zum {_datum(GUELTIG_AB)} wie folgt anpassen:

  - Artikel 47110001, Dichtring NBR 40x52x7: 12,85 EUR je Stueck (bisher 12,40 EUR)
  - Artikel 47110002, O-Ring Viton 25x3: 0,89 EUR/St ab 500 Stueck
  - Artikel 47110003, Wellendichtring FPM 30x47x7: 18,95 EUR/St (unveraendert)

Die Mindestbestellmenge betraegt weiterhin 50 Stueck, die Lieferzeit 14 Tage.
Unsere Angebotsnummer lautet ANG-2026-04712, das Angebot ist 60 Tage gueltig.
Zahlungsbedingungen: 30 Tage netto.

Mit freundlichen Gruessen

Thomas Wagner
Vertrieb
Muster Dichtungstechnik GmbH
Industriestrasse 14, 33602 Bielefeld
Telefon 0521 555-120
vertrieb@muster-dichtungstechnik.de

Diese E-Mail enthaelt vertrauliche Informationen. Sollten Sie nicht der richtige
Adressat sein, informieren Sie bitte den Absender und loeschen Sie diese Mail.
"""

_MAIL_HTML = f"""<html><body>
<p>Guten Tag,</p>
<p>anbei unsere aktualisierten Preise, gueltig ab {_datum(GUELTIG_AB)}.
Unsere Angebotsnummer: <b>Q-2026-8842</b>.</p>
<table border="1" cellpadding="4">
  <tr><th>Material</th><th>Bezeichnung</th><th>Menge</th><th>ME</th>
      <th>Preis</th><th>PE</th></tr>
  <tr><td>47110005</td><td>Kugellager 6204-2RS</td><td>1.000</td><td>ST</td>
      <td>4,35 EUR</td><td>1</td></tr>
  <tr><td>49900010</td><td>Hydraulikschlauch DN12 2SN</td><td>500</td><td>M</td>
      <td>7,10 EUR</td><td>1</td></tr>
</table>
<p>Zahlungsbedingungen: 30 Tage netto. Lieferzeit 21 Tage.</p>
<p>Freundliche Gruesse<br>Nordtec Industriebedarf AG</p>
</body></html>"""


def mail_freitext(pfad: Path) -> None:
    nachricht = EmailMessage()
    nachricht["From"] = "Thomas Wagner <vertrieb@muster-dichtungstechnik.de>"
    nachricht["To"] = "einkauf@unsere-firma.de"
    nachricht["Subject"] = f"Preisanpassung zum {_datum(GUELTIG_AB)} – Angebot ANG-2026-04712"
    nachricht["Date"] = HEUTE.strftime("%a, %d %b %Y 09:14:00 +0200")
    nachricht.set_content(_MAIL_TEXT)
    pfad.write_bytes(nachricht.as_bytes())


def mail_mit_anhang(pfad: Path, anhang: Path) -> None:
    nachricht = EmailMessage()
    nachricht["From"] = "Vertrieb Nordtec <vertrieb@nordtec-industriebedarf.de>"
    nachricht["To"] = "einkauf@unsere-firma.de"
    nachricht["Subject"] = "Angebot Q-2026-8842 – Preisliste im Anhang"
    nachricht["Date"] = HEUTE.strftime("%a, %d %b %Y 11:02:00 +0200")
    nachricht.set_content("Guten Tag,\n\nanbei unsere aktuelle Preisliste.\n\n"
                          "Freundliche Gruesse\nNordtec Industriebedarf AG\n")
    nachricht.add_alternative(_MAIL_HTML, subtype="html")
    if anhang.exists():
        nachricht.add_attachment(
            anhang.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=anhang.name)
    pfad.write_bytes(nachricht.as_bytes())


# ---------------------------------------------------------------------------
# 6 + 7: Text und CSV
# ---------------------------------------------------------------------------

def textdatei(pfad: Path) -> None:
    pfad.write_text(_MAIL_TEXT, encoding="utf-8")


def csv_datei(pfad: Path) -> None:
    with pfad.open("w", encoding="utf-8-sig", newline="") as datei:
        schreiber = csv.writer(datei, delimiter=";")
        schreiber.writerow(["Pos", "Material", "Bezeichnung", "Menge", "ME", "Preis",
                            "PE", "Waehrung", "Gueltig ab"])
        schreiber.writerow([10, "47110001", "Dichtring NBR 40x52x7", "500", "ST",
                            "12,85", "1", "EUR", _datum(GUELTIG_AB)])
        schreiber.writerow([20, "47110002", "O-Ring Viton 25x3", "2.000", "ST",
                            "8,90", "10", "EUR", _datum(GUELTIG_AB)])
        schreiber.writerow([30, "49900011", "Schlauchschelle W2 20-32", "1.500", "ST",
                            "0,42", "1", "EUR", _datum(GUELTIG_AB)])


# ---------------------------------------------------------------------------

def main() -> int:
    ZIEL.mkdir(parents=True, exist_ok=True)
    print(f"Beispieldateien werden erzeugt in: {ZIEL}")

    aufgaben = [
        ("Angebot_Muster_Dichtungstechnik.xlsx", excel_mit_kopfzeile),
        ("Preisliste_ohne_Kopfzeile.xlsx", excel_ohne_kopfzeile),
        ("Quotation_Nordtec_mit_Stoerfaellen.xlsx", excel_mit_stoerfaellen),
        ("Angebot_Pumpen_Weber.pdf", pdf_angebot),
        ("Preisanpassung_Muster.eml", mail_freitext),
        ("Preismitteilung.txt", textdatei),
        ("Preisliste_Muster.csv", csv_datei),
    ]
    # Hinweis: Die Windows-Konsole nutzt cp1252 -- deshalb bewusst nur ASCII
    # ausgeben, sonst bricht die Ausgabe mit einem UnicodeEncodeError ab.
    for name, funktion in aufgaben:
        ziel = ZIEL / name
        try:
            funktion(ziel)
            print(f"  [ok]     {name}")
        except Exception as fehler:  # noqa: BLE001 - Beispiele duerfen nie den Rest stoppen
            print(f"  [FEHLER] {name}: {fehler}")

    try:
        mail_mit_anhang(ZIEL / "Angebot_Nordtec_mit_Anhang.eml",
                        ZIEL / "Quotation_Nordtec_mit_Stoerfaellen.xlsx")
        print("  [ok]     Angebot_Nordtec_mit_Anhang.eml")
    except Exception as fehler:  # noqa: BLE001
        print(f"  [FEHLER] Angebot_Nordtec_mit_Anhang.eml: {fehler}")

    print("\nFertig. Ziehen Sie eine dieser Dateien einfach in das Anwendungsfenster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
