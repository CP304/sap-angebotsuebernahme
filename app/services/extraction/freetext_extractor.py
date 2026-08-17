"""Positionen aus Fliesstext -- fuer E-Mails und PDFs ohne Tabellenstruktur.

Sehr viele Preisinformationen kommen im Einkauf formlos:

    "Artikel 4711 / 100 Stueck / 12,85 EUR"
    "Mat.-Nr. 47110001, Preis 12,85 EUR/St ab 01.09.2026"
    "Wir erhoehen den Preis fuer Dichtring 40x52x7 zum 01.09.2026 auf 12,85 EUR"
    "- 4711-002  Dichtring  100 St  12,85 EUR"

Leitlinie: **lieber weniger erkennen, dafuer nachvollziehbar.**  Ohne einen
klar erkennbaren Preis entsteht keine Position.  Alle abgeleiteten Werte werden
als :class:`FieldOrigin.UNCERTAIN` markiert, und die Originalzeile steht in
``source_hint``/``raw_text``, damit der Anwender jede Zeile pruefen kann.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ...config.settings import Settings
from ...models.enums import FieldOrigin
from ...models.offer_position import OfferPosition
from ...utils.parsing import (
    detect_currency,
    normalize_material_number,
    normalize_uom,
    normalize_whitespace,
    parse_date,
    parse_int,
)
from ..readers.base import RawDocument
from .table_extractor import parse_number

logger = logging.getLogger(__name__)

__all__ = [
    "FreetextExtractor",
    "FreetextResult",
    "DEFAULT_MATERIAL_CANDIDATE",
]

#: Standardmuster fuer eine Materialnummer: 6-18 Ziffern oder alphanumerisch
#: mit Trennzeichen (4711-002, AB.12.345, X100/7).
DEFAULT_MATERIAL_CANDIDATE = (
    r"(?<![\w./-])(?:\d{6,18}|[A-Za-z0-9]{2,}(?:[-/.][A-Za-z0-9]{1,})+)(?![\w./-])"
)

#: Beschriftete Materialnummer -- deutlich verlaesslicher als ein blosser Fund
_LABELLED_MATERIAL = re.compile(
    r"(?:Mat(?:erial)?[\s.\-]{0,3}(?:Nr|Nummer)?|Artikel(?:\s?nummer|[\s.\-]{0,3}Nr)?|"
    r"Art[\s.\-]{0,3}Nr|Sachnummer|Teile[\s.\-]{0,3}(?:Nr|nummer)|"
    r"Bestell[\s.\-]{0,3}(?:Nr|nummer)|"
    r"Item|Part(?:\s*(?:No|Number))?|SKU)\.?\s*[:.\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._\-/]{2,24})",
    re.I,
)

#: Preis mit Waehrung oder Preis hinter einem eindeutigen Schluesselwort
_PRICE_WITH_CURRENCY = re.compile(
    r"(?:(?P<pre>EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£)\s*)?"
    r"(?P<value>\d{1,3}(?:[.\s]\d{3})*[.,]\d{1,4}|\d{1,7}[.,]\d{1,4}|\d{1,7})"
    r"\s*(?P<post>EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£)?",
    re.I,
)

#: Schluesselwoerter, die den Zahlwert dahinter als Preis ausweisen
_PRICE_KEYWORD = re.compile(
    r"(?:preis|price|kostet|kosten|betraegt|betr(?:ae|ä)gt|auf|à|a|je|zu|"
    r"stueckpreis|st(?:ue|ü)ckpreis|einzelpreis|netto|unit price)\s*[:=]?\s*$",
    re.I,
)

#: Menge mit Einheit
_QUANTITY = re.compile(
    r"(?<![\w,.])(\d{1,3}(?:[.\s]\d{3})*|\d{1,7})(?:[.,]\d{1,3})?\s*"
    r"(stk\.?|st(?:ue|ü)ck|st\.?|stck\.?|pcs\.?|pc\.?|pieces|kg|g|m|mm|cm|lfm|"
    r"m2|m3|qm|l(?:tr)?\.?|set|satz|paar|pa|karton|kar|rolle|rol|pack|pak)\b",
    re.I,
)

#: Preiseinheit: "/100 St", "je 100 Stk", "pro 1000 Stueck"
_PRICE_UNIT = re.compile(
    r"(?:/|je|pro|per)\s*(\d{1,6})\s*"
    r"(?:stk\.?|st(?:ue|ü)ck|st\.?|pcs\.?|m|kg|l|set)\b",
    re.I,
)

#: Datumsangabe mit Schluesselwort -> gueltig ab
_VALID_FROM = re.compile(
    r"(?:ab|zum|per|gueltig\s+ab|g(?:ue|ü)ltig\s+ab|mit\s+Wirkung\s+(?:zum|ab)|"
    r"effective|from|valid\s+from|wirksam\s+ab)\s+"
    r"(?:dem\s+)?(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}\.?\s*[A-Za-zÄÖÜäöüß]{3,10}\.?\s+\d{4})",
    re.I,
)

#: Datum ohne Jahr ("zum 01.09.") -- wird bewusst NICHT geraten
_INCOMPLETE_DATE = re.compile(r"(?:ab|zum|per)\s+(\d{1,2}\.\d{1,2}\.)(?!\d)", re.I)

#: "Wir erhoehen den Preis fuer <Artikel> zum ..."
_PRICE_FOR = re.compile(
    r"(?:preis(?:e)?|price)\s+(?:f(?:ue|ü)r|for|von)\s+(?P<name>.{3,60}?)"
    r"\s+(?:zum|ab|auf|per|von|to|on)\b",
    re.I,
)

#: Zeilen, die keine Position sein koennen
_NOISE = re.compile(
    r"^\s*(?:mit\s+freundlich|best\s+regards|kind\s+regards|tel\.?|fax|mobil|"
    r"e-?mail|www\.|http|ust[\-\s]?id|steuernummer|handelsregister|amtsgericht|"
    r"gesch(?:ae|ä)ftsf(?:ue|ü)hrer|sitz\s+der|bankverbindung|iban|bic|"
    r"betreff|von|an|gesendet|subject|from|to|sent)\b",
    re.I,
)

#: Summen-/Steuerzeilen
_TOTALS = re.compile(
    r"\b(?:summe|gesamt|total|zwischensumme|mwst|mehrwertsteuer|umsatzsteuer|vat|"
    r"netto\s+gesamt|rechnungsbetrag|versandkosten|frachtkosten)\b", re.I)

#: Aufzaehlungszeichen am Zeilenanfang
_BULLET = re.compile(r"^\s*(?:[-*•·–—o]|\d{1,3}[.)])\s+")

_MAX_LINE_LENGTH = 300


@dataclass
class FreetextResult:
    """Positionen und Protokoll der Freitexterkennung."""

    positions: list[OfferPosition]
    notes: list[str]

    def __bool__(self) -> bool:
        return bool(self.positions)


class FreetextExtractor:
    """Zeilenbasierte Positionserkennung fuer unstrukturierte Quellen."""

    def __init__(self, settings: Settings,
                 material_pattern: str = DEFAULT_MATERIAL_CANDIDATE,
                 decimal_style: str = "auto") -> None:
        self.settings = settings
        self.decimal_style = decimal_style or "auto"
        try:
            self.material_re = re.compile(material_pattern)
        except re.error:
            logger.error("Ungueltiges Materialmuster, Standard wird verwendet")
            self.material_re = re.compile(DEFAULT_MATERIAL_CANDIDATE)

    # ------------------------------------------------------------------
    def extract(self, document: RawDocument, source_hint_prefix: str = "") -> FreetextResult:
        """Freitext eines Dokuments auswerten."""
        text = document.text or ""
        if not text.strip():
            return FreetextResult([], [])
        result = self.extract_text(text, document.source_kind, source_hint_prefix
                                   or document.display_name)
        logger.info("Freitexterkennung: %d Positionen aus %s",
                    len(result.positions), document.display_name)
        return result

    def extract_text(self, text: str, source_kind: Any = None,
                     source_label: str = "Freitext") -> FreetextResult:
        positions: list[OfferPosition] = []
        notes: list[str] = []
        lines = text.splitlines()
        used_lines: set[int] = set()

        for index, raw_line in enumerate(lines):
            line = normalize_whitespace(_BULLET.sub("", raw_line))
            if not self._is_candidate(line):
                continue
            position = self._position_from_line(line, notes)
            if position is None:
                continue
            position.source_kind = source_kind or position.source_kind
            position.source_hint = f"{source_label}, Zeile {index + 1}"
            position.raw_text = normalize_whitespace(raw_line)
            positions.append(position)
            used_lines.add(index)

        block_positions = self._extract_blocks(lines, used_lines, source_kind,
                                               source_label, notes)
        positions.extend(block_positions)

        if positions:
            notes.append(f"{len(positions)} Position(en) aus Fliesstext abgeleitet -- "
                         "alle abgeleiteten Werte sind als unsicher markiert und "
                         "muessen geprueft werden.")
        return FreetextResult(positions, notes)

    # ------------------------------------------------------------------
    def _is_candidate(self, line: str) -> bool:
        if not line or len(line) > _MAX_LINE_LENGTH:
            return False
        if _NOISE.match(line):
            return False
        if _TOTALS.search(line):
            return False
        if not re.search(r"\d", line):
            return False
        return True

    # ------------------------------------------------------------------
    def _position_from_line(self, line: str, notes: list[str]) -> OfferPosition | None:
        price, currency, price_span = self._find_price(line)
        if price is None:
            return None

        position = OfferPosition()
        position.set_field("price", price, FieldOrigin.UNCERTAIN)
        if currency:
            position.set_field("currency", currency, FieldOrigin.UNCERTAIN)

        consumed: list[tuple[int, int]] = [price_span]

        material, material_confident, material_span = self._find_material(line, price_span)
        if material:
            position.set_field("material_number", normalize_material_number(material),
                               FieldOrigin.EXTRACTED if material_confident
                               else FieldOrigin.UNCERTAIN)
            consumed.append(material_span)

        quantity, uom, quantity_span = self._find_quantity(line, consumed)
        if quantity is not None:
            position.set_field("quantity", quantity, FieldOrigin.UNCERTAIN)
            consumed.append(quantity_span)
        if uom:
            position.set_field("uom", normalize_uom(uom), FieldOrigin.UNCERTAIN)

        unit_match = _PRICE_UNIT.search(line)
        if unit_match:
            unit = parse_int(unit_match.group(1))
            if unit and unit > 0:
                position.set_field("price_unit", unit, FieldOrigin.UNCERTAIN)
                consumed.append(unit_match.span())

        date_match = _VALID_FROM.search(line)
        if date_match:
            valid_from = parse_date(date_match.group(1))
            if valid_from:
                position.set_field("valid_from", valid_from, FieldOrigin.UNCERTAIN)
                consumed.append(date_match.span())
        else:
            incomplete = _INCOMPLETE_DATE.search(line)
            if incomplete:
                notes.append(
                    f"Datum '{incomplete.group(1)}' ohne Jahresangabe in '{line[:60]}' -- "
                    "es wird kein Jahr geraten, bitte 'gueltig ab' pruefen.")

        description = self._find_description(line, consumed)
        if description:
            position.set_field("description", description, FieldOrigin.UNCERTAIN)

        if not position.material_number and not position.description:
            return None
        return position

    # ------------------------------------------------------------------
    def _find_price(self, line: str) -> tuple[Decimal | None, str, tuple[int, int]]:
        """Preis in einer Zeile finden (Waehrung oder Schluesselwort noetig)."""
        best: tuple[Decimal, str, tuple[int, int], int] | None = None
        for match in _PRICE_WITH_CURRENCY.finditer(line):
            value_text = match.group("value")
            has_decimals = bool(re.search(r"[.,]\d{1,4}$", value_text))
            currency_token = match.group("post") or match.group("pre") or ""
            before = line[:match.start()]
            keyword = bool(_PRICE_KEYWORD.search(before.rstrip()))
            if not currency_token and not (keyword and has_decimals):
                continue
            if not has_decimals and not currency_token:
                continue
            value = parse_number(value_text, self.decimal_style)
            if value is None or value <= 0:
                continue
            score = (2 if currency_token else 0) + (1 if keyword else 0) + \
                    (1 if has_decimals else 0)
            if best is None or score > best[3]:
                best = (value, detect_currency(currency_token), match.span(), score)
        if best is None:
            return None, "", (0, 0)
        return best[0], best[1], best[2]

    def _find_material(self, line: str,
                       price_span: tuple[int, int]) -> tuple[str, bool, tuple[int, int]]:
        labelled = _LABELLED_MATERIAL.search(line)
        if labelled and not _overlaps(labelled.span(1), price_span):
            return labelled.group(1).strip(" .,;"), True, labelled.span()
        for match in self.material_re.finditer(line):
            if _overlaps(match.span(), price_span):
                continue
            candidate = match.group(0)
            if parse_date(candidate) is not None:
                continue          # ein Datum ist keine Materialnummer
            if re.fullmatch(r"\d{1,5}", candidate):
                continue
            return candidate, False, match.span()
        return "", False, (0, 0)

    def _find_quantity(self, line: str, consumed: list[tuple[int, int]]
                       ) -> tuple[Decimal | None, str, tuple[int, int]]:
        for match in _QUANTITY.finditer(line):
            if any(_overlaps(match.span(1), span) for span in consumed):
                continue
            quantity = parse_number(match.group(1), self.decimal_style)
            if quantity is None or quantity <= 0:
                continue
            return quantity, match.group(2), match.span()
        return None, "", (0, 0)

    def _find_description(self, line: str, consumed: list[tuple[int, int]]) -> str:
        """Beschreibung ableiten -- bevorzugt aus 'Preis fuer <X> ...'."""
        named = _PRICE_FOR.search(line)
        if named:
            candidate = normalize_whitespace(named.group("name")).strip(" .,;:-")
            if len(candidate) >= 3:
                return candidate[:80]

        remainder = list(line)
        for start, end in consumed:
            for position in range(max(0, start), min(len(remainder), end)):
                remainder[position] = " "
        text = normalize_whitespace("".join(remainder))
        text = re.sub(r"\b(?:artikel|art\.?|mat(?:erial)?\.?|nr\.?|nummer|preis|price|"
                      r"menge|st(?:ue|ü)ck|stk\.?|je|pro|per|ab|zum|auf|netto|"
                      r"neuer|neu|wir|erh(?:oe|ö)hen|den|der|die|das|f(?:ue|ü)r|und)\b",
                      " ", text, flags=re.I)
        text = normalize_whitespace(re.sub(r"[/|,;:]+", " ", text)).strip(" .-")
        if len(text) < 3 or not re.search(r"[A-Za-zÄÖÜäöü]{3}", text):
            return ""
        return text[:80]

    # ------------------------------------------------------------------
    def _extract_blocks(self, lines: list[str], used_lines: set[int],
                        source_kind: Any, source_label: str,
                        notes: list[str]) -> list[OfferPosition]:
        """Bloecke aus "Beschriftung: Wert"-Zeilen auswerten.

        Beispiel (haeufig in Mails):

            Artikel:   4711-002
            Menge:     100 St
            Preis:     12,85 EUR
        """
        positions: list[OfferPosition] = []
        block: list[tuple[int, str]] = []

        def flush() -> None:
            if len(block) >= 2:
                position = self._position_from_block(block)
                if position is not None:
                    position.source_kind = source_kind or position.source_kind
                    first, last = block[0][0] + 1, block[-1][0] + 1
                    position.source_hint = f"{source_label}, Zeilen {first}-{last}"
                    position.raw_text = " | ".join(text for _, text in block)
                    positions.append(position)
            block.clear()

        for index, raw_line in enumerate(lines):
            line = normalize_whitespace(raw_line)
            if index in used_lines or not line or not re.match(r"^[^:]{2,30}:\s*\S", line):
                flush()
                continue
            block.append((index, line))
        flush()

        if positions:
            notes.append(f"{len(positions)} Position(en) aus Beschriftungsbloecken "
                         "('Artikel: ... / Preis: ...') abgeleitet.")
        return positions

    def _position_from_block(self, block: list[tuple[int, str]]) -> OfferPosition | None:
        values: dict[str, str] = {}
        for _, line in block:
            label, _, value = line.partition(":")
            key = normalize_whitespace(label).lower()
            values[key] = normalize_whitespace(value)

        def pick(*names: str) -> str:
            for name in names:
                for key, value in values.items():
                    if key.startswith(name):
                        return value
            return ""

        price_text = pick("preis", "price", "ep", "einzelpreis", "netto")
        if not price_text:
            return None
        price, currency, _ = self._find_price(price_text)
        if price is None:
            price = parse_number(price_text, self.decimal_style)
            currency = detect_currency(price_text)
        if price is None or price <= 0:
            return None

        position = OfferPosition()
        position.set_field("price", price, FieldOrigin.UNCERTAIN)
        if currency:
            position.set_field("currency", currency, FieldOrigin.UNCERTAIN)

        material = pick("artikel", "material", "mat", "teile", "sachnummer", "item", "part")
        if material:
            position.set_field("material_number", normalize_material_number(material),
                               FieldOrigin.UNCERTAIN)
        description = pick("bezeichnung", "beschreibung", "benennung", "text",
                           "description")
        if description:
            position.set_field("description", description, FieldOrigin.UNCERTAIN)

        quantity_text = pick("menge", "anzahl", "quantity", "stueckzahl", "stückzahl")
        if quantity_text:
            quantity, uom, _ = self._find_quantity(quantity_text, [])
            if quantity is None:
                quantity = parse_number(quantity_text, self.decimal_style)
            if quantity is not None:
                position.set_field("quantity", quantity, FieldOrigin.UNCERTAIN)
            if uom:
                position.set_field("uom", normalize_uom(uom), FieldOrigin.UNCERTAIN)

        uom_text = pick("einheit", "me", "mengeneinheit", "uom", "unit")
        if uom_text and not position.uom:
            position.set_field("uom", normalize_uom(uom_text), FieldOrigin.UNCERTAIN)

        valid_text = pick("gueltig ab", "gültig ab", "gueltig", "gültig", "valid from",
                          "preis gueltig")
        if valid_text:
            valid_from = parse_date(valid_text)
            if valid_from:
                position.set_field("valid_from", valid_from, FieldOrigin.UNCERTAIN)

        if not position.material_number and not position.description:
            return None
        return position


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
