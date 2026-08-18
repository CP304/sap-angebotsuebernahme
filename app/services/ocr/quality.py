"""Ehrlichkeit ueber die Qualitaet einer Texterkennung.

Der schwierigste Teil an OCR ist nicht das Erkennen, sondern das *Zugeben*.
Eine Texterkennung liefert immer etwas -- ob sie recht hat, sagt sie nicht von
allein.  Dieses Modul beantwortet daher zwei Fragen und beantwortet sie
konservativ:

1. **Wie sicher ist eine einzelne Zelle?**
   :func:`cell_confidences` legt neben die Tabelle eine gleich grosse Matrix
   mit der Konfidenz je Zelle (``None`` = unbekannt).  Bei mehreren Woertern
   in einer Zelle zaehlt das *schlechteste* -- eine Zelle ist nur so gut wie
   ihr unsicherstes Zeichen.

2. **Sieht eine Zelle nach einer verwechselten Ziffer aus?**
   :func:`suspicious_number` prueft, ob eine Zelle eine Zahl waere, wenn man
   die klassischen OCR-Verwechslungen (0/O, 1/l/I, 5/S, 8/B, 6/G) ersetzte.
   Ist das der Fall, wird das **gemeldet** -- und ausdruecklich *nicht*
   ersetzt.  Ein automatisch aus ``O`` gemachtes ``0`` waere ein erfundener
   Wert, und erfundene Werte gibt es in dieser Anwendung nicht.
"""

from __future__ import annotations

import logging
import re

from .base import OcrWord, mean_of

logger = logging.getLogger(__name__)

__all__ = [
    "CONFUSABLE_CHARACTERS",
    "suspicious_number",
    "cell_confidences",
    "uncertain_cells",
    "ocr_warning",
    "confidence_percent",
]

#: Typische Verwechslungen der Zeichenerkennung: Buchstabe -> gemeinte Ziffer.
#: Wird nur zum *Erkennen* eines Verdachts benutzt, nie zum Ersetzen.
CONFUSABLE_CHARACTERS: dict[str, str] = {
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "l": "1", "I": "1", "i": "1", "|": "1",
    "S": "5", "s": "5",
    "B": "8",
    "G": "6",
    "Z": "2", "z": "2",
}

#: So sieht eine Zahl in einem Angebot aus (deutsche und englische Schreibweise,
#: optional Vorzeichen, Prozent oder Waehrungszeichen)
_NUMBER_PATTERN = re.compile(r"^[+-]?[0-9]{1,3}(?:[.,\s][0-9]+)*\s*(?:%|EUR|€|\$)?$")


def _map_confusables(text: str) -> str:
    """Verwechselbare Buchstaben testweise durch Ziffern ersetzen."""
    return "".join(CONFUSABLE_CHARACTERS.get(char, char) for char in text)


def suspicious_number(text: str) -> str:
    """Verdacht auf eine verwechselte Ziffer -- Klartextbegruendung oder ``""``.

    Angeschlagen wird nur, wenn der Text **als Zahl gelesen werden koennte**,
    sobald man die typischen Verwechslungen ersetzt.  ``"1.2O0"`` faellt damit
    auf, ``"Schraube M8"`` nicht -- dort ergaebe die Ersetzung keine Zahl.
    """
    raw = (text or "").strip()
    if len(raw) < 2:
        return ""
    confused = [char for char in raw if char in CONFUSABLE_CHARACTERS]
    if not confused:
        return ""
    mapped = _map_confusables(raw)
    if sum(1 for char in mapped if char.isdigit()) < 2:
        return ""
    if not _NUMBER_PATTERN.match(mapped.strip()):
        return ""
    zeichen = ", ".join(sorted(set(confused)))
    return (f"'{raw}' koennte eine Zahl sein -- verwechselbare Zeichen: {zeichen}.  "
            f"Der Wert wurde NICHT automatisch geaendert, bitte im Beleg pruefen.")


