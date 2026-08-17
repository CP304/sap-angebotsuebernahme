"""Belegplanung: Mengenkontrakt (ME31K/ME32K) und Bestellung (ME21N/ME22N).

Infosatz und Orderbuch sind *pro Material/Lieferant* gepflegte Stammdaten --
sie werden Position fuer Position verarbeitet.  Kontrakt und Bestellung sind
dagegen *Belege*: ein Kopf, viele Positionen.  Deshalb werden die vom Anwender
angehakten Positionen hier zu Belegen gebuendelt (je Lieferant + Einkaufs-
organisation + Waehrung ein Beleg), bevor SAP angesprochen wird.

Das spart Transaktionswechsel -- eine der wichtigsten Regeln beim
GUI-Scripting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..utils.parsing import format_date, format_decimal
from .offer_position import OfferPosition


@dataclass
class DocumentItem:
    """Eine Belegposition (Kontrakt oder Bestellung)."""

    position_uid: int
    material_number: str = ""
    description: str = ""
    quantity: Decimal | None = None
    uom: str = ""
    net_price: Decimal | None = None
    price_unit: int | None = None
    plant: str = ""
    material_group: str = ""
    delivery_date: date | None = None
    account_assignment: str = ""
    cost_center: str = ""
    gl_account: str = ""
    #: Bezug auf die Kontraktposition (nur bei Bestellungen als Abruf).
    #: Ohne die Positionsnummer zieht SAP nicht die richtige Kontraktzeile.
    contract_number: str = ""
    contract_item: str = ""
    # Ergebnis nach der Verarbeitung
    item_number: str = ""
    error: str = ""

    def display(self) -> str:
        parts = [self.material_number or self.description[:30]]
        if self.quantity is not None:
            parts.append(f"{format_decimal(self.quantity, 3)} {self.uom}".strip())
        if self.net_price is not None:
            price = format_decimal(self.net_price)
            if self.price_unit:
                price += f" / {self.price_unit}"
            parts.append(price)
        return "  ".join(parts)


@dataclass
class ContractPlan:
    """Geplanter Mengenkontrakt (Belegart MK)."""

    vendor_number: str = ""
    vendor_name: str = ""
    purchasing_org: str = ""
    purchasing_group: str = ""
    plant: str = ""
    currency: str = ""
    document_type: str = "MK"          # MK = Mengenkontrakt, WK = Wertkontrakt
    valid_from: date | None = None
    valid_to: date | None = None
    payment_terms: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    target_value: Decimal | None = None
    reference_offer: str = ""
    header_text: str = ""

    #: Leer = neuer Kontrakt (ME31K); gesetzt = bestehenden erweitern (ME32K)
    existing_contract_number: str = ""

    items: list[DocumentItem] = field(default_factory=list)

    # Ergebnis
    document_number: str = ""
    error: str = ""

    @property
    def is_change(self) -> bool:
        return bool(self.existing_contract_number)

    @property
    def transaction_hint(self) -> str:
        return "ME32K" if self.is_change else "ME31K"

    @property
    def computed_target_value(self) -> Decimal | None:
        """Zielwert aus Menge x Preis, falls nicht manuell vorgegeben."""
        if self.target_value is not None:
            return self.target_value
        total = Decimal(0)
        seen = False
        for item in self.items:
            if item.quantity is None or item.net_price is None:
                continue
            unit = Decimal(item.price_unit or 1)
            total += item.quantity * item.net_price / unit
            seen = True
        return total if seen else None

    def summary(self) -> str:
        return (
            f"{self.transaction_hint} | Lieferant {self.vendor_number} | "
            f"{len(self.items)} Position(en) | Laufzeit "
            f"{format_date(self.valid_from) or '?'} – {format_date(self.valid_to) or '?'}"
        )


@dataclass
class PurchaseOrderPlan:
    """Geplante Bestellung (Belegart NB)."""

    vendor_number: str = ""
    vendor_name: str = ""
    purchasing_org: str = ""
    purchasing_group: str = ""
    plant: str = ""
    currency: str = ""
    document_type: str = "NB"          # NB = Normalbestellung
    document_date: date | None = None
    delivery_date: date | None = None
    payment_terms: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    reference_offer: str = ""
    header_text: str = ""
    your_reference: str = ""

    #: Leer = neue Bestellung (ME21N); gesetzt = bestehende erweitern (ME22N)
    existing_order_number: str = ""

    #: Optional: Bestellung mit Bezug auf diesen Kontrakt anlegen
    reference_contract: str = ""

    items: list[DocumentItem] = field(default_factory=list)

    # Ergebnis
    document_number: str = ""
    error: str = ""

    @property
    def is_change(self) -> bool:
        return bool(self.existing_order_number)

    @property
    def transaction_hint(self) -> str:
        return "ME22N" if self.is_change else "ME21N"

    @property
    def net_total(self) -> Decimal | None:
        total = Decimal(0)
        seen = False
        for item in self.items:
            if item.quantity is None or item.net_price is None:
                continue
            unit = Decimal(item.price_unit or 1)
            total += item.quantity * item.net_price / unit
            seen = True
        return total if seen else None

    def summary(self) -> str:
        total = self.net_total
        value = f" | Netto {format_decimal(total)} {self.currency}" if total is not None else ""
        return (
            f"{self.transaction_hint} | Lieferant {self.vendor_number} | "
            f"{len(self.items)} Position(en){value}"
        )


# ---------------------------------------------------------------------------
# Kontierung
# ---------------------------------------------------------------------------

def apply_account_assignment(item: DocumentItem, settings) -> None:
    """Kontierung einer Bestellposition vorbelegen.

    Was an der Position gepflegt ist, gewinnt immer.  Erst wenn dort nichts
    steht, greift die Vorbelegung aus den Einstellungen.  Es wird nichts
    erfunden: ist beides leer, bleibt das Feld leer -- die Position ist dann
    eine Lagerbestellung ohne Kontierungsbild.
    """
    purchasing = settings.purchasing
    if not item.account_assignment:
        item.account_assignment = purchasing.account_assignment_category or ""
    if not item.account_assignment:
        return
    if not item.cost_center:
        item.cost_center = purchasing.default_cost_center or ""
    if not item.gl_account:
        item.gl_account = purchasing.default_gl_account or ""


def validate_account_assignment(item: DocumentItem, settings) -> str:
    """Kontierung pruefen, BEVOR etwas nach SAP geschrieben wird.

    Liefert einen Klartext-Grund oder "" (in Ordnung).
    """
    category = (item.account_assignment or "").strip()
    if not category:
        return ""
    label = item.material_number or item.description[:40] or "Position"
    if settings.purchasing.require_gl_account_when_assigned and not item.gl_account:
        return (f"Position {label}: Kontierungstyp '{category}' ist gesetzt, aber es "
                f"fehlt das Sachkonto. Bitte Sachkonto pflegen oder die Kontierung "
                f"entfernen -- ohne Sachkonto wird nicht bestellt.")
    if category.upper() == "K" and not item.cost_center:
        return (f"Position {label}: Kontierungstyp 'K' verlangt eine Kostenstelle, "
                f"es ist aber keine hinterlegt.")
    return ""


# ---------------------------------------------------------------------------
# Buendelung
# ---------------------------------------------------------------------------

def _item_from_position(position: OfferPosition, quantity: Decimal | None,
                        delivery_date: date | None = None) -> DocumentItem:
    return DocumentItem(
        position_uid=position.uid,
        material_number=position.material_number,
        description=position.description,
        quantity=quantity,
        uom=position.uom,
        net_price=position.price,
        price_unit=position.price_unit,
        plant=position.plant,
        delivery_date=delivery_date,
        account_assignment=position.account_assignment,
        cost_center=position.cost_center,
        gl_account=position.gl_account,
    )


def build_contract_plans(positions: list[OfferPosition], *, vendor_names: dict[str, str],
                         purchasing_group: str, valid_from: date | None,
                         valid_to: date | None, offer_number: str = "",
                         payment_terms: str = "", incoterm: str = "",
                         incoterm_location: str = "",
                         document_type: str = "MK") -> list[ContractPlan]:
    """Angehakte Positionen zu Kontrakten je Lieferant/EKorg/Waehrung buendeln."""
    plans: dict[tuple[str, str, str], ContractPlan] = {}
    for position in positions:
        if not (position.selected and position.do_contract):
            continue
        key = (position.vendor_number, position.purchasing_org, position.currency)
        plan = plans.get(key)
        if plan is None:
            plan = ContractPlan(
                vendor_number=position.vendor_number,
                vendor_name=vendor_names.get(position.vendor_number, ""),
                purchasing_org=position.purchasing_org,
                purchasing_group=purchasing_group,
                plant=position.plant,
                currency=position.currency,
                document_type=document_type,
                valid_from=valid_from or position.valid_from,
                valid_to=valid_to,
                payment_terms=payment_terms,
                incoterm=incoterm,
                incoterm_location=incoterm_location,
                reference_offer=offer_number,
            )
            plans[key] = plan
        quantity = position.contract_quantity
        if quantity is None:
            quantity = position.quantity
        plan.items.append(_item_from_position(position, quantity))
    return list(plans.values())


def build_purchase_order_plans(positions: list[OfferPosition], *,
                               vendor_names: dict[str, str], purchasing_group: str,
                               default_delivery_date: date | None,
                               document_date: date | None = None,
                               offer_number: str = "", payment_terms: str = "",
                               incoterm: str = "", incoterm_location: str = "",
                               document_type: str = "NB") -> list[PurchaseOrderPlan]:
    """Angehakte Positionen zu Bestellungen je Lieferant/EKorg/Waehrung buendeln."""
    plans: dict[tuple[str, str, str], PurchaseOrderPlan] = {}
    for position in positions:
        if not (position.selected and position.do_purchase_order):
            continue
        key = (position.vendor_number, position.purchasing_org, position.currency)
        plan = plans.get(key)
        if plan is None:
            plan = PurchaseOrderPlan(
                vendor_number=position.vendor_number,
                vendor_name=vendor_names.get(position.vendor_number, ""),
                purchasing_org=position.purchasing_org,
                purchasing_group=purchasing_group,
                plant=position.plant,
                currency=position.currency,
                document_type=document_type,
                document_date=document_date,
                delivery_date=default_delivery_date,
                payment_terms=payment_terms,
                incoterm=incoterm,
                incoterm_location=incoterm_location,
                reference_offer=offer_number,
                your_reference=offer_number,
            )
            plans[key] = plan
        quantity = position.order_quantity
        if quantity is None:
            quantity = position.quantity
        plan.items.append(
            _item_from_position(position, quantity,
                                position.delivery_date or default_delivery_date)
        )
    return list(plans.values())
