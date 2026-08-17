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
from ..utils.parsing import format_date, parse_date
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import ContractServiceBase, WriteContext
from .message_guard import MessageGuard, MessageSuppressionError

logger = logging.getLogger(__name__)

_DOC_NUMBER = re.compile(r"\b(\d{10})\b")


class SapContractService(ContractServiceBase):
    """Mengenkontraktanlage ueber SAP GUI Scripting."""

    write_operation = "contract_write"
    search_operation = "contract_search"

    # ==================================================================
    # Suche nach einem bestehenden Kontrakt (ME33K / Beleguebersicht)
    # ==================================================================
    def find_existing_contract(self, vendor_number: str, purchasing_org: str,
                               document_type: str, min_valid_to: date) -> str:
        """Laufenden Mengenkontrakt suchen; "" = keiner gefunden.

        Bewusst defensiv: Ist die Maske ``contract_search`` nicht geprueft,
        wird gar nicht gesucht.  Der Aufrufer legt dann wie bisher einen neuen
        Kontrakt an -- ein geratener Belegbezug waere weit schlimmer als eine
        zusaetzliche Belegnummer.

        Die Methode ist absichtlich *nicht* Teil von ``ContractServiceBase``:
        die Schnittstelle bleibt stabil, Aufrufer holen sie ueber
        ``getattr(service, "find_existing_contract", None)``.
        """
        connection = self.connection
        if connection is None:
            logger.info("Kontraktsuche uebersprungen: keine SAP-Verbindung.")
            return ""
        try:
            self.selectors.ensure_ready(self.search_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            logger.info("Kontraktsuche uebersprungen (Feld-IDs ungeprueft): %s", exc)
            return ""

        registry = self.selectors
        try:
            connection.ensure_transaction(self.settings.transactions.contract_display)
            connection.set_text(registry.id_for("contract_search", "vendor"),
                                vendor_number, wait=True)
            connection.set_text(registry.id_for("contract_search", "purchasing_org"),
                                purchasing_org)
            if document_type:
                connection.set_text(registry.id_for("contract_search", "document_type"),
                                    document_type)
            if registry.has("contract_search", "valid_to_from") and min_valid_to:
                element_id = registry.id_for("contract_search", "valid_to_from")
                if connection.exists(element_id):
                    connection.set_text(element_id, format_date(min_valid_to))

            connection.press_button(registry.id_for("contract_search", "execute_button"))
            status = connection.read_status()
            if status.is_error:
                logger.info("Kontraktsuche ohne Treffer: %s", status.text)
                return ""
            connection.ensure_no_popup()

            for row in range(self.settings.sap.max_table_rows):
                nummer_id = registry.id_for("contract_search", "result_number_cell", row=row)
                if not connection.exists(nummer_id):
                    break
                nummer = (connection.read_text(nummer_id) or "").strip()
                if not nummer:
                    break
                # Laufzeitende pruefen, sofern die Spalte lesbar ist
                if registry.has("contract_search", "result_valid_to_cell"):
                    ende_id = registry.id_for("contract_search", "result_valid_to_cell",
                                              row=row)
                    if connection.exists(ende_id):
                        ende = parse_date(connection.read_text(ende_id))
                        if ende is not None and min_valid_to and ende < min_valid_to:
                            continue
                logger.info("Bestehender Kontrakt gefunden: %s", nummer)
                return nummer
            return ""
        except SapError as exc:
            logger.warning("Kontraktsuche fehlgeschlagen: %s", exc.message)
            return ""
        finally:
            try:
                connection.leave_transaction()
            except SapError:
                pass

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

            if plan.is_change:
                # Bestandskontrakt: Kopfdaten (vor allem die Laufzeit) bleiben
                # unangetastet -- wir haengen nur Positionen an.
                messages.append(f"Bestehender Kontrakt {plan.existing_contract_number} "
                                f"wird erweitert, Kopfdaten bleiben unveraendert.")
            else:
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
            if not number:
                # Ohne Belegnummer ist voellig offen, ob und was gesichert
                # wurde.  Das darf nicht als Erfolg durchgehen.
                raise SapBusinessError(
                    "SAP hat nach dem Sichern keine Kontraktnummer gemeldet. Es ist "
                    f"unklar, ob der Beleg angelegt wurde -- bitte in "
                    f"{self.settings.transactions.contract_display} pruefen.")
            plan.document_number = number
            for index, item in enumerate(plan.items, start=1):
                if not item.item_number:
                    item.item_number = f"{index * 10:05d}"
            messages.extend(self._verify_document(number))

            return self._result("contract", ResultState.SUCCESS,
                                (f"Mengenkontrakt erweitert ({summary})" if plan.is_change
                                 else f"Mengenkontrakt angelegt ({summary})"),
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

    def _verify_document(self, number: str) -> list[str]:
        """Nach dem Sichern nachsehen, ob der Beleg wirklich existiert (ME33K)."""
        if not self.settings.sap.verify_after_write:
            return []
        connection = self.connection
        registry = self.selectors
        if not registry.has("contract_initial", "agreement_number"):
            return ["Ruecklese-Pruefung uebersprungen: Feld 'Kontraktnummer' ist nicht "
                    "konfiguriert."]
        try:
            connection.ensure_transaction(self.settings.transactions.contract_display)
            connection.set_text(registry.id_for("contract_initial", "agreement_number"),
                                number, wait=True)
            connection.send_vkey(0)
            status = connection.read_status()
            if status.is_error:
                meldung = (f"Ruecklese-Pruefung: Kontrakt {number} ist in "
                           f"{self.settings.transactions.contract_display} nicht "
                           f"aufrufbar ({status.text}).")
                if self.settings.sap.verify_failure_is_error:
                    raise SapBusinessError(meldung)
                return [meldung]
            return [f"Ruecklese-Pruefung: Kontrakt {number} existiert."]
        except SapBusinessError:
            raise
        except SapError as exc:
            return [f"Ruecklese-Pruefung nicht moeglich: {exc.message}"]

    def _existing_item_count(self) -> int:
        """Wie viele Positionen stehen im Bestandskontrakt schon drin?

        Nur so koennen neue Zeilen *angehaengt* werden, statt vorhandene zu
        ueberschreiben.
        """
        connection = self.connection
        table_id = self.selectors.id_for("contract_items", "table")
        if not connection.exists(table_id):
            return 0
        try:
            return max(0, min(connection.table_row_count(table_id),
                              self.settings.sap.max_table_rows))
        except SapError:
            return 0

    def _fill_items(self, plan: ContractPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors
        table_id = registry.id_for("contract_items", "table")
        visible = max(1, connection.table_visible_rows(table_id))

        # Beim Erweitern eines Bestandskontrakts wird hinter den vorhandenen
        # Zeilen weitergeschrieben -- niemals darueber.
        offset = self._existing_item_count() if plan.is_change else 0
        if offset:
            messages.append(f"{offset} vorhandene Kontraktposition(en) bleiben unberuehrt, "
                            f"neue Positionen werden angehaengt.")

        for position_index, item in enumerate(plan.items):
            index = position_index + offset
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
