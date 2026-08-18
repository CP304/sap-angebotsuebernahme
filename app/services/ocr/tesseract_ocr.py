"""OCR ueber Tesseract (``pytesseract`` + Tesseract-Programm).

Zwei Dinge muessen vorhanden sein -- das wird haeufig verwechselt:

1. das Python-Paket ``pytesseract`` (``pip install pytesseract``) und
2. das eigentliche **Programm** Tesseract-OCR samt Sprachdaten.
   Unter Windows ueblicherweise ueber das Installationspaket von UB Mannheim,
   danach muss ``tesseract.exe`` im PATH liegen (oder in den Einstellungen
   hinterlegt werden).

``is_available()`` prueft deshalb beides: das Modul *und* ob sich die
Programmversion abfragen laesst.

Vorteil gegenueber der Windows-Engine: Tesseract liefert je Wort eine
Konfidenz.  Damit laesst sich ehrlich sagen, welche Zelle unsicher ist -- und
genau das ist im Einkauf der springende Punkt.
"""

from __future__ import annotations

import logging
import os
import tempfile

from .base import OcrResult, OcrWord

logger = logging.getLogger(__name__)

__all__ = ["TesseractOcrBackend", "INSTALL_HINT", "LANGUAGE_CODES"]

INSTALL_HINT = (
    "Tesseract: 'pip install pytesseract' ausfuehren UND zusaetzlich das "
    "Programm Tesseract-OCR installieren (Windows: Installationspaket der "
    "UB Mannheim), danach muss 'tesseract.exe' ueber den PATH erreichbar sein.  "
    "Fuer deutsche Belege wird das Sprachpaket 'deu' benoetigt."
)

#: Kuerzel der Anwendung -> Sprachcode von Tesseract
LANGUAGE_CODES: dict[str, str] = {
    "de": "deu",
    "de-de": "deu",
    "deu": "deu",
    "en": "eng",
    "en-us": "eng",
    "eng": "eng",
    "fr": "fra",
    "it": "ita",
    "es": "spa",
    "nl": "nld",
    "pl": "pol",
    "cs": "ces",
}


def tesseract_language(language: str) -> str:
    """Sprachkuerzel der Anwendung in einen Tesseract-Code uebersetzen.

    Unbekannte Angaben werden unveraendert durchgereicht -- wer ``deu+eng``
    einstellt, meint das vermutlich so.
    """
    key = (language or "").strip().lower()
    if not key:
        return "deu"
    return LANGUAGE_CODES.get(key, key)


def _import_pytesseract():
    """``pytesseract`` laden -- oder ``None``, wenn es nicht installiert ist."""
    try:
        import pytesseract  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- fehlendes Paket darf nichts ausloesen
        return None
    return pytesseract


class TesseractOcrBackend:
    """Texterkennung ueber Tesseract."""

    name = "tesseract"
    label = "Tesseract-OCR (pytesseract)"
    install_hint = INSTALL_HINT

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """Modul UND Programm pruefen -- eines von beidem genuegt nicht."""
        module = _import_pytesseract()
        if module is None:
            return False
        try:
            version = module.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001 -- Binary fehlt/ist kaputt
            logger.debug("Tesseract-Programm nicht erreichbar: %s", exc)
            return False
        return bool(version)

    # ------------------------------------------------------------------
    def recognize(self, image_bytes: bytes, language: str = "de") -> OcrResult:
        """Bild erkennen.  Liefert Wortkoordinaten und Konfidenzen."""
        result = OcrResult(backend_name=self.name)
        if not image_bytes:
            result.add_warning("Leeres Bild -- es konnte nichts erkannt werden.")
            return result

        module = _import_pytesseract()
        if module is None:
            result.add_warning(f"Tesseract ist nicht verfuegbar.  {INSTALL_HINT}")
            return result

        path = ""
        try:
            handle, path = tempfile.mkstemp(suffix=".png", prefix="sap_ocr_")
            with os.fdopen(handle, "wb") as stream:
                stream.write(image_bytes)
            data = module.image_to_data(
                path,
                lang=tesseract_language(language),
                output_type=module.Output.DICT,
            )
        except Exception as exc:  # noqa: BLE001 -- nie nach aussen werfen
            logger.warning("Tesseract-OCR fehlgeschlagen: %s", exc, exc_info=True)
            result.add_warning(f"Tesseract-Texterkennung fehlgeschlagen: {exc}")
            return result
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        result.words = _words_from_data(data)
        result.text = _text_from_data(data)
        result.recompute_mean()
        if not result.words:
            result.add_warning(
                "Tesseract hat auf dieser Seite kein einziges Wort erkannt -- "
                "die Vorlage ist vermutlich zu schwach aufgeloest oder zu schief.")
        return result


def _words_from_data(data) -> list[OcrWord]:
    """Aus dem ``image_to_data``-Woerterbuch Woerter mit Rechteck bauen.

    Tesseract meldet die Konfidenz als Prozentwert; ``-1`` steht fuer "kein
    Wert" (z. B. bei Struktur-, nicht Textzeilen).  Daraus wird ``None``, nicht
    etwa 0.0 -- "unbekannt" ist nicht "schlecht".
    """
    if not isinstance(data, dict):
        return []
    texts = data.get("text") or []
    words: list[OcrWord] = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not text:
            continue
        left = _number(data.get("left"), index)
        top = _number(data.get("top"), index)
        width = _number(data.get("width"), index)
        height = _number(data.get("height"), index)
        confidence = _confidence(data.get("conf"), index)
        page = int(_number(data.get("page_num"), index) or 1) or 1
        words.append(OcrWord(text=text, x0=left, y0=top,
                             x1=left + width, y1=top + height,
                             confidence=confidence, page=page))
    return words


def _text_from_data(data) -> str:
    """Fliesstext aus den Wortdaten zusammensetzen (zeilenweise)."""
    if not isinstance(data, dict):
        return ""
    texts = data.get("text") or []
    lines_key = data.get("line_num") or []
    blocks_key = data.get("block_num") or []
    lines: list[str] = []
    current: list[str] = []
    previous: tuple = ()
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        marker = (
            blocks_key[index] if index < len(blocks_key) else 0,
            lines_key[index] if index < len(lines_key) else 0,
        )
        if previous and marker != previous and current:
            lines.append(" ".join(current))
            current = []
        previous = marker
        if text:
            current.append(text)
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def _number(values, index: int) -> float:
    """Zahl aus einer Spalte holen -- unbrauchbare Angaben werden 0.0."""
    try:
        return float(values[index])
    except (TypeError, ValueError, IndexError, KeyError):
        return 0.0


def _confidence(values, index: int) -> float | None:
    """Prozentkonfidenz in 0..1 umrechnen; ``-1`` wird zu ``None``."""
    try:
        raw = float(values[index])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if raw < 0:
        return None
    return max(0.0, min(1.0, raw / 100.0))
