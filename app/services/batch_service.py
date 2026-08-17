"""Der Komplettvorgang: aus einem Angebot alles in einem Rutsch nach SAP.

Reihenfolge (fest verdrahtet, siehe ``WorkflowDefaults``)
--------------------------------------------------------
    1. Infosaetze je Position     (ME11/ME12)
    2. Mengenkontrakte je Beleg   (ME31K)  -- liefert die Kontraktnummer
    3. Orderbuch je Position      (ME01)   -- traegt den Kontrakt als Vereinbarung ein
    4. Bestellungen je Beleg      (ME21N)  -- Abruf mit Bezug auf den Kontrakt

Wer die Reihenfolge aendert, verliert den Belegbezug.

Fehlerverhalten
---------------
* **Isolation:** Ein Fehler in einer Position beendet den Lauf nicht.  Die
  uebrigen Positionen werden weiter verarbeitet.
* **Kein falscher Belegbezug:** Scheitert ein Kontrakt, werden die davon
  abhaengigen Bestellungen *nicht* ohne Bezug angelegt, sondern mit klarer
  Begruendung uebersprungen.  Ohne Kontraktbezug wird nur dann bestellt, wenn
  ``purchase_order_from_contract`` ausgeschaltet ist.
* **Harter Abbruch** bei ``MessageSuppressionError`` (es koennte etwas an den
  Lieferanten hinausgehen) und bei einem unerwarteten Popup im Belegteil (der
  SAP-Zustand ist dann unklar).  Der Rest wird uebersprungen.
* **Abbruch durch den Anwender:** Die laufende Aktion wird zu Ende gefuehrt,
  danach wird sauber abgebrochen.
* Vor *jeder* Schreibaktion wird erneut validiert.  Wer zwischenzeitlich
  blockierend geworden ist, wird uebersprungen statt geschrieben.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Callable

from ..config.settings import Settings
from ..models.document_plan import ContractPlan, PurchaseOrderPlan
from ..models.enums import InfoRecordAction, PositionStatus, ResultState, SourceListAction
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..models.results import ActionResult, BatchSummary, PositionResult
from ..sap.connection import SapError, SapPopupError
from ..sap.gateway import SapGateway
from ..sap.message_guard import MessageSuppressionError
from .comparison_service import ComparisonService
from .preview_service import PreviewSummary
from .validation_service import ValidationService

logger = logging.getLogger(__name__)

__all__ = ["BatchProcessor", "ProgressEvent"]

#: Verarbeitungsschritte in ihrer festen Reihenfolge
PHASES: tuple[str, ...] = ("info_record", "contract", "source_list", "purchase_order")

PHASE_LABELS: dict[str, str] = {
    "info_record": "Infosatz",
    "contract": "Mengenkontrakt",
    "source_list": "Orderbuch",
    "purchase_order": "Bestellung",
    "done": "fertig",
}


# ---------------------------------------------------------------------------
# Fortschritt
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """Eine Rueckmeldung waehrend des Laufs -- reicht fuer Fortschrittsbalken
    und Protokollfenster."""

    index: int
    total: int
    position_uid: int
    label: str
    phase: str
    message: str
    result: ActionResult | None = None

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 100
        return min(100, int(self.index * 100 / self.total))

    @property
    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)

    def display(self) -> str:
        return f"[{self.index}/{self.total}] {self.phase_label}: {self.message}"


# ---------------------------------------------------------------------------
# Interne Steuerung
# ---------------------------------------------------------------------------

class _HardAbort(Exception):
    """Der Lauf muss sofort angehalten werden (unklarer SAP-Zustand)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _Cancelled(Exception):
    """Der Anwender hat abgebrochen."""


@dataclass
class _Run:
    """Zustand eines laufenden Vorgangs."""

    offer: Offer
    preview: PreviewSummary
    summary: BatchSummary
    progress: Callable[[ProgressEvent], None] | None = None
    is_cancelled: Callable[[], bool] | None = None

    positions: dict[int, OfferPosition] = field(default_factory=dict)
    results: dict[int, PositionResult] = field(default_factory=dict)

    info_units: list[OfferPosition] = field(default_factory=list)
    contract_units: list[ContractPlan] = field(default_factory=list)
    source_units: list[OfferPosition] = field(default_factory=list)
    order_units: list[PurchaseOrderPlan] = field(default_factory=list)

    done: set[tuple[str, int]] = field(default_factory=set)
    blocked_uids: set[int] = field(default_factory=set)
    contract_numbers: dict[int, tuple[str, str]] = field(default_factory=dict)
    contract_failed_uids: set[int] = field(default_factory=set)

    index: int = 0
    total: int = 0


