"""Robuste Parser fuer Zahlen, Datumsangaben und Texte aus Lieferantenangeboten.

Grundsatz des gesamten Projekts: Es wird nie geraten. Kann ein Wert nicht
zweifelsfrei interpretiert werden, liefern die Funktionen ``None`` zurueck --
der Aufrufer muss den Anwender entscheiden lassen.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

__all__ = [
    "parse_decimal",
    "parse_int",
    "parse_date",
    "format_decimal",
    "format_date",
    "normalize_whitespace",
    "normalize_material_number",
    "normalize_vendor_number",
    "normalize_uom",
    "detect_currency",
    "similarity",
]


# --------------------------------------------------------------------------
# Zahlen
# --------------------------------------------------------------------------

_CURRENCY_CHARS = "€$£¥"
# Zeichen, die rund um eine Zahl stehen duerfen, ohne sie ungueltig zu machen.
_NUMBER_NOISE = re.compile(r"[\s '`´]|(?<=\d)\.(?=$)")
_NUMBER_BODY = re.compile(r"^[+-]?[\d.,]+$")


def parse_decimal(value: object) -> Decimal | None:
    """Wandelt deutsche *und* englische Zahlformate in ``Decimal``.

    Erkannt werden u. a. ``1.234,56``, ``1,234.56``, ``1234.56``, ``1234,56``,
    ``12,40 EUR``, ``EUR 12.40``, ``-3,5``.  Mehrdeutige Faelle wie ``1,234``
    (1234 oder 1.234?) werden nach der gaengigen Konvention aufgeloest:
    Gruppen von exakt drei Ziffern nach einem einzelnen Trenner gelten als
    Tausendertrennung, alles andere als Dezimaltrennung.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool ist Subtyp von int -> abfangen
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None

    # Waehrungssymbole/-codes und Klammer-Negation entfernen
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    for char in _CURRENCY_CHARS:
        text = text.replace(char, " ")
    text = re.sub(r"\b(EUR|USD|CHF|GBP|JPY|SEK|DKK|NOK|PLN|CZK)\b", " ", text, flags=re.I)
    text = _NUMBER_NOISE.sub("", text).strip()

    if not text or not _NUMBER_BODY.match(text):
        return None

    sign = ""
    if text[0] in "+-":
        sign, text = ("-" if text[0] == "-" else ""), text[1:]
    if not text:
        return None

    comma_count = text.count(",")
    dot_count = text.count(".")

    if comma_count and dot_count:
        # Der zuletzt auftretende Trenner ist der Dezimaltrenner.
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousand_sep = "." if decimal_sep == "," else ","
        if text.count(decimal_sep) > 1:
            return None
        text = text.replace(thousand_sep, "").replace(decimal_sep, ".")
    elif comma_count or dot_count:
        sep = "," if comma_count else "."
        count = comma_count or dot_count
        parts = text.split(sep)
        tail = parts[-1]
        if count > 1:
            # Mehrfach -> nur als Tausendertrennung sinnvoll (1.234.567)
            if all(len(p) == 3 for p in parts[1:]) and parts[0]:
                text = "".join(parts)
            else:
                return None
        elif len(tail) == 3 and len(parts[0]) <= 3 and parts[0] and sep == ".":
            # 1.234 -> Tausendertrennung (Punkt ist im DE-Umfeld nie Dezimal
            # bei genau 3 Nachkommastellen)
            text = "".join(parts)
        elif len(tail) == 3 and sep == "," and len(parts[0]) <= 3 and parts[0]:
            # 1,234 -> im deutschen Kontext Tausendertrennung
            text = "".join(parts)
        else:
            text = parts[0] + "." + tail

    try:
        result = Decimal(sign + text)
    except InvalidOperation:
        return None
    return -result if negative else result


def parse_int(value: object) -> int | None:
    """Ganzzahl aus beliebiger Eingabe; ``None`` wenn nicht eindeutig."""
    dec = parse_decimal(value)
    if dec is None:
        return None
    if dec != dec.to_integral_value():
        return None
    try:
        return int(dec)
    except (ValueError, OverflowError):
        return None


def format_decimal(value: Decimal | None, decimals: int = 2) -> str:
    """Deutsche Darstellung ``1.234,56``; leerer String bei ``None``."""
    if value is None:
        return ""
    quantized = round(value, decimals)
    text = f"{quantized:,.{decimals}f}"
    # Gleichzeitiger Tausch der Trennzeichen (1,234.56 -> 1.234,56)
    return text.translate(str.maketrans({",": ".", ".": ","}))


# --------------------------------------------------------------------------
# Datum
# --------------------------------------------------------------------------

_MONTHS_DE = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "maerz": 3, "märz": 3,
    "mrz": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6,
    "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "okt": 10, "oct": 10, "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "dec": 12, "january": 1, "february": 2,
    "march": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12,
}

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{1,2})[.](\d{1,2})[.](\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})[.](\d{1,2})[.](\d{2})$"), "dmy2"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "slash"),
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "ymd"),
)


