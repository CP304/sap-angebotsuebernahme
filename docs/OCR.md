# Texterkennung (OCR) fuer gescannte Angebote

Diese Anleitung beschreibt, wann die Texterkennung hilft, wann nicht, wie sie
installiert wird und woran sich erkennen laesst, ob man dem Ergebnis trauen
darf.

---

## 1. Wozu OCR hier da ist

Ein Teil der Angebote kommt nicht als Datei, sondern als Bild: eingescanntes
PDF, Faxausdruck, Handyfoto. Solche Dateien enthalten keinen durchsuchbaren
Text. Bisher hat die Anwendung das korrekt erkannt und die Datei abgelehnt --
richtig, aber der Anwender stand danach ohne Loesung da und musste alles
abtippen.

Mit einer installierten Texterkennung wird die Seite gerendert, gelesen und
durch dieselbe Tabellenrekonstruktion geschickt wie ein normales PDF. Aus einem
Scan kann so wieder eine Positionstabelle werden statt eines Textklumpens.

**Das Ergebnis ist ein Vorschlag zum Pruefen, kein Beleg.**

## 2. Wozu OCR hier ausdruecklich NICHT da ist

Der Grundsatz des Projekts gilt unveraendert: Es wird nie ein Wert erfunden.
Bei OCR ist das besonders heikel, denn eine Texterkennung liefert *immer* ein
Ergebnis -- auch dann, wenn sie nichts Sinnvolles gelesen hat. Eine `8` statt
einer `3` im Preis kostet mehr Geld, als eine gar nicht erkannte Position je
kosten koennte.

Deshalb:

* Alles, was aus OCR stammt, bleibt durchgaengig als unsicher gekennzeichnet.
  Der Tabellenblock traegt `origin = "pdf-ocr"` bzw. `"image-ocr"`.
* Es wird **nichts korrigiert**. Sieht ein Wert wie `1.2O0` aus, wird das
  `O` *nicht* automatisch zur `0` -- der Verdacht wird gemeldet, die Zeichen
  bleiben, wie sie erkannt wurden. Eine automatische Korrektur waere ein
  erfundener Wert.
* Jede erkannte Seite erzeugt eine Warnung im Protokoll.

## 3. Installation -- zwei Wege

Beide Wege sind optional. Ohne sie laeuft die Anwendung unveraendert weiter und
meldet lediglich, dass das PDF keinen durchsuchbaren Text enthaelt.

### Weg 1: Bordmittel von Windows (empfohlen zum Ausprobieren)

Windows 10 und 11 bringen eine Texterkennung mit (`Windows.Media.Ocr`). Es muss
kein zusaetzliches Programm installiert und genehmigt werden -- in einer
Einkaufsumgebung ist das oft der entscheidende Punkt.

```
pip install winsdk
```

Zusaetzlich muss das Sprachpaket in Windows vorhanden sein:

**Einstellungen → Zeit und Sprache → Sprache → Deutsch → Optionen →
Optionale Sprachfeatures → Basis-Schrifterkennung**

*Einschraenkung:* Diese Engine liefert **keine Sicherheitswerte je Wort**. Die
Anwendung kann dann nicht sagen, welche Zelle wackelig ist -- sie weist
stattdessen das *gesamte* Ergebnis als ungeprueft aus. Fuer eine schnelle
Vorbefuellung reicht das; fuer Belege mit vielen Zahlen ist Weg 2 besser.

### Weg 2: Tesseract (genauer, mit Sicherheitswerten)

Hier sind **zwei** Dinge noetig. Das wird haeufig verwechselt:

1. Das Python-Paket:

   ```
   pip install pytesseract
   ```

2. Das **Programm** Tesseract-OCR. Unter Windows ueblicherweise ueber das
   Installationspaket der UB Mannheim. Bei der Installation die Sprachdaten
   fuer Deutsch (`deu`) mit auswaehlen. Danach muss `tesseract.exe` ueber den
   `PATH` erreichbar sein.

Pruefen laesst sich das in einer Eingabeaufforderung:

```
tesseract --version
```

Meldet der Befehl eine Versionsnummer, ist alles vorhanden. Meldet er
"Befehl nicht gefunden", fehlt der PATH-Eintrag -- das Python-Paket allein
nuetzt dann nichts.

### Welcher Weg gilt?

In den Einstellungen unter `ocr.backend_order` steht die Reihenfolge, in der
probiert wird -- Standard `["windows", "tesseract"]`. Verwendet wird die erste
Engine, die tatsaechlich einsatzbereit ist.

## 4. Einstellungen

