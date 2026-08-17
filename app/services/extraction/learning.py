"""Live-Anlernen aus Anwenderkorrekturen.

Wenn der Einkaeufer eine erkannte Position korrigiert, steckt darin eine
Information, die kein Regelwerk vorwegnehmen kann: *bei diesem Lieferanten
steht der Preis in der Spalte "Netto EUR"*.  Genau das -- und nur das -- wird
hier gelernt.

Drei eiserne Regeln:

1. **Gelernt wird, WO ein Wert steht, nie WELCHER Wert es ist.**  In einem
   Profil landen Spaltenueberschriften, Regex-Muster und Ueberspringregeln --
   niemals Preise, Mengen oder Datumsangaben.
2. **Erst ab N Bestaetigungen.**  Eine einzelne Korrektur kann ein Versehen
   sein.  Spaltenzuordnungen greifen ab einer Bestaetigung, abgeleitete
   Kopfregeln erst ab zwei.
3. **Immer rueckbaubar.**  Jede Regel hat einen Zaehler und kann einzeln
   (``forget_rule``) oder komplett (``VendorProfile.reset_learning``) entfernt
   werden.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ...models.offer import Offer
from ...models.offer_position import TRACKED_FIELDS, OfferPosition
from ...utils.parsing import (
    normalize_whitespace,
    parse_date,
    parse_decimal,
)
from ..readers.base import RawDocument
from .profiles import VendorProfile, fingerprint, new_profile, vendor_key_for
from .table_extractor import parse_number

logger = logging.getLogger(__name__)

__all__ = [
    "LearningConfig",
    "LearningReport",
    "learn_from_corrections",
    "forget_rule",
    "describe_learning",
]

#: Felder, deren Spaltenzuordnung gelernt werden darf
_LEARNABLE_POSITION_FIELDS = tuple(f for f in TRACKED_FIELDS if f != "position_number")

#: Kopffelder, fuer die eine Regel abgeleitet werden darf
_LEARNABLE_HEADER_FIELDS = (
    "vendor_name", "vendor_number", "offer_number", "offer_date", "currency",
    "valid_from", "valid_to", "incoterm", "payment_terms", "contact",
)

#: Platzhalter je Feldtyp -- so enthaelt eine gelernte Regel nie einen Wert
_VALUE_PATTERNS: dict[str, str] = {
    "date": (r"(\d{1,2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{2,4}|\d{4}-\d{1,2}-\d{1,2}"
             r"|\d{1,2}\.?\s*[A-Za-zÄÖÜäöüß]{3,10}\.?\s+\d{4})"),
    "code": r"([A-Za-z0-9][A-Za-z0-9._\-/]{2,29})",
    "line": r"([^\n\r]{2,120})",
}

_HEADER_FIELD_KIND = {
    "offer_date": "date", "valid_from": "date", "valid_to": "date",
    "payment_terms": "line", "contact": "line", "vendor_name": "line",
}


@dataclass(slots=True)
class LearningConfig:
    """Wie viele Bestaetigungen braucht eine Regel, bis sie greift?"""

    column_map_confirmations: int = 1
    regex_confirmations: int = 2
    skip_confirmations: int = 1
    decimal_confirmations: int = 1
    #: Wie viele Skip-Muster darf ein Profil hoechstens sammeln
    max_skip_patterns: int = 40


@dataclass
class LearningReport:
    """Was wurde bei diesem Lernvorgang beobachtet und uebernommen?"""

    profile: VendorProfile
    activated: list[str]
    pending: list[str]
    observations: list[str]

    def summary(self) -> str:
        if not self.activated and not self.pending:
            return f"Profil '{self.profile.label}': nichts Neues zu lernen."
        parts = []
        if self.activated:
            parts.append(f"{len(self.activated)} Regel(n) uebernommen")
        if self.pending:
            parts.append(f"{len(self.pending)} Vorschlag/Vorschlaege vorgemerkt")
        return f"Profil '{self.profile.label}': " + ", ".join(parts)


# --------------------------------------------------------------------------
# Hauptfunktion
# --------------------------------------------------------------------------

def learn_from_corrections(original: Offer, corrected: Offer, document: RawDocument,
                           profile: VendorProfile | None = None,
                           config: LearningConfig | None = None) -> VendorProfile:
    """Aus dem Unterschied zweier Angebote Erkennungsregeln ableiten.

    ``original``  wie die Erkennung es geliefert hat
    ``corrected`` wie der Anwender es korrigiert hat
    ``document``  das zugehoerige Rohdokument (liefert die Zellbezuege)
    ``profile``   bestehendes Profil; ohne Angabe wird eines angelegt
    """
    config = config or LearningConfig()
    profile = profile or new_profile(corrected, document)
    report = LearningReport(profile=profile, activated=[], pending=[], observations=[])

    profile.sample_count += 1
    profile.add_fingerprint(fingerprint(document))
    if document.email is not None and document.email.sender_domain:
        profile.add_domain(document.email.sender_domain)
    if corrected.vendor_name and not profile.vendor_name:
        profile.vendor_name = corrected.vendor_name
    if not profile.vendor_key:
        profile.vendor_key = vendor_key_for(corrected, document)

    changed = False
    changed |= _learn_columns(original, corrected, document, profile, config, report)
    changed |= _learn_header_rules(original, corrected, document, profile, config, report)
    changed |= _learn_skips(original, corrected, profile, config, report)
    changed |= _learn_decimal_style(original, corrected, document, profile, config, report)
    _learn_defaults(corrected, profile, report)

    if changed:
        profile.correction_count += 1
    else:
        profile.success_count += 1
    profile.touch()

    logger.info("Lernvorgang: %s (%d aktiviert, %d vorgemerkt)", report.summary(),
                len(report.activated), len(report.pending))
    profile.notes = _compose_notes(profile, report)
    return profile


def forget_rule(profile: VendorProfile, key: str) -> bool:
    """Eine gelernte Regel gezielt zuruecknehmen.  ``True`` bei Erfolg."""
    removed = profile.pending_rules.pop(key, None) is not None
    if profile.rule_counters.pop(key, None) is not None:
        removed = True
    kind, _, payload = key.partition(":")
    if kind == "column_map":
        header, _, _field = payload.partition("=")
        removed |= profile.column_map.pop(header, None) is not None
    elif kind == "header_regex":
        field_name, _, _pattern = payload.partition("=")
        removed |= profile.header_regexes.pop(field_name, None) is not None
    elif kind == "skip":
        if payload in profile.skip_patterns:
            profile.skip_patterns.remove(payload)
            removed = True
    elif kind == "decimal":
        profile.decimal_style = "auto"
        removed = True
    if removed:
        profile.touch()
        logger.info("Gelernte Regel entfernt: %s", key)
    return removed


def describe_learning(profile: VendorProfile) -> list[str]:
    """Klartextliste des Gelernten -- fuer die Profilverwaltung in der GUI."""
    lines: list[str] = []
    for header, field_name in sorted(profile.column_map.items()):
        count = profile.rule_counters.get(f"column_map:{header}={field_name}", 1)
        lines.append(f"Spalte '{header}' -> Feld '{field_name}' ({count}x bestaetigt)")
    for field_name, pattern in sorted(profile.header_regexes.items()):
        lines.append(f"Kopffeld '{field_name}' ueber Muster {pattern!r}")
    for pattern in profile.skip_patterns:
        lines.append(f"Zeilen mit {pattern!r} werden uebersprungen")
    if profile.decimal_style != "auto":
        lines.append(f"Zahlenformat: {profile.decimal_style}")
    if profile.default_uom:
        lines.append(f"Standard-Mengeneinheit: {profile.default_uom}")
    if profile.default_price_unit:
        lines.append(f"Standard-Preiseinheit: {profile.default_price_unit}")
    for key, count in sorted(profile.pending_rules.items()):
        lines.append(f"vorgemerkt (noch nicht aktiv, {count}x): {key}")
    return lines


# --------------------------------------------------------------------------
# Teilschritte
# --------------------------------------------------------------------------

def _confirm(profile: VendorProfile, key: str, needed: int,
             report: LearningReport) -> bool:
    """Zaehler hochsetzen und melden, ob die Regel jetzt greifen darf."""
    count = profile.pending_rules.get(key, 0) + 1
    if count >= needed:
        profile.pending_rules.pop(key, None)
        profile.rule_counters[key] = profile.rule_counters.get(key, 0) + count
        report.activated.append(key)
        return True
    profile.pending_rules[key] = count
    report.pending.append(f"{key} ({count}/{needed})")
    return False


def _position_pairs(original: Offer, corrected: Offer
                    ) -> list[tuple[OfferPosition, OfferPosition]]:
    """Positionen einander zuordnen -- ueber uid, sonst ueber die Reihenfolge."""
    by_uid = {p.uid: p for p in original.positions}
    pairs: list[tuple[OfferPosition, OfferPosition]] = []
    unmatched: list[OfferPosition] = []
    for position in corrected.positions:
        source = by_uid.get(position.uid)
        if source is not None:
            pairs.append((source, position))
        else:
            unmatched.append(position)
    if unmatched and len(original.positions) == len(corrected.positions):
        for source, target in zip(original.positions, corrected.positions):
            if (source, target) not in pairs:
                pairs.append((source, target))
    return pairs


def _learn_columns(original: Offer, corrected: Offer, document: RawDocument,
                   profile: VendorProfile, config: LearningConfig,
                   report: LearningReport) -> bool:
    """Korrigierte Positionsfelder auf Spaltenueberschriften zurueckfuehren."""
    row_cells: dict[int, dict[str, str]] = document.meta.get("row_cells", {})
    if not row_cells:
        return False
    current_map: dict[str, str] = document.meta.get("column_map", {})
    changed = False

    for source, target in _position_pairs(original, corrected):
        cells = row_cells.get(source.uid)
        if not cells:
            continue
        for field_name in _LEARNABLE_POSITION_FIELDS:
            old_value = getattr(source, field_name, None)
            new_value = getattr(target, field_name, None)
            if _same_value(old_value, new_value) or new_value in (None, ""):
                continue
            header = _find_header_for_value(cells, new_value)
            if not header:
                report.observations.append(
                    f"Korrektur '{field_name}' liess sich keiner Spalte zuordnen -- "
                    "es wird nichts gelernt (kein Wert wird gespeichert).")
                continue
            if current_map.get(header) == field_name or profile.column_map.get(header) == field_name:
                continue
            key = f"column_map:{header}={field_name}"
            if _confirm(profile, key, config.column_map_confirmations, report):
                profile.column_map[header] = field_name
                changed = True
                logger.info("Gelernt: Spalte '%s' ist '%s' (Profil %s)",
                            header, field_name, profile.label)
    return changed


def _learn_header_rules(original: Offer, corrected: Offer, document: RawDocument,
                        profile: VendorProfile, config: LearningConfig,
                        report: LearningReport) -> bool:
    """Aus korrigierten Kopffeldern eine Fundstellenregel ableiten."""
    text = document.all_text()
    if not text:
        return False
    changed = False
    for field_name in _LEARNABLE_HEADER_FIELDS:
        old_value = getattr(original, field_name, None)
        new_value = getattr(corrected, field_name, None)
        if _same_value(old_value, new_value) or new_value in (None, ""):
            continue
        pattern = _derive_header_regex(text, field_name, new_value)
        if not pattern:
            report.observations.append(
                f"Fuer '{field_name}' war im Dokument keine Beschriftung zu finden -- "
                "es wird keine Regel gelernt.")
            continue
        if profile.header_regexes.get(field_name) == pattern:
            continue
        key = f"header_regex:{field_name}={pattern}"
        if _confirm(profile, key, config.regex_confirmations, report):
            profile.header_regexes[field_name] = pattern
            changed = True
            logger.info("Gelernt: Kopffeld '%s' ueber %r (Profil %s)",
                        field_name, pattern, profile.label)
    return changed


def _learn_skips(original: Offer, corrected: Offer, profile: VendorProfile,
                 config: LearningConfig, report: LearningReport) -> bool:
    """Vom Anwender geloeschte Positionen -> Ueberspringregel."""
    kept = {p.uid for p in corrected.positions}
    changed = False
    for position in original.positions:
        if position.uid in kept or not position.raw_text:
            continue
        pattern = _derive_skip_pattern(position.raw_text)
        if not pattern or pattern in profile.skip_patterns:
            continue
        if len(profile.skip_patterns) >= config.max_skip_patterns:
            report.observations.append(
                "Obergrenze fuer Ueberspringregeln erreicht -- es wird nichts "
                "weiter gelernt.")
            break
        key = f"skip:{pattern}"
        if _confirm(profile, key, config.skip_confirmations, report):
            profile.skip_patterns.append(pattern)
            changed = True
            logger.info("Gelernt: Zeilen mit %r ueberspringen (Profil %s)",
                        pattern, profile.label)
    return changed


def _learn_decimal_style(original: Offer, corrected: Offer, document: RawDocument,
                         profile: VendorProfile, config: LearningConfig,
                         report: LearningReport) -> bool:
    """Zahlenformat lernen, wenn Preise systematisch umgedeutet wurden."""
    row_cells: dict[int, dict[str, str]] = document.meta.get("row_cells", {})
    if not row_cells:
        return False
    votes = {"de": 0, "en": 0}
    for source, target in _position_pairs(original, corrected):
        cells = row_cells.get(source.uid) or {}
        for field_name in ("price", "quantity", "min_order_qty"):
            old_value = getattr(source, field_name, None)
            new_value = getattr(target, field_name, None)
            if new_value is None or _same_value(old_value, new_value):
                continue
            for raw in cells.values():
                if not raw:
                    continue
                for style in ("de", "en"):
                    if parse_number(raw, style) == new_value and parse_number(raw, "auto") != new_value:
                        votes[style] += 1
    winner = max(votes, key=lambda k: votes[k])
    if votes[winner] == 0 or profile.decimal_style == winner:
        return False
    key = f"decimal:{winner}"
    if _confirm(profile, key, config.decimal_confirmations, report):
        profile.decimal_style = winner
        logger.info("Gelernt: Zahlenformat '%s' (Profil %s)", winner, profile.label)
        return True
    return False


def _learn_defaults(corrected: Offer, profile: VendorProfile,
                    report: LearningReport) -> None:
    """Einheitliche Mengen-/Preiseinheit als Vorbelegung merken.

    Das ist eine *Voreinstellung*, kein erfundener Wert: sie greift spaeter nur
    dort, wo im Angebot nichts steht, und wird als DEFAULT gekennzeichnet.
    """
    uoms = {p.uom for p in corrected.positions if p.uom}
    if len(uoms) == 1:
        value = uoms.pop()
        if profile.default_uom != value:
            profile.default_uom = value
            report.observations.append(f"Standard-Mengeneinheit '{value}' gemerkt.")
    units = {p.price_unit for p in corrected.positions if p.price_unit}
    if len(units) == 1:
        value = units.pop()
        if profile.default_price_unit != value:
            profile.default_price_unit = int(value)
            report.observations.append(f"Standard-Preiseinheit '{value}' gemerkt.")


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _same_value(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
        except (ValueError, ArithmeticError):
            return False
    if isinstance(a, date) or isinstance(b, date):
        return a == b
    return normalize_whitespace(a).casefold() == normalize_whitespace(b).casefold()


def _find_header_for_value(cells: dict[str, str], value: Any) -> str:
    """In welcher Spalte stand der vom Anwender eingetragene Wert?"""
    for header, raw in cells.items():
        if not raw or header.startswith("spalte_"):
            continue
        if _cell_matches(raw, value):
            return header
    # Zweite Runde: auch Spalten ohne Ueberschrift zulassen
    for header, raw in cells.items():
        if raw and _cell_matches(raw, value):
            return header
    return ""


def _cell_matches(raw: str, value: Any) -> bool:
    if isinstance(value, Decimal):
        for style in ("auto", "de", "en"):
            if parse_number(raw, style) == value:
                return True
        return False
    if isinstance(value, date):
        return parse_date(raw) == value
    if isinstance(value, int):
        parsed = parse_decimal(raw)
        return parsed is not None and parsed == Decimal(value)
    text = normalize_whitespace(value).casefold()
    raw_text = normalize_whitespace(raw).casefold()
    if not text or not raw_text:
        return False
    return raw_text == text or (len(text) >= 4 and text in raw_text)


def _derive_header_regex(text: str, field_name: str, value: Any) -> str:
    """Regex aus der *Beschriftung* vor dem korrigierten Wert bilden.

    Der Wert selbst kommt im Muster nicht vor -- gelernt wird nur die Stelle.
    """
    needle = _searchable(value)
    if not needle:
        return ""
    position = text.lower().find(needle.lower())
    if position < 0:
        return ""
    prefix = text[max(0, position - 60):position]
    prefix = prefix.split("\n")[-1]
    label = re.search(r"([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .\-]{2,30}?)\s*[:.\-]?\s*$", prefix)
    if not label:
        return ""
    label_text = normalize_whitespace(label.group(1))
    if len(label_text) < 3:
        return ""
    kind = _HEADER_FIELD_KIND.get(field_name, "code")
    return (re.escape(label_text).replace(r"\ ", r"\s+")
            + r"\s*[:.\-]?\s*" + _VALUE_PATTERNS[kind])


def _searchable(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, Decimal):
        return format(value, "f")
    return normalize_whitespace(value)


def _derive_skip_pattern(raw_text: str) -> str:
    """Aus einer verworfenen Zeile ein sparsames Muster bilden.

    Es werden nur die Worte uebernommen (keine Zahlen), damit das Muster die
    *Art* der Zeile trifft ("Mindermengenzuschlag") und nicht einen Betrag.
    """
    text = normalize_whitespace(raw_text)
    words = [w for w in re.findall(r"[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß.\-]{2,}", text)][:3]
    if len(words) < 1:
        return ""
    pattern = r"\s+".join(re.escape(w) for w in words)
    return pattern


def _compose_notes(profile: VendorProfile, report: LearningReport) -> str:
    parts = [profile.notes] if profile.notes else []
    stamp = profile.updated_at.strftime("%d.%m.%Y %H:%M")
    parts.append(f"{stamp}: {report.summary()}")
    return "\n".join(parts[-20:])
