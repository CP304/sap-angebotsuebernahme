"""Aufzaehlungstypen des Datenmodells."""

from __future__ import annotations

from enum import Enum


class SourceKind(str, Enum):
    """Woher stammt eine Angebotsposition?"""

    PDF = "pdf"
    EXCEL = "excel"
    EMAIL_BODY = "email_body"
    EMAIL_ATTACHMENT = "email_attachment"
    TEXT = "text"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        return {
            SourceKind.PDF: "PDF",
            SourceKind.EXCEL: "Excel",
            SourceKind.EMAIL_BODY: "E-Mail-Text",
            SourceKind.EMAIL_ATTACHMENT: "E-Mail-Anhang",
            SourceKind.TEXT: "Text",
            SourceKind.MANUAL: "manuell",
        }[self]


class FieldOrigin(str, Enum):
    """Herkunft eines einzelnen Feldwerts -- zentrale Vertrauensinformation.

    Es wird bewusst unterschieden, ob ein Wert sicher erkannt, nur geraten,
    aus einem Mapping ergaenzt, per Voreinstellung gesetzt oder vom Anwender
    eingetragen wurde.  Nur ``MANUAL``, ``EXTRACTED`` und ``MAPPED`` gelten als
    belastbar genug fuer eine automatische SAP-Schreibaktion.
    """

    MISSING = "missing"
    EXTRACTED = "extracted"
    UNCERTAIN = "uncertain"
    MAPPED = "mapped"
    DEFAULT = "default"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        return {
            FieldOrigin.MISSING: "nicht erkannt",
            FieldOrigin.EXTRACTED: "aus Angebot erkannt",
            FieldOrigin.UNCERTAIN: "unsicher erkannt",
            FieldOrigin.MAPPED: "ueber Mapping ergaenzt",
            FieldOrigin.DEFAULT: "Voreinstellung",
            FieldOrigin.MANUAL: "manuell erfasst",
        }[self]

    @property
    def is_trustworthy(self) -> bool:
        return self in (FieldOrigin.EXTRACTED, FieldOrigin.MAPPED, FieldOrigin.MANUAL)


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {IssueSeverity.INFO: 0, IssueSeverity.WARNING: 1, IssueSeverity.ERROR: 2}[self]

    @property
    def label(self) -> str:
        return {IssueSeverity.INFO: "Hinweis", IssueSeverity.WARNING: "Warnung",
                IssueSeverity.ERROR: "Fehler"}[self]


class PositionStatus(str, Enum):
    """Farbstatus je Position (siehe Statuskonzept)."""

    NOT_SELECTED = "not_selected"   # grau
    READY = "ready"                 # gruen
    CHECK = "check"                 # gelb
    ERROR = "error"                 # rot
    RUNNING = "running"             # blau
    DONE = "done"                   # gruen, verarbeitet
    SKIPPED = "skipped"             # grau, uebersprungen

    @property
    def label(self) -> str:
        return {
            PositionStatus.NOT_SELECTED: "nicht ausgewaehlt",
            PositionStatus.READY: "bereit",
            PositionStatus.CHECK: "Pruefung erforderlich",
            PositionStatus.ERROR: "Fehler",
            PositionStatus.RUNNING: "Verarbeitung laeuft",
            PositionStatus.DONE: "verarbeitet",
            PositionStatus.SKIPPED: "uebersprungen",
        }[self]


class InfoRecordAction(str, Enum):
    NONE = "none"
    CREATE = "create"      # ME11
    UPDATE = "update"      # ME12
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"    # Vorbedingung fehlt

    @property
    def label(self) -> str:
        return {
            InfoRecordAction.NONE: "-",
            InfoRecordAction.CREATE: "neu anlegen",
            InfoRecordAction.UPDATE: "aendern",
            InfoRecordAction.UNCHANGED: "unveraendert",
            InfoRecordAction.BLOCKED: "nicht moeglich",
        }[self]


class SourceListAction(str, Enum):
    NONE = "none"
    CREATE = "create"      # ME01 neuer Eintrag
    UPDATE = "update"      # ME01 bestehenden Eintrag anpassen
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"

    @property
    def label(self) -> str:
        return {
            SourceListAction.NONE: "-",
            SourceListAction.CREATE: "neu anlegen",
            SourceListAction.UPDATE: "aendern",
            SourceListAction.UNCHANGED: "unveraendert",
            SourceListAction.BLOCKED: "nicht moeglich",
        }[self]


class ResultState(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    SIMULATED = "simulated"   # Dry Run

    @property
    def label(self) -> str:
        return {
            ResultState.SUCCESS: "erfolgreich",
            ResultState.FAILED: "fehlgeschlagen",
            ResultState.SKIPPED: "uebersprungen",
            ResultState.SIMULATED: "simuliert (Dry Run)",
        }[self]

    @property
    def symbol(self) -> str:
        return {
            ResultState.SUCCESS: "✓",
            ResultState.FAILED: "✗",
            ResultState.SKIPPED: "○",
            ResultState.SIMULATED: "▷",
        }[self]
