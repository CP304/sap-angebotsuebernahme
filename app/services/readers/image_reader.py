"""Leser fuer Bilddateien: das abfotografierte oder eingescannte Angebot.

Der Alltag sieht so aus: Der Lieferant faxt, der Empfang scannt, oder der
Kollege fotografiert das Blatt mit dem Telefon und schickt es per Mail.  Bis
hierher konnte die Anwendung damit nichts anfangen.

Was dieser Leser tut
--------------------
1. Bild laden (ueber PyMuPDF -- das ist bereits Pflichtabhaengigkeit, es kommt
   also keine weitere hinzu).  Mehrseitige TIFF-Dateien werden Seite fuer
   Seite verarbeitet.
2. Durch die Texterkennung schicken (:mod:`app.services.ocr`).
3. Aus den erkannten Woertern mit ihren Koordinaten **dieselbe**
   Tabellenrekonstruktion fahren wie beim PDF -- der Block traegt
   ``origin="image-ocr"``.

Was dieser Leser NICHT tut
--------------------------
Raten.  Ist keine Erkennung installiert, kommt eine klare Meldung mit dem
Installationshinweis zurueck und kein leeres, scheinbar erfolgreiches Ergebnis.
Und auch mit Erkennung gilt: Jeder Wert ist ein Vorschlag, der gesichtet werden
muss.  Ein Foto mit Schatten, ein Stempel quer ueber der Preisspalte oder eine
handschriftliche Notiz sind fuer OCR nicht sicher lesbar -- das steht dann als
Warnung im Dokument.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...models.enums import SourceKind
from ..ocr import NO_BACKEND_HINT, best_backend, ocr_settings_of
from .base import DocumentReader, RawDocument
from .pdf_reader import (
    _dpi_of,
    ocr_page_into_document,
    render_page_png,
    tolerances_for,
)

logger = logging.getLogger(__name__)

__all__ = ["ImageReader", "IMAGE_EXTENSIONS", "IMAGE_OCR_ORIGIN", "NO_OCR_WARNING"]

#: Bildformate, die als Angebotsquelle in Frage kommen
IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")

#: Herkunftskennzeichen der aus einem Bild erkannten Bloecke
IMAGE_OCR_ORIGIN = "image-ocr"

NO_OCR_WARNING = (
    "Bilddateien koennen nur mit einer Texterkennung ausgewertet werden, und "
    "es ist keine installiert.\n"
    + NO_BACKEND_HINT
    + "\nOhne Erkennung bleibt nur, ein Text-PDF oder eine Excel-Datei "
      "anzufordern oder die Positionen manuell zu erfassen."
)

DISABLED_WARNING = (
    "Bilddateien koennen nur mit einer Texterkennung ausgewertet werden.  Die "
    "Texterkennung ist in den Einstellungen abgeschaltet (Einstellung "
    "'ocr.enabled')."
)


class ImageReader(DocumentReader):
    """Liest gescannte oder fotografierte Angebote ueber die Texterkennung."""

    extensions = IMAGE_EXTENSIONS

    def __init__(self, settings: object | None = None,
                 ocr_settings: object | None = None,
                 ocr_backend: object | None = None,
                 profile: object | None = None) -> None:
        self.ocr_settings = ocr_settings_of(ocr_settings if ocr_settings is not None
                                            else settings)
        self.ocr_backend = ocr_backend
        self.y_tolerance_factor, self.x_bin = tolerances_for(settings, profile)

    # ------------------------------------------------------------------
    def resolve_ocr_backend(self):
        """Backend bestimmen: fest vorgegeben oder erstes verfuegbares."""
        if self.ocr_backend is not None:
            if not bool(getattr(self.ocr_settings, "enabled", True)):
                return None
            return self.ocr_backend
        return best_backend(self.ocr_settings)

    # ------------------------------------------------------------------
    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.PDF)
        document.meta["image_source"] = True

        backend = self.resolve_ocr_backend()
        if backend is None:
            enabled = bool(getattr(self.ocr_settings, "enabled", True))
            document.add_warning(DISABLED_WARNING if not enabled else NO_OCR_WARNING)
            return document

        try:
            import fitz  # PyMuPDF
        except ImportError:
            document.add_warning(
                "PyMuPDF (fitz) ist nicht installiert -- Bilddateien koennen "
                "nicht geladen werden.  Bitte 'pip install pymupdf' ausfuehren.")
            return document

        try:
            handle = fitz.open(path)
        except Exception as exc:  # noqa: BLE001 -- kaputte Datei darf nicht killen
            document.add_warning(
                f"Bilddatei konnte nicht geoeffnet werden (moeglicherweise "
                f"beschaedigt oder kein unterstuetztes Format): {exc}")
            logger.warning("Bild nicht lesbar (%s): %s", path, exc)
            return document

        try:
            self._read_pages(handle, document, backend)
        except Exception as exc:  # noqa: BLE001
            document.add_warning(f"Bild nur teilweise auswertbar: {exc}")
            logger.warning("Bildauswertung unvollstaendig (%s): %s", path, exc,
                           exc_info=True)
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

        document.text = "\n".join(document.pages).strip()
        if not document.text and not document.tables:
            document.add_warning(
                "Auf dem Bild wurde kein Text gefunden.  Bitte pruefen, ob die "
                "Vorlage scharf, gerade und ausreichend hell ist -- oder das "
                "Angebot als Datei anfordern.")
        logger.info("Bild gelesen: %s (%d Seiten, %d Tabellenbloecke, %d Zeichen)",
                    Path(path).name, len(document.pages), len(document.tables),
                    len(document.text))
        return document

    # ------------------------------------------------------------------
    def _read_pages(self, handle, document: RawDocument, backend) -> None:
        """Alle Bildseiten erkennen (TIFF kann mehrere enthalten)."""
        page_count = int(getattr(handle, "page_count", 1) or 1)
        max_pages = max(1, int(getattr(self.ocr_settings, "max_pages", 20) or 1))
        document.meta["page_count"] = page_count
        if page_count > max_pages:
            document.add_warning(
                f"Die Datei hat {page_count} Seiten -- erkannt werden nur die "
                f"ersten {max_pages} (Einstellung 'max_pages').")

        dpi = _dpi_of(self.ocr_settings)
        preprocess = bool(getattr(self.ocr_settings, "preprocess", True))
        language = str(getattr(self.ocr_settings, "language", "de") or "de")

        for index in range(min(page_count, max_pages)):
            page_number = index + 1
            document.pages.append("")
            try:
                page = handle.load_page(index)
                image = render_page_png(page, dpi=dpi, preprocess=preprocess)
            except Exception as exc:  # noqa: BLE001
                document.add_warning(
                    f"Bildseite {page_number} konnte nicht aufbereitet werden: {exc}")
                continue
            if not image:
                document.add_warning(
                    f"Bildseite {page_number} lieferte keine Bilddaten.")
                continue

            try:
                result = backend.recognize(image, language)
            except Exception as exc:  # noqa: BLE001 -- fremder Code
                document.add_warning(
                    f"Texterkennung auf Bildseite {page_number} fehlgeschlagen: {exc}")
                logger.warning("OCR auf Bildseite %d fehlgeschlagen: %s",
                               page_number, exc, exc_info=True)
                continue

            ocr_page_into_document(
                document, result, page_number,
                ocr_settings=self.ocr_settings,
                y_tolerance_factor=self.y_tolerance_factor,
                x_bin=self.x_bin,
                scale=72.0 / float(dpi),
                origin=IMAGE_OCR_ORIGIN,
            )