| Einstellung      | Standard              | Bedeutung |
|------------------|-----------------------|-----------|
| `enabled`        | `true`                | Erkennung ueberhaupt verwenden |
| `ask_before_ocr` | `true`                | Bei erkanntem Scan erst fragen. OCR dauert einige Sekunden je Seite. |
| `backend_order`  | `["windows","tesseract"]` | Reihenfolge der Engines |
| `language`       | `"de"`                | Erkennungssprache |
| `dpi`            | `300`                 | Aufloesung, mit der PDF-Seiten gerendert werden |
| `min_confidence` | `0.60`                | Darunter gilt ein Wort als unsicher |
| `max_pages`      | `20`                  | Schutz gegen 200-Seiten-Scans |
| `preprocess`     | `true`                | Graustufen vor der Erkennung |

Zu `dpi`: Unter 200 wird die Erkennung unbrauchbar, ueber 400 wird sie nur noch
langsamer, ohne besser zu werden. 300 ist der uebliche Scannerwert und der
richtige Ausgangspunkt.

## 5. Die Qualitaet beurteilen

Nach dem Import stehen im Protokoll die Angaben, auf die es ankommt:

* **Mittlere Sicherheit je Seite.** Ueber 90 % ist ein sauberer Scan.
  70--90 % heisst: brauchbar, aber Zahlen einzeln vergleichen. Unter 70 %
  sollte die Vorlage neu angefordert werden -- Nacharbeit kostet mehr Zeit als
  ein zweiter Anruf beim Lieferanten.
* **Anzahl unsicher erkannter Angaben.** Jede einzelne davon steht mit Seite,
  Zeile und Spalte als eigene Warnung im Protokoll.
* **Ziffernverdacht.** Verwechslungen wie `0`/`O`, `1`/`l`/`I`, `5`/`S`,
  `8`/`B` und `6`/`G` sind bei OCR haeufig. Sieht eine Zelle wie eine Zahl aus,
  sobald man diese Zeichen ersetzt, wird sie gemeldet. Ersetzt wird sie nicht.
* **Steht "unbekannt" als Sicherheit**, wurde die Windows-Engine benutzt. Dann
  gibt es keine Einzelbewertung, und es muss alles gesichtet werden.

Faustregel fuer den Alltag: **Beschreibungstexte** duerfen aus OCR uebernommen
werden, **Zahlen** (Preis, Menge, Preiseinheit, Materialnummer) werden vor dem
Schreiben nach SAP mit dem Beleg verglichen. Genau dort entsteht der Schaden.

## 6. Wo OCR an Grenzen stoesst

Die folgenden Faelle sind keine Fehler der Anwendung, sondern Grenzen des
Verfahrens. Sie fuehren zu falschen oder fehlenden Werten:

* **Handschrift.** Nachtraeglich handschriftlich geaenderte Preise oder
  Mengen werden nicht zuverlaessig gelesen. Beide Engines sind fuer Druckschrift
  gebaut.
* **Stempel und Unterschriften ueber dem Text.** Was der Stempel verdeckt, ist
  weg; die Erkennung liefert an dieser Stelle trotzdem irgendetwas.
* **Mehrspaltige Layouts.** Steht neben der Positionstabelle ein zweiter
  Textblock, koennen beide in einer Zeile landen. Die Spaltenerkennung arbeitet
  ueber Wortkoordinaten und kann zwei nebeneinanderliegende Raster nicht immer
  trennen.
* **Tabellen ohne gezeichnete Linien**, deren Spalten sehr eng stehen. Menge
  und Preis rutschen dann in dieselbe Zelle.
* **Schiefe oder verwellte Vorlagen.** Schon wenige Grad Schraeglage zerlegen
  die Zeilenerkennung. Vorlage gerade auflegen, nicht fotografieren.
* **Schwache Aufloesung.** Ein Handyfoto aus der Hand liegt oft unter dem, was
  bei kleiner Schrift noetig waere. Ein echter Scan mit 300 dpi ist immer
  besser.
* **Durchscheinende Rueckseiten** bei duennem Papier.

In all diesen Faellen ist der schnellste Weg nicht die Nachbearbeitung, sondern
die Bitte an den Lieferanten, das Angebot als PDF oder Excel zu schicken.

## 7. Unterstuetzte Dateiformate

Neben gescannten PDFs koennen auch reine Bilddateien importiert werden:

`.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`

Mehrseitige TIFF-Dateien werden Seite fuer Seite verarbeitet. Ist keine
Erkennung installiert, kommt eine Meldung mit dem Installationshinweis --
niemals ein leeres, scheinbar erfolgreiches Ergebnis.
