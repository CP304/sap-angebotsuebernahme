"""Schutz gegen ungewollten Nachrichtenversand.

HINTERGRUND
===========
Beim Sichern eines Kontrakts oder einer Bestellung fuehrt SAP die
Nachrichtenfindung aus.  Je nach Customizing entsteht dabei ein Nachrichten-
satz (NAST), der die Bestellung sofort oder per Sammellauf (RSNAST00) an den
Lieferanten sendet -- per EDI, Fax oder E-Mail.

Fuer dieses Werkzeug gilt: **Es geht nichts an den Lieferanten hinaus.**
Die Belege werden angelegt, die Kommunikation macht weiterhin der Mensch.

VORGEHEN
========
Vor dem Sichern:

  1. Nachrichtenbild oeffnen (Kopf -> Nachrichten)
  2. Alle gefundenen Nachrichtensaetze loeschen
  3. Nachweisen, dass die Tabelle leer ist
  4. Erst dann sichern

Kann Schritt 2 oder 3 nicht durchgefuehrt werden -- etwa weil die Feld-IDs
noch nicht geprueft sind -- wird der Beleg **nicht** gesichert, sondern der
Vorgang mit einer klaren Meldung abgebrochen (einstellbar ueber
``workflow.abort_if_messages_present``).

Zusaetzlich empfohlen (Sache des SAP-Teams, nicht dieses Werkzeugs):
Eigener Benutzer ohne Nachrichtenfindung bzw. Konditionssatz-Ausschluss in
NACE fuer die Belegarten, die dieses Werkzeug anlegt.
"""

from __future__ import annotations

import logging

from ..config.settings import Settings
from .connection import SapError, SapGuiConnection
from .selectors import SelectorRegistry

logger = logging.getLogger(__name__)


class MessageSuppressionError(SapError):
    """Nachrichten konnten nicht nachweislich entfernt werden."""


class MessageGuard:
    """Entfernt Nachrichtensaetze und weist die leere Tabelle nach."""

    def __init__(self, connection: SapGuiConnection, selectors: SelectorRegistry,
                 settings: Settings) -> None:
        self.connection = connection
        self.selectors = selectors
        self.settings = settings

    # ------------------------------------------------------------------
    def enforce(self, context_label: str) -> list[str]:
        """Nachrichten unterdruecken.  Gibt Protokollzeilen zurueck.

        Wirft ``MessageSuppressionError``, wenn nicht sichergestellt werden
        kann, dass nichts hinausgeht.
        """
        workflow = self.settings.workflow
        log: list[str] = []

        if not workflow.suppress_output_messages:
            log.append("Nachrichtenunterdrueckung ist in den Einstellungen abgeschaltet.")
            logger.warning("Nachrichtenunterdrueckung abgeschaltet (%s)", context_label)
            return log

        try:
            opened = self._open_message_screen()
        except SapError as exc:
            return self._fail(f"Das Nachrichtenbild konnte nicht geoeffnet werden: "
                              f"{exc.message}", log)

        if not opened:
            return self._fail(
                "Das Nachrichtenbild wurde nicht gefunden. Es kann nicht sichergestellt "
                "werden, dass keine Nachricht an den Lieferanten hinausgeht.", log)

        removed = self._delete_all_rows(log)
        remaining = self._count_rows()

        if remaining > 0:
            return self._fail(
                f"Es sind weiterhin {remaining} Nachrichtensatz/-saetze vorhanden.", log)

        log.append(f"Nachrichten geprueft: {removed} Satz/Saetze entfernt, Tabelle leer.")
        logger.info("Nachrichtenunterdrueckung erfolgreich (%s): %d entfernt",
                    context_label, removed)
        self._leave_message_screen()
        return log

    # ------------------------------------------------------------------
    def _fail(self, reason: str, log: list[str]) -> list[str]:
        log.append(reason)
        if self.settings.workflow.abort_if_messages_present:
            raise MessageSuppressionError(
                "Sicherheitsabbruch: Der Beleg wurde NICHT gesichert, weil der "
                "Nachrichtenversand nicht ausgeschlossen werden konnte.",
                detail=reason,
            )
        logger.warning("Nachrichtenpruefung fehlgeschlagen, wird aber ignoriert: %s", reason)
        return log

    def _open_message_screen(self) -> bool:
        """Zum Nachrichtenbild navigieren.

        TODO: kundenspezifische SAP-GUI-ID einsetzen.  Der Weg unterscheidet
        sich zwischen den klassischen Transaktionen (Menue Kopf -> Nachrichten)
        und den Enjoy-Transaktionen (Registerkarte im Kopfbereich).
        """
        connection = self.connection
        registry = self.selectors

        table_key = ("messages", "message_table")
        if registry.has(*table_key):
            table_id = registry.id_for(*table_key)
            if connection.exists(table_id):
                return True

        if registry.has("messages", "messages_menu"):
            menu_id = registry.id_for("messages", "messages_menu")
            if connection.exists(menu_id):
                connection.select_element(menu_id)
                if registry.has(*table_key) and connection.exists(registry.id_for(*table_key)):
                    return True

        return registry.has(*table_key) and connection.exists(registry.id_for(*table_key))

    def _count_rows(self) -> int:
        """Belegte Nachrichtenzeilen zaehlen."""
        connection = self.connection
        registry = self.selectors
        if not registry.has("messages", "message_table"):
            return -1
        table_id = registry.id_for("messages", "message_table")
        if not connection.exists(table_id):
            return -1
        if not registry.has("messages", "message_kind_cell"):
            return -1

        visible = max(1, connection.table_visible_rows(table_id))
        count = 0
        for row in range(visible):
            cell_id = registry.id_for("messages", "message_kind_cell", row=row)
            if not connection.exists(cell_id):
                break
            if connection.read_text(cell_id).strip():
                count += 1
        return count

    def _delete_all_rows(self, log: list[str]) -> int:
        """Nachrichtenzeilen leeren.

        Zwei Wege, je nach Maske:
        a) Schaltflaeche "Zeile loeschen" (falls konfiguriert)
        b) Nachrichtenart in der Zelle leeren
        """
        connection = self.connection
        registry = self.selectors
        removed = 0

        if not registry.has("messages", "message_kind_cell"):
            return 0

        table_id = registry.id_for("messages", "message_table")
        visible = max(1, connection.table_visible_rows(table_id))
        delete_button = (registry.id_for("messages", "delete_row_button")
                         if registry.has("messages", "delete_row_button") else "")

        for _ in range(visible * 2):     # Sicherheitsnetz gegen Endlosschleifen
            cell_id = registry.id_for("messages", "message_kind_cell", row=0)
            if not connection.exists(cell_id):
                break
            value = connection.read_text(cell_id).strip()
            if not value:
                break
            try:
                if delete_button and connection.exists(delete_button):
                    connection.set_focus(cell_id)
                    connection.press_button(delete_button, expect_write=True)
                else:
                    connection.set_text(cell_id, "")
                    connection.send_vkey(0)
                removed += 1
            except SapError as exc:
                log.append(f"Nachrichtenzeile konnte nicht entfernt werden: {exc.message}")
                break
        if removed:
            log.append(f"{removed} Nachrichtensatz/-saetze entfernt.")
        return removed

    def _leave_message_screen(self) -> None:
        registry = self.selectors
        if not registry.has("messages", "back_from_messages"):
            return
        try:
            element_id = registry.id_for("messages", "back_from_messages")
            if self.connection.exists(element_id):
                self.connection.press_button(element_id)
        except SapError as exc:
            logger.debug("Ruecksprung aus dem Nachrichtenbild fehlgeschlagen: %s", exc)
