"""Datenmodell einer Angebotsposition."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..utils.parsing import format_date, format_decimal
from .enums import (
    FieldOrigin,
    InfoRecordAction,
    PositionStatus,
    SourceKind,
    SourceListAction,
)
from .issue import IssueList
from .sap_info_record import SapInfoRecord
from .sap_source_list import SapSourceList

_ID_COUNTER = itertools.count(1)

#: Felder, die aus dem Angebot stammen und deren Herkunft nachgehalten wird.
TRACKED_FIELDS = (
    "position_number",
    "material_number",
    "vendor_material_number",
    "description",
    "quantity",
    "uom",
    "price",
    "price_unit",
    "currency",
    "min_order_qty",
    "lead_time_days",
    "valid_from",
    "remarks",
)


@dataclass
class OfferPosition:
    """Eine Position aus dem Lieferantenangebot inkl. SAP-Abgleich."""

    # -- Identitaet ------------------------------------------------------
    uid: int = field(default_factory=lambda: next(_ID_COUNTER))
    source_kind: SourceKind = SourceKind.MANUAL
    source_hint: str = ""          # z. B. "Seite 2, Zeile 14" / "Anhang: preise.xlsx"
    raw_text: str = ""             # Originalzeile -- fuer Nachvollziehbarkeit

    # -- Angebotsdaten ---------------------------------------------------
    position_number: str = ""
    material_number: str = ""
    vendor_material_number: str = ""
    description: str = ""
    quantity: Decimal | None = None
    uom: str = ""
    price: Decimal | None = None
    price_unit: int | None = None
    currency: str = ""
    min_order_qty: Decimal | None = None
    lead_time_days: int | None = None
    valid_from: date | None = None
    remarks: str = ""

    # -- Zuordnung -------------------------------------------------------
    vendor_number: str = ""        # aufgeloester SAP-Lieferant (Kopf oder Position)
    purchasing_org: str = ""
    plant: str = ""

    # -- Benutzerentscheidungen -----------------------------------------
    selected: bool = True
    do_info_record: bool = True
    do_source_list: bool = False
    do_contract: bool = False          # Mengenkontrakt (ME31K/ME32K)
    do_purchase_order: bool = False    # Bestellung (ME21N/ME22N)

    # -- Belegspezifische Zusatzangaben ---------------------------------
    contract_quantity: Decimal | None = None   # Zielmenge Mengenkontrakt
    order_quantity: Decimal | None = None      # Bestellmenge
    delivery_date: date | None = None          # Lieferdatum Bestellung
    account_assignment: str = ""               # Kontierungstyp (leer = Lager)
    cost_center: str = ""
    gl_account: str = ""
    requisitioner: str = ""

    # -- SAP-Ist-Zustand -------------------------------------------------
    sap_info_record: SapInfoRecord | None = None
    sap_source_list: SapSourceList | None = None
    sap_loaded: bool = False
    material_exists: bool | None = None      # None = noch nicht geprueft
    vendor_exists: bool | None = None
    sap_material_description: str = ""

    # -- Bewertung -------------------------------------------------------
    field_origins: dict[str, FieldOrigin] = field(default_factory=dict)
    issues: IssueList = field(default_factory=IssueList)
    info_record_action: InfoRecordAction = InfoRecordAction.NONE
    source_list_action: SourceListAction = SourceListAction.NONE
    status: PositionStatus = PositionStatus.CHECK
    result_text: str = ""

    # -- Ergebnisse der SAP-Verarbeitung --------------------------------
    created_info_record: str = ""      # Infosatznummer
    created_contract: str = ""         # Kontraktnummer + Position
    created_purchase_order: str = ""   # Bestellnummer + Position

    # ------------------------------------------------------------------
    # Herkunft
    # ------------------------------------------------------------------
    def set_field(self, name: str, value: Any, origin: FieldOrigin) -> None:
        """Feld setzen und Herkunft protokollieren."""
        setattr(self, name, value)
        self.field_origins[name] = origin

    def origin(self, name: str) -> FieldOrigin:
        if name in self.field_origins:
            return self.field_origins[name]
        return FieldOrigin.EXTRACTED if getattr(self, name, None) not in (None, "") else FieldOrigin.MISSING

    def mark_manual(self, name: str) -> None:
        self.field_origins[name] = FieldOrigin.MANUAL

    @property
    def uncertain_fields(self) -> list[str]:
        return [f for f in TRACKED_FIELDS
                if self.field_origins.get(f) is FieldOrigin.UNCERTAIN]

    # ------------------------------------------------------------------
    # Abgeleitete Werte
    # ------------------------------------------------------------------
    @property
    def key(self) -> tuple[str, str, str, str]:
        """Fachlicher Schluessel: Material / Lieferant / EKorg / Werk."""
        return (self.material_number, self.vendor_number, self.purchasing_org, self.plant)

    @property
    def old_price(self) -> Decimal | None:
        record = self.sap_info_record
        return record.price if record and record.exists else None

    @property
    def old_price_unit(self) -> int | None:
        record = self.sap_info_record
        return record.price_unit if record and record.exists else None

    @property
    def normalized_new_price(self) -> Decimal | None:
        """Preis je *einer* Mengeneinheit -- Basis fuer den Prozentvergleich."""
        if self.price is None or not self.price_unit:
            return None
        return self.price / Decimal(self.price_unit)

    @property
    def normalized_old_price(self) -> Decimal | None:
        old, unit = self.old_price, self.old_price_unit
        if old is None or not unit:
            return None
        return old / Decimal(unit)

    @property
    def price_change_percent(self) -> Decimal | None:
        """Preisaenderung in Prozent, preiseinheitsbereinigt."""
        old = self.normalized_old_price
        new = self.normalized_new_price
        if old is None or new is None or old == 0:
            return None
        return (new - old) / old * Decimal(100)

    @property
    def price_changed(self) -> bool:
        old = self.normalized_old_price
        new = self.normalized_new_price
        if old is None or new is None:
            return old is not new
        return old != new

    @property
    def info_record_pending(self) -> bool:
        return self.do_info_record and self.info_record_action in (
            InfoRecordAction.CREATE, InfoRecordAction.UPDATE
        )

    @property
    def source_list_pending(self) -> bool:
        return self.do_source_list and self.source_list_action in (
            SourceListAction.CREATE, SourceListAction.UPDATE
        )

    @property
    def has_changes(self) -> bool:
        """Gibt es ueberhaupt etwas in SAP zu tun?

        Kontrakt und Bestellung sind Neuanlagen und damit immer eine
        Aenderung, sobald der Anwender sie angehakt hat.
        """
        return (
            self.info_record_pending
            or self.source_list_pending
            or self.do_contract
            or self.do_purchase_order
        )

    @property
    def is_processable(self) -> bool:
        """Darf diese Position geschrieben werden?"""
        if not self.selected:
            return False
        if self.issues.has_blocking:
            return False
        return self.has_changes

    @property
    def display_name(self) -> str:
        if self.material_number:
            return self.material_number
        if self.vendor_material_number:
            return f"({self.vendor_material_number})"
        return self.description[:40] or f"Position {self.position_number or self.uid}"

    # ------------------------------------------------------------------
    # Darstellung
    # ------------------------------------------------------------------
    def summary_line(self) -> str:
        parts = [self.display_name]
        if self.description:
            parts.append(self.description[:60])
        if self.price is not None:
            parts.append(f"{format_decimal(self.price)} {self.currency}".strip())
        if self.valid_from:
            parts.append(f"ab {format_date(self.valid_from)}")
        return " | ".join(p for p in parts if p)

    def to_dict(self) -> dict:
        """Flache Darstellung fuer Historie/Export."""
        return {
            "uid": self.uid,
            "source_kind": self.source_kind.value,
            "source_hint": self.source_hint,
            "position_number": self.position_number,
            "material_number": self.material_number,
            "vendor_material_number": self.vendor_material_number,
            "description": self.description,
            "quantity": str(self.quantity) if self.quantity is not None else "",
            "uom": self.uom,
            "price": str(self.price) if self.price is not None else "",
            "price_unit": self.price_unit or "",
            "currency": self.currency,
            "min_order_qty": str(self.min_order_qty) if self.min_order_qty is not None else "",
            "lead_time_days": self.lead_time_days or "",
            "valid_from": self.valid_from.isoformat() if self.valid_from else "",
            "remarks": self.remarks,
            "vendor_number": self.vendor_number,
            "purchasing_org": self.purchasing_org,
            "plant": self.plant,
            "info_record_action": self.info_record_action.value,
            "source_list_action": self.source_list_action.value,
            "contract_quantity": str(self.contract_quantity) if self.contract_quantity is not None else "",
            "order_quantity": str(self.order_quantity) if self.order_quantity is not None else "",
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else "",
            "created_info_record": self.created_info_record,
            "created_contract": self.created_contract,
            "created_purchase_order": self.created_purchase_order,
            "status": self.status.value,
        }
