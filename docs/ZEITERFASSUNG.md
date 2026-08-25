# Zeiterfassung

Erfasst die Arbeitszeit anhand der Einschaltzeiten des Rechners, fragt bei
Luecken im Tag nach, laesst sich von Hand korrigieren, kennt Urlaub und
Krankheit, zeigt die Tageszahlen auf Tastendruck und exportiert alles als
formatierte Excel-Mappe mit Auswertung und Diagrammen.

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
| Tag korrigieren                | Doppelklick auf eine Tageszeile (Begruendung noetig) |
| Urlaub, Krank, Feiertag        | Uebersicht -> *Urlaub / Krank / Feiertag...*      |
| Excel-Export                   | Uebersicht -> *Als Excel exportieren*             |
| Beenden                        | Rechtsklick auf das Symbol -> *Zeiterfassung beenden* |

Das Mini-Fenster steht genau solange, wie die Tasten gehalten werden, nimmt
keinen Fokus und zeigt:

* eingestempelt seit / taetig seit
* Arbeitszeit heute, Pausen, Rest bis zum Tagesziel
* voraussichtlicher Feierabend
* Ist und Soll der Woche sowie den Wochenrest

Oben rechts sitzt ein kleiner Pfeil (**↗**): ein Klick springt in die grosse
Uebersicht.  Damit er ueberhaupt zu treffen ist, bleibt das Fenster nach dem
Loslassen noch 2,5 Sekunden stehen -- und solange die Maus darin steht, bleibt
es offen.

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

## Belastbare Zeitstempel

Ein Zeitnachweis taugt nur so viel wie seine Nachvollziehbarkeit.  Deshalb
traegt **jeder** Zeitstempel seine Herkunft mit sich, und der Bericht weist
sie aus:

| Herkunft | Bedeutung | Beleg im Bericht |
|---|---|---|
| **automatisch gemessen** | Der Rechner lief von an bis aus; das Programm hat die Zeit selbst gemessen. | "automatisch gemessen (Rechner an bis aus)" |
| **Ende aus letztem Herzschlag** | Der Rechner ging hart aus (Stromausfall, Absturz). Das Ende ist der letzte Herzschlag -- also die letzte Minute, in der der Rechner nachweislich lief. | "Beginn automatisch, Ende aus dem letzten Herzschlag" |
| **auf Rueckfrage eingeordnet** | Der Rechner war zwischendurch aus. Das Programm hat gefragt, der Benutzer hat geantwortet. | "Rechner war aus, auf Rueckfrage eingeordnet am *Zeitpunkt*: *Begruendung*" |
| **von Hand erfasst** | Nachgetragen oder korrigiert. **Nur mit Begruendung moeglich.** | "von Hand erfasst am *Zeitpunkt*: *Begruendung*" |

Daraus ergibt sich je Tag ein kurzer **Nachweis** ("automatisch",
"automatisch, von Hand", "Ende geschaetzt, Rueckfrage" ...).  Er steht in der
Uebersicht, im Mini-Fenster und in der Excel-Spalte *Nachweis*.

**Begruendungspflicht.**  Ohne Begruendung entsteht kein Handeintrag: Der
Knopf *Uebernehmen* bleibt gesperrt, und auch das Loeschen einer Buchung
fragt danach.  Die Begruendung ist spaeter nicht mehr wegzudiskutieren --
sie steht in der Buchung **und** im Aenderungsprotokoll.

**Aenderungsprotokoll.**  Jeder Eingriff von Hand wird fortgeschrieben --
Zeitpunkt (sekundengenau), Benutzer, Rechnername, Aktion, was vorher stand,
was jetzt steht und warum.  Das Protokoll wird nur ergaenzt, nie
ueberschrieben, und immer vollstaendig exportiert.  Die automatische
Erfassung erzeugt darin keine Eintraege -- was im Protokoll steht, ist genau
das, was ein Mensch angefasst hat.

---

## Korrigieren, Urlaub und Krankheit

Die Automatik ist gut, aber nicht allwissend: der Rechner lief in der
Mittagspause weiter, das Programm wurde zu spaet gestartet, ein halber Tag war
Urlaub.  Deshalb laesst sich jeder Tag von Hand nachziehen.

**Tag bearbeiten** (Doppelklick auf eine Tageszeile) zeigt alle Buchungen des
Tages und erlaubt:

* **Nachtragen** -- vergessene Arbeitszeit oder eine Pause eintragen
* **Bearbeiten** -- Zeiten und Art einer Buchung aendern
* **Loeschen** -- eine falsche Buchung entfernen
* **Tagesart** -- Arbeitstag, Urlaub, Krank, Feiertag, Gleittag, Dienstreise;
  ganz- oder halbtags

Von Hand geaenderte Buchungen sind als solche gekennzeichnet und tauchen im
Excel-Blatt *Buchungen* auf -- die Korrektur bleibt also nachvollziehbar.

Eine Pause mitten in einer Sitzung kuerzt die Anwesenheit tatsaechlich; sich
ueberschneidende Buchungen werden nur einmal gezaehlt.

**Urlaub / Krank / Feiertag** traegt einen ganzen Zeitraum auf einmal ein.
Tage ohne Sollstunden (Samstag und Sonntag) werden dabei uebersprungen --
Urlaub am Wochenende kostet keinen Urlaubstag.

So wird gerechnet:

| Fall | Wirkung |
|---|---|
| Ganzer Tag Urlaub/Krank/Feiertag | Tagessoll wird gutgeschrieben, Saldo 0. Eine trotzdem aufgezeichnete Rechnerlaufzeit bleibt unberuecksichtigt. |
| Halber Tag | Halbe Gutschrift **plus** die wirklich geleistete Zeit. |
| Zurueck auf *Arbeitstag* | Der Eintrag wird geloescht, es zaehlt wieder die Erfassung. |

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

Vier Blaetter:

* **Uebersicht** -- Ist, Soll, Saldo, Pausen und Gutschriften des Zeitraums,
  Salden je Kalenderwoche und ein Balkendiagramm Ist gegen Soll.
* **Tage** -- eine Zeile je Tag mit Tagesart, Kommen, Gehen, Anwesenheit,
  Pause, Ist, Soll und Saldo; Wochenenden grau, Abwesenheiten gelb
  hinterlegt, Summenzeile, Autofilter.
* **Auswertung** -- Kennzahlen (Durchschnitt je Erfassungstag, laengster Tag,
  Kommen und Gehen im Mittel, Urlaubs- und Krankheitstage) und **drei
  Diagramme**: Saldoverlauf als Linie, Verteilung der Zeit als Kreis,
  Durchschnitt je Wochentag als Balken.
* **Buchungen** -- jede Sitzung und jede eingeordnete Luecke **sekundengenau**
  mit Spalte *Herkunft* und *Beleg / Begruendung*; Handeintraege sind rot
  hinterlegt.
* **Protokoll** -- das vollstaendige Aenderungsprotokoll: Zeitpunkt,
  Benutzer, Rechner, Aktion, vorher, nachher, Begruendung.  Gab es keine
  Eingriffe, steht das dort ausdruecklich.

Der Kopf der Uebersicht nennt Erstellzeitpunkt, Benutzer, Rechnername und
Programmstand; darunter steht, an wie vielen Tagen von Hand eingegriffen
wurde und an wie vielen das Ende geschaetzt ist.

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
| `zeiterfassung.db`    | SQLite mit Sitzungen, Luecken, Tagesarten und Aenderungsprotokoll |
| `einstellungen.json`  | Wochensoll, Tagessoll, Hotkey, Pausenautomatik     |

Einstellbar sind unter anderem Wochenarbeitszeit (Vorgabe 38), Sollstunden
je Wochentag, Hotkey, Herzschlagtakt und die Schwelle, ab der eine Luecke
nachgefragt wird (Vorgabe 5 Minuten).

---

## Tests

```
python -m unittest tests.test_zeiterfassung -v
```

Die Tests laufen ohne Windows -- geprueft werden Pausenregeln, Tages- und
Wochenzahlen, Lueckenerkennung, hartes Ausschalten, Urlaub und Krankheit,
manuelle Korrekturen, die Herkunft jedes Zeitstempels, die
Begruendungspflicht, das Aenderungsprotokoll, Hotkey-Zerlegung und der
Excel-Export.  Die
Oberflaechentests laufen bildschirmlos (`QT_QPA_PLATFORM=offscreen`) und
werden uebersprungen, wenn PySide6 nicht vorhanden ist.
