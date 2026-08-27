"""SAP GUI Scripting: .vbs-Aufzeichnung parsen und Feld-IDs extrahieren.

Eine .vbs-Datei von der SAP GUI Scripting-Aufzeichnung sieht so aus:

    session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"
    session.findById("wnd[0]/usr/ctxtEINA-MATNR").text = "4711001"
    session.findById("wnd[0]/usr/ctxtEIKA-EKORG").text = "1000"

Aus so einer Zeile extrahiert der Parser:
- Die Feld-ID: "wnd[0]/usr/ctxtEINA-LIFNR"
- Den Wert, der reingeschrieben wurde: "100234"
- Den Feldtyp: "text" oder "value" (in Klammern nach findById)

Das reicht, um dem Nutzer zu zeigen, was wo eingetragen wurde, ohne dass
er die Feldnamen kennen muss.

Dieser Parser ist "forgiving" -- wenn die .vbs beschaedigt oder
ungewoehnlich formatiert ist, extrahiert er trotzdem das Maximum, anstatt
abzubrechen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..utils.textkodierung import entferne_nullzeichen

logger = logging.getLogger(__name__)

__all__ = ["VbsField", "parse_vbs_recording", "describe_field",
           "detect_transaction", "saubere_aufzeichnung", "TRANSACTION_NAMES"]

#: Transaktionen, die dieses Werkzeug kennt -- Code auf Klartext.
TRANSACTION_NAMES = {
    "ME11": "Einkaufsinfosatz anlegen",
    "ME12": "Einkaufsinfosatz aendern",
    "ME13": "Einkaufsinfosatz anzeigen",
    "ME01": "Orderbuch pflegen",
    "ME03": "Orderbuch anzeigen",
    "ME31K": "Mengenkontrakt anlegen",
    "ME32K": "Mengenkontrakt aendern",
    "ME33K": "Mengenkontrakt anzeigen",
    "ME21N": "Bestellung anlegen",
    "ME22N": "Bestellung aendern",
    "ME23N": "Bestellung anzeigen",
    "MM03": "Material anzeigen",
    "XK03": "Lieferant anzeigen",
}

#: Felder, die zur Navigation gehoeren und keine Daten tragen.
_IGNORIERTE_FELDER = re.compile(r"(?:tbar\[|/okcd$|mbar/|sbar)", re.I)

#: Ein Unterstrich am Zeilenende setzt die Anweisung in der naechsten
#: Zeile fort.  Er zaehlt nur als Fortsetzung, wenn er allein hinter
#: Leerraum steht -- in einem Feldnamen ("SUB_0100") ist er ein Buchstabe.
_FORTSETZUNG = re.compile(r"[ \t]_[ \t]*\r?\n[ \t]*")

#: Ein einzelner ``findById("...")``-Aufruf.  Der Objektname davor wird
#: bewusst nicht festgelegt: der Recorder schreibt ``session``, wer die
#: Aufzeichnung nachbearbeitet, benutzt oft eine eigene Variable
#: (``sess``, ``oSession``).  Am Aufruf selbst aendert das nichts.
_FIND_BY_ID = re.compile(r'findById\s*\(\s*"([^"]*)"\s*\)', re.I)

#: Eine vollstaendige Zuweisung: eine -- moeglicherweise verkettete --
#: Folge von ``findById``-Aufrufen, danach die Eigenschaft und der Wert.
#: Verkettet wird, wenn die Aufzeichnung ueber einen Subscreen geht:
#: ``session.findById("wnd[0]/usr/subSUB0:...").findById("ctxtEINA-LIFNR")``.
_ZUWEISUNG = re.compile(
    r'((?:findById\s*\(\s*"[^"]*"\s*\)\s*\.?\s*)+)'
    r'([A-Za-z_]\w*)\s*=\s*(.*)$',
    re.I,
)

#: So startet eine Transaktion in einer Aufzeichnung.  Beide Schreibweisen
#: kommen vor: ueber das Kommandofeld ("/nME11") und ueber den direkten
#: Aufruf (session.startTransaction "ME11").  Letzterer wird in VBS mit
#: und ohne Klammern geschrieben -- beides ist dasselbe Statement.
_TRANSACTION_PATTERNS = (
    re.compile(r'startTransaction\s*\(?\s*"?([A-Z0-9]{2,6})"?', re.I),
    re.compile(r'okcd"?\s*\)?\s*\.text\s*=\s*"/n([A-Z0-9]{2,6})"', re.I),
    re.compile(r'\.text\s*=\s*"/n([A-Z0-9]{2,6})"', re.I),
)


def saubere_aufzeichnung(vbs_text: str) -> str:
    """Aufzeichnung in die Form bringen, in der sie zeilenweise lesbar ist.

    Zwei Dinge stehen dem im Weg:

    *Nullzeichen.*  Beim Lesen einer Datei ist die Kodierung geklaert,
    bevor der Text hier ankommt.  Eingefuegt werden kann er aber aus
    einem Editor, der die UTF-16-Aufzeichnung selbst falsch geoeffnet
    hat -- dann steht zwischen je zwei Buchstaben ein Nullzeichen und
    keine einzige Zeile passt mehr auf das Muster.  Sie herauszunehmen
    macht die Zeilen wieder auswertbar; die Feld-IDs bestehen ohnehin
    nur aus ASCII, es geht also nichts verloren.

    *Zeilenfortsetzungen.*  Ein Unterstrich am Zeilenende setzt in VBS
    die Anweisung in der naechsten Zeile fort.  Lange Feld-IDs werden so
    umbrochen -- ein zeilenweiser Parser sieht dann zwei Bruchstuecke,
    von denen keines fuer sich ein Treffer ist.  Zusammengefuegt ergeben
    sie wieder eine Anweisung.
    """
    text = entferne_nullzeichen(vbs_text)
    if "_" in text:
        text = _FORTSETZUNG.sub(" ", text)
    return text


def detect_transaction(vbs_text: str) -> str:
    """Aus welcher Transaktion stammt die Aufzeichnung?

    Der Anwender soll nicht wissen muessen, was er aufgezeichnet hat --
    das steht in der Datei.  Wird nichts gefunden, ist das kein Fehler:
    dann wird eben nichts behauptet und der Anwender waehlt selbst.
    """
    if not vbs_text:
        return ""
    vbs_text = saubere_aufzeichnung(vbs_text)
    for muster in _TRANSACTION_PATTERNS:
        treffer = muster.search(vbs_text)
        if treffer:
            code = treffer.group(1).upper()
            if code in TRANSACTION_NAMES:
                return code
    return ""


@dataclass
class VbsField:
    """Ein Feld aus der SAP GUI Scripting-Aufzeichnung."""

    field_id: str
    """Die SAP-interne Feld-ID, z. B. "wnd[0]/usr/ctxtEINA-LIFNR"."""
    value: str
    """Der Wert, der dort eingegeben wurde."""
    field_type: str
    """Der Feldtyp: "text", "value", oder leer wenn unbekannt."""

    def short_id(self) -> str:
        """Kuerzel fuer die Anzeige: nur der Feldname ohne Steuerungstyp.

        "wnd[0]/usr/ctxtEINA-LIFNR" wird zu "EINA-LIFNR".  Das Praefix
        bezeichnet nur die Art des Bedienelements (Textfeld, Auswahlfeld
        ...) und sagt ueber die Bedeutung nichts aus.
        """
        letzter = self.field_id.split("/")[-1]
        for praefix in ("ctxt", "txt", "cmbx", "cmb", "chk", "rad", "lbl", "btn"):
            if letzter.startswith(praefix):
                return letzter[len(praefix):]
        return letzter

    def looks_like_number(self) -> bool:
        """Sieht der Wert wie eine Nummernkreis aus (Lieferant, Material)?"""
        if not self.value:
            return False
        return bool(re.match(r"^[\d\-]+$", self.value))

    def looks_like_currency(self) -> bool:
        """Sieht aus wie Dezimalzahl mit Waehrung."""
        return bool(re.search(r"[0-9]+[.,][0-9]{2}", self.value))


def _vbs_string(rohtext: str) -> str:
    """Den Wert einer Zuweisung lesen -- als VBS-Zeichenkette.

    Rechts vom ``=`` steht in einer Aufzeichnung fast immer ein
    Zeichenkettenliteral, und darin verdoppelt VBS das
    Anfuehrungszeichen: ``"Dichtring 1"" NPT"`` bedeutet ``Dichtring 1"
    NPT``.  Wer nur bis zum naechsten Anfuehrungszeichen liest, schneidet
    genau dort ab -- und im Infosatz landet ein halber Kurztext.

    Was nicht als Literal beginnt (``= True``, ``= 0``), wird
    unveraendert uebernommen; nachgestellte Kommentare fallen weg.
    """
    rohtext = rohtext.strip()
    if not rohtext.startswith('"'):
        # Kein Literal: alles bis zu einem Kommentarzeichen gilt.
        return rohtext.split("'")[0].strip()
    teile: list[str] = []
    stelle = 1
    while stelle < len(rohtext):
        zeichen = rohtext[stelle]
        if zeichen == '"':
            if rohtext[stelle + 1:stelle + 2] == '"':
                teile.append('"')      # verdoppelt = ein Anfuehrungszeichen
                stelle += 2
                continue
            break                      # hier endet das Literal
        teile.append(zeichen)
        stelle += 1
    return "".join(teile)


def _kette_zu_id(kette: str) -> str:
    """Aus verketteten ``findById``-Aufrufen eine Feld-ID machen.

    Geht die Aufzeichnung ueber einen Subscreen, teilt der Recorder den
    Pfad auf zwei Aufrufe auf::

        session.findById("wnd[0]/usr/subSUB0:SAPLMEGUI:0030") _
               .findById("ctxtEINA-LIFNR").text = "100234"

    Gemeint ist dasselbe Feld wie bei der einteiligen Schreibweise.
    Zusammengesetzt ergibt die Kette wieder den vollen Pfad -- und damit
    eine ID, die zu einer frueher gespeicherten Zuordnung passt.
    """
    teile = [t.strip("/") for t in _FIND_BY_ID.findall(kette) if t.strip("/")]
    return "/".join(teile)


def parse_vbs_recording(vbs_text: str) -> list[VbsField]:
    """Eine .vbs-Aufzeichnung parsen und alle Feld-IDs extrahieren.

    Sucht nach Zeilen mit ``findById("...")`` und haelt fest, was dort
    eingegeben wurde.  Der Objektname davor spielt keine Rolle: der
    Recorder schreibt ``session``, eine nachbearbeitete Aufzeichnung
    benutzt oft eine eigene Variable.  Auch die Schreibweise ist frei --
    VBS unterscheidet keine Gross- und Kleinschreibung, und eine
    Aufzeichnung, die sich nur darin unterscheidet, ist dieselbe.
    """
    if not vbs_text:
        return []

    fields = []
    lines = saubere_aufzeichnung(vbs_text).split("\n")

    gesehen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or "findbyid" not in line.lower():
            continue
        if line.startswith("'") or line.lower().startswith("rem "):
            continue  # Kommentarzeile
        match = _ZUWEISUNG.search(line)
        if not match:
            continue
        kette, field_type, rohwert = match.groups()
        field_id = _kette_zu_id(kette)
        value = _vbs_string(rohwert)
        if not field_id or not value:
            continue
        if field_type.lower() not in ("text", "value"):
            continue  # press(), select() usw. tragen keinen Wert
        if _IGNORIERTE_FELDER.search(field_id):
            # Das Kommandofeld ("/nME11") und die Werkzeugleisten sind
            # Navigation, keine Daten -- sie wuerden die Liste nur
            # verlaengern und den Anwender ratlos machen.
            continue
        if field_id in gesehen:
            # Aufzeichnungen setzen dasselbe Feld oft mehrfach.  Der erste
            # Wert ist der aussagekraeftige -- spaetere sind meist
            # Korrekturen oder Wiederholungen beim Durchklicken.
            continue
        gesehen.add(field_id)
        fields.append(VbsField(field_id=field_id, value=value,
                               field_type=field_type))

    logger.info("VBS geparst: %d Felder gefunden", len(fields))
    return fields


def describe_field(field: VbsField) -> str:
    """Eine Feld-ID verstaendlich beschreiben."""
    short = field.short_id()
    hints = []
    if field.looks_like_number():
        hints.append("sieht nach Nummer aus")
    if field.looks_like_currency():
        hints.append("sieht nach Preis aus")
    hint_text = f" ({', '.join(hints)})" if hints else ""
    return f"{short} = {field.value!r}{hint_text}"