def parse_date(value: object, day_first: bool = True) -> date | None:
    """Datum aus Text/Excel-Wert; ``None`` wenn nicht eindeutig erkennbar.

    ``day_first`` steuert nur die Aufloesung von ``tt/mm/jjjj`` gegen
    ``mm/tt/jjjj``.  Ist der erste Wert > 12, entscheidet der Wert selbst.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = normalize_whitespace(str(value))
    if not text:
        return None
    text = text.replace(",", " ").strip()

    # "01. September 2026" / "1 Sep 2026"
    match = re.match(r"^(\d{1,2})\.?\s+([A-Za-zÄÖÜäöü]+)\.?\s+(\d{4})$", text)
    if match:
        month = _MONTHS_DE.get(match.group(2).lower())
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(1)))

    compact = text.replace(" ", "")
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.match(compact)
        if not match:
            continue
        a, b, c = (int(g) for g in match.groups())
        if kind == "ymd":
            return _safe_date(a, b, c)
        if kind == "dmy":
            return _safe_date(c, b, a)
        if kind == "dmy2":
            year = 2000 + c if c < 70 else 1900 + c
            return _safe_date(year, b, a)
        if kind == "slash":
            if a > 12:
                return _safe_date(c, b, a)
            if b > 12:
                return _safe_date(c, a, b)
            return _safe_date(c, b, a) if day_first else _safe_date(c, a, b)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def format_date(value: date | None) -> str:
    """SAP-uebliche deutsche Darstellung ``TT.MM.JJJJ``."""
    return value.strftime("%d.%m.%Y") if value else ""


# --------------------------------------------------------------------------
# Texte / Schluessel
# --------------------------------------------------------------------------

def normalize_whitespace(text: object) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).replace(" ", " ")).strip()


def normalize_material_number(value: object) -> str:
    """Materialnummer vereinheitlichen (Trimmen, fuehrende Nullen bei rein
    numerischen Nummern entfernen -- SAP zeigt sie je nach Customizing mal mit,
    mal ohne).  Nicht-numerische Nummern bleiben unveraendert (nur getrimmt).
    """
    text = normalize_whitespace(value).replace(" ", "")
    if not text:
        return ""
    if text.isdigit():
        stripped = text.lstrip("0")
        return stripped or "0"
    return text.upper()


def normalize_vendor_number(value: object) -> str:
    """Lieferantennummer vereinheitlichen (analog zur Materialnummer)."""
    return normalize_material_number(value)


_UOM_ALIASES = {
    "STK": "ST", "STCK": "ST", "STUECK": "ST", "STÜCK": "ST", "PCS": "ST",
    "PC": "ST", "PIECE": "ST", "EA": "ST", "EACH": "ST", "ST": "ST",
    "M": "M", "MTR": "M", "METER": "M", "METRE": "M",
    "MM": "MM", "CM": "CM", "KM": "KM",
    "KG": "KG", "KGM": "KG", "KILOGRAMM": "KG", "G": "G", "GR": "G",
    "L": "L", "LTR": "L", "LITER": "L",
    "M2": "M2", "QM": "M2", "M3": "M3", "CBM": "M3",
    "H": "STD", "STD": "STD", "HOUR": "STD", "STUNDE": "STD",
    "PA": "PA", "PAAR": "PA", "PAIR": "PA",
    "SET": "SET", "SATZ": "SET",
    "ROL": "ROL", "ROLLE": "ROL", "ROLL": "ROL",
    "PAK": "PAK", "PACK": "PAK", "PACKUNG": "PAK",
    "KAR": "KAR", "KARTON": "KAR", "BOX": "KAR",
}


def normalize_uom(value: object) -> str:
    """Mengeneinheit auf SAP-uebliche Kuerzel normieren.

    Unbekannte Einheiten werden lediglich getrimmt/grossgeschrieben, damit
    nichts stillschweigend verfaelscht wird.
    """
    text = normalize_whitespace(value).upper().replace(".", "")
    if not text:
        return ""
    return _UOM_ALIASES.get(text, text)


_CURRENCY_MAP = {
    "€": "EUR", "EUR": "EUR", "EURO": "EUR",
    "$": "USD", "USD": "USD", "US$": "USD",
    "£": "GBP", "GBP": "GBP",
    "CHF": "CHF", "SFR": "CHF",
    "JPY": "JPY", "¥": "JPY",
    "SEK": "SEK", "DKK": "DKK", "NOK": "NOK", "PLN": "PLN", "CZK": "CZK",
}


def detect_currency(text: object) -> str:
    """Findet einen Waehrungscode in beliebigem Text; ``""`` wenn keiner."""
    raw = normalize_whitespace(text).upper()
    if not raw:
        return ""
    for symbol in ("€", "£", "¥"):
        if symbol in raw:
            return _CURRENCY_MAP[symbol]
    for token in re.findall(r"[A-Z$]{2,4}", raw):
        if token in _CURRENCY_MAP:
            return _CURRENCY_MAP[token]
    if "$" in raw:
        return "USD"
    return ""


def similarity(a: str, b: str) -> float:
    """Aehnlichkeit zweier Strings zwischen 0.0 und 1.0 (Token-basiert).

    Wird fuer die Lieferanten-/Materialzuordnung verwendet.  Bewusst simpel und
    ohne externe Abhaengigkeit; das Ergebnis ist nur ein *Vorschlag*, ueber den
    der Anwender entscheidet.
    """
    from difflib import SequenceMatcher

    norm_a = re.sub(r"[^a-z0-9 ]", " ", a.lower())
    norm_b = re.sub(r"[^a-z0-9 ]", " ", b.lower())
    norm_a = normalize_whitespace(norm_a)
    norm_b = normalize_whitespace(norm_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()

    tokens_a = {t for t in norm_a.split() if len(t) > 2}
    tokens_b = {t for t in norm_b.split() if len(t) > 2}
    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        ratio = max(ratio, (ratio + jaccard) / 2)
    return round(ratio, 4)
