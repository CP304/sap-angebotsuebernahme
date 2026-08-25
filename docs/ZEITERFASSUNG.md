# Zeiterfassung

Erfasst die Arbeitszeit anhand der Einschaltzeiten des Rechners, fragt bei
Luecken im Tag nach, zeigt die Tageszahlen auf Tastendruck und exportiert
alles als formatierte Excel-Mappe.

Grundlage ist eine **Wochenarbeitszeit von 38 Stunden**, verteilt auf Montag
bis Freitag -- also **7,6 Stunden (7:36) je Tag**.  Fuehrend ist das
Tagesziel; der Rest bis 38 Stunden wird zusaetzlich ausgewiesen.

---

## Schnellstart

```
start_zeiterfassung.bat
```

Der erste Start installiert fehlende Pakete (PySide6, openpyxl) und traegt
die Zeiterfassung in den Autostart ein.  Ab dann laeuft sie nach jedem
Anmelden von selbst; im Infobereich der Taskleiste liegt ein kleines
Ziffernblatt.

---

## Der Alltag

| Was                            | Wie                                              |
|--------------------------------|--------------------------------------------------|
| Tageszahlen nachschauen        | **Strg+Shift+Z** gedrueckt halten                 |
| Uebersicht und Zeitraum        | Doppelklick auf das Symbol im Infobereich         |
| Excel-Export                   | Uebersicht -> *Als Excel exportieren*             |
| Beenden                        | Rechtsklick auf das Symbol -> *Zeiterfassung beenden* |

Das Mini-Fenster steht genau solange, wie die Tasten gehalten werden, nimmt
keinen Fokus und zeigt:

* eingestempelt seit / taetig seit
* Arbeitszeit heute, Pausen, Rest bis zum Tagesziel
* voraussichtlicher Feierabend
* Ist und Soll der Woche sowie den Wochenrest

---

## Wie gezaehlt wird

1. **Einschaltzeit.** Solange das Programm laeuft, schreibt es jede Minute
   einen Herzschlag.  Daraus ergibt sich der Zeitraum, in dem der Rechner
   nachweislich lief -- auch nach einem Stromausfall, denn das Ende ist dann
   der letzte Herzschlag.
2. **Luecken am selben Tag.** Wird der Rechner zwischendurch heruntergefahren
   oder schlaeft er laenger als fuenf Minuten, fragt das Werkzeug beim
   naechsten Start genau einmal nach:
   *Gearbeitet* (zaehlt), *Pause* (zaehlt als Pause) oder *Nicht gearbeitet*
   (zaehlt gar nicht).  Ueber Nacht wird nicht gefragt.
   Kuerzere Aussetzer gelten als durchgehende Arbeit.
3. **Pflichtpausen nach ArbZG.** Ab mehr als 6 Stunden werden 30 Minuten, ab
   mehr als 9 Stunden 45 Minuten Pause vorausgesetzt.  Bereits erfasste
   Pausen werden angerechnet -- abgezogen wird nur die Differenz.  Die
   Automatik laesst sich in den Einstellungen abschalten.

Beispiel: 8:00 bis 17:00 ohne erfasste Pause
= 9:00 Anwesenheit - 0:30 Pflichtpause = **8:30 Arbeitszeit**, Saldo +0:54.

---

## Autostart

Vorgabe ist ein Eintrag unter
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`.  Der
Schluessel gehoert dem angemeldeten Benutzer -- **keine Administratorrechte
noetig**, jederzeit ueber das Haekchen in den Einstellungen wieder zu
entfernen.  Ist die Registry gesperrt, weicht das Programm auf eine
Startdatei im Autostart-Ordner (`shell:startup`) aus.  Klappt beides nicht,
sagt es das deutlich, statt still nichts zu tun.

Ein doppelter Start ist harmlos: das Programm erkennt sich selbst und
meldet, dass es bereits laeuft.

---

## Excel-Export

Drei Blaetter:

* **Uebersicht** -- Ist, Soll, Saldo und Pausen des Zeitraums, Salden je
  Kalenderwoche und ein Balkendiagramm Ist/Soll.
* **Tage** -- eine Zeile je Tag mit Kommen, Gehen, Anwesenheit, Pause, Ist,
  Soll und Saldo; Wochenenden hinterlegt, Summenzeile, Autofilter.
* **Buchungen** -- jede Sitzung und jede eingeordnete Luecke als Nachweis.

Alle Stundenwerte stehen als Industriestunden (8,50 = 8:30) und lassen sich
direkt weiterrechnen.

Ohne Oberflaeche geht es auch:

```
python -m zeiterfassung.main --export Zeiten.xlsx --von 2026-08-01 --bis 2026-08-31
```

---

## Einstellungen und Daten

Beides liegt im Benutzerprofil unter
`%APPDATA%\Zeiterfassung`:

| Datei                 | Inhalt                                            |
|-----------------------|---------------------------------------------------|
| `zeiterfassung.db`    | SQLite mit Sitzungen und eingeordneten Luecken     |
| `einstellungen.json`  | Wochensoll, Tagessoll, Hotkey, Pausenautomatik     |

Einstellbar sind unter anderem Wochenarbeitszeit (Vorgabe 38), Sollstunden
je Wochentag, Hotkey, Herzschlagtakt und die Schwelle, ab der eine Luecke
nachgefragt wird (Vorgabe 5 Minuten).

---

## Tests

```
python -m unittest tests.test_zeiterfassung -v
```

Die Tests laufen ohne Oberflaeche und ohne Windows -- geprueft werden
Pausenregeln, Tages- und Wochenzahlen, Lueckenerkennung, hartes Ausschalten,
Hotkey-Zerlegung und der Excel-Export.
