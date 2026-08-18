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

logger = logging.getLogger(__name__)

__all__ = ["VbsField", "parse_vbs_recording", "describe_field",
           "detect_transaction", "TRANSACTION_NAMES"]

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

#: So startet eine Transaktion in einer Aufzeichnung.  Beide Schreibweisen
#: kommen vor: ueber das Kommandofeld ("/nME11") und ueber den direkten
#: Aufruf (session.startTransaction "ME11").
_TRANSACTION_PATTERNS = (
    re.compile(r'startTransaction\s+"?([A-Z0-9]{2,6})"?', re.I),
    re.compile(r'okcd"?\s*\)?\s*\.text\s*=\s*"/n([A-Z0-9]{2,6})"', re.I),
    re.compile(r'\.text\s*=\s*"/n([A-Z0-9]{2,6})"', re.I),
)


def detect_transaction(vbs_text: str) -> str:
    """Aus welcher Transaktion stammt die Aufzeichnung?

    Der Anwender soll nicht wissen muessen, was er aufgezeichnet hat --
    das steht in der Datei.  Wird nichts gefunden, ist das kein Fehler:
    dann wird eben nichts behauptet und der Anwender waehlt selbst.
    """
    if not vbs_text:
        return ""
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


def parse_vbs_recording(vbs_text: str) -> list[VbsField]:
    """Eine .vbs-Aufzeichnung parsen und alle Feld-IDs extrahieren.

    Sucht nach Zeilen, die `session.findById("...")` enthalten, und
    haelt fest, was dort eingegeben wurde.
    """
    if not vbs_text:
        return []

    fields = []
    lines = vbs_text.split("\n")

    # Regex fuer session.findById("...").text = "..." oder .value = ...
    pattern = re.compile(
        r'session\.findById\s*\(\s*"([^"]+)"\s*\)\s*\.\s*(\w+)\s*=\s*"?([^"]*)"?',
        re.IGNORECASE,
    )

    gesehen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or "findById" not in line:
            continue
        if line.startswith("'") or line.lower().startswith("rem "):
            continue  # Kommentarzeile
        match = pattern.search(line)
        if not match:
            continue
        field_id, field_type, value = match.groups()
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
