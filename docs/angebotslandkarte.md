# Angebotslandkarte

Warum es dieses Dokument gibt: Die Frage „klappt auch das 23. Angebot?"
lässt sich nicht mit einer Liste von Formaten beantworten — die Liste ist
unendlich. Sie lässt sich nur beantworten, indem man die **Achsen**
benennt, entlang derer Angebote sich unterscheiden, und dann prüft, ob
das Werkzeug jede Achse entweder beherrscht **oder sichtbar aufgibt**.

Diese Datei ist die Arbeitsgrundlage dafür. Sie speist den kombinatorischen
Belegerzeuger in `tests/test_belegvielfalt.py` und wird ergänzt, wann immer
ein echter Beleg etwas zeigt, das hier fehlt.

## Der Maßstab

Ein Beleg hat drei mögliche Ausgänge:

| Ausgang | Bedeutung | Kosten |
|---|---|---|
| **richtig** | Werte stimmen | keine |
| **laut** | falsch oder unvollständig, **aber gemeldet** | Minuten Nacharbeit |
| **still** | falscher Wert **ohne Warnung** | falscher Preis in SAP, fällt Monate später auf |

**Das Ziel ist nicht 100 % richtig — das ist unerreichbar. Das Ziel ist
0 % still.** Eine höhere Trefferquote, die stille Fehler einführt, ist eine
Verschlechterung, kein Fortschritt.

## Achsen

### A. Datei und Transport
- Format: PDF (Text), PDF (Scan), Excel, CSV, TXT, Word, ODS/ODT, RTF, E-Mail, ZIP
- Angebot im Mailtext statt im Anhang; mehrere Anhänge; Anhang im Anhang
- Kodierung: UTF-8, UTF-8 mit BOM, Latin-1, Windows-1252
- Zeilenenden: Windows, Unix, gemischt

### B. Tabellenaufbau
- echte Tabelle · Text mit Tabulatoren · Fließtext · Bild/Scan
- **keine Kopfzeile**
- Kopfzeile über mehrere Zeilen (zweite Zeile trägt Einheiten)
- **Kopfzeile mitten in der Tabelle wiederholt** (Seitenumbruch)
- Übertragszeilen („Übertrag", „Summe Seite 2")
- Zwischenüberschriften (Artikelgruppen)
- Zwischensummen · Gesamtsummenzeile
- verbundene Zellen (Excel, Word `vMerge`)
- mehrzeilige Positionen (Beschreibung läuft über Folgezeilen)
- mehrere Tabellen in einem Beleg
- zweispaltiges Layout · gedrehte Seiten
- Leerspalten · Leerzeilen zwischen Positionen

