"""Mengenkontrakt ueber ME31K/ME32K.

Ein Kontrakt buendelt alle angehakten Positionen eines Lieferanten in *einem*
Beleg -- das ist der Grund, warum die Belegplanung (``models.document_plan``)
vor der Verarbeitung stattfindet: ein Transaktionsaufruf statt n Stueck.

Vor dem Sichern laeuft zwingend der ``MessageGuard``: es darf keine Nachricht
an den Lieferanten ausgeloest werden.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from ..models.document_plan import ContractPlan
from ..models.enums import ResultState
from ..models.results import ActionResult
from ..utils.parsing import format_date
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import ContractServiceBase, WriteContext
from .message_guard import MessageGuard, MessageSuppressionError

logger = logging.getLogger(__name__)

_DOC_NUMBER = re.compile(r"\b(\d{10})\b")


class SapContractService(ContractServiceBase):
    """Mengenkontraktanlage ueber SAP GUI Scripting."""

    write_operation = "contract_write"

    def create(self, plan: ContractPlan, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        connection = self.connection

        problem = self._validate(plan)
        if problem:
            return self._result("contract", ResultState.FAILED, problem, started_ms=started)

        transaction = (self.settings.transactions.contract_change if plan.is_change
                       else self.settings.transactions.contract_create)
        summary = plan.summary()

        if context.dry_run:
            return self._result("contract", ResultState.SIMULATED,
                                f"Mengenkontrakt anlegen ({transaction}): {summary}",
                                transaction=transaction, new_value=summary,
                                started_ms=started)

        try:
            self.selectors.ensure_ready(self.write_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result("contract", ResultState.FAILED,
                                "Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft.",
                                detail=str(exc), started_ms=started)

        if connection is None:
            return self._result("contract", ResultState.FAILED, "Keine SAP-Verbindung.",
                                started_ms=started)

        messages: list[str] = []
        try:
            connection.allow_write = True
            connection.ensure_transaction(transaction)
            self._fill_initial_screen(plan)
            connection.send_vkey(0)
            connection.raise_on_error_status("Einstieg Kontrakt")
            connection.ensure_no_popup()

            self._fill_header(plan, messages)
            connection.send_vkey(0)
            connection.raise_on_error_status("Kontraktkopf")

            self._fill_items(plan, messages)
            connection.send_vkey(0)
            connection.raise_on_error_status("Kontraktpositionen")

            # SICHERHEIT: kein Nachrichtenversand
            guard = MessageGuard(connection, self.selectors, self.settings)
            messages.extend(guard.enforce(f"Kontrakt {plan.vendor_number}"))

            status = connection.press_save()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)
            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError("Nach dem Sichern des Kontrakts erschien ein Fenster.",
                                    popup_text=popup.get("text", ""),
                                    title=popup.get("title", ""))

            if status.text:
                messages.append(status.display())
            number = self._extract_number(status.text) or plan.existing_contract_number
            plan.document_number = number
            for index, item in enumerate(plan.items, start=1):
                if not item.item_number:
                    item.item_number = f"{index * 10:05d}"

            return self._result("contract", ResultState.SUCCESS,
                                f"Mengenkontrakt angelegt ({summary})",
                                transaction=transaction, document_number=number,
                                new_value=summary, started_ms=started,
                                sap_messages=messages)
        except MessageSuppressionError as exc:
            plan.error = exc.message
            return self._result("contract", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        except SapPopupError as exc:
            plan.error = exc.message
            return self._result("contract", ResultState.FAILED,
                                f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}",
                                transaction=transaction, detail=exc.popup_text,
                                started_ms=started, sap_messages=messages)
        except SapBusinessError as exc:
            plan.error = exc.message
            return self._result("contract", ResultState.FAILED, exc.message,
                                transaction=transaction,
                                detail=f"{exc.message_id} {exc.number}".strip(),
                                started_ms=started, sap_messages=messages)
        except SapError as exc:
            plan.error = exc.message
            return self._result("contract", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        finally:
            if connection is not None:
                connection.allow_write = False
                try:
                    connection.leave_transaction()
                except SapError:
                    pass

    # ------------------------------------------------------------------
    def _fill_initial_screen(self, plan: ContractPlan) -> None:
        connection = self.connection
        registry = self.selectors

        def maybe(key: str, value: str) -> None:
            if not value or not registry.has("contract_initial", key):
                return
            element_id = registry.id_for("contract_initial", key)
            if connection.exists(element_id):
                connection.set_text(element_id, value)

        connection.set_text(registry.id_for("contract_initial", "vendor"),
                            plan.vendor_number, wait=True)
        maybe("document_type", plan.document_type)
        maybe("purchasing_org", plan.purchasing_org)
        maybe("purchasing_group", plan.purchasing_group)
        maybe("plant", plan.plant)
        maybe("agreement_date", format_date(date.today()))
        if plan.is_change:
            maybe("agreement_number", plan.existing_contract_number)

    def _fill_header(self, plan: ContractPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors

        def maybe(key: str, value: str) -> None:
            if not value or not registry.has("contract_header", key):
                return
            element_id = registry.id_for("contract_header", key)
            if connection.exists(element_id):
                connection.set_text(element_id, value)

        maybe("valid_from", format_date(plan.valid_from))
        maybe("valid_to", format_date(plan.valid_to))
        maybe("currency", plan.currency)
        maybe("payment_terms", plan.payment_terms)
        maybe("incoterm", plan.incoterm)
        maybe("incoterm_location", plan.incoterm_location)
        maybe("your_reference", plan.reference_offer)

        target = plan.computed_target_value
        if target is not None:
            maybe("target_value", self._decimal_text(target))
        messages.append(f"Kopfdaten gesetzt (Laufzeit {format_date(plan.valid_from)} – "
                        f"{format_date(plan.valid_to)})")

    def _fill_items(self, plan: ContractPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors
        table_id = registry.id_for("contract_items", "table")
        visible = max(1, connection.table_visible_rows(table_id))

        for index, item in enumerate(plan.items):
            top = (index // visible) * visible
            if top:
                connection.scroll_table(table_id, top)
            row = index - top

            def cell(key: str, value: str) -> None:
                if not value or not registry.has("contract_items", key):
                    return
                element_id = registry.id_for("contract_items", key, row=row)
                if connection.exists(element_id):
                    connection.set_text(element_id, value)

            cell("material_cell", item.material_number)
            cell("target_qty_cell", self._decimal_text(item.quantity))
            cell("uom_cell", item.uom)
            cell("net_price_cell", self._decimal_text(item.net_price))
            cell("price_unit_cell", str(item.price_unit) if item.price_unit else "")
            cell("plant_cell", item.plant or plan.plant)
            if not item.material_number:
                cell("short_text_cell", item.description[:40])
            item.item_number = f"{(index + 1) * 10:05d}"

        connection.send_vkey(0)
        connection.raise_on_error_status("Kontraktpositionen erfassen")
        messages.append(f"{len(plan.items)} Kontraktposition(en) erfasst")

    # ------------------------------------------------------------------
    def _validate(self, plan: ContractPlan) -> str:
        if not plan.vendor_number:
            return "Kein SAP-Lieferant zugeordnet -- Kontrakt kann nicht angelegt werden."
        if not plan.items:
            return "Der Kontrakt enthaelt keine Positionen."
        if not plan.purchasing_org:
            return "Keine Einkaufsorganisation angegeben."
        if not plan.valid_from or not plan.valid_to:
            return "Kontraktlaufzeit ist unvollstaendig (gueltig von/bis)."
        if plan.valid_to < plan.valid_from:
            return "Kontraktlaufzeit ist ungueltig (Ende liegt vor dem Beginn)."
        for item in plan.items:
            if not item.material_number:
                return (f"Position ohne Materialnummer ({item.description[:40]}) -- "
                        f"Kontraktposition kann nicht angelegt werden.")
            if item.quantity is None or item.quantity <= 0:
                return f"Position {item.material_number}: keine gueltige Zielmenge."
            if item.net_price is None:
                return f"Position {item.material_number}: kein Preis erkannt."
        return ""

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str:
        if value is None:
            return ""
        return format(value.normalize(), "f").replace(".", ",")

    @staticmethod
    def _extract_number(text: str) -> str:
        match = _DOC_NUMBER.search(text or "")
        return match.group(1) if match else ""
