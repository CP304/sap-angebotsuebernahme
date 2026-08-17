# SAP-Angebotsuebernahme

Windows-Desktop-Anwendung fuer den Einkauf: Lieferantenangebote einlesen,
Positionen pruefen und korrigieren, mit dem SAP-Ist-Zustand vergleichen und
anschliessend per SAP GUI Scripting pflegen — **Infosatz, Orderbuch,
Mengenkontrakt und Bestellung in einem Zug.**

> **SAP wird ausschliesslich ueber SAP GUI Scripting angesprochen.**
> Keine BAPIs, keine RFCs, kein OData. Die Anwendung meldet sich nicht selbst
> an, sondern nutzt eine bereits geoeffnete Session des Anwenders.

---

## Inhalt

1. [Schnellstart](#schnellstart)
2. [Der Alltag in 10 Schritten](#der-alltag-in-10-schritten)
3. [Der Komplettvorgang](#der-komplettvorgang)
4. [Betriebsarten](#betriebsarten)
5. [Sicherheitskonzept](#sicherheitskonzept)
6. [Angebotserkennung](#angebotserkennung)
7. [SAP-Feld-IDs einsetzen](#sap-feld-ids-einsetzen)
8. [Konfiguration](#konfiguration)
9. [Projektstruktur](#projektstruktur)
10. [Tests](#tests)
11. [Wenn etwas nicht funktioniert](#wenn-etwas-nicht-funktioniert)

---

## Schnellstart

**Voraussetzung:** Python 3.12 oder neuer (entwickelt und getestet mit 3.14).

```bat
start.bat
```

Das Startskript installiert beim ersten Aufruf die benoetigten Pakete und
startet die Anwendung. Alternativ von Hand:

```bat
pip install -r requirements.txt
python -m app.main
```

Beim ersten Start laeuft die Anwendung im **Testsystem (Mock-SAP)** und im
**Dry Run** — es wird garantiert nichts in SAP geschrieben. So koennen Sie
alles gefahrlos ausprobieren.

Beispielangebote erzeugen (liegen bereits in `sample_data/erzeugt/`):

```bat
python sample_data/erzeuge_beispiele.py
```

Ziehen Sie eine der Dateien einfach in das Anwendungsfenster.

---

## Der Alltag in 10 Schritten

1. Tool starten
2. Angebot per Drag & Drop hineinziehen (oder **Angebot oeffnen**, oder
   **Text einfuegen** fuer eine formlose Preis-Mail)
3. Positionen werden erkannt und dargestellt
4. Ein bis zwei Werte korrigieren, falls noetig
5. **SAP verbinden** (entfaellt im Testsystem)
6. **SAP-Daten laden** (F5) — es wird nur gelesen
7. Alt/Neu-Vergleich pruefen, auffaellige Positionen kontrollieren
8. **Komplettvorgang** festlegen: was soll passieren?
9. Positionen an- und abwaehlen
10. **Uebernehmen** (F9) → Vorschau bestaetigen → Ergebnisuebersicht

Unterstuetzte Formate: **PDF, XLSX, XLSM, XLS, CSV, E-Mail (.msg / .eml),
Text und HTML** — sowie direkt eingefuegter Text.

---

## Der Komplettvorgang

Aus einem Angebot heraus alles auf einmal, in **fester Reihenfolge**:

```
1. Infosatz pflegen        ME11 / ME12    Preis, Preiseinheit, Waehrung,
                                          gueltig bis 31.12.2099 (einstellbar)
        ↓
2. Mengenkontrakt schreiben ME31K         ein Beleg je Lieferant,
                                          liefert die Kontraktnummer
        ↓
3. Orderbuch pflegen        ME01          Lieferant aktiv setzen,
                                          Kontrakt als Vereinbarung eintragen
        ↓
4. Bestellung anlegen       ME21N         Abruf aus dem Kontrakt
                                          ueber eine Teilmenge X
```

Die Reihenfolge ist bewusst nicht frei sortierbar: Orderbuch und Bestellung
brauchen die Kontraktnummer aus Schritt 2, sonst geht der Belegbezug verloren.

**Abrufmenge (Teilmenge X)** ist einstellbar als Prozentsatz der Kontraktmenge
(Standard 20 %), als feste Menge oder als volle Menge.

Belege werden **gebuendelt**: 20 Positionen eines Lieferanten ergeben *einen*
Kontrakt und *eine* Bestellung, nicht 20 Belege. Das spart Transaktions-
wechsel — beim GUI-Scripting der teuerste Teil.

Jede der vier Aktionen ist unabhaengig an- und abwaehlbar, sowohl global
(Schaltflaeche **Komplettvorgang**) als auch je Position (Spalten in der
Tabelle oder Detailansicht).

---

## Betriebsarten

| Betriebsart | SAP lesen | SAP schreiben | Wofuer |
|---|---|---|---|
| **Testsystem (Mock-SAP)** | eingebauter Testbestand | in den Testbestand | Einarbeitung, Vorfuehrung, Entwicklung ohne SAP |
| **Echtes SAP + Dry Run** | ja | nein, nur simuliert | Kontrolle vor dem ersten Echtlauf |
| **Echtbetrieb** | ja | ja | Produktivbetrieb |

Umschaltbar unter *Einstellungen → Betriebsart* oder per Startoption:

```bat
python -m app.main --mock            REM Testsystem
python -m app.main --real --dry-run  REM echtes SAP, nur lesen
python -m app.main --real --write    REM Echtbetrieb
python -m app.main --datei "C:\Pfad\Angebot.pdf"
```

Der Testbestand liegt in `%APPDATA%\SAP-Angebotsuebernahme\mock_sap.json` und
ist ueber *Einstellungen → Testdaten zuruecksetzen* jederzeit wiederherstellbar.
Er enthaelt bewusst auch Stoerfaelle: ein Material ohne Infosatz, ein gesperrtes
Material, einen gesperrten Lieferanten, ein Material mit fremdem Fix-Lieferanten
im Orderbuch und eine Materialnummer, die es gar nicht gibt.

---

## Sicherheitskonzept

Diese Anwendung schreibt in ein Produktivsystem. Deshalb vier harte Sperren:

### 1. Ungepruefte Feld-IDs sperren das Schreiben

Jede SAP-GUI-ID traegt ein Kennzeichen `verified`. Die mitgelieferten IDs sind
**Vorschlaege in der ueblichen Notation und ausdruecklich ungeprueft**. Solange
eine fuer den Vorgang benoetigte Pflicht-ID nicht bestaetigt ist, verweigert die
Anwendung im Echtbetrieb jede Schreibaktion. Lesen und Dry Run bleiben erlaubt.

### 2. Kein Nachrichtenversand

Vor dem Sichern eines Kontrakts oder einer Bestellung werden alle
Nachrichtensaetze entfernt und die leere Nachrichtentabelle nachgewiesen.
Gelingt das nicht, wird der Beleg **nicht gesichert** (`MessageGuard`).
Es soll ausdruecklich nichts an den Lieferanten hinausgehen — die Kommunikation
macht weiterhin der Mensch.

### 3. Nichts wird erfunden

Jeder Wert traegt seine Herkunft: *erkannt*, *unsicher erkannt*, *ueber
Zuordnung ergaenzt*, *Voreinstellung* oder *manuell erfasst*. Unsicher erkannte
Werte werden in der Tabelle gelb hinterlegt. Ist ein fuer die gewaehlte Aktion
zwingendes Feld unsicher, blockiert die Pruefung die Verarbeitung, bis der
Anwender es bestaetigt hat. Nicht erkannt heisst leer — nie geraten.

### 4. Kontrollierter Ablauf statt Warteschleifen

* Keine `sleep`-Kaskaden: es wird auf `session.Busy` und auf konkrete Elemente
  gewartet, mit Zeitlimit und begrenzten Wiederholungen.
* Nach jedem Schritt wird die Statusleiste ausgewertet; E-/A-Meldungen brechen
  die Position kontrolliert ab.
* Unerwartete Popups werden erkannt, ihr Text protokolliert und der Vorgang
  angehalten — **es wird nie blind Enter gedrueckt**.
* Vor dem Schreiben wird geprueft, ob in der SAP-Maske wirklich das erwartete
  Material und der erwartete Lieferant stehen.
* Ein Fehler in einer Position bricht den Lauf nicht ab; jede Position bekommt
  ihr eigenes Ergebnis. Nur bei unklarem SAP-Zustand (Popup im Belegteil,
  fehlgeschlagene Nachrichtenunterdrueckung) wird der ganze Lauf angehalten.

---

## Angebotserkennung

Jeder Lieferant baut sein Angebot anders auf. Deshalb arbeitet die Erkennung
mehrstufig und lernt mit:

**Quellen**
PDF (layoutbewusste Spaltenrekonstruktion, nicht nur Rohtext) · Excel/CSV
(mit und ohne Kopfzeile) · E-Mail `.eml` und `.msg` (Outlook-Dateien werden mit
einem eigenen, in der Standardbibliothek umgesetzten Leser geoeffnet — keine
Zusatzabhaengigkeit) · HTML-Tabellen aus Mails · Anhaenge rekursiv · direkt
eingefuegter Text.

**Erkennung**
Kopfdaten ueber einen umfangreichen deutsch/englischen Regelkatalog ·
Positionen ueber Spaltenzuordnung mit Konfidenz, bei fehlender Kopfzeile ueber
Datentyp-Analyse der Spalten · Freitexterkennung fuer formlose Preis-Mails ·
Erkennung von Summen-, Zwischensummen- und Fortsetzungszeilen · Staffelpreise
werden als eigene Positionen ausgewiesen, nicht stillschweigend zusammengefasst.

**Lernen**
Korrigiert der Anwender eine Zuordnung, wird daraus ein Lieferantenprofil
abgeleitet (Absenderdomain, Layout-Fingerabdruck, Spaltenzuordnung,
Zahlenformat). Gelernt wird ausschliesslich, **wo** ein Wert steht — nie,
**welcher** Wert dort steht. Profile sind unter *Zuordnungen → Gelernte
Angebotsformate* einsehbar und jederzeit verwerfbar.

**Zuordnungen**
Lieferantenname/E-Mail-Domain → SAP-Lieferantennummer und
Lieferantenartikelnummer/Text → eigene Materialnummer werden lokal gepflegt und
beim naechsten Angebot automatisch angewandt. Unterhalb des Schwellwerts wird
**nicht** automatisch zugeordnet, sondern nur vorgeschlagen.

### Wenn die Erkennung nicht greift

Kein Werkzeug erkennt jedes Format. Entscheidend ist, dass der Anwender dann
nicht in einer Sackgasse steht. Werden keine Positionen gefunden, bietet die
Anwendung von sich aus drei Wege an — **alle drei lernen fuer das naechste Mal
mit**:

**1. Tabelle einfuegen** (schnellster Weg, funktioniert immer)
Bereich in Excel markieren → Strg+C → im Dialog Strg+V. Die Tabelle erscheint
als Raster, ueber jeder Spalte ein Auswahlfeld: *Material*, *Preis*, *Menge* …
Ein Vorschlag ist bereits gesetzt, der Anwender korrigiert nur. Alternativ
laesst sich eine Datei direkt laden. Trennzeichen (Tabulator, Semikolon,
mehrere Leerzeichen) werden automatisch erkannt.

**2. Grafisch anlernen** (fuer PDFs mit ungewoehnlichem Layout)
Das Angebot wird als Seitenbild angezeigt. Zwei Fragen werden nacheinander
geklaert:

```
Schritt 1: Was ist eine Position?
    Rechteck um EINE vollstaendige Positionszeile ziehen.
    -> Startpunkt der Liste, Zeilenhoehe, Ankerspalte

Schritt 2: Was ist eine Spalte?
    In dieser Zeile die Felder markieren und benennen.
    -> Die Breite des Rechtecks definiert die Spalte

Schritt 3 (freiwillig): zweite Zeile als Gegenprobe
```

Aus der Beispielzeile wird zusaetzlich ein Plausibilitaetsmuster fuer die
Ankerspalte abgeleitet (z. B. „achtstellige Zahl"). Deshalb wird Fliesstext
unterhalb der Tabelle nicht faelschlich als Position gelesen, obwohl er
geometrisch in dieselben Spalten faellt. Mehrseitige Angebote uebernehmen das
Raster auf alle Seiten.

**3. Von Hand erfassen**
Einzelne Positionen ergaenzen — fuer den Rest eines sonst gut erkannten
Angebots.

Der eigentliche Gewinn: Die Zuordnung aus Weg 1 und 2 wird als
Lieferantenprofil gespeichert. Bei Weg 2 wird dabei aus der Geometrie die
Kopfzeile ueber den markierten Spalten ausgelesen und in eine ganz normale
Spaltenzuordnung „Ueberschrift → Feld" uebersetzt — davon profitiert dann auch
die *automatische* Erkennung beim naechsten Angebot desselben Lieferanten.

---

## SAP-Feld-IDs einsetzen

1. In SAP: **Alt+F12 → Skript-Aufzeichnung und -Wiedergabe → Aufzeichnen**
2. Den Vorgang einmal von Hand durchklicken (z. B. ME12 fuer einen Infosatz)
3. Aufzeichnung stoppen — es entsteht eine `.vbs`-Datei
4. In der Anwendung: Seite **SAP-Feld-IDs** → *Aufzeichnung (.vbs) einlesen*
5. Vorgeschlagene Zuordnungen pruefen, dann Haken **Geprueft** setzen
6. Speichern

Alternativ direkt die JSON-Datei bearbeiten:
`%APPDATA%\SAP-Angebotsuebernahme\sap_selectors.json`

Die Seite zeigt je Vorgang an, ob er bereit ist:

```
✓ info_record_read: bereit      ✗ contract_write: 12 offen
```

Voraussetzung im SAP-System: Scripting muss serverseitig
(`sapgui/user_scripting = TRUE`) und im SAP Logon unter
*Optionen → Zugriffshilfen & Scripting → Scripting* aktiviert sein.

Einzelheiten: [`docs/SAP_EINRICHTUNG.md`](docs/SAP_EINRICHTUNG.md)

---

## Konfiguration

Alles Kundenspezifische ist einstellbar — nichts davon steht im Code:

* Einkaufsorganisation, Einkaeufergruppe, Werk, Waehrung, Mengen-/Preiseinheit
* Belegarten fuer Kontrakt (MK) und Bestellung (NB)
* Gueltig-bis-Platzhalter (Standard **31.12.2099**), Kontraktlaufzeit,
  Standard-Lieferzeit
* Abrufmenge fuer die Bestellung (Modus und Prozentsatz)
* Grenzwerte: Preisabweichung gelb/rot, Preisobergrenze, Mindestaehnlichkeit
  fuer die Lieferantenzuordnung, Warnung bei altem Angebot
* Alle SAP-Transaktionen (falls Z-Transaktionen verwendet werden)
* Alle SAP-Feld-IDs
* Laufzeitverhalten: Zeitlimits, Wiederholungen, Kontextpruefung
* Pfade fuer Datenbank, Logs und Feld-IDs

Ablage: `%APPDATA%\SAP-Angebotsuebernahme\`
(ueber die Umgebungsvariable `SAP_ANGEBOT_HOME` umlenkbar)

```
settings.json        Konfiguration
sap_selectors.json   SAP-GUI-Feld-IDs
historie.sqlite3     Historie, Zuordnungen, gelernte Profile
mock_sap.json        Testbestand
logs/                Logdateien (rotierend)
```

---

## Projektstruktur

```
app/
    main.py                     Startpunkt
    bootstrap.py                Zusammenbau aller Dienste

    gui/                        Oberflaeche (enthaelt KEINE SAP-Logik)
        main_window.py          Hauptfenster
        offer_table.py          Positionstabelle
        position_details.py     Detailansicht
        dialogs.py              Vorschau, Ergebnis, Zuordnung, Komplettvorgang
        history_view.py         Historie
        mapping_view.py         Zuordnungen und gelernte Profile
        settings_view.py        Einstellungen
        selector_view.py        SAP-Feld-IDs
        workers.py              Hintergrundverarbeitung (QThread)
        style.py                Farben und Stylesheet

    models/                     Datenmodelle (dataclasses)
        offer.py                Angebot inkl. E-Mail-Kontext
        offer_position.py       Position mit Herkunft und Status
        sap_info_record.py      Infosatz-Ist-Zustand
        sap_source_list.py      Orderbuch-Ist-Zustand
        document_plan.py        Kontrakt- und Bestellplanung
        results.py              Ergebnisobjekte
        enums.py, issue.py

    services/                   Fachlogik ohne SAP-Details
        offer_import_service.py Fassade der Angebotserkennung
        readers/                PDF, Excel, E-Mail, Text
        extraction/             Kopfregeln, Tabellen, Freitext, Profile, Lernen
        comparison_service.py   Alt/Neu-Vergleich
        validation_service.py   Pruefungen und Warnungen
        preview_service.py      Vorschau vor der Verarbeitung
        batch_service.py        Komplettvorgang, Fehlerisolation
        undo_service.py         Undo vor dem SAP-Schreibvorgang

    sap/                        SAP-Anbindung
        connection.py           GUI-Scripting-Verbindung (einzige COM-Stelle)
        selectors.py            zentrale Feld-ID-Registry
        interfaces.py           Schnittstellen der Services
        gateway.py              Fassade, Mock/Echt-Umschaltung
        info_record_service.py  ME11/ME12/ME13
        source_list_service.py  ME01/ME03
        contract_service.py     ME31K/ME32K
        purchase_order_service.py ME21N/ME22N
        material_service.py     MM03
        vendor_service.py       XK03
        message_guard.py        Schutz gegen Nachrichtenversand
        mock_backend.py         eingebautes Testsystem

    database/                   SQLite
        schema.py               Tabellen und Migrationen
        repository.py           Historie, Zuordnungen, Profile
        mapping_store.py        fachliche Zuordnungslogik

    config/settings.py          gesamte Konfiguration
    utils/                      Parser, Logging, .msg-Leser

sample_data/                    Beispielangebote und Generator
tests/                          Testsuiten (unittest)
docs/                           SAP-Einrichtung
```

**Trennung von UI und SAP:** Die GUI ruft ausschliesslich Services auf.
Kein `session.findById(...)` ausserhalb von `app/sap/`.

---

## Tests

```bat
python -m unittest discover -s tests -v
```

Die Testsuiten laufen vollstaendig ohne SAP (gegen das Mock-System) und ohne
Zugriff auf Ihre echten Anwendungsdaten (temporaeres `SAP_ANGEBOT_HOME`).

| Suite | Umfang |
|---|---|
| `tests/test_database.py` | Migrationen, Historie, Zuordnungen, CSV-Export, Nebenlaeufigkeit |
| `tests/test_services.py` | Vergleich, Pruefung, Vorschau, Komplettvorgang, Undo |
| `tests/test_extraction.py` | Angebotsformate, E-Mails, Freitext, Lernen |
| `tests/test_fallback.py` | Tabelle einfuegen, grafisches Anlernen |
| `tests/test_gui_smoke.py` | Aufbau der Oberflaeche (ohne sichtbares Fenster) |
| `tests/test_integration.py` | komplette Kette Datei → SAP-Beleg, Robustheit |

---

## Wenn etwas nicht funktioniert

**„Es wurde keine laufende SAP-GUI gefunden."**
SAP Logon starten und an einem System anmelden. Die Anwendung meldet sich
bewusst nicht selbst an.

**„Die SAP-GUI-Scripting-Schnittstelle ist nicht verfuegbar."**
Scripting ist deaktiviert. Serverseitig `sapgui/user_scripting = TRUE`
(Basis-Team) und im SAP Logon unter *Optionen → Zugriffshilfen & Scripting*.

**„Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft."**
Gewollt. Siehe [SAP-Feld-IDs einsetzen](#sap-feld-ids-einsetzen).

**„Ein erwartetes SAP-Feld wurde nicht gefunden."**
Die hinterlegte ID passt nicht zu Ihrer Maske. ID neu aufzeichnen und
eintragen. Die technische Element-ID steht in den aufklappbaren Details.

**Keine Positionen erkannt**
Die Anwendung bietet dann selbst die drei Auffangwege an (Tabelle einfuegen,
grafisch anlernen, von Hand erfassen) — siehe
[Wenn die Erkennung nicht greift](#wenn-die-erkennung-nicht-greift).
Bei PDFs ohne Textebene (reine Scans) hilft auch das Anlernen nicht, weil gar
kein Text vorhanden ist; OCR ist bewusst nicht eingebaut. Dann bleibt
**Tabelle einfuegen** oder eine Excel-Datei vom Lieferanten.

**`.xls` laesst sich nicht oeffnen**
Altes Excel-Format. Entweder `pip install xlrd` oder die Datei als `.xlsx`
speichern.

**pywin32 fehlt**
`pip install pywin32`. Ohne pywin32 laeuft nur das Testsystem — das ist kein
Fehler, sondern Absicht.

Logdateien: `%APPDATA%\SAP-Angebotsuebernahme\logs\`
In der Anwendung ausserdem live auf der Seite **Protokoll**.

---

## Stand und Grenzen

* Die mitgelieferten SAP-Feld-IDs sind **ungeprueft** und muessen am Zielsystem
  bestaetigt werden. Bis dahin ist der Echtbetrieb gesperrt — Testsystem und
  Dry Run funktionieren vollstaendig.
* Eine Lieferanten-Namenssuche direkt in SAP ist nicht aktiviert; verwendet
  wird das lokale Zuordnungsverzeichnis (schneller und nachvollziehbar).
* OCR fuer gescannte PDFs ist nicht enthalten. Die Erkennung ist so aufgebaut,
  dass OCR oder eine KI-Auswertung als weitere Stufe ergaenzt werden koennen.
