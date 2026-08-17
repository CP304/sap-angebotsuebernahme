"""Wer ist wer? -- Rollenklaerung der beiden Artikelnummern.

In jedem Lieferantenangebot stehen zwei Nummern fuer denselben Artikel:

* **unsere** SAP-Materialnummer -- der Lieferant nennt sie aus seiner Sicht
  "Kundenartikelnummer", "Ihre Art.-Nr.", "Customer part no." ...
* **seine** eigene Nummer -- fuer ihn schlicht "Artikelnummer", "Unsere
  Art.-Nr.", "Supplier part no." ...

Das Tueckische: dieselbe Beschriftung bedeutet je nach Blickwinkel etwas
anderes.  "Artikelnummer" ohne Zusatz kann beides sein.  Deshalb entscheidet
hier *nicht die Ueberschrift allein*, sondern zusaetzlich der **Inhalt**:
passt ein Wert auf den eigenen Nummernkreis
(:attr:`~app.config.settings.ExtractionSettings.own_material_pattern`)?

Drei Regeln, in dieser Reihenfolge:

1. **Marker in der Ueberschrift schlagen alles.**  "Ihre/Kunden/Customer/Your"
   -> unsere Nummer.  "Unsere/Our/Supplier/Hersteller" -> seine Nummer.
2. **Inhalt entscheidet bei neutralen Beschriftungen.**  Passen mindestens
   70 % der Werte einer Spalte auf den eigenen Nummernkreis und ist die
   Materialspalte sonst leer, gilt sie als unsere Nummer.
3. **Vertauschtes wird getauscht -- aber nie stillschweigend.**  Jede so
   getroffene Entscheidung ist ``FieldOrigin.UNCERTAIN`` und erzeugt eine
   Notiz im Protokoll.  Es wird nie ein Wert erfunden oder geloescht.
"""

from __future__ import annotations

import logging
import re

from ...models.enums import FieldOrigin
from ...models.offer_position import OfferPosition
from ...utils.parsing import normalize_whitespace

logger = logging.getLogger(__name__)

__all__ = [
    "CUSTOMER_MARKERS",
    "SUPPLIER_MARKERS",
    "MATERIAL_FIELDS",
    "compile_own_pattern",
    "header_role",
    "matches_own_pattern",
    "own_ratio",
    "resolve_position_roles",
    "find_labelled_material_matches",
    "find_labelled_material_numbers",
]

#: Felder, um die es hier geht
MATERIAL_FIELDS = ("material_number", "vendor_material_number")

#: Woerter, die eine Spalte als *unsere* Nummer ausweisen ("aus Kundensicht")
CUSTOMER_MARKERS: tuple[str, ...] = (
    "ihre", "ihr", "kunden", "kunde", "kd", "kd-", "customer", "cust",
    "your", "buyer", "client",
)

#: Woerter, die eine Spalte als Nummer des *Lieferanten* ausweisen
SUPPLIER_MARKERS: tuple[str, ...] = (
    "unsere", "unser", "our", "supplier", "vendor", "lieferant", "lieferanten",
    "lief", "hersteller", "manufacturer", "maker",
)

#: Marker als Wortgrenzen-Regex (damit "kunde" nicht in "kundig" trifft)
_CUSTOMER_RE = re.compile(
    r"(?:^|[\s\-_/.])(?:" + "|".join(re.escape(m) for m in CUSTOMER_MARKERS)
    + r")(?:$|[\s\-_/.])", re.I)
_SUPPLIER_RE = re.compile(
    r"(?:^|[\s\-_/.])(?:" + "|".join(re.escape(m) for m in SUPPLIER_MARKERS)
    + r")(?:$|[\s\-_/.])", re.I)

#: Rueckfallmuster, falls die Konfiguration ein ungueltiges Regex enthaelt
FALLBACK_OWN_PATTERN = r"^\d{6,18}$"


# --------------------------------------------------------------------------
# Muster und Ueberschriften
# --------------------------------------------------------------------------

