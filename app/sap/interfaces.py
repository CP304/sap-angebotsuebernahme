"""Schnittstellen der SAP-Services.

Mock- und Echtbetrieb implementieren dieselben Signaturen.  Die GUI und die
Batch-Verarbeitung kennen ausschliesslich diese Schnittstellen -- sie wissen
nicht, ob dahinter ein echtes SAP oder das eingebaute Testsystem steht.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from ..config.settings import Settings
from ..models.document_plan import ContractPlan, PurchaseOrderPlan
from ..models.enums import ResultState
from ..models.offer_position import OfferPosition
from ..models.results import ActionResult
from ..models.sap_info_record import SapInfoRecord
from ..models.sap_source_list import SapSourceList
from .connection import SapGuiConnection
from .selectors import SelectorRegistry

logger = logging.getLogger(__name__)


@dataclass
class VendorMatch:
    """Treffer der Lieferantensuche."""

    vendor_number: str
    name: str
    score: float = 0.0
    city: str = ""
    blocked: bool = False


@dataclass
class MaterialInfo:
    """Ergebnis der Materialpruefung."""

    material_number: str
    exists: bool
    description: str = ""
    base_unit: str = ""
    material_group: str = ""
    purchasing_blocked: bool = False
    error: str = ""


@dataclass
class WriteContext:
    """Rahmenbedingungen einer Schreibaktion."""

    dry_run: bool
    valid_from: date | None
    valid_to: date | None
    settings: Settings

    @property
    def state(self) -> ResultState:
        return ResultState.SIMULATED if self.dry_run else ResultState.SUCCESS


class SapServiceBase:
    """Gemeinsame Basis aller Services (Zeitmessung, Ergebnisobjekte)."""

    #: Operationsschluessel fuer die Selektorpruefung (siehe REQUIRED_SCREENS)
    read_operation: str = ""
    write_operation: str = ""

    def __init__(self, connection: SapGuiConnection | None, settings: Settings,
                 selectors: SelectorRegistry) -> None:
        self.connection = connection
        self.settings = settings
        self.selectors = selectors

    # -- Hilfen ---------------------------------------------------------
    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000.0

    def _result(self, action: str, state: ResultState, message: str, *,
                transaction: str = "", document_number: str = "", old_value: str = "",
                new_value: str = "", detail: str = "", started_ms: float | None = None,
                sap_messages: list[str] | None = None) -> ActionResult:
        duration = int(self._now_ms() - started_ms) if started_ms else 0
        return ActionResult(
            action=action, state=state, message=message, transaction=transaction,
            document_number=document_number, old_value=old_value, new_value=new_value,
            detail=detail, duration_ms=duration, sap_messages=sap_messages or [],
        )


# ---------------------------------------------------------------------------
# Fachliche Schnittstellen
# ---------------------------------------------------------------------------

class InfoRecordServiceBase(SapServiceBase, ABC):
    """Einkaufsinfosatz (ME11/ME12/ME13)."""

    @abstractmethod
    def read(self, material_number: str, vendor_number: str, purchasing_org: str,
             plant: str) -> SapInfoRecord:
        """Ist-Zustand lesen.  Wirft nicht, sondern setzt ``read_error``."""

    @abstractmethod
    def write(self, position: OfferPosition, context: WriteContext) -> ActionResult:
        """Infosatz anlegen oder aendern."""


class SourceListServiceBase(SapServiceBase, ABC):
    """Orderbuch (ME01/ME03)."""

    @abstractmethod
    def read(self, material_number: str, plant: str) -> SapSourceList:
        ...

    @abstractmethod
    def write(self, position: OfferPosition, context: WriteContext,
              contract_number: str = "", contract_item: str = "") -> ActionResult:
        """Orderbucheintrag anlegen/aktualisieren.

        ``contract_number`` wird -- falls in den Einstellungen aktiviert -- als
        Vereinbarung im Orderbuch hinterlegt.
        """


class MaterialServiceBase(SapServiceBase, ABC):
    """Materialstamm (MM03)."""

    @abstractmethod
    def check(self, material_number: str, plant: str = "") -> MaterialInfo:
        ...


class VendorServiceBase(SapServiceBase, ABC):
    """Lieferantenstamm (XK03)."""

    @abstractmethod
    def check(self, vendor_number: str, purchasing_org: str = "") -> VendorMatch | None:
        ...

    @abstractmethod
    def search_by_name(self, name: str, limit: int = 10) -> list[VendorMatch]:
        """Namenssuche -- liefert *Vorschlaege*, nie eine automatische Zuordnung."""


class ContractServiceBase(SapServiceBase, ABC):
    """Mengenkontrakt (ME31K/ME32K/ME33K)."""

    @abstractmethod
    def create(self, plan: ContractPlan, context: WriteContext) -> ActionResult:
        ...


class PurchaseOrderServiceBase(SapServiceBase, ABC):
    """Bestellung (ME21N/ME22N/ME23N)."""

    @abstractmethod
    def create(self, plan: PurchaseOrderPlan, context: WriteContext) -> ActionResult:
        ...