def _normalize(token: str) -> str:
    """Token fuer den Vergleich vereinheitlichen."""
    return re.sub(r"\s+", "", (token or "")).strip()


def cell_confidences(rows: list[list[str]],
                     words: list[OcrWord]) -> list[list[float | None]]:
    """Konfidenzmatrix passend zu ``rows`` aufbauen.

    Die Zuordnung laeuft ueber den Worttext: Jede Zelle wird in Token zerlegt
    und je Token die *niedrigste* bekannte Konfidenz gleichlautender Woerter
    gesucht.  Bewusst pessimistisch -- kommt ein Wort mehrfach vor, gilt der
    schlechteste Fund.  Findet sich nichts, bleibt die Zelle ``None``
    ("unbekannt"), nie ein geschoenter Wert.
    """
    lookup: dict[str, float] = {}
    for word in words:
        key = _normalize(word.text)
        if not key or word.confidence is None:
            continue
        previous = lookup.get(key)
        if previous is None or word.confidence < previous:
            lookup[key] = word.confidence

    matrix: list[list[float | None]] = []
    for row in rows:
        line: list[float | None] = []
        for cell in row:
            found = [lookup[key] for key in
                     (_normalize(t) for t in (cell or "").split()) if key in lookup]
            line.append(min(found) if found else None)
        matrix.append(line)
    return matrix


def uncertain_cells(rows: list[list[str]], matrix: list[list[float | None]],
                    min_confidence: float, page: int = 0) -> list[dict]:
    """Alle Zellen auflisten, die der Anwender ansehen muss.

    Zwei Gruende fuehren auf die Liste:

    ``"konfidenz"``  Die Erkennung selbst meldet eine Sicherheit unterhalb
                     ``min_confidence``.
    ``"ziffern"``    Der Text sieht nach einer Zahl mit verwechselten Zeichen
                     aus (siehe :func:`suspicious_number`).
    """
    findings: list[dict] = []
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            text = (cell or "").strip()
            if not text:
                continue
            confidence = None
            if row_index < len(matrix) and column_index < len(matrix[row_index]):
                confidence = matrix[row_index][column_index]
            if confidence is not None and confidence < min_confidence:
                findings.append({
                    "page": page, "row": row_index, "column": column_index,
                    "text": text, "confidence": round(confidence, 3),
                    "reason": "konfidenz",
                    "hinweis": (f"Zelle '{text}' wurde nur mit "
                                f"{confidence * 100:.0f} % Sicherheit erkannt."),
                })
                continue
            note = suspicious_number(text)
            if note:
                findings.append({
                    "page": page, "row": row_index, "column": column_index,
                    "text": text,
                    "confidence": None if confidence is None else round(confidence, 3),
                    "reason": "ziffern", "hinweis": note,
                })
    return findings


def confidence_percent(value: float | None) -> str:
    """Konfidenz als Text -- ``None`` wird ehrlich zu "unbekannt"."""
    if value is None:
        return "unbekannt"
    return f"{value * 100:.0f} %"


def ocr_warning(mean_confidence: float | None, backend_name: str = "",
                low_count: int = 0) -> str:
    """Die Warnung, die der Anwender im Protokoll sehen muss."""
    sicherheit = confidence_percent(mean_confidence)
    quelle = f" ueber {backend_name}" if backend_name else ""
    text = (f"Text stammt aus Texterkennung (OCR{quelle}, mittlere Sicherheit "
            f"{sicherheit}) -- bitte alle Werte pruefen, besonders Zahlen.")
    if low_count:
        text += (f"  {low_count} Angabe(n) gelten als unsicher und sind gesondert "
                 f"aufgefuehrt.")
    return text


def mean_confidence(words: list[OcrWord]) -> float | None:
    """Mittlere Konfidenz einer Wortliste (``None`` = keine Werte vorhanden)."""
    return mean_of(word.confidence for word in words)
