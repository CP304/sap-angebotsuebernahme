"""Eingebautes Test-SAP ("Mock-Modus").

Zweck
-----
Die komplette Anwendung -- Import, Vergleich, Vorschau, Batch, Historie --
laesst sich damit ohne SAP-Zugang entwickeln, vorfuehren und testen.  Die
Mock-Services erfuellen exakt dieselben Schnittstellen wie die echten.

Der Datenbestand wird in ``%APPDATA%/SAP-Angebotsuebernahme/mock_sap.json``
gespeichert, damit angelegte Infosaetze/Kontrakte/Bestellungen auch nach einem
Neustart noch da sind.  Ueber "Testdaten zuruecksetzen" in den Einstellungen
laesst sich der Auslieferungszustand wiederherstellen.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..config.settings import Settings
from ..models.document_plan import (
    ContractPlan,
    PurchaseOrderPlan,
    apply_account_assignment,
    validate_account_assignment,
)
from ..models.enums import ResultState
from ..models.offer_position import OfferPosition
from ..models.results import ActionResult
from ..models.sap_info_record import SapCondition, SapInfoRecord
from ..models.sap_source_list import SapSourceList, SourceListEntry
from ..utils.parsing import (
    format_date,
    format_decimal,
    normalize_material_number,
    normalize_vendor_number,
    similarity,
)
from .info_record_service import (
    scale_levels_for,
    scale_not_written_message,
    unverified_scale_selectors,
    verify_info_record_write,
    verify_source_list_write,
)
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
from .selectors import SelectorRegistry

logger = logging.getLogger(__name__)


def _d(value: str) -> Decimal:
    return Decimal(value)


# ---------------------------------------------------------------------------
# Datenbestand
# ---------------------------------------------------------------------------

class MockSapSystem:
    """Speicher des Testsystems."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.vendors: dict[str, dict] = {}
        self.materials: dict[str, dict] = {}
        self.info_records: dict[str, dict] = {}
        self.source_lists: dict[str, list[dict]] = {}
        self.contracts: dict[str, dict] = {}
        self.purchase_orders: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        #: Pruefhilfe: Schluessel eines Infosatzes -> Preis, den das
        #: Testsystem beim Sichern *statt* des uebergebenen Wertes ablegt.
        #: Damit laesst sich der reale Fall nachstellen, in dem SAP
        #: "gesichert" meldet, den Preis aber gar nicht uebernommen hat
        #: (Feld nicht eingabebereit, Berechtigung auf Detailebene).
        #: Wird bewusst NICHT gespeichert -- reine Laufzeit-Einstellung.
        self.forced_price_deviations: dict[str, str] = {}
        self.load()

    # -- Schluessel -----------------------------------------------------
    # SAP behandelt "0000100234" und "100234" ueber die ALPHA-Konvertierung
    # als dieselbe Nummer.  Das Testsystem muss sich genauso verhalten, sonst
    # findet es Saetze nicht wieder, sobald irgendwo fuehrende Nullen wegfallen.
    @staticmethod
    def ir_key(material: str, vendor: str, ekorg: str, plant: str) -> str:
        return (f"{normalize_material_number(material)}|"
                f"{normalize_vendor_number(vendor)}|{ekorg}|{plant}")

    @staticmethod
    def sl_key(material: str, plant: str) -> str:
        return f"{normalize_material_number(material)}|{plant}"

    def _rekey(self) -> None:
        """Gespeicherte Bestaende auf die normalisierten Schluessel bringen."""
        neu_ir: dict[str, dict] = {}
        for schluessel, wert in self.info_records.items():
            teile = schluessel.split("|")
            if len(teile) == 4:
                schluessel = self.ir_key(*teile)
            neu_ir[schluessel] = wert
        self.info_records = neu_ir

        neu_sl: dict[str, list[dict]] = {}
        for schluessel, wert in self.source_lists.items():
            teile = schluessel.split("|")
            if len(teile) == 2:
                schluessel = self.sl_key(*teile)
            neu_sl[schluessel] = wert
        self.source_lists = neu_sl

    # -- Persistenz -----------------------------------------------------
    def load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.vendors = data.get("vendors", {})
                self.materials = data.get("materials", {})
                self.info_records = data.get("info_records", {})
                self.source_lists = data.get("source_lists", {})
                self.contracts = data.get("contracts", {})
                self.purchase_orders = data.get("purchase_orders", {})
                self.counters = data.get("counters", {})
                self._rekey()
                logger.info("Mock-SAP geladen: %s", self.path)
                return
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Mock-Daten unlesbar (%s): %s -- Auslieferungszustand", self.path, exc)
        self.reset()

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "vendors": self.vendors,
                "materials": self.materials,
                "info_records": self.info_records,
                "source_lists": self.source_lists,
                "contracts": self.contracts,
                "purchase_orders": self.purchase_orders,
                "counters": self.counters,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.error("Mock-Daten konnten nicht gespeichert werden: %s", exc)

    def next_number(self, kind: str, prefix: str) -> str:
        current = self.counters.get(kind, 0) + 1
        self.counters[kind] = current
        return f"{prefix}{current:06d}"

    # -- Auslieferungszustand -------------------------------------------
    def reset(self) -> None:
        """Realistischer Testbestand, passend zu den Beispielangeboten."""
        today = date.today()
        last_year = today.replace(year=today.year - 1)

        self.vendors = {
            "0000100234": {"name": "Muster Dichtungstechnik GmbH", "city": "Bielefeld",
                           "blocked": False},
            "0000100987": {"name": "Nordtec Industriebedarf AG", "city": "Hamburg",
                           "blocked": False},
            "0000101455": {"name": "Schmidt & Partner Werkstoffe KG", "city": "Solingen",
                           "blocked": False},
            "0000102100": {"name": "Pumpen Weber GmbH & Co. KG", "city": "Mannheim",
                           "blocked": False},
            "0000103777": {"name": "Gesperrt Handels GmbH", "city": "Berlin",
                           "blocked": True},
        }

        self.materials = {
            "47110001": {"description": "Dichtring NBR 40x52x7", "base_unit": "ST",
                         "group": "010", "blocked": False},
            "47110002": {"description": "O-Ring Viton 25x3", "base_unit": "ST",
                         "group": "010", "blocked": False},
            "47110003": {"description": "Wellendichtring FPM 30x47x7", "base_unit": "ST",
                         "group": "010", "blocked": False},
            "47110004": {"description": "Flachdichtung Klingersil 100x60x2", "base_unit": "ST",
                         "group": "010", "blocked": False},
            "47110005": {"description": "Kugellager 6204-2RS", "base_unit": "ST",
                         "group": "020", "blocked": False},
            "48200110": {"description": "Kreiselpumpe Typ KP-40", "base_unit": "ST",
                         "group": "030", "blocked": False},
            "48200111": {"description": "Gleitringdichtung KP-40", "base_unit": "ST",
                         "group": "030", "blocked": False},
            "49900010": {"description": "Hydraulikschlauch DN12 2SN", "base_unit": "M",
                         "group": "040", "blocked": False},
            "49900011": {"description": "Schlauchschelle W2 20-32", "base_unit": "ST",
                         "group": "040", "blocked": True},
            # Hinweis: 47119999 existiert bewusst NICHT -> Fehlerpfad testbar
        }

        def info(material: str, vendor: str, price: str, unit: int = 1, uom: str = "ST",
                 lead: int = 14, minqty: str = "1", ekorg: str = "1000",
                 plant: str = "1000") -> None:
            self.info_records[self.ir_key(material, vendor, ekorg, plant)] = {
                "info_record_number": self.next_number("info_record", "530000"),
                "price": price, "price_unit": unit, "currency": "EUR", "order_unit": uom,
                "valid_from": last_year.isoformat(), "valid_to": "2099-12-31",
                "lead_time_days": lead, "min_order_qty": minqty,
                "vendor_material_number": "", "purchasing_group": "100",
                "conditions": [{"type": "PB00", "description": "Bruttopreis",
                                "amount": price, "currency": "EUR", "price_unit": unit,
                                "uom": uom}],
            }

        self.info_records = {}
        self.counters = {}
        info("47110001", "0000100234", "12.40")
        # Preiseinheit 10: 8,40 EUR je 10 ST = 0,84 EUR/ST
        info("47110002", "0000100234", "8.40", unit=10)
        info("47110003", "0000100234", "18.95")
        info("47110005", "0000100987", "4.35", unit=1, lead=21)
        info("48200110", "0000102100", "1245.00", lead=42, minqty="1")
        info("49900010", "0000100987", "6.80", unit=1, uom="M", lead=10, minqty="50")
        # 47110004 und 48200111 haben bewusst KEINEN Infosatz -> ME11-Pfad

        self.source_lists = {
            self.sl_key("47110001", "1000"): [{
                "vendor_number": "0000100234", "plant": "1000", "purchasing_org": "1000",
                "valid_from": last_year.isoformat(), "valid_to": "2099-12-31",
                "fixed": True, "blocked": False, "mrp_indicator": "1",
                "agreement": "", "agreement_item": "",
            }],
            self.sl_key("47110002", "1000"): [{
                "vendor_number": "0000100234", "plant": "1000", "purchasing_org": "1000",
                "valid_from": last_year.isoformat(), "valid_to": "2099-12-31",
                "fixed": False, "blocked": True, "mrp_indicator": "",
                "agreement": "", "agreement_item": "",
            }],
            self.sl_key("47110005", "1000"): [{
                "vendor_number": "0000101455", "plant": "1000", "purchasing_org": "1000",
                "valid_from": last_year.isoformat(), "valid_to": "2099-12-31",
                "fixed": True, "blocked": False, "mrp_indicator": "1",
                "agreement": "", "agreement_item": "",
            }],
        }
        self.contracts = {}
        self.purchase_orders = {}
        self.counters["info_record"] = 100
        logger.info("Mock-SAP auf Auslieferungszustand zurueckgesetzt.")
        self.save()


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

