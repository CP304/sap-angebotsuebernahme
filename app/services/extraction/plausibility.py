"""Kreuzpruefungen ueber die Redundanz im Beleg.

Ein Angebot enthaelt fast immer dieselbe Information mehrfach: die Zeilensumme
wiederholt ``Menge x Preis``, die Belegsumme wiederholt die Summe aller
Zeilensummen, und die Positionsnummern bilden eine lueckenlose Folge.  Genau
diese Redundanz laesst sich nutzen, um Lesefehler zu *finden* -- ohne dabei
irgendetwas zu raten.

Klassischer Fall aus dem Einkaufsalltag::

    Pos 20   500 St   12,85 EUR   Gesamt 642,50

Rechnerisch waeren das 6.425,00 EUR.  Entweder ist die Menge falsch gelesen
(50 statt 500), oder der Preis (1,285 statt 12,85), oder die Zeilensumme hat
einen verlorenen Tausenderpunkt.  Welcher der drei Werte falsch ist, kann
niemand sicher sagen -- also wird **nichts** korrigiert, sondern alles
Betroffene als :class:`FieldOrigin.UNCERTAIN` markiert und im Klartext
begruendet.

Einzige Ausnahme: Ist der Preis gar nicht erkannt worden, waehrend Menge und
Zeilensumme vorliegen, wird er *ausgerechnet* (nicht geraten) und mit
``UNCERTAIN`` gesetzt -- inklusive Begruendung in den Notizen.

Alle Befunde sind Warnungen und blockieren nie: der Anwender entscheidet.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from statistics import median

from ...models.enums import FieldOrigin, IssueSeverity
from ...models.issue import Issue
from ...models.offer import Offer
from ...models.offer_position import OfferPosition
from ...utils.parsing import format_decimal, normalize_whitespace, parse_decimal

logger = logging.getLogger(__name__)

__all__ = [
    "CODE_LINE_TOTAL_MISMATCH",
    "CODE_DOCUMENT_TOTAL_MISMATCH",
    "CODE_POSITION_GAP",
    "CODE_PRICE_OUTLIER",
    "CODE_PRICE_DERIVED",
    "CODE_VALUE_DERIVABLE",
    "CROSS_CHECK_CODES",
    "check_line_totals",
    "check_document_total",
    "check_position_numbers",
    "check_price_outliers",
    "counts_in_document_total",
    "is_material_price",
    "is_scale_row",
    "expected_line_total",
    "find_document_total",
    "position_value",
    "run_checks",
]

# --------------------------------------------------------------------------
# Stellschrauben
# TODO: bei Bedarf in ExtractionSettings verschieben
# --------------------------------------------------------------------------

#: Relative Toleranz beim Vergleich von Betraegen (1 %).  Sie faengt
#: Rundungen und kaufmaennisch gerundete Zeilensummen ab.
TOLERANCE_RELATIVE = Decimal("0.01")

#: Absolute Zusatztoleranz in Waehrungseinheiten (2 Cent).
TOLERANCE_ABSOLUTE = Decimal("0.02")

#: Ab welchem Faktor gegenueber dem Median gilt ein Preis als Ausreisser.
#: Faktor 100 ist bewusst grob gewaehlt: er trifft den Dezimaltrennerfehler
#: (12,85 gegen 1285) und nicht das teure Sonderteil in einer Liste
#: von Normteilen.
PRICE_OUTLIER_FACTOR = Decimal("100")

#: Ab wie vielen Positionen mit Preis ist ein Median ueberhaupt aussagekraeftig.
PRICE_OUTLIER_MIN_POSITIONS = 3

#: Auf wie viele Nachkommastellen ein abgeleiteter Preis gerundet wird.
DERIVED_PRICE_DECIMALS = Decimal("0.0001")

# --------------------------------------------------------------------------
# Befundschluessel
# --------------------------------------------------------------------------

CODE_LINE_TOTAL_MISMATCH = "line_total_mismatch"
CODE_DOCUMENT_TOTAL_MISMATCH = "document_total_mismatch"
CODE_POSITION_GAP = "position_gap"
CODE_POSITION_DUPLICATE = "position_duplicate"
CODE_PRICE_OUTLIER = "price_outlier"
CODE_PRICE_DERIVED = "price_derived"
CODE_VALUE_DERIVABLE = "value_derivable"

#: Befunde, die als *fehlgeschlagene* Kreuzpruefung zaehlen (Konfidenzabzug).
CROSS_CHECK_CODES = frozenset({
    CODE_LINE_TOTAL_MISMATCH,
    CODE_DOCUMENT_TOTAL_MISMATCH,
    CODE_POSITION_GAP,
    CODE_PRICE_OUTLIER,
})

#: Beschriftungen einer Belegsumme im Fliesstext.  Reihenfolge egal -- die
#: Auswahl unter mehreren Treffern regelt :func:`find_document_total`.
_TOTAL_LABELS = (
    r"gesamtsumme", r"nettosumme", r"netto\s*gesamt", r"summe\s*netto",
    r"gesamtbetrag", r"auftragswert", r"angebotswert", r"gesamtwert",
    r"warenwert", r"zwischensumme", r"nettowert",
    r"net\s*total", r"total\s*net", r"total\s*amount", r"grand\s*total",
    r"order\s*value", r"subtotal", r"total",
)

_TOTAL_RE = re.compile(
    r"(?P<label>" + "|".join(_TOTAL_LABELS) + r")"
    r"\s*(?:\(netto\)|netto|net)?\s*[:=]?\s*"
    r"(?:EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£)?\s*"
    # Zahl in deutscher wie englischer Schreibweise; die Formen mit
    # Tausendertrennung stehen zuerst, sonst bliebe von "1.234,56" nur "1".
    r"(?P<value>-?\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?"
    r"|-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|-?\d+(?:[.,]\d{1,2})?)"
    r"\s*(?:EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£)?",
    re.I,
)

#: Positionen, die nur eine Preisstaffel zur Vorposition abbilden, duerfen
#: nicht in die Belegsumme einfliessen -- sie sind dieselbe Ware.
_SCALE_MARK = "staffelpreis"


# --------------------------------------------------------------------------
# Hilfsmittel
# --------------------------------------------------------------------------

def _tolerance(reference: Decimal) -> Decimal:
    """Erlaubte Abweichung zu einem Referenzbetrag."""
    return abs(reference) * TOLERANCE_RELATIVE + TOLERANCE_ABSOLUTE


def _matches(a: Decimal, b: Decimal) -> bool:
    """Stimmen zwei Betraege im Rahmen der Toleranz ueberein?"""
    return abs(a - b) <= _tolerance(max(abs(a), abs(b)))


def _price_unit(position: OfferPosition) -> Decimal:
    """Preiseinheit als ``Decimal``; fehlt sie, gilt 1."""
    unit = position.price_unit
    try:
        value = Decimal(unit) if unit else Decimal(1)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(1)
    return value if value > 0 else Decimal(1)


def expected_line_total(position: OfferPosition) -> Decimal | None:
    """``Menge * Preis / Preiseinheit`` -- ``None``, wenn etwas fehlt."""
    if position.quantity is None or position.price is None:
        return None
    return Decimal(position.quantity) * Decimal(position.price) / _price_unit(position)


def position_value(position: OfferPosition) -> Decimal | None:
    """Wert einer Position: die Zeilensumme aus dem Beleg, sonst gerechnet."""
    if position.line_total is not None:
        return Decimal(position.line_total)
    return expected_line_total(position)


def is_scale_row(position: OfferPosition) -> bool:
    """Ist diese Position nur eine Staffelstufe der Vorposition?"""
    return _SCALE_MARK in (position.remarks or "").lower()


def counts_in_document_total(position: OfferPosition) -> bool:
    """Darf diese Position in die Summe aller Positionen einfliessen?

    Staffelstufen sind dieselbe Ware wie die Vorposition, Zwischensummen
    wiederholen Betraege, und Alternativpositionen sind ein Entweder-oder --
    alle drei wuerden die Belegsumme verfaelschen.  Einmalkosten dagegen
    zaehlen mit: der Lieferant rechnet sie in seine Belegsumme ein.
    """
    from .position_kinds import counts_towards_document_total

    return not is_scale_row(position) and counts_towards_document_total(position)


def is_material_price(position: OfferPosition) -> bool:
    """Traegt diese Position einen vergleichbaren *Material*preis?

    Ein Werkzeugpreis von 8.500 EUR neben Drehteilen zu 12 EUR ist kein
    Dezimaltrennerfehler -- er darf die Ausreisserpruefung nicht ausloesen.
    """
    from .position_kinds import counts_as_material_price

    return counts_as_material_price(position)


def position_label(position: OfferPosition) -> str:
    """Sprechende Bezeichnung fuer Notizen ("Position 20")."""
    if position.position_number:
        return f"Position {position.position_number}"
    return f"Position '{position.display_name}'"


def _mark_uncertain(position: OfferPosition, *fields: str) -> None:
    """Felder als unsicher kennzeichnen, ohne ihren Wert anzutasten."""
    for name in fields:
        if getattr(position, name, None) in (None, ""):
            continue
        position.field_origins[name] = FieldOrigin.UNCERTAIN


def _add_issue(position: OfferPosition, code: str, message: str,
               field_name: str = "", detail: str = "") -> None:
    position.issues.add(Issue(code, message, IssueSeverity.WARNING,
                              detail=detail, field=field_name, blocking=False))


# --------------------------------------------------------------------------
# a) Zeilensumme gegen Menge x Preis
# --------------------------------------------------------------------------

def check_line_totals(offer: Offer) -> list[str]:
    """Jede Position gegen ihre Zeilensumme pruefen.

    Ohne Gesamtpreisspalte im Beleg passiert hier bewusst *nichts*: eine
    Pruefung ohne Vergleichswert waere eine Falschmeldung.
    """
    notes: list[str] = []
    for position in offer.positions:
        notes.extend(check_line_total(position))
    return notes


def check_line_total(position: OfferPosition) -> list[str]:
    """Einzelne Position gegen ihre Zeilensumme pruefen."""
    total = position.line_total
    if total is None:
        return []
    total = Decimal(total)
    label = position_label(position)
    unit = _price_unit(position)
    notes: list[str] = []

    expected = expected_line_total(position)
    if expected is not None:
        if _matches(expected, total):
            position.confidence_reasons.append(
                "Zeilensumme bestaetigt Menge und Preis")
            return notes
        vorschlag = _mismatch_suggestion(position, total, unit)
        note = (f"{label}: {format_decimal(Decimal(position.quantity), 3)} x "
                f"{format_decimal(Decimal(position.price))} = "
                f"{format_decimal(expected)}, im Beleg steht aber "
                f"{format_decimal(total)} -- bitte Menge/Preis pruefen.")
        if vorschlag:
            note = f"{note} {vorschlag}"
        notes.append(note)
        _mark_uncertain(position, "quantity", "price")
        _add_issue(position, CODE_LINE_TOTAL_MISMATCH, note, "price",
                   detail=f"Zeilensumme laut Beleg: {format_decimal(total)}")
        return notes

    # Preis fehlt, Menge und Zeilensumme liegen vor -> ausrechnen (nicht raten)
    if position.price is None and position.quantity not in (None, 0):
        quantity = Decimal(position.quantity)
        if quantity != 0:
            price = (total * unit / quantity).quantize(DERIVED_PRICE_DECIMALS)
            position.set_field("price", price, FieldOrigin.UNCERTAIN)
            note = (f"{label}: der Preis stand nicht in der Tabelle und wurde aus "
                    f"Zeilensumme und Menge berechnet "
                    f"({format_decimal(total)} / {format_decimal(quantity, 3)}"
                    f"{f' x {unit}' if unit != 1 else ''} = "
                    f"{format_decimal(price, 4)}) -- gerechnet, nicht geraten; "
                    "bitte bestaetigen.")
            notes.append(note)
            _add_issue(position, CODE_PRICE_DERIVED, note, "price")
            return notes

    # Menge fehlt: sie waere ableitbar -- aber nur als Vorschlag in der Notiz.
    if position.quantity is None and position.price not in (None, 0):
        price = Decimal(position.price)
        if price != 0:
            quantity = (total * unit / price).quantize(DERIVED_PRICE_DECIMALS)
            note = (f"{label}: die Menge wurde nicht erkannt. Aus Zeilensumme "
                    f"{format_decimal(total)} und Preis {format_decimal(price)} "
                    f"ergaebe sich {format_decimal(quantity, 3)} -- als Vorschlag "
                    "gedacht, bitte selbst eintragen.")
            notes.append(note)
            _add_issue(position, CODE_VALUE_DERIVABLE, note, "quantity")
    return notes


def _mismatch_suggestion(position: OfferPosition, total: Decimal,
                         unit: Decimal) -> str:
    """Konkreten Verdacht formulieren, ohne etwas zu aendern."""
    quantity = Decimal(position.quantity)
    price = Decimal(position.price)
    teile: list[str] = []
    if quantity != 0:
        passender_preis = (total * unit / quantity).quantize(DERIVED_PRICE_DECIMALS)
        teile.append(f"Preis {format_decimal(passender_preis, 4)}")
    if price != 0:
        passende_menge = (total * unit / price).quantize(DERIVED_PRICE_DECIMALS)
        teile.append(f"Menge {format_decimal(passende_menge, 3)}")
    if not teile:
        return ""
    return ("Zur Zeilensumme wuerde passen: " + " oder ".join(teile)
            + " (Vorschlag -- es wird nichts automatisch geaendert).")


# --------------------------------------------------------------------------
# b) Belegsumme gegen Summe der Positionen
# --------------------------------------------------------------------------

def find_document_total(text: str) -> tuple[Decimal, str] | None:
    """Belegsumme im Fliesstext suchen: ``(Betrag, Beschriftung)``.

    Unter mehreren Treffern gewinnt die *Nettosumme* -- danach gilt der letzte
    Treffer, weil die Endsumme ueblicherweise unten steht.  Gibt es keinen
    eindeutigen Zahlenwert, wird nichts zurueckgegeben.
    """
    if not text:
        return None
    treffer: list[tuple[Decimal, str]] = []
    for match in _TOTAL_RE.finditer(text):
        value = parse_decimal(match.group("value"))
        if value is None:
            continue
        treffer.append((value, normalize_whitespace(match.group("label"))))
    if not treffer:
        return None
    for value, label in treffer:
        if "netto" in label.lower() or re.search(r"\bnet\b", label.lower()):
            return value, label
    return treffer[-1]


def check_document_total(offer: Offer, text: str | None = None) -> list[str]:
    """Belegsumme gegen die Summe der erkannten Positionen halten.

    Das ist die wertvollste Pruefung ueberhaupt: sie deckt *fehlende*
    Positionen auf -- also genau das, was man an der Oberflaeche nicht sieht.
    """
    quelle = text if text is not None else offer.raw_text
    gefunden = find_document_total(quelle or "")
    if gefunden is None:
        return []
    beleg_summe, label = gefunden

    werte: list[Decimal] = []
    ohne_wert = 0
    ausgeklammert = 0
    for position in offer.positions:
        if is_scale_row(position):
            continue
        if not counts_in_document_total(position):
            ausgeklammert += 1
            continue
        wert = position_value(position)
        if wert is None:
            ohne_wert += 1
            continue
        werte.append(wert)

    if not werte:
        return []
    if ohne_wert:
        # Fehlt bei einzelnen Positionen der Preis (z. B. "auf Anfrage"), kann
        # die Belegsumme nicht exakt bestaetigt werden.  Die uebrigen
        # Positionen werden trotzdem geprueft: uebersteigt ihre Teilsumme die
        # Belegsumme bereits, ist sicher etwas falsch gelesen worden.
        teilsumme = sum(werte, Decimal(0))
        if teilsumme > beleg_summe + _tolerance(beleg_summe):
            note = (f"Teilpruefung der Belegsumme: schon die {len(werte)} "
                    f"Position(en) mit Preis summieren sich auf "
                    f"{format_decimal(teilsumme)} und liegen damit UEBER der "
                    f"Belegsumme {format_decimal(beleg_summe)} ('{label}') -- "
                    f"bitte Mengen und Preise pruefen ({ohne_wert} Position(en) "
                    "ohne Preis blieben unberuecksichtigt).")
            offer.issues.add(Issue(CODE_DOCUMENT_TOTAL_MISMATCH, note,
                                   IssueSeverity.WARNING, blocking=False,
                                   detail=f"Beschriftung im Beleg: {label}"))
        else:
            note = (f"Belegsumme {format_decimal(beleg_summe)} nur teilweise "
                    f"geprueft: {ohne_wert} Position(en) ohne Menge oder Preis "
                    f"(z. B. 'auf Anfrage'). Die uebrigen {len(werte)} "
                    f"Position(en) summieren sich auf {format_decimal(teilsumme)} "
                    "und liegen nicht ueber der Belegsumme -- kein Widerspruch.")
        offer.add_note(note)
        return [note]

    summe = sum(werte, Decimal(0))
    if _matches(summe, beleg_summe):
        for position in offer.positions:
            position.confidence_reasons.append(
                "Belegsumme bestaetigt die erkannten Positionen")
        return []

    differenz = beleg_summe - summe
    vermutung = ("moeglicherweise wurde eine Position uebersehen"
                 if differenz > 0
                 else "moeglicherweise wurde eine Position zu viel erkannt")
    note = (f"Summe der {len(werte)} erkannten Positionen: {format_decimal(summe)}, "
            f"im Beleg steht {format_decimal(beleg_summe)} ('{label}') -- "
            f"Differenz {format_decimal(abs(differenz))}, {vermutung}.")
    if ausgeklammert:
        note += (f" ({ausgeklammert} Zwischensummen-/Alternativposition(en) "
                 "wurden bewusst nicht mitgezaehlt.)")
    offer.issues.add(Issue(CODE_DOCUMENT_TOTAL_MISMATCH, note,
                           IssueSeverity.WARNING, blocking=False,
                           detail=f"Beschriftung im Beleg: {label}"))
    offer.add_note(note)
    for position in offer.positions:
        position.confidence_reasons.append(
            "Belegsumme passt nicht zur Summe der Positionen")
    return [note]


# --------------------------------------------------------------------------
# c) Positionsnummern-Folge
# --------------------------------------------------------------------------

def check_position_numbers(offer: Offer) -> list[str]:
    """Luecken und Dubletten in der Positionsnummernfolge melden.

    Es wird ausschliesslich *gemeldet*.  Eine fehlende 30 zwischen 20 und 40
    ist ein starker Hinweis auf eine uebersehene Zeile -- aber eben nur ein
    Hinweis: manche Lieferanten lassen Nummern schlicht aus.
    """
    notes: list[str] = []
    for gruppe in _number_groups(offer).values():
        notes.extend(_check_number_group(offer, gruppe))
    return notes


def _signature(position: OfferPosition) -> tuple[str, str]:
    """Kennung einer Position fuer die Dublettenpruefung.

    Zwei Zeilen mit derselben Positionsnummer sind noch keine Dublette: bei
    Staffelpreisen wiederholt der Lieferant die Nummer absichtlich, nennt aber
    einen anderen Preis.  Nur wenn Artikel *und* Preis uebereinstimmen, ist
    tatsaechlich zweimal dasselbe erkannt worden.
    """
    artikel = (position.material_number or position.vendor_material_number
               or position.description)
    return artikel, "" if position.price is None else str(position.price)


def _number_groups(offer: Offer) -> dict[str, list[tuple[int, tuple[str, str]]]]:
    """Positionsnummern nach Herkunftstabelle buendeln.

    Eine Luecke oder Dublette sagt nur *innerhalb einer Tabelle* etwas aus.
    Kommen Mail und Anhang zusammen, faengt der Anhang selbstverstaendlich
    wieder bei 10 an -- daraus eine Dublette zu machen, waere eine
    Falschmeldung.
    """
    gruppen: dict[str, list[tuple[int, tuple[str, str]]]] = {}
    for position in offer.positions:
        if position.origin("position_number") is FieldOrigin.DEFAULT:
            continue        # selbst vergebene Nummern beweisen nichts
        if is_scale_row(position):
            continue
        if getattr(position, "position_kind", "material") == "subtotal":
            continue        # Summenzeilen tragen keine echte Positionsnummer
        text = normalize_whitespace(position.position_number)
        if not re.fullmatch(r"\d{1,6}", text):
            continue
        schluessel = (position.source_hint or "").split(", Zeile")[0]
        gruppen.setdefault(schluessel or position.source_kind.value, []).append(
            (int(text), _signature(position)))
    return gruppen


def _check_number_group(offer: Offer,
                        eintraege: list[tuple[int, tuple[str, str]]]) -> list[str]:
    """Eine Nummernfolge auf Luecken und Dubletten pruefen."""
    if len(eintraege) < 3:
        return []

    nummern = [nummer for nummer, _ in eintraege]
    notes: list[str] = []
    kennungen = [(nummer, signatur) for nummer, signatur in eintraege]
    dubletten = sorted({nummer for nummer, signatur in kennungen
                        if kennungen.count((nummer, signatur)) > 1})
    if dubletten:
        note = ("Positionsnummern kommen mehrfach vor: "
                + ", ".join(str(n) for n in dubletten)
                + " -- bitte pruefen, ob eine Zeile doppelt erkannt wurde.")
        offer.issues.add(Issue(CODE_POSITION_DUPLICATE, note,
                               IssueSeverity.WARNING, blocking=False))
        offer.add_note(note)
        notes.append(note)

    eindeutig = sorted(set(nummern))
    schritt = _common_step(eindeutig)
    if schritt is None:
        return notes
    fehlend: list[int] = []
    for links, rechts in zip(eindeutig, eindeutig[1:]):
        luecke = rechts - links
        if luecke > schritt and luecke % schritt == 0:
            fehlend.extend(range(links + schritt, rechts, schritt))
    if not fehlend:
        return notes

    note = (f"In der Positionsnummernfolge fehlt/fehlen "
            f"{', '.join(str(n) for n in fehlend)} (Schrittweite {schritt}) -- "
            "moeglicherweise wurde(n) die zugehoerige(n) Zeile(n) uebersehen.")
    offer.issues.add(Issue(CODE_POSITION_GAP, note, IssueSeverity.WARNING,
                           blocking=False,
                           detail="erkannte Nummern: "
                                  + ", ".join(str(n) for n in eindeutig)))
    offer.add_note(note)
    for position in offer.positions:
        position.confidence_reasons.append("Luecke in der Positionsnummernfolge")
    notes.append(note)
    return notes


#: Ab dieser Schrittweite gilt eine Nummernfolge auch dann als geplant, wenn
#: sich der Schritt nicht wiederholt: 10/20/40 ist erkennbar ein 10er-Raster,
#: 1/2/7 dagegen eine blosse Aufzaehlung.
#: TODO: bei Bedarf in ExtractionSettings verschieben
MIN_OBVIOUS_STEP = 5


def _common_step(nummern: list[int]) -> int | None:
    """Uebliche Schrittweite der Nummernfolge -- ``None`` heisst "kein Muster".

    Als Schrittweite gilt die kleinste Differenz, und nur dann, wenn *alle*
    Differenzen ein Vielfaches davon sind.  Sonst wuerde aus einer beliebigen
    Aufzaehlung (1, 2, 7) eine Luecke herbeigerechnet.
    """
    if len(nummern) < 3:
        return None
    differenzen = [b - a for a, b in zip(nummern, nummern[1:]) if b > a]
    if not differenzen:
        return None
    schritt = min(differenzen)
    if schritt <= 0 or any(d % schritt for d in differenzen):
        return None
    if differenzen.count(schritt) < 2 and schritt < MIN_OBVIOUS_STEP:
        return None        # kein erkennbares Raster -> keine Aussage
    return schritt


# --------------------------------------------------------------------------
# d) Einheitliche Preisgroessenordnung
# --------------------------------------------------------------------------

def check_price_outliers(offer: Offer) -> list[str]:
    """Preise, die um mehr als Faktor 100 vom Median abweichen, melden.

    Das ist so gut wie immer ein Dezimaltrennerfehler ("1285" statt "12,85").
    Korrigiert wird trotzdem nichts -- es koennte ja das teure Sonderteil sein.
    """
    kandidaten = [p for p in offer.positions
                  if p.price is not None and not is_scale_row(p)
                  and is_material_price(p)]
    if len(kandidaten) < PRICE_OUTLIER_MIN_POSITIONS:
        return []
    werte = [Decimal(p.price) / _price_unit(p) for p in kandidaten]
    if any(w <= 0 for w in werte):
        werte = [w for w in werte if w > 0]
        if len(werte) < PRICE_OUTLIER_MIN_POSITIONS:
            return []
    mittelwert = Decimal(str(median(sorted(werte))))
    if mittelwert <= 0:
        return []

    obergrenze = mittelwert * PRICE_OUTLIER_FACTOR
    untergrenze = mittelwert / PRICE_OUTLIER_FACTOR
    notes: list[str] = []
    for position in kandidaten:
        wert = Decimal(position.price) / _price_unit(position)
        if wert <= 0 or untergrenze <= wert <= obergrenze:
            continue
        richtung = "ueber" if wert > obergrenze else "unter"
        note = (f"{position_label(position)}: der Preis "
                f"{format_decimal(Decimal(position.price))} liegt um mehr als "
                f"Faktor {int(PRICE_OUTLIER_FACTOR)} {richtung} dem Mittel der "
                f"uebrigen Positionen ({format_decimal(mittelwert)}) -- "
                "typisch fuer einen falsch gelesenen Dezimaltrenner, bitte pruefen.")
        _mark_uncertain(position, "price")
        _add_issue(position, CODE_PRICE_OUTLIER, note, "price")
        position.confidence_reasons.append("Preis weicht stark vom Median ab")
        notes.append(note)
    return notes


# --------------------------------------------------------------------------
# Gesamtlauf
# --------------------------------------------------------------------------

def run_checks(offer: Offer, text: str | None = None) -> list[str]:
    """Alle Kreuzpruefungen ausfuehren und die Notizen ins Angebot schreiben.

    Reihenfolge mit Bedacht: erst die Zeilensummen (dort kann ein Preis noch
    entstehen), dann die Belegsumme (die von genau diesen Werten lebt), dann
    Nummernfolge und Preisausreisser.
    """
    notes: list[str] = []
    try:
        notes.extend(check_line_totals(offer))
        notes.extend(check_document_total(offer, text))
        notes.extend(check_position_numbers(offer))
        notes.extend(check_price_outliers(offer))
    except Exception as exc:  # noqa: BLE001 -- eine Pruefung darf nie den Import kippen
        logger.error("Kreuzpruefung fehlgeschlagen: %s", exc, exc_info=True)
        return notes
    for note in notes:
        offer.add_note(note)
    if notes:
        logger.info("Kreuzpruefung: %d Hinweis(e) zum Angebot", len(notes))
    return notes
