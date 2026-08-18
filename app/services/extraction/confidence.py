"""Konfidenz je Position -- wie sehr darf man dieser Zeile trauen?

Die Zahl ersetzt keine Pruefung, sie *sortiert* die Pruefung: der Einkaeufer
soll zuerst dort hinsehen, wo die Erkennung selbst unsicher ist.  Sie entsteht
ausschliesslich aus nachvollziehbaren Bausteinen:

* **Erkennungsweg** -- eine Tabelle mit Kopfzeile ist verlaesslicher als eine
  Tabelle ohne Kopfzeile, und die wiederum verlaesslicher als Fliesstext.
* **Abzuege** -- fuer jedes unsicher erkannte Feld, fuer jede fehlgeschlagene
  Kreuzpruefung (siehe :mod:`.plausibility`) und fuer fehlende Pflichtfelder.
* **Zuschlag** -- wenn die Zeilensumme des Belegs Menge und Preis bestaetigt.

Jeder Baustein hinterlaesst einen Klartextgrund in
``OfferPosition.confidence_reasons``; die Zahl allein waere wertlos.
"""

from __future__ import annotations

from ...models.enums import FieldOrigin
from ...models.offer import Offer
from ...models.offer_position import OfferPosition
from .plausibility import CROSS_CHECK_CODES

__all__ = [
    "PATH_TABLE_HEADER",
    "PATH_TABLE_NO_HEADER",
    "PATH_FREETEXT",
    "PATH_MANUAL",
    "BASE_CONFIDENCE",
    "apply_confidence",
    "compute_confidence",
]

# --------------------------------------------------------------------------
# Stellschrauben
# TODO: bei Bedarf in ExtractionSettings verschieben
# --------------------------------------------------------------------------

#: Erkennungswege (werden in ``OfferPosition.extraction_path`` hinterlegt)
PATH_TABLE_HEADER = "table_header"
PATH_TABLE_NO_HEADER = "table_no_header"
PATH_FREETEXT = "freetext"
PATH_MANUAL = "manual"

#: Ausgangswert je Erkennungsweg
BASE_CONFIDENCE: dict[str, float] = {
    PATH_TABLE_HEADER: 0.90,
    PATH_TABLE_NO_HEADER: 0.75,
    PATH_FREETEXT: 0.55,
    PATH_MANUAL: 1.00,
}

#: Ausgangswert, wenn der Weg nicht festgehalten wurde
BASE_UNKNOWN = 0.60

#: Abzug je unsicher erkanntem Feld -- und Deckel dafuer
PENALTY_PER_UNCERTAIN = 0.08
PENALTY_UNCERTAIN_MAX = 0.32

#: Abzug je fehlgeschlagener Kreuzpruefung
PENALTY_PER_CROSS_CHECK = 0.20

#: Abzuege fuer fehlende Pflichtangaben
PENALTY_NO_PRICE = 0.30
PENALTY_NO_IDENTITY = 0.20
PENALTY_NO_CURRENCY = 0.05

#: Zuschlag, wenn die Zeilensumme Menge und Preis bestaetigt
BONUS_LINE_TOTAL_CONFIRMED = 0.08

#: Schwellen fuer :meth:`OfferPosition.confidence_label`
LABEL_SURE = 0.80
LABEL_CHECK = 0.50

#: Text, den :mod:`.plausibility` bei bestaetigter Zeilensumme hinterlegt
_CONFIRM_MARK = "Zeilensumme bestaetigt"


def compute_confidence(position: OfferPosition,
                       path: str | None = None) -> float:
    """Konfidenz einer Position berechnen und im Objekt hinterlegen.

    Die Gruende werden dabei neu aufgebaut; bereits vorhandene Gruende (etwa
    aus der Kreuzpruefung) bleiben erhalten und stehen vorn.
    """
    weg = path or position.extraction_path or ""
    basis = BASE_CONFIDENCE.get(weg, BASE_UNKNOWN)
    # Bereits vorhandene Gruende bleiben erhalten -- ohne Dubletten, damit ein
    # zweiter Aufruf (etwa nach einer Korrektur) die Liste nicht aufblaeht.
    gruende: list[str] = []
    for grund in position.confidence_reasons:
        if grund not in gruende:
            gruende.append(grund)
    wert = basis
    weg_grund = f"Erkennungsweg: {_path_label(weg)} ({basis:.0%})"
    if weg_grund in gruende:
        gruende.remove(weg_grund)
    gruende.insert(0, weg_grund)

    unsicher = position.uncertain_fields
    if unsicher:
        abzug = min(PENALTY_UNCERTAIN_MAX,
                    PENALTY_PER_UNCERTAIN * len(unsicher))
        wert -= abzug
        gruende.append(f"{len(unsicher)} unsicher erkannte(s) Feld(er) "
                       f"({', '.join(unsicher)}): -{abzug:.0%}")

    fehlgeschlagen = sorted({issue.code for issue in position.issues
                             if issue.code in CROSS_CHECK_CODES})
    if fehlgeschlagen:
        abzug = PENALTY_PER_CROSS_CHECK * len(fehlgeschlagen)
        wert -= abzug
        gruende.append("Kreuzpruefung nicht bestanden "
                       f"({', '.join(fehlgeschlagen)}): -{abzug:.0%}")

    if position.price is None:
        wert -= PENALTY_NO_PRICE
        gruende.append(f"kein Preis erkannt: -{PENALTY_NO_PRICE:.0%}")
    if not (position.material_number or position.vendor_material_number
            or position.description):
        wert -= PENALTY_NO_IDENTITY
        gruende.append(f"keine Artikelangabe erkannt: -{PENALTY_NO_IDENTITY:.0%}")
    if not position.currency:
        wert -= PENALTY_NO_CURRENCY
        gruende.append(f"keine Waehrung erkannt: -{PENALTY_NO_CURRENCY:.0%}")

    if any(_CONFIRM_MARK in grund for grund in position.confidence_reasons):
        wert += BONUS_LINE_TOTAL_CONFIRMED
        gruende.append("Zeilensumme des Belegs bestaetigt Menge und Preis: "
                       f"+{BONUS_LINE_TOTAL_CONFIRMED:.0%}")

    wert = max(0.0, min(1.0, wert))
    position.confidence = round(wert, 3)
    position.confidence_reasons = _ohne_dubletten(gruende)
    return position.confidence


def _ohne_dubletten(gruende: list[str]) -> list[str]:
    """Reihenfolge erhalten, Wiederholungen entfernen."""
    gesehen: list[str] = []
    for grund in gruende:
        if grund not in gesehen:
            gesehen.append(grund)
    return gesehen


def apply_confidence(offer: Offer) -> None:
    """Konfidenz fuer alle Positionen eines Angebots berechnen."""
    for position in offer.positions:
        compute_confidence(position)


def _path_label(path: str) -> str:
    return {
        PATH_TABLE_HEADER: "Tabelle mit Kopfzeile",
        PATH_TABLE_NO_HEADER: "Tabelle ohne Kopfzeile",
        PATH_FREETEXT: "Fliesstext",
        PATH_MANUAL: "manuell erfasst",
    }.get(path, "unbekannt")