class _MockBase:
    """Gemeinsamer Zugriff auf den Datenbestand."""

    def __init__(self, system: MockSapSystem, settings: Settings,
                 selectors: SelectorRegistry) -> None:
        self.system = system
        self.settings = settings
        self.selectors = selectors
        self.connection = None

    @staticmethod
    def _now_ms() -> float:
        import time
        return time.monotonic() * 1000.0

    def _result(self, action: str, state: ResultState, message: str, **kwargs) -> ActionResult:
        started = kwargs.pop("started_ms", None)
        duration = int(self._now_ms() - started) if started else 0
        return ActionResult(action=action, state=state, message=message,
                            duration_ms=duration, **kwargs)


class MockInfoRecordService(_MockBase, InfoRecordServiceBase):
    """Infosatz im Testsystem."""

    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    def read(self, material_number: str, vendor_number: str, purchasing_org: str,
             plant: str) -> SapInfoRecord:
        record = SapInfoRecord(
            material_number=material_number, vendor_number=vendor_number,
            purchasing_org=purchasing_org, plant=plant, read_at=datetime.now(),
        )
        raw = self.system.info_records.get(
            MockSapSystem.ir_key(material_number, vendor_number, purchasing_org, plant)
        )
        if raw is None:
            # Zweiter Versuch ohne Werk (werksunabhaengiger Infosatz)
            raw = self.system.info_records.get(
                MockSapSystem.ir_key(material_number, vendor_number, purchasing_org, "")
            )
        if raw is None:
            record.exists = False
            return record

        record.exists = True
        record.info_record_number = raw.get("info_record_number", "")
        record.price = _d(str(raw.get("price", "0")))
        record.price_unit = int(raw.get("price_unit", 1))
        record.currency = raw.get("currency", "EUR")
        record.order_unit = raw.get("order_unit", "ST")
        record.valid_from = date.fromisoformat(raw["valid_from"]) if raw.get("valid_from") else None
        record.valid_to = date.fromisoformat(raw["valid_to"]) if raw.get("valid_to") else None
        record.lead_time_days = raw.get("lead_time_days")
        if raw.get("min_order_qty") not in (None, ""):
            record.min_order_qty = _d(str(raw["min_order_qty"]))
        record.vendor_material_number = raw.get("vendor_material_number", "")
        record.purchasing_group = raw.get("purchasing_group", "")
        record.conditions = [
            SapCondition(condition_type=c.get("type", ""), description=c.get("description", ""),
                         amount=_d(str(c.get("amount", "0"))), currency=c.get("currency", "EUR"),
                         price_unit=c.get("price_unit"), uom=c.get("uom", ""),
                         valid_from=record.valid_from, valid_to=record.valid_to)
            for c in raw.get("conditions", [])
        ]
        record.raw = dict(raw)
        return record

    def write(self, position: OfferPosition, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        key = MockSapSystem.ir_key(position.material_number, position.vendor_number,
                                   position.purchasing_org, position.plant)
        existing = self.system.info_records.get(key)
        transaction = (self.settings.transactions.info_record_change if existing
                       else self.settings.transactions.info_record_create)
        old_value = (f"{existing['price']} {existing.get('currency', '')} / "
                     f"{existing.get('price_unit', 1)}" if existing else "kein Infosatz")
        new_value = (f"{position.price} {position.currency} / {position.price_unit or 1}")

        if context.dry_run:
            return self._result(
                "info_record", ResultState.SIMULATED,
                f"{'aendern' if existing else 'neu anlegen'} ({transaction})",
                transaction=transaction, old_value=old_value, new_value=new_value,
                document_number=existing.get("info_record_number", "") if existing else "",
                started_ms=started,
            )

        number = (existing or {}).get("info_record_number") or \
            self.system.next_number("info_record", "530000")

        messages: list[str] = [
            f"Infosatz {number} wurde {'geaendert' if existing else 'angelegt'}"]

        # -- Mengenstaffel -------------------------------------------------
        staffel: list[list[str]] = []
        if self.settings.workflow.info_record_write_scales and position.has_scales:
            offen = unverified_scale_selectors(self.selectors)
            if offen:
                # Kein stillschweigendes Weglassen: nur Grundpreis, mit Vermerk.
                messages.append(scale_not_written_message(offen, self.settings))
            else:
                stufen, hinweise = scale_levels_for(position, self.settings)
                messages.extend(hinweise)
                staffel = [[str(menge), str(preis)] for menge, preis in stufen]
                if staffel:
                    # Bewusst nur die *geschriebenen* Stufen nennen -- alles
                    # andere waere eine Erfolgsmeldung fuer nicht Geleistetes.
                    beschreibung = "; ".join(
                        f"ab {format_decimal(menge, 3)}: {format_decimal(preis)}"
                        for menge, preis in stufen)
                    messages.append(f"Mengenstaffel mit {len(staffel)} Stufe(n) "
                                    f"gepflegt: {beschreibung}")

        # SAP legt den Konditionsbetrag zweistellig ab -- das Testsystem
        # ebenso, sonst waere die Ruecklese-Pruefung unrealistisch scharf.
        gespeicherter_preis = position.price
        if gespeicherter_preis is not None:
            gespeicherter_preis = gespeicherter_preis.quantize(Decimal("0.01"))
        abweichung = self.system.forced_price_deviations.get(key)
        if abweichung is not None:
            gespeicherter_preis = _d(str(abweichung))

        self.system.info_records[key] = {
            "info_record_number": number,
            "price": str(gespeicherter_preis) if gespeicherter_preis is not None else "0",
            "price_unit": position.price_unit or 1,
            "currency": position.currency or self.settings.purchasing.currency,
            "order_unit": position.uom or self.settings.purchasing.order_unit,
            "valid_from": (context.valid_from or date.today()).isoformat(),
            "valid_to": (context.valid_to.isoformat() if context.valid_to else "2099-12-31"),
            "lead_time_days": position.lead_time_days,
            "min_order_qty": str(position.min_order_qty) if position.min_order_qty is not None else "",
            "vendor_material_number": position.vendor_material_number,
            "purchasing_group": self.settings.purchasing.purchasing_group,
            "conditions": [{"type": "PB00", "description": "Bruttopreis",
                            "amount": str(gespeicherter_preis), "currency": position.currency,
                            "price_unit": position.price_unit or 1, "uom": position.uom}],
            "scales": staffel,
        }
        self.system.save()

        # -- Ruecklese-Pruefung (wie im Echtbetrieb) ------------------------
        if self.settings.sap.verify_after_write:
            geprueft = self.read(position.material_number, position.vendor_number,
                                 position.purchasing_org, position.plant)
            in_ordnung, hinweise = verify_info_record_write(
                geprueft, position, context, self.settings)
            messages.extend(hinweise)
            if not in_ordnung and self.settings.sap.verify_failure_is_error:
                return self._result(
                    "info_record", ResultState.FAILED, hinweise[0],
                    transaction=transaction, document_number=number,
                    old_value=old_value, new_value=new_value, started_ms=started,
                    sap_messages=messages)
            if not in_ordnung:
                messages.append("Hinweis: Es wurde gesichert, die Ruecklese-Pruefung ist "
                                "aber nicht sauber -- bitte nachsehen.")

        return self._result(
            "info_record", ResultState.SUCCESS,
            f"Infosatz {'geaendert' if existing else 'angelegt'}",
            transaction=transaction, document_number=number,
            old_value=old_value, new_value=new_value, started_ms=started,
            sap_messages=messages,
        )


class MockSourceListService(_MockBase, SourceListServiceBase):
    """Orderbuch im Testsystem."""

    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    def read(self, material_number: str, plant: str) -> SapSourceList:
        source_list = SapSourceList(material_number=material_number, plant=plant,
                                    read_at=datetime.now())
        rows = self.system.source_lists.get(MockSapSystem.sl_key(material_number, plant), [])
        for index, row in enumerate(rows):
            source_list.entries.append(SourceListEntry(
                vendor_number=row.get("vendor_number", ""),
                plant=row.get("plant", plant),
                purchasing_org=row.get("purchasing_org", ""),
                valid_from=date.fromisoformat(row["valid_from"]) if row.get("valid_from") else None,
                valid_to=date.fromisoformat(row["valid_to"]) if row.get("valid_to") else None,
                fixed=bool(row.get("fixed")), blocked=bool(row.get("blocked")),
                mrp_indicator=row.get("mrp_indicator", ""),
                agreement=row.get("agreement", ""), agreement_item=row.get("agreement_item", ""),
                row_index=index,
            ))
        source_list.exists = bool(source_list.entries)
        return source_list

    def write(self, position: OfferPosition, context: WriteContext,
              contract_number: str = "", contract_item: str = "") -> ActionResult:
        started = self._now_ms()
        transaction = self.settings.transactions.source_list_maintain
        key = MockSapSystem.sl_key(position.material_number, position.plant)
        rows = self.system.source_lists.setdefault(key, [])
        workflow = self.settings.workflow

        existing = next((r for r in rows if r.get("vendor_number") == position.vendor_number), None)
        old_value = ("gesperrt" if existing and existing.get("blocked")
                     else "aktiv" if existing else "kein Eintrag")
        new_value = "aktiv" + (f", Kontrakt {contract_number}" if contract_number else "")

        if context.dry_run:
            return self._result(
                "source_list", ResultState.SIMULATED,
                f"{'aendern' if existing else 'neu anlegen'} ({transaction})",
                transaction=transaction, old_value=old_value, new_value=new_value,
                started_ms=started,
            )

        row = existing if existing is not None else {}
        row.update({
            "vendor_number": position.vendor_number,
            "plant": position.plant,
            "purchasing_org": position.purchasing_org,
            "valid_from": (context.valid_from or date.today()).isoformat(),
            "valid_to": (self.settings.parsed_source_list_valid_to()
                         or date(2099, 12, 31)).isoformat(),
            "fixed": workflow.source_list_set_fixed or bool(row.get("fixed")),
            "blocked": False if workflow.source_list_set_active else bool(row.get("blocked")),
            "mrp_indicator": workflow.source_list_mrp_indicator,
            "agreement": contract_number or row.get("agreement", ""),
            "agreement_item": contract_item or row.get("agreement_item", ""),
        })
        if existing is None:
            rows.append(row)
        self.system.save()

        messages = ["Orderbuchsaetze wurden gesichert"]
        if self.settings.sap.verify_after_write:
            geprueft = self.read(position.material_number, position.plant)
            in_ordnung, hinweise = verify_source_list_write(geprueft, position,
                                                           self.settings)
            messages.extend(hinweise)
            if not in_ordnung and self.settings.sap.verify_failure_is_error:
                return self._result(
                    "source_list", ResultState.FAILED, hinweise[0],
                    transaction=transaction, old_value=old_value, new_value=new_value,
                    started_ms=started, sap_messages=messages)

        return self._result(
            "source_list", ResultState.SUCCESS,
            f"Orderbucheintrag {'aktualisiert' if existing else 'angelegt'}, Lieferant aktiv",
            transaction=transaction, old_value=old_value, new_value=new_value,
            started_ms=started, sap_messages=messages,
        )


class MockMaterialService(_MockBase, MaterialServiceBase):
    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    def check(self, material_number: str, plant: str = "") -> MaterialInfo:
        raw = self.system.materials.get(material_number)
        if raw is None:
            return MaterialInfo(material_number=material_number, exists=False,
                                error="Material im Materialstamm nicht vorhanden")
        return MaterialInfo(
            material_number=material_number, exists=True,
            description=raw.get("description", ""), base_unit=raw.get("base_unit", ""),
            material_group=raw.get("group", ""),
            purchasing_blocked=bool(raw.get("blocked")),
        )


class MockVendorService(_MockBase, VendorServiceBase):
    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    def check(self, vendor_number: str, purchasing_org: str = "") -> VendorMatch | None:
        raw = self.system.vendors.get(vendor_number)
        if raw is None:
            # ALPHA-Konvertierung: fuehrende Nullen sind bedeutungslos
            gesucht = normalize_vendor_number(vendor_number)
            for number, candidate in self.system.vendors.items():
                if normalize_vendor_number(number) == gesucht:
                    raw = candidate
                    vendor_number = number
                    break
        if raw is None:
            return None
        return VendorMatch(vendor_number=vendor_number, name=raw.get("name", ""),
                           score=1.0, city=raw.get("city", ""),
                           blocked=bool(raw.get("blocked")))

    def search_by_name(self, name: str, limit: int = 10) -> list[VendorMatch]:
        matches = [
            VendorMatch(vendor_number=number, name=data.get("name", ""),
                        score=similarity(name, data.get("name", "")),
                        city=data.get("city", ""), blocked=bool(data.get("blocked")))
            for number, data in self.system.vendors.items()
        ]
        matches.sort(key=lambda m: m.score, reverse=True)
        return [m for m in matches if m.score > 0.2][:limit]


class MockContractService(_MockBase, ContractServiceBase):
    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    # ------------------------------------------------------------------
    def find_existing_contract(self, vendor_number: str, purchasing_org: str,
                               document_type: str, min_valid_to: date) -> str:
        """Passenden laufenden Mengenkontrakt im Testbestand suchen.

        Kriterien wie im Echtbetrieb: gleicher Lieferant, gleiche Einkaufs-
        organisation, gleiche Belegart, Laufzeitende nicht vor
        ``min_valid_to``.  Gibt es mehrere, gewinnt der am laengsten laufende
        -- so landen Folgepositionen im tragfaehigsten Beleg.
        """
        gesucht = normalize_vendor_number(vendor_number)
        treffer: list[tuple[date, str]] = []
        for nummer, kontrakt in self.system.contracts.items():
            if normalize_vendor_number(kontrakt.get("vendor", "")) != gesucht:
                continue
            if (kontrakt.get("purchasing_org") or "") != (purchasing_org or ""):
                continue
            if document_type and (kontrakt.get("document_type") or "") != document_type:
                continue
            rohes_ende = kontrakt.get("valid_to") or ""
            if not rohes_ende:
                continue
            try:
                ende = date.fromisoformat(rohes_ende)
            except ValueError:
                continue
            if min_valid_to and ende < min_valid_to:
                continue
            treffer.append((ende, nummer))
        if not treffer:
            return ""
        treffer.sort(reverse=True)
        return treffer[0][1]

    def create(self, plan: ContractPlan, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        transaction = (self.settings.transactions.contract_change if plan.is_change
                       else self.settings.transactions.contract_create)
        summary = (f"{len(plan.items)} Position(en), Laufzeit "
                   f"{format_date(plan.valid_from)} – {format_date(plan.valid_to)}")

        if context.dry_run:
            return self._result("contract", ResultState.SIMULATED,
                                f"Mengenkontrakt anlegen ({transaction}): {summary}",
                                transaction=transaction, new_value=summary, started_ms=started)

        bestand = self.system.contracts.get(plan.existing_contract_number or "")
        erweitert = bool(plan.existing_contract_number and bestand is not None)
        number = plan.existing_contract_number or self.system.next_number("contract", "4600")

        # Beim Erweitern wird hinter den vorhandenen Zeilen weitergezaehlt --
        # die Bestellung muss spaeter auf die richtige Kontraktzeile zeigen.
        vorhandene = list(bestand.get("items", [])) if erweitert else []
        naechste = len(vorhandene) + 1
        neue = []
        for versatz, item in enumerate(plan.items):
            item.item_number = f"{(naechste + versatz) * 10:05d}"
            neue.append({
                "item": item.item_number, "material": item.material_number,
                "quantity": str(item.quantity) if item.quantity is not None else "",
                "uom": item.uom,
                "net_price": str(item.net_price) if item.net_price is not None else "",
                "price_unit": item.price_unit or 1, "plant": item.plant,
            })

        if erweitert:
            # Kopfdaten des Bestandskontrakts bleiben unangetastet -- vor
            # allem die Laufzeit.  Es kommen nur Positionen hinzu.
            bestand["items"] = vorhandene + neue
            meldungen = [f"Kontrakt {number} wurde um {len(neue)} Position(en) erweitert",
                         "Kopfdaten (Laufzeit) unveraendert uebernommen",
                         "Nachrichtenfindung unterdrueckt (kein Versand)"]
            nachricht = f"Mengenkontrakt erweitert ({summary})"
        else:
            self.system.contracts[number] = {
                "document_type": plan.document_type, "vendor": plan.vendor_number,
                "purchasing_org": plan.purchasing_org,
                "purchasing_group": plan.purchasing_group,
                "currency": plan.currency,
                "valid_from": plan.valid_from.isoformat() if plan.valid_from else "",
                "valid_to": plan.valid_to.isoformat() if plan.valid_to else "",
                "target_value": str(plan.computed_target_value or ""),
                "reference_offer": plan.reference_offer, "items": neue,
                "created_at": datetime.now().isoformat(),
                "messages_suppressed": True,
            }
            meldungen = [f"Kontrakt {number} wurde angelegt",
                         "Nachrichtenfindung unterdrueckt (kein Versand)"]
            nachricht = f"Mengenkontrakt angelegt ({summary})"
        self.system.save()
        plan.document_number = number

        # Ohne Belegnummer waere voellig offen, was gesichert wurde.
        if not number:
            return self._result("contract", ResultState.FAILED,
                                "SAP hat keine Kontraktnummer gemeldet -- es ist unklar, "
                                "ob der Beleg angelegt wurde.",
                                transaction=transaction, started_ms=started)
        if self.settings.sap.verify_after_write and number not in self.system.contracts:
            return self._result("contract", ResultState.FAILED,
                                f"Ruecklese-Pruefung: Kontrakt {number} ist in "
                                f"{self.settings.transactions.contract_display} nicht "
                                f"auffindbar.",
                                transaction=transaction, document_number=number,
                                started_ms=started, sap_messages=meldungen)
        if self.settings.sap.verify_after_write:
            meldungen.append(f"Ruecklese-Pruefung: Kontrakt {number} existiert mit "
                             f"{len(self.system.contracts[number]['items'])} Position(en).")

        return self._result("contract", ResultState.SUCCESS, nachricht,
                            transaction=transaction, document_number=number,
                            new_value=summary, started_ms=started,
                            sap_messages=meldungen)


class MockPurchaseOrderService(_MockBase, PurchaseOrderServiceBase):
    def __init__(self, system, settings, selectors) -> None:  # noqa: D107
        _MockBase.__init__(self, system, settings, selectors)

    def create(self, plan: PurchaseOrderPlan, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        transaction = (self.settings.transactions.purchase_order_change if plan.is_change
                       else self.settings.transactions.purchase_order_create)

        # Kontierung erst vorbelegen, dann pruefen -- und zwar bevor irgend
        # etwas gespeichert wird.
        for item in plan.items:
            apply_account_assignment(item, self.settings)
        for item in plan.items:
            problem = validate_account_assignment(item, self.settings)
            if problem:
                return self._result("purchase_order", ResultState.FAILED, problem,
                                    transaction=transaction, started_ms=started)
        reference = f" mit Bezug auf Kontrakt {plan.reference_contract}" if plan.reference_contract else ""
        total = plan.net_total
        summary = (f"{len(plan.items)} Position(en){reference}"
                   + (f", Netto {format_decimal(total)} {plan.currency}" if total is not None else ""))

        if context.dry_run:
            return self._result("purchase_order", ResultState.SIMULATED,
                                f"Bestellung anlegen ({transaction}): {summary}",
                                transaction=transaction, new_value=summary, started_ms=started)

        number = plan.existing_order_number or self.system.next_number("purchase_order", "4500")
        items = []
        for index, item in enumerate(plan.items, start=1):
            item.item_number = f"{index * 10:05d}"
            items.append({
                "item": item.item_number, "material": item.material_number,
                "quantity": str(item.quantity) if item.quantity is not None else "",
                "uom": item.uom,
                "net_price": str(item.net_price) if item.net_price is not None else "",
                "price_unit": item.price_unit or 1, "plant": item.plant,
                "delivery_date": item.delivery_date.isoformat() if item.delivery_date else "",
                "contract_number": item.contract_number or plan.reference_contract,
                "contract_item": item.contract_item,
                "account_assignment": item.account_assignment,
                "cost_center": item.cost_center,
                "gl_account": item.gl_account,
            })
        self.system.purchase_orders[number] = {
            "document_type": plan.document_type, "vendor": plan.vendor_number,
            "purchasing_org": plan.purchasing_org, "purchasing_group": plan.purchasing_group,
            "currency": plan.currency, "reference_contract": plan.reference_contract,
            "reference_offer": plan.reference_offer,
            "delivery_date": plan.delivery_date.isoformat() if plan.delivery_date else "",
            "items": items, "created_at": datetime.now().isoformat(),
            "messages_suppressed": True,
        }
        self.system.save()
        plan.document_number = number

        meldungen = [f"Normalbestellung {number} wurde angelegt",
                     "Nachrichtenfindung unterdrueckt (kein Versand)"]
        if not number:
            return self._result("purchase_order", ResultState.FAILED,
                                "SAP hat keine Bestellnummer gemeldet -- es ist unklar, "
                                "ob die Bestellung angelegt wurde.",
                                transaction=transaction, started_ms=started)
        if self.settings.sap.verify_after_write:
            if number not in self.system.purchase_orders:
                return self._result("purchase_order", ResultState.FAILED,
                                    f"Ruecklese-Pruefung: Bestellung {number} ist in "
                                    f"{self.settings.transactions.purchase_order_display} "
                                    f"nicht auffindbar.",
                                    transaction=transaction, document_number=number,
                                    started_ms=started, sap_messages=meldungen)
            meldungen.append(f"Ruecklese-Pruefung: Bestellung {number} existiert.")

        return self._result("purchase_order", ResultState.SUCCESS,
                            f"Bestellung angelegt ({summary})",
                            transaction=transaction, document_number=number,
                            new_value=summary, started_ms=started,
                            sap_messages=meldungen)
