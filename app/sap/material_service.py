"""Materialstammpruefung ueber MM03.

Bewusst schlank: Es wird nur geprueft, ob das Material existiert und wie es
heisst.  Ein voller Materialstammabzug waere teuer und ist fuer die
Angebotsuebernahme nicht noetig.

Die Ergebnisse werden vom Aufrufer zwischengespeichert (siehe
``SapGateway``-Cache), damit dasselbe Material bei 40 Positionen nicht
40-mal in SAP nachgeschlagen wird.
"""

from __future__ import annotations

import logging

from .connection import SapBusinessError, SapError
from .interfaces import MaterialInfo, MaterialServiceBase

logger = logging.getLogger(__name__)

_NOT_FOUND_HINTS = ("existiert nicht", "nicht vorhanden", "does not exist",
                    "kein material", "not found")


class SapMaterialService(MaterialServiceBase):
    """Materialpruefung ueber SAP GUI Scripting."""

    read_operation = "material_read"

    def check(self, material_number: str, plant: str = "") -> MaterialInfo:
        info = MaterialInfo(material_number=material_number, exists=False)
        connection = self.connection
        if connection is None:
            info.error = "Keine SAP-Verbindung."
            return info
        if not material_number:
            info.error = "Keine Materialnummer angegeben."
            return info

        try:
            self.selectors.ensure_ready(self.read_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            info.error = str(exc)
            return info

        registry = self.selectors
        try:
            connection.ensure_transaction(self.settings.transactions.material_display)
            connection.set_text(registry.id_for("material_display", "material"),
                                material_number, wait=True)
            connection.send_vkey(0)

            status = connection.read_status()
            if status.is_error:
                if any(hint in status.text.lower() for hint in _NOT_FOUND_HINTS):
                    info.exists = False
                    info.error = status.text
                    return info
                raise SapBusinessError(status.text, status.message_id, status.number)

            # MM03 oeffnet ein Sichtenauswahl-Popup -- das ist erwartet und wird
            # bewusst abgebrochen, weil wir nur die Existenz pruefen wollen.
            popup = connection.detect_popup()
            if popup is not None:
                logger.debug("Sichtenauswahl erkannt -- Material %s existiert.", material_number)
                info.exists = True
                connection.close_popup(accept=False)
                return info

            info.exists = True
            if registry.has("material_display", "description"):
                info.description = connection.read_text(
                    registry.id_for("material_display", "description"))
            if registry.has("material_display", "base_unit"):
                info.base_unit = connection.read_text(
                    registry.id_for("material_display", "base_unit"))
        except SapError as exc:
            info.error = exc.message
            logger.warning("Materialpruefung %s fehlgeschlagen: %s", material_number, exc.message)
        finally:
            try:
                connection.leave_transaction()
            except SapError:
                pass
        return info
