# SAP-Einrichtung und Uebernahme der Feld-IDs

Diese Anleitung fuehrt vom frisch installierten Werkzeug bis zum ersten
kontrollierten Echtlauf. Rechnen Sie mit etwa einer Stunde — einmalig.

---

## 1. Voraussetzungen im SAP-System

### 1.1 Scripting serverseitig freischalten (Basis-Team)

Profilparameter am Applikationsserver:

```
sapgui/user_scripting = TRUE
```

Pruefen laesst sich das in **RZ11**. Der Parameter ist dynamisch aenderbar,
sollte aber zusaetzlich im Profil verankert werden, sonst ist er nach dem
naechsten Systemstart wieder weg.

Optional, aber ueblich:

```
sapgui/user_scripting_disable_recording = FALSE
sapgui/user_scripting_force_notification = FALSE
sapgui/user_scripting_set_readonly       = FALSE
```

`user_scripting_set_readonly = TRUE` wuerde lesende Skripte erlauben, aber
jedes Schreiben unterbinden. Fuer den reinen Dry-Run-Betrieb ist das eine
sinnvolle Zwischenstufe.

### 1.2 Scripting am Frontend freischalten

SAP Logon → **Optionen** → *Zugriffshilfen & Scripting* → **Scripting**

* [x] Scripting aktivieren
* [ ] Benachrichtigen, wenn ein Skript sich an SAP GUI anhaengt
* [ ] Benachrichtigen, wenn ein Skript geoeffnet wird

Die beiden Benachrichtigungen erzeugen sonst bei jedem Schritt ein Popup —
und die Anwendung bricht bei unerwarteten Popups bewusst ab.

### 1.3 Berechtigungen

Der ausfuehrende SAP-Benutzer braucht genau die Berechtigungen, die er auch
fuer die manuelle Pflege braucht:

| Transaktion | Objekt (typisch) |
|---|---|
| ME11 / ME12 / ME13 | `M_EINF_EKO`, `M_EINF_WRK` |
| ME01 / ME03 | `M_ORDR_WRK`, `M_ORDR_EKO` |
| ME31K / ME32K / ME33K | `M_BEST_EKO`, `M_BEST_BSA`, `M_BEST_WRK` |
| ME21N / ME22N / ME23N | `M_BEST_EKO`, `M_BEST_BSA`, `M_BEST_WRK` |
| MM03 | `M_MATE_MAT` |
| XK03 | `F_LFA1_APP`, `M_LIEF_EKO` |

Es werden bewusst **keine** technischen Sonderrechte benoetigt. Das Werkzeug
tut nichts, was der Anwender nicht auch von Hand tun duerfte.

### 1.4 Empfehlung zum Nachrichtenversand

Die Anwendung entfernt vor dem Sichern alle Nachrichtensaetze und weist die
leere Tabelle nach. Zusaetzlich empfohlen — als zweite, unabhaengige Sperre:

* In **NACE** fuer die von diesem Werkzeug verwendeten Belegarten (MK/NB)
  pruefen, welche Nachrichtenarten gefunden werden.
* Falls moeglich, einen Konditionssatz-Ausschluss oder eine eigene Belegart
  ohne Nachrichtenfindung verwenden.

Sicherheit gehoert nicht in eine einzige Schicht.

---

## 2. Dezimal- und Datumsformat des Benutzers

Die Anwendung uebergibt Zahlen im **deutschen Format** (Komma als
Dezimaltrennzeichen) und Datumsangaben als `TT.MM.JJJJ`.

Pruefen Sie in **SU3** → *Festwerte*:

* Dezimaldarstellung: `1.234.567,89`
* Datumsdarstellung: `TT.MM.JJJJ`

Weicht Ihre Anmeldung davon ab, passen Sie entweder die Benutzervorgaben an
oder die Formatierung in
`app/sap/info_record_service.py` (`_decimal_text`) sowie in den beiden
Belegservices — die Stelle ist dort mit `TODO` gekennzeichnet.

