"""Regelkatalog fuer die Kopfdaten eines Angebots (deutsch und englisch).

Jeder Lieferant beschriftet seine Kopfdaten anders -- "Angebot Nr.",
"Angebots-Nr", "unser Angebot", "Offer No.", "Quotation".  Statt das im Code zu
verstecken, steht hier eine *deklarative* Liste von :class:`HeaderRule`.  Neue
Schreibweisen sind damit eine Zeile Arbeit und kein Umbau.

Jede Regel liefert einen Wert **mit Konfidenz** (0..1).  Alles unterhalb von
:data:`UNCERTAIN_THRESHOLD` wird als :class:`FieldOrigin.UNCERTAIN` markiert und
muss vom Anwender bestaetigt werden.  Es wird nie geraten: liefert keine Regel
einen Treffer, bleibt das Feld leer (``FieldOrigin.MISSING``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from ...models.enums import FieldOrigin
from ...models.offer import EmailContext, Offer
from ...utils.parsing import (
    detect_currency,
    normalize_whitespace,
    normalize_vendor_number,
    parse_date,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HeaderRule",
    "HeaderMatch",
    "HEADER_RULES",
    "INCOTERMS",
    "UNCERTAIN_THRESHOLD",
    "extract_header_fields",
    "apply_header_matches",
    "find_incoterm",
    "vendor_name_from_signature",
    "vendor_name_from_domain",
]

#: Ab hier gilt ein Treffer als belastbar; darunter -> UNCERTAIN
UNCERTAIN_THRESHOLD = 0.6

#: Alle gaengigen Incoterm-Klauseln (2010 und 2020, DDU aus Altvertraegen)
INCOTERMS: tuple[str, ...] = (
    "EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
    "DAP", "DPU", "DDP", "DDU", "DAF", "DES", "DEQ", "DAT",
)

# --------------------------------------------------------------------------
# Bausteine fuer die Regeln
# --------------------------------------------------------------------------

#: Datumsangabe in allen ueblichen Schreibweisen
_DATE = (r"(\d{1,2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{2,4}"
         r"|\d{4}-\d{1,2}-\d{1,2}"
         r"|\d{1,2}\.?\s*[A-Za-zÄÖÜäöüß]{3,10}\.?\s+\d{4})")

#: Beleg-/Kundennummer: Buchstaben, Ziffern, Trenner -- aber kein Satzende
_DOCNO = r"([A-Za-z0-9][A-Za-z0-9._\-/]{2,29})"

#: Rest der Zeile (fuer Zahlungsbedingungen, Ansprechpartner, ...)
_LINE = r"([^\n\r]{2,120})"

#: Zeilenanfang bzw. Zellengrenze -- verhindert Treffer mitten im Wort
_START = r"(?:^|[\n\r\t|;])\s*"


@dataclass(frozen=True, slots=True)
class HeaderRule:
    """Eine Erkennungsregel fuer genau ein Kopffeld.

    ``field``       Zielfeld im :class:`~app.models.offer.Offer`
    ``pattern``     Regex mit *einer* Fanggruppe (Incoterm: zwei)
    ``confidence``  Wie sicher ist dieser Treffer (0..1)
    ``priority``    Bei gleicher Konfidenz gewinnt die hoehere Prioritaet
    ``post``        Nachbearbeitung: text|line|date|currency|vendor_no|incoterm
    ``description`` Klartext fuer das Protokoll ("Angebotsnummer ueber 'Angebot Nr.'")
    """

    field: str
    pattern: str
    confidence: float = 0.8
    priority: int = 50
    post: str = "text"
    description: str = ""
    flags: int = re.I | re.M

    def compiled(self) -> re.Pattern[str]:
        return _compile(self.pattern, self.flags)


@dataclass(slots=True)
class HeaderMatch:
    """Ein erkannter Kopfwert samt Herkunft und Konfidenz."""

    field: str
    value: Any
    raw: str = ""
    confidence: float = 0.0
    rule: str = ""
    source: str = "Dokument"

    @property
    def origin(self) -> FieldOrigin:
        return (FieldOrigin.EXTRACTED if self.confidence >= UNCERTAIN_THRESHOLD
                else FieldOrigin.UNCERTAIN)

    def note(self) -> str:
        return (f"{self.field}: '{normalize_whitespace(self.raw)[:60]}' "
                f"({self.rule or 'Regel'}, Konfidenz {self.confidence:.2f}, "
                f"Quelle {self.source})")


_PATTERN_CACHE: dict[tuple[str, int], re.Pattern[str]] = {}


def _compile(pattern: str, flags: int) -> re.Pattern[str]:
    key = (pattern, flags)
    compiled = _PATTERN_CACHE.get(key)
    if compiled is None:
        compiled = re.compile(pattern, flags)
        _PATTERN_CACHE[key] = compiled
    return compiled


# --------------------------------------------------------------------------
# Der Regelkatalog
# --------------------------------------------------------------------------

HEADER_RULES: tuple[HeaderRule, ...] = (
    # -- Angebotsnummer --------------------------------------------------
    HeaderRule("offer_number", r"Angebots?[\s\-_]?(?:Nr|Nummer)\.?\s*[:.\-]?\s*" + _DOCNO,
               0.95, 90, "text", "Angebots-Nr."),
    HeaderRule("offer_number", r"Angebotsnummer\s*[:.\-]?\s*" + _DOCNO,
               0.95, 90, "text", "Angebotsnummer"),
    HeaderRule("offer_number", r"unser(?:es)?\s+Angebot\s*(?:Nr\.?|Nummer)?\s*[:.\-]?\s*" + _DOCNO,
               0.90, 80, "text", "unser Angebot"),
    HeaderRule("offer_number", r"(?:Offer|Quotation|Quote)\s*(?:No|Nr|Number)\.?\s*[:.\-#]?\s*" + _DOCNO,
               0.95, 90, "text", "Offer No."),
    HeaderRule("offer_number", r"(?:Quotation|Offer)\s*[:#]\s*" + _DOCNO,
               0.85, 70, "text", "Quotation:"),
    HeaderRule("offer_number", _START + r"Angebot\s*[:#]\s*" + _DOCNO,
               0.85, 70, "text", "Angebot:"),
    HeaderRule("offer_number", r"(?:Beleg|Dokument)(?:en)?[\s\-]?(?:Nr|Nummer)\.?\s*[:.\-]?\s*" + _DOCNO,
               0.70, 40, "text", "Belegnummer"),
    HeaderRule("offer_number", r"\bAngebot\s+(?:Nr\.?\s*)?([0-9][0-9._\-/]{3,20})",
               0.70, 30, "text", "Angebot <Nummer>"),
    HeaderRule("offer_number", r"(?:AN|QT|OF)[\-/]?(\d{5,12})\b",
               0.55, 10, "text", "Nummernmuster AN-/QT-"),

    # -- Angebotsdatum ---------------------------------------------------
    HeaderRule("offer_date", r"Angebotsdatum\s*[:.\-]?\s*" + _DATE,
               0.95, 90, "date", "Angebotsdatum"),
    HeaderRule("offer_date", r"(?:Offer|Quotation|Quote)\s*Date\s*[:.\-]?\s*" + _DATE,
               0.95, 90, "date", "Offer Date"),
    HeaderRule("offer_date", r"(?:Ausstellungs|Beleg|Erstellungs)datum\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "Belegdatum"),
    HeaderRule("offer_date", _START + r"Datum\s*[:.\-]\s*" + _DATE,
               0.80, 60, "date", "Datum"),
    HeaderRule("offer_date", r"\b(?:vom|dated)\s+" + _DATE,
               0.70, 40, "date", "vom <Datum>"),
    HeaderRule("offer_date", _START + r"Date\s*[:.\-]\s*" + _DATE,
               0.75, 50, "date", "Date"),
    HeaderRule("offer_date", r"[A-Za-zÄÖÜäöüß]+,\s*(?:den\s*)?" + _DATE,
               0.55, 10, "date", "Ort, den <Datum>"),

    # -- Gueltigkeit -----------------------------------------------------
    HeaderRule("valid_to", r"(?:Angebot\s+)?g(?:ue|ü)ltig\s+bis\s*(?:zum)?\s*[:.\-]?\s*" + _DATE,
               0.95, 90, "date", "gueltig bis"),
    HeaderRule("valid_to", r"freibleibend\s+bis\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "freibleibend bis"),
    HeaderRule("valid_to", r"G(?:ue|ü)ltigkeit(?:sdatum|sende)?\s*[:.\-]?\s*(?:bis\s*)?" + _DATE,
               0.85, 70, "date", "Gueltigkeit"),
    HeaderRule("valid_to", r"Preise?\s+(?:gelten|gilt|g(?:ue|ü)ltig)\s+bis\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "Preise gelten bis"),
    HeaderRule("valid_to", r"(?:valid\s+(?:until|till|thru|through)|validity)\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "valid until"),
    HeaderRule("valid_to", r"(?:bis\s+zum|befristet\s+bis)\s*" + _DATE,
               0.60, 20, "date", "bis zum"),

    HeaderRule("valid_from", r"Preise?\s+g(?:ue|ü)ltig\s+ab\s*[:.\-]?\s*" + _DATE,
               0.95, 90, "date", "Preise gueltig ab"),
    HeaderRule("valid_from", r"g(?:ue|ü)ltig\s+ab\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "gueltig ab"),
    HeaderRule("valid_from", r"(?:gelten|gilt|wirksam)\s+ab\s*(?:dem)?\s*[:.\-]?\s*" + _DATE,
               0.85, 70, "date", "gilt ab"),
    HeaderRule("valid_from", r"mit\s+Wirkung\s+(?:zum|ab)\s*" + _DATE,
               0.85, 70, "date", "mit Wirkung zum"),
    HeaderRule("valid_from", r"(?:valid|effective)\s+(?:from|as\s+of)\s*[:.\-]?\s*" + _DATE,
               0.90, 80, "date", "valid from"),
    HeaderRule("valid_from", r"\bab\s+(?:dem\s+)?" + _DATE,
               0.60, 20, "date", "ab <Datum>"),
    HeaderRule("valid_from", r"\bzum\s+" + _DATE,
               0.60, 20, "date", "zum <Datum>"),

    # -- Waehrung --------------------------------------------------------
    HeaderRule("currency", r"W(?:ae|ä)hrung\s*[:.\-]?\s*([A-Za-z€$£¥]{1,4})",
               0.95, 90, "currency", "Waehrung"),
    HeaderRule("currency", r"Currency\s*[:.\-]?\s*([A-Za-z€$£¥]{1,4})",
               0.95, 90, "currency", "Currency"),
    HeaderRule("currency", r"(?:alle\s+)?Preise\s+(?:verstehen\s+sich\s+)?in\s+([A-Za-z€$£¥]{1,4})\b",
               0.90, 80, "currency", "Preise in <Waehrung>"),
    HeaderRule("currency", r"(?:all\s+)?prices?\s+in\s+([A-Za-z€$£¥]{1,4})\b",
               0.90, 80, "currency", "prices in"),
    HeaderRule("currency", r"\b(EUR|USD|CHF|GBP|SEK|DKK|NOK|PLN|CZK|JPY)\b\s*(?:/|pro|per|je)?",
               0.55, 10, "currency", "Waehrungskuerzel im Text"),
    HeaderRule("currency", r"([€$£¥])\s*\d",
               0.55, 5, "currency", "Waehrungszeichen am Preis"),

    # -- Lieferantennummer ----------------------------------------------
    HeaderRule("vendor_number", r"(?:Lieferanten|Kreditoren)[\s\-]?(?:Nr|Nummer|konto)\.?\s*[:.\-]?\s*([0-9A-Za-z]{3,12})",
               0.90, 90, "vendor_no", "Lieferantennummer"),
    HeaderRule("vendor_number", r"Ihre\s+Lieferantennummer\s*(?:bei\s+uns)?\s*[:.\-]?\s*([0-9A-Za-z]{3,12})",
               0.90, 90, "vendor_no", "Ihre Lieferantennummer"),
    HeaderRule("vendor_number", r"(?:Vendor|Supplier)\s*(?:No|Number|Code|ID)\.?\s*[:.\-]?\s*([0-9A-Za-z]{3,12})",
               0.90, 90, "vendor_no", "Vendor No."),
    HeaderRule("vendor_number", r"Kreditor\s*[:.\-]\s*([0-9]{4,10})",
               0.80, 60, "vendor_no", "Kreditor"),

    # -- Kundennummer (unsere Nummer beim Lieferanten) -------------------
    HeaderRule("customer_number", r"(?:Ihre\s+)?Kunden(?:\-|\s)?(?:Nr|Nummer)\.?\s*[:.\-]?\s*([0-9A-Za-z]{3,15})",
               0.90, 90, "text", "Kundennummer"),
    HeaderRule("customer_number", r"(?:Customer|Account)\s*(?:No|Number|Code)\.?\s*[:.\-]?\s*([0-9A-Za-z]{3,15})",
               0.90, 90, "text", "Customer No."),
    HeaderRule("customer_number", r"Debitor(?:en)?(?:\-|\s)?(?:Nr|Nummer)?\.?\s*[:.\-]\s*([0-9A-Za-z]{3,15})",
               0.80, 60, "text", "Debitorennummer"),

    # -- Lieferant (Name) ------------------------------------------------
    HeaderRule("vendor_name", _START + r"Lieferant(?:ename)?\s*[:.\-]\s*" + _LINE,
               0.85, 80, "line", "Lieferant:"),
    HeaderRule("vendor_name", _START + r"(?:Vendor|Supplier)\s*[:.\-]\s*" + _LINE,
               0.85, 80, "line", "Vendor:"),
    HeaderRule("vendor_name", _START + r"Firma\s*[:.\-]\s*" + _LINE,
               0.70, 50, "line", "Firma:"),

    # -- Incoterm --------------------------------------------------------
    HeaderRule("incoterm",
               r"(?:Incoterms?|Lieferbedingung(?:en)?|Lieferkondition(?:en)?|"
               r"Delivery\s+terms?|Terms\s+of\s+delivery)\s*(?:20\d\d)?\s*[:.\-]?\s*"
               r"\b(" + "|".join(INCOTERMS) + r")\b[\s,:\-]*([^\n\r,;]{0,40})?",
               0.95, 90, "incoterm", "Incoterm mit Beschriftung"),
    HeaderRule("incoterm",
               r"\b(" + "|".join(INCOTERMS) + r")\b\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]{2,25}"
               r"(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]{2,25})?)",
               0.70, 40, "incoterm", "Incoterm-Klausel im Text"),
    HeaderRule("incoterm", r"\b(" + "|".join(INCOTERMS) + r")\b",
               0.60, 20, "incoterm", "Incoterm-Kuerzel"),
    HeaderRule("incoterm", r"(?:Lieferbedingung(?:en)?|Versand)\s*[:.\-]\s*(frei\s+Haus|ab\s+Werk|unfrei)",
               0.70, 45, "incoterm_text", "Lieferbedingung im Klartext"),

    # -- Zahlungsbedingungen --------------------------------------------
    HeaderRule("payment_terms", r"Zahlungsbedingung(?:en)?\s*[:.\-]?\s*" + _LINE,
               0.95, 90, "line", "Zahlungsbedingungen"),
    HeaderRule("payment_terms", r"Zahlungsziel\s*[:.\-]?\s*" + _LINE,
               0.90, 85, "line", "Zahlungsziel"),
    HeaderRule("payment_terms", r"(?:Terms\s+of\s+payment|Payment\s+terms?)\s*[:.\-]?\s*" + _LINE,
               0.95, 90, "line", "Payment terms"),
    HeaderRule("payment_terms", _START + r"Zahlung\s*[:.\-]\s*" + _LINE,
               0.80, 60, "line", "Zahlung:"),
    HeaderRule("payment_terms", r"(\d{1,2}\s*%\s*Skonto[^\n\r]{0,60})",
               0.75, 50, "text", "Skontoregel"),
    HeaderRule("payment_terms", r"(netto\s+\d{1,3}\s*Tage(?:\s+ohne\s+Abzug)?)",
               0.80, 55, "text", "netto X Tage"),
    HeaderRule("payment_terms", r"(\d{1,3}\s*Tage\s+netto)",
               0.80, 55, "text", "X Tage netto"),
    HeaderRule("payment_terms", r"((?:net|payment)\s+\d{1,3}\s*days)",
               0.80, 55, "text", "net X days"),

    # -- Ansprechpartner -------------------------------------------------
    HeaderRule("contact", r"(?:Ihr(?:e)?\s+)?Ansprechpartner(?:in)?\s*[:.\-]?\s*" + _LINE,
               0.90, 90, "line", "Ansprechpartner"),
    HeaderRule("contact", r"(?:Ihr\s+)?(?:Sachbearbeiter(?:in)?|Kundenbetreuer(?:in)?)\s*[:.\-]?\s*" + _LINE,
               0.85, 80, "line", "Sachbearbeiter"),
    HeaderRule("contact", r"(?:Your\s+)?contact(?:\s+person)?\s*[:.\-]\s*" + _LINE,
               0.85, 80, "line", "Contact person"),
    HeaderRule("contact", r"(?:Bei\s+R(?:ue|ü)ckfragen\s+wenden\s+Sie\s+sich\s+an)\s*[:.\-]?\s*" + _LINE,
               0.70, 50, "line", "Rueckfragen an"),
)


# --------------------------------------------------------------------------
# Nachbearbeitung
# --------------------------------------------------------------------------

_TRAILING = " \t.,;:-–—|)"


def _post_text(raw: str) -> str:
    return normalize_whitespace(raw).strip(_TRAILING)


def _post_line(raw: str) -> str:
    """Rest einer Zeile saeubern (Doppelbeschriftungen abschneiden)."""
    text = normalize_whitespace(raw)
    # Eine zweite Beschriftung in derselben Zeile beendet den Wert
    text = re.split(r"\s{3,}|\s\|\s|\t", text)[0]
    return text.strip(_TRAILING)


def _post_date(raw: str) -> date | None:
    return parse_date(normalize_whitespace(raw).replace(" ", ""))


def _post_currency(raw: str) -> str:
    return detect_currency(raw)


def _post_vendor_no(raw: str) -> str:
    value = _post_text(raw)
    return normalize_vendor_number(value) if value else ""


_LIEFERBEDINGUNG_MAP = {
    "frei haus": ("DDP", ""),
    "ab werk": ("EXW", ""),
}


def _post_incoterm(match: re.Match[str]) -> tuple[str, str]:
    """Incoterm-Treffer in (Klausel, Ort) zerlegen."""
    code = (match.group(1) or "").upper()
    location = ""
    if match.lastindex and match.lastindex >= 2:
        location = _post_text(match.group(2) or "")
    # Offensichtlichen Fliesstext hinter der Klausel verwerfen
    if location and (len(location) > 40 or location.lower().startswith(
            ("und", "sowie", "gemaess", "gemäß", "inkl", "zzgl", "nach", "laut"))):
        location = ""
    return code, location


def find_incoterm(text: str) -> tuple[str, str, float]:
    """Incoterm-Klausel und Ort im Text suchen.  ``("", "", 0.0)`` wenn keiner."""
    for rule in HEADER_RULES:
        if rule.field != "incoterm" or rule.post != "incoterm":
            continue
        match = rule.compiled().search(text)
        if match:
            code, location = _post_incoterm(match)
            if code in INCOTERMS:
                return code, location, rule.confidence
    return "", "", 0.0


# --------------------------------------------------------------------------
# Lieferantenname aus E-Mail-Umfeld
# --------------------------------------------------------------------------

#: Rechtsformen, an denen sich ein Firmenname erkennen laesst
_LEGAL_FORMS = (
    "gmbh & co. kg", "gmbh & co kg", "gmbh", "ag", "kg", "ohg", "gbr", "e.k.",
    "se", "ug", "ltd", "ltd.", "limited", "inc", "inc.", "corp", "corporation",
    "b.v.", "bv", "n.v.", "nv", "s.a.", "sa", "s.p.a.", "spa", "s.r.l.", "srl",
    "sarl", "s.a.r.l.", "oy", "ab", "a/s", "as", "plc", "co. kg", "kgaa",
)

#: Freemail-Domaenen taugen nicht als Lieferantenname
_FREEMAIL = {
    "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "gmx.com",
    "t-online.de", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
    "yahoo.com", "yahoo.de", "freenet.de", "aol.com", "icloud.com", "me.com",
    "mail.com", "posteo.de", "protonmail.com",
}


def vendor_name_from_signature(text: str, max_lines: int = 400) -> tuple[str, float]:
    """Firmennamen aus Signatur/Briefkopf ableiten (Zeile mit Rechtsform).

    Bewusst streng: die Rechtsform muss am *Ende* einer kurzen, ziffernfreien
    Zeile stehen.  Sonst wird aus "gueltig ab 01.09.2026" ueber das schwedische
    "AB" ein Firmenname -- genau solche stillen Fehlgriffe sind hier verboten.
    """
    if not text:
        return "", 0.0
    for line in text.splitlines()[:max_lines]:
        candidate = normalize_whitespace(line).strip(_TRAILING)
        # Beschriftungen wie "Lieferant: Muster GmbH" auf den Wert kuerzen
        if ":" in candidate:
            candidate = candidate.split(":", 1)[1].strip(_TRAILING) or candidate
        if not candidate or len(candidate) > 70 or re.search(r"\d", candidate):
            continue
        words = candidate.split()
        if not 2 <= len(words) <= 8:
            continue
        tail = " ".join(words[-3:])
        for form in _LEGAL_FORMS:
            if len(form) <= 3:
                # Kurzformen (AG, AB, SE, KG) nur in Grossschreibung akzeptieren
                if re.search(rf"(?:^|[\s.]){re.escape(form.upper())}$", tail):
                    return candidate, 0.75
            elif tail.lower().endswith(form):
                return candidate, 0.75
    return "", 0.0


def vendor_name_from_domain(email: EmailContext | None) -> tuple[str, float]:
    """Notbehelf: Firmenname aus der Absenderdomaene ableiten.

    Bewusst mit niedriger Konfidenz -- "dichtungen-mueller.de" wird zu
    "Dichtungen Mueller", das ist ein *Vorschlag*, keine Wahrheit.
    """
    if email is None:
        return "", 0.0
    domain = email.sender_domain
    if not domain or domain in _FREEMAIL:
        return "", 0.0
    label = domain.rsplit(".", 2)[0] if domain.count(".") > 1 else domain.split(".")[0]
    label = label.replace("mail.", "").strip()
    if not label or len(label) < 3:
        return "", 0.0
    words = [w.capitalize() for w in re.split(r"[-_.]", label) if w]
    return " ".join(words), 0.5


# --------------------------------------------------------------------------
# Hauptfunktion
# --------------------------------------------------------------------------

def extract_header_fields(text: str, email: EmailContext | None = None,
                          extra_rules: Iterable[HeaderRule] = (),
                          source: str = "Dokument") -> dict[str, HeaderMatch]:
    """Alle Kopffelder aus einem Text erkennen.

    Es gewinnt je Feld der Treffer mit der hoechsten Konfidenz; bei Gleichstand
    die hoehere Prioritaet und danach die fruehere Fundstelle im Text.
    """
    matches: dict[str, HeaderMatch] = {}
    ranks: dict[str, tuple[float, int, int]] = {}
    if not text and email is None:
        return matches

    haystack = text or ""
    rules = list(HEADER_RULES) + list(extra_rules)

    for rule in rules:
        try:
            pattern = rule.compiled()
        except re.error as exc:  # pragma: no cover -- nur bei kaputten Zusatzregeln
            logger.error("Ungueltige Kopfregel fuer %s: %s", rule.field, exc)
            continue
        try:
            found = pattern.search(haystack)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kopfregel '%s' fehlgeschlagen: %s", rule.description, exc)
            continue
        if not found:
            continue

        for candidate in _matches_from_rule(rule, found, source):
            _keep_best(matches, ranks, candidate, rule.priority, found.start())

    # E-Mail als zusaetzliche Quelle fuer den Lieferantennamen
    if email is not None:
        _add_email_sources(matches, email, haystack)

    if "vendor_name" not in matches:
        # Briefkopf: die erste Zeile mit Rechtsform ist fast immer der Absender.
        # Bewusst nur "unsicher" -- im Kopf kann auch unsere eigene Anschrift stehen.
        letterhead, _ = vendor_name_from_signature(haystack, max_lines=15)
        if letterhead:
            matches["vendor_name"] = HeaderMatch(
                "vendor_name", letterhead, letterhead, 0.58,
                "Firmenname aus dem Briefkopf", source)

    if matches:
        logger.debug("Kopffelder erkannt: %s",
                     {k: str(v.value)[:40] for k, v in matches.items()})
    return matches


def _matches_from_rule(rule: HeaderRule, found: re.Match[str],
                       source: str) -> list[HeaderMatch]:
    """Aus einem Regex-Treffer die konkreten Feldwerte bilden."""
    raw = found.group(1) if found.lastindex else found.group(0)
    out: list[HeaderMatch] = []

    if rule.post == "incoterm":
        code, location = _post_incoterm(found)
        if code not in INCOTERMS:
            return []
        out.append(HeaderMatch("incoterm", code, found.group(0), rule.confidence,
                               rule.description, source))
        if location:
            out.append(HeaderMatch("incoterm_location", location, found.group(0),
                                   rule.confidence * 0.95, rule.description, source))
        return out

    if rule.post == "incoterm_text":
        key = normalize_whitespace(raw).lower()
        mapped = _LIEFERBEDINGUNG_MAP.get(key)
        if not mapped:
            return []
        out.append(HeaderMatch("incoterm", mapped[0], raw, rule.confidence,
                               rule.description, source))
        return out

    if rule.post == "date":
        value: Any = _post_date(raw)
    elif rule.post == "currency":
        value = _post_currency(raw)
    elif rule.post == "line":
        value = _post_line(raw)
    elif rule.post == "vendor_no":
        value = _post_vendor_no(raw)
    else:
        value = _post_text(raw)

    if value in (None, "", []):
        return []
    return [HeaderMatch(rule.field, value, raw, rule.confidence, rule.description, source)]


def _keep_best(matches: dict[str, HeaderMatch], ranks: dict[str, tuple[float, int, int]],
               candidate: HeaderMatch, priority: int, position: int) -> None:
    """Besseren Treffer behalten (Konfidenz > Prioritaet > Fundstelle)."""
    new_rank = (candidate.confidence, priority, -position)
    if candidate.field not in matches or new_rank > ranks.get(candidate.field,
                                                              (-1.0, 0, 0)):
        matches[candidate.field] = candidate
        ranks[candidate.field] = new_rank


def _add_email_sources(matches: dict[str, HeaderMatch], email: EmailContext,
                       text: str) -> None:
    """Absender/Signatur als zusaetzliche Quelle fuer den Lieferantennamen."""
    existing = matches.get("vendor_name")
    best_confidence = existing.confidence if existing else 0.0

    signature_name, signature_confidence = vendor_name_from_signature(
        text or email.body_text)
    if signature_name and signature_confidence > best_confidence:
        matches["vendor_name"] = HeaderMatch(
            "vendor_name", signature_name, signature_name, signature_confidence,
            "Firmenname aus Signatur/Briefkopf", "E-Mail-Signatur")
        best_confidence = signature_confidence

    if email.from_name and len(email.from_name) > 2 and best_confidence < 0.6:
        # Der Anzeigename ist oft "Max Muster (Muster GmbH)"
        company = re.search(r"\(([^)]{3,60})\)", email.from_name)
        candidate = company.group(1) if company else ""
        if candidate:
            matches["vendor_name"] = HeaderMatch(
                "vendor_name", normalize_whitespace(candidate), email.from_name, 0.6,
                "Firmenname aus dem Absender-Anzeigenamen", "E-Mail-Absender")
            best_confidence = 0.6

    domain_name, domain_confidence = vendor_name_from_domain(email)
    if domain_name and domain_confidence > best_confidence:
        matches["vendor_name"] = HeaderMatch(
            "vendor_name", domain_name, email.from_address, domain_confidence,
            "Firmenname aus der Absenderdomaene", "E-Mail-Domaene")

    if email.sent is not None and "offer_date" not in matches:
        # Das Sendedatum ist ein *Hinweis* auf das Angebotsdatum, kein Beleg --
        # deshalb bewusst nur "unsicher".
        matches["offer_date"] = HeaderMatch(
            "offer_date", email.sent.date(), email.sent.strftime("%d.%m.%Y"), 0.55,
            "Sendedatum der E-Mail (kein Angebotsdatum im Text gefunden)",
            "E-Mail-Kopf")

    if email.from_name and "contact" not in matches:
        person = re.sub(r"\s*\([^)]*\)\s*", " ", email.from_name).strip()
        if person and " " in person and len(person) <= 60:
            matches["contact"] = HeaderMatch(
                "contact", normalize_whitespace(person), email.from_name, 0.65,
                "Ansprechpartner aus dem Absender", "E-Mail-Absender")


# --------------------------------------------------------------------------
# Uebernahme in das Angebot
# --------------------------------------------------------------------------

#: Felder, die es im Offer-Modell wirklich gibt
_OFFER_FIELDS = (
    "vendor_name", "vendor_number", "offer_number", "offer_date", "currency",
    "valid_from", "valid_to", "incoterm", "incoterm_location", "payment_terms",
    "contact",
)


def apply_header_matches(offer: Offer, matches: dict[str, HeaderMatch],
                         overwrite: bool = False) -> list[str]:
    """Erkannte Kopfwerte in das Angebot uebernehmen und protokollieren.

    ``overwrite=False`` (Standard) laesst bereits gesetzte Werte unangetastet --
    wichtig, wenn mehrere Dateien in *ein* Angebot importiert werden.
    Rueckgabe: Protokollzeilen fuer ``Offer.extraction_notes``.
    """
    notes: list[str] = []
    for name in _OFFER_FIELDS:
        match = matches.get(name)
        if match is None or match.value in (None, ""):
            continue
        current = getattr(offer, name, None)
        if current not in (None, "") and not overwrite:
            if str(current) != str(match.value):
                notes.append(f"Kopffeld {name}: bestehender Wert '{current}' behalten, "
                             f"Fund '{match.value}' verworfen.")
            continue
        offer.set_field(name, match.value, match.origin)
        notes.append("Kopf erkannt -- " + match.note())

    extra = matches.get("customer_number")
    if extra is not None and extra.value:
        notes.append(f"Kundennummer beim Lieferanten erkannt: {extra.value} "
                     f"(Konfidenz {extra.confidence:.2f})")

    for note in notes:
        offer.add_note(note)
    return notes


def missing_header_note(offer: Offer) -> str:
    """Klartext, welche Pflichtfelder fehlen (fuer die Oberflaeche)."""
    missing = offer.missing_header_fields
    if not missing:
        return ""
    labels = {
        "vendor_name": "Lieferant", "offer_number": "Angebotsnummer",
        "offer_date": "Angebotsdatum", "currency": "Waehrung",
    }
    return ("Nicht erkannt und daher leer: "
            + ", ".join(labels.get(f, f) for f in missing))