def compile_own_pattern(pattern: str) -> re.Pattern[str]:
    """Muster fuer die eigene Materialnummer uebersetzen.

    Ein kaputtes Muster aus der Konfiguration darf den Import nicht stoppen --
    dann greift der Auslieferungszustand, und das steht im Protokoll.
    """
    try:
        return re.compile(pattern or FALLBACK_OWN_PATTERN)
    except re.error as exc:
        logger.error("Ungueltiges Muster fuer die eigene Materialnummer (%r): %s -- "
                     "es wird %r verwendet", pattern, exc, FALLBACK_OWN_PATTERN)
        return re.compile(FALLBACK_OWN_PATTERN)


def header_role(header: str) -> str:
    """Rolle einer Spaltenueberschrift anhand ihrer Marker.

    Liefert ``"material_number"`` (unsere Nummer), ``"vendor_material_number"``
    (seine Nummer) oder ``""``, wenn die Ueberschrift neutral ist.
    """
    text = normalize_whitespace(header).lower()
    if not text:
        return ""
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    customer = bool(_CUSTOMER_RE.search(text))
    supplier = bool(_SUPPLIER_RE.search(text))
    if customer and not supplier:
        return "material_number"
    if supplier and not customer:
        return "vendor_material_number"
    return ""


def matches_own_pattern(value: object, own_re: re.Pattern[str]) -> bool:
    """Sieht dieser Wert nach *unserer* Materialnummer aus?"""
    text = normalize_whitespace(value).replace(" ", "")
    if not text:
        return False
    return bool(own_re.match(text))


def own_ratio(values: list[str], own_re: re.Pattern[str]) -> float:
    """Anteil der Werte einer Spalte, die auf den eigenen Nummernkreis passen."""
    filled = [v for v in (normalize_whitespace(v) for v in values) if v]
    if not filled:
        return 0.0
    hits = sum(1 for v in filled if matches_own_pattern(v, own_re))
    return hits / len(filled)


# --------------------------------------------------------------------------
# Rollenklaerung auf Positionsebene
# --------------------------------------------------------------------------

def resolve_position_roles(position: OfferPosition, own_re: re.Pattern[str],
                           swap_detection: bool = True) -> str:
    """Vertauschte Artikelnummern einer Position richtigstellen.

    Zwei Faelle werden behandelt:

    * **Tausch** -- in ``material_number`` steht etwas, das *nicht* auf den
      eigenen Nummernkreis passt (z. B. ``DR-40527-NBR``), in
      ``vendor_material_number`` dagegen etwas, das passt (``47110001``).
    * **Nachziehen** -- ``material_number`` ist leer, und die Lieferantennummer
      sieht nach unserer Nummer aus.

    Rueckgabe: Klartextnotiz fuer ``Offer.extraction_notes`` (leer, wenn nichts
    geaendert wurde).  Beide betroffenen Felder werden auf
    :class:`FieldOrigin.UNCERTAIN` gesetzt -- der Anwender muss es sehen.
    """
    if not swap_detection:
        return ""
    own = normalize_whitespace(position.material_number)
    vendor = normalize_whitespace(position.vendor_material_number)

    if own and vendor:
        if not matches_own_pattern(own, own_re) and matches_own_pattern(vendor, own_re):
            position.set_field("material_number", vendor, FieldOrigin.UNCERTAIN)
            position.set_field("vendor_material_number", own, FieldOrigin.UNCERTAIN)
            note = (f"Artikelnummern getauscht: '{vendor}' sieht nach unserer "
                    f"Materialnummer aus, '{own}' nach der Nummer des Lieferanten "
                    f"(Position '{position.position_number or position.display_name}') "
                    "-- bitte pruefen.")
            logger.info("Vertauschte Artikelnummern korrigiert: %s <-> %s", own, vendor)
            return note
        return ""

    if not own and vendor and matches_own_pattern(vendor, own_re):
        position.set_field("material_number", vendor, FieldOrigin.UNCERTAIN)
        position.set_field("vendor_material_number", "", FieldOrigin.MISSING)
        note = (f"'{vendor}' war als Lieferantennummer erfasst, sieht aber nach "
                "unserer Materialnummer aus und wurde als solche uebernommen "
                f"(Position '{position.position_number or position.display_name}') "
                "-- bitte pruefen.")
        logger.info("Lieferantennummer %s als eigene Materialnummer uebernommen", vendor)
        return note
    return ""


