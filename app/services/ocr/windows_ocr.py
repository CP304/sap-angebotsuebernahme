"""OCR ueber die in Windows eingebaute Engine (``Windows.Media.Ocr``).

Warum dieses Backend zuerst?
----------------------------
Es ist auf jedem Windows 10/11 bereits vorhanden -- es muss kein zusaetzliches
Programm installiert und genehmigt werden.  Noetig ist lediglich das Python-
Paket ``winsdk`` (frueher ``winrt``), das die WinRT-Schnittstelle bereitstellt.
Fehlt es, meldet :meth:`WindowsOcrBackend.is_available` sauber ``False`` und
die Anwendung laeuft unveraendert weiter.

Einschraenkung -- ausdruecklich
-------------------------------
``Windows.Media.Ocr`` liefert **keine Konfidenzwerte** je Wort.  Deshalb bleibt
``OcrWord.confidence`` hier ``None`` ("unbekannt") und das Ergebnis traegt eine
entsprechende Warnung.  ``None`` darf nirgends als "sicher" ausgelegt werden;
mit diesem Backend ist eine Sichtpruefung durch den Anwender Pflicht.

Sprachpakete: Erkannt wird nur, wofuer in Windows ein Sprachpaket installiert
ist (Einstellungen -> Zeit und Sprache -> Sprache -> Optionales Sprachfeature
"Basis-Schrifterkennung").  Fehlt die gewuenschte Sprache, wird auf die
Benutzersprache ausgewichen und das gemeldet.
"""

from __future__ import annotations

import logging

from .base import OcrResult, OcrWord

logger = logging.getLogger(__name__)

__all__ = ["WindowsOcrBackend", "INSTALL_HINT"]

INSTALL_HINT = (
    "Windows-Texterkennung: 'pip install winsdk' ausfuehren.  Zusaetzlich muss "
    "in Windows unter 'Zeit und Sprache -> Sprache' das optionale Feature "
    "'Basis-Schrifterkennung' fuer die gewuenschte Sprache installiert sein."
)

#: Ohne Konfidenz vom Backend: dieser Hinweis gehoert in jedes Ergebnis
NO_CONFIDENCE_WARNING = (
    "Die Windows-Texterkennung liefert keine Sicherheitswerte je Wort.  Die "
    "erkannten Werte sind daher vollstaendig ungeprueft und muessen gesichtet "
    "werden."
)


def _import_winsdk():
    """Die benoetigten WinRT-Module laden (``winsdk`` oder alt ``winrt``).

    Rueckgabe ist ein Tupel der Module oder ``None``.  Bewusst breit
    abgesichert: auf Nicht-Windows-Systemen und bei unpassenden Paketversionen
    darf hier nichts nach aussen dringen.
    """
    for prefix in ("winsdk", "winrt"):
        try:
            ocr = __import__(f"{prefix}.windows.media.ocr", fromlist=["OcrEngine"])
            imaging = __import__(f"{prefix}.windows.graphics.imaging",
                                 fromlist=["BitmapDecoder"])
            streams = __import__(f"{prefix}.windows.storage.streams",
                                 fromlist=["InMemoryRandomAccessStream", "DataWriter"])
            globalization = __import__(f"{prefix}.windows.globalization",
                                       fromlist=["Language"])
        except Exception:  # noqa: BLE001 -- Paket fehlt oder passt nicht
            continue
        return ocr, imaging, streams, globalization
    return None


class WindowsOcrBackend:
    """Texterkennung ueber die Bordmittel von Windows."""

    name = "windows"
    label = "Windows-Texterkennung (Windows.Media.Ocr)"
    install_hint = INSTALL_HINT

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """Sind die WinRT-Module da und laesst sich eine Engine erzeugen?"""
        modules = _import_winsdk()
        if modules is None:
            return False
        ocr = modules[0]
        try:
            engine_class = getattr(ocr, "OcrEngine")
            return bool(engine_class.get_available_recognizer_languages())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Windows-OCR nicht verwendbar: %s", exc)
            return False

    # ------------------------------------------------------------------
    def recognize(self, image_bytes: bytes, language: str = "de") -> OcrResult:
        """Bild erkennen.  Fehler werden als Warnung geliefert, nicht geworfen."""
        result = OcrResult(backend_name=self.name)
        if not image_bytes:
            result.add_warning("Leeres Bild -- es konnte nichts erkannt werden.")
            return result

        modules = _import_winsdk()
        if modules is None:
            result.add_warning(f"Windows-Texterkennung nicht verfuegbar.  {INSTALL_HINT}")
            return result

        try:
            words, text, notes = self._run(modules, image_bytes, language)
        except Exception as exc:  # noqa: BLE001 -- WinRT-Fehler nie durchlassen
            logger.warning("Windows-OCR fehlgeschlagen: %s", exc, exc_info=True)
            result.add_warning(f"Windows-Texterkennung fehlgeschlagen: {exc}")
            return result

        result.words = words
        result.text = text
        for note in notes:
            result.add_warning(note)
        result.add_warning(NO_CONFIDENCE_WARNING)
        result.recompute_mean()   # bleibt None -- es gibt keine Konfidenzen
        return result

    # ------------------------------------------------------------------
    def _run(self, modules, image_bytes: bytes,
             language: str) -> tuple[list[OcrWord], str, list[str]]:
        """Der eigentliche WinRT-Ablauf (asynchron, hier synchron gefahren)."""
        import asyncio

        ocr, imaging, streams, globalization = modules
        notes: list[str] = []

        async def work():
            stream = streams.InMemoryRandomAccessStream()
            writer = streams.DataWriter(stream)
            writer.write_bytes(bytes(image_bytes))
            await writer.store_async()
            await writer.flush_async()
            writer.detach_stream()
            stream.seek(0)

            decoder = await imaging.BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()

            engine = None
            if language:
                try:
                    engine = ocr.OcrEngine.try_create_from_language(
                        globalization.Language(language))
                except Exception:  # noqa: BLE001 -- unbekanntes Sprachkuerzel
                    engine = None
            if engine is None:
                engine = ocr.OcrEngine.try_create_from_user_profile_languages()
                notes.append(
                    f"Fuer die Sprache '{language}' ist in Windows kein "
                    f"Schrifterkennungspaket installiert -- es wurde die "
                    f"Benutzersprache verwendet.")
            if engine is None:
                raise RuntimeError(
                    "Windows hat kein Schrifterkennungspaket installiert.")
            return await engine.recognize_async(bitmap)

        recognized = asyncio.run(work())

        words: list[OcrWord] = []
        lines: list[str] = []
        for line in getattr(recognized, "lines", None) or []:
            parts: list[str] = []
            for word in getattr(line, "words", None) or []:
                text = str(getattr(word, "text", "") or "").strip()
                if not text:
                    continue
                rect = getattr(word, "bounding_rect", None)
                x = float(getattr(rect, "x", 0.0) or 0.0)
                y = float(getattr(rect, "y", 0.0) or 0.0)
                width = float(getattr(rect, "width", 0.0) or 0.0)
                height = float(getattr(rect, "height", 0.0) or 0.0)
                words.append(OcrWord(text=text, x0=x, y0=y,
                                     x1=x + width, y1=y + height,
                                     confidence=None))
                parts.append(text)
            if parts:
                lines.append(" ".join(parts))

        text = "\n".join(lines)
        if not text:
            fallback = str(getattr(recognized, "text", "") or "")
            text = fallback
        return words, text, notes
