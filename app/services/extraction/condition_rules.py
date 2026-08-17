"""Erkennung von Zusatzkonditionen (Rabatte, Zuschlaege, Fracht, Skonto).

WARUM DIESE DATEI EXISTIERT
===========================
Bisher wurde aus einem Angebot nur der Bruttopreis uebernommen.  Ein Angebot
enthaelt aber regelmaessig mehr::

    "abzueglich 3 % Mengenrabatt"
    "zzgl. 45,00 EUR Frachtpauschale"
    "2 % Skonto bei 10 Tagen"

Diese Angaben gehoeren als *eigene* Kondition in den Infosatz.  Wuerde man sie
in den Nettopreis einrechnen, waere spaeter nicht mehr nachvollziehbar, woraus
der Preis entstanden ist -- und genau das ist im Einkauf die haeufigste Ursache
fuer Streit mit dem Lieferanten.

LEITLINIE
=========
**Bei Unklarheit lieber nichts erkennen.**  Ein falsch erkannter Rabatt
verfaelscht den Einkaufspreis, und zwar dauerhaft und unbemerkt.  Deshalb:

* Ein Zahlwert wird nur dann zur Kondition, wenn *in seiner unmittelbaren
  Umgebung* ein eindeutiges Schluesselwort steht (Rabatt, Zuschlag, Fracht,
  Skonto) oder wenigstens ein Richtungswort davor (abzgl./zzgl.).
* Absolute Betraege brauchen zwingend eine Waehrungsangabe.  "abzueglich 50"
  bleibt liegen -- 50 was?
* Steuerangaben ("zzgl. 19 % MwSt") sind keine Einkaufskondition und werden
  ausdruecklich ausgeschlossen.
* Preisangaben in der Naehe ("Preis von 12,85 EUR") werden ausgeschlossen,
  damit der Grundpreis nicht versehentlich zur Kondition wird.
* "frei Haus" bedeutet: es gibt *keine* Frachtkondition.  Erkannte
  Frachtwerte werden dann verworfen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from ...utils.parsing import detect_currency, normalize_whitespace, parse_decimal

logger = logging.getLogger(__name__)

__all__ = [
    "ConditionCandidate",
    "CONDITION_KINDS",
    "extract_conditions",
    "merge_head_and_position",
    "attach_conditions",
    "condition_kind_label",
]

#: Alle unterstuetzten Konditionsarten in der Reihenfolge, in der sie spaeter
#: in das Konditionsbild geschrieben werden.  Die Reihenfolge ist bewusst fest
#: verdrahtet und nicht die Reihenfolge des Fundes im Text: so sieht jeder
#: Infosatz gleich aus, unabhaengig davon, wie der Lieferant formuliert hat.
CONDITION_KINDS: tuple[str, ...] = (
    "discount_percent",
    "discount_absolute",
    "surcharge_percent",
    "surcharge_absolute",
    "freight_percent",
    "freight_absolute",
    "cash_discount",
)

#: Klartext fuer Meldungen und Oberflaeche
_KIND_LABELS: dict[str, str] = {
    "discount_percent": "Rabatt (%)",
    "discount_absolute": "Rabatt (Betrag)",
    "surcharge_percent": "Zuschlag (%)",
    "surcharge_absolute": "Zuschlag (Betrag)",
    "freight_percent": "Fracht (%)",
    "freight_absolute": "Fracht (Betrag)",
    "cash_discount": "Skonto",
}


def condition_kind_label(kind: str) -> str:
    """Konditionsart als deutscher Klartext."""
    return _KIND_LABELS.get(kind, kind)


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class ConditionCandidate:
    """Eine im Angebotstext erkannte Zusatzkondition.

    ``wert`` ist immer *positiv*.  Ob abgezogen oder aufgeschlagen wird, sagt
    allein ``art`` -- so kann ein Vorzeichenfehler nicht aus Versehen aus einem
    Rabatt einen Zuschlag machen.
    """

    art: str = ""
    wert: Decimal = Decimal("0")
    waehrung: str = ""
    quelltext: str = ""
    konfidenz: float = 0.0
    #: "kopf" oder "position" -- rein informativ fuer Protokoll und Oberflaeche
    ebene: str = "position"

    @property
    def ist_prozent(self) -> bool:
        return self.art.endswith("_percent") or self.art == "cash_discount"

    @property
    def ist_abzug(self) -> bool:
        """Mindert diese Kondition den Preis?"""
        return self.art.startswith("discount") or self.art == "cash_discount"

    def display(self) -> str:
        vorzeichen = "-" if self.ist_abzug else "+"
        if self.ist_prozent:
            wert = f"{_zahl(self.wert)} %"
        else:
            wert = f"{_zahl(self.wert)} {self.waehrung}".strip()
        return f"{condition_kind_label(self.art)} {vorzeichen}{wert}"

    def schluessel(self) -> tuple[str, str, str]:
        """Vergleichsschluessel zur Dublettenerkennung."""
        return (self.art, str(self.wert), self.waehrung)


def _zahl(wert: Decimal) -> str:
    """Dezimalzahl im deutschen Format, ohne ueberfluessige Nullen."""
    text = format(wert.normalize(), "f")
    if "." in text:
        ganz, _, rest = text.partition(".")
        rest = rest[:3]
        return f"{ganz},{rest}"
    return text


# ---------------------------------------------------------------------------
# Regelkatalog
# ---------------------------------------------------------------------------
#
# Alle Schluesselwoerter stehen in "gefalteter" Form: kleingeschrieben und ohne
# Umlaute (ae/oe/ue/ss).  Damit greifen deutsche Texte unabhaengig davon, ob
# der Lieferant "abzueglich", "abzüglich" oder "ABZGL." schreibt.
#
# Gesucht wird per Teiltreffer, damit auch zusammengesetzte Woerter erfasst
# werden: "Mengenrabatt", "Teuerungszuschlag", "Frachtpauschale".

#: Steuern sind keine Einkaufskondition -- hartes Ausschlusskriterium
_TAX_WORDS = ("mwst", "mehrwertsteuer", "umsatzsteuer", "ust.", "ust ", "vat",
              "sales tax", "steuer")

#: Frachtkosten
_FREIGHT_WORDS = ("fracht", "versandkost", "versandpauschale", "transportkost",
                  "freight", "shipping", "carriage", "porto")

#: Skonto
_CASH_WORDS = ("skonto", "cash discount", "settlement discount")

#: Rabatt / Abschlag
_DISCOUNT_WORDS = ("rabatt", "nachlass", "abschlag", "discount", "rebate")

#: Zuschlag / Aufschlag
_SURCHARGE_WORDS = ("zuschlag", "aufschlag", "surcharge", "extra charge",
                    "mehrpreis")

#: Richtungswoerter -- sie stehen IMMER vor dem Wert ("abzgl. 50,00 EUR")
_MINUS_WORDS = ("abzgl", "abzuegl", "abzueglich", "./.", "weniger", "minus",
                "less ", "deduct")
_PLUS_WORDS = ("zzgl", "zuzgl", "zuzuegl", "zuzueglich", "plus ", "zusaetzlich",
               "add ")

#: "frei Haus" & Co: es faellt KEINE Fracht an
_NO_FREIGHT_WORDS = ("frei haus", "frachtfrei", "franko", "free delivery",
                     "free shipping", "carriage paid", "lieferung frei",
                     "versandkostenfrei", "frei lieferung")

#: Woerter, die den Zahlwert als Preis ausweisen -- dann ist es keine Kondition
_PRICE_WORDS = ("preis", "price", "netto", "brutto", "kostet", "betrag",
                "summe", "total", "gesamt", "wert von", "je stueck",
                "stueckpreis", "unit price", "listenpreis")

#: Wieviel Text um den Zahlwert herum ausgewertet wird.  Bewusst knapp: je
#: weiter das Schluesselwort entfernt steht, desto unsicherer der Bezug.
_CONTEXT_BEFORE = 30
_CONTEXT_AFTER = 30
#: Fenster fuer den Preis-Ausschluss (unmittelbar davor)
_PRICE_WINDOW = 22

#: Waehrungsschluessel und -zeichen
_CURRENCIES = r"EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£"

#: Betragsmuster (deutsch wie englisch): 45,00 / 1.250,00 / 1,250.00 / 45
_AMOUNT = r"\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"

#: Zahlwerte, die ueberhaupt eine Kondition sein koennen: Prozentangabe oder
#: Betrag MIT Waehrung.  Eine nackte Zahl wird nie ausgewertet.
_VALUE = re.compile(
    r"(?P<pct>\d{1,3}(?:[.,]\d{1,3})?)\s*(?:%|\bprozent\b|\bpercent\b)"
    r"|(?P<cur_pre>" + _CURRENCIES + r")\s*(?P<amt_post>" + _AMOUNT + r")"
    r"|(?P<amt_pre>" + _AMOUNT + r")\s*(?P<cur_post>" + _CURRENCIES + r")",
    re.I,
)

#: Segmenttrenner: Zeilenumbruch, Semikolon, Aufzaehlung, Satzende.
#: Ein Punkt trennt nur, wenn ein Grossbuchstabe folgt -- sonst zerschnitte er
#: genau die Abkuerzungen, auf die es hier ankommt ("abzgl. 3 %", "zzgl. 45,00
#: EUR").  Ein Punkt zwischen Ziffern (12.500) trennt ohnehin nie.
_SEGMENT_SPLIT = re.compile(r"[\n\r;•]+|(?<!\d)\.\s+(?=[A-ZÄÖÜ])")


def _fold(text: str) -> str:
    """Text vergleichbar machen: klein, ohne Umlaute."""
    lowered = (text or "").lower()
    for umlaut, ersatz in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
                           ("ß", "ss")):
        lowered = lowered.replace(umlaut, ersatz)
    return lowered


def _contains(haystack: str, words: tuple[str, ...]) -> bool:
    return any(word in haystack for word in words)


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

def extract_conditions(text: str, settings=None) -> list[ConditionCandidate]:
    """Zusatzkonditionen aus einem Text ableiten.

    ``settings`` wird derzeit nur fuer die Obergrenze der Trefferzahl
    ausgewertet (``settings.conditions.max_additional_conditions``); der
    Parameter steht bewusst in der Signatur, damit der Regelkatalog spaeter
    kundenspezifisch gesteuert werden kann, ohne alle Aufrufer zu aendern.
    """
    if not text or not text.strip():
        return []

    grenze = 0
    if settings is not None:
        grenze = int(getattr(getattr(settings, "conditions", None),
                             "max_additional_conditions", 0) or 0)

    gefaltet_gesamt = _fold(text)
    keine_fracht = _contains(gefaltet_gesamt, _NO_FREIGHT_WORDS)

    treffer: list[ConditionCandidate] = []
    gesehen: set[tuple[str, str, str]] = set()

    for segment in _SEGMENT_SPLIT.split(text):
        segment = normalize_whitespace(segment)
        if not segment:
            continue
        for kandidat in _conditions_in_segment(segment):
            if keine_fracht and kandidat.art.startswith("freight"):
                logger.debug("Fracht verworfen ('frei Haus'): %s", kandidat.quelltext)
                continue
            schluessel = kandidat.schluessel()
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            treffer.append(kandidat)

    treffer.sort(key=lambda k: CONDITION_KINDS.index(k.art)
                 if k.art in CONDITION_KINDS else len(CONDITION_KINDS))
    if grenze > 0 and len(treffer) > grenze:
        # Nicht stillschweigend abschneiden: das Kappen protokolliert die
        # Schreibschicht.  Hier bleibt alles erhalten.
        logger.debug("%d Konditionen erkannt, Obergrenze ist %d", len(treffer), grenze)
    return treffer


def _conditions_in_segment(segment: str) -> list[ConditionCandidate]:
    """Alle Konditionen eines Satzes/einer Zeile."""
    funde = list(_VALUE.finditer(segment))
    ergebnis: list[ConditionCandidate] = []

    for index, fund in enumerate(funde):
        start, ende = fund.span()
        grenze_links = funde[index - 1].end() if index else 0
        grenze_rechts = funde[index + 1].start() if index + 1 < len(funde) else len(segment)

        davor = segment[max(grenze_links, start - _CONTEXT_BEFORE):start]
        danach = segment[ende:min(grenze_rechts, ende + _CONTEXT_AFTER)]

        art, konfidenz = _classify(davor, danach)
        if not art:
            continue

        prozent = fund.group("pct")
        if prozent is not None:
            wert = parse_decimal(prozent)
            waehrung = ""
            if wert is None or wert <= 0 or wert > Decimal(100):
                continue
            art_voll = art if art == "cash_discount" else f"{art}_percent"
        else:
            betrag = fund.group("amt_pre") or fund.group("amt_post")
            zeichen = fund.group("cur_post") or fund.group("cur_pre") or ""
            wert = parse_decimal(betrag)
            waehrung = detect_currency(zeichen)
            if wert is None or wert <= 0 or not waehrung:
                continue
            if art == "cash_discount":
                # Skonto ist im Infosatz eine Prozentkondition.  Ein absoluter
                # "Skontobetrag" ist zu unklar, um ihn zu uebernehmen.
                continue
            art_voll = f"{art}_absolute"

        ergebnis.append(ConditionCandidate(
            art=art_voll, wert=wert, waehrung=waehrung,
            quelltext=segment[:200], konfidenz=konfidenz,
        ))
    return ergebnis


def _classify(davor: str, danach: str) -> tuple[str, float]:
    """Konditionsart aus dem Umfeld eines Zahlwerts bestimmen.

    Liefert ``("", 0.0)``, wenn der Bezug nicht zweifelsfrei ist -- das ist der
    Normalfall und ausdruecklich gewollt.
    """
    links = _fold(davor)
    rechts = _fold(danach)
    umfeld = f"{links} {rechts}"

    # 1. Harte Ausschluesse
    if _contains(umfeld, _TAX_WORDS):
        return "", 0.0
    if _contains(links[-_PRICE_WINDOW:], _PRICE_WORDS):
        return "", 0.0

    richtung_minus = _contains(links, _MINUS_WORDS)
    richtung_plus = _contains(links, _PLUS_WORDS)

    # 2. Eindeutige Sachbegriffe -- sie schlagen jedes Richtungswort
    for woerter, art in ((_FREIGHT_WORDS, "freight"),
                         (_CASH_WORDS, "cash_discount"),
                         (_DISCOUNT_WORDS, "discount"),
                         (_SURCHARGE_WORDS, "surcharge")):
        if _contains(umfeld, woerter):
            passend = ((art in ("discount", "cash_discount") and richtung_minus)
                       or (art in ("surcharge", "freight") and richtung_plus))
            return art, 0.95 if passend else 0.9

    # 3. Nur ein Richtungswort: schwaecher, aber noch belastbar
    if richtung_minus and not richtung_plus:
        return "discount", 0.7
    if richtung_plus and not richtung_minus:
        return "surcharge", 0.7
    return "", 0.0


# ---------------------------------------------------------------------------
# Kopf- gegen Positionsebene
# ---------------------------------------------------------------------------

def merge_head_and_position(kopf: list[ConditionCandidate],
                            position: list[ConditionCandidate]
                            ) -> list[ConditionCandidate]:
    """Kopf- und Positionskonditionen zusammenfuehren.

    Grundsatz: **Die Position gewinnt.**  Nennt eine Position einen eigenen
    Rabatt, gilt genau dieser -- die Kopfangabe wird fuer diese Konditionsart
    verworfen.  Alles andere waere fachlich falsch: der Lieferant hat den
    Positionswert bewusst abweichend angegeben.
    """
    ergebnis = list(position)
    vorhandene_arten = {k.art for k in position}
    for kandidat in kopf:
        if kandidat.art in vorhandene_arten:
            continue
        uebernommen = ConditionCandidate(
            art=kandidat.art, wert=kandidat.wert, waehrung=kandidat.waehrung,
            quelltext=kandidat.quelltext, konfidenz=kandidat.konfidenz,
            ebene="kopf",
        )
        ergebnis.append(uebernommen)
    ergebnis.sort(key=lambda k: CONDITION_KINDS.index(k.art)
                  if k.art in CONDITION_KINDS else len(CONDITION_KINDS))
    return ergebnis


# ---------------------------------------------------------------------------
# Anbindung an den Import
# ---------------------------------------------------------------------------

def attach_conditions(offer, settings) -> list[str]:
    """Konditionen erkennen und an den Positionen des Angebots hinterlegen.

    Kopfebene ist der Angebotstext *ohne* die Zeilen, die bereits einer
    Position zugeordnet sind.  Ohne diese Trennung wuerde ein Positionsrabatt
    als Kopfrabatt auf alle uebrigen Positionen durchschlagen -- genau der
    Fehler, der einen Einkaufspreis unbemerkt verfaelscht.

    Liefert Protokollzeilen fuer ``offer.extraction_notes``.
    """
    notizen: list[str] = []
    positionen = list(getattr(offer, "positions", []) or [])
    if not positionen:
        return notizen

    kopf = extract_conditions(_head_text(offer, positionen), settings)

    mit_konditionen = 0
    for position in positionen:
        eigener_text = " ".join(
            teil for teil in (position.remarks or "", position.raw_text or "")
            if teil)
        eigene = extract_conditions(eigener_text, settings)
        for kandidat in eigene:
            kandidat.ebene = "position"
        position.conditions = merge_head_and_position(kopf, eigene)
        if position.conditions:
            mit_konditionen += 1

    if kopf:
        notizen.append(
            f"Kopfkonditionen erkannt: {', '.join(k.display() for k in kopf)} -- "
            "sie gelten fuer alle Positionen ohne eigene Angabe.")
    if mit_konditionen:
        notizen.append(
            f"{mit_konditionen} Position(en) tragen Zusatzkonditionen "
            "(Rabatt/Zuschlag/Fracht/Skonto). Bitte pruefen: sie werden als "
            "eigene Konditionszeilen in den Infosatz geschrieben und NICHT in "
            "den Preis eingerechnet.")
    return notizen


def _head_text(offer, positionen) -> str:
    """Angebotstext ohne die Zeilen, die zu einer Position gehoeren."""
    roh = getattr(offer, "raw_text", "") or ""
    if not roh:
        return ""
    positionszeilen = set()
    for position in positionen:
        for zeile in (position.raw_text or "").splitlines():
            zeile = normalize_whitespace(zeile)
            if zeile:
                positionszeilen.add(zeile)
    if not positionszeilen:
        return roh
    behalten = [zeile for zeile in roh.splitlines()
                if normalize_whitespace(zeile) not in positionszeilen]
    return "\n".join(behalten)
