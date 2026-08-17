"""Positionen aus Tabellenbloecken gewinnen.

Der schwierige Teil eines Angebotsimports ist nicht das Lesen der Datei,
sondern die Frage: *Welche Spalte ist der Preis?*  Jeder Lieferant beschriftet
anders, manche liefern gar keine Kopfzeile.  Dieses Modul geht deshalb in drei
Stufen vor:

1. **Kopfzeile finden** -- Zeilen werden gegen ``settings.extraction.column_aliases``
   bewertet (exakt, enthalten, aehnlich).  Mehrzeilige Koepfe werden verschmolzen.
2. **Spalten zuordnen** -- je Spalte das wahrscheinlichste Feld mit Konfidenz.
   Mehrdeutigkeiten ("Listenpreis" *und* "Nettopreis") werden nach festen,
   nachvollziehbaren Regeln aufgeloest und protokolliert.
3. **Datentypen pruefen** -- eine Stichprobe je Spalte entscheidet mit.  Eine
   Spalte, die zu 80 % als Datum parst, *ist* ein Datum -- auch ohne Ueberschrift.
   Dadurch funktionieren auch Tabellen voellig ohne Kopfzeile.

Zusaetzlich: Summenzeilen ausschliessen, Fortsetzungszeilen anhaengen und
Staffelpreise als eigene Positionen ausweisen (niemals stillschweigend
zusammenfassen).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable

from ...config.settings import Settings
from ...models.enums import FieldOrigin, SourceKind
from ...models.offer_position import OfferPosition
from ...utils.parsing import (
    detect_currency,
    normalize_material_number,
    normalize_uom,
    normalize_whitespace,
    parse_date,
    parse_decimal,
    parse_int,
    similarity,
)
from ..readers.base import RawDocument, TableBlock
from .material_roles import (
    MATERIAL_FIELDS,
    compile_own_pattern,
    header_role,
    own_ratio,
)

if TYPE_CHECKING:  # pragma: no cover -- nur fuer die Typpruefung
    from .profiles import VendorProfile

logger = logging.getLogger(__name__)

__all__ = [
    "TableExtractor",
    "TableAnalysis",
    "ColumnAssignment",
    "ExtractionResult",
    "parse_number",
    "find_price_tiers",
    "is_summary_row",
]

# --------------------------------------------------------------------------
# Konstanten
# --------------------------------------------------------------------------

#: Pseudofelder: erkannt, aber bewusst *nicht* uebernommen
PSEUDO_TOTAL = "_total"
PSEUDO_IGNORE = "_ignore"
PSEUDO_ALT_PRICE = "_alternative_price"

#: Zusatzaliase fuer Spalten, die nicht in eine Position gehoeren.
#: Sie stehen bewusst vor den echten Feldern, damit "Gesamtpreis" nicht als
#: "Preis" durchgeht.
PSEUDO_ALIASES: dict[str, list[str]] = {
    PSEUDO_TOTAL: ["gesamt", "gesamtpreis", "gesamtwert", "gesamtbetrag", "summe",
                   "zeilensumme", "betrag", "wert", "total", "total price",
                   "amount", "net amount", "line total", "extended price",
                   "positionswert", "gesamtsumme"],
    PSEUDO_IGNORE: ["rabatt", "discount", "nachlass", "mwst", "ust", "umsatzsteuer",
                    "steuer", "tax", "vat", "skonto", "zoll", "gewicht", "kg-preis",
                    "bild", "foto", "status"],
}

#: Zeilen, die eine Summe/Steuer/Fracht ausweisen -- keine Position
_SUMMARY_WORDS = (
    "summe", "zwischensumme", "gesamtsumme", "gesamt", "gesamtbetrag", "total",
    "subtotal", "sub-total", "netto gesamt", "nettobetrag", "endbetrag",
    "rechnungsbetrag", "mwst", "ust.", "umsatzsteuer", "mehrwertsteuer", "vat",
    "zzgl", "inkl", "frachtkosten", "versandkosten", "verpackung", "mindermenge",
    "grand total", "net total", "sum", "auftragswert",
)

_SUMMARY_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(w) for w in _SUMMARY_WORDS) + r")\b", re.I)

#: Staffelpreis in einer Zelle: "ab 100 Stk 11,90 EUR"
_TIER_RE = re.compile(
    r"(?:ab|from|aber?\s+|>=|≥)\s*"
    r"(\d{1,3}(?:[.\s]\d{3})*|\d{1,7})\s*"
    r"(stk\.?|st\.?|st(?:ue|ü)ck|pcs\.?|pc\.?|pieces|m|kg|l|set|paar)?\s*"
    r"[:\-=]?\s*"
    r"(?:je|a|à|zu|fuer|für|for)?\s*"
    r"(\d{1,7}(?:[.,]\d{1,4}))\s*"
    r"(eur|euro|€|usd|\$|chf|gbp|£)?",
    re.I,
)

#: Materialnummer-Kandidat (konfigurierbar ueber TableExtractor)
DEFAULT_MATERIAL_PATTERN = r"^(?:\d{6,18}|[A-Za-z0-9]{2,}(?:[-/.][A-Za-z0-9]+)+)$"

#: Klartext der beiden Artikelnummern-Rollen (fuer das Protokoll)
_ROLE_LABEL = {
    "material_number": "unsere Materialnummer",
    "vendor_material_number": "Artikelnummer des Lieferanten",
}

#: Wie viele Zeilen werden je Spalte fuer die Typerkennung betrachtet
_SAMPLE_SIZE = 25

#: Kopfzeile wird nur in den ersten N Zeilen gesucht
_HEADER_SEARCH_ROWS = 12

#: Feldtypen -- steuern das Parsen der Zellwerte
FIELD_KIND: dict[str, str] = {
    "position_number": "text",
    "material_number": "material",
    "vendor_material_number": "text",
    "description": "text",
    "quantity": "decimal",
    "uom": "uom",
    "price": "decimal",
    "price_unit": "int",
    "currency": "currency",
    "min_order_qty": "decimal",
    "lead_time_days": "int",
    "valid_from": "date",
    "remarks": "text",
}

#: Diese Felder zaehlen fuer ``min_fields_per_position``
_COUNTING_FIELDS = ("material_number", "vendor_material_number", "description",
                    "quantity", "uom", "price", "price_unit", "min_order_qty",
                    "valid_from", "lead_time_days")


# --------------------------------------------------------------------------
# Ergebnisstrukturen
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ColumnAssignment:
    """Zuordnung einer Tabellenspalte zu einem Positionsfeld."""

    index: int
    field: str
    header: str = ""
    confidence: float = 0.0
    reason: str = ""
    #: Erzwungene Herkunft.  Wird gesetzt, wenn die Zuordnung nicht aus der
    #: Ueberschrift, sondern aus dem *Inhalt* der Spalte stammt -- solche
    #: Werte sind grundsaetzlich zu pruefen, egal wie klar der Inhalt wirkt.
    forced_origin: FieldOrigin | None = None

    @property
    def origin(self) -> FieldOrigin:
        if self.forced_origin is not None:
            return self.forced_origin
        return FieldOrigin.EXTRACTED if self.confidence >= 0.6 else FieldOrigin.UNCERTAIN


@dataclass
class TableAnalysis:
    """Ergebnis der Strukturanalyse eines Tabellenblocks."""

    block: TableBlock
    header_row_index: int | None = None
    header_texts: list[str] = field(default_factory=list)
    columns: dict[int, ColumnAssignment] = field(default_factory=dict)
    data_start: int = 0
    notes: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def mapped_fields(self) -> set[str]:
        return {c.field for c in self.columns.values() if not c.field.startswith("_")}

    @property
    def usable(self) -> bool:
        """Genug erkannt, um ueberhaupt Positionen zu bilden?"""
        fields_found = self.mapped_fields
        if not fields_found:
            return False
        has_identity = bool(fields_found & {"material_number", "vendor_material_number",
                                            "description"})
        has_value = bool(fields_found & {"price", "quantity"})
        return has_identity and has_value


@dataclass
class ExtractionResult:
    """Positionen plus Protokoll einer Tabellenextraktion."""

    positions: list[OfferPosition] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    analyses: list[TableAnalysis] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.positions)


# --------------------------------------------------------------------------
# Zahl- und Mustererkennung
# --------------------------------------------------------------------------

def parse_number(value: object, style: str = "auto") -> Decimal | None:
    """Zahl parsen, optional mit gelerntem Dezimalstil.

    ``style="de"``  -> Punkt ist Tausender, Komma ist Dezimaltrenner
    ``style="en"``  -> Komma ist Tausender, Punkt ist Dezimaltrenner
    ``style="auto"`` -> Heuristik aus :func:`app.utils.parsing.parse_decimal`
    """
    if style not in ("de", "en") or value is None:
        return parse_decimal(value)
    text = normalize_whitespace(value)
    if not text:
        return None
    if style == "de":
        # Nur eindeutige Faelle umdeuten, sonst der Standardheuristik vertrauen
        if re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+", text):
            return parse_decimal(text.replace(".", ""))
        if re.fullmatch(r"[+-]?\d+,\d+", text):
            # Das Komma ist im deutschen Format *immer* der Dezimaltrenner --
            # auch bei "1,234".  parse_decimal() wuerde hier die Mehrdeutigkeit
            # zugunsten der Tausendertrennung aufloesen; genau das darf das
            # gelernte Format verhindern.
            return _decimal_or_none(text.replace(",", "."))
    else:
        if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+", text):
            return parse_decimal(text.replace(",", ""))
        if re.fullmatch(r"[+-]?\d+\.\d+", text):
            # Analog: im englischen Format ist der Punkt der Dezimaltrenner,
            # "1.234" ist also 1,234 -- und nicht Eintausendzweihundertvier.
            return _decimal_or_none(text)
    return parse_decimal(text)


def _decimal_or_none(text: str) -> Decimal | None:
    """``Decimal`` ohne Heuristik -- der Trenner ist bereits eindeutig."""
    try:
        return Decimal(text)
    except (ArithmeticError, ValueError):
        return None


def find_price_tiers(text: str, style: str = "auto") -> list[tuple[Decimal, Decimal, str]]:
    """Staffelpreise in einem Text finden: ``[(Menge, Preis, Rohtext), ...]``.

    Erkennt "ab 100 Stk 11,90", "ab 500: 10,50 EUR", ">= 1000 9,80".
    Der Preis muss Nachkommastellen haben -- so wird kein Datum ("ab 01.09.2026")
    als Staffel missverstanden.
    """
    tiers: list[tuple[Decimal, Decimal, str]] = []
    if not text:
        return tiers
    for match in _TIER_RE.finditer(text):
        quantity = parse_number(match.group(1), style)
        price = parse_number(match.group(3), style)
        if quantity is None or price is None or quantity <= 0 or price <= 0:
            continue
        tiers.append((quantity, price, match.group(0).strip()))
    return tiers


def is_summary_row(cells: Iterable[str]) -> bool:
    """Erkennt Summen-, Steuer- und Frachtzeilen."""
    values = [normalize_whitespace(c) for c in cells]
    texts = [v for v in values if v]
    if not texts:
        return False
    for value in texts:
        if _SUMMARY_RE.match(value):
            # "Gesamtgewicht 12 kg" ist keine Summenzeile im Geldsinn, wird aber
            # ebenfalls nicht als Position gebraucht -> bewusst mit ausgeschlossen
            return True
    joined = " ".join(texts).lower()
    if "mwst" in joined or "mehrwertsteuer" in joined or "umsatzsteuer" in joined:
        return True
    return False


def _looks_like_date(value: str) -> bool:
    return parse_date(value) is not None


def _looks_like_number(value: str) -> bool:
    return parse_decimal(value) is not None


def _has_decimals(value: str) -> bool:
    return bool(re.search(r"[.,]\d{1,4}\s*(?:eur|usd|chf|€|\$)?$", value.strip(), re.I))


def _looks_like_currency(value: str) -> bool:
    text = normalize_whitespace(value)
    return bool(text) and len(text) <= 5 and bool(detect_currency(text))


def _looks_like_uom(value: str) -> bool:
    text = normalize_whitespace(value).upper().replace(".", "")
    if not text or len(text) > 8 or any(ch.isdigit() for ch in text):
        return False
    return normalize_uom(text) != text or text in ("ST", "M", "KG", "L", "M2", "M3")


# --------------------------------------------------------------------------
# Der Extraktor
# --------------------------------------------------------------------------

class TableExtractor:
    """Baut aus :class:`TableBlock`-Objekten :class:`OfferPosition`-Objekte."""

    def __init__(self, settings: Settings, profile: "VendorProfile | None" = None,
                 material_pattern: str = DEFAULT_MATERIAL_PATTERN) -> None:
        self.settings = settings
        self.profile = profile
        self.aliases = dict(settings.extraction.column_aliases)
        try:
            self.material_re = re.compile(material_pattern)
        except re.error:
            logger.error("Ungueltiges Materialnummernmuster, Standard wird verwendet")
            self.material_re = re.compile(DEFAULT_MATERIAL_PATTERN)
        self.own_material_re = compile_own_pattern(
            settings.extraction.own_material_pattern)
        self.decimal_style = (profile.decimal_style if profile else "auto") or "auto"
        self._skip_patterns: list[re.Pattern[str]] = []
        if profile:
            for pattern in profile.skip_patterns:
                try:
                    self._skip_patterns.append(re.compile(pattern, re.I))
                except re.error:
                    logger.warning("Profil %s: ungueltiges Skip-Muster %r",
                                   profile.profile_id, pattern)

    # ------------------------------------------------------------------
    # Oeffentliche Schnittstelle
    # ------------------------------------------------------------------
    def extract(self, document: RawDocument) -> ExtractionResult:
        """Alle Tabellen eines Dokuments auswerten."""
        result = ExtractionResult()
        row_cells: dict[int, dict[str, str]] = {}
        column_map: dict[str, str] = {}

        for number, block in enumerate(document.tables, start=1):
            normalized = block.normalized()
            if normalized.row_count < 2 or normalized.column_count < 2:
                continue
            try:
                analysis = self.analyze(normalized)
            except Exception as exc:  # noqa: BLE001 -- Tabelle darf nicht killen
                result.notes.append(f"Tabelle {number} konnte nicht analysiert werden: {exc}")
                logger.warning("Tabellenanalyse fehlgeschlagen: %s", exc, exc_info=True)
                continue

            result.analyses.append(analysis)
            label = self._block_label(normalized, number)
            if not analysis.usable:
                result.notes.append(
                    f"{label}: keine verwertbare Positionsstruktur erkannt "
                    f"(gefundene Felder: {', '.join(sorted(analysis.mapped_fields)) or 'keine'})."
                )
                continue

            result.notes.extend(f"{label}: {note}" for note in analysis.notes)
            positions = self._positions_from_analysis(analysis, document, label,
                                                      row_cells, result.notes)
            result.positions.extend(positions)
            for assignment in analysis.columns.values():
                if assignment.header:
                    column_map[assignment.header] = assignment.field

        if row_cells:
            document.meta.setdefault("row_cells", {}).update(row_cells)
        if column_map:
            document.meta.setdefault("column_map", {}).update(column_map)

        logger.info("Tabellenextraktion: %d Positionen aus %d Tabellen (%s)",
                    len(result.positions), len(document.tables), document.display_name)
        return result

    # ------------------------------------------------------------------
    # Strukturanalyse
    # ------------------------------------------------------------------
    def analyze(self, block: TableBlock) -> TableAnalysis:
        """Kopfzeile finden und Spalten zuordnen."""
        analysis = TableAnalysis(block=block)
        header_index, header_texts, merged, score = self._find_header(block)

        if header_index is None:
            analysis.data_start = 0
            analysis.notes.append(
                "keine Kopfzeile erkannt -- Spalten werden ueber die Datentypen bestimmt")
        else:
            analysis.header_row_index = header_index
            analysis.header_texts = header_texts
            analysis.data_start = header_index + (2 if merged else 1)
            analysis.score = score
            if merged:
                analysis.notes.append(
                    f"mehrzeilige Kopfzeile (Zeilen {header_index + 1}/{header_index + 2}) "
                    "zusammengefuehrt")

        self._assign_columns(analysis)
        self._resolve_material_columns(analysis)
        return analysis

    # ------------------------------------------------------------------
    # Artikelnummern: welche Spalte ist UNSERE Materialnummer?
    # ------------------------------------------------------------------
    def _resolve_material_columns(self, analysis: TableAnalysis) -> None:
        """Rollen der beiden Artikelnummern-Spalten ueber den Inhalt klaeren.

        Die Ueberschrift allein genuegt nicht: "Artikelnummer" kann unsere
        Nummer oder die des Lieferanten sein.  Deshalb wird zusaetzlich
        gezaehlt, welcher Anteil der Werte auf den eigenen Nummernkreis passt
        (:attr:`ExtractionSettings.own_material_pattern`).

        Entschieden wird nur, was sich belegen laesst, und jede Entscheidung
        landet als Notiz im Protokoll.  Umsortierte Spalten sind immer
        ``UNCERTAIN`` -- der Anwender bekommt sie gelb angezeigt.
        """
        extraction = self.settings.extraction
        if not extraction.own_material_pattern_active:
            return
        candidates = {index: assignment for index, assignment in analysis.columns.items()
                      if assignment.field in MATERIAL_FIELDS}
        if not candidates:
            return

        rows = [r for r in analysis.block.rows[analysis.data_start:]
                if not is_summary_row(r)][:_SAMPLE_SIZE]
        if not rows:
            return

        learned = self._learned_roles()
        minimum = max(0.0, min(1.0, float(extraction.own_material_min_ratio or 0.7)))
        ratios: dict[int, float] = {}
        for index in candidates:
            values = [r[index] if index < len(r) else "" for r in rows]
            ratios[index] = own_ratio(values, self.own_material_re)

        used_fields = {a.field for a in analysis.columns.values()}

        # 1. Gelernte Rollen: der Anwender hat es einmal korrigiert -- fertig.
        for index, assignment in list(candidates.items()):
            role = learned.get(_normalize_header(assignment.header))
            if role and role != assignment.field:
                self._retag(analysis, index, role, None,
                            f"Spalte '{assignment.header}' gilt laut Lieferantenprofil "
                            f"als '{_ROLE_LABEL[role]}' (vom Anwender bestaetigt).")
        if learned:
            candidates = {index: assignment
                          for index, assignment in analysis.columns.items()
                          if assignment.field in MATERIAL_FIELDS}
            used_fields = {a.field for a in analysis.columns.values()}
            if not candidates:
                return

        # 2. Zwei (oder mehr) Kandidaten: die Marker in der Ueberschrift
        #    ("Ihre .../Kunden..." gegen "Unsere .../Hersteller...") entscheiden.
        if len(candidates) >= 2:
            for index, assignment in list(candidates.items()):
                if _normalize_header(assignment.header) in learned:
                    continue
                role = header_role(assignment.header)
                if not role or role == assignment.field:
                    continue
                self._retag(analysis, index, role, None,
                            f"Spalte '{assignment.header}' wird als "
                            f"'{_ROLE_LABEL[role]}' gefuehrt -- die Ueberschrift "
                            "nennt ausdruecklich, aus wessen Sicht die Nummer ist.")
            return

        # 3. Genau ein Kandidat, als Lieferantennummer gefuehrt, Inhalt sieht
        #    aber nach unserer Nummer aus -> umtragen, aber unsicher.
        index, assignment = next(iter(candidates.items()))
        if (assignment.field == "vendor_material_number"
                and "material_number" not in used_fields
                and ratios.get(index, 0.0) >= minimum
                and _normalize_header(assignment.header) not in learned
                and not header_role(assignment.header)):
            ratio = ratios[index]
            self._retag(
                analysis, index, "material_number", FieldOrigin.UNCERTAIN,
                f"Spalte '{assignment.header or f'Nr. {index + 1}'}' sieht nach "
                f"unserer Materialnummer aus ({ratio:.0%} der Werte passen auf das "
                f"Muster {extraction.own_material_pattern}) und wird als solche "
                "uebernommen -- bitte pruefen.")

    def _retag(self, analysis: TableAnalysis, index: int, field_name: str,
               forced_origin: FieldOrigin | None, note: str) -> None:
        """Eine Spalte einem anderen Feld zuordnen und das protokollieren."""
        assignment = analysis.columns[index]
        analysis.columns[index] = ColumnAssignment(
            index=index, field=field_name, header=assignment.header,
            confidence=assignment.confidence,
            reason="Inhalt der Spalte (Nummernkreis)",
            forced_origin=forced_origin)
        analysis.notes.append(note)
        logger.info("Spalte %d ('%s') -> %s: %s", index + 1, assignment.header,
                    field_name, note)

    def _learned_roles(self) -> dict[str, str]:
        """Vom Anwender bestaetigte Spaltenrollen aus dem Lieferantenprofil."""
        if self.profile is None:
            return {}
        roles = {header: field_name
                 for header, field_name in getattr(
                     self.profile, "material_column_role", {}).items()
                 if field_name in MATERIAL_FIELDS}
        for header, field_name in self.profile.column_map.items():
            if field_name in MATERIAL_FIELDS:
                roles.setdefault(header, field_name)
        return roles

    def _find_header(self, block: TableBlock) -> tuple[int | None, list[str], bool, float]:
        """Beste Kopfzeile suchen; liefert (Index, Texte, verschmolzen, Score)."""
        best_index: int | None = None
        best_texts: list[str] = []
        best_merged = False
        best_score = 0.0

        limit = min(_HEADER_SEARCH_ROWS, max(block.row_count - 1, 1))
        for index in range(limit):
            row = block.rows[index]
            single_score, _ = self._score_header_row(row)
            candidate_score, candidate_texts, merged = single_score, list(row), False

            if index + 1 < block.row_count:
                combined = [normalize_whitespace(f"{a} {b}")
                            for a, b in zip(row, block.rows[index + 1])]
                if len(block.rows[index + 1]) > len(row):
                    combined += block.rows[index + 1][len(row):]
                merged_score, _ = self._score_header_row(combined)
                # Verschmolzen wird in zwei Faellen:
                #   a) es wird deutlich besser ("Preis" + "EUR/Stueck")
                #   b) die Folgezeile ist erkennbar eine Kopfzeilen-Fortsetzung
                #      ("Netto"/"Preis") und keine Datenzeile
                if merged_score > single_score + 0.4:
                    candidate_score, candidate_texts, merged = merged_score, combined, True
                elif (self._is_header_continuation(block.rows[index + 1])
                        and merged_score >= single_score - 0.5):
                    candidate_score = max(single_score, merged_score)
                    candidate_texts, merged = combined, True

            if candidate_score > best_score:
                best_index, best_texts = index, candidate_texts
                best_merged, best_score = merged, candidate_score

        if best_score < 1.6:      # entspricht ~2 sicher erkannten Spalten
            return None, [], False, best_score
        return best_index, best_texts, best_merged, best_score

    def _is_header_continuation(self, row: list[str]) -> bool:
        """Ist diese Zeile die zweite Haelfte einer mehrzeiligen Kopfzeile?

        Kennzeichen einer Fortsetzung: sie enthaelt ausschliesslich Text (keine
        Zahl, kein Datum -- sonst waere es eine Datenzeile) und mindestens zwei
        Zellen, die selbst wie Spaltenbeschriftungen aussehen.
        """
        texts = [normalize_whitespace(cell) for cell in row]
        filled = [t for t in texts if t]
        if len(filled) < 2:
            return False
        for value in filled:
            if _looks_like_number(value) or _looks_like_date(value):
                return False
        score, hits = self._score_header_row(texts)
        return len(hits) >= 2 and score >= 1.2

    def _score_header_row(self, row: list[str]) -> tuple[float, dict[int, tuple[str, float, str]]]:
        """Wie gut passt eine Zeile als Kopfzeile?"""
        score = 0.0
        hits: dict[int, tuple[str, float, str]] = {}
        numeric = 0
        filled = 0
        for index, cell in enumerate(row):
            text = normalize_whitespace(cell)
            if not text:
                continue
            filled += 1
            if _looks_like_number(text) and not re.search(r"[A-Za-zÄÖÜäöü]", text):
                numeric += 1
                continue
            match_field, confidence, reason = self._match_header_text(text)
            if match_field and confidence >= 0.7:
                hits[index] = (match_field, confidence, reason)
                score += confidence
        if filled and numeric / filled > 0.5:
            score *= 0.3      # ueberwiegend Zahlen -> das ist eine Datenzeile
        return score, hits

    # ------------------------------------------------------------------
    def _match_header_text(self, text: str) -> tuple[str, float, str]:
        """Ueberschrift -> (Feld, Konfidenz, Begruendung)."""
        header = _normalize_header(text)
        if not header:
            return "", 0.0, ""

        # 1. Gelerntes Profil hat Vorrang
        if self.profile is not None:
            learned = self.profile.column_map.get(header)
            if learned:
                return learned, 0.98, f"Profil '{self.profile.vendor_name or self.profile.vendor_key}'"

        best_field, best_confidence, best_reason = "", 0.0, ""
        catalogue: list[tuple[str, list[str]]] = list(PSEUDO_ALIASES.items())
        catalogue += [(f, a) for f, a in self.aliases.items()]

        for field_name, alias_list in catalogue:
            for alias in alias_list:
                alias_norm = _normalize_header(alias)
                if not alias_norm:
                    continue
                confidence, reason = _alias_confidence(header, alias_norm)
                if confidence > best_confidence:
                    best_field, best_confidence, best_reason = field_name, confidence, reason
        if best_confidence >= 0.55:
            return best_field, best_confidence, best_reason
        return "", 0.0, ""

    # ------------------------------------------------------------------
    def _assign_columns(self, analysis: TableAnalysis) -> None:
        """Spalten zuordnen: erst Ueberschriften, dann Datentypen."""
        block = analysis.block
        width = block.column_count
        headers = analysis.header_texts or [""] * width
        headers = headers + [""] * (width - len(headers))

        candidates: dict[int, tuple[str, float, str]] = {}
        for index in range(width):
            text = normalize_whitespace(headers[index]) if index < len(headers) else ""
            if not text:
                continue
            field_name, confidence, reason = self._match_header_text(text)
            if field_name:
                candidates[index] = (field_name, confidence, reason)

        assigned: dict[int, ColumnAssignment] = {}
        used: dict[str, int] = {}
        # Nach Konfidenz absteigend zuweisen, damit die beste Spalte gewinnt
        for index, (field_name, confidence, reason) in sorted(
                candidates.items(), key=lambda kv: -kv[1][1]):
            header_text = normalize_whitespace(headers[index])
            if field_name.startswith("_"):
                assigned[index] = ColumnAssignment(index, field_name, header_text,
                                                   confidence, reason)
                continue
            if field_name in used:
                resolved, note = self._resolve_conflict(field_name, used[field_name],
                                                        index, headers, assigned)
                analysis.notes.append(note)
                if resolved == index:
                    # Neue Spalte gewinnt, die alte wird zur Alternative
                    old = used[field_name]
                    assigned[old] = ColumnAssignment(old, PSEUDO_ALT_PRICE,
                                                     normalize_whitespace(headers[old]),
                                                     confidence, "Mehrdeutigkeit")
                    assigned[index] = ColumnAssignment(index, field_name, header_text,
                                                       confidence, reason)
                    used[field_name] = index
                else:
                    assigned[index] = ColumnAssignment(index, PSEUDO_ALT_PRICE,
                                                       header_text, confidence,
                                                       "Mehrdeutigkeit")
                continue
            assigned[index] = ColumnAssignment(index, field_name, header_text,
                                               confidence, reason)
            used[field_name] = index

        self._assign_by_type(analysis, assigned, used, headers)
        analysis.columns = dict(sorted(assigned.items()))

    def _resolve_conflict(self, field_name: str, old_index: int, new_index: int,
                          headers: list[str],
                          assigned: dict[int, ColumnAssignment]) -> tuple[int, str]:
        """Zwei Spalten wollen dasselbe Feld -- nachvollziehbar entscheiden."""
        old_header = _normalize_header(headers[old_index]) if old_index < len(headers) else ""
        new_header = _normalize_header(headers[new_index]) if new_index < len(headers) else ""
        old_rank = _price_rank(old_header)
        new_rank = _price_rank(new_header)

        # Eine gelernte Zuordnung schlaegt jede Heuristik -- der Anwender hat
        # sie fuer genau diesen Lieferanten bestaetigt.
        old_learned = assigned.get(old_index) is not None and \
            assigned[old_index].reason.startswith("Profil")
        if old_learned:
            note = (f"Mehrdeutige Spalten fuer '{field_name}': '{old_header}' bleibt "
                    f"gesetzt (gelernte Profilregel), '{new_header}' wird nur als "
                    "Bemerkung gefuehrt.")
            logger.info(note)
            return old_index, note

        if new_rank > old_rank:
            winner, loser = new_index, old_index
            winner_header, loser_header = new_header, old_header
        else:
            winner, loser = old_index, new_index
            winner_header, loser_header = old_header, new_header
        note = (f"Mehrdeutige Spalten fuer '{field_name}': '{winner_header}' wird "
                f"verwendet, '{loser_header}' wird nur als Bemerkung gefuehrt.")
        logger.info(note)
        return winner, note

    # ------------------------------------------------------------------
    def _assign_by_type(self, analysis: TableAnalysis,
                        assigned: dict[int, ColumnAssignment],
                        used: dict[str, int], headers: list[str]) -> None:
        """Nicht zugeordnete Spalten ueber ihre Datentypen bestimmen."""
        block = analysis.block
        width = block.column_count
        rows = block.rows[analysis.data_start:]
        rows = [r for r in rows if not is_summary_row(r)][:_SAMPLE_SIZE]
        if not rows:
            return

        profiles: dict[int, dict[str, float]] = {}
        for index in range(width):
            if index in assigned:
                continue
            values = [normalize_whitespace(r[index]) if index < len(r) else ""
                      for r in rows]
            values = [v for v in values if v]
            if not values:
                continue
            profiles[index] = _column_profile(values, self.material_re)

        # Alle Vorschlaege sammeln und nach Konfidenz vergeben -- so gewinnt die
        # eindeutigste Spalte, unabhaengig von der Spaltenreihenfolge.
        order = ("valid_from", "currency", "uom", "material_number", "price",
                 "quantity", "position_number", "price_unit", "min_order_qty",
                 "lead_time_days", "description")
        rank = {name: index for index, name in enumerate(order)}
        candidates: list[tuple[float, int, int, str]] = []
        for index, stats in profiles.items():
            for field_name, confidence in stats.items():
                if confidence >= 0.55 and field_name not in used:
                    candidates.append((confidence, -rank.get(field_name, 99),
                                       -index, field_name))
        for confidence, _rank, negative_index, field_name in sorted(candidates,
                                                                    reverse=True):
            index = -negative_index
            if index in assigned or field_name in used:
                continue
            header_text = (normalize_whitespace(headers[index])
                           if index < len(headers) else "")
            assigned[index] = ColumnAssignment(
                index, field_name, header_text,
                min(confidence, 0.58) if not header_text else confidence,
                "Datentyp der Spalte")
            used[field_name] = index
            analysis.notes.append(
                f"Spalte {index + 1} ohne passende Ueberschrift wurde ueber "
                f"den Datentyp als '{field_name}' erkannt (Anteil "
                f"{confidence:.0%}) -- bitte pruefen")

    # ------------------------------------------------------------------
    # Positionen bilden
    # ------------------------------------------------------------------
    def _positions_from_analysis(self, analysis: TableAnalysis, document: RawDocument,
                                 label: str, row_cells: dict[int, dict[str, str]],
                                 notes: list[str]) -> list[OfferPosition]:
        positions: list[OfferPosition] = []
        block = analysis.block
        minimum = max(1, self.settings.extraction.min_fields_per_position)
        skipped_rows = 0

        for row_index in range(analysis.data_start, block.row_count):
            row = block.rows[row_index]
            raw_line = " | ".join(cell for cell in row if cell).strip()
            if not raw_line:
                continue
            if self._is_skipped(raw_line):
                skipped_rows += 1
                continue
            if is_summary_row(row):
                notes.append(f"{label}: Zeile {row_index + 1} als Summenzeile "
                             f"uebersprungen ('{raw_line[:50]}')")
                continue
            if analysis.header_row_index is not None and self._is_repeated_header(row, analysis):
                continue

            values, present = self._row_values(row, analysis)
            if len(present) < minimum:
                if positions and self._is_continuation(values, present):
                    self._append_continuation(positions[-1], row, analysis)
                    continue
                skipped_rows += 1
                continue

            position = self._make_position(values, analysis, document, label,
                                           row_index, raw_line)
            positions.append(position)
            row_cells[position.uid] = self._row_cell_map(row, analysis)

            for tier in self._tier_positions(position, row, analysis, document, label,
                                            row_index):
                positions.append(tier)

        # Zeilen, die aussehen wie Staffelpreise der Vorposition
        positions = self._link_tier_rows(positions, notes, label)

        if skipped_rows:
            notes.append(f"{label}: {skipped_rows} Zeile(n) ohne ausreichende "
                         f"Feldbelegung verworfen (Mindestanzahl {minimum})")
        return positions

    def _is_skipped(self, raw_line: str) -> bool:
        return any(pattern.search(raw_line) for pattern in self._skip_patterns)

    def _is_repeated_header(self, row: list[str], analysis: TableAnalysis) -> bool:
        """Wiederholte Kopfzeile (Seitenumbruch im PDF) erkennen."""
        if not analysis.header_texts:
            return False
        same = sum(1 for a, b in zip(row, analysis.header_texts)
                   if a and _normalize_header(a) == _normalize_header(b))
        return same >= 2

    def _row_values(self, row: list[str],
                    analysis: TableAnalysis) -> tuple[dict[str, Any], list[str]]:
        """Zellwerte einer Zeile in Felder wandeln."""
        values: dict[str, Any] = {}
        present: list[str] = []
        for index, assignment in analysis.columns.items():
            if index >= len(row):
                continue
            raw = normalize_whitespace(row[index])
            if not raw:
                continue
            field_name = assignment.field
            if field_name == PSEUDO_IGNORE or field_name == PSEUDO_TOTAL:
                continue
            if field_name == PSEUDO_ALT_PRICE:
                values.setdefault("remarks_extra", []).append(
                    f"{assignment.header or 'weitere Preisspalte'}: {raw}")
                continue
            parsed = self._parse_cell(field_name, raw)
            if parsed in (None, "") and FIELD_KIND.get(field_name) == "decimal":
                # "100 Stk" / "12,85 EUR/St": Zahl und Einheit stehen in einer Zelle
                parsed, unit = self._split_number_unit(raw)
                if parsed is not None and unit:
                    values.setdefault("_extra_uom", normalize_uom(unit))
            if parsed in (None, ""):
                continue
            if field_name == "price":
                currency = detect_currency(raw)
                if currency:
                    values.setdefault("_extra_currency", currency)
            values[field_name] = parsed
            values.setdefault("_raw", {})[field_name] = raw
            if field_name in _COUNTING_FIELDS:
                present.append(field_name)

        # Aus einer Zelle mitgelesene Einheit/Waehrung nur ergaenzend uebernehmen
        if values.get("_extra_uom") and "uom" not in values:
            values["uom"] = values["_extra_uom"]
        if values.get("_extra_currency") and "currency" not in values:
            values["currency"] = values["_extra_currency"]
        return values, present

    @staticmethod
    def _split_number_unit(raw: str) -> tuple[Decimal | None, str]:
        """"100 Stk" -> (100, "Stk").  Ohne klare Zahl: ``(None, "")``."""
        match = re.match(r"^\s*([+-]?[\d.,\s']+)\s*([A-Za-zÄÖÜäöüß./]{1,10})?\s*$", raw)
        if not match:
            return None, ""
        number = parse_decimal(match.group(1))
        if number is None:
            return None, ""
        unit = (match.group(2) or "").strip(" ./")
        if unit and detect_currency(unit):
            return number, ""       # Waehrung ist keine Mengeneinheit
        return number, unit

    def _parse_cell(self, field_name: str, raw: str) -> Any:
        kind = FIELD_KIND.get(field_name, "text")
        if kind == "decimal":
            return parse_number(raw, self.decimal_style)
        if kind == "int":
            value = parse_int(raw)
            return value if value is not None and value >= 0 else None
        if kind == "date":
            return parse_date(raw)
        if kind == "currency":
            return detect_currency(raw)
        if kind == "uom":
            return normalize_uom(raw)
        if kind == "material":
            return normalize_material_number(raw)
        return normalize_whitespace(raw)

    def _make_position(self, values: dict[str, Any], analysis: TableAnalysis,
                       document: RawDocument, label: str, row_index: int,
                       raw_line: str) -> OfferPosition:
        position = OfferPosition()
        position.source_kind = document.source_kind
        position.source_hint = f"{label}, Zeile {row_index + 1}"
        position.raw_text = raw_line

        for index, assignment in analysis.columns.items():
            field_name = assignment.field
            if field_name.startswith("_") or field_name not in values:
                continue
            position.set_field(field_name, values[field_name], assignment.origin)

        # Werte, die aus einer gemeinsamen Zelle stammen ("100 Stk", "12,85 EUR")
        if values.get("_extra_uom") and not position.uom:
            position.set_field("uom", values["_extra_uom"], FieldOrigin.EXTRACTED)
        if values.get("_extra_currency") and not position.currency:
            position.set_field("currency", values["_extra_currency"],
                               FieldOrigin.EXTRACTED)

        extra = values.get("remarks_extra")
        if extra:
            merged = "; ".join(extra)
            existing = position.remarks
            position.set_field("remarks",
                               f"{existing}; {merged}" if existing else merged,
                               FieldOrigin.EXTRACTED)
        return position

    def _row_cell_map(self, row: list[str], analysis: TableAnalysis) -> dict[str, str]:
        """Rohzellen mit ihrer Ueberschrift -- Grundlage fuer das Lernen."""
        mapping: dict[str, str] = {}
        for index in range(len(row)):
            header = ""
            if index < len(analysis.header_texts):
                header = _normalize_header(analysis.header_texts[index])
            key = header or f"spalte_{index + 1}"
            mapping[key] = normalize_whitespace(row[index])
        return mapping

    # -- Fortsetzungszeilen ---------------------------------------------
    @staticmethod
    def _is_continuation(values: dict[str, Any], present: list[str]) -> bool:
        """Zeile enthaelt nur noch Beschreibungstext -> gehoert zur Vorposition."""
        if any(f in present for f in ("price", "quantity", "material_number",
                                      "vendor_material_number")):
            return False
        return bool(values.get("description") or values.get("remarks"))

    @staticmethod
    def _append_continuation(position: OfferPosition, row: list[str],
                             analysis: TableAnalysis) -> None:
        text_parts: list[str] = []
        for index, assignment in analysis.columns.items():
            if assignment.field in ("description", "remarks") and index < len(row):
                value = normalize_whitespace(row[index])
                if value:
                    text_parts.append(value)
        if not text_parts:
            return
        addition = " ".join(text_parts)
        merged = f"{position.description} {addition}".strip()
        position.set_field("description", merged, position.origin("description"))
        position.raw_text = f"{position.raw_text} / {addition}"

    # -- Staffelpreise ---------------------------------------------------
    def _tier_positions(self, base: OfferPosition, row: list[str],
                        analysis: TableAnalysis, document: RawDocument,
                        label: str, row_index: int) -> list[OfferPosition]:
        """Staffelpreise aus Freitextzellen als eigene Positionen ausweisen."""
        text_sources: list[str] = []
        for index, assignment in analysis.columns.items():
            if assignment.field in ("description", "remarks") and index < len(row):
                text_sources.append(row[index])
        tiers = find_price_tiers(" ; ".join(t for t in text_sources if t),
                                 self.decimal_style)
        out: list[OfferPosition] = []
        for quantity, price, raw in tiers:
            if base.price is not None and price == base.price:
                continue
            tier = _copy_position(base)
            tier.source_kind = document.source_kind
            tier.source_hint = f"{label}, Zeile {row_index + 1} (Staffel)"
            tier.raw_text = raw
            tier.set_field("quantity", quantity, FieldOrigin.UNCERTAIN)
            tier.set_field("price", price, FieldOrigin.UNCERTAIN)
            tier.set_field("min_order_qty", quantity, FieldOrigin.UNCERTAIN)
            tier.set_field("remarks",
                           _join_remark(base.remarks, f"Staffelpreis: {raw}"),
                           FieldOrigin.EXTRACTED)
            out.append(tier)
        if out:
            logger.debug("%d Staffelpreise zu Position %s erkannt", len(out),
                         base.display_name)
        return out

    @staticmethod
    def _link_tier_rows(positions: list[OfferPosition], notes: list[str],
                        label: str) -> list[OfferPosition]:
        """Zeilen ohne Artikel, aber mit Menge und Preis, sind Staffelzeilen."""
        result: list[OfferPosition] = []
        for position in positions:
            headless = (not position.material_number
                        and not position.vendor_material_number
                        and not position.description)
            if headless and position.price is not None and result:
                previous = result[-1]
                position.set_field("material_number", previous.material_number,
                                   FieldOrigin.MAPPED)
                position.set_field("vendor_material_number",
                                   previous.vendor_material_number, FieldOrigin.MAPPED)
                position.set_field("description", previous.description,
                                   FieldOrigin.MAPPED)
                position.set_field("remarks",
                                   _join_remark(position.remarks,
                                                "Staffelpreis zur vorherigen Position"),
                                   FieldOrigin.EXTRACTED)
                notes.append(f"{label}: Zeile '{position.raw_text[:40]}' als Staffelpreis "
                             f"zu '{previous.display_name}' uebernommen")
            result.append(position)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _block_label(block: TableBlock, number: int) -> str:
        if block.title:
            return f"Tabelle '{block.title}'"
        if block.page:
            return f"Tabelle auf Seite {block.page}"
        return f"Tabelle {number}"


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _normalize_header(text: object) -> str:
    """Ueberschrift vereinheitlichen: klein, ohne Sonderzeichen und Einheiten."""
    value = normalize_whitespace(text).lower()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    value = value.replace("ß", "ss")
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)     # "(netto)" entfernen
    value = re.sub(r"[*:#°]+", " ", value)
    value = re.sub(r"[^a-z0-9/. \-]", " ", value)
    return normalize_whitespace(value).strip(" .-/")


def _alias_confidence(header: str, alias: str) -> tuple[float, str]:
    """Wie gut passt eine Ueberschrift zu einem Alias?"""
    if not header or not alias:
        return 0.0, ""
    if header == alias:
        return 1.0, f"Ueberschrift '{alias}'"
    # Kurze Aliase ("me", "pe", "ep") nur als ganzes Wort akzeptieren
    if len(alias) <= 3:
        if re.search(rf"(?:^|[ /\-]){re.escape(alias)}(?:$|[ /\-])", header):
            return 0.9, f"Kuerzel '{alias}'"
        return 0.0, ""
    if alias in header:
        ratio = len(alias) / max(len(header), 1)
        return 0.6 + 0.3 * ratio, f"enthaelt '{alias}'"
    if header in alias and len(header) >= 4:
        ratio = len(header) / len(alias)
        return 0.55 + 0.3 * ratio, f"Teil von '{alias}'"
    score = similarity(header, alias)
    if score >= 0.82:
        return min(0.8, 0.45 + 0.4 * score), f"aehnlich zu '{alias}' ({score:.2f})"
    return 0.0, ""


#: Je hoeher, desto lieber wird diese Preisspalte genommen
_PRICE_PREFERENCE = (
    (r"\bnetto", 5), (r"\bnet\b", 5), (r"einzelpreis", 4), (r"unit price", 4),
    (r"stueckpreis|stckpreis", 4), (r"ihr preis|your price", 4), (r"^preis", 3),
    (r"preis", 2), (r"price", 2), (r"listen", -3), (r"list price", -3),
    (r"brutto", -4), (r"uvp|rrp", -4), (r"alt(?:er)? preis|old price", -5),
)


def _price_rank(header: str) -> int:
    """Bewertet, wie sehr eine Ueberschrift nach dem *massgeblichen* Preis klingt."""
    rank = 0
    for pattern, weight in _PRICE_PREFERENCE:
        if re.search(pattern, header):
            rank += weight
    return rank


def _column_profile(values: list[str], material_re: re.Pattern[str]) -> dict[str, float]:
    """Anteile der Datentypen einer Spalte -> moegliche Felder mit Konfidenz."""
    total = len(values)
    if not total:
        return {}
    dates = sum(1 for v in values if _looks_like_date(v))
    numbers = sum(1 for v in values if _looks_like_number(v))
    decimals = sum(1 for v in values if _looks_like_number(v) and _has_decimals(v))
    currencies = sum(1 for v in values if _looks_like_currency(v))
    uoms = sum(1 for v in values if _looks_like_uom(v))
    materials = sum(1 for v in values if material_re.match(v.replace(" ", "")))
    texts = sum(1 for v in values if len(v) >= 10 and re.search(r"[A-Za-zÄÖÜäöü]{3}", v))
    integers = sum(1 for v in values
                   if re.fullmatch(r"\d{1,4}", v.replace(".", "").replace(" ", "")))

    profile: dict[str, float] = {}
    if dates / total >= 0.8:
        profile["valid_from"] = dates / total
        return profile          # ein Datum ist ein Datum -- nichts anderes
    if currencies / total >= 0.8 and numbers / total < 0.5:
        profile["currency"] = currencies / total
        return profile
    if uoms / total >= 0.8 and numbers / total < 0.5:
        profile["uom"] = uoms / total
        return profile
    if decimals / total >= 0.6:
        profile["price"] = decimals / total
    if numbers / total >= 0.8 and decimals / total < 0.6:
        profile["quantity"] = 0.6 * (numbers / total)
        if integers / total >= 0.8:
            profile["position_number"] = 0.55 * (integers / total)
    if materials / total >= 0.6 and decimals / total < 0.5:
        profile["material_number"] = 0.6 + 0.3 * (materials / total)
    if texts / total >= 0.6:
        profile["description"] = 0.6 + 0.2 * (texts / total)
    return profile


def _copy_position(source: OfferPosition) -> OfferPosition:
    """Flache Kopie einer Position (neue uid, gleiche Angebotsdaten)."""
    clone = OfferPosition()
    for name in ("position_number", "material_number", "vendor_material_number",
                 "description", "quantity", "uom", "price", "price_unit", "currency",
                 "min_order_qty", "lead_time_days", "valid_from", "remarks"):
        setattr(clone, name, getattr(source, name))
    clone.field_origins = dict(source.field_origins)
    clone.source_kind = source.source_kind
    clone.source_hint = source.source_hint
    clone.raw_text = source.raw_text
    return clone


def _join_remark(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}; {addition}"
