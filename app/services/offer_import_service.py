"""Fassade des Angebotsimports: Datei(en) oder Text -> :class:`Offer`.

Ablauf eines Imports::

    Datei -> Leser -> RawDocument
                       |
                       +-- Lieferantenprofil suchen (gelerntes Wissen)
                       +-- Kopfregeln  (Angebotsnummer, Datum, Waehrung, ...)
                       +-- Tabellenextraktion (Positionen aus Spalten)
                       +-- Freitextextraktion (nur falls zu wenig gefunden)
                       +-- Anhaenge rekursiv (E-Mail)
                       |
                       +-- Nachbearbeitung (Waehrung vererben, Preiseinheit,
                           Mengeneinheit normalisieren)
                       |
                       -> Offer inkl. extraction_notes und issues

Grundsatz: **Kein Wert wird erfunden.**  Was nicht erkannt wurde, bleibt leer
und traegt :class:`FieldOrigin.MISSING`; unsichere Treffer werden als
``UNCERTAIN`` markiert und in der Oberflaeche hervorgehoben.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config.settings import Settings
from ..models.enums import FieldOrigin, IssueSeverity, SourceKind
from ..models.issue import Issue
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..utils.parsing import normalize_uom
from .extraction.condition_rules import attach_conditions
from .extraction.email_merge import apply_email_header, apply_email_supplements
from .extraction.freetext_extractor import FreetextExtractor
from .extraction.header_rules import apply_header_matches, extract_header_fields, \
    label_value_text, missing_header_note
from .extraction.learning import learn_from_corrections
from .extraction.material_roles import compile_own_pattern, resolve_position_roles
from .extraction.profiles import (
    InMemoryProfileStore,
    VendorProfile,
    VendorProfileStore,
    apply_profile,
    match_profile,
    new_profile,
)
from .extraction.table_extractor import TableExtractor
from .readers import ReaderRegistry
from .readers.base import RawDocument

logger = logging.getLogger(__name__)

__all__ = ["OfferImportService"]

#: Ab wie vielen Tabellenpositionen wird auf die Freitextsuche verzichtet
_FREETEXT_THRESHOLD = 1

#: Laenge des im Angebot mitgefuehrten Rohtexts (Nachvollziehbarkeit)
_MAX_RAW_TEXT = 20000


class OfferImportService:
    """Verbindet Leser, Profile und Extraktion zu einem Importvorgang."""

    def __init__(self, settings: Settings,
                 profile_store: VendorProfileStore | None = None) -> None:
        self.settings = settings
        # Bewusst gegen ``None`` geprueft: ein *leerer* Profilspeicher ist
        # falsy, wuerde bei ``or`` also stillschweigend durch einen neuen
        # ersetzt -- und alles Gelernte landete im Nichts.
        self.profile_store: VendorProfileStore = (
            profile_store if profile_store is not None else InMemoryProfileStore())
        self.registry = ReaderRegistry(settings.extraction)
        self._last_document: RawDocument | None = None
        self._last_profile: VendorProfile | None = None
        #: Mails, deren Text erst nach dem Anhang ausgewertet wird
        self._deferred: list[tuple[RawDocument, str]] = []

    # ------------------------------------------------------------------
    # Auskunft
    # ------------------------------------------------------------------
    def can_import(self, path: str) -> bool:
        """Kann diese Datei ueberhaupt gelesen werden? (nur Endungspruefung)"""
        return self.registry.can_read(path)

    def supported_extensions(self) -> list[str]:
        return self.registry.supported_extensions()

    def last_document(self) -> RawDocument | None:
        """Das zuletzt gelesene Rohdokument -- Grundlage fuer das Lernen."""
        return self._last_document

    def last_profile(self) -> VendorProfile | None:
        return self._last_profile

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def import_file(self, path: str) -> Offer:
        """Eine Datei importieren (PDF/XLSX/CSV/EML/MSG/TXT/HTML)."""
        return self.import_files([path])

    def import_files(self, paths: list[str]) -> Offer:
        """Mehrere Dateien zu *einem* Angebot zusammenfassen.

        Typischer Fall: die Mail des Lieferanten plus die Preisliste als
        separater Anhang, den der Anwender einzeln gespeichert hat.
        """
        offer = Offer()
        if not paths:
            offer.issues.add(Issue("no_input", "Es wurde keine Datei ausgewaehlt.",
                                   IssueSeverity.ERROR, blocking=True))
            return offer

        documents: list[RawDocument] = []
        for path in paths:
            document = self.registry.read(str(path))
            documents.append(document)
            offer.source_files.append(str(path))
            logger.info("Import: %s (%d Tabellen, %d Anhaenge, %d Warnungen)",
                        Path(path).name, len(document.tables),
                        len(document.attachments), len(document.warnings))

        self._last_document = documents[0] if documents else None
        self._process_documents(documents, offer)
        return offer

    def import_text(self, text: str, source_name: str = "Eingefuegter Text") -> Offer:
        """Aus der Zwischenablage eingefuegten Text importieren."""
        offer = Offer()
        document = self.registry.read_text(text, source_name)
        self._last_document = document
        offer.source_files.append(source_name)
        self._process_documents([document], offer)
        return offer

    # ------------------------------------------------------------------
    # Lernen
    # ------------------------------------------------------------------
    def learn(self, original: Offer, corrected: Offer) -> VendorProfile | None:
        """Aus den Korrekturen des Anwenders lernen und das Profil sichern."""
        document = self._last_document
        if document is None:
            logger.info("Kein Dokument im Speicher -- es kann nichts gelernt werden.")
            return None
        try:
            profile = self._last_profile
            if profile is None:
                profile = self._find_profile(document, corrected) or new_profile(
                    corrected, document)
            profile = learn_from_corrections(original, corrected, document, profile)
            self.profile_store.save_profile(profile)
            self._last_profile = profile
            logger.info("Profil '%s' gespeichert (%d Spaltenregeln, %d Kopfregeln)",
                        profile.label, len(profile.column_map),
                        len(profile.header_regexes))
            return profile
        except Exception as exc:  # noqa: BLE001 -- Lernen darf nie den Ablauf stoppen
            logger.error("Lernvorgang fehlgeschlagen: %s", exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Ablauf
    # ------------------------------------------------------------------
    def _process_documents(self, documents: list[RawDocument], offer: Offer) -> None:
        raw_parts: list[str] = []
        # Mailtexte, deren Auswertung zurueckgestellt wird, weil erst der Anhang
        # feststehen muss (siehe _finish_deferred).
        self._deferred: list[tuple[RawDocument, str]] = []
        merge = self.settings.extraction.merge_email_and_attachment
        has_other_files = any(d.email is None for d in documents)

        for document in documents:
            offer.source_kind = _merge_source_kind(offer.source_kind, document.source_kind)
            if document.email is not None and offer.email is None:
                offer.email = document.email
            raw_parts.append(document.all_text())
            defer = bool(merge and document.email is not None
                         and (document.attachments or has_other_files))
            self._process_one(document, offer, prefix="", defer_email=defer)

        self._finish_deferred(offer)
        offer.raw_text = "\n\n".join(p for p in raw_parts if p)[:_MAX_RAW_TEXT]
        self._postprocess(offer)

    # ------------------------------------------------------------------
    def _finish_deferred(self, offer: Offer) -> None:
        """Mailtexte auswerten, nachdem die Anhaenge gelesen sind.

        Liegen Positionen aus dem Anhang (oder einer zweiten Datei) vor, wird
        der Mailtext als *Ergaenzung* dazu verstanden -- Kopfdaten und
        Positionsangaben werden zusammengefuehrt.  Gibt es keine, bleibt es
        beim bisherigen Verhalten: der Mailtext ist dann selbst die Quelle der
        Positionen.
        """
        deferred, self._deferred = getattr(self, "_deferred", []), []
        for document, header_text in deferred:
            matches = extract_header_fields(header_text, document.email,
                                            source=document.display_name)
            base = [p for p in offer.positions
                    if p.source_kind is not SourceKind.EMAIL_BODY]
            body = (document.email.body_text if document.email is not None
                    else document.text)
            if not base:
                # Kein Anhang, kein zweites Dokument, keine Positionen: die Mail
                # steht fuer sich -- also der gewohnte Ablauf.
                apply_header_matches(offer, matches)
                self._freetext_positions(document, offer)
                continue

            for note in apply_email_header(offer, matches, self.settings,
                                           source="Mailtext"):
                offer.add_note(note)
            if self.settings.extraction.apply_email_supplements:
                notes = apply_email_supplements(offer, body, self.settings,
                                                source="Mailtext")
                for note in notes:
                    offer.add_note(note)
                if notes:
                    offer.add_note(
                        f"Mail und Anhang wurden zu EINEM Angebot zusammengefuehrt: "
                        f"{len(notes)} Angabe(n) aus dem Mailtext uebernommen -- "
                        "die betroffenen Positionen sind im Herkunftshinweis "
                        "entsprechend gekennzeichnet.")
            else:
                offer.add_note("Ergaenzungen aus dem Mailtext wurden laut Einstellung "
                               "nicht angewendet (apply_email_supplements = aus).")

    def _freetext_positions(self, document: RawDocument, offer: Offer) -> None:
        """Positionen aus dem Fliesstext eines Dokuments nachtragen."""
        if not self.settings.extraction.enable_freetext_positions:
            return
        profile = self._last_profile
        freetext = FreetextExtractor(
            self.settings,
            decimal_style=(profile.decimal_style if profile else "auto"))
        result = freetext.extract(document)
        for note in result.notes:
            offer.add_note(f"{document.display_name}: {note}")
        if not result.positions:
            return
        self._check_material_roles(offer, result.positions)
        for position in result.positions:
            self._attach_position(offer, position, document, prefix="")
        offer.add_note(f"{document.display_name}: {len(result.positions)} Position(en) "
                       "aus Fliesstext abgeleitet (bitte pruefen).")

    def _process_one(self, document: RawDocument, offer: Offer, prefix: str,
                     defer_email: bool = False) -> None:
        """Ein Dokument (inkl. seiner Anhaenge) in das Angebot uebernehmen."""
        for warning in document.warnings:
            offer.issues.add(Issue("reader_warning", warning, IssueSeverity.WARNING,
                                   detail=document.display_name))

        text = document.all_text()
        # Die abgeschnittene Signatur enthaelt oft Firma und Ansprechpartner --
        # fuer die *Kopfdaten* ist sie wertvoll, fuer Positionen bleibt sie aussen vor.
        signature = document.meta.get("stripped_signature", "")
        # In Tabellenblaettern steht der Kopf als Beschriftung/Wert in
        # *benachbarten Zellen* ("Angebot Nr.:" in A4, der Wert in B4).  Im
        # zusammengefuegten Text stuende dazwischen ein Trennzeichen, an dem
        # jede Kopfregel scheitert -- deshalb werden die Paare eigens
        # aufgeloest und als saubere Zeilen mit angeboten.
        pairs = label_value_text(document.tables)
        header_text = "\n\n".join(part for part in (text, pairs, signature) if part)

        # 1. Profil suchen -- gelerntes Wissen hat Vorrang vor Standardregeln
        profile = self._find_profile(document, offer)
        if profile is not None:
            self._last_profile = profile
            profile.sample_count += 1
            apply_profile(profile, document, offer)

        # 2. Kopfdaten.  Bei einer Mail mit Anhang wird das zurueckgestellt:
        #    erst muss feststehen, was im Anhang steht, sonst laesst sich
        #    "die Mail gewinnt bei Widerspruch" gar nicht entscheiden.
        if not defer_email:
            matches = extract_header_fields(header_text, document.email,
                                            source=document.display_name)
            apply_header_matches(offer, matches)

        # 3. Positionen aus Tabellen
        table_extractor = TableExtractor(self.settings, profile)
        table_result = table_extractor.extract(document)
        for note in table_result.notes:
            offer.add_note(note)
        positions = list(table_result.positions)
        if positions:
            offer.add_note(f"{document.display_name}: {len(positions)} Position(en) "
                           f"aus Tabellenstruktur erkannt.")

        # 4. Freitext -- nur wenn die Tabellenerkennung zu wenig geliefert hat
        if (not defer_email and len(positions) < _FREETEXT_THRESHOLD
                and self.settings.extraction.enable_freetext_positions):
            freetext = FreetextExtractor(
                self.settings,
                decimal_style=(profile.decimal_style if profile else "auto"))
            result = freetext.extract(document)
            for note in result.notes:
                offer.add_note(f"{document.display_name}: {note}")
            positions.extend(result.positions)
            if result.positions:
                offer.add_note(f"{document.display_name}: "
                               f"{len(result.positions)} Position(en) aus Fliesstext "
                               "abgeleitet (bitte pruefen).")

        # 4b. Artikelnummern-Rollen pruefen: steht unsere Nummer wirklich in
        #     "Material"?  Vertauschtes wird getauscht -- mit Notiz, nie still.
        self._check_material_roles(offer, positions)

        for position in positions:
            self._attach_position(offer, position, document, prefix)

        # 5. Anhaenge rekursiv
        for attachment in document.attachments:
            name = attachment.meta.get("attachment_name") or attachment.display_name
            self._process_attachment(attachment, offer, name)

        # 6. Zurueckgestellte Mail vormerken (siehe _finish_deferred)
        if defer_email:
            self._deferred.append((document, header_text))

    def _process_attachment(self, document: RawDocument, offer: Offer,
                            name: str) -> None:
        before = len(offer.positions)
        self._process_one(document, offer, prefix=f"Anhang: {name}")
        added = len(offer.positions) - before
        for position in offer.positions[before:]:
            position.source_kind = SourceKind.EMAIL_ATTACHMENT
        offer.add_note(f"Anhang '{name}': {added} Position(en) uebernommen.")

    def _attach_position(self, offer: Offer, position: OfferPosition,
                         document: RawDocument, prefix: str) -> None:
        if prefix:
            position.source_kind = SourceKind.EMAIL_ATTACHMENT
            position.source_hint = f"{prefix} / {position.source_hint}".strip(" /")
        elif document.email is not None:
            position.source_kind = SourceKind.EMAIL_BODY
        else:
            position.source_kind = document.source_kind
        offer.positions.append(position)

    # ------------------------------------------------------------------
    def _check_material_roles(self, offer: Offer,
                              positions: list[OfferPosition]) -> None:
        """Vertauschte bzw. falsch beschriftete Artikelnummern richtigstellen.

        Die Spaltenzuordnung erledigt der :class:`TableExtractor`; hier geht es
        um die einzelne Position -- also auch um alles, was aus Freitext kommt.
        Jede Aenderung wird als ``UNCERTAIN`` markiert und protokolliert.
        """
        extraction = self.settings.extraction
        if not extraction.own_material_pattern_active or not extraction.swap_detection:
            return
        own_re = compile_own_pattern(extraction.own_material_pattern)
        changed = 0
        for position in positions:
            note = resolve_position_roles(position, own_re, extraction.swap_detection)
            if note:
                offer.add_note(note)
                changed += 1
        if changed:
            offer.add_note(
                f"{changed} Position(en): die beiden Artikelnummern wurden anhand "
                f"des eigenen Nummernkreises ({extraction.own_material_pattern}) "
                "neu zugeordnet -- bitte die gelb markierten Felder pruefen.")

    # ------------------------------------------------------------------
    def _find_profile(self, document: RawDocument, offer: Offer) -> VendorProfile | None:
        try:
            profiles = self.profile_store.load_profiles()
        except Exception as exc:  # noqa: BLE001
            logger.error("Profile konnten nicht geladen werden: %s", exc, exc_info=True)
            return None
        if not profiles:
            return None
        profile, score = match_profile(profiles, document, offer)
        if profile is not None:
            offer.add_note(f"Lieferantenprofil '{profile.label}' erkannt "
                           f"(Uebereinstimmung {score:.0%}).")
        return profile

    # ------------------------------------------------------------------
    # Nachbearbeitung
    # ------------------------------------------------------------------
    def _postprocess(self, offer: Offer) -> None:
        """Fehlende Standardwerte ergaenzen und das Ergebnis bewerten."""
        profile = self._last_profile
        defaults = self.settings.purchasing

        if not offer.currency:
            currencies = {p.currency for p in offer.positions if p.currency}
            if len(currencies) == 1:
                offer.set_field("currency", currencies.pop(), FieldOrigin.EXTRACTED)
                offer.add_note("Waehrung aus den Positionen uebernommen.")

        inherited = 0
        for position in offer.positions:
            if not position.currency and offer.currency:
                position.set_field("currency", offer.currency, FieldOrigin.MAPPED)
                inherited += 1
            if position.uom:
                position.set_field("uom", normalize_uom(position.uom),
                                   position.origin("uom"))
            elif profile is not None and profile.default_uom:
                position.set_field("uom", profile.default_uom, FieldOrigin.DEFAULT)
            if not position.price_unit:
                unit = (profile.default_price_unit if profile
                        and profile.default_price_unit else defaults.price_unit)
                if unit:
                    position.set_field("price_unit", int(unit), FieldOrigin.DEFAULT)
            if not position.valid_from and offer.valid_from:
                position.set_field("valid_from", offer.valid_from, FieldOrigin.MAPPED)
            position.purchasing_org = position.purchasing_org or defaults.purchasing_org
            position.plant = position.plant or defaults.plant
            if offer.vendor_number and not position.vendor_number:
                position.vendor_number = offer.vendor_number

        if inherited:
            offer.add_note(f"Waehrung '{offer.currency}' auf {inherited} Position(en) "
                           "vererbt.")

        # Zusatzkonditionen (Rabatt, Zuschlag, Fracht, Skonto) erkennen.  Erst
        # jetzt, weil dafuer sowohl der Angebotstext als auch die fertigen
        # Positionen vorliegen muessen -- Kopfkonditionen gelten nur fuer
        # Positionen ohne eigene Angabe.
        for note in attach_conditions(offer, self.settings):
            offer.add_note(note)

        offer.renumber()
        self._add_issues(offer)

        summary = (f"Import abgeschlossen: {len(offer.positions)} Position(en), "
                   f"Quelle {offer.source_kind.label}.")
        offer.add_note(summary)
        logger.info("%s Kopf: %s", summary,
                    {k: str(v) for k, v in offer.header_summary().items() if v != "-"})

    def _add_issues(self, offer: Offer) -> None:
        if not offer.positions:
            offer.issues.add(Issue(
                "no_positions",
                "Es konnte keine Position erkannt werden. Bitte die Datei pruefen "
                "oder die Positionen manuell erfassen.",
                IssueSeverity.ERROR, blocking=True))
        if not offer.currency:
            offer.issues.add(Issue(
                "currency_missing",
                "Die Waehrung wurde nicht erkannt und wird bewusst nicht geraten. "
                "Bitte im Kopf eintragen.",
                IssueSeverity.WARNING, field="currency"))
        note = missing_header_note(offer)
        if note:
            offer.issues.add(Issue("header_incomplete", note, IssueSeverity.WARNING))

        uncertain = sum(1 for p in offer.positions if p.uncertain_fields)
        if uncertain:
            offer.issues.add(Issue(
                "uncertain_positions",
                f"{uncertain} Position(en) enthalten unsicher erkannte Werte -- "
                "bitte die gelb markierten Felder pruefen.",
                IssueSeverity.INFO))


def _merge_source_kind(current: SourceKind, incoming: SourceKind) -> SourceKind:
    """Quellart des Angebots bestimmen, wenn mehrere Dateien beteiligt sind."""
    if current is SourceKind.MANUAL:
        return incoming
    if current is incoming:
        return current
    # Gemischte Quellen: die E-Mail ist die fuehrende Quelle
    if SourceKind.EMAIL_BODY in (current, incoming):
        return SourceKind.EMAIL_BODY
    return current
