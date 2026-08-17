"""Abbild eines SAP-Einkaufsinfosatzes (ME11/ME12/ME13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..utils.parsing import format_date, format_decimal


@dataclass
class SapCondition:
    """Eine Kondition aus dem Konditionsbild des Infosatzes."""

    condition_type: str = ""        # z. B. PB00, RA01, FRA1
    description: str = ""
    amount: Decimal | None = None
    currency: str = ""
    price_unit: int | None = None
    uom: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    is_percentage: bool = False

    def display(self) -> str:
        if self.amount is None:
            return f"{self.condition_type} (kein Wert gelesen)"
        if self.is_percentage:
            value = f"{format_decimal(self.amount, 3)} %"
        else:
            value = f"{format_decimal(self.amount)} {self.currency}"
            if self.price_unit:
                value += f" / {self.price_unit} {self.uom}".rstrip()
        label = f"{self.condition_type} {self.description}".strip()
        return f"{label}: {value}"


@dataclass
class SapInfoRecord:
    """Ist-Zustand eines Infosatzes in SAP.

    ``exists=False`` bedeutet: In SAP wurde zu Material/Lieferant/EKorg/Werk
    kein Infosatz gefunden.  ``read_at is None`` bedeutet dagegen: Es wurde
    noch gar nicht gelesen -- diese beiden Faelle duerfen nie vermischt werden.
    """

    material_number: str = ""
    vendor_number: str = ""
    purchasing_org: str = ""
    plant: str = ""

    exists: bool = False
    info_record_number: str = ""
    info_category: str = "0"          # 0 = Normal, 1 = Lohnbearbeitung, ...

    # Konditionen / Preis
    price: Decimal | None = None
    price_unit: int | None = None
    currency: str = ""
    order_unit: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    conditions: list[SapCondition] = field(default_factory=list)

    # Einkaufsdaten
    lead_time_days: int | None = None
    min_order_qty: Decimal | None = None
    standard_qty: Decimal | None = None
    vendor_material_number: str = ""
    vendor_material_text: str = ""
    purchasing_group: str = ""
    tax_code: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    net_price_flag: bool = False

    # Meta
    read_at: datetime | None = None
    read_error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def was_read(self) -> bool:
        return self.read_at is not None

    @property
    def is_valid_on(self) -> bool:
        return True

    def valid_on(self, day: date) -> bool:
        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        return True

    def price_display(self) -> str:
        if not self.exists or self.price is None:
            return "-"
        text = f"{format_decimal(self.price)} {self.currency}".strip()
        if self.price_unit:
            text += f" / {self.price_unit} {self.order_unit}".rstrip()
        return text

    def validity_display(self) -> str:
        if not self.valid_from and not self.valid_to:
            return "-"
        return f"{format_date(self.valid_from) or '...'} – {format_date(self.valid_to) or '...'}"

    def summary(self) -> dict[str, str]:
        if not self.was_read:
            return {"Infosatz": "noch nicht gelesen"}
        if not self.exists:
            return {"Infosatz": "nicht vorhanden"}
        return {
            "Infosatz": self.info_record_number or "vorhanden",
            "SAP-Lieferant": self.vendor_number,
            "Aktueller Preis": self.price_display(),
            "Preiseinheit": f"{self.price_unit or '-'} {self.order_unit}".strip(),
            "Waehrung": self.currency or "-",
            "Gueltigkeit": self.validity_display(),
            "Lieferzeit": f"{self.lead_time_days} Tage" if self.lead_time_days is not None else "-",
            "Mindestmenge": format_decimal(self.min_order_qty, 3) or "-",
            "Lieferantenmaterial": self.vendor_material_number or "-",
        }
