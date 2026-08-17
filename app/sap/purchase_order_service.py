"""Bestellung ueber ME21N/ME22N.

Der uebliche Weg dieses Werkzeugs ist der **Abruf aus dem Mengenkontrakt**:
Der Kontrakt wurde direkt zuvor angelegt, die Bestellung referenziert ihn
positionsweise (Feld Vereinbarung/Kontraktposition) und ruft eine Teilmenge
ab.  Dadurch ziehen Preis und Konditionen aus dem Kontrakt.

Hinweis zu ME21N
----------------
ME21N ist eine Enjoy-Transaktion.  Die Element-IDs haengen davon ab, welche
Bereiche (Kopf/Positionsuebersicht/Positionsdetail) auf- oder zugeklappt sind.
Diese IDs *muessen* am Zielsystem aufgezeichnet werden -- Vorschlaege sind in
``selectors.py`` hinterlegt, aber ungeprueft.

Vor dem Sichern laeuft zwingend der ``MessageGuard``.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from ..models.document_plan import PurchaseOrderPlan
from ..models.enums import ResultState
from ..models.results import ActionResult
from ..utils.parsing import format_date
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import PurchaseOrderServiceBase, WriteContext
from .message_guard import MessageGuard, MessageSuppressionError

logger = logging.getLogger(__name__)

_DOC_NUMBER = re.compile(r"\b(\d{10})\b")


class SapPurchaseOrderService(PurchaseOrderServiceBase):
    """Bestellanlage ueber SAP GUI Scripting."""

    write_operation = "purchase_order_write"

    def create(self, plan: PurchaseOrderPlan, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        connection = self.connection

        problem = self._validate(plan)
        if problem:
            return self._result("purchase_order", ResultState.FAILED, problem,
                                started_ms=started)

        transaction = (self.settings.transactions.purchase_order_change if plan.is_change
                       else self.settings.transactions.purchase_order_create)
        summary = plan.summary()
        operation = ("purchase_order_from_contract" if plan.reference_contract
                     else self.write_operation)

        if context.dry_run:
            return self._result("purchase_order", ResultState.SIMULATED,
                                f"Bestellung anlegen ({transaction}): {summary}",
                                transaction=transaction, new_value=summary,
                                started_ms=started)

        try:
            self.selectors.ensure_ready(operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result("purchase_order", ResultState.FAILED,
                                "Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft.",
                                detail=str(exc), started_ms=started)

        if connection is None:
            return self._result("purchase_order", ResultState.FAILED,
                                "Keine SAP-Verbindung.", started_ms=started)

        messages: list[str] = []
        try:
            connection.allow_write = True
            connection.ensure_transaction(transaction)
            connection.ensure_no_popup()

            self._fill_header(plan, messages)
            self._fill_items(plan, messages)

            # Beleg pruefen lassen, bevor gesichert wird
            self._check_document(messages)

            # SICHERHEIT: kein Nachrichtenversand
            guard = MessageGuard(connection, self.selectors, self.settings)
            messages.extend(guard.enforce(f"Bestellung {plan.vendor_number}"))

            status = connection.press_save()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)
            popup = connection.detect_popup()
            if popup is not None:
                # ME21N fragt beim Sichern gelegentlich nach ("Beleg sichern?").
                # Auch das wird NICHT automatisch bestaetigt.
                raise SapPopupError(
                    "Nach dem Sichern der Bestellung erschien ein Fenster. Bitte in SAP "
                    "pruefen, ob die Bestellung angelegt wurde.",
                    popup_text=popup.get("text", ""), title=popup.get("title", ""))

            if status.text:
                messages.append(status.display())
            number = self._extract_number(status.text) or plan.existing_order_number
            plan.document_number = number
            for index, item in enumerate(plan.items, start=1):
                if not item.item_number:
                    item.item_number = f"{index * 10:05d}"

            return self._result("purchase_order", ResultState.SUCCESS,
                                f"Bestellung angelegt ({summary})",
                                transaction=transaction, document_number=number,
                                new_value=summary, started_ms=started,
                                sap_messages=messages)
        except MessageSuppressionError as exc:
            plan.error = exc.message
            return self._result("purchase_order", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        except SapPopupError as exc:
            plan.error = exc.message
            return self._result("purchase_order", ResultState.FAILED,
                                f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}",
                                transaction=transaction, detail=exc.popup_text,
                                started_ms=started, sap_messages=messages)
        except SapBusinessError as exc:
            plan.error = exc.message
            return self._result("purchase_order", ResultState.FAILED, exc.message,
                                transaction=transaction,
                                detail=f"{exc.message_id} {exc.number}".strip(),
                                started_ms=started, sap_messages=messages)
        except SapError as exc:
            plan.error = exc.message
            return self._result("purchase_order", ResultState.FAILED, exc.message,
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
    def _fill_header(self, plan: PurchaseOrderPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors

        connection.set_text(registry.id_for("purchase_order", "vendor"),
                            plan.vendor_number, wait=True)

        if plan.document_type and registry.has("purchase_order", "document_type"):
            element_id = registry.id_for("purchase_order", "document_type")
            if connection.exists(element_id):
                try:
                    connection.set_combo(element_id, plan.document_type)
                except SapError:
                    connection.set_text(element_id, plan.document_type)

        if registry.has("purchase_order", "document_date"):
            element_id = registry.id_for("purchase_order", "document_date")
            if connection.exists(element_id):
                connection.set_text(element_id,
                                    format_date(plan.document_date or date.today()))
        connection.send_vkey(0)
        connection.raise_on_error_status("Bestellkopf")

        for key, value in (("org_purchasing_org", plan.purchasing_org),
                           ("org_purchasing_group", plan.purchasing_group)):
            if value and registry.has("purchase_order", key):
                element_id = registry.id_for("purchase_order", key)
                if connection.exists(element_id):
                    connection.set_text(element_id, value)
        connection.send_vkey(0)
        connection.raise_on_error_status("Organisationsdaten")
        messages.append(f"Bestellkopf gesetzt (Lieferant {plan.vendor_number})")

    def _fill_items(self, plan: PurchaseOrderPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors
        table_id = registry.id_for("purchase_order", "item_table")
        visible = max(1, connection.table_visible_rows(table_id))

        for index, item in enumerate(plan.items):
            top = (index // visible) * visible
            if top:
                connection.scroll_table(table_id, top)
            row = index - top

            def cell(screen: str, key: str, value: str) -> None:
                if not value or not registry.has(screen, key):
                    return
                element_id = registry.id_for(screen, key, row=row)
                if connection.exists(element_id):
                    connection.set_text(element_id, value)

            # Bei Kontraktbezug zuerst die Vereinbarung setzen: Material,
            # Preis und Konditionen zieht SAP dann aus dem Kontrakt.
            # Wichtig ist dabei die *Kontraktposition* -- ohne sie waere nicht
            # eindeutig, welche Zeile des Kontrakts abgerufen wird.
            contract_number = item.contract_number or plan.reference_contract
            if contract_number:
                cell("purchase_order_reference", "item_agreement_cell", contract_number)
                if item.contract_item:
                    cell("purchase_order_reference", "item_agreement_item_cell",
                         item.contract_item)
                else:
                    logger.warning(
                        "Bestellposition %s ohne Kontraktposition -- SAP muss die "
                        "Zeile selbst ermitteln.", item.material_number)
                connection.send_vkey(0)
                connection.raise_on_error_status("Kontraktbezug setzen")

            cell("purchase_order", "item_material_cell", item.material_number)
            cell("purchase_order", "item_quantity_cell", self._decimal_text(item.quantity))
            cell("purchase_order", "item_uom_cell", item.uom)
            cell("purchase_order", "item_plant_cell", item.plant or plan.plant)
            delivery = item.delivery_date or plan.delivery_date
            if delivery:
                cell("purchase_order", "item_delivery_date_cell", format_date(delivery))
            # Preis nur setzen, wenn KEIN Kontraktbezug besteht -- sonst wuerde
            # die Kontraktkondition ueberschrieben.
            if not plan.reference_contract:
                cell("purchase_order", "item_net_price_cell", self._decimal_text(item.net_price))
            item.item_number = item.item_number or f"{(index + 1) * 10:05d}"

        connection.send_vkey(0)
        connection.raise_on_error_status("Bestellpositionen erfassen")
        reference = (f" mit Bezug auf Kontrakt {plan.reference_contract}"
                     if plan.reference_contract else "")
        messages.append(f"{len(plan.items)} Bestellposition(en) erfasst{reference}")

    def _check_document(self, messages: list[str]) -> None:
        """SAP-eigene Belegpruefung ausloesen, bevor gesichert wird."""
        registry = self.selectors
        if not registry.has("purchase_order", "check_button"):
            return
        element_id = registry.id_for("purchase_order", "check_button")
        if not self.connection.exists(element_id):
            return
        try:
            self.connection.press_button(element_id)
            status = self.connection.read_status()
            if status.text:
                messages.append(f"Belegpruefung: {status.display()}")
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)
        except SapPopupError as exc:
            # Die Pruefung meldet Fehler haeufig in einem eigenen Fenster
            raise SapBusinessError(
                f"Die SAP-Belegpruefung meldet ein Problem: {exc.popup_text[:300]}"
            ) from exc

    # ------------------------------------------------------------------
    def _validate(self, plan: PurchaseOrderPlan) -> str:
        if not plan.vendor_number:
            return "Kein SAP-Lieferant zugeordnet -- Bestellung kann nicht angelegt werden."
        if not plan.items:
            return "Die Bestellung enthaelt keine Positionen."
        if not plan.purchasing_org:
            return "Keine Einkaufsorganisation angegeben."
        for item in plan.items:
            if not item.material_number:
                return (f"Position ohne Materialnummer ({item.description[:40]}) -- "
                        f"Bestellposition kann nicht angelegt werden.")
            if item.quantity is None or item.quantity <= 0:
                return f"Position {item.material_number}: keine gueltige Bestellmenge."
            if not (item.plant or plan.plant):
                return f"Position {item.material_number}: kein Werk angegeben."
            if not plan.reference_contract and item.net_price is None:
                return (f"Position {item.material_number}: kein Preis erkannt "
                        f"(ohne Kontraktbezug ist der Preis zwingend).")
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
