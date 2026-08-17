"""Abbild des SAP-Orderbuchs (ME01/ME03)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..utils.parsing import format_date


@dataclass
class SourceListEntry:
    """Eine Orderbuchzeile."""

    vendor_number: str = ""
    plant: str = ""
    purchasing_org: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    fixed: bool = False              # Feste Bezugsquelle (Kennzeichen "Fix")
    blocked: bool = False            # Gesperrt
    mrp_indicator: str = ""          # Disposition: "", "1", "2"
    agreement: str = ""              # Rahmenvertrag/Kontrakt
    agreement_item: str = ""
    order_unit: str = ""
    row_index: int = -1              # Zeilenindex in der SAP-Tabelle

    def valid_on(self, day: date) -> bool:
        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        return True

    def display(self) -> str:
        flags = []
        if self.fixed:
            flags.append("fix")
        if self.blocked:
            flags.append("gesperrt")
        if self.mrp_indicator:
            flags.append(f"Dispo {self.mrp_indicator}")
        if self.agreement:
            flags.append(f"Vertrag {self.agreement}")
        suffix = f" ({', '.join(flags)})" if flags else ""
        return (
            f"{self.vendor_number} | Werk {self.plant} | "
            f"{format_date(self.valid_from) or '...'} – {format_date(self.valid_to) or '...'}{suffix}"
        )


@dataclass
class SapSourceList:
    """Orderbuch eines Materials in einem Werk."""

    material_number: str = ""
    plant: str = ""

    exists: bool = False              # mindestens ein Eintrag vorhanden
    entries: list[SourceListEntry] = field(default_factory=list)

    read_at: datetime | None = None
    read_error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def was_read(self) -> bool:
        return self.read_at is not None

    def entries_for_vendor(self, vendor_number: str) -> list[SourceListEntry]:
        return [e for e in self.entries if e.vendor_number == vendor_number]

    def active_entry_for(self, vendor_number: str, day: date) -> SourceListEntry | None:
        for entry in self.entries_for_vendor(vendor_number):
            if entry.valid_on(day):
                return entry
        return None

    def has_other_fixed_vendor(self, vendor_number: str, day: date) -> SourceListEntry | None:
        """Anderer Lieferant ist als feste Bezugsquelle hinterlegt?"""
        for entry in self.entries:
            if entry.vendor_number != vendor_number and entry.fixed and entry.valid_on(day):
                return entry
        return None

    def summary(self) -> dict[str, str]:
        if not self.was_read:
            return {"Orderbuch": "noch nicht gelesen"}
        if not self.exists:
            return {"Orderbuch": "kein Eintrag vorhanden"}
        return {
            "Orderbuch": f"{len(self.entries)} Eintrag/Eintraege",
            "Eintraege": "\n".join(e.display() for e in self.entries),
        }
