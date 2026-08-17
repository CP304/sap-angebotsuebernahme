"""Leser fuer ZIP-Archive.

Im Einkaufsalltag ist das ZIP eine sehr haeufige Verpackung: Der Lieferant
legt Anschreiben (PDF) und Preisliste (Excel) zusammen und schickt ein
Archiv.  Der Leser entpackt es in ein temporaeres Verzeichnis, liest jede
enthaltene Datei ueber dieselbe Registry wie sonst auch und haengt die
Ergebnisse als ``attachments`` an *ein* :class:`RawDocument`.

Damit sieht ein ZIP fuer alles Nachgelagerte genauso aus wie eine E-Mail mit
Anhaengen -- die vorhandene Zusammenfuehrungslogik greift ohne Aenderung.

Sicherheit
----------
Ein Archiv ist Fremddatei; entpackt wird deshalb nur unter Auflagen:

* **Zip-Slip** -- Eintraege mit ``..`` oder absolutem Pfad (auch mit
  Laufwerksbuchstabe) werden verworfen.  Zusaetzlich wird der Zielpfad nach
  dem Aufloesen noch einmal gegen das Zielverzeichnis geprueft.  Es wird nie
  ausserhalb geschrieben.
* **Zip-Bombe** -- Gesamtgroesse (:data:`_MAX_TOTAL_BYTES`) und Anzahl der
  Eintraege (:data:`_MAX_ENTRIES`) sind gedeckelt.  Gezaehlt wird die
  *tatsaechlich* geschriebene Menge, nicht die im Verzeichnis angegebene.
* **Kennwortgeschuetzte Archive** -- klare Meldung statt Ausnahme.
* **Verschachtelte Archive** -- hoechstens eine Ebene tief
  (:data:`_MAX_DEPTH`), danach eine Warnung statt einer Endlosschleife.

Nicht unterstuetzt: RAR und 7z (proprietaere bzw. nicht in der
Standardbibliothek enthaltene Verfahren) sowie mehrteilige ZIP-Archive.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from ...models.enums import SourceKind
from .base import DocumentReader, RawDocument

logger = logging.getLogger(__name__)

__all__ = ["ArchiveReader"]

#: Hoechstens so viele Eintraege werden entpackt
_MAX_ENTRIES = 200

#: Hoechstens so viele Bytes werden insgesamt entpackt (Schutz vor Zip-Bombe)
_MAX_TOTAL_BYTES = 200 * 1024 * 1024

#: Ein ZIP im ZIP ist noch plausibel, tiefer wird es zum Selbstzweck
_MAX_DEPTH = 2

#: Blockgroesse beim Kopieren -- so kann mitgezaehlt werden
_CHUNK = 64 * 1024

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _is_unsafe_entry(name: str) -> bool:
    """Zip-Slip-Pruefung: absoluter Pfad oder Ausbruch ueber ``..``?"""
    if not name:
        return True
    normalisiert = name.replace("\\", "/")
    if normalisiert.startswith("/") or normalisiert.startswith("//"):
        return True
    # Laufwerksbuchstabe (C:/...) oder UNC-Pfad
    if re.match(r"^[A-Za-z]:", normalisiert):
        return True
    return any(teil == ".." for teil in PurePosixPath(normalisiert).parts)


def _safe_filename(name: str) -> str:
    """Nur den Dateinamen uebernehmen und von Sonderzeichen befreien."""
    basis = PurePosixPath(name.replace("\\", "/")).name
    bereinigt = _UNSAFE_NAME.sub("_", basis).strip(" .")
    return bereinigt or "datei.bin"


class ArchiveReader(DocumentReader):
    """Entpackt ZIP-Archive und liest den Inhalt ueber die Registry."""

    extensions = (".zip",)

    def __init__(self,
                 attachment_reader: Callable[[str], RawDocument] | None = None,
                 can_read: Callable[[str], bool] | None = None,
                 temp_dir: str | None = None) -> None:
        #: Wird von der Registry gesetzt -- genau wie beim ``EmailReader``
        self.attachment_reader = attachment_reader
        #: Prueft, ob die Registry eine Endung ueberhaupt kennt
        self.can_read_entry = can_read
        self.temp_dir = temp_dir
        #: Laufende Schachtelungstiefe (ZIP im ZIP)
        self._depth = 0

    # ------------------------------------------------------------------
    def read(self, path: str) -> RawDocument:
        document = RawDocument(source_path=str(path), source_kind=SourceKind.TEXT)
        document.meta["leser"] = "zip"

        if self._depth >= _MAX_DEPTH:
            document.add_warning(
                f"Das Archiv '{Path(path).name}' ist zu tief verschachtelt "
                f"(mehr als {_MAX_DEPTH - 1} Ebene(n)) und wird nicht weiter "
                "entpackt.")
            return document

        self._depth += 1
        ziel: Path | None = None
        try:
            ziel = Path(self.temp_dir or tempfile.mkdtemp(prefix="angebot_archiv_"))
            ziel.mkdir(parents=True, exist_ok=True)
            self._read_into(path, ziel, document)
        finally:
            self._depth -= 1
            if ziel is not None and self.temp_dir is None:
                shutil.rmtree(ziel, ignore_errors=True)
        return document

    # ------------------------------------------------------------------
    def _read_into(self, path: str, ziel: Path, document: RawDocument) -> None:
        try:
            archiv = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            document.add_warning("Das ZIP-Archiv ist beschaedigt oder unvollstaendig.")
            return
        except OSError as fehler:
            document.add_warning(f"Die Datei konnte nicht gelesen werden: {fehler}")
            return

        uebersprungen: list[str] = []
        gelesen: list[str] = []
        try:
            with archiv:
                eintraege = [e for e in archiv.infolist() if not e.is_dir()]
                if not eintraege:
                    document.add_warning("Das ZIP-Archiv enthaelt keine Dateien.")
                    return
                if len(eintraege) > _MAX_ENTRIES:
                    document.add_warning(
                        f"Das Archiv enthaelt {len(eintraege)} Dateien -- mehr als die "
                        f"zulaessigen {_MAX_ENTRIES}. Es wird nicht entpackt.")
                    return

                gesamt = 0
                for eintrag in eintraege:
                    name = eintrag.filename
                    if _is_unsafe_entry(name):
                        document.add_warning(
                            f"Eintrag '{name}' wurde verworfen: Der Pfad zeigt aus dem "
                            "Archiv heraus (moeglicher Angriff).")
                        uebersprungen.append(name)
                        continue
                    if eintrag.flag_bits & 0x1:
                        document.add_warning(
                            "Das Archiv ist kennwortgeschuetzt und kann nicht "
                            "gelesen werden.")
                        return
                    if self.can_read_entry is not None and not self.can_read_entry(name):
                        endung = PurePosixPath(name).suffix or "ohne Endung"
                        document.add_warning(
                            f"Datei '{name}' im Archiv wird nicht ausgewertet "
                            f"({endung} ist kein bekanntes Angebotsformat).")
                        uebersprungen.append(name)
                        continue

                    dateipfad = self._entpacke(archiv, eintrag, ziel, document,
                                               gesamt)
                    if dateipfad is None:
                        return                      # Groessenlimit ueberschritten
                    gesamt += dateipfad.stat().st_size

                    kind = self._lies(str(dateipfad))
                    kind.meta["attachment_name"] = name
                    kind.source_kind = SourceKind.EMAIL_ATTACHMENT
                    document.attachments.append(kind)
                    gelesen.append(name)
                    for warnung in kind.warnings:
                        document.add_warning(f"Archivdatei '{name}': {warnung}")
        except Exception as fehler:  # noqa: BLE001 -- nie eine Ausnahme nach aussen
            document.add_warning(f"Das Archiv konnte nur teilweise gelesen werden: "
                                 f"{fehler}")
            logger.warning("ZIP unvollstaendig gelesen (%s): %s", path, fehler,
                           exc_info=True)

        document.meta.update({
            "archiv_gelesen": gelesen,
            "archiv_uebersprungen": uebersprungen,
        })
        if not document.attachments and not document.warnings:
            document.add_warning("Aus dem Archiv konnte nichts ausgewertet werden.")
        logger.info("ZIP gelesen: %s (%d Datei(en), %d uebersprungen)",
                    Path(path).name, len(gelesen), len(uebersprungen))

    # ------------------------------------------------------------------
    def _entpacke(self, archiv: zipfile.ZipFile, eintrag: zipfile.ZipInfo,
                  ziel: Path, document: RawDocument, bisher: int) -> Path | None:
        """Einen Eintrag schreiben und dabei die Gesamtgroesse mitzaehlen.

        Rueckgabe ``None`` bedeutet: Limit ueberschritten, Abbruch.
        """
        dateipfad = ziel / _safe_filename(eintrag.filename)
        # Zweiter Riegel: der aufgeloeste Pfad muss im Zielverzeichnis liegen
        try:
            aufgeloest = dateipfad.resolve()
            basis = ziel.resolve()
            if basis != aufgeloest and basis not in aufgeloest.parents:
                document.add_warning(
                    f"Eintrag '{eintrag.filename}' wurde verworfen: Zielpfad "
                    "liegt ausserhalb des Arbeitsverzeichnisses.")
                return None
        except OSError:
            pass

        zaehler = 1
        while dateipfad.exists():
            stamm = _safe_filename(eintrag.filename)
            dateipfad = ziel / f"{Path(stamm).stem}_{zaehler}{Path(stamm).suffix}"
            zaehler += 1

        geschrieben = 0
        with archiv.open(eintrag) as quelle, dateipfad.open("wb") as ausgabe:
            while True:
                block = quelle.read(_CHUNK)
                if not block:
                    break
                geschrieben += len(block)
                if bisher + geschrieben > _MAX_TOTAL_BYTES:
                    ausgabe.close()
                    dateipfad.unlink(missing_ok=True)
                    document.add_warning(
                        f"Das Archiv ist entpackt groesser als "
                        f"{_MAX_TOTAL_BYTES // (1024 * 1024)} MB und wird aus "
                        "Sicherheitsgruenden nicht weiter verarbeitet.")
                    return None
                ausgabe.write(block)
        return dateipfad

    # ------------------------------------------------------------------
    def _lies(self, path: str) -> RawDocument:
        """Eine entpackte Datei ueber die Registry lesen."""
        if self.attachment_reader is None:
            document = RawDocument(source_path=path,
                                   source_kind=SourceKind.EMAIL_ATTACHMENT)
            document.add_warning("Archivinhalte koennen in dieser Konfiguration "
                                 "nicht gelesen werden.")
            return document
        try:
            return self.attachment_reader(path)
        except Exception as fehler:  # noqa: BLE001
            document = RawDocument(source_path=path,
                                   source_kind=SourceKind.EMAIL_ATTACHMENT)
            document.add_warning(f"Archivdatei nicht lesbar: {fehler}")
            logger.warning("Archivdatei %s nicht lesbar: %s", path, fehler,
                           exc_info=True)
            return document
