"""Vorschau des Komplettvorgangs ("Was passiert jetzt in SAP?").

Vor jedem Schreibvorgang bekommt der Anwender genau eine Zusammenfassung zu
sehen -- kurz, in ganzen Saetzen und ohne Fachjargon:

    17 Positionen ausgewählt
    Infosätze: 12 ändern, 3 neu anlegen, 2 unverändert
    Orderbuch: 4 neu anlegen, 6 prüfen/ändern, 7 unverändert
    Mengenkontrakte: 2 Belege mit 15 Positionen
    Bestellungen: 2 Belege mit 15 Positionen (Abruf 20 %)
    Warnungen: 2

Die Vorschau rechnet dabei auch die Abrufmenge der Bestellungen aus und
schreibt sie in die Position zurueck, damit in der Tabelle steht, was gleich
tatsaechlich bestellt wird.

Voraussetzung: ``ComparisonService`` und ``ValidationService`` sind vorher
gelaufen -- die Vorschau zaehlt nur zusammen, sie bewertet nicht neu.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..config.settings import Settings
from ..models.document_plan import (
    ContractPlan,
    PurchaseOrderPlan,
    build_contract_plans,
    build_purchase_order_plans,
)
from ..models.enums import InfoRecordAction, IssueSeverity, SourceListAction
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..utils.parsing import format_decimal
from .comparison_service import comparison_currency, offer_currency
from .currency_service import CurrencyService

logger = logging.getLogger(__name__)

__all__ = ["PreviewService", "PreviewSummary", "call_off_quantity"]


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def add_months(day: date, months: int) -> date:
    """Datum um Monate verschieben (ohne externe Bibliothek)."""
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def call_off_quantity(base: Decimal | None, settings: Settings) -> Decimal | None:
    """Abrufmenge einer Bestellung aus der Kontraktmenge berechnen.

    ``percent``  -- Anteil der Kontrakt-Zielmenge
    ``absolute`` -- feste Menge aus den Einstellungen
    ``full``     -- volle Menge

    Bei ``call_off_round_to_integer`` wird kaufmaennisch auf ganze Einheiten
    gerundet, mindestens jedoch auf 1.
    """
    workflow = settings.workflow
    mode = (workflow.call_off_mode or "full").strip().lower()

    if mode == "absolute":
        quantity = workflow.call_off_quantity
        if quantity is None:
            quantity = base
        else:
            quantity = Decimal(str(quantity))
    elif mode == "percent":
        if base is None:
            return None
        percent = Decimal(str(workflow.call_off_percent or 0))
        quantity = base * percent / Decimal(100)
    else:
        quantity = base

    if quantity is None:
        return None

    if workflow.call_off_round_to_integer:
        try:
            quantity = quantity.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return quantity
        if quantity < 1:
            quantity = Decimal(1)
    return quantity


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class PreviewSummary:
    """Was der Komplettvorgang gleich tun wird -- fertig aufbereitet."""

    positions_selected: int = 0

    info_records_create: int = 0
    info_records_update: int = 0
    info_records_unchanged: int = 0

    source_lists_create: int = 0
    source_lists_update: int = 0
    source_lists_unchanged: int = 0

    contract_plans: list[ContractPlan] = field(default_factory=list)
    purchase_order_plans: list[PurchaseOrderPlan] = field(default_factory=list)

    #: Fremdwaehrungen der ausgewaehlten Positionen (Kuerzel, sortiert)
    foreign_currencies: list[str] = field(default_factory=list)
    #: Klartextzeilen zur Fremdwaehrung (Kurs, Kursalter, Hinweis zum Schreiben)
    currency_lines: list[str] = field(default_factory=list)

    warnings: int = 0
    errors: int = 0
    blocking: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    dry_run: bool = False
    chain_order: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def contract_items(self) -> int:
        return sum(len(plan.items) for plan in self.contract_plans)

    @property
    def purchase_order_items(self) -> int:
        return sum(len(plan.items) for plan in self.purchase_order_plans)

    @property
    def info_records_total(self) -> int:
        return self.info_records_create + self.info_records_update

    @property
    def source_lists_total(self) -> int:
        return self.source_lists_create + self.source_lists_update

    @property
    def has_work(self) -> bool:
        return bool(self.info_records_total or self.source_lists_total
                    or self.contract_plans or self.purchase_order_plans)

    def as_text(self) -> str:
        """Mehrzeiliger Text fuer den Bestaetigungsdialog."""
        return "\n".join(self.lines)

    def headline(self) -> str:
        """Einzeiler fuer die Statusleiste."""
        return " / ".join(self.lines)


# ---------------------------------------------------------------------------
# Dienst
# ---------------------------------------------------------------------------

class PreviewService:
    """Baut aus dem bewerteten Angebot die Vorschau des Komplettvorgangs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    def build(self, offer: Offer, settings: Settings | None = None) -> PreviewSummary:
        """Vorschau erzeugen und Abrufmengen in die Positionen zurueckschreiben."""
        settings = settings or self.settings
        if settings is None:
            raise ValueError("Es wurden keine Einstellungen uebergeben.")

        summary = PreviewSummary(dry_run=settings.dry_run)
        selected = [p for p in offer.positions if p.selected]
        summary.positions_selected = len(selected)

        self._count_actions(selected, summary)
        self._fill_call_off_quantities(selected, settings)
        self._build_plans(offer, selected, settings, summary)
        self._count_issues(offer, selected, summary)
        self._check_currencies(selected, settings, summary)
        summary.chain_order = self._chain_order(settings, summary)
        summary.lines = self._build_lines(settings, summary)

        logger.info("Vorschau erstellt: %s", summary.headline())
        return summary

    # ------------------------------------------------------------------
    def _count_actions(self, selected: list[OfferPosition], summary: PreviewSummary) -> None:
        for position in selected:
            if position.do_info_record:
                action = position.info_record_action
                if action is InfoRecordAction.CREATE:
                    summary.info_records_create += 1
                elif action is InfoRecordAction.UPDATE:
                    summary.info_records_update += 1
                elif action is InfoRecordAction.UNCHANGED:
                    summary.info_records_unchanged += 1
            if position.do_source_list:
                action = position.source_list_action
                if action is SourceListAction.CREATE:
                    summary.source_lists_create += 1
                elif action is SourceListAction.UPDATE:
                    summary.source_lists_update += 1
                elif action is SourceListAction.UNCHANGED:
                    summary.source_lists_unchanged += 1

    # ------------------------------------------------------------------
    def _fill_call_off_quantities(self, selected: list[OfferPosition],
                                  settings: Settings) -> None:
        """Abrufmenge berechnen -- aber nur, wo noch nichts eingetragen ist."""
        for position in selected:
            if not position.do_purchase_order:
                continue
            if position.order_quantity is not None:
                continue
            base = position.contract_quantity
            if base is None:
                base = position.quantity
            quantity = call_off_quantity(base, settings)
            if quantity is not None:
                position.order_quantity = quantity

    # ------------------------------------------------------------------
    def _build_plans(self, offer: Offer, selected: list[OfferPosition],
                     settings: Settings, summary: PreviewSummary) -> None:
        purchasing = settings.purchasing
        vendor_names = {position.vendor_number: offer.vendor_name
                        for position in selected if position.vendor_number}
        if offer.vendor_number:
            vendor_names.setdefault(offer.vendor_number, offer.vendor_name)

        today = date.today()
        contract_from = offer.valid_from or today
        contract_to = settings.parsed_contract_valid_to()
        if contract_to is None:
            contract_to = add_months(contract_from, purchasing.contract_duration_months)

        summary.contract_plans = build_contract_plans(
            selected,
            vendor_names=vendor_names,
            purchasing_group=purchasing.purchasing_group,
            valid_from=contract_from,
            valid_to=contract_to,
            offer_number=offer.offer_number,
            payment_terms=offer.payment_terms,
            incoterm=offer.incoterm,
            incoterm_location=offer.incoterm_location,
            document_type=purchasing.contract_document_type,
        )

        delivery = today + timedelta(days=purchasing.default_delivery_days)
        summary.purchase_order_plans = build_purchase_order_plans(
            selected,
            vendor_names=vendor_names,
            purchasing_group=purchasing.purchasing_group,
            default_delivery_date=delivery,
            document_date=today,
            offer_number=offer.offer_number,
            payment_terms=offer.payment_terms,
            incoterm=offer.incoterm,
            incoterm_location=offer.incoterm_location,
            document_type=purchasing.purchase_order_document_type,
        )

    # ------------------------------------------------------------------
    def _count_issues(self, offer: Offer, selected: list[OfferPosition],
                      summary: PreviewSummary) -> None:
        for issue in offer.issues:
            if issue.acknowledged:
                continue
            if issue.severity is IssueSeverity.WARNING:
                summary.warnings += 1
            elif issue.severity is IssueSeverity.ERROR:
                summary.errors += 1

        for position in selected:
            for issue in position.issues:
                if issue.acknowledged:
                    continue
                if issue.severity is IssueSeverity.WARNING:
                    summary.warnings += 1
                elif issue.severity is IssueSeverity.ERROR:
                    summary.errors += 1
            if position.issues.has_blocking:
                reasons = "; ".join(i.message for i in position.issues
                                    if i.blocking and not i.acknowledged)
                summary.blocking.append(
                    f"Position {position.position_number or position.uid} "
                    f"({position.display_name}): {reasons}")

    # ------------------------------------------------------------------
    def _check_currencies(self, selected: list[OfferPosition], settings: Settings,
                          summary: PreviewSummary) -> None:
        """Fremdwaehrungen der Auswahl aufbereiten.

        Vor dem Schreiben muss unmissverstaendlich dastehen, welche Waehrungen
        im Spiel sind, mit welchem (wie alten) Kurs verglichen wurde -- und
        dass nach SAP der Originalbetrag in der Originalwaehrung geht.
        Fehlt zu einer ausgewaehlten Position der Kurs, ist das ein Eintrag in
        ``blocking``: der Anwender muss ausdruecklich bestaetigen.
        """
        service = CurrencyService(settings)
        fremde: list[str] = []
        ohne_kurs: list[OfferPosition] = []

        for position in selected:
            quelle = offer_currency(position, settings)
            ziel = comparison_currency(position, settings)
            if not quelle or not ziel or quelle == ziel:
                continue
            if quelle not in fremde:
                fremde.append(quelle)
            if not settings.currency.convert_for_comparison:
                continue
            if service.rate(quelle, ziel) is None:
                ohne_kurs.append(position)

        summary.foreign_currencies = sorted(fremde)
        if not fremde:
            return

        zeilen: list[str] = [
            "Fremdwährung: " + ", ".join(summary.foreign_currencies)
        ]
        if not settings.currency.convert_for_comparison:
            zeilen.append("Umrechnung für den Vergleich ist abgeschaltet – "
                          "es wird keine Preisänderung in Prozent ausgewiesen.")
        else:
            kurse = service.rate_info_text(summary.foreign_currencies)
            if kurse:
                zeilen.append(f"Verwendete Kurse: {kurse}")
            alter = service.rate_age_days()
            if alter is None:
                zeilen.append("Kursalter: kein Pflegedatum hinterlegt.")
            elif service.is_stale():
                zeilen.append(
                    f"Kursalter: {alter} Tage – älter als die zulässigen "
                    f"{settings.currency.max_rate_age_days} Tage.")
            else:
                zeilen.append(f"Kursalter: {alter} Tage.")
        zeilen.append("Nach SAP wird immer der Originalbetrag in der "
                      "Originalwährung des Angebots geschrieben – nie ein "
                      "umgerechneter Wert.")

        summary.currency_lines = zeilen

        for position in ohne_kurs:
            quelle = offer_currency(position, settings)
            summary.blocking.append(
                f"Position {position.position_number or position.uid} "
                f"({position.display_name}): kein Umrechnungskurs für {quelle} "
                f"hinterlegt – die Preisänderung ist nicht überprüfbar.")

    # ------------------------------------------------------------------
    def _chain_order(self, settings: Settings, summary: PreviewSummary) -> list[str]:
        """Feste Reihenfolge des Komplettvorgangs -- nur die aktiven Schritte."""
        transactions = settings.transactions
        steps: list[str] = []
        if summary.info_records_total:
            steps.append(f"Infosätze ({transactions.info_record_create}/"
                         f"{transactions.info_record_change})")
        if summary.contract_plans:
            steps.append(f"Mengenkontrakte ({transactions.contract_create})")
        if summary.source_lists_total:
            steps.append(f"Orderbuch ({transactions.source_list_maintain})")
        if summary.purchase_order_plans:
            steps.append(f"Bestellungen ({transactions.purchase_order_create})")
        return [f"{index}. {step}" for index, step in enumerate(steps, start=1)]

    # ------------------------------------------------------------------
    def _call_off_text(self, settings: Settings) -> str:
        workflow = settings.workflow
        mode = (workflow.call_off_mode or "full").strip().lower()
        if mode == "percent":
            return f"Abruf {format_decimal(Decimal(str(workflow.call_off_percent or 0)), 0)} %"
        if mode == "absolute":
            if workflow.call_off_quantity is None:
                return "Abruf volle Menge"
            return f"Abruf feste Menge {format_decimal(Decimal(str(workflow.call_off_quantity)), 3)}"
        return "Abruf volle Menge"

    def _build_lines(self, settings: Settings, summary: PreviewSummary) -> list[str]:
        lines: list[str] = [
            _plural(summary.positions_selected, "Position ausgewählt", "Positionen ausgewählt")
        ]

        info_parts: list[str] = []
        if summary.info_records_update:
            info_parts.append(f"{summary.info_records_update} ändern")
        if summary.info_records_create:
            info_parts.append(f"{summary.info_records_create} neu anlegen")
        if summary.info_records_unchanged:
            info_parts.append(f"{summary.info_records_unchanged} unverändert")
        if info_parts:
            lines.append("Infosätze: " + ", ".join(info_parts))

        source_parts: list[str] = []
        if summary.source_lists_create:
            source_parts.append(f"{summary.source_lists_create} neu anlegen")
        if summary.source_lists_update:
            source_parts.append(f"{summary.source_lists_update} prüfen/ändern")
        if summary.source_lists_unchanged:
            source_parts.append(f"{summary.source_lists_unchanged} unverändert")
        if source_parts:
            lines.append("Orderbuch: " + ", ".join(source_parts))

        if summary.contract_plans:
            lines.append(
                "Mengenkontrakte: "
                + _plural(len(summary.contract_plans), "Beleg", "Belege")
                + " mit "
                + _plural(summary.contract_items, "Position", "Positionen")
            )

        if summary.purchase_order_plans:
            lines.append(
                "Bestellungen: "
                + _plural(len(summary.purchase_order_plans), "Beleg", "Belege")
                + " mit "
                + _plural(summary.purchase_order_items, "Position", "Positionen")
                + f" ({self._call_off_text(settings)})"
            )

        lines.extend(summary.currency_lines)

        if summary.warnings:
            lines.append(f"Warnungen: {summary.warnings}")
        if summary.errors:
            lines.append(f"Fehler: {summary.errors}")
        if not summary.has_work:
            lines.append("Es ist nichts zu tun – der SAP-Stand entspricht bereits dem Angebot.")
        if summary.dry_run:
            lines.append("Betriebsart: Dry Run – es wird nichts in SAP geschrieben.")
        return lines