# --------------------------------------------------------------------------
# Freitext: beschriftete Nummern
# --------------------------------------------------------------------------

#: "Ihre Materialnummer 47110001", "Kundenartikelnummer: 47110001",
#: "unter Ihrer Art.-Nr. 47110001", "Customer part 47110001"
_CUSTOMER_LABELLED = re.compile(
    r"(?:"
    r"ihre?r?\s*(?:art|artikel|material|mat|teile?|sach|bestell|zeichnungs)"
    r"[\s.\-]{0,3}(?:nummer|nr|no)?"
    r"|kunden[\s.\-]?(?:artikel|material|teile?|sach|bestell)?"
    r"[\s.\-]{0,3}(?:nummer|nr|no)"
    r"|kd[\s.\-]{0,3}art(?:ikel)?[\s.\-]{0,3}(?:nummer|nr|no)?"
    r"|customer\s*(?:part|material|item|article)?\s*(?:no|number|#)?"
    r"|your\s*(?:part|material|item|article|ref(?:erence)?)\s*(?:no|number|#)?"
    r")"
    r"\.?\s*[:.\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._\-/]{2,24})",
    re.I,
)

#: "unsere Artikelnummer DR-40527", "our part no. X-12", "Hersteller-Nr. ABC"
_SUPPLIER_LABELLED = re.compile(
    r"(?:"
    r"unsere?r?\s*(?:art|artikel|material|mat|sach|teile?|bestell)"
    r"[\s.\-]{0,3}(?:nummer|nr|no)?"
    r"|(?:lieferanten|hersteller)[\s.\-]?(?:artikel|material|teile?)?"
    r"[\s.\-]{0,3}(?:nummer|nr|no)"
    r"|our\s*(?:part|material|item|article)?\s*(?:no|number|#)?"
    r"|(?:supplier|vendor|manufacturer)\s*(?:part|material|item)?\s*(?:no|number|#)?"
    r")"
    r"\.?\s*[:.\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._\-/]{2,24})",
    re.I,
)


def find_labelled_material_matches(text: str) -> dict[str, tuple[str, tuple[int, int]]]:
    """Eindeutig beschriftete Artikelnummern mit ihrer Fundstelle.

    Rueckgabe: ``{"material_number": (Wert, Span), ...}`` -- nur fuer die
    Rollen, die sich *eindeutig* aus der Beschriftung ergeben.  Neutrale
    Beschriftungen ("Artikel 4711") stehen bewusst nicht darin; darum kuemmert
    sich der Freitextleser wie bisher.
    """
    found: dict[str, tuple[str, tuple[int, int]]] = {}
    if not text:
        return found
    own_match = _CUSTOMER_LABELLED.search(text)
    vendor_match = _SUPPLIER_LABELLED.search(text)
    if own_match and vendor_match and own_match.span(1) == vendor_match.span(1):
        # Beide Muster treffen dieselbe Stelle -- die Beschriftung ist also
        # nicht eindeutig, und es wird nichts entschieden.
        return found
    if own_match:
        value = own_match.group(1).strip(" .,;:")
        if value:
            found["material_number"] = (value, own_match.span(1))
    if vendor_match:
        value = vendor_match.group(1).strip(" .,;:")
        if value:
            found["vendor_material_number"] = (value, vendor_match.span(1))
    return found


def find_labelled_material_numbers(text: str) -> tuple[str, str]:
    """Bequeme Kurzform: ``(unsere_nummer, lieferantennummer)``."""
    found = find_labelled_material_matches(text)
    return (found.get("material_number", ("", (0, 0)))[0],
            found.get("vendor_material_number", ("", (0, 0)))[0])
