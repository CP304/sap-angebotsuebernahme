"""Preisangaben haerten: Preiseinheit, Preisspannen, Waehrung je Position.

``app/utils/parsing.py`` bleibt bewusst unangetastet -- es wird von der ganzen
Anwendung benutzt.  Die angebotsspezifischen Sonderfaelle liegen deshalb hier:

* **Preis je Mengeneinheit** -- "12,85 EUR/100 St", "EUR 4.50 per 1000 pcs",
  "je 100 Stueck 12,85".  Wird die Preiseinheit uebersehen, ist der Preis um
  Faktor 100 falsch.  Das ist der teuerste Einzelfehler ueberhaupt, denn er
  faellt in SAP erst bei der Rechnungspruefung auf.
* **Preisspannen** -- "12,00 - 14,00 EUR".  Hier gibt es keinen richtigen
  Wert; es wird nichts uebernommen, sondern nachgefragt.
* **Waehrung je Position** -- steht in der Preiszelle "USD", gilt USD fuer
  diese Position, auch wenn der Kopf EUR sagt.

Zusaetzlich Zeilenfilter fuer Fusszeilen, Seitenzahlen und "Uebertrag" --
solche Zeilen duerfen niemals Positionen werden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from ...utils.parsing import detect_currency, normalize_uom, normalize_whitespace

__all__ = [
    "PriceInfo",
    "parse_price_text",
    "price_unit_from_header",
    "footer_price_unit",
    "is_on_request",
    "is_page_noise_row",
    "is_carryover_row",
]

# --------------------------------------------------------------------------
# Stellschrauben
# TODO: bei Bedarf in ExtractionSettings verschieben
# --------------------------------------------------------------------------

#: Groesste Preiseinheit, die noch plausibel ist (SAP erlaubt 5 Stellen).
MAX_PRICE_UNIT = 99999

#: Mengeneinheiten, die hinter einer Preiseinheit stehen duerfen
_UNIT_WORDS = (r"st(?:ue|ü)ck|stk|st|pcs|pc|pieces|piece|ea|each|"
               r"m(?:eter)?|kg|g|l(?:iter)?|set|satz|paar|pair|rolle|roll|"
               r"pak|packung|karton|box|m2|m3|qm")

#: Zahl in deutscher *und* englischer Schreibweise.  Die Reihenfolge der
#: Alternativen ist wichtig: die Formen mit Tausendertrennung muessen zuerst
#: greifen, sonst bliebe von "1.234,56" nur die "1" uebrig.
_NUMBER = (r"\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,4})?"
           r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,4})?"
           r"|\d+(?:[.,]\d{1,4})?")
_CURRENCY = r"EUR|EURO|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£"

#: "12,85 EUR/100 St", "4.50 per 1000 pcs", "12,85 / 100 Stk"
_PER_UNIT_RE = re.compile(
    rf"(?P<pre>{_CURRENCY})?\s*"
    rf"(?P<value>{_NUMBER})\s*"
    rf"(?P<post>{_CURRENCY})?\s*"
    rf"(?:/|je|pro|per|fuer|für)\s*"
    rf"(?:(?P<unit>\d{{1,5}})(?!\d)\s*)?"
    rf"(?P<uom>{_UNIT_WORDS})?\.?",
    re.I,
)

#: "je 100 Stueck 12,85 EUR" -- Einheit steht *vor* dem Preis
_UNIT_FIRST_RE = re.compile(
    rf"(?:je|pro|per|fuer|für)\s+(?P<unit>\d{{1,5}})(?!\d)\s*"
    rf"(?P<uom>{_UNIT_WORDS})?\.?\s*[:=]?\s*"
    rf"(?P<pre>{_CURRENCY})?\s*(?P<value>{_NUMBER})\s*(?P<post>{_CURRENCY})?",
    re.I,
)

#: "12,00 - 14,00 EUR", "EUR 12.00 to 14.00", "zwischen 12,00 und 14,00"
_RANGE_RE = re.compile(
    rf"(?P<pre>{_CURRENCY})?\s*(?P<low>{_NUMBER})\s*(?:{_CURRENCY})?\s*"
    rf"(?:-|–|—|bis|to|\.\.\.|und)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<post>{_CURRENCY})?",
    re.I,
)

#: Reine Zahl (mit optionaler Waehrung)
_PLAIN_RE = re.compile(
    rf"^\s*(?P<pre>{_CURRENCY})?\s*(?P<value>{_NUMBER})\s*(?P<post>{_CURRENCY})?\s*$",
    re.I,
)

#: "Seite 2 von 3", "Blatt 2", "Page 2 of 3", "- 2 -"
_PAGE_RE = re.compile(
    r"^\s*(?:seite|blatt|page|pg\.?|sheet)\s*\d+\s*(?:(?:von|of|/)\s*\d+)?\s*$"
    r"|^\s*[-–]\s*\d{1,3}\s*[-–]\s*$",
    re.I,
)

#: "Uebertrag", "Uebertrag von Seite 1", "carried forward"
_CARRYOVER_RE = re.compile(
    r"^\s*(?:uebertrag|übertrag|übertrag:|vortrag|carry[\s-]?over|"
    r"carried\s+forward|balance\s+carried)\b",
    re.I,
)


@dataclass(slots=True)
class PriceInfo:
    """Ergebnis der Preisanalyse einer einzelnen Zelle bzw. Textstelle.

    ``price`` bleibt bei einer Preisspanne bewusst ``None`` -- dort *gibt* es
    keinen gueltigen Wert, und ein gemittelter Preis waere eine Erfindung.
    """

    price: Decimal | None = None
    price_unit: int | None = None
    currency: str = ""
    uom: str = ""
    is_range: bool = False
    range_low: Decimal | None = None
    range_high: Decimal | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.price is not None


def _to_decimal(text: str, style: str = "auto") -> Decimal | None:
    """Zahl im gelernten Dezimalstil lesen (Umweg ueber den Tabellenparser)."""
    from .table_extractor import parse_number      # spaeter Import: Zyklus meiden

    return parse_number(text, style)


def parse_price_text(raw: object, style: str = "auto") -> PriceInfo:
    """Preisangabe aus einer Zelle oder einem Textstueck lesen.

    Rueckgabe ist immer ein :class:`PriceInfo`; ob etwas Brauchbares darin
    steht, sagt ``usable``.  Es wird nie ein Wert erfunden: bei einer
    Preisspanne bleibt ``price`` leer und ``notes`` erklaert, warum.
    """
    text = normalize_whitespace(raw)
    info = PriceInfo()
    if not text:
        return info

    waehrung = detect_currency(text)

    # 1. Preisspanne -- muss vor allem anderen geprueft werden, sonst wuerde
    #    der erste Wert als "der" Preis durchgehen.
    spanne = _RANGE_RE.search(text)
    if spanne and _is_real_range(text, spanne):
        low = _to_decimal(spanne.group("low"), style)
        high = _to_decimal(spanne.group("high"), style)
        if low is not None and high is not None and high > low:
            info.is_range = True
            info.range_low, info.range_high = low, high
            info.currency = waehrung
            info.notes.append(
                "Preisspanne im Angebot -- bitte den gueltigen Preis eintragen "
                f"(genannt sind {_fmt(low)} bis {_fmt(high)}"
                f"{' ' + waehrung if waehrung else ''}).")
            return info

    # 2. Einheit vor dem Preis: "je 100 Stueck 12,85"
    treffer = _UNIT_FIRST_RE.search(text)
    if treffer:
        _fill(info, treffer, style, waehrung)
        if info.usable:
            return info

    # 3. Preis mit Bezugsmenge: "12,85 EUR/100 St", "4.50 per 1000 pcs"
    treffer = _PER_UNIT_RE.search(text)
    if treffer:
        _fill(info, treffer, style, waehrung)
        if info.usable:
            return info

    # 4. Schlichte Zahl
    treffer = _PLAIN_RE.match(text)
    if treffer:
        info.price = _to_decimal(treffer.group("value"), style)
        info.currency = waehrung
    return info


def _fill(info: PriceInfo, treffer: re.Match[str], style: str,
          waehrung: str) -> None:
    """Treffer eines Preismusters in das Ergebnis uebernehmen."""
    info.price = _to_decimal(treffer.group("value"), style)
    if info.price is None:
        return
    info.currency = (treffer.groupdict().get("pre") or ""
                     or treffer.groupdict().get("post") or "")
    info.currency = detect_currency(info.currency) or waehrung
    einheit = treffer.groupdict().get("unit")
    if einheit:
        try:
            wert = int(einheit)
        except ValueError:
            wert = 0
        if 0 < wert <= MAX_PRICE_UNIT:
            info.price_unit = wert
    elif treffer.groupdict().get("uom"):
        info.price_unit = 1          # "12,85 EUR/St" -> Preiseinheit 1
    mengeneinheit = treffer.groupdict().get("uom")
    if mengeneinheit:
        info.uom = normalize_uom(mengeneinheit)


def _is_real_range(text: str, spanne: re.Match[str]) -> bool:
    """Bindestrich-Faelle abgrenzen, die keine Preisspanne sind.

    "40-52-7" ist eine Abmessung, "12,00 - 14,00 EUR" eine Spanne.  Als Spanne
    gilt nur, was zwei *unterschiedliche* Betraege mit Nachkommastellen oder
    ein ausgeschriebenes "bis"/"to" nennt.
    """
    verbinder = text[spanne.end("low"):spanne.start("high")].lower()
    if re.search(r"\b(bis|to|und)\b", verbinder):
        return True
    low, high = spanne.group("low"), spanne.group("high")
    return bool(re.search(r"[.,]\d", low) and re.search(r"[.,]\d", high))


def _fmt(value: Decimal) -> str:
    from ...utils.parsing import format_decimal

    return format_decimal(value)


#: Preiseinheit in einer Spaltenueberschrift: "Preis EUR/100 St", "Preis je 1000"
_HEADER_UNIT_RE = re.compile(
    rf"(?:/|je|pro|per)\s*(?P<unit>\d{{1,5}})(?!\d)\s*(?:{_UNIT_WORDS})?\b",
    re.I,
)


def price_unit_from_header(text: object) -> int | None:
    """Preiseinheit aus einer Spaltenueberschrift lesen.

    Steht die Bezugsmenge nur im Kopf ("Preis EUR/100 St") und nicht in jeder
    Zelle, ginge sie sonst verloren -- und der Preis waere um Faktor 100
    falsch.  Ohne ausdrueckliche Zahl wird nichts angenommen.
    """
    treffer = _HEADER_UNIT_RE.search(normalize_whitespace(text))
    if not treffer:
        return None
    try:
        wert = int(treffer.group("unit"))
    except ValueError:
        return None
    return wert if 1 < wert <= MAX_PRICE_UNIT else None


#: Preisangabe im Fusstext unterhalb der Tabelle: "Preise je 100 Stueck",
#: "Alle Preise pro 1000 Stk", "prices per 100 pcs".  Sie gilt dann fuer alle
#: Positionen ohne eigene Preiseinheit.
_FOOTER_PRICE_UNIT_RE = re.compile(
    rf"(?:alle\s+)?preise?\s+(?:verstehen\s+sich\s+)?(?:gelten\s+)?"
    rf"(?:je|pro|per)\s+(?P<unit_de>\d{{1,5}})(?!\d)\s*(?:{_UNIT_WORDS})\b"
    rf"|(?:all\s+)?prices?\s+(?:are\s+)?(?:quoted\s+)?(?:per|for)\s+"
    rf"(?P<unit_en>\d{{1,5}})(?!\d)\s*(?:{_UNIT_WORDS})\b",
    re.I,
)


def footer_price_unit(text: object) -> int | None:
    """Preiseinheit aus einer Fusstext-Aussage lesen ("Preise je 100 Stueck").

    Steht die Bezugsmenge nur unterhalb der Tabelle, waere sonst jeder Preis
    um genau diesen Faktor falsch.  Ohne ausdrueckliche Zahl > 1 wird nichts
    angenommen.
    """
    if not text:
        return None
    treffer = _FOOTER_PRICE_UNIT_RE.search(str(text))
    if not treffer:
        return None
    roh = treffer.group("unit_de") or treffer.group("unit_en")
    try:
        wert = int(roh)
    except (TypeError, ValueError):
        return None
    return wert if 1 < wert <= MAX_PRICE_UNIT else None


#: "auf Anfrage"/"a.A."/"on request": es gibt keinen Preis -- und es darf
#: niemals einer erfunden werden.  Die Position bleibt trotzdem erhalten.
_ON_REQUEST_RE = re.compile(
    r"^\s*(?:preis\s+)?(?:auf\s+anfrage|a\.\s*a\.|on\s+request|upon\s+request|"
    r"price\s+on\s+application|p\.\s*o\.\s*a\.?|nach\s+vereinbarung|"
    r"nach\s+absprache)\s*!?\s*$",
    re.I,
)


def is_on_request(raw: object) -> bool:
    """Ist diese Preisangabe ein "auf Anfrage"-Vermerk (kein Zahlenwert)?"""
    return bool(_ON_REQUEST_RE.match(normalize_whitespace(raw)))


# --------------------------------------------------------------------------
# Zeilenfilter
# --------------------------------------------------------------------------

def is_page_noise_row(cells) -> bool:
    """Fuss-/Kopfzeilen mit Seitenzahl erkennen ("Seite 2 von 3", "Blatt 2").

    Solche Zeilen sehen in einem PDF wie eine Tabellenzeile aus, tragen aber
    keine Ware.  Sie duerfen nie zu einer Position werden.
    """
    texte = [normalize_whitespace(c) for c in cells]
    gefuellt = [t for t in texte if t]
    if not gefuellt:
        return False
    if len(gefuellt) > 2:
        return False        # eine echte Positionszeile hat mehr Inhalt
    return any(_PAGE_RE.match(t) for t in gefuellt)


def is_carryover_row(cells) -> bool:
    """"Uebertrag"-Zeilen erkennen (Zwischensumme beim Seitenwechsel)."""
    texte = [normalize_whitespace(c) for c in cells]
    return any(_CARRYOVER_RE.match(t) for t in texte if t)
