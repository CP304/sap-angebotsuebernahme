"""Lieferantenpruefung ueber XK03.

Wichtig: Eine Namenssuche liefert hier ausschliesslich *Vorschlaege*.  Die
Zuordnung Lieferantenname -> SAP-Lieferantennummer trifft immer der Anwender
(oder ein bestaetigtes Mapping aus der lokalen Datenbank).  Automatisch
zugeordnet wird nie -- ein falsch zugeordneter Lieferant wuerde Preise am
falschen Stammsatz aendern.
"""

from __future__ import annotations

import logging

from .connection import SapBusinessError, SapError
from .interfaces import VendorMatch, VendorServiceBase

logger = logging.getLogger(__name__)

_NOT_FOUND_HINTS = ("existiert nicht", "nicht vorhanden", "does not exist",
                    "kein lieferant", "not found", "nicht angelegt")


class SapVendorService(VendorServiceBase):
    """Lieferantenpruefung ueber SAP GUI Scripting."""

    read_operation = "vendor_read"

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
