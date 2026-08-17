"""Zusammenbau der SAP-Services (Echtbetrieb oder Mock).

Die uebrige Anwendung kennt nur dieses Objekt.  Ob dahinter eine echte
SAP-GUI-Session oder das eingebaute Testsystem steht, entscheidet allein die
Einstellung ``use_mock_sap``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..config.settings import Settings
from ..models.offer_position import OfferPosition
from .connection import SapError, SapGuiConnection, SapNotAvailableError, SessionInfo
from .contract_service import SapContractService
from .info_record_service import SapInfoRecordService
from .interfaces import (
    ContractServiceBase,
    InfoRecordServiceBase,
    MaterialInfo,
    MaterialServiceBase,
    PurchaseOrderServiceBase,
    SourceListServiceBase,
    VendorMatch,
    VendorServiceBase,
    WriteContext,
)
from .material_service import SapMaterialService
from .mock_backend import (
    MockContractService,
    MockInfoRecordService,
    MockMaterialService,
    MockPurchaseOrderService,
    MockSapSystem,
    MockSourceListService,
    MockVendorService,
)
from .purchase_order_service import SapPurchaseOrderService
from .selectors import REQUIRED_SCREENS, SelectorRegistry
from .source_list_service import SapSourceListService
from .vendor_service import SapVendorService

logger = logging.getLogger(__name__)


@dataclass
class ConnectionStatus:
    """Verbindungszustand fuer die Anzeige in der GUI."""

    connected: bool = False
    mock: bool = False
    dry_run: bool = True
    label: str = "nicht verbunden"
    detail: str = ""
    user: str = ""
    system: str = ""
    write_ready: bool = False
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def color(self) -> str:
        if not self.connected:
            return "grau"
        if self.mock:
            return "blau"
        return "gruen" if self.write_ready else "gelb"

    def short(self) -> str:
        if self.mock:
            return f"Testsystem (Mock){' – Dry Run' if self.dry_run else ''}"
        if not self.connected:
            return "SAP nicht verbunden"
        return f"{self.system or 'SAP'} / {self.user}" + (" – Dry Run" if self.dry_run else "")


class SapGateway:
    """Fassade ueber alle SAP-Services."""

    def __init__(self, settings: Settings, selectors: SelectorRegistry | None = None) -> None:
        self.settings = settings
        self.selectors = selectors or SelectorRegistry.load(settings.selectors_file)

        self.connection: SapGuiConnection | None = None
        self._mock_system: MockSapSystem | None = None

        # Caches -- ein Material/Lieferant wird pro Lauf nur einmal geprueft
        self._material_cache: dict[str, MaterialInfo] = {}
        self._vendor_cache: dict[str, VendorMatch | None] = {}

        self.info_records: InfoRecordServiceBase
        self.source_lists: SourceListServiceBase
        self.materials: MaterialServiceBase
        self.vendors: VendorServiceBase
        self.contracts: ContractServiceBase
        self.purchase_orders: PurchaseOrderServiceBase

        self._build_services()

    # ------------------------------------------------------------------
    # Aufbau
    # ------------------------------------------------------------------
    @property
    def is_mock(self) -> bool:
        return self.settings.use_mock_sap

    @property
    def mock_system(self) -> MockSapSystem | None:
        return self._mock_system

    def _build_services(self) -> None:
        if self.settings.use_mock_sap:
            self._mock_system = MockSapSystem(self.settings.home / "mock_sap.json")
            system = self._mock_system
            self.info_records = MockInfoRecordService(system, self.settings, self.selectors)
            self.source_lists = MockSourceListService(system, self.settings, self.selectors)
            self.materials = MockMaterialService(system, self.settings, self.selectors)
            self.vendors = MockVendorService(system, self.settings, self.selectors)
            self.contracts = MockContractService(system, self.settings, self.selectors)
            self.purchase_orders = MockPurchaseOrderService(system, self.settings, self.selectors)
            logger.info("SAP-Services im Mock-Modus aufgebaut.")
        else:
            self._mock_system = None
            connection = self.connection
            self.info_records = SapInfoRecordService(connection, self.settings, self.selectors)
            self.source_lists = SapSourceListService(connection, self.settings, self.selectors)
            self.materials = SapMaterialService(connection, self.settings, self.selectors)
            self.vendors = SapVendorService(connection, self.settings, self.selectors)
            self.contracts = SapContractService(connection, self.settings, self.selectors)
            self.purchase_orders = SapPurchaseOrderService(connection, self.settings, self.selectors)
            logger.info("SAP-Services im Echtbetrieb aufgebaut.")
        self.clear_cache()

    def set_mode(self, use_mock: bool) -> None:
        """Zwischen Echtbetrieb und Testsystem umschalten."""
        if self.settings.use_mock_sap == use_mock:
            return
        self.settings.use_mock_sap = use_mock
        if use_mock and self.connection is not None:
            self.connection.disconnect()
            self.connection = None
        self._build_services()

    def _attach_connection(self) -> None:
        """Verbindung in die (echten) Services nachziehen."""
        for service in (self.info_records, self.source_lists, self.materials,
                        self.vendors, self.contracts, self.purchase_orders):
            service.connection = self.connection

    # ------------------------------------------------------------------
    # Verbindung
    # ------------------------------------------------------------------
    def connect(self) -> ConnectionStatus:
        if self.settings.use_mock_sap:
            return self.status()
        if self.connection is None:
            self.connection = SapGuiConnection(self.settings.sap, self.selectors)
        self.connection.connect()
        self._attach_connection()
        self.clear_cache()
        return self.status()

    def disconnect(self) -> ConnectionStatus:
        if self.connection is not None:
            self.connection.disconnect()
        self.clear_cache()
        return self.status()

    def get_sessions(self) -> list[SessionInfo]:
        if self.settings.use_mock_sap or self.connection is None:
            return []
        return self.connection.get_sessions()

    def select_session(self, connection_index: int, session_index: int) -> ConnectionStatus:
        if self.connection is None:
            self.connect()
        if self.connection is not None:
            self.connection.select_session(connection_index, session_index)
            self.clear_cache()
        return self.status()

    def is_connected(self) -> bool:
        if self.settings.use_mock_sap:
            return True
        return self.connection is not None and self.connection.is_connected()

    def status(self) -> ConnectionStatus:
        status = ConnectionStatus(mock=self.settings.use_mock_sap,
                                  dry_run=self.settings.dry_run)
        if self.settings.use_mock_sap:
            status.connected = True
            status.label = "Testsystem (Mock-SAP)"
            status.detail = ("Es wird kein echtes SAP angesprochen. Alle Aktionen laufen "
                             "gegen den eingebauten Testbestand.")
            status.write_ready = True
            return status

        if self.connection is None or not self.connection.is_connected():
            status.connected = False
            status.label = "nicht verbunden"
            status.detail = "Bitte SAP Logon starten, anmelden und 'SAP verbinden' waehlen."
            status.blocking_reasons.append("Keine SAP-Verbindung")
            return status

        info = self.connection.session_info
        status.connected = True
        status.user = info.user if info else ""
        status.system = info.system if info else ""
        status.label = self.connection.describe()
        reasons = self.write_blocking_reasons()
        status.blocking_reasons = reasons
        status.write_ready = not reasons
        status.detail = ("Schreiben freigegeben." if not reasons
                         else "Schreiben gesperrt: " + "; ".join(reasons))
        return status

    def write_blocking_reasons(self, operations: tuple[str, ...] = ()) -> list[str]:
        """Warum darf (noch) nicht geschrieben werden?"""
        reasons: list[str] = []
        if self.settings.use_mock_sap:
            return reasons
        if self.connection is None or not self.connection.is_connected():
            reasons.append("keine SAP-Verbindung")
            return reasons
        checked = operations or ("info_record_write", "source_list_write",
                                 "contract_write", "purchase_order_write")
        for operation in checked:
            missing = self.selectors.unverified(REQUIRED_SCREENS.get(operation, ()))
            if missing:
                reasons.append(f"{len(missing)} ungepruefte Feld-IDs fuer '{operation}'")
        return reasons

    # ------------------------------------------------------------------
    # Stammdaten mit Cache
    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        self._material_cache.clear()
        self._vendor_cache.clear()

    def check_material(self, material_number: str, plant: str = "") -> MaterialInfo:
        key = f"{material_number}|{plant}"
        cached = self._material_cache.get(key)
        if cached is not None:
            return cached
        info = self.materials.check(material_number, plant)
        self._material_cache[key] = info
        return info

    def check_vendor(self, vendor_number: str, purchasing_org: str = "") -> VendorMatch | None:
        key = f"{vendor_number}|{purchasing_org}"
        if key in self._vendor_cache:
            return self._vendor_cache[key]
        match = self.vendors.check(vendor_number, purchasing_org)
        self._vendor_cache[key] = match
        return match

    # ------------------------------------------------------------------
    # Bequemlichkeit
    # ------------------------------------------------------------------
    def write_context(self, valid_from: date | None = None,
                      valid_to: date | None = None) -> WriteContext:
        return WriteContext(
            dry_run=self.settings.dry_run,
            valid_from=valid_from or date.today(),
            valid_to=valid_to or self.settings.parsed_info_record_valid_to(),
            settings=self.settings,
        )

    def load_position_state(self, position: OfferPosition) -> None:
        """SAP-Ist-Zustand einer Position einsammeln (Infosatz + Orderbuch)."""
        if position.material_number:
            material = self.check_material(position.material_number, position.plant)
            position.material_exists = material.exists
            position.sap_material_description = material.description
        if position.vendor_number:
            vendor = self.check_vendor(position.vendor_number, position.purchasing_org)
            position.vendor_exists = vendor is not None

        if position.material_number and position.vendor_number:
            position.sap_info_record = self.info_records.read(
                position.material_number, position.vendor_number,
                position.purchasing_org, position.plant)
        if position.material_number and position.plant:
            position.sap_source_list = self.source_lists.read(
                position.material_number, position.plant)
        position.sap_loaded = True

    def sap_user(self) -> str:
        if self.settings.use_mock_sap:
            return "MOCK"
        return self.connection.sap_user if self.connection else ""

    def sap_system(self) -> str:
        if self.settings.use_mock_sap:
            return "MOCK"
        return self.connection.sap_system if self.connection else ""

    def reset_mock_data(self) -> bool:
        if self._mock_system is None:
            return False
        self._mock_system.reset()
        self.clear_cache()
        return True


__all__ = ["SapGateway", "ConnectionStatus", "SapError", "SapNotAvailableError"]
