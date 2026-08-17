"""Plausibilitaets- und Vollstaendigkeitspruefung vor dem SAP-Zugriff.

Die Validierung entscheidet, ob eine Position ueberhaupt geschrieben werden
darf.  Sie ist damit die letzte Sicherung vor SAP und bewusst streng:

* Jeder Befund hat einen maschinenlesbaren ``code``, einen verstaendlichen
  deutschen Text und ein betroffenes Feld -- damit die Oberflaeche genau die
  Zelle markieren kann, um die es geht.
* ``blocking=True`` verhindert das Schreiben.  Warnungen kann der Anwender
  bewusst quittieren (``acknowledge``), Fehler in den Stammdaten nicht.
* Unsicher erkannte Angebotsdaten blockieren, sobald sie fuer die gewaehlte
  Aktion zwingend gebraucht werden.  Damit ist die Hausregel umgesetzt:
  **niemals aufgrund unsicher erkannter Angebotsdaten automatisch schreiben.**

Zusatzinformationen aus SAP
---------------------------
Ob ein Material fuer den Einkauf gesperrt ist oder ein Lieferant eine
Sperre traegt, steht nicht im Angebot.  Diese Kennzeichen werden von der
aufrufenden Schicht (Batch/GUI) ueber :meth:`ValidationService.apply_sap_flags`
an der Position hinterlegt und hier ausgewertet.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from ..config.settings import Settings
from ..models.enums import InfoRecordAction, IssueSeverity, PositionStatus, SourceListAction
from ..models.issue import Issue, IssueList
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..utils.parsing import format_decimal
from .comparison_service import price_change_percent

logger = logging.getLogger(__name__)

__all__ = ["ValidationService", "MANDATORY_FIELDS"]

#: Attributnamen, unter denen SAP-Zusatzkennzeichen an der Position abgelegt
#: werden (siehe :meth:`ValidationService.apply_sap_flags`).
ATTR_MATERIAL_BLOCKED = "material_purchasing_blocked"
ATTR_VENDOR_BLOCKED = "vendor_blocked"
ATTR_VENDOR_CANDIDATES = "vendor_candidates"

#: Felder, die je Aktion zwingend belastbar sein muessen.
MANDATORY_FIELDS: dict[str, tuple[str, ...]] = {
    "info_record": ("material_number", "price", "price_unit", "currency"),
    "source_list": ("material_number",),
    "contract": ("material_number", "price", "quantity", "uom"),
    "purchase_order": ("material_number", "price", "quantity", "uom"),
}

#: Klartext der Feldnamen fuer Meldungen
FIELD_LABELS: dict[str, str] = {
    "position_number": "Positionsnummer",
    "material_number": "Materialnummer",
    "vendor_material_number": "Lieferantenmaterial",
    "description": "Bezeichnung",
    "quantity": "Menge",
    "uom": "Mengeneinheit",
    "price": "Preis",
    "price_unit": "Preiseinheit",
    "currency": "Währung",
    "min_order_qty": "Mindestmenge",
    "lead_time_days": "Lieferzeit",
    "valid_from": "Gültig ab",
    "remarks": "Bemerkung",
}


def _label(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name)


class ValidationService:
    """Prueft Angebot und Positionen und setzt Befunde sowie Status."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # SAP-Zusatzkennzeichen
    # ------------------------------------------------------------------
    @staticmethod
    def apply_sap_flags(position: OfferPosition, material: Any = None,
                        vendor: Any = None, candidates: Iterable[Any] | None = None) -> None:
        """Ergebnisse der SAP-Stammdatenpruefung an der Position hinterlegen.

        ``material`` ist ein ``MaterialInfo``, ``vendor`` ein ``VendorMatch``
        (oder ``None``, wenn der Lieferant nicht gefunden wurde),
        ``candidates`` sind Namenstreffer der Lieferantensuche.
        """
        if material is not None:
            position.material_exists = bool(material.exists)
            position.sap_material_description = getattr(material, "description", "") or ""
            setattr(position, ATTR_MATERIAL_BLOCKED,
                    bool(getattr(material, "purchasing_blocked", False)))
        if vendor is not None:
            position.vendor_exists = True
            setattr(position, ATTR_VENDOR_BLOCKED, bool(getattr(vendor, "blocked", False)))
        if candidates is not None:
            setattr(position, ATTR_VENDOR_CANDIDATES, list(candidates))

    # ------------------------------------------------------------------
    # Angebot
    # ------------------------------------------------------------------
    def validate_offer(self, offer: Offer) -> None:
        """Kopfdaten pruefen und anschliessend jede Position bewerten."""
        acknowledged = self._remember_acknowledged(offer.issues)
        offer.issues.items = []

        missing = offer.missing_header_fields
        if missing:
            names = ", ".join(_label(f) if f in FIELD_LABELS else _HEADER_LABELS.get(f, f)
                              for f in missing)
            offer.issues.add(Issue(
                code="offer_header_incomplete",
                message=f"Der Angebotskopf ist unvollständig: {names} fehlt/fehlen.",
                severity=IssueSeverity.WARNING,
                field="header",
                detail="Fehlende Kopfdaten erschweren die Nachvollziehbarkeit im Protokoll.",
            ))

        if offer.offer_date is not None:
            age = (date.today() - offer.offer_date).days
            limit = int(self.settings.thresholds.offer_age_warn_days)
            if limit > 0 and age > limit:
                offer.issues.add(Issue(
                    code="offer_too_old",
                    message=(f"Das Angebot ist {age} Tage alt (Grenzwert {limit} Tage). "
                             f"Bitte prüfen, ob die Preise noch gelten."),
                    severity=IssueSeverity.WARNING,
                    field="offer_date",
                ))

        if not offer.positions:
            offer.issues.add(Issue(
                code="offer_without_positions",
                message="Das Angebot enthält keine Positionen.",
                severity=IssueSeverity.ERROR,
                field="positions",
                blocking=True,
            ))

        self._restore_acknowledged(offer.issues, acknowledged)

        for position in offer.positions:
            self.validate_position(position, offer)

        logger.debug("Validierung abgeschlossen: %s", self.summary(offer))

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------
    def validate_position(self, position: OfferPosition, offer: Offer) -> None:
        """Alle Pruefungen einer Position durchfuehren und Status setzen."""
        acknowledged = self._remember_acknowledged(position.issues)
        position.issues.items = []

        self._check_material(position)
        self._check_vendor(position)
        self._check_price(position)
        self._check_price_change(position)
        self._check_sap_differences(position)
        self._check_organisation(position)
        self._check_validity(position, offer)
        self._check_documents(position)
        self._check_maintenance_hints(position)
        self._check_uncertain(position)
        self._check_duplicate(position, offer)
        self._check_nothing_to_do(position)

        self._restore_acknowledged(position.issues, acknowledged)
        position.status = self.status_for(position)

    # -- Material -------------------------------------------------------
    def _check_material(self, position: OfferPosition) -> None:
        if not position.material_number:
            position.issues.add(Issue(
                code="material_missing",
                message=("Der Position ist keine SAP-Materialnummer zugeordnet. "
                         "Ohne Material kann in SAP nichts gepflegt werden."),
                severity=IssueSeverity.ERROR,
                field="material_number",
                blocking=True,
            ))
            return

        if position.material_exists is False:
            position.issues.add(Issue(
                code="material_not_in_sap",
                message=(f"Material {position.material_number} ist im Materialstamm "
                         f"nicht vorhanden."),
                severity=IssueSeverity.ERROR,
                field="material_number",
                blocking=True,
                detail="Bitte die Materialnummer korrigieren oder das Material anlegen lassen.",
            ))

        if getattr(position, ATTR_MATERIAL_BLOCKED, False):
            position.issues.add(Issue(
                code="material_purchasing_blocked",
                message=(f"Material {position.material_number} ist für den Einkauf gesperrt."),
                severity=IssueSeverity.ERROR,
                field="material_number",
                blocking=True,
                detail="Sperrkennzeichen im Materialstamm (Sicht Einkauf).",
            ))

    # -- Lieferant ------------------------------------------------------
    def _check_vendor(self, position: OfferPosition) -> None:
        if not position.vendor_number:
            position.issues.add(Issue(
                code="vendor_unresolved",
                message=("Der Lieferant konnte keiner SAP-Kreditorennummer zugeordnet "
                         "werden. Bitte manuell auswählen."),
                severity=IssueSeverity.ERROR,
                field="vendor_number",
                blocking=True,
            ))
        else:
            if position.vendor_exists is False:
                position.issues.add(Issue(
                    code="vendor_not_in_sap",
                    message=f"Lieferant {position.vendor_number} ist in SAP nicht vorhanden.",
                    severity=IssueSeverity.ERROR,
                    field="vendor_number",
                    blocking=True,
                ))
            if getattr(position, ATTR_VENDOR_BLOCKED, False):
                position.issues.add(Issue(
                    code="vendor_blocked",
                    message=f"Lieferant {position.vendor_number} ist gesperrt.",
                    severity=IssueSeverity.ERROR,
                    field="vendor_number",
                    blocking=True,
                    detail="Einkaufssperre im Lieferantenstamm (XK03).",
                ))

        candidates = list(getattr(position, ATTR_VENDOR_CANDIDATES, []) or [])
        if len(candidates) > 1:
            threshold = Decimal(str(self.settings.thresholds.vendor_match_threshold))
            best = max((Decimal(str(getattr(c, "score", 0))) for c in candidates),
                       default=Decimal(0))
            if best < threshold:
                names = ", ".join(
                    f"{getattr(c, 'vendor_number', '')} {getattr(c, 'name', '')}".strip()
                    for c in candidates[:3])
                position.issues.add(Issue(
                    code="vendor_ambiguous",
                    message=("Die Lieferantenzuordnung ist nicht eindeutig. "
                             "Bitte den richtigen Lieferanten auswählen."),
                    severity=IssueSeverity.WARNING,
                    field="vendor_number",
                    detail=f"Mögliche Treffer: {names}",
                ))

    # -- Preis ----------------------------------------------------------
    def _check_price(self, position: OfferPosition) -> None:
        if position.price is None:
            position.issues.add(Issue(
                code="price_missing",
                message="Für die Position wurde kein Preis erkannt.",
                severity=IssueSeverity.ERROR,
                field="price",
                blocking=True,
            ))
            return

        if position.price <= 0:
            position.issues.add(Issue(
                code="price_zero_or_negative",
                message=(f"Der Preis {format_decimal(position.price)} ist nicht plausibel "
                         f"(0 oder negativ)."),
                severity=IssueSeverity.ERROR,
                field="price",
                blocking=True,
            ))
            return

        limit = Decimal(str(self.settings.thresholds.max_absolute_price))
        if limit > 0 and position.price > limit:
            position.issues.add(Issue(
                code="price_above_limit",
                message=(f"Der Preis {format_decimal(position.price)} überschreitet die "
                         f"Obergrenze von {format_decimal(limit)}. Bitte auf Tippfehler prüfen."),
                severity=IssueSeverity.ERROR,
                field="price",
                blocking=True,
            ))

    def _check_price_change(self, position: OfferPosition) -> None:
        percent = price_change_percent(position, self.settings)
        if percent is None:
            return
        thresholds = self.settings.thresholds
        warn = Decimal(str(thresholds.price_warn_percent))
        error = Decimal(str(thresholds.price_error_percent))
        change = abs(percent)
        direction = "Erhöhung" if percent > 0 else "Senkung"
        text = format_decimal(change, 2)

        if error > 0 and change > error:
            position.issues.add(Issue(
                code="price_change_high",
                message=(f"Sehr große Preisänderung: {direction} um {text} % "
                         f"(Grenzwert {format_decimal(error, 2)} %). "
                         f"Bitte ausdrücklich bestätigen."),
                severity=IssueSeverity.ERROR,
                field="price",
                blocking=True,
                detail="Der Schreibvorgang ist gesperrt, bis die Abweichung quittiert wurde.",
            ))
        elif warn > 0 and change > warn:
            position.issues.add(Issue(
                code="price_change_warn",
                message=(f"Deutliche Preisänderung: {direction} um {text} % "
                         f"(Grenzwert {format_decimal(warn, 2)} %)."),
                severity=IssueSeverity.WARNING,
                field="price",
            ))

    # -- Abweichungen gegenueber SAP ------------------------------------
    def _check_sap_differences(self, position: OfferPosition) -> None:
        record = position.sap_info_record
        if record is None or not record.exists:
            return

        default_unit = self.settings.purchasing.price_unit or 1
        offered_unit = position.price_unit or default_unit
        stored_unit = record.price_unit or 1
        if int(offered_unit) != int(stored_unit):
            position.issues.add(Issue(
                code="price_unit_changed",
                message=(f"Die Preiseinheit weicht vom Infosatz ab "
                         f"(SAP: {stored_unit}, Angebot: {offered_unit})."),
                severity=IssueSeverity.WARNING,
                field="price_unit",
            ))

        default_currency = self.settings.purchasing.currency
        offered_currency = (position.currency or default_currency).upper()
        stored_currency = (record.currency or default_currency).upper()
        if offered_currency != stored_currency:
            position.issues.add(Issue(
                code="currency_changed",
                message=(f"Die Währung weicht vom Infosatz ab "
                         f"(SAP: {stored_currency}, Angebot: {offered_currency})."),
                severity=IssueSeverity.WARNING,
                field="currency",
                detail="Ein Währungswechsel im Infosatz sollte bewusst entschieden werden.",
            ))

        from ..utils.parsing import normalize_uom

        offered_uom = normalize_uom(position.uom or self.settings.purchasing.order_unit)
        stored_uom = normalize_uom(record.order_unit or self.settings.purchasing.order_unit)
        if offered_uom != stored_uom:
            position.issues.add(Issue(
                code="uom_differs_from_sap",
                message=(f"Die Mengeneinheit weicht vom Infosatz ab "
                         f"(SAP: {stored_uom}, Angebot: {offered_uom})."),
                severity=IssueSeverity.WARNING,
                field="uom",
            ))

    # -- Organisation ---------------------------------------------------
    def _check_organisation(self, position: OfferPosition) -> None:
        needs_plant = position.do_source_list or position.do_contract or position.do_purchase_order
        if not position.plant:
            position.issues.add(Issue(
                code="plant_missing",
                message=("Es ist kein Werk angegeben. Orderbuch, Kontrakt und Bestellung "
                         "benötigen ein Werk."),
                severity=IssueSeverity.ERROR if needs_plant else IssueSeverity.WARNING,
                field="plant",
                blocking=needs_plant,
            ))
        if not position.purchasing_org:
            position.issues.add(Issue(
                code="purchasing_org_missing",
                message="Es ist keine Einkaufsorganisation angegeben.",
                severity=IssueSeverity.ERROR,
                field="purchasing_org",
                blocking=True,
            ))

    # -- Gueltigkeit ----------------------------------------------------
    def _check_validity(self, position: OfferPosition, offer: Offer) -> None:
        valid_from = position.valid_from or offer.valid_from
        if valid_from is None:
            position.issues.add(Issue(
                code="valid_from_missing",
                message=("Es wurde kein Gültig-ab-Datum erkannt. Es wird das heutige "
                         "Datum verwendet."),
                severity=IssueSeverity.WARNING,
                field="valid_from",
            ))
            return
        if valid_from < date.today():
            position.issues.add(Issue(
                code="valid_from_in_past",
                message=(f"Das Gültig-ab-Datum {valid_from.strftime('%d.%m.%Y')} liegt in "
                         f"der Vergangenheit."),
                severity=IssueSeverity.WARNING,
                field="valid_from",
                detail="Rückwirkende Konditionen können bereits gebuchte Belege betreffen.",
            ))

    # -- Belege ---------------------------------------------------------
    def _check_documents(self, position: OfferPosition) -> None:
        if position.do_contract:
            quantity = position.contract_quantity
            if quantity is None:
                quantity = position.quantity
            if quantity is None or quantity <= 0:
                position.issues.add(Issue(
                    code="contract_quantity_missing",
                    message=("Für den Mengenkontrakt fehlt die Zielmenge."),
                    severity=IssueSeverity.ERROR,
                    field="contract_quantity",
                    blocking=True,
                ))

        if position.do_purchase_order:
            quantity = position.order_quantity
            if quantity is None:
                quantity = position.quantity
            if quantity is None or quantity <= 0:
                position.issues.add(Issue(
                    code="order_quantity_missing",
                    message="Für die Bestellung fehlt die Bestellmenge.",
                    severity=IssueSeverity.ERROR,
                    field="order_quantity",
                    blocking=True,
                ))
            if position.delivery_date is None:
                days = self.settings.purchasing.default_delivery_days
                position.issues.add(Issue(
                    code="delivery_date_missing",
                    message=(f"Für die Bestellung wurde kein Lieferdatum erkannt. "
                             f"Es wird die Voreinstellung (+{days} Tage) verwendet."),
                    severity=IssueSeverity.WARNING,
                    field="delivery_date",
                ))

    # -- Pflegehinweise -------------------------------------------------
    def _check_maintenance_hints(self, position: OfferPosition) -> None:
        record = position.sap_info_record
        if position.do_source_list and not position.do_info_record:
            if record is not None and record.was_read and not record.exists:
                position.issues.add(Issue(
                    code="info_record_missing",
                    message=("Zu Material und Lieferant existiert kein Einkaufsinfosatz. "
                             "Es soll aber nur das Orderbuch gepflegt werden."),
                    severity=IssueSeverity.INFO,
                    field="do_info_record",
                    detail="Ohne Infosatz zieht die Bestellung keinen Preis aus dem Stammsatz.",
                ))

        source_list = position.sap_source_list
        if position.do_info_record and not position.do_source_list and position.vendor_number:
            if source_list is not None and source_list.was_read:
                if not source_list.entries_for_vendor(position.vendor_number):
                    position.issues.add(Issue(
                        code="source_list_missing",
                        message=("Der Lieferant ist für dieses Material nicht im Orderbuch "
                                 "hinterlegt."),
                        severity=IssueSeverity.INFO,
                        field="do_source_list",
                        detail="Ohne Orderbucheintrag wird der Lieferant von der Disposition "
                               "nicht vorgeschlagen.",
                    ))

    # -- Unsichere Felder -----------------------------------------------
    def required_fields(self, position: OfferPosition) -> set[str]:
        """Felder, die fuer die angehakten Aktionen zwingend belastbar sind."""
        required: set[str] = set()
        if position.do_info_record:
            required.update(MANDATORY_FIELDS["info_record"])
        if position.do_source_list:
            required.update(MANDATORY_FIELDS["source_list"])
        if position.do_contract:
            required.update(MANDATORY_FIELDS["contract"])
        if position.do_purchase_order:
            required.update(MANDATORY_FIELDS["purchase_order"])
        return required

    def _check_uncertain(self, position: OfferPosition) -> None:
        uncertain = position.uncertain_fields
        if not uncertain:
            return
        required = self.required_fields(position)
        critical = sorted(set(uncertain) & required)
        names = ", ".join(_label(f) for f in uncertain)
        if critical:
            critical_names = ", ".join(_label(f) for f in critical)
            position.issues.add(Issue(
                code="uncertain_fields",
                message=(f"Unsicher erkannte Felder: {names}. Davon zwingend erforderlich: "
                         f"{critical_names}. Bitte prüfen und bestätigen."),
                severity=IssueSeverity.WARNING,
                field=critical[0],
                blocking=True,
                detail="Es wird grundsätzlich nichts geschrieben, was nur unsicher erkannt wurde.",
            ))
        else:
            position.issues.add(Issue(
                code="uncertain_fields",
                message=f"Unsicher erkannte Felder: {names}.",
                severity=IssueSeverity.WARNING,
                field=uncertain[0],
            ))

    # -- Dubletten ------------------------------------------------------
    def _check_duplicate(self, position: OfferPosition, offer: Offer) -> None:
        if not position.material_number or not position.vendor_number:
            return
        same = [p for p in offer.positions
                if p.material_number == position.material_number
                and p.vendor_number == position.vendor_number]
        if len(same) > 1:
            numbers = ", ".join(p.position_number or str(p.uid) for p in same)
            position.issues.add(Issue(
                code="duplicate_position",
                message=(f"Material {position.material_number} kommt für diesen Lieferanten "
                         f"mehrfach im Angebot vor (Positionen {numbers})."),
                severity=IssueSeverity.WARNING,
                field="material_number",
                detail="Die zuletzt verarbeitete Position überschreibt die vorherige.",
            ))

    # -- Nichts zu tun --------------------------------------------------
    def _check_nothing_to_do(self, position: OfferPosition) -> None:
        if not position.selected:
            return
        if position.issues.has_blocking:
            return
        if position.has_changes:
            return
        if position.info_record_action is InfoRecordAction.NONE and \
                position.source_list_action is SourceListAction.NONE and \
                not position.sap_loaded:
            return
        position.issues.add(Issue(
            code="nothing_to_do",
            message="Der SAP-Stand entspricht bereits dem Angebot – unverändert.",
            severity=IssueSeverity.INFO,
            field="",
        ))

    # ------------------------------------------------------------------
    # Status / Quittierung / Zusammenfassung
    # ------------------------------------------------------------------
    def status_for(self, position: OfferPosition) -> PositionStatus:
        """Farbstatus der Position aus den Befunden ableiten."""
        if not position.selected:
            return PositionStatus.NOT_SELECTED
        if position.issues.has_blocking:
            return PositionStatus.ERROR
        open_issues = [i for i in position.issues
                       if not i.acknowledged and i.severity is not IssueSeverity.INFO]
        if open_issues:
            return PositionStatus.CHECK
        return PositionStatus.READY

    def acknowledge(self, position: OfferPosition, code: str) -> bool:
        """Einen Befund bewusst quittieren.  Gibt ``True`` bei Treffer zurueck."""
        hit = False
        for issue in position.issues:
            if issue.code == code:
                issue.acknowledged = True
                hit = True
        if hit:
            position.status = self.status_for(position)
            logger.info("Befund '%s' an Position %s quittiert.", code, position.uid)
        return hit

    def acknowledge_all(self, position: OfferPosition) -> int:
        """Alle offenen Befunde einer Position quittieren."""
        count = 0
        for issue in position.issues:
            if not issue.acknowledged:
                issue.acknowledged = True
                count += 1
        if count:
            position.status = self.status_for(position)
        return count

    def summary(self, offer: Offer) -> dict[str, int]:
        """Anzahl der offenen Befunde je Schweregrad plus Statuszaehler."""
        counts = {"info": 0, "warning": 0, "error": 0, "blocking": 0, "acknowledged": 0}
        status_counts = {"positions": len(offer.positions), "selected": 0, "ready": 0,
                         "check": 0, "error_positions": 0, "not_selected": 0}

        for issue in offer.issues:
            self._count_issue(issue, counts)

        for position in offer.positions:
            if position.selected:
                status_counts["selected"] += 1
            else:
                status_counts["not_selected"] += 1
            for issue in position.issues:
                self._count_issue(issue, counts)
            status = position.status
            if status is PositionStatus.READY:
                status_counts["ready"] += 1
            elif status is PositionStatus.CHECK:
                status_counts["check"] += 1
            elif status is PositionStatus.ERROR:
                status_counts["error_positions"] += 1

        counts.update(status_counts)
        return counts

    @staticmethod
    def _count_issue(issue: Issue, counts: dict[str, int]) -> None:
        if issue.acknowledged:
            counts["acknowledged"] += 1
            return
        counts[issue.severity.value] = counts.get(issue.severity.value, 0) + 1
        if issue.blocking:
            counts["blocking"] += 1

    # ------------------------------------------------------------------
    # Quittierungen ueber einen Pruflauf hinweg erhalten
    # ------------------------------------------------------------------
    @staticmethod
    def _remember_acknowledged(issues: IssueList) -> set[tuple[str, str]]:
        return {(i.code, i.field) for i in issues if i.acknowledged}

    @staticmethod
    def _restore_acknowledged(issues: IssueList, remembered: set[tuple[str, str]]) -> None:
        if not remembered:
            return
        for issue in issues:
            if (issue.code, issue.field) in remembered:
                issue.acknowledged = True


#: Klartext der Kopffelder fuer Meldungen
_HEADER_LABELS = {
    "vendor_name": "Lieferantenname",
    "vendor_number": "Lieferantennummer",
    "offer_number": "Angebotsnummer",
    "offer_date": "Angebotsdatum",
    "currency": "Währung",
}
