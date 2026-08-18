"""Datenmodell des Angebots (Kopf + Positionen + Herkunft)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .document_plan import AttachmentRef, attachment_from_paths
from .enums import FieldOrigin, SourceKind
from .issue import IssueList
from .offer_position import OfferPosition

#: Kopffelder mit Herkunftsverfolgung
HEADER_FIELDS = (
    "vendor_name",
    "vendor_number",
    "offer_number",
    "offer_date",
    "currency",
    "valid_from",
    "valid_to",
    "incoterm",
    "incoterm_location",
    "payment_terms",
    "contact",
)


@dataclass
class EmailContext:
    """Zusatzinformationen, wenn das Angebot aus einer E-Mail stammt.

    Gerade im Einkaufsalltag kommen Preise haeufig als formlose Mail
    ("wir erhoehen zum 01.09. auf 12,85 EUR") statt als PDF-Angebot.  Diese
    Metadaten helfen bei der Lieferantenzuordnung und bei der Nachvollzieh-
    barkeit im Protokoll.
    """

    from_name: str = ""
    from_address: str = ""
    to: str = ""
    subject: str = ""
    sent: datetime | None = None
    body_text: str = ""
    attachment_names: list[str] = field(default_factory=list)
    #: Vollstaendige Pfade der abgelegten Anhaenge, sofern die Mail beim
    #: Einlesen ausgepackt wurde.  Nur was hier steht, laesst sich spaeter
    #: als Anlage an ein SAP-Objekt haengen -- ein blosser Dateiname reicht
    #: dafuer nicht.
    attachment_paths: list[str] = field(default_factory=list)
    message_id: str = ""

    @property
    def sender_domain(self) -> str:
        if "@" not in self.from_address:
            return ""
        return self.from_address.rsplit("@", 1)[1].lower().strip(">")


@dataclass
class Offer:
    """Ein eingelesenes Lieferantenangebot."""

    # -- Kopf ------------------------------------------------------------
    vendor_name: str = ""
    vendor_number: str = ""
    offer_number: str = ""
    offer_date: date | None = None
    currency: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    incoterm: str = ""
    incoterm_location: str = ""
    payment_terms: str = ""
    contact: str = ""

    # -- Positionen ------------------------------------------------------
    positions: list[OfferPosition] = field(default_factory=list)

    # -- Herkunft --------------------------------------------------------
    source_files: list[str] = field(default_factory=list)
    source_kind: SourceKind = SourceKind.MANUAL
    email: EmailContext | None = None
    imported_at: datetime = field(default_factory=datetime.now)
    raw_text: str = ""

    #: Dokument, das als Anlage an die erzeugten SAP-Objekte gehaengt wird.
    #: Wird von :meth:`resolve_attachment` ermittelt und dort zwischengelegt.
    attachment: AttachmentRef | None = None

    # -- Bewertung -------------------------------------------------------
    field_origins: dict[str, FieldOrigin] = field(default_factory=dict)
    issues: IssueList = field(default_factory=IssueList)
    extraction_notes: list[str] = field(default_factory=list)
    #: Kaufmaennische Klauseln aus dem Fliesstext (Legierungszuschlag,
    #: Preisgleitklausel, Skonto ...).  Sie werden NIE in den Preis
    #: eingerechnet -- siehe app/services/extraction/clauses.py.
    clauses: list = field(default_factory=list)

    # ------------------------------------------------------------------
    def set_field(self, name: str, value, origin: FieldOrigin) -> None:
        setattr(self, name, value)
        self.field_origins[name] = origin

    def origin(self, name: str) -> FieldOrigin:
        if name in self.field_origins:
            return self.field_origins[name]
        return FieldOrigin.EXTRACTED if getattr(self, name, None) not in (None, "") else FieldOrigin.MISSING

    def set_if_empty(self, name: str, value, origin: FieldOrigin) -> bool:
        """Kopffeld nur setzen, wenn es noch leer ist.  Gibt True bei Aenderung."""
        current = getattr(self, name, None)
        if current in (None, "") and value not in (None, ""):
            self.set_field(name, value, origin)
            return True
        return False

    # ------------------------------------------------------------------
    # Anlage (Angebotsdokument fuer SAP)
    # ------------------------------------------------------------------
    def resolve_attachment(self) -> AttachmentRef:
        """Welches Dokument soll am erzeugten SAP-Objekt haengen?

        Reihenfolge: zuerst die eingelesene Datei (``source_files``), dann der
        abgelegte E-Mail-Anhang.  Gibt es weder das eine noch das andere --
        etwa bei eingefuegtem Text --, wird das als Klartext gemeldet und
        nicht stillschweigend uebergangen.
        """
        pfade = [p for p in self.source_files if p]
        if self.email is not None:
            pfade.extend(p for p in self.email.attachment_paths if p)
        hinweis = ""
        if not pfade:
            if self.source_kind is SourceKind.TEXT:
                hinweis = "eingefuegter Text ohne Datei"
            elif self.email is not None and self.email.attachment_names:
                hinweis = ("die E-Mail nennt Anhaenge, deren Dateien aber nicht "
                           "abgelegt wurden")
            elif self.email is not None:
                hinweis = "E-Mail-Text ohne Anhang"
        ref = attachment_from_paths(pfade, hinweis)
        self.attachment = ref
        return ref

    # ------------------------------------------------------------------
    @property
    def source_label(self) -> str:
        if self.email:
            return f"E-Mail: {self.email.subject or '(ohne Betreff)'}"
        if self.source_files:
            return Path(self.source_files[0]).name
        return "manuell erfasst"

    @property
    def selected_positions(self) -> list[OfferPosition]:
        return [p for p in self.positions if p.selected]

    @property
    def processable_positions(self) -> list[OfferPosition]:
        return [p for p in self.positions if p.is_processable]

    @property
    def missing_header_fields(self) -> list[str]:
        """Pflicht-Kopffelder, die nicht erkannt wurden."""
        required = ("vendor_name", "offer_number", "offer_date", "currency")
        return [f for f in required if not getattr(self, f)]

    def renumber(self) -> None:
        """Fortlaufende Positionsnummern vergeben, wo keine erkannt wurden."""
        for index, position in enumerate(self.positions, start=1):
            if not position.position_number:
                position.set_field("position_number", f"{index * 10}", FieldOrigin.DEFAULT)

    def add_note(self, note: str) -> None:
        if note and note not in self.extraction_notes:
            self.extraction_notes.append(note)

    def header_summary(self) -> dict[str, str]:
        from ..utils.parsing import format_date

        return {
            "Lieferant": self.vendor_name or "-",
            "Lieferantennummer": self.vendor_number or "-",
            "Angebotsnummer": self.offer_number or "-",
            "Angebotsdatum": format_date(self.offer_date) or "-",
            "Waehrung": self.currency or "-",
            "Gueltig ab": format_date(self.valid_from) or "-",
            "Gueltig bis": format_date(self.valid_to) or "-",
            "Incoterm": " ".join(x for x in (self.incoterm, self.incoterm_location) if x) or "-",
            "Zahlungsbedingungen": self.payment_terms or "-",
            "Ansprechpartner": self.contact or "-",
            "Quelle": self.source_label,
        }
