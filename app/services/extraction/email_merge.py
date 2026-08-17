"""Mail und Anhang bilden EIN Angebot.

Der haeufigste Fall im Einkaufsalltag sieht so aus: Der Lieferant haengt seine
Preisliste als PDF oder Excel an -- und schreibt das Entscheidende in den
Mailtext::

    "Anbei unsere Preisliste.  Die Preise gelten ab 01.09.2026,
     Zahlungsziel 30 Tage netto.
     Position 30 entfaellt, dafuer neu: 47110009 zu 4,20 EUR.
     Fuer Artikel 47110001 gilt abweichend eine Mindestmenge von 500 Stueck.
     Der Preis fuer 47110002 im Anhang ist ueberholt, es gilt 9,10 EUR."

Wer nur den Anhang liest, kauft zu falschen Konditionen ein.  Wer nur die Mail
liest, hat keine Positionen.  Dieses Modul fuehrt beides zusammen:

* **Kopfdaten** aus dem Mailtext ergaenzen den Anhang; bei Widerspruch gewinnt
  die Mail (:attr:`ExtractionSettings.email_header_wins_over_attachment`) --
  aber nur mit Protokolleintrag.
* **Positionsergaenzungen** aus dem Mailtext (Preis, Gueltigkeit, Mindestmenge,
  Lieferzeit, Streichung, Zusatzposition) werden auf die Positionen des Anhangs
  angewandt (:attr:`ExtractionSettings.apply_email_supplements`).

Drei eiserne Regeln, die auch hier gelten:

1. **Nichts wird stillschweigend ueberschrieben.**  Jede Uebernahme aus dem
   Mailtext steht als Klartextsatz in ``Offer.extraction_notes``, und die
   betroffene Position traegt im ``source_hint``, dass sie aus zwei Quellen
   stammt.
2. **Nichts wird geloescht.**  Eine im Mailtext gestrichene Position wird
   *abgewaehlt* und bekommt eine Bemerkung -- der Anwender sieht sie weiter.
3. **Widerspruch heisst unsicher.**  Nennen Anhang und Mail verschiedene
   Preise, wird der Mailwert gesetzt *und* als
   :class:`~app.models.enums.FieldOrigin.UNCERTAIN` markiert
   (:attr:`ExtractionSettings.conflict_marks_uncertain`).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from ...config.settings import Settings
from ...models.enums import FieldOrigin, SourceKind
from ...models.offer import Offer
from ...models.offer_position import OfferPosition
from ...utils.parsing import (
    detect_currency,
    format_decimal,
    normalize_material_number,
    normalize_uom,
    normalize_whitespace,
    parse_date,
    parse_decimal,
)
from .header_rules import HeaderMatch, apply_header_matches
from .material_roles import compile_own_pattern, matches_own_pattern

logger = logging.getLogger(__name__)

__all__ = [
    "MAIL_SOURCE_MARK",
    "apply_email_header",
    "apply_email_supplements",
    "mail_segments",
]

#: Kennzeichen im ``source_hint``: diese Position stammt aus zwei Quellen
MAIL_SOURCE_MARK = "ergaenzt aus dem Mailtext"

#: Kopffelder, die eine Mail sinnvoll gegen den Anhang setzen kann
_HEADER_FIELDS = (
    "offer_number", "offer_date", "currency", "valid_from", "valid_to",
    "incoterm", "incoterm_location", "payment_terms", "contact",
    "vendor_name", "vendor_number",
)

_CURRENCY_TOKEN = r"EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|€|\$|£"

#: Preis mit ausdruecklicher Waehrung.  Ohne Waehrung wird nichts als Preis
#: gedeutet -- "500 Stueck" ist kein Betrag.
_PRICE = re.compile(
    r"(?:(?P<pre>" + _CURRENCY_TOKEN + r")\s*)?"
    r"(?P<value>\d{1,3}(?:[.\s]\d{3})*[.,]\d{1,4}|\d{1,7}[.,]\d{1,4}|\d{1,7})"
    r"\s*(?P<post>" + _CURRENCY_TOKEN + r")?",
    re.I,
)

#: Formulierungen, die einen Wert des Anhangs ausser Kraft setzen
_OVERRIDE = re.compile(
    r"(?:(?:ue|ü)berholt|(?:ue|ü)berschrieben|abweichend|stattdessen|"
    r"statt\s+dessen|nicht\s+mehr\s+g(?:ue|ü)ltig|es\s+gilt|gilt\s+(?:jetzt|nun|neu)|"
    r"neuer\s+Preis|Preis\s+(?:betr(?:ae|ä)gt|lautet|(?:ae|ä)ndert)|korrigiert|"
    r"berichtigt|bitte\s+(?:beachten|verwenden)|richtig(?:er)?\s+ist)",
    re.I,
)

#: Streichung einer Position
_DELETION = re.compile(
    r"\b(?:entf(?:ae|ä)llt|entfallen|gestrichen|streichen\s+wir|"
    r"nicht\s+mehr\s+(?:lieferbar|im\s+Programm|im\s+Sortiment)|"
    r"wird\s+nicht\s+(?:mehr\s+)?angeboten)\b",
    re.I,
)

#: Zusatzposition: "dafuer neu: 47110009 zu 4,20 EUR"
_NEW_POSITION = re.compile(
    r"(?:daf(?:ue|ü)r\s+|zus(?:ae|ä)tzlich\s+|erg(?:ae|ä)nzend\s+)?"
    r"\bneu\s*[:\-]?\s*"
    r"(?P<number>[A-Za-z0-9][A-Za-z0-9._\-/]{2,24})"
    r"\s*(?:zu|à|a|f(?:ue|ü)r|mit|zum\s+Preis\s+von)\s*"
    r"(?P<price>\d{1,3}(?:[.\s]\d{3})*[.,]\d{1,4}|\d{1,7}[.,]\d{1,4}|\d{1,7})"
    r"\s*(?P<currency>" + _CURRENCY_TOKEN + r")?",
    re.I,
)

#: Mindestmenge
_MIN_QTY = re.compile(
    r"Mindest(?:bestell|abnahme)?menge\s*(?:von|betr(?:ae|ä)gt|:)?\s*"
    r"(\d{1,3}(?:[.\s]\d{3})*|\d{1,7})\s*"
    r"(St(?:ue|ü)ck|Stk\.?|St\.?|PCS|Meter|m|kg|l)?",
    re.I,
)

#: Lieferzeit in Tagen
_LEAD_TIME = re.compile(
    r"Lieferzeit\s*(?:von|betr(?:ae|ä)gt|:)?\s*(\d{1,3})\s*"
    r"(?:Arbeits|Werk)?tage?n?\b",
    re.I,
)

#: "gilt ab 01.09.2026" -- als Positionsangabe nur mit Artikelbezug
_VALID_FROM = re.compile(
    r"(?:g(?:ue|ü)ltig\s+ab|gilt\s+ab|gelten\s+ab|ab)\s+(?:dem\s+)?"
    r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})",
    re.I,
)

#: "Position 30", "Pos. 30", "Pos 30"
_POSITION_REF = re.compile(r"\bPos(?:ition)?\.?\s*(\d{1,4})\b", re.I)


# --------------------------------------------------------------------------
# Textzerlegung
# --------------------------------------------------------------------------

def mail_segments(text: str) -> list[str]:
    """Mailtext in einzelne Aussagen zerlegen.

    Zerlegt wird an Zeilenumbruechen und an Satzenden.  Der Punkt zaehlt nur
    dann als Satzende, wenn er auf einen Buchstaben folgt -- sonst zerfielen
    Datumsangaben ("01.09.2026") und Nummern in Bruchstuecke.
    """
    segments: list[str] = []
    for line in (text or "").splitlines():
        line = normalize_whitespace(line)
        if not line:
            continue
        for part in re.split(r"(?<=[A-Za-zÄÖÜäöüß%\)])\.\s+", line):
            part = normalize_whitespace(part).strip(" .;")
            if part:
                segments.append(part)
    return segments


# --------------------------------------------------------------------------
# Kopfdaten
# --------------------------------------------------------------------------

def apply_email_header(offer: Offer, matches: dict[str, HeaderMatch],
                       settings: Settings, source: str = "Mailtext") -> list[str]:
    """Kopfdaten aus dem Mailtext gegen die Werte des Anhangs stellen.

    Leere Felder werden ergaenzt.  Widerspricht die Mail dem Anhang, gewinnt
    laut :attr:`ExtractionSettings.email_header_wins_over_attachment` die Mail
    -- der verdraengte Wert steht aber im Protokoll, und das Feld wird bei
    ``conflict_marks_uncertain`` als unsicher markiert.
    """
    extraction = settings.extraction
    notes: list[str] = []

    # Erst alle noch leeren Felder fuellen -- das ist nie ein Widerspruch.
    notes.extend(apply_header_matches(offer, matches, overwrite=False))

    if not extraction.email_header_wins_over_attachment:
        return notes

    for name in _HEADER_FIELDS:
        match = matches.get(name)
        if match is None or match.value in (None, ""):
            continue
        current = getattr(offer, name, None)
        if current in (None, "") or str(current) == str(match.value):
            continue
        origin = (FieldOrigin.UNCERTAIN if extraction.conflict_marks_uncertain
                  else match.origin)
        offer.set_field(name, match.value, origin)
        note = (f"Widerspruch im Kopf: '{name}' stand im Anhang als "
                f"'{_pretty(current)}', im Mailtext als '{_pretty(match.value)}' -- "
                f"der Wert aus dem {source} wurde uebernommen"
                + (" und als unsicher markiert." if extraction.conflict_marks_uncertain
                   else "."))
        notes.append(note)
        logger.info("Kopffeld %s aus dem Mailtext gesetzt (vorher %r)", name, current)
    return notes


# --------------------------------------------------------------------------
# Positionsergaenzungen
# --------------------------------------------------------------------------

def apply_email_supplements(offer: Offer, body_text: str, settings: Settings,
                            source: str = "Mailtext") -> list[str]:
    """Aussagen des Mailtexts auf die Positionen des Anhangs anwenden.

    Rueckgabe: Klartextnotizen fuer ``Offer.extraction_notes``.  Positionen
    werden nur ergaenzt, abgewaehlt oder hinzugefuegt -- nie geloescht.
    """
    notes: list[str] = []
    if not body_text or not body_text.strip():
        return notes
    extraction = settings.extraction
    own_re = compile_own_pattern(extraction.own_material_pattern)

    for segment in mail_segments(body_text):
        targets = _referenced_positions(offer, segment)

        # 1. Streichung -- die Position bleibt sichtbar, wird aber abgewaehlt
        if _DELETION.search(segment):
            for position in targets:
                notes.extend(_strike(position, segment, source))

        # 2. Zusatzposition ("dafuer neu: 47110009 zu 4,20 EUR")
        for match in _NEW_POSITION.finditer(segment):
            notes.extend(_add_position(offer, match, segment, source, own_re,
                                       settings))

        if not targets:
            continue

        # 3. Mindestmenge
        min_match = _MIN_QTY.search(segment)
        if min_match:
            quantity = parse_decimal(min_match.group(1))
            if quantity is not None and quantity > 0:
                for position in targets:
                    notes.extend(_set_value(
                        position, "min_order_qty", quantity, segment, source,
                        settings, label="Mindestmenge"))
                    unit = min_match.group(2)
                    if unit and not position.uom:
                        position.set_field("uom", normalize_uom(unit),
                                           FieldOrigin.EXTRACTED)

        # 4. Lieferzeit
        lead_match = _LEAD_TIME.search(segment)
        if lead_match:
            days = int(lead_match.group(1))
            for position in targets:
                notes.extend(_set_value(position, "lead_time_days", days, segment,
                                        source, settings, label="Lieferzeit (Tage)"))

        # 5. Preis -- nur, wenn der Text den Wert des Anhangs ausdruecklich
        #    ausser Kraft setzt.  Ein blosser Preis im Fliesstext genuegt nicht.
        if _OVERRIDE.search(segment):
            price, currency = _find_price(segment)
            if price is not None:
                for position in targets:
                    notes.extend(_set_value(position, "price", price, segment,
                                            source, settings, label="Preis"))
                    if currency and not position.currency:
                        position.set_field("currency", currency,
                                           FieldOrigin.EXTRACTED)

        # 6. Gueltig ab -- nur mit ausdruecklichem Artikelbezug
        date_match = _VALID_FROM.search(segment)
        if date_match:
            valid_from = parse_date(date_match.group(1))
            if valid_from is not None:
                for position in targets:
                    notes.extend(_set_value(position, "valid_from", valid_from,
                                            segment, source, settings,
                                            label="Gueltig ab"))

    if notes:
        logger.info("%d Ergaenzung(en) aus dem Mailtext uebernommen", len(notes))
    return notes


# --------------------------------------------------------------------------
# Einzelschritte
# --------------------------------------------------------------------------

def _referenced_positions(offer: Offer, segment: str) -> list[OfferPosition]:
    """Welche vorhandenen Positionen nennt dieser Satz?

    Bezug ueber die Artikelnummer (unsere oder die des Lieferanten) oder ueber
    "Position 30".  Positionen, die selbst aus dem Mailtext stammen, bleiben
    aussen vor -- eine Mail ergaenzt den Anhang, nicht sich selbst.
    """
    found: list[OfferPosition] = []
    numbers = {m.group(1) for m in _POSITION_REF.finditer(segment)}
    for position in offer.positions:
        if position.source_kind is SourceKind.EMAIL_BODY:
            continue
        hit = False
        for value in (position.material_number, position.vendor_material_number):
            value = normalize_whitespace(value)
            if value and re.search(rf"(?<![\w\-/.]){re.escape(value)}(?![\w\-/.])",
                                   segment, re.I):
                hit = True
        if not hit and position.position_number:
            hit = normalize_whitespace(position.position_number) in numbers
        if hit and position not in found:
            found.append(position)
    return found


def _mark_two_sources(position: OfferPosition) -> None:
    """Im ``source_hint`` festhalten, dass zwei Quellen beteiligt sind."""
    if MAIL_SOURCE_MARK in position.source_hint:
        return
    base = position.source_hint or "Anhang"
    position.source_hint = f"{base} ({MAIL_SOURCE_MARK})"


def _strike(position: OfferPosition, segment: str, source: str) -> list[str]:
    """Position abwaehlen -- niemals entfernen."""
    if not position.selected:
        return []
    position.selected = False
    position.do_info_record = False
    position.do_source_list = False
    position.do_contract = False
    position.do_purchase_order = False
    remark = f"Laut {source} gestrichen: \"{segment[:80]}\""
    position.set_field("remarks",
                       f"{position.remarks}; {remark}" if position.remarks else remark,
                       FieldOrigin.EXTRACTED)
    _mark_two_sources(position)
    note = (f"Position {position.position_number or position.display_name} wurde "
            f"laut {source} gestrichen und deshalb abgewaehlt (nicht geloescht) -- "
            f"Fundstelle: \"{segment[:80]}\".")
    logger.info("Position %s laut Mailtext abgewaehlt", position.display_name)
    return [note]


def _add_position(offer: Offer, match: re.Match[str], segment: str, source: str,
                  own_re, settings: Settings) -> list[str]:
    """Im Mailtext neu genannte Position anlegen."""
    number = normalize_material_number(match.group("number"))
    price = parse_decimal(match.group("price"))
    if not number or price is None or price <= 0:
        return []
    if any(number in (p.material_number, p.vendor_material_number)
           for p in offer.positions):
        return []

    position = OfferPosition()
    position.source_kind = SourceKind.EMAIL_BODY
    position.source_hint = f"{source} (Zusatz zum Anhang)"
    position.raw_text = segment
    field_name = ("material_number" if matches_own_pattern(number, own_re)
                  else "vendor_material_number")
    position.set_field(field_name, number, FieldOrigin.EXTRACTED)
    position.set_field("price", price, FieldOrigin.EXTRACTED)
    currency = detect_currency(match.group("currency") or "") or offer.currency
    if currency:
        position.set_field("currency", currency, FieldOrigin.EXTRACTED)
    position.set_field("remarks", f"Aus dem {source} ergaenzt", FieldOrigin.EXTRACTED)
    offer.positions.append(position)

    label = ("unsere Materialnummer" if field_name == "material_number"
             else "Artikelnummer des Lieferanten")
    note = (f"Zusatzposition {number} ({label}) zu {format_decimal(price)} "
            f"{currency or ''} aus dem {source} ergaenzt -- "
            f"Fundstelle: \"{segment[:80]}\".").replace("  ", " ")
    logger.info("Zusatzposition %s aus dem Mailtext ergaenzt", number)
    return [note]


def _set_value(position: OfferPosition, field_name: str, value, segment: str,
               source: str, settings: Settings, label: str) -> list[str]:
    """Feld einer Anhangposition aus dem Mailtext setzen -- immer mit Notiz."""
    extraction = settings.extraction
    current = getattr(position, field_name, None)
    if _same(current, value):
        return []

    conflict = current not in (None, "")
    origin = FieldOrigin.EXTRACTED
    if conflict and extraction.conflict_marks_uncertain:
        origin = FieldOrigin.UNCERTAIN
    position.set_field(field_name, value, origin)
    _mark_two_sources(position)

    name = position.material_number or position.vendor_material_number \
        or position.display_name
    if conflict:
        note = (f"Widerspruch bei {label} fuer {name}: Anhang '{_pretty(current)}', "
                f"{source} '{_pretty(value)}' -- der Wert aus dem {source} wurde "
                "uebernommen"
                + (" und als unsicher markiert." if extraction.conflict_marks_uncertain
                   else ".")
                + f" Fundstelle: \"{segment[:80]}\".")
    else:
        note = (f"{label} {_pretty(value)} fuer {name} aus dem {source} ergaenzt "
                f"-- Fundstelle: \"{segment[:80]}\".")
    logger.info("Position %s: %s aus dem Mailtext gesetzt (%s)", name, field_name,
                "Widerspruch" if conflict else "Ergaenzung")
    return [note]


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _find_price(segment: str) -> tuple[Decimal | None, str]:
    """Betrag mit Waehrung aus einem Satz holen (ohne Waehrung: nichts)."""
    for match in _PRICE.finditer(segment):
        token = match.group("post") or match.group("pre") or ""
        if not token:
            continue
        value = parse_decimal(match.group("value"))
        if value is None or value <= 0:
            continue
        return value, detect_currency(token)
    return None, ""


def _same(a, b) -> bool:
    if a is None or a == "":
        return False
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    return str(a) == str(b)


def _pretty(value) -> str:
    if isinstance(value, Decimal):
        return format_decimal(value)
    return normalize_whitespace(value) if not hasattr(value, "strftime") \
        else value.strftime("%d.%m.%Y")
