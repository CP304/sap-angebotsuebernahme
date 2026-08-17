"""Orderbuchpflege ueber ME01/ME03.

Besonderheit Table-Control
--------------------------
Die Zellen-IDs eines Table-Controls beziehen sich immer auf den *sichtbaren*
Bereich.  Zeile 12 eines Orderbuchs mit 8 sichtbaren Zeilen ist also nicht
``[...,12]``, sondern erfordert vorheriges Scrollen.  Das erledigt
``_iterate_rows()`` / ``_cell_id()``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..models.enums import ResultState
from ..models.offer_position import OfferPosition
from ..models.results import ActionResult
from ..models.sap_source_list import SapSourceList, SourceListEntry
from ..utils.parsing import format_date, parse_date
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import SourceListServiceBase, WriteContext

logger = logging.getLogger(__name__)

_NOT_FOUND_HINTS = ("kein orderbuch", "nicht vorhanden", "existiert nicht",
                    "no source list", "does not exist")


class SapSourceListService(SourceListServiceBase):
    """Echte Orderbuchpflege ueber SAP GUI Scripting."""

    read_operation = "source_list_read"
    write_operation = "source_list_write"

    # ==================================================================
    # Lesen
    # ==================================================================
    def read(self, material_number: str, plant: str) -> SapSourceList:
        source_list = SapSourceList(material_number=material_number, plant=plant)
        connection = self.connection
        if connection is None:
            source_list.read_error = "Keine SAP-Verbindung."
            return source_list

        try:
            self.selectors.ensure_ready(self.read_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            source_list.read_error = str(exc)
            source_list.read_at = datetime.now()
            return source_list

        try:
            connection.ensure_transaction(self.settings.transactions.source_list_display)
            self._fill_initial_screen(material_number, plant)
            connection.send_vkey(0)

            status = connection.read_status()
            if status.is_error:
                if any(hint in status.text.lower() for hint in _NOT_FOUND_HINTS):
                    source_list.exists = False
                    source_list.read_at = datetime.now()
                    return source_list
                raise SapBusinessError(status.text, status.message_id, status.number)

            connection.ensure_no_popup()
            source_list.entries = self._read_rows(plant)
            source_list.exists = bool(source_list.entries)
            source_list.read_at = datetime.now()
        except SapError as exc:
            source_list.read_error = exc.message
            source_list.read_at = datetime.now()
            logger.warning("Orderbuch %s/%s nicht lesbar: %s", material_number, plant,
                           exc.message)
        finally:
            self._leave_quietly()
        return source_list

    # ==================================================================
    # Schreiben
    # ==================================================================
    def write(self, position: OfferPosition, context: WriteContext,
              contract_number: str = "", contract_item: str = "") -> ActionResult:
        started = self._now_ms()
        connection = self.connection
        workflow = self.settings.workflow

        problem = self._validate(position)
        if problem:
            return self._result("source_list", ResultState.FAILED, problem, started_ms=started)

        current = position.sap_source_list
        if current is None or not current.was_read:
            current = self.read(position.material_number, position.plant)
            position.sap_source_list = current
        if current.read_error:
            return self._result("source_list", ResultState.FAILED,
                                f"SAP-Ist-Zustand nicht lesbar: {current.read_error}",
                                started_ms=started)

        existing = current.active_entry_for(position.vendor_number,
                                            context.valid_from or date.today()) \
            or next(iter(current.entries_for_vendor(position.vendor_number)), None)
        transaction = self.settings.transactions.source_list_maintain
        old_value = ("gesperrt" if existing and existing.blocked else
                     "aktiv" if existing else "kein Eintrag")
        valid_to = self.settings.parsed_source_list_valid_to()
        new_value = "aktiv"
        if contract_number and workflow.source_list_reference_contract:
            new_value += f", Kontrakt {contract_number}"
        if valid_to:
            new_value += f", gueltig bis {format_date(valid_to)}"

        if context.dry_run:
            return self._result(
                "source_list", ResultState.SIMULATED,
                f"{'aendern' if existing else 'neu anlegen'} ({transaction})",
                transaction=transaction, old_value=old_value, new_value=new_value,
                started_ms=started,
            )

        try:
            self.selectors.ensure_ready(self.write_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result("source_list", ResultState.FAILED,
                                "Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft.",
                                detail=str(exc), started_ms=started)

        messages: list[str] = []
        try:
            connection.allow_write = True
            connection.ensure_transaction(transaction)
            self._fill_initial_screen(position.material_number, position.plant)
            connection.send_vkey(0)
            connection.raise_on_error_status("Einstieg Orderbuch")
            connection.ensure_no_popup()

            rows = self._read_rows(position.plant)
            target = next((r for r in rows if r.vendor_number.lstrip("0") ==
                           position.vendor_number.lstrip("0")), None)
            row_index = target.row_index if target else self._find_free_row(rows)
            if row_index is None:
                raise SapBusinessError(
                    "Im Orderbuch ist keine freie Zeile verfuegbar. Bitte manuell pflegen."
                )

            self._write_row(row_index, position, context, contract_number, contract_item,
                            is_new=target is None)
            connection.send_vkey(0)
            connection.raise_on_error_status("Orderbuchzeile pruefen")

            status = connection.press_save()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)
            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError("Nach dem Sichern des Orderbuchs erschien ein Fenster.",
                                    popup_text=popup.get("text", ""),
                                    title=popup.get("title", ""))
            if status.text:
                messages.append(status.display())

            return self._result(
                "source_list", ResultState.SUCCESS,
                f"Orderbucheintrag {'aktualisiert' if target else 'angelegt'}, Lieferant aktiv",
                transaction=transaction, old_value=old_value, new_value=new_value,
                started_ms=started, sap_messages=messages,
            )
        except SapPopupError as exc:
            return self._result("source_list", ResultState.FAILED,
                                f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}",
                                transaction=transaction, detail=exc.popup_text,
                                started_ms=started, sap_messages=messages)
        except SapBusinessError as exc:
            return self._result("source_list", ResultState.FAILED, exc.message,
                                transaction=transaction,
                                detail=f"{exc.message_id} {exc.number}".strip(),
                                started_ms=started, sap_messages=messages)
        except SapError as exc:
            return self._result("source_list", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        finally:
            if connection is not None:
                connection.allow_write = False
                self._leave_quietly()

    # ==================================================================
    # Teilschritte
    # ==================================================================
    def _fill_initial_screen(self, material_number: str, plant: str) -> None:
        connection = self.connection
        registry = self.selectors
        connection.set_text(registry.id_for("source_list_initial", "material"),
                            material_number, wait=True)
        connection.set_text(registry.id_for("source_list_initial", "plant"), plant)

    def _cell_id(self, key: str, absolute_row: int, top_row: int) -> str:
        """Zellen-ID relativ zum sichtbaren Bereich."""
        return self.selectors.id_for("source_list_overview", key,
                                     row=absolute_row - top_row)

    def _read_rows(self, plant: str) -> list[SourceListEntry]:
        """Alle Orderbuchzeilen einlesen (inklusive Scrollen)."""
        connection = self.connection
        registry = self.selectors
        table_id = registry.id_for("source_list_overview", "table")
        if not connection.exists(table_id):
            return []

        total = min(connection.table_row_count(table_id), self.settings.sap.max_table_rows)
        visible = max(1, connection.table_visible_rows(table_id))
        entries: list[SourceListEntry] = []

        top = 0
        while top < max(total, 1):
            if top:
                connection.scroll_table(table_id, top)
            for offset in range(visible):
                absolute = top + offset
                if absolute >= total:
                    break
                vendor_id = self._cell_id("vendor_cell", absolute, top)
                if not connection.exists(vendor_id):
                    continue
                vendor = connection.read_text(vendor_id)
                if not vendor:
                    continue
                entry = SourceListEntry(vendor_number=vendor, plant=plant, row_index=absolute)
                if registry.has("source_list_overview", "valid_from_cell"):
                    entry.valid_from = parse_date(connection.read_text(
                        self._cell_id("valid_from_cell", absolute, top)))
                if registry.has("source_list_overview", "valid_to_cell"):
                    entry.valid_to = parse_date(connection.read_text(
                        self._cell_id("valid_to_cell", absolute, top)))
                if registry.has("source_list_overview", "fixed_cell"):
                    entry.fixed = connection.read_checkbox(
                        self._cell_id("fixed_cell", absolute, top))
                if registry.has("source_list_overview", "blocked_cell"):
                    entry.blocked = connection.read_checkbox(
                        self._cell_id("blocked_cell", absolute, top))
                if registry.has("source_list_overview", "mrp_cell"):
                    entry.mrp_indicator = connection.read_text(
                        self._cell_id("mrp_cell", absolute, top))
                if registry.has("source_list_overview", "agreement_cell"):
                    entry.agreement = connection.read_text(
                        self._cell_id("agreement_cell", absolute, top))
                if registry.has("source_list_overview", "purchasing_org_cell"):
                    entry.purchasing_org = connection.read_text(
                        self._cell_id("purchasing_org_cell", absolute, top))
                entries.append(entry)
            top += visible
            if total <= visible:
                break
        # Nach dem Lesen wieder an den Anfang scrollen
        if total > visible:
            connection.scroll_table(table_id, 0)
        return entries

    def _find_free_row(self, rows: list[SourceListEntry]) -> int | None:
        """Erste freie Zeile fuer einen neuen Eintrag ermitteln."""
        connection = self.connection
        table_id = self.selectors.id_for("source_list_overview", "table")
        used = {r.row_index for r in rows}
        visible = max(1, connection.table_visible_rows(table_id))
        total = max(connection.table_row_count(table_id), len(rows) + 1)

        for absolute in range(min(total + visible, self.settings.sap.max_table_rows)):
            if absolute in used:
                continue
            top = (absolute // visible) * visible
            if top:
                connection.scroll_table(table_id, top)
            vendor_id = self._cell_id("vendor_cell", absolute, top)
            if not connection.exists(vendor_id):
                continue
            if not connection.read_text(vendor_id):
                return absolute
        return None

    def _write_row(self, absolute_row: int, position: OfferPosition, context: WriteContext,
                   contract_number: str, contract_item: str, is_new: bool) -> None:
        connection = self.connection
        registry = self.selectors
        workflow = self.settings.workflow
        table_id = registry.id_for("source_list_overview", "table")

        visible = max(1, connection.table_visible_rows(table_id))
        top = (absolute_row // visible) * visible
        if top:
            connection.scroll_table(table_id, top)

        def set_cell(key: str, value: str) -> None:
            if not value or not registry.has("source_list_overview", key):
                return
            element_id = self._cell_id(key, absolute_row, top)
            if connection.exists(element_id):
                connection.set_text(element_id, value)

        def set_flag(key: str, value: bool) -> None:
            if not registry.has("source_list_overview", key):
                return
            element_id = self._cell_id(key, absolute_row, top)
            if connection.exists(element_id):
                connection.set_checkbox(element_id, value)

        valid_from = context.valid_from or date.today()
        valid_to = self.settings.parsed_source_list_valid_to()

        set_cell("vendor_cell", position.vendor_number)
        set_cell("valid_from_cell", format_date(valid_from))
        if valid_to:
            set_cell("valid_to_cell", format_date(valid_to))
        if position.purchasing_org:
            set_cell("purchasing_org_cell", position.purchasing_org)

        # "Lieferant aktiv setzen": Sperre raus, Dispokennzeichen rein
        if workflow.source_list_set_active:
            set_flag("blocked_cell", False)
            if workflow.source_list_mrp_indicator:
                set_cell("mrp_cell", workflow.source_list_mrp_indicator)
        if workflow.source_list_set_fixed:
            set_flag("fixed_cell", True)

        if contract_number and workflow.source_list_reference_contract:
            set_cell("agreement_cell", contract_number)
            if contract_item:
                set_cell("agreement_item_cell", contract_item)

        logger.debug("Orderbuchzeile %d %s geschrieben", absolute_row,
                     "neu" if is_new else "aktualisiert")

    # -- Kleinkram -----------------------------------------------------
    def _validate(self, position: OfferPosition) -> str:
        if not position.material_number:
            return "Keine Materialnummer -- Orderbuch kann nicht gepflegt werden."
        if not position.vendor_number:
            return "Kein SAP-Lieferant zugeordnet."
        if not position.plant:
            return "Kein Werk angegeben -- das Orderbuch ist werksabhaengig."
        return ""

    def _leave_quietly(self) -> None:
        try:
            if self.connection is not None:
                self.connection.leave_transaction()
        except SapError:
            pass