---

## 3. Feld-IDs aufzeichnen

### 3.1 Aufzeichnung starten

1. In SAP: **Alt + F12** → *Skript-Aufzeichnung und -Wiedergabe*
2. **Aufzeichnen** druecken, Zieldatei waehlen
3. Vorgang **einmal vollstaendig von Hand** durchklicken
4. **Stopp** druecken

Zeichnen Sie jeden Vorgang einzeln auf — das erleichtert die Zuordnung:

| Aufzeichnung | Was durchklicken |
|---|---|
| `me13_lesen.vbs` | ME13, vorhandenen Infosatz anzeigen bis zum Konditionsbild |
| `me12_aendern.vbs` | ME12, Preis aendern, Gueltigkeit setzen, sichern |
| `me11_anlegen.vbs` | ME11, neuen Infosatz komplett anlegen |
| `me01_orderbuch.vbs` | ME01, Zeile anlegen, Lieferant aktiv, sichern |
| `me31k_kontrakt.vbs` | ME31K, Kontrakt mit 2 Positionen anlegen, **inkl. Kopf → Nachrichten** |
| `me21n_bestellung.vbs` | ME21N, Bestellung mit Kontraktbezug, **inkl. Nachrichten** |
| `mm03_material.vbs` | MM03, Material aufrufen |
| `xk03_lieferant.vbs` | XK03, Lieferant aufrufen |

> Die Nachrichtenbilder **muessen** mit aufgezeichnet werden. Ohne geprueftes
> Nachrichtenbild sichert die Anwendung Kontrakte und Bestellungen nicht.

### 3.2 Aufzeichnung einlesen

In der Anwendung: Seite **SAP-Feld-IDs** → *Aufzeichnung (.vbs) einlesen*

Die Zuordnung erfolgt ueber den technischen Feldnamen am Ende der ID
(z. B. `EINE-NETPR`), nicht ueber den vollstaendigen Pfad — der Praefix
unterscheidet sich je nach Bildaufbau. Vorgeschlagene Aenderungen werden vor
der Uebernahme angezeigt.

### 3.3 Von Hand nacharbeiten

Was der Automatismus nicht findet, tragen Sie direkt in der Tabelle ein.
Eine Zeile aus einer Aufzeichnung sieht so aus:

```vbs
session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
session.findById("wnd[0]/usr/txtEINE-NETPR").text = "12,85"
```

Der Teil in Anfuehrungszeichen gehoert in die Spalte **SAP-GUI-ID**.

**Table-Controls:** Zellen-IDs enthalten einen Zeilenindex, der sich auf den
*sichtbaren* Bereich bezieht:

```
wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-LIFNR[2,0]
                                            ^ ^
                                     Spalte | Zeile
```

Tragen Sie fuer die Zeile den Platzhalter `%d` ein:

```
wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-LIFNR[2,%d]
```

Die Anwendung setzt den richtigen Index ein und scrollt selbst, wenn die
gesuchte Zeile ausserhalb des sichtbaren Bereichs liegt.

### 3.4 Bestaetigen

Setzen Sie den Haken **Geprueft** nur fuer IDs, die Sie tatsaechlich am
Zielsystem kontrolliert haben. Unten auf der Seite steht je Vorgang, ob er
bereit ist:

```
✓ info_record_read: bereit
✓ info_record_write: bereit
✗ contract_write: 4 offen
```

Anschliessend **Speichern**.

---

## 4. Schrittweise in Betrieb nehmen

Gehen Sie in dieser Reihenfolge vor — jede Stufe erst freigeben, wenn die
vorige sauber laeuft:

| Stufe | Einstellung | Was Sie pruefen |
|---|---|---|
| 1 | Testsystem | Erkennung, Bedienung, Komplettvorgang verstanden |
| 2 | Echtes SAP + Dry Run | Werden Infosaetze und Orderbuecher korrekt **gelesen**? Stimmen Alt/Neu und die Prozentangaben? |
| 3 | Echtbetrieb, **eine** Position, nur Infosatz | Steht der Preis in SAP so, wie erwartet? |
| 4 | Echtbetrieb, Infosatz + Orderbuch | Ist der Lieferant aktiv, Gueltigkeit korrekt? |
| 5 | Echtbetrieb, Komplettvorgang mit 2–3 Positionen | Kontrakt und Bestellung korrekt, **keine Nachricht** ausgeloest? |
| 6 | Normalbetrieb | |

Pruefen Sie nach Stufe 5 unbedingt in **NAST** bzw. ueber die Belegnachrichten,
dass tatsaechlich keine Ausgabe erzeugt wurde.

---

## 5. Waehrend der Verarbeitung

* **SAP nicht anfassen.** GUI-Scripting arbeitet in der sichtbaren Session;
  eine gleichzeitige Eingabe bringt beides durcheinander.
* Fuer laengere Laeufe eine **eigene SAP-Session** oeffnen und diese unter
  *Session waehlen* auswaehlen. Dann koennen Sie in einer anderen Session
  weiterarbeiten.
* Die Anwendung laesst sich jederzeit ueber **Abbrechen** anhalten. Die gerade
  laufende Position wird noch zu Ende gefuehrt, der Rest wird uebersprungen.

---

## 6. Bekannte Stolpersteine

**ME21N: IDs aendern sich**
ME21N ist eine Enjoy-Transaktion. Die Element-IDs haengen davon ab, welche
Bereiche (Kopf, Positionsuebersicht, Positionsdetail) auf- oder zugeklappt
sind. Zeichnen Sie deshalb **im gleichen Zustand** auf, in dem die Anwendung
spaeter arbeitet — am einfachsten: alle Bereiche einmal in den gewuenschten
Zustand bringen, SAP schliessen und wieder oeffnen (der Zustand wird gemerkt),
dann aufzeichnen.

**Bildfolge im Infosatz**
Zwischen Einstiegsbild und Einkaufsorganisationsdaten liegt je nach
Customizing noch das Bild „Allgemeine Daten". Die Anwendung drueckt bis zu
dreimal Enter, bis das Preisfeld sichtbar ist, und bricht danach kontrolliert
ab.

**Konditionsbild**
Das hauseigene „gueltig bis 31.12.2099" laesst sich nur ueber das
Konditionsbild setzen. Ist das bei Ihnen anders geloest, schalten Sie unter
*Einstellungen → SAP-Laufzeitverhalten* die Option „Infosatzpreis ueber das
Konditionsbild pflegen" ab; dann wird der Preis direkt im EKorg-Bild gesetzt.

**Berechtigungspruefung erst beim Sichern**
Manche Systeme melden fehlende Berechtigungen erst beim Sichern. Solche Faelle
erscheinen als fehlgeschlagene Position mit der SAP-Meldung im Ergebnis — die
uebrigen Positionen laufen weiter.

---

## 7. Checkliste vor dem ersten Echtlauf

- [ ] `sapgui/user_scripting = TRUE` gesetzt und im Profil verankert
- [ ] Scripting im SAP Logon aktiviert, beide Benachrichtigungen abgeschaltet
- [ ] Benutzervorgaben: Dezimal `1.234.567,89`, Datum `TT.MM.JJJJ`
- [ ] Alle benoetigten Feld-IDs eingetragen und **geprueft**
- [ ] Nachrichtenbild aufgezeichnet und geprueft
- [ ] „Beleg nicht sichern, wenn Nachrichten nicht entfernt werden konnten" ist aktiv
- [ ] Einkaufsorganisation, Werk, Einkaeufergruppe, Belegarten stimmen
- [ ] Gueltig-bis-Platzhalter entspricht Ihrer Hausregel (Standard 31.12.2099)
- [ ] Dry-Run-Lauf mit einem echten Angebot durchgefuehrt und Ergebnis geprueft
- [ ] Erster Echtlauf mit genau einer Position vereinbart
