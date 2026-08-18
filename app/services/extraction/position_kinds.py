"""Positionsarten: was ist ueberhaupt ein Materialpreis?

Ein Lieferantenangebot enthaelt regelmaessig Zeilen, die *aussehen* wie eine
Position, aber keinen Materialpreis tragen.  Aus 22 echten Angeboten stammen
diese drei Gruppen:

``one_time_cost``
    Einmalkosten -- Werkzeugkosten, Musterkosten, Prototypenpreise,
    Einricht-/Ruestkosten, Entwicklungs-, Zeichnungs-, Verpackungs- und
    Frachtkosten als eigene Zeile.  Wuerde so eine Zeile als normale Position
    durchgehen, landete z. B. ein Werkzeugpreis von 8.500 EUR als Materialpreis
    im Einkaufsinfosatz -- und faellt erst auf, wenn jemand danach bestellt.

``alternative``
    Alternativ-/Optionalpositionen.  Typisch ist dasselbe Material zweimal mit
    unterschiedlicher Menge und unterschiedlichem Preis (Jahresmenge gegen
    Einzelabruf).  Wuerden beide geschrieben, entstuenden zwei widersprechende
    Infosaetze oder eine doppelte Kontraktzeile.

``subtotal``
    Zwischensummen-, Uebertrags- und Summenzeilen.

Alles andere bleibt ``material``.

**Grundsaetze dieses Moduls**

* Es wird **nichts verworfen.**  Eine erkannte Zeile bleibt erhalten, sie wird
  lediglich nicht vorausgewaehlt (``selected = False``) und traegt einen
  Klartext-Befund.  Der Anwender kann sie bewusst anhaken.
* Es wird **nie entschieden**, welche von mehreren Alternativen die richtige
  ist.  Vorausgewaehlt bleibt die *erste* -- nicht die guenstigste, nicht die
  teuerste.  Bewertet wird nichts.
* Lieber eine Zeile zu wenig markieren als eine echte Materialposition
  faelschlich abwaehlen.  Deshalb sind die Wortlisten eng gefasst:
  "Werkzeugstahl", "Musterring" und "Summenscheibe" sind normale Materialien
  und duerfen **nicht** anschlagen.

Ausserdem liegt hier die Mindestbestellmenge aus dem Fliesstext (Aufgabe 4):
steht "Mindestbestellmenge 500 Stueck" nur im Anschreiben und nicht in der
Tabelle, wird sie auf die Positionen uebernommen -- bei widerspruechlichen
Angaben aber ausdruecklich *nicht*, sondern nur gemeldet.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from ...models.enums import FieldOrigin, IssueSeverity
from ...models.issue import Issue
from ...models.offer import Offer
from ...models.offer_position import OfferPosition
from ...utils.parsing import (
    format_decimal,
    normalize_material_number,
    normalize_whitespace,
    parse_decimal,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_ALTERNATIVE",
    "KIND_MATERIAL",
    "KIND_ONE_TIME_COST",
    "KIND_SUBTOTAL",
    "KIND_LABELS",
    "CODE_ONE_TIME_COST",
    "CODE_ALTERNATIVE",
    "CODE_SUBTOTAL",
    "CODE_MIN_ORDER_TEXT",
    "CODE_MIN_ORDER_CONFLICT",
    "classify_position",
    "detect_one_time_cost",
    "detect_subtotal",
    "detect_alternative_keyword",
    "counts_towards_document_total",
    "counts_as_material_price",
    "find_min_order_quantities",
    "apply_document_min_order_qty",
    "apply_position_kinds",
]

# --------------------------------------------------------------------------
# Positionsarten
# --------------------------------------------------------------------------

KIND_MATERIAL = "material"
KIND_ONE_TIME_COST = "one_time_cost"
KIND_SUBTOTAL = "subtotal"
KIND_ALTERNATIVE = "alternative"

#: Klartextbezeichnung fuer Oberflaeche und Protokoll.
KIND_LABELS = {
    KIND_MATERIAL: "Materialposition",
    KIND_ONE_TIME_COST: "Einmalkosten",
    KIND_SUBTOTAL: "Zwischensumme",
    KIND_ALTERNATIVE: "Alternativposition",
}

# --------------------------------------------------------------------------
# Befundschluessel
# --------------------------------------------------------------------------

CODE_ONE_TIME_COST = "position_one_time_cost"
CODE_ALTERNATIVE = "position_alternative"
CODE_SUBTOTAL = "position_subtotal"
CODE_MIN_ORDER_TEXT = "min_order_from_text"
CODE_MIN_ORDER_CONFLICT = "min_order_conflict"


# --------------------------------------------------------------------------
# Textnormalisierung
# --------------------------------------------------------------------------

_UMLAUTS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
})


def _normalize(text: object) -> str:
    """Kleinschreibung, Umlaute aufgeloest, Trennzeichen zu Leerzeichen.

    Damit trifft dieselbe Wortliste "Werkzeugkosten", "WERKZEUG-KOSTEN" und
    "Werkzeugkosten/Formkosten" gleichermassen.
    """
    if text is None:
        return ""
    plain = normalize_whitespace(str(text)).translate(_UMLAUTS).lower()
    # Binde- und Schraegstriche trennen im Deutschen Woerter -- nicht aber der
    # Punkt in "ust.", der bleibt Teil des Wortes.
    plain = re.sub(r"[\-/\\|,;:()\[\]{}*+_]+", " ", plain)
    return normalize_whitespace(plain)


def _text_of(position: OfferPosition) -> str:
    """Der Text, auf den sich die Erkennung stuetzt: Bezeichnung + Bemerkung."""
    return _normalize(f"{position.description} {position.remarks}")


# --------------------------------------------------------------------------
# a) Einmalkosten
# --------------------------------------------------------------------------

#: Nachsilben, die aus einem Sachwort eine Kostenzeile machen.  Sie sind der
#: Grund, warum "Werkzeugkostenanteil" trifft und "Werkzeugstahl" nicht.
_COST_TAIL = r"(?:kosten\w*|kostenanteil\w*|preis\w*|anteil\w*|pauschale\w*|" \
             r"zuschlag\w*|umlage\w*|beitrag\w*|aufwand\w*|charge\w*|" \
             r"cost\w*|costs\w*|fee\w*|fees\w*|charges\w*)"

#: Begriffe, die fuer sich allein schon eine Einmalkostenzeile bezeichnen
#: ("Werkzeug", "Tooling", "Muster").  Ein angehaengtes Wort macht daraus ein
#: normales Material ("Werkzeugstahl") -- deshalb steht ueberall ein ``\b``.
_ONE_TIME_STANDALONE = (
    r"werkzeug",
    r"werkzeuge",
    r"formwerkzeug",
    r"formwerkzeuge",
    r"tooling",
    r"tool",
    r"muster",
    r"bemusterung",
    r"erstmuster\w*",
    r"empb",
    r"ppap",
    r"prototyp",
    r"prototypen",
    r"vorserie",
    r"vorserien",
    r"einmalkosten",
    r"einmalige kosten",
    r"einmalaufwand",
    r"nre",
    r"one time \w*",
    r"one off \w*",
)

#: Begriffe, die nur zusammen mit einem Kostenwort zaehlen.  "Setup" allein
#: koennte in einer Artikelbezeichnung stehen, "Setup cost" nie.
_ONE_TIME_WITH_TAIL = (
    r"werkzeug",
    r"form",
    r"formen",
    r"muster",
    r"bemusterung",
    r"prototyp",
    r"prototypen",
    r"vorserie",
    r"vorserien",
    r"einricht",
    r"einrichtungs",
    r"einrichte",
    r"ruest",
    r"ruestungs",
    r"anlauf",
    r"entwicklungs",
    r"entwicklung",
    r"zeichnungs",
    r"zeichnung",
    r"konstruktions",
    r"konstruktion",
    r"verpackungs",
    r"verpackung",
    r"fracht",
    r"versand",
    r"transport",
    r"setup",
    r"sample",
    r"samples",
    r"tooling",
    r"development",
    r"packaging",
    r"freight",
    r"shipping",
)

#: Feste Begriffe, die schon durch sich selbst Kostenzeilen sind.
_ONE_TIME_EXACT = (
    r"einrichtkosten",
    r"ruestkosten",
    r"anlaufkosten",
    r"musterkosten",
    r"formkosten",
    r"werkzeugkosten",
    r"werkzeugkostenanteil",
    r"entwicklungskosten",
    r"zeichnungskosten",
    r"verpackungskosten",
    r"frachtkosten",
    r"setup\w*",
)

_ONE_TIME_RE = re.compile(
    r"\b(?:"
    + "|".join(_ONE_TIME_EXACT)
    + r"|(?:" + "|".join(_ONE_TIME_STANDALONE) + r")"
    + r"|(?:" + "|".join(_ONE_TIME_WITH_TAIL) + r")\s*" + _COST_TAIL
    + r")\b",
    re.I,
)


def detect_one_time_cost(text: object) -> str:
    """Trifft die Einmalkosten-Erkennung?  Liefert das Stichwort oder ``""``.

    Das gefundene Stichwort wandert in den Befundtext -- der Anwender soll
    sehen, *woran* die Erkennung haengt, und nicht raten muessen.
    """
    match = _ONE_TIME_RE.search(_normalize(text))
    return match.group(0).strip() if match else ""


# --------------------------------------------------------------------------
# b) Zwischensummen
# --------------------------------------------------------------------------

#: Summenbeschriftungen.  Sie muessen am *Anfang* der Bezeichnung stehen --
#: "Summenscheibe" ist ein Bauteil, "Summe Baugruppe A" eine Summenzeile.
#: Die Tabellenerkennung filtert solche Zeilen bereits vorne weg
#: (``_SUMMARY_RE`` in ``table_extractor``); hier steht der Auffangnetz-Fall
#: fuer Positionen aus Freitext, Anhang oder manueller Erfassung.
_SUBTOTAL_WORDS = (
    r"zwischensumme",
    r"zwischen summe",
    r"gesamtsumme",
    r"nettosumme",
    r"bruttosumme",
    r"endsumme",
    r"summe",
    r"uebertrag",
    r"vortrag",
    r"gesamtbetrag",
    r"nettobetrag",
    r"endbetrag",
    r"rechnungsbetrag",
    r"subtotal",
    r"sub total",
    r"grand total",
    r"net total",
    r"total net",
    r"total amount",
    r"total",
    r"carry over",
    r"carried forward",
)

_SUBTOTAL_RE = re.compile(
    r"^(?:pos\.?\s*)?(?:\d{1,6}\.?\s+)?(?:" + "|".join(_SUBTOTAL_WORDS) + r")\b",
    re.I,
)


def detect_subtotal(text: object) -> str:
    """Ist der Text eine Summen-/Uebertragszeile?  Stichwort oder ``""``."""
    match = _SUBTOTAL_RE.match(_normalize(text))
    return match.group(0).strip() if match else ""


# --------------------------------------------------------------------------
# c) Alternativpositionen ueber die Bezeichnung
# --------------------------------------------------------------------------

#: Eindeutige Kennzeichnungen -- sie duerfen ueberall im Text stehen.
_ALTERNATIVE_STRONG = (
    r"alternativposition\w*",
    r"alternativ position\w*",
    r"alternativpreis\w*",
    r"alternativ preis\w*",
    r"alternativmenge\w*",
    r"alternativ menge\w*",
    r"alternativangebot\w*",
    r"alternativ angebot\w*",
    r"alternativausfuehrung\w*",
    r"alternativvorschlag\w*",
    r"optionale position\w*",
    r"optionale positionen",
    r"alternative position\w*",
    r"alternative offer",
    r"alternative price",
    r"optional item\w*",
    r"optional position\w*",
)

#: Schwache Kennzeichnungen.  "Option", "Variante" und vor allem "oder"
#: stehen viel zu oft mitten in einer normalen Artikelbezeichnung
#: ("Schraube M6 oder M8", "Ring Variante B").  Sie zaehlen deshalb nur, wenn
#: sie die Zeile *eroeffnen* oder eingeklammert fuer sich stehen.
_ALTERNATIVE_WEAK = (
    r"alternative",
    r"alternativ",
    r"alternatively",
    r"option",
    r"optional",
    r"variante",
    r"wahlweise",
    r"oder",
    r"or",
)

_ALT_STRONG_RE = re.compile(r"\b(?:" + "|".join(_ALTERNATIVE_STRONG) + r")\b", re.I)
_ALT_WEAK_RE = re.compile(r"^(?:" + "|".join(_ALTERNATIVE_WEAK) + r")\b", re.I)


def detect_alternative_keyword(text: object) -> str:
    """Weist der Text eine Position ausdruecklich als Alternative aus?"""
    plain = _normalize(text)
    match = _ALT_STRONG_RE.search(plain)
    if match:
        return match.group(0).strip()
    match = _ALT_WEAK_RE.match(plain)
    return match.group(0).strip() if match else ""


# --------------------------------------------------------------------------
# Einordnung einer einzelnen Position
# --------------------------------------------------------------------------

def _is_scale_row(position: OfferPosition) -> bool:
    """Staffelzeile?  Die Erkennung liegt in ``plausibility`` -- nicht doppeln.

    Der Import steht bewusst in der Funktion: ``plausibility`` benutzt dieses
    Modul, ein Import auf Modulebene waere ein Ringschluss.
    """
    from .plausibility import is_scale_row

    return is_scale_row(position) or position.has_scales


def classify_position(position: OfferPosition) -> tuple[str, str]:
    """Positionsart einer einzelnen Zeile: ``(Art, Stichwort)``.

    Geprueft wird in der Reihenfolge Summe -> Einmalkosten -> Alternative;
    eine Zeile "Zwischensumme Werkzeugkosten" ist zuerst eine Summenzeile.
    Staffelzeilen bleiben unangetastet: sie sind bereits erkannt und tragen
    ihren eigenen Vermerk.
    """
    if _is_scale_row(position):
        return KIND_MATERIAL, ""

    text = _text_of(position)
    if not text:
        return KIND_MATERIAL, ""

    stichwort = detect_subtotal(text)
    if stichwort:
        return KIND_SUBTOTAL, stichwort

    stichwort = detect_one_time_cost(text)
    if stichwort:
        return KIND_ONE_TIME_COST, stichwort

    stichwort = detect_alternative_keyword(text)
    if stichwort:
        return KIND_ALTERNATIVE, stichwort

    return KIND_MATERIAL, ""


# --------------------------------------------------------------------------
# Auskunft fuer die Kreuzpruefungen
# --------------------------------------------------------------------------

def counts_towards_document_total(position: OfferPosition) -> bool:
    """Zaehlt diese Position in die Summe *aller* Positionen?

    * Einmalkosten: **ja** -- der Lieferant rechnet sie in die Belegsumme ein.
    * Zwischensummen: **nein** -- sonst waere jeder Betrag doppelt.
    * Alternativpositionen: **nein** -- sie sind ein Entweder-oder; wuerde man
      beide zaehlen, entstuende eine Falschmeldung "Position zu viel erkannt".
    """
    return getattr(position, "position_kind", KIND_MATERIAL) not in (
        KIND_SUBTOTAL, KIND_ALTERNATIVE)


def counts_as_material_price(position: OfferPosition) -> bool:
    """Ist der Preis dieser Position ein Materialpreis?

    Nur solche Preise duerfen in den Preisvergleich (Ausreisserpruefung) und
    spaeter in den Einkaufsinfosatz.  Ein Werkzeugpreis von 8.500 EUR neben
    Drehteilen zu 12 EUR ist kein Dezimaltrennerfehler, sondern schlicht ein
    Werkzeug.
    """
    return getattr(position, "position_kind", KIND_MATERIAL) in (
        KIND_MATERIAL, KIND_ALTERNATIVE)


# --------------------------------------------------------------------------
# Hilfsmittel
# --------------------------------------------------------------------------

def _label(position: OfferPosition) -> str:
    """"Position 40" bzw. ersatzweise die Bezeichnung."""
    if position.position_number:
        return f"Position {position.position_number}"
    return f"Position '{position.display_name}'"


def _deselect(position: OfferPosition, kind: str) -> None:
    """Position abwaehlen -- aber niemals loeschen."""
    position.position_kind = kind
    position.selected = False
    position.do_info_record = False
    position.do_source_list = False
    position.do_contract = False
    position.do_purchase_order = False


def _add_issue(position: OfferPosition, code: str, message: str,
               detail: str = "") -> None:
    position.issues.add(Issue(code, message, IssueSeverity.WARNING,
                              detail=detail, blocking=False))


def _unit_price(position: OfferPosition) -> Decimal | None:
    """Preis je *einer* Mengeneinheit -- Grundlage des Preisvergleichs."""
    if position.price is None:
        return None
    try:
        unit = Decimal(position.price_unit) if position.price_unit else Decimal(1)
    except (InvalidOperation, TypeError, ValueError):
        unit = Decimal(1)
    if unit <= 0:
        unit = Decimal(1)
    try:
        return Decimal(position.price) / unit
    except (InvalidOperation, TypeError, ValueError):
        return None


def _material_key(position: OfferPosition) -> str:
    """Schluessel fuer die Dublettensuche: SAP-Material vor Lieferantennummer.

    Die blosse Bezeichnung genuegt ausdruecklich **nicht** -- zwei Zeilen
    "Dichtring" muessen nicht dasselbe Teil sein.
    """
    if position.material_number:
        return "M:" + normalize_material_number(position.material_number)
    if position.vendor_material_number:
        return "L:" + normalize_material_number(position.vendor_material_number)
    return ""


# --------------------------------------------------------------------------
# 1) Einmalkosten und Zwischensummen markieren
# --------------------------------------------------------------------------

def _mark_by_keyword(offer: Offer) -> list[str]:
    """Jede Position anhand ihrer Bezeichnung einordnen."""
    notes: list[str] = []
    for position in offer.positions:
        if getattr(position, "position_kind", KIND_MATERIAL) != KIND_MATERIAL:
            continue        # bereits eingeordnet (z. B. durch die GUI)
        kind, stichwort = classify_position(position)
        if kind == KIND_MATERIAL:
            continue

        label = _label(position)
        if kind == KIND_ONE_TIME_COST:
            note = (f"{label} sieht nach Einmalkosten aus ({stichwort}) -- sie "
                    "wurde nicht vorausgewaehlt, weil ein Werkzeugpreis kein "
                    "Materialpreis ist. Wenn die Zeile doch ein Material ist, "
                    "bitte anhaken.")
            code = CODE_ONE_TIME_COST
        elif kind == KIND_SUBTOTAL:
            note = (f"{label} ist eine Summenzeile ({stichwort}) und keine "
                    "Position -- sie wurde nicht vorausgewaehlt.")
            code = CODE_SUBTOTAL
        else:
            note = (f"{label} ist als Alternative gekennzeichnet ({stichwort}) "
                    "-- sie wurde nicht vorausgewaehlt. Bitte pruefen, welche "
                    "Position gelten soll.")
            code = CODE_ALTERNATIVE

        _deselect(position, kind)
        _add_issue(position, code, note,
                   detail=f"erkannt in: {position.description or position.remarks}")
        position.confidence_reasons.append(f"Erkannt als {KIND_LABELS[kind]}")
        notes.append(note)
    return notes


# --------------------------------------------------------------------------
# 2) Dasselbe Material mehrfach mit unterschiedlichem Preis
# --------------------------------------------------------------------------

def _mark_duplicate_materials(offer: Offer) -> list[str]:
    """Mehrfachnennungen mit abweichendem Preis als Alternative markieren.

    Eine echte Mengenstaffel ist ausdruecklich **keine** Alternative: sie ist
    bereits erkannt, traegt den Vermerk "Staffelpreis" und wird hier
    uebersprungen.  Gleicher Preis zweimal ist ebenfalls keine Alternative --
    das ist eine Dublette und wird in ``plausibility`` gemeldet.
    """
    gruppen: dict[str, list[OfferPosition]] = {}
    for position in offer.positions:
        if getattr(position, "position_kind", KIND_MATERIAL) != KIND_MATERIAL:
            continue
        if _is_scale_row(position):
            continue
        if position.price is None:
            continue
        schluessel = _material_key(position)
        if not schluessel:
            continue
        gruppen.setdefault(schluessel, []).append(position)

    notes: list[str] = []
    for positionen in gruppen.values():
        if len(positionen) < 2:
            continue
        preise = {_unit_price(p) for p in positionen}
        if len(preise) < 2:
            continue        # gleicher Preis -> Dublette, nicht Alternative

        erste, weitere = positionen[0], positionen[1:]
        nummern = " und ".join(_label(p).replace("Position ", "")
                               for p in positionen)
        note = (f"Material {erste.display_name} kommt mehrfach mit "
                f"unterschiedlichem Preis vor (Position {nummern}) -- es wurde "
                "nur die erste vorausgewaehlt. Bitte pruefen, welche gelten "
                "soll.")
        detail = "; ".join(
            f"{_label(p)}: {format_decimal(p.price)} {p.currency}".strip()
            + (f" je {p.price_unit}" if p.price_unit and p.price_unit != 1 else "")
            for p in positionen)
        for position in weitere:
            _deselect(position, KIND_ALTERNATIVE)
            _add_issue(position, CODE_ALTERNATIVE, note, detail=detail)
            position.confidence_reasons.append(
                "Material kommt mehrfach mit anderem Preis vor")
        _add_issue(erste, CODE_ALTERNATIVE, note, detail=detail)
        erste.confidence_reasons.append(
            "Material kommt mehrfach mit anderem Preis vor")
        notes.append(note)

    if notes:
        # Nur *ein* Befund am Angebot: die Befundliste ersetzt gleiche
        # Schluessel, mehrere Einzelmeldungen wuerden einander verdraengen.
        offer.issues.add(Issue(
            CODE_ALTERNATIVE,
            f"{len(notes)} Material(ien) kommen mehrfach mit unterschiedlichem "
            "Preis vor -- jeweils nur die erste Position ist vorausgewaehlt. "
            "Bitte pruefen, welche gelten soll.",
            IssueSeverity.WARNING, blocking=False,
            detail="\n".join(notes)))
    return notes


# --------------------------------------------------------------------------
# 3) Mindestbestellmenge aus dem Fliesstext
# --------------------------------------------------------------------------

#: "Mindestbestellmenge 500 Stueck", "Mindestabnahme: 250 ST",
#: "Mindestmenge von 100", "MOQ 1000 pcs", "ab 500 Stueck lieferbar".
#: Bewusst eng gefasst: ein blosses "ab 500 Stueck" ohne Lieferbezug waere
#: genauso gut eine Staffelstufe -- und die wird woanders behandelt.
_MIN_ORDER_PATTERNS = (
    re.compile(
        r"\bmindest(?:bestell|abnahme|auftrags|liefer)?menge\b\s*"
        r"(?:von|betr(?:ae|ä)gt|liegt\s+bei|ist|:|=)?\s*"
        r"(\d{1,3}(?:[.\s]\d{3})+|\d{1,7})(?:[.,]\d{1,3})?",
        re.I),
    re.compile(
        r"\bmindestabnahme\b\s*(?:von|:|=)?\s*"
        r"(\d{1,3}(?:[.\s]\d{3})+|\d{1,7})(?:[.,]\d{1,3})?",
        re.I),
    re.compile(
        r"\b(?:moq|mbm|min(?:\.|imum)?\s*order\s*(?:qty|quantity|size)?)\b"
        r"\s*(?:of|is|:|=)?\s*"
        r"(\d{1,3}(?:[.,]\d{3})+|\d{1,7})(?:\.\d{1,3})?",
        re.I),
    re.compile(
        r"\bab\s+(\d{1,3}(?:[.\s]\d{3})+|\d{1,7})\s*"
        r"(?:stk\.?|st\.?|st(?:ue|ü)ck|pcs\.?|pieces|kg|m|l)?\s*"
        r"(?:lieferbar|bestellbar|abnahme|lieferung|erhaeltlich|erhältlich)",
        re.I),
)


def find_min_order_quantities(text: str) -> list[tuple[Decimal, str]]:
    """Alle Mindestmengenangaben im Fliesstext: ``[(Menge, Fundstelle), ...]``.

    Es wird bewusst *alles* gesammelt und nichts ausgewaehlt -- ueber
    widerspruechliche Angaben entscheidet der Anwender, nicht dieses Modul.
    """
    if not text:
        return []
    treffer: list[tuple[Decimal, str]] = []
    gesehen: set[tuple[Decimal, int]] = set()
    for muster in _MIN_ORDER_PATTERNS:
        for match in muster.finditer(text):
            menge = parse_decimal(match.group(1))
            if menge is None or menge <= 0:
                continue
            marke = (menge, match.start())
            if marke in gesehen:
                continue
            gesehen.add(marke)
            treffer.append((menge, normalize_whitespace(match.group(0))))
    return treffer


def apply_document_min_order_qty(offer: Offer, text: str | None = None) -> list[str]:
    """Mindestbestellmenge aus dem Anschreiben auf die Positionen uebernehmen.

    Sie gilt nur fuer Positionen **ohne** eigene Angabe -- eine Zahl aus der
    Tabelle ist immer belastbarer als eine aus dem Fliesstext.  Nennt der
    Beleg mehrere verschiedene Mindestmengen, wird keine davon uebernommen,
    sondern gemeldet: welche fuer welches Material gilt, steht nirgends.
    """
    quelle = text if text is not None else (offer.raw_text or "")
    treffer = find_min_order_quantities(quelle)
    if not treffer:
        return []

    werte = sorted({menge for menge, _ in treffer})
    if len(werte) > 1:
        note = ("Im Text stehen mehrere verschiedene Mindestbestellmengen ("
                + ", ".join(format_decimal(w, 0) for w in werte)
                + ") -- es wurde keine uebernommen, weil nicht erkennbar ist, "
                  "welche fuer welche Position gilt. Bitte selbst eintragen.")
        offer.issues.add(Issue(CODE_MIN_ORDER_CONFLICT, note,
                               IssueSeverity.WARNING, blocking=False,
                               detail="Fundstellen: "
                                      + "; ".join(f for _, f in treffer)))
        offer.add_note(note)
        return [note]

    menge, fundstelle = treffer[0][0], treffer[0][1]
    betroffen = [p for p in offer.positions
                 if p.min_order_qty is None
                 and getattr(p, "position_kind", KIND_MATERIAL) == KIND_MATERIAL]
    if not betroffen:
        return []
    for position in betroffen:
        position.set_field("min_order_qty", menge, FieldOrigin.EXTRACTED)
        position.remarks = _join_remark(
            position.remarks,
            f"Mindestbestellmenge {format_decimal(menge, 0)} aus dem Text")
    note = (f"Mindestbestellmenge {format_decimal(menge, 0)} stand nur im "
            f"Fliesstext ('{fundstelle}') und wurde auf {len(betroffen)} "
            "Position(en) ohne eigene Angabe uebernommen -- bitte bestaetigen.")
    for position in betroffen:
        _add_issue(position, CODE_MIN_ORDER_TEXT, note, detail=fundstelle)
    offer.add_note(note)
    return [note]


def _join_remark(existing: str, addition: str) -> str:
    """Bemerkung ergaenzen, ohne Bestehendes zu ueberschreiben."""
    existing = (existing or "").strip()
    if not existing:
        return addition
    if addition.lower() in existing.lower():
        return existing
    return f"{existing} | {addition}"


# --------------------------------------------------------------------------
# Gesamtlauf
# --------------------------------------------------------------------------

def apply_position_kinds(offer: Offer, text: str | None = None) -> list[str]:
    """Alle Einordnungen ausfuehren und die Notizen ins Angebot schreiben.

    Reihenfolge mit Bedacht: erst die Stichworterkennung (sie nimmt
    Einmalkosten und Summenzeilen aus dem Rennen), dann die Dublettensuche
    (die nur noch echte Materialpositionen sieht), zuletzt die Mindestmenge
    aus dem Fliesstext (die nur auf Materialpositionen gehoert).

    Eine fehlgeschlagene Einordnung darf den Import nie kippen -- im
    Zweifelsfall bleibt alles so, wie die Extraktion es geliefert hat.
    """
    notes: list[str] = []
    try:
        notes.extend(_mark_by_keyword(offer))
        notes.extend(_mark_duplicate_materials(offer))
        for note in notes:
            offer.add_note(note)
        notes.extend(apply_document_min_order_qty(offer, text))
    except Exception as exc:  # noqa: BLE001 -- Einordnung ist Beiwerk
        logger.error("Einordnung der Positionsarten fehlgeschlagen: %s",
                     exc, exc_info=True)
        return notes
    if notes:
        logger.info("Positionsarten: %d Hinweis(e)", len(notes))
    return notes