# ---------------------------------------------------------------------------
# Verarbeitung
# ---------------------------------------------------------------------------

class BatchProcessor:
    """Fuehrt den Komplettvorgang gegen SAP (oder das Testsystem) aus."""

    def __init__(self, gateway: SapGateway, settings: Settings,
                 comparison: ComparisonService, validation: ValidationService) -> None:
        self.gateway = gateway
        self.settings = settings
        self.comparison = comparison
        self.validation = validation

    # ------------------------------------------------------------------
    # Einstieg
    # ------------------------------------------------------------------
    def run(self, offer: Offer, preview: PreviewSummary,
            progress: Callable[[ProgressEvent], None] | None = None,
            is_cancelled: Callable[[], bool] | None = None) -> BatchSummary:
        """Kompletten Vorgang ausfuehren und das Gesamtergebnis liefern."""
        summary = BatchSummary(dry_run=self.settings.dry_run, started_at=datetime.now())
        run = _Run(offer=offer, preview=preview, summary=summary,
                   progress=progress, is_cancelled=is_cancelled)

        selected = [p for p in offer.positions if p.selected]
        for position in selected:
            run.positions[position.uid] = position
            run.results[position.uid] = PositionResult(position_uid=position.uid,
                                                       label=position.display_name)
        summary.results = [run.results[p.uid] for p in selected]

        self._prepare(run, selected)
        self._plan_units(run, selected)

        logger.info("Komplettvorgang gestartet: %d Einheit(en), Dry Run=%s",
                    run.total, summary.dry_run)

        try:
            self._phase_info_records(run)
            self._phase_contracts(run)
            self._phase_source_lists(run)
            self._phase_purchase_orders(run)
        except _Cancelled:
            reason = "Der Vorgang wurde vom Anwender abgebrochen."
            summary.aborted = True
            summary.abort_reason = reason
            self._skip_remaining(run, reason)
            logger.warning("Komplettvorgang abgebrochen (Anwender).")
        except _HardAbort as abort:
            summary.aborted = True
            summary.abort_reason = abort.reason
            self._skip_remaining(run, abort.reason)
            logger.error("Komplettvorgang angehalten: %s", abort.reason)

        self._finalize(run, selected)
        summary.finished_at = datetime.now()
        self._emit(run, None, "done", summary.headline(), None, advance=False)
        logger.info("Komplettvorgang beendet: %s", summary.headline())
        return summary

    # ------------------------------------------------------------------
    # Vorbereitung
    # ------------------------------------------------------------------
    def _prepare(self, run: _Run, selected: list[OfferPosition]) -> None:
        """SAP-Ist-Zustand frisch lesen, neu vergleichen und neu validieren.

        Zwischen Vorschau und Schreibvorgang koennen Minuten liegen -- in
        dieser Zeit kann sich in SAP etwas geaendert haben.
        """
        for position in selected:
            try:
                self.gateway.load_position_state(position)
                if position.material_number:
                    material = self.gateway.check_material(position.material_number,
                                                           position.plant)
                    self.validation.apply_sap_flags(position, material=material)
                if position.vendor_number:
                    vendor = self.gateway.check_vendor(position.vendor_number,
                                                       position.purchasing_org)
                    if vendor is None:
                        position.vendor_exists = False
                    else:
                        self.validation.apply_sap_flags(position, vendor=vendor)
            except SapError as exc:
                logger.error("SAP-Stand der Position %s nicht lesbar: %s", position.uid, exc)
                run.results[position.uid].actions.append(ActionResult(
                    action="info_record", state=ResultState.FAILED,
                    message=f"SAP-Stand konnte nicht gelesen werden: {exc.message}",
                    detail=getattr(exc, "detail", ""),
                ))
            self.comparison.compare_position(position)
            self.validation.validate_position(position, run.offer)

    def _plan_units(self, run: _Run, selected: list[OfferPosition]) -> None:
        run.info_units = [p for p in selected
                          if p.do_info_record and p.info_record_action in
                          (InfoRecordAction.CREATE, InfoRecordAction.UPDATE)]
        run.contract_units = list(run.preview.contract_plans)
        run.source_units = [p for p in selected
                            if p.do_source_list and p.source_list_action in
                            (SourceListAction.CREATE, SourceListAction.UPDATE)]
        run.order_units = list(run.preview.purchase_order_plans)
        run.total = (len(run.info_units) + len(run.contract_units)
                     + len(run.source_units) + len(run.order_units))

    # ------------------------------------------------------------------
    # 1. Infosaetze
    # ------------------------------------------------------------------
    def _phase_info_records(self, run: _Run) -> None:
        for position in run.info_units:
            key = ("info_record", position.uid)
            if key in run.done:
                continue
            self._guard_cancel(run)
            run.done.add(key)
            if not self._position_ready(run, position, "info_record"):
                continue

            position.status = PositionStatus.RUNNING
            context = self.gateway.write_context(
                valid_from=position.valid_from or date.today(),
                valid_to=self.settings.parsed_info_record_valid_to(),
            )
            try:
                result = self.gateway.info_records.write(position, context)
            except MessageSuppressionError as exc:
                raise _HardAbort(exc.message, getattr(exc, "detail", "")) from exc
            except SapError as exc:
                result = self._failure("info_record", exc)
            if result.ok and result.document_number:
                position.created_info_record = result.document_number
            self._record(run, position, result, "info_record")

    # ------------------------------------------------------------------
    # 2. Mengenkontrakte
    # ------------------------------------------------------------------
    def _phase_contracts(self, run: _Run) -> None:
        for index, plan in enumerate(run.contract_units):
            key = ("contract", index)
            if key in run.done:
                continue
            self._guard_cancel(run)
            run.done.add(key)

            kept = []
            for item in plan.items:
                position = run.positions.get(item.position_uid)
                if position is None or not self._position_ready(run, position, "contract"):
                    run.contract_failed_uids.add(item.position_uid)
                    continue
                kept.append(item)
            plan.items = kept

            if not kept:
                plan.error = "Keine verarbeitbare Position im Beleg."
                run.summary.document_results.append(ActionResult(
                    action="contract", state=ResultState.SKIPPED,
                    message=("Mengenkontrakt übersprungen: keine verarbeitbare Position "
                             "im Beleg."),
                ))
                run.index += 1
                continue

            context = self.gateway.write_context(valid_from=plan.valid_from,
                                                 valid_to=plan.valid_to)
            try:
                result = self.gateway.contracts.create(plan, context)
            except MessageSuppressionError as exc:
                self._fail_plan_positions(run, plan, "contract", exc.message)
                raise _HardAbort(exc.message, getattr(exc, "detail", "")) from exc
            except SapPopupError as exc:
                self._fail_plan_positions(run, plan, "contract", exc.message)
                raise _HardAbort(
                    "Unerwartetes SAP-Fenster im Belegteil – der Zustand in SAP ist unklar. "
                    f"({exc.message})", getattr(exc, "detail", "")) from exc
            except SapError as exc:
                result = self._failure("contract", exc)

            run.summary.document_results.append(result)
            number = plan.document_number or result.document_number
            if result.ok:
                plan.document_number = number
                for item in plan.items:
                    run.contract_numbers[item.position_uid] = (number, item.item_number)
            else:
                plan.error = result.message
                for item in plan.items:
                    run.contract_failed_uids.add(item.position_uid)

            self._record_plan(run, plan, result, "contract", number)

    # ------------------------------------------------------------------
    # 3. Orderbuch
    # ------------------------------------------------------------------
    def _phase_source_lists(self, run: _Run) -> None:
        workflow = self.settings.workflow
        for position in run.source_units:
            key = ("source_list", position.uid)
            if key in run.done:
                continue
            self._guard_cancel(run)
            run.done.add(key)
            if not self._position_ready(run, position, "source_list"):
                continue

            contract_number, contract_item = "", ""
            note = ""
            if workflow.source_list_reference_contract:
                contract_number, contract_item = run.contract_numbers.get(position.uid, ("", ""))
                if not contract_number and position.uid in run.contract_failed_uids:
                    note = (" Hinweis: ohne Kontraktbezug, weil der Mengenkontrakt nicht "
                            "angelegt werden konnte.")

            position.status = PositionStatus.RUNNING
            context = self.gateway.write_context(
                valid_from=position.valid_from or date.today(),
                valid_to=self.settings.parsed_source_list_valid_to(),
            )
            try:
                result = self.gateway.source_lists.write(
                    position, context, contract_number=contract_number,
                    contract_item=contract_item)
            except MessageSuppressionError as exc:
                raise _HardAbort(exc.message, getattr(exc, "detail", "")) from exc
            except SapError as exc:
                result = self._failure("source_list", exc)
            if note:
                result.message = f"{result.message}{note}"
            self._record(run, position, result, "source_list")

    # ------------------------------------------------------------------
    # 4. Bestellungen
    # ------------------------------------------------------------------
    def _phase_purchase_orders(self, run: _Run) -> None:
        from_contract = self.settings.workflow.purchase_order_from_contract

        for index, plan in enumerate(run.order_units):
            key = ("purchase_order", index)
            if key in run.done:
                continue
            self._guard_cancel(run)
            run.done.add(key)

            kept = []
            for item in plan.items:
                position = run.positions.get(item.position_uid)
                if position is None or not self._position_ready(run, position, "purchase_order"):
                    continue
                if from_contract and item.position_uid in run.contract_failed_uids:
                    # Lieber keine Bestellung als eine ohne den vereinbarten Bezug.
                    self._record(run, position, ActionResult(
                        action="purchase_order", state=ResultState.SKIPPED,
                        message=("Bestellung übersprungen: der zugehörige Mengenkontrakt "
                                 "konnte nicht angelegt werden, ein Abruf ohne Belegbezug "
                                 "wäre falsch."),
                    ), "purchase_order", advance=False)
                    continue
                kept.append(item)
            plan.items = kept

            if not kept:
                plan.error = "Keine verarbeitbare Position im Beleg."
                run.summary.document_results.append(ActionResult(
                    action="purchase_order", state=ResultState.SKIPPED,
                    message="Bestellung übersprungen: keine verarbeitbare Position im Beleg.",
                ))
                run.index += 1
                continue

            if from_contract:
                # Kopfbezug *und* Positionsbezug: ohne die Kontraktposition kann
                # SAP die richtige Kontraktzeile nicht ziehen.
                for item in kept:
                    number, contract_item = run.contract_numbers.get(
                        item.position_uid, ("", ""))
                    if not number:
                        continue
                    item.contract_number = number
                    item.contract_item = contract_item
                    if not plan.reference_contract:
                        plan.reference_contract = number

            context = self.gateway.write_context(valid_from=date.today(),
                                                 valid_to=plan.delivery_date)
            try:
                result = self.gateway.purchase_orders.create(plan, context)
            except MessageSuppressionError as exc:
                self._fail_plan_positions(run, plan, "purchase_order", exc.message)
                raise _HardAbort(exc.message, getattr(exc, "detail", "")) from exc
            except SapPopupError as exc:
                self._fail_plan_positions(run, plan, "purchase_order", exc.message)
                raise _HardAbort(
                    "Unerwartetes SAP-Fenster im Belegteil – der Zustand in SAP ist unklar. "
                    f"({exc.message})", getattr(exc, "detail", "")) from exc
            except SapError as exc:
                result = self._failure("purchase_order", exc)

            run.summary.document_results.append(result)
            number = plan.document_number or result.document_number
            if result.ok:
                plan.document_number = number
            else:
                plan.error = result.message
            self._record_plan(run, plan, result, "purchase_order", number)

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------
    def _guard_cancel(self, run: _Run) -> None:
        if run.is_cancelled is not None and run.is_cancelled():
            raise _Cancelled()

    def _position_ready(self, run: _Run, position: OfferPosition, phase: str) -> bool:
        """Vorbedingungen unmittelbar vor der Schreibaktion erneut pruefen."""
        if position.uid in run.blocked_uids:
            return False
        self.validation.validate_position(position, run.offer)
        if not position.issues.has_blocking:
            return True

        run.blocked_uids.add(position.uid)
        reasons = "; ".join(issue.message for issue in position.issues
                            if issue.blocking and not issue.acknowledged)
        result = ActionResult(
            action=phase, state=ResultState.SKIPPED,
            message=f"Übersprungen: {reasons}" if reasons else "Übersprungen",
        )
        self._record(run, position, result, phase, advance=False)
        position.status = PositionStatus.SKIPPED
        logger.info("Position %s übersprungen: %s", position.uid, reasons)
        return False

    @staticmethod
    def _failure(action: str, exc: SapError) -> ActionResult:
        return ActionResult(action=action, state=ResultState.FAILED,
                            message=exc.message if hasattr(exc, "message") else str(exc),
                            detail=getattr(exc, "detail", ""))

    def _record(self, run: _Run, position: OfferPosition, result: ActionResult,
                phase: str, advance: bool = True) -> None:
        position_result = run.results.get(position.uid)
        if position_result is None:
            position_result = PositionResult(position_uid=position.uid,
                                             label=position.display_name)
            run.results[position.uid] = position_result
            run.summary.results.append(position_result)
        if position_result.started_at is None:
            position_result.started_at = datetime.now()
        position_result.actions.append(result)
        position_result.finished_at = datetime.now()
        if advance:
            run.index += 1
        self._emit(run, position, phase, result.message, result)

    def _record_plan(self, run: _Run, plan: ContractPlan | PurchaseOrderPlan,
                     result: ActionResult, phase: str, number: str) -> None:
        """Belegergebnis auf die beteiligten Positionen verteilen."""
        run.index += 1
        for item in plan.items:
            position = run.positions.get(item.position_uid)
            if position is None:
                continue
            document = f"{number}/{item.item_number}" if number and item.item_number else number
            item_result = replace(result, document_number=document)
            if result.ok and document:
                if phase == "contract":
                    position.created_contract = document
                else:
                    position.created_purchase_order = document
            self._record(run, position, item_result, phase, advance=False)

    def _fail_plan_positions(self, run: _Run, plan: ContractPlan | PurchaseOrderPlan,
                             phase: str, message: str) -> None:
        for item in plan.items:
            if phase == "contract":
                run.contract_failed_uids.add(item.position_uid)
            position = run.positions.get(item.position_uid)
            if position is None:
                continue
            self._record(run, position, ActionResult(
                action=phase, state=ResultState.FAILED,
                message=f"Sicherheitsabbruch: {message}",
            ), phase, advance=False)

    # ------------------------------------------------------------------
    def _skip_remaining(self, run: _Run, reason: str) -> None:
        """Alles, was nicht mehr ausgefuehrt wurde, sauber als uebersprungen
        kennzeichnen -- es darf nichts unkommentiert liegen bleiben."""
        for position in run.info_units:
            self._skip_unit(run, ("info_record", position.uid), position, "info_record", reason)
        for index, plan in enumerate(run.contract_units):
            self._skip_plan(run, ("contract", index), plan, "contract", reason)
        for position in run.source_units:
            self._skip_unit(run, ("source_list", position.uid), position, "source_list", reason)
        for index, plan in enumerate(run.order_units):
            self._skip_plan(run, ("purchase_order", index), plan, "purchase_order", reason)

    def _skip_unit(self, run: _Run, key: tuple[str, int], position: OfferPosition,
                   phase: str, reason: str) -> None:
        if key in run.done:
            return
        run.done.add(key)
        self._record(run, position, ActionResult(
            action=phase, state=ResultState.SKIPPED,
            message=f"Nicht mehr ausgeführt: {reason}",
        ), phase)

    def _skip_plan(self, run: _Run, key: tuple[str, int],
                   plan: ContractPlan | PurchaseOrderPlan, phase: str, reason: str) -> None:
        if key in run.done:
            return
        run.done.add(key)
        run.summary.document_results.append(ActionResult(
            action=phase, state=ResultState.SKIPPED,
            message=f"Nicht mehr ausgeführt: {reason}",
        ))
        run.index += 1
        for item in plan.items:
            position = run.positions.get(item.position_uid)
            if position is None:
                continue
            self._record(run, position, ActionResult(
                action=phase, state=ResultState.SKIPPED,
                message=f"Nicht mehr ausgeführt: {reason}",
            ), phase, advance=False)

    # ------------------------------------------------------------------
    def _finalize(self, run: _Run, selected: list[OfferPosition]) -> None:
        """Ergebnisse in die Positionen zurueckschreiben."""
        for position in selected:
            result = run.results.get(position.uid)
            if result is None or not result.actions:
                position.status = PositionStatus.SKIPPED
                if not position.result_text:
                    position.result_text = "Nichts zu tun – unverändert."
                continue

            if any(a.state is ResultState.FAILED for a in result.actions):
                position.status = PositionStatus.ERROR
            elif all(a.state is ResultState.SKIPPED for a in result.actions):
                position.status = PositionStatus.SKIPPED
            else:
                position.status = PositionStatus.DONE
            position.result_text = "\n".join(a.display() for a in result.actions)

    # ------------------------------------------------------------------
    def _emit(self, run: _Run, position: OfferPosition | None, phase: str,
              message: str, result: ActionResult | None, advance: bool = True) -> None:
        if run.progress is None:
            return
        event = ProgressEvent(
            index=min(run.index, run.total) if advance else run.index,
            total=run.total,
            position_uid=position.uid if position is not None else 0,
            label=position.display_name if position is not None else "",
            phase=phase,
            message=message,
            result=result,
        )
        try:
            run.progress(event)
        except Exception:  # noqa: BLE001 - eine defekte Anzeige darf den Lauf nicht killen
            logger.exception("Fortschrittsmeldung konnte nicht zugestellt werden.")