### C. Trennung und Zahlen
- Trennzeichen: `;` `,` Tabulator `|`
- **Dezimalkomma gegen Feldtrenner** (der härteste Fall)
- Tausendertrennzeichen: `1.234,56` · `1,234.56` · `1 234,56` · `1234.56`
- Zahl mit angehängter Einheit: „500 ST", „1.000 kg"
- Zahl mit angehängter Währung: „2,95 EUR", „€ 2,95", „2,95 €"
- Beschreibung enthält das Trennzeichen (in Anführungszeichen)
- drei Nachkommastellen („2,950")

### D. Spaltenbenennung
- Synonyme je Feld (siehe `column_aliases` in `settings.py`)
- Abkürzungen: „EP", „GP", „ME", „PE", „WE"
- umbrochene Köpfe: „Preis-einheit", „Liefer-woche"
- Kopf trägt Zusatzinformation: „Preis EUR", „DDP 12345 Musterstadt (EUR/ST)"
- **unverstandenes Kürzel**: „Menge RS" → übernehmen, aber gelb
- doppelte Köpfe: „ME | ME | ME"
- durchnummerierte Mengenspalten: „Menge1 | Menge2 | Menge3"
- beliebige Spaltenreihenfolge (Menge vor Material, Position ganz hinten)

### E. Preisangabe
- Einzelpreis · Zeilensumme · beides
- **Staffelpreise**: als Zeilen · als Spaltenmatrix („ab 100 | ab 500")
- Preiseinheit: eigene Spalte · im Fußtext („Preise je 100 Stück") · fehlt
- **Preisspanne** („12,00 – 14,00 EUR")
- **„auf Anfrage"** / „a. A." / „on request" / „P.O.A."
- Rabatt als Spalte (`%`, „Rab.", „P") oder als Text
- Alternativpositionen · Optionen · Varianten

### F. Positionsarten, die kein Materialpreis sind
- Werkzeug-, Form-, Musterkosten, EMPB, Prototypen, Vorserie
- Einricht-, Rüst-, Anlauf-, Entwicklungs-, Zeichnungskosten
- Verpackung, Fracht, Mindermengenzuschlag als eigene Position

### G. Kaufmännische Klauseln, die den Preis verändern
*(Ergebnis der Recherche — hier ist die größte bekannte Lücke)*
- **Preisgleitklausel / Stoffpreisgleitklausel** — Preis gilt nur bei
  gleichbleibenden Rohstoffkosten
- **Legierungszuschlag** (Metall) — schwankt monatlich, oft nur im Fließtext
- **Kupfer-, Energie-, Sonderzuschlag**
- **Mindermengenzuschlag** unterhalb einer Menge
- **Mindestbestellmenge (MOQ)** — oft nur im Fließtext
- **Bindefrist / Angebotsgültigkeit** — „freibleibend", „30 Tage"
- **Skonto und Zahlungsziel** („2 % 14 Tage, 30 Tage netto")
- **Incoterm** (EXW, FCA, DDP …) — bestimmt, ob Fracht im Preis ist
- **Wechselkursklausel** bei Fremdwährung

> Diese Gruppe ist gefährlich, weil sie den *effektiven* Preis ändert,
> ohne in der Tabelle zu stehen. Ein Preis ohne seine Klausel ist nicht
> falsch abgelesen — er ist unvollständig verstanden. Regel: erkennen und
> **melden**, niemals selbst einrechnen.

### H. Sprache und Herkunft
- Deutsch · Englisch · gemischt
- Datumsformate: `TT.MM.JJJJ` · `MM/TT/JJJJ` · `JJJJ/M/T` · `TT-MMM-JJ`
- **mehrdeutige Daten** (03/04/2026) → nie raten, Spalte als unsicher
- Fremdwährung · Umsatzsteuer im Ausland

## Was daraus folgt

1. **Neue echte Belege sind mehr wert als erdachte.** Fehlgeschlagene
   Importe landen samt Protokoll in `nicht_erkannt/` — dieser Ordner ist
   die wichtigste Quelle für die nächste Härtungsrunde.
2. **Der Belegerzeuger prüft die Achsen, nicht die Fälle.** Er kombiniert
   die Achsen zufällig und misst, wie oft still danebengegangen wird.
3. **Jede neue Achse kommt zuerst hierher**, dann in den Erzeuger, dann in
   den Code.

## Quellen der Recherche

- [Table Extraction from Documents: The Complete Guide](https://www.docupipe.ai/blog/table-extraction-documents)
- [Nested Data Table Extraction](https://www.extend.ai/resources/nested-data-table-extraction-ai)
- [Complex Table Extraction for Multi-Line Invoices](https://docspire.ai/blog/multi-line-invoice-processing-automation/)
- [Preisgleitklausel (Wikipedia)](https://de.wikipedia.org/wiki/Preisgleitklausel)
- [Leitfaden Stoffpreisgleitklausel (BVMB)](https://www.bvmb.de/images/pdf/Stoffpreisgleitklausel/Leitfaden_Stoffpreisgleitklausel_Stand_2015.pdf)
- [Angebote richtig schreiben — Pflichtangaben](https://www.allrecht.de/alles-was-recht-ist/angebote-richtig-schreiben/)
