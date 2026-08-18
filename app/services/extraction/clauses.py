"""Kaufmaennische Klauseln: wenn der Preis stimmt und trotzdem nicht gilt.

Das Problem
-----------
Bisher behandelt dieses Werkzeug einen Preis als richtig oder falsch
gelesen.  Es gibt aber einen dritten Zustand, der gefaehrlicher ist als
beide: **richtig gelesen und trotzdem unvollstaendig verstanden**.

    "Dichtring NBR 40x52x7 ... 2,95 EUR/ST"
    "Preise freibleibend, zzgl. tagesaktuellem Legierungszuschlag."

2,95 ist korrekt abgelesen.  Es ist trotzdem nicht der Preis, den der
Einkauf zahlt.  Wandert die Zahl ohne ihre Klausel in den Infosatz, steht
dort eine Wahrheit, die keine ist -- und niemand sieht es der Zahl an.

Der Grundsatz
-------------
Klauseln werden **erkannt und gemeldet, niemals eingerechnet**.

Das ist keine Bequemlichkeit, sondern Absicht.  Ein Legierungszuschlag
ist tagesabhaengig, eine Preisgleitklausel braucht einen Indexstand, ein
Skonto haengt vom Zahlungsverhalten ab.  Wuerde dieses Werkzeug daraus
einen "effektiven Preis" rechnen, stuende in SAP eine Zahl, die in keinem
Beleg steht und die niemand nachvollziehen kann.  Lieber ein sichtbarer
Hinweis als eine unsichtbare Rechnung.

Was hier NICHT hingehoert
-------------------------
Zuschlaege, die als eigene POSITION im Beleg stehen (Werkzeugkosten,
Frachtposition), gehoeren nach ``position_kinds.py``.  Hier geht es um
Fliesstext: Saetze, die den Preis der uebrigen Positionen beruehren,
ohne selbst eine Position zu sein.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = [
    "PriceClause",
    "CLAUSE_LABELS",
    "SEVERITY_BY_KIND",
    "find_clauses",
    "clause_summary",
]

# --------------------------------------------------------------------------
# Klauselarten
# --------------------------------------------------------------------------
KIND_ESCALATION = "preisgleitklausel"
KIND_ALLOY = "legierungszuschlag"
KIND_SURCHARGE = "zuschlag"
KIND_SMALL_QTY = "mindermengenzuschlag"
KIND_NON_BINDING = "freibleibend"
KIND_VALIDITY = "bindefrist"
KIND_CASH_DISCOUNT = "skonto"
KIND_INCOTERM = "incoterm"
KIND_EXCHANGE_RATE = "wechselkurs"
KIND_FREIGHT = "fracht"

CLAUSE_LABELS: dict[str, str] = {
    KIND_ESCALATION: "Preisgleitklausel",
    KIND_ALLOY: "Legierungszuschlag",
    KIND_SURCHARGE: "Zuschlag",
    KIND_SMALL_QTY: "Mindermengenzuschlag",
    KIND_NON_BINDING: "Freibleibendes Angebot",
    KIND_VALIDITY: "Bindefrist",
    KIND_CASH_DISCOUNT: "Skonto",
    KIND_INCOTERM: "Lieferbedingung (Incoterm)",
    KIND_EXCHANGE_RATE: "Wechselkursklausel",
    KIND_FREIGHT: "Frachtkosten",
}

#: Wie sehr beruehrt die Klausel den Preis, der nach SAP geschrieben wird?
#:
#: "hoch"   -- der genannte Preis ist ohne diese Angabe unvollstaendig.
#:             Der Anwender muss das sehen, bevor er schreibt.
#: "mittel" -- kaufmaennisch wichtig, aber der Stueckpreis bleibt der
#:             Stueckpreis (Skonto, Bindefrist).
#: "info"   -- festhalten, damit es nicht verloren geht.
SEVERITY_BY_KIND: dict[str, str] = {
    KIND_ESCALATION: "hoch",
    KIND_ALLOY: "hoch",
    KIND_SURCHARGE: "hoch",
    KIND_SMALL_QTY: "hoch",
    KIND_NON_BINDING: "hoch",
    KIND_EXCHANGE_RATE: "hoch",
    KIND_FREIGHT: "mittel",
    KIND_CASH_DISCOUNT: "mittel",
    KIND_VALIDITY: "mittel",
    KIND_INCOTERM: "info",
}


@dataclass
class PriceClause:
    """Eine im Fliesstext gefundene Klausel."""

    kind: str
    #: Der Satz, in dem sie steht -- damit der Anwender nachlesen kann,
    #: statt dem Werkzeug glauben zu muessen.
    quote: str
    #: Zahlenwert, falls einer eindeutig dazugehoert (Skontosatz,
    #: Bindefrist in Tagen).  Bewusst optional: meistens gibt es keinen.
    value: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return CLAUSE_LABELS.get(self.kind, self.kind)

    @property
    def severity(self) -> str:
        return SEVERITY_BY_KIND.get(self.kind, "info")

    @property
    def affects_price(self) -> bool:
        """Ist der genannte Preis ohne diese Angabe unvollstaendig?"""
        return self.severity == "hoch"

    def text(self) -> str:
        teile = [self.label]
        if self.value:
            teile.append(self.value)
        return ": ".join(teile)


# --------------------------------------------------------------------------
# Muster
# --------------------------------------------------------------------------
# Grundsatz bei den Mustern: lieber eine Klausel uebersehen als eine
# erfinden.  Ein Fehlalarm bei jedem zweiten Angebot fuehrt dazu, dass die
# Hinweise weggeklickt werden -- und dann wirkt auch der echte nicht mehr.
_MUSTER: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_ESCALATION, re.compile(
        r"preisgleit\w*|stoffpreisgleit\w*|gleitklausel|"
        r"preis(?:e|en)?\s+(?:sind\s+)?(?:an\s+die\s+)?(?:entwicklung|"
        r"notierung|index)\w*\s+gekoppelt|"
        r"price\s+escalation|price\s+adjustment\s+clause", re.I)),

    (KIND_ALLOY, re.compile(
        r"legierungszuschlag|legierungs-\s*zuschlag|alloy\s+surcharge|"
        r"schrott(?:preis)?zuschlag", re.I)),

    (KIND_SMALL_QTY, re.compile(
        r"mindermengen(?:zuschlag)?|mindermenge|"
        r"kleinmengenzuschlag|minimum\s+order\s+surcharge", re.I)),

    (KIND_SURCHARGE, re.compile(
        r"(?:kupfer|energie|material|rohstoff|teuerungs|inflations|"
        r"nickel|chrom|molybdaen|molybd\w*n|zoll|maut)"
        r"[-\s]*zuschlag\w*|"
        r"zzgl\.?\s+[^.\n]{0,40}zuschlag|"
        r"copper\s+surcharge|energy\s+surcharge", re.I)),

    (KIND_NON_BINDING, re.compile(
        r"freibleibend|unverbindlich(?:es)?\s+angebot|"
        r"preis(?:e|aenderungen|\w*)\s+vorbehalten|"
        r"aenderungen\s+vorbehalten|subject\s+to\s+change", re.I)),

    (KIND_VALIDITY, re.compile(
        r"bindefrist|angebot\s+(?:ist\s+)?(?:gueltig|g\w+ltig)\s+bis|"
        r"g(?:ue|ü)ltig(?:keit)?\s+(?:bis|f(?:ue|ü)r)\s|"
        r"zuschlagsfrist|valid\s+(?:until|for)\s|"
        r"quotation\s+valid", re.I)),

    (KIND_CASH_DISCOUNT, re.compile(
        r"skonto|cash\s+discount|"
        r"\d{1,2}\s*%\s*(?:bei\s+)?zahlung\s+(?:innerhalb|binnen)", re.I)),

    (KIND_EXCHANGE_RATE, re.compile(
        r"wechselkurs\w*|kursklausel|umrechnungskurs|"
        r"exchange\s+rate\s+(?:clause|basis)|"
        r"kurs\w*\s+vorbehalten", re.I)),

    (KIND_FREIGHT, re.compile(
        r"zzgl\.?\s+(?:fracht|versand|transport|verpackung)\w*|"
        r"fracht(?:kosten)?\s+(?:werden\s+)?(?:separat|gesondert|zusaetzlich)|"
        r"versandkosten\s+(?:werden\s+)?(?:separat|gesondert)|"
        r"plus\s+freight", re.I)),

    (KIND_INCOTERM, re.compile(
        r"\b(?:EXW|FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP|DDU)\b"
        r"(?:\s+[A-ZÄÖÜ][\w.-]*|\s+\d{4,5}\b)", re.I)),
)

#: Zusatzangaben, die zu einer Klausel gehoeren koennen
_SKONTO_WERT = re.compile(
    r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%[^.\n]{0,30}?(\d{1,3})\s*tag", re.I)
_FRIST_TAGE = re.compile(r"(\d{1,3})\s*tag(?:e|en)?\b", re.I)
_FRIST_DATUM = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\b")
_MINDERMENGE = re.compile(
    r"unter\s+(\d[\d.,]*)\s*(?:st(?:ue|ü)ck|stk|st\b|kg|m\b)", re.I)

#: Satzgrenzen -- fuer das Zitat, das dem Anwender gezeigt wird.
_SATZ = re.compile(r"[^.!?\n]*[.!?\n]|[^.!?\n]+$")

#: Abkuerzungen, die auf einen Punkt enden, ohne einen Satz zu beenden.
#:
#: Ohne diese Liste zerschneidet der Satztrenner "Alle Preise zzgl. Fracht"
#: hinter "zzgl." -- und die Klausel wird nie gefunden, weil das Stichwort
#: im naechsten Bruchstueck liegt.  Gerade in Angebotsfusszeilen stehen
#: Abkuerzungen dicht an dicht, also faellt das schwer ins Gewicht.
_ABKUERZUNGEN = (
    "zzgl", "abzgl", "inkl", "exkl", "ca", "evtl", "ggf", "bzw", "usw",
    "u.a", "z.B", "z. B", "d.h", "i.d.R", "gem", "lt", "Nr", "Art", "Pos",
    "Stk", "St", "Bsp", "max", "min", "netto", "brutto", "Mio", "Mrd",
    "Fa", "Str", "Tel", "ggfs", "ff", "vgl", "s.o", "s.u",
)
#: Platzhalter fuer den Punkt einer Abkuerzung waehrend der Trennung.
_PUNKT_MARKE = "\x00"
_ABK_MUSTER = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ABKUERZUNGEN) + r")\.",
    re.I)
#: Punkt zwischen zwei Ziffern -- Datum oder Tausendertrennzeichen.
_ZIFFERNPUNKT = re.compile(r"(\d)\.(\d)")

#: Laenge des Zitats.  Lang genug zum Verstehen, kurz genug fuers Protokoll.
_MAX_ZITAT = 200


def _saetze(text: str) -> list[str]:
    """In Saetze zerlegen, ohne an Abkuerzungen zu zerbrechen."""
    if not text:
        return []
    # Punkte, die keinen Satz beenden, voruebergehend maskieren.
    geschuetzt = _ABK_MUSTER.sub(lambda t: t.group(1) + _PUNKT_MARKE, text)
    # ... dazu Punkte zwischen Ziffern: Datumsangaben ("30.09.2026") und
    # Tausenderpunkte ("1.234,56") zerfielen sonst in Bruchstuecke, und die
    # Bindefrist stuende ohne ihr Datum da.
    geschuetzt = _ZIFFERNPUNKT.sub(r"\1" + _PUNKT_MARKE + r"\2", geschuetzt)
    ergebnis = []
    for treffer in _SATZ.finditer(geschuetzt):
        satz = treffer.group(0).replace(_PUNKT_MARKE, ".").strip()
        if satz:
            ergebnis.append(satz)
    return ergebnis


#: Alles, was kein Buchstabe und keine Ziffer ist -- fuer den Vergleich.
_NUR_WORTE = re.compile(r"[^0-9a-zaeoeuess]+", re.I)


def _vergleichsform(satz: str) -> str:
    """Fassung eines Satzes, die von Satzzeichen absieht.

    Dient nur dem Erkennen von Dubletten, nie der Anzeige.
    """
    return _NUR_WORTE.sub(" ", satz.lower()).strip()


def _kuerze(satz: str) -> str:
    """Das Zitat fuer die Anzeige aufbereiten.

    Aus einer Tabelle gelesener Text kommt oft als zusammengelaufene Zeile
    an: "Angebot Nr.: | ANG-2026-04711 | | Zahlungsbedingungen: | 30 Tage
    netto".  Als Beleg fuer eine Klausel ist das unlesbar.  Leere Zellen
    und die Trennstriche werden deshalb zu Satzzeichen zusammengezogen --
    der Wortlaut bleibt unveraendert, nur das Geruest der Tabelle faellt
    weg.
    """
    satz = re.sub(r"\s*\|\s*", " | ", satz)
    satz = re.sub(r"(?:\|\s*)+\|", "|", satz)          # leere Zellen
    satz = re.sub(r"\s*\|\s*", " – ", satz)            # Rest als Gedankenstrich
    satz = re.sub(r"\s+", " ", satz).strip(" .;:-–")
    if len(satz) <= _MAX_ZITAT:
        return satz
    return satz[:_MAX_ZITAT - 1].rstrip() + "…"


def _zusatzwert(kind: str, satz: str) -> tuple[str, str]:
    """Zahlenwert und Erlaeuterung zu einer Klausel, sofern eindeutig."""
    if kind == KIND_CASH_DISCOUNT:
        treffer = _SKONTO_WERT.search(satz)
        if treffer:
            return (f"{treffer.group(1)} % bei Zahlung in {treffer.group(2)} Tagen",
                    "Skonto wird NICHT in den Preis eingerechnet -- der "
                    "Infosatz fuehrt den Bruttopreis.")
        return "", ""
    if kind == KIND_VALIDITY:
        datum = _FRIST_DATUM.search(satz)
        if datum:
            return f"bis {datum.group(1)}", ""
        tage = _FRIST_TAGE.search(satz)
        if tage:
            return f"{tage.group(1)} Tage", ""
        return "", ""
    if kind == KIND_SMALL_QTY:
        treffer = _MINDERMENGE.search(satz)
        if treffer:
            return f"unter {treffer.group(1)}", ""
        return "", ""
    return "", ""


def find_clauses(text: str) -> list[PriceClause]:
    """Klauseln im Fliesstext eines Angebots finden.

    Es wird satzweise gesucht, damit das Zitat stimmt: Der Anwender soll
    nachlesen koennen, worauf sich ein Hinweis stuetzt, statt dem Werkzeug
    glauben zu muessen.
    """
    if not text or not text.strip():
        return []

    gefunden: list[PriceClause] = []
    gesehen: set[tuple[str, str]] = set()

    for satz in _saetze(text):
        if len(satz) > 400:
            # Ein "Satz" dieser Laenge ist meist eine zusammengelaufene
            # Tabellenzeile.  Das Zitat waere unbrauchbar.
            continue
        for kind, muster in _MUSTER:
            if not muster.search(satz):
                continue
            zitat = _kuerze(satz)
            # Der Rohtext eines Belegs enthaelt denselben Satz oft zweimal:
            # einmal wie gelesen und einmal aus der zerlegten Tabelle, wo
            # Komma oder Semikolon durch das Trennzeichen ersetzt sind.
            # Der Schluessel muss davon absehen, sonst steht jede Klausel
            # doppelt im Protokoll.
            schluessel = (kind, _vergleichsform(zitat))
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            wert, detail = _zusatzwert(kind, satz)
            gefunden.append(PriceClause(kind=kind, quote=zitat,
                                        value=wert, detail=detail))

    if gefunden:
        logger.info("Klauseln gefunden: %s",
                    ", ".join(sorted({k.label for k in gefunden})))
    return gefunden


def clause_summary(clauses: list[PriceClause]) -> str:
    """Klartextsatz fuer Protokoll und Oberflaeche."""
    if not clauses:
        return ""
    preisrelevant = [k for k in clauses if k.affects_price]
    teile = []
    if preisrelevant:
        namen = sorted({k.label for k in preisrelevant})
        teile.append(
            "Der genannte Preis ist ohne diese Angabe(n) unvollstaendig: "
            + ", ".join(namen)
            + ". Sie werden bewusst NICHT eingerechnet -- bitte pruefen, "
              "was in den Infosatz gehoert.")
    uebrige = sorted({k.label for k in clauses if not k.affects_price})
    if uebrige:
        teile.append("Ausserdem vermerkt: " + ", ".join(uebrige) + ".")
    return " ".join(teile)
