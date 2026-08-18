"""Lieferantenpruefung ueber XK03.

Wichtig: Eine Namenssuche liefert hier ausschliesslich *Vorschlaege*.  Die
Zuordnung Lieferantenname -> SAP-Lieferantennummer trifft immer der Anwender
(oder ein bestaetigtes Mapping aus der lokalen Datenbank).  Automatisch
zugeordnet wird nie -- ein falsch zugeordneter Lieferant wuerde Preise am
falschen Stammsatz aendern.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..models.enums import ResultState
from ..models.results import ActionResult
from ..models.sap_vendor import SapVendorRecord, VendorMasterPlan
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import VendorMatch, VendorServiceBase, WriteContext

logger = logging.getLogger(__name__)

_NOT_FOUND_HINTS = ("existiert nicht", "nicht vorhanden", "does not exist",
                    "kein lieferant", "not found", "nicht angelegt")


def verify_vendor_master_write(record: SapVendorRecord, plan: VendorMasterPlan
                               ) -> tuple[bool, list[str]]:
    """Gelesenen Lieferantensatz gegen den Sollzustand vergleichen.

    Wie bei Infosatz/Orderbuch gilt: Erst der erneute Blick ueber die
    Anzeigetransaktion (XK03) ist ein Beweis, dass wirklich gesichert wurde.
    """
    if record.read_error:
        return False, [f"Ruecklese-Pruefung nicht moeglich: {record.read_error}"]
    if not record.exists:
        return False, ["Ruecklese-Pruefung fehlgeschlagen: SAP meldet 'gesichert', der "
                       "Lieferant ist aber nicht auffindbar -- bitte in XK03 pruefen."]

    abweichungen: list[str] = []
    if plan.name and record.name and plan.name.strip().upper() != record.name.strip().upper():
        abweichungen.append(f"Name '{record.name}' statt '{plan.name}'")
    if plan.city and record.city and plan.city.strip().upper() != record.city.strip().upper():
        abweichungen.append(f"Ort '{record.city}' statt '{plan.city}'")

    if abweichungen:
        return False, ["Ruecklese-Pruefung fehlgeschlagen: " + "; ".join(abweichungen)
                       + " -- bitte in XK03 pruefen."]
    return True, [f"Ruecklese-Pruefung: Lieferant {record.vendor_number} "
                  f"'{record.name}' bestaetigt"]


class SapVendorService(VendorServiceBase):
    """Lieferantenpruefung ueber SAP GUI Scripting."""

    read_operation = "vendor_read"
    write_operation = "vendor_master_write"

    def check(self, vendor_number: str, purchasing_org: str = "") -> VendorMatch | None:
        connection = self.connection
        if connection is None or not vendor_number:
            return None

        try:
            self.selectors.ensure_ready(self.read_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            logger.info("Lieferantenpruefung uebersprungen: %s", exc)
            return None

        registry = self.selectors
        try:
            connection.ensure_transaction(self.settings.transactions.vendor_display)
            connection.set_text(registry.id_for("vendor_display", "vendor"),
                                vendor_number, wait=True)
            if purchasing_org and registry.has("vendor_display", "purchasing_org"):
                element_id = registry.id_for("vendor_display", "purchasing_org")
                if connection.exists(element_id):
                    connection.set_text(element_id, purchasing_org)
            connection.send_vkey(0)

            status = connection.read_status()
            if status.is_error:
                if any(hint in status.text.lower() for hint in _NOT_FOUND_HINTS):
                    return None
                raise SapBusinessError(status.text, status.message_id, status.number)

            name = ""
            if registry.has("vendor_display", "name"):
                name = connection.read_text(registry.id_for("vendor_display", "name"))
            return VendorMatch(vendor_number=vendor_number, name=name, score=1.0)
        except SapError as exc:
            logger.warning("Lieferantenpruefung %s fehlgeschlagen: %s", vendor_number,
                           exc.message)
            return None
        finally:
            try:
                connection.leave_transaction()
            except SapError:
                pass

    def search_by_name(self, name: str, limit: int = 10) -> list[VendorMatch]:
        """Namenssuche in SAP.

        TODO: kundenspezifisch festlegen.  Ueber reines GUI-Scripting ist eine
        Namenssuche nur ueber die Wertehilfe (F4) oder eine Liste wie MKVZ
        moeglich; beides ist stark vom Customizing abhaengig und langsam.
        Der empfohlene Weg ist stattdessen das lokale Lieferanten-Mapping
        (GUI-Seite "Zuordnungen"), das der Anwender einmalig pflegt.
        """
        logger.info("Namenssuche in SAP ist nicht aktiviert -- lokales Mapping verwenden "
                    "(gesucht: %r)", name)
        return []

    # ==================================================================
    # Vollstaendig lesen (XK03, mehrere Sichten)
    # ==================================================================
    def read(self, vendor_number: str) -> SapVendorRecord:
        record = SapVendorRecord(vendor_number=vendor_number)
        connection = self.connection
        if connection is None:
            record.read_error = "Keine SAP-Verbindung."
            return record
        if not vendor_number:
            record.read_error = "Keine Lieferantennummer angegeben."
            return record

        try:
            self.selectors.ensure_ready(self.read_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            record.read_error = str(exc)
            record.read_at = datetime.now()
            return record

        registry = self.selectors
        try:
            connection.ensure_transaction(self.settings.transactions.vendor_display)
            connection.set_text(registry.id_for("vendor_display", "vendor"),
                                vendor_number, wait=True)
            if registry.has("vendor_display", "purchasing_org"):
                element_id = registry.id_for("vendor_display", "purchasing_org")
                if connection.exists(element_id):
                    connection.set_text(element_id, self.settings.purchasing.purchasing_org)
            if registry.has("vendor_display", "view_address"):
                element_id = registry.id_for("vendor_display", "view_address")
                if connection.exists(element_id):
                    connection.set_checkbox(element_id, True)
            connection.send_vkey(0)

            status = connection.read_status()
            if status.is_error:
                if any(hint in status.text.lower() for hint in _NOT_FOUND_HINTS):
                    record.exists = False
                    record.read_at = datetime.now()
                    return record
                raise SapBusinessError(status.text, status.message_id, status.number)

            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError("Beim Lesen des Lieferanten erschien ein Fenster.",
                                    popup_text=popup.get("text", ""),
                                    title=popup.get("title", ""))

            record.exists = True
            self._read_general_screen(record)
            self._read_purchasing_screen(record)
            record.read_at = datetime.now()
        except SapError as exc:
            record.read_error = exc.message
            record.read_at = datetime.now()
            logger.warning("Lieferant %s konnte nicht gelesen werden: %s",
                           vendor_number, exc.message)
        finally:
            try:
                connection.leave_transaction()
            except SapError:
                pass
        return record

    def _read_general_screen(self, record: SapVendorRecord) -> None:
        connection = self.connection
        registry = self.selectors

        def text(key: str) -> str:
            if not registry.has("vendor_master", key):
                return ""
            element_id = registry.id_for("vendor_master", key)
            if not connection.exists(element_id):
                return ""
            return connection.read_text(element_id)

        record.name = text("name")
        record.name2 = text("name2")
        record.street = text("street")
        record.postal_code = text("postal_code")
        record.city = text("city")
        record.country = text("country")
        record.region = text("region")
        record.language = text("language")
        record.search_term = text("search_term")
        record.telephone = text("telephone")
        record.email = text("email")
        record.tax_number = text("tax_number")
        record.vat_id = text("vat_id")
        # Fallback ueber die schlanke Anzeigemaske, falls die Vollmaske
        # (vendor_master) noch nicht geprueft ist.
        if not record.name and registry.has("vendor_display", "name"):
            element_id = registry.id_for("vendor_display", "name")
            if connection.exists(element_id):
                record.name = connection.read_text(element_id)

    def _read_purchasing_screen(self, record: SapVendorRecord) -> None:
        connection = self.connection
        registry = self.selectors

        def text(key: str) -> str:
            if not registry.has("vendor_master", key):
                return ""
            element_id = registry.id_for("vendor_master", key)
            if not connection.exists(element_id):
                return ""
            return connection.read_text(element_id)

        record.purchasing_org = self.settings.purchasing.purchasing_org
        record.currency = text("currency")
        record.payment_terms = text("payment_terms")
        record.incoterm = text("incoterm")
        record.incoterm_location = text("incoterm_location")
        record.purchasing_group = text("purchasing_group")
        if registry.has("vendor_master", "blocked"):
            element_id = registry.id_for("vendor_master", "blocked")
            if connection.exists(element_id):
                record.blocked = connection.read_text(element_id).strip() not in ("", "0")

    # ==================================================================
    # Schreiben -- ausschliesslich Aenderung (XK02)
    # ==================================================================
    def write(self, plan: VendorMasterPlan, context: WriteContext) -> ActionResult:
        """Bestehenden Lieferantenstammsatz pflegen.

        Eine Neuanlage findet hier bewusst NICHT statt (siehe ``_validate``).
        """
        started = self._now_ms()

        problem = self._validate(plan)
        if problem:
            return self._result("vendor_master", ResultState.FAILED, problem,
                                started_ms=started)

        transaction = self.settings.transactions.vendor_change
        existing = self.read(plan.existing_vendor_number)
        if existing.read_error:
            return self._result("vendor_master", ResultState.FAILED,
                                f"SAP-Ist-Zustand nicht lesbar: {existing.read_error}",
                                started_ms=started)
        if not existing.exists:
            return self._result(
                "vendor_master", ResultState.FAILED,
                f"Lieferant {plan.existing_vendor_number} ist in SAP nicht "
                "auffindbar -- Aenderung abgebrochen.", started_ms=started)
        # Bestehender Lieferant mit widersprechendem Namen wird NIE
        # ueberschrieben -- ein falscher Treffer wuerde den Stammsatz des
        # falschen Lieferanten veraendern.
        if plan.name and existing.name and \
                plan.name.strip().upper() != existing.name.strip().upper():
            return self._result(
                "vendor_master", ResultState.FAILED,
                f"Sicherheitsabbruch: Lieferant {plan.existing_vendor_number} heisst "
                f"in SAP '{existing.name}', der Plan nennt aber '{plan.name}'. "
                "Es wurde nichts geaendert.", started_ms=started)
        old_value = existing.name or existing.summary().get("Name", "")
        new_value = plan.name or "(kein Name)"

        if context.dry_run:
            return self._result(
                "vendor_master", ResultState.SIMULATED,
                f"aendern ({transaction})",
                transaction=transaction, old_value=old_value, new_value=new_value,
                started_ms=started,
            )

        try:
            self.selectors.ensure_ready(self.write_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result("vendor_master", ResultState.FAILED,
                                "Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft.",
                                detail=str(exc), started_ms=started)

        connection = self.connection
        if connection is None:
            return self._result("vendor_master", ResultState.FAILED,
                                "Keine SAP-Verbindung.", started_ms=started)

        messages: list[str] = []
        registry = self.selectors
        try:
            connection.allow_write = True
            connection.ensure_transaction(transaction)

            popup = connection.detect_popup()
            if popup is not None:
                # XK02 zeigt je nach Customizing ein Popup -- wird NIE
                # automatisch bestaetigt.
                raise SapPopupError(
                    "Beim Einstieg in XK02 erschien ein unerwartetes Fenster. "
                    "Bitte in SAP pruefen.",
                    popup_text=popup.get("text", ""), title=popup.get("title", ""))

            connection.set_text(registry.id_for("vendor_master", "vendor"),
                                plan.existing_vendor_number, wait=True)
            connection.set_text(registry.id_for("vendor_master", "purchasing_org"),
                                plan.purchasing_org or self.settings.purchasing.purchasing_org)
            connection.send_vkey(0)
            connection.raise_on_error_status("Einstieg Lieferantenstamm")

            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError(
                    "Nach dem Einstieg in XK02 erschien ein unerwartetes Fenster.",
                    popup_text=popup.get("text", ""), title=popup.get("title", ""))

            self._write_general_fields(plan, messages)
            self._write_purchasing_fields(plan, messages)

            status = connection.press_save()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)

            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError(
                    "Nach dem Sichern erschien ein unerwartetes Fenster. Bitte in SAP pruefen.",
                    popup_text=popup.get("text", ""), title=popup.get("title", ""))

            if status.text:
                messages.append(status.display())
            number = self._extract_number(status.text) or plan.existing_vendor_number
            plan.document_number = number

            if not number:
                return self._result(
                    "vendor_master", ResultState.FAILED,
                    "SAP hat keine Lieferantennummer gemeldet -- es ist unklar, ob der "
                    "Lieferant angelegt wurde.", transaction=transaction,
                    started_ms=started, sap_messages=messages)

            if self.settings.sap.verify_after_write:
                geprueft = self.read(number)
                in_ordnung, hinweise = verify_vendor_master_write(geprueft, plan)
                messages.extend(hinweise)
                if not in_ordnung:
                    if self.settings.sap.verify_failure_is_error:
                        return self._result(
                            "vendor_master", ResultState.FAILED, hinweise[0],
                            transaction=transaction, document_number=number,
                            old_value=old_value, new_value=new_value,
                            started_ms=started, sap_messages=messages)
                    messages.append("Hinweis: Es wurde gesichert, die Ruecklese-Pruefung "
                                    "ist aber nicht sauber -- bitte nachsehen.")

            return self._result(
                "vendor_master", ResultState.SUCCESS,
                "Lieferantenstammsatz geaendert",
                transaction=transaction, document_number=number, old_value=old_value,
                new_value=new_value, started_ms=started, sap_messages=messages,
            )
        except SapPopupError as exc:
            return self._result("vendor_master", ResultState.FAILED,
                                f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}",
                                transaction=transaction, detail=exc.popup_text,
                                started_ms=started, sap_messages=messages)
        except SapBusinessError as exc:
            return self._result("vendor_master", ResultState.FAILED, exc.message,
                                transaction=transaction,
                                detail=f"{exc.message_id} {exc.number}".strip(),
                                started_ms=started, sap_messages=messages)
        except SapError as exc:
            return self._result("vendor_master", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        finally:
            if connection is not None:
                connection.allow_write = False
                try:
                    connection.leave_transaction()
                except SapError:
                    pass

    def _write_general_fields(self, plan: VendorMasterPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors

        def maybe(key: str, value) -> None:
            if not value or not registry.has("vendor_master", key):
                return
            element_id = registry.id_for("vendor_master", key)
            if connection.exists(element_id):
                connection.set_text(element_id, str(value))

        maybe("name", plan.name)
        maybe("name2", plan.name2)
        maybe("search_term", plan.search_term)
        maybe("street", plan.street)
        maybe("postal_code", plan.postal_code)
        maybe("city", plan.city)
        maybe("country", plan.country)
        maybe("region", plan.region)
        maybe("language", plan.language)
        maybe("telephone", plan.telephone)
        maybe("email", plan.email)
        maybe("tax_number", plan.tax_number)
        maybe("vat_id", plan.vat_id)
        connection.send_vkey(0)
        connection.raise_on_error_status("Anschrift setzen")
        messages.append("Anschrift gesetzt")

    def _write_purchasing_fields(self, plan: VendorMasterPlan, messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors

        def maybe(key: str, value) -> None:
            if not value or not registry.has("vendor_master", key):
                return
            element_id = registry.id_for("vendor_master", key)
            if connection.exists(element_id):
                connection.set_text(element_id, str(value))

        maybe("currency", plan.currency)
        maybe("payment_terms", plan.payment_terms)
        maybe("incoterm", plan.incoterm)
        maybe("incoterm_location", plan.incoterm_location)
        maybe("purchasing_group", plan.purchasing_group)
        connection.send_vkey(0)
        connection.raise_on_error_status("Einkaufsorganisationsdaten setzen")
        messages.append("Einkaufsorganisationsdaten gesetzt")

    # -- Kleinkram -----------------------------------------------------
    def _validate(self, plan: VendorMasterPlan) -> str:
        # GRUNDSATZLICH KEINE NEUANLAGE.
        # Einen Lieferanten anzulegen ist ein kontrollierter Vorgang:
        # Dublettenpruefung, Compliance, Bankdaten, Kontengruppe, Freigabe.
        # Das gehoert nicht in ein Einkaufswerkzeug, das nebenbei Preise
        # pflegt.  Dieses Werkzeug aendert nur, was es schon gibt.
        if not plan.existing_vendor_number:
            return ("Neuanlage von Lieferanten ist in diesem Werkzeug nicht "
                    "vorgesehen. Bitte den Lieferanten zuerst im dafuer "
                    "vorgesehenen Prozess in SAP anlegen lassen und ihn danach "
                    "hier zuordnen.")
        if not plan.name:
            return "Kein Name angegeben -- Lieferant kann nicht geaendert werden."
        if not plan.country:
            return "Kein Land angegeben -- Lieferant kann nicht geaendert werden."
        return ""

    @staticmethod
    def _extract_number(text: str) -> str:
        import re
        match = re.search(r"\b(\d{5,10})\b", text or "")
        return match.group(1) if match else ""
