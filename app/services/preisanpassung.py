"""Preise in einem Rutsch anpassen -- ohne sie einzeln herauszusuchen.

Der Fall aus der Praxis
-----------------------
Der Lieferant meldet "alle Preise plus 3 %" oder "jede Position 0,10 EUR
teurer".  Bisher hiess das: jede Zeile anfassen, alten Wert ablesen,
neuen Wert ausrechnen, eintippen.  Bei vierzig Positionen ist das eine
halbe Stunde und vierzig Gelegenheiten, sich zu vertippen.

Womit gerechnet wird
--------------------
Die Bezugsgroesse ist eine bewusste Entscheidung des Anwenders, keine
Annahme dieses Moduls:

* ``BASE_CURRENT`` -- der Preis, der jetzt in der Tabelle steht.  Das ist
  gemeint, wenn das Angebot schon eingelesen ist und nachtraeglich
  korrigiert wird.
* ``BASE_OLD``     -- der Preis, der in SAP steht.  Das ist gemeint, wenn
  der Lieferant eine Erhoehung auf den BESTEHENDEN Preis nennt, ohne
  selbst neue Preise zu schicken.

Die Verwechslung dieser beiden ist der teuerste denkbare Fehler hier --
"plus 3 %" auf einen bereits erhoehten Preis gerechnet ergibt stillen
Unsinn.  Deshalb wird die Bezugsgroesse nie geraten, und das Ergebnis
nennt sie im Klartext.

Grundsaetze
-----------
* Es wird nichts uebersprungen, ohne es zu sagen.  Positionen ohne
  Ausgangswert erscheinen im Bericht, nicht im Nichts.
* Ein Ergebnis kleiner oder gleich null wird NICHT geschrieben.  Ein
  Preis von 0,00 EUR ist im Infosatz nicht "guenstig", sondern falsch.
* Jeder geaenderte Preis traegt danach ``FieldOrigin.MANUAL`` und eine
  Bemerkung, woraus er entstanden ist -- der Wert ist damit
  nachvollziehbar und nicht mit einem gelesenen zu verwechseln.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

logger = logging.getLogger(__name__)

__all__ = [
    "MODE_PERCENT", "MODE_ABSOLUTE", "MODE_SET",
    "BASE_CURRENT", "BASE_OLD",
    "AdjustmentResult", "preview_adjustment", "apply_adjustment",
]

#: Art der Anpassung
MODE_PERCENT = "prozent"
MODE_ABSOLUTE = "absolut"
#: Festwert -- fuer den Fall "alle auf denselben Preis"
MODE_SET = "festwert"

#: Bezugsgroesse
BASE_CURRENT = "aktuell"
BASE_OLD = "sap"

MODE_LABELS = {
    MODE_PERCENT: "prozentual",
    MODE_ABSOLUTE: "um einen festen Betrag",
    MODE_SET: "auf einen festen Preis",
}
BASE_LABELS = {
    BASE_CURRENT: "dem jetzigen Preis in der Tabelle",
    BASE_OLD: "dem bestehenden Preis aus SAP",
}


@dataclass
class AdjustmentResult:
    """Was eine Anpassung bewirken wuerde bzw. bewirkt hat."""

    #: (Position, alter Wert, neuer Wert)
    changes: list[tuple[object, Decimal, Decimal]] = field(default_factory=list)
    #: (Position, Grund) -- alles, was NICHT geaendert wurde
    skipped: list[tuple[object, str]] = field(default_factory=list)
    mode: str = MODE_PERCENT
    base: str = BASE_CURRENT
    value: Decimal = Decimal("0")

    @property
    def count(self) -> int:
        return len(self.changes)

    def summary(self) -> str:
        if self.mode == MODE_SET:
            was = f"auf {_zeige(self.value)} gesetzt"
        elif self.mode == MODE_PERCENT:
            was = f"um {_zeige(self.value)} % geaendert"
        else:
            was = f"um {_zeige(self.value)} geaendert"
        teile = [f"{self.count} Preis(e) {was}"]
        if self.mode != MODE_SET:
            teile.append(f"gerechnet auf {BASE_LABELS.get(self.base, self.base)}")
        if self.skipped:
            teile.append(f"{len(self.skipped)} Position(en) unveraendert")
        return ", ".join(teile) + "."

    def details(self) -> str:
        zeilen: list[str] = []
        if self.changes:
            zeilen.append("Geaendert:")
            for position, alt, neu in self.changes:
                nummer = getattr(position, "position_number", "") or "?"
                zeilen.append(f"  Pos. {nummer}: {_zeige(alt)} -> {_zeige(neu)}")
        if self.skipped:
            if zeilen:
                zeilen.append("")
            zeilen.append("Unveraendert geblieben:")
            for position, grund in self.skipped:
                nummer = getattr(position, "position_number", "") or "?"
                zeilen.append(f"  Pos. {nummer}: {grund}")
        return "\n".join(zeilen)


def _zeige(wert: Decimal) -> str:
    """Zahl in deutscher Schreibweise."""
    text = f"{wert:,.2f}"
    return text.replace(",", "#").replace(".", ",").replace("#", ".")


def _basiswert(position, base: str) -> tuple[Decimal | None, str]:
    """Ausgangswert einer Position -- oder der Grund, warum es keinen gibt."""
    if base == BASE_OLD:
        satz = getattr(position, "sap_info_record", None)
        alt = getattr(satz, "net_price", None) if satz is not None else None
        if alt is None:
            return None, "kein bestehender Preis aus SAP bekannt"
        return Decimal(str(alt)), ""
    preis = getattr(position, "price", None)
    if preis is None:
        return None, "kein Preis vorhanden"
    return Decimal(str(preis)), ""


def _rechne(basis: Decimal, mode: str, value: Decimal,
            decimals: int) -> Decimal:
    if mode == MODE_SET:
        neu = value
    elif mode == MODE_PERCENT:
        neu = basis * (Decimal("100") + value) / Decimal("100")
    else:
        neu = basis + value
    quant = Decimal(1).scaleb(-decimals)
    return neu.quantize(quant, rounding=ROUND_HALF_UP)


def preview_adjustment(positions, mode: str, value, base: str = BASE_CURRENT,
                       decimals: int = 2) -> AdjustmentResult:
    """Berechnen, was passieren wuerde -- ohne etwas zu aendern.

    Damit kann die Oberflaeche das Ergebnis zeigen, BEVOR der Anwender
    zustimmt.  Bei vierzig Positionen ist das der Unterschied zwischen
    einer Entscheidung und einem Sprung ins Wasser.
    """
    try:
        betrag = Decimal(str(value))
    except (InvalidOperation, ValueError):
        ergebnis = AdjustmentResult(mode=mode, base=base)
        for position in positions:
            ergebnis.skipped.append((position, f"unbrauchbarer Wert: {value!r}"))
        return ergebnis

    ergebnis = AdjustmentResult(mode=mode, base=base, value=betrag)
    for position in positions:
        basis, grund = _basiswert(position, base)
        if basis is None:
            ergebnis.skipped.append((position, grund))
            continue
        neu = _rechne(basis, mode, betrag, decimals)
        if neu <= 0:
            # Ein Preis von 0,00 ist im Infosatz nicht guenstig, sondern
            # falsch -- und faellt spaeter niemandem auf.
            ergebnis.skipped.append(
                (position, f"Ergebnis waere {_zeige(neu)} -- kein gueltiger Preis"))
            continue
        if neu == basis:
            ergebnis.skipped.append((position, "unveraendert"))
            continue
        ergebnis.changes.append((position, basis, neu))
    return ergebnis


def apply_adjustment(positions, mode: str, value, base: str = BASE_CURRENT,
                     decimals: int = 2) -> AdjustmentResult:
    """Die Anpassung tatsaechlich durchfuehren."""
    from ..models.enums import FieldOrigin

    ergebnis = preview_adjustment(positions, mode, value, base, decimals)
    for position, alt, neu in ergebnis.changes:
        position.set_field("price", neu, FieldOrigin.MANUAL)
        vermerk = _vermerk(ergebnis, alt, neu)
        bemerkungen = getattr(position, "confidence_reasons", None)
        if bemerkungen is not None:
            bemerkungen.append(vermerk)
    logger.info("Preisanpassung: %s", ergebnis.summary())
    return ergebnis


def _vermerk(ergebnis: AdjustmentResult, alt: Decimal, neu: Decimal) -> str:
    """Klartext, woraus dieser Preis entstanden ist."""
    if ergebnis.mode == MODE_SET:
        return f"Preis von Hand auf {_zeige(neu)} gesetzt (vorher {_zeige(alt)})"
    if ergebnis.mode == MODE_PERCENT:
        art = f"{_zeige(ergebnis.value)} %"
    else:
        art = _zeige(ergebnis.value)
    return (f"Preis um {art} angepasst: {_zeige(alt)} -> {_zeige(neu)} "
            f"(Grundlage: {BASE_LABELS.get(ergebnis.base, ergebnis.base)})")
