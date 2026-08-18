"""Fachlogik rund um die Lieferantenstammanlage/-aenderung.

Lieferantenanlage ist bewusst IMMER ein separater, expliziter Schritt --
anders als Infosatz/Orderbuch/Kontrakt wird sie NIE vom :class:`BatchProcessor`
automatisch mitgezogen.  Ein falsch angelegter Lieferant ist in SAP nur mit
erheblichem Aufwand wieder loszuwerden.
"""

from __future__ import annotations

import logging

from ..models.enums import ResultState
from ..models.offer import Offer
from ..models.results import ActionResult
from ..models.sap_vendor import VendorMasterPlan
from ..sap.gateway import SapGateway
from ..sap.interfaces import VendorMatch, WriteContext

logger = logging.getLogger(__name__)


class VendorMasterService:
    """Fassade fuer Pruefung, Entwurf und Anlage/Aenderung eines Lieferanten."""

    def __init__(self, gateway: SapGateway) -> None:
        self.gateway = gateway

    # ------------------------------------------------------------------
    # (a) Existiert der Lieferant?
    # ------------------------------------------------------------------
    def check_exists(self, vendor_number: str, purchasing_org: str = "") -> VendorMatch | None:
        """Liefert den Treffer, falls der Lieferant bereits existiert -- sonst ``None``."""
        if not vendor_number:
            return None
        return self.gateway.check_vendor(vendor_number, purchasing_org)

    # ------------------------------------------------------------------
    # (b) Entwurf aus dem Angebotskopf
    # ------------------------------------------------------------------
    def draft_from_offer(self, offer: Offer) -> VendorMasterPlan:
        """Neuanlage-Entwurf, wenn der Lieferant nicht existiert.

        Es wird ausdruecklich NUR der Name aus dem Angebot uebernommen -- alle
        weiteren Felder bleiben leer.  Eine Adresse zu erfinden (z. B. aus der
        E-Mail-Signatur zu raten) waere ein Datenqualitaetsrisiko, das der
        Anwender in der Vorschau selbst beheben muss.
        """
        plan = VendorMasterPlan(
            existing_vendor_number="",
            name=(offer.vendor_name or "").strip(),
            reference_offer=offer.offer_number,
        )
        if not plan.name:
            plan.error = "Kein Lieferantenname im Angebot erkannt -- bitte von Hand eintragen."
        return plan

    # ------------------------------------------------------------------
    # Vorbedingungen pruefen (auch fuer die GUI-Vorschau nutzbar)
    # ------------------------------------------------------------------
    @staticmethod
    def missing_fields(plan: VendorMasterPlan) -> list[str]:
        """Welche Pflichtfelder fehlen noch? Leer = Plan darf geschrieben werden."""
        fehlend: list[str] = []
        if not plan.name:
            fehlend.append("Name")
        if not plan.country:
            fehlend.append("Land")
        return fehlend

    # ------------------------------------------------------------------
    # (c) Anlegen/Aendern
    # ------------------------------------------------------------------
    def create_or_update(self, plan: VendorMasterPlan, context: WriteContext) -> ActionResult:
        """Plan validieren und an ``gateway.vendors.write`` durchreichen."""
        fehlend = self.missing_fields(plan)
        if fehlend:
            return ActionResult(
                action="vendor_master", state=ResultState.FAILED,
                message="Lieferant kann nicht gespeichert werden -- es fehlen: "
                        + ", ".join(fehlend) + ".",
            )
        return self.gateway.vendors.write(plan, context)
