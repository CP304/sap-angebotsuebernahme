"""Vergleich zwischen Angebot und SAP-Ist-Zustand.

Der Vergleich ist die fachliche Mitte der Anwendung: Er beantwortet fuer jede
Position die Frage "Was muss in SAP eigentlich passieren?" -- und zwar
*bevor* irgendetwas geschrieben wird.

Grundsaetze
-----------
* Es wird nie geraten.  Wurde der SAP-Ist-Zustand noch gar nicht gelesen,
  lautet die Antwort ``NONE`` ("keine Aussage moeglich") und nicht etwa
  "neu anlegen".
* Preise werden **preiseinheitsbereinigt** verglichen.  12,40 EUR je 1 ST und
  124,00 EUR je 10 ST sind derselbe Preis -- daraus darf keine Aenderung
  abgeleitet werden.
* Fehlt eine Vorbedingung (Material, Lieferant oder Preis), lautet die Antwort
  ``BLOCKED``.  Der Anwender sieht damit sofort, dass hier nicht SAP das
  Problem ist, sondern die Datenlage.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from ..config.settings import Settings
from ..models.enums import InfoRecordAction, SourceListAction
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..models.sap_info_record import SapInfoRecord
from ..models.sap_source_list import SourceListEntry
from ..utils.parsing import format_date, format_decimal, normalize_uom

logger = logging.getLogger(__name__)

__all__ = [
    "ComparisonService",
    "normalized_offer_price",
    "normalized_sap_price",
    "price_change_percent",
]


# ---------------------------------------------------------------------------
# Preisnormierung -- auch von der Validierung genutzt
# ---------------------------------------------------------------------------

def normalized_offer_price(position: OfferPosition, settings: Settings) -> Decimal | None:
    """Angebotspreis je *einer* Mengeneinheit.

    Anders als ``OfferPosition.normalized_new_price`` wird eine fehlende
    Preiseinheit hier ueber die Voreinstellung ergaenzt (in aller Regel 1),
    damit aus einer nicht erkannten Preiseinheit kein Scheinunterschied wird.
    """
    if position.price is None:
        return None
    unit = position.price_unit or settings.purchasing.price_unit or 1
    try:
        return position.price / Decimal(unit)
    except (ArithmeticError, ValueError):
        return None


def normalized_sap_price(record: SapInfoRecord | None) -> Decimal | None:
    """Infosatzpreis je *einer* Mengeneinheit."""
    if record is None or not record.exists or record.price is None:
        return None
    unit = record.price_unit or 1
    try:
        return record.price / Decimal(unit)
    except (ArithmeticError, ValueError):
        return None


def price_change_percent(position: OfferPosition, settings: Settings) -> Decimal | None:
    """Preisaenderung in Prozent, preiseinheitsbereinigt.

    ``None``, wenn kein Vergleich moeglich ist (kein Infosatz, kein Preis oder
    ein alter Preis von 0).
    """
    old = normalized_sap_price(position.sap_info_record)
    new = normalized_offer_price(position, settings)
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * Decimal(100)


# ---------------------------------------------------------------------------
# Dienst
# ---------------------------------------------------------------------------

class ComparisonService:
    """Leitet aus Angebot + SAP-Ist-Zustand die notwendigen Aktionen ab."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # Oeffentliche Schnittstelle
    # ------------------------------------------------------------------
    def compare_offer(self, offer: Offer) -> None:
        """Alle Positionen eines Angebots bewerten."""
        for position in offer.positions:
            if not position.currency and offer.currency:
                # Kopfwaehrung durchreichen, damit der Vergleich nicht an einer
                # nur im Kopf genannten Waehrung scheitert.
                position.currency = offer.currency
            if position.valid_from is None and offer.valid_from is not None:
                position.valid_from = offer.valid_from
            self.compare_position(position)
        logger.debug("Vergleich fuer %d Position(en) durchgefuehrt.", len(offer.positions))

    def compare_position(self, position: OfferPosition) -> None:
        """Infosatz- und Orderbuchaktion einer Position bestimmen."""
        position.info_record_action = self._info_record_action(position)
        position.source_list_action = self._source_list_action(position)

    # ------------------------------------------------------------------
    # Infosatz
    # ------------------------------------------------------------------
    def _info_record_action(self, position: OfferPosition) -> InfoRecordAction:
        record = position.sap_info_record

        # 1. Noch gar nichts gelesen -> keine Aussage moeglich.
        if record is None and not position.sap_loaded:
            return InfoRecordAction.NONE

        # 2. Vorbedingungen: ohne Material, Lieferant oder Preis geht nichts.
        if self.info_record_blocking_reasons(position):
            return InfoRecordAction.BLOCKED

        if record is None or not record.was_read:
            return InfoRecordAction.NONE

        # 3. Kein Infosatz vorhanden -> ME11.
        if not record.exists:
            return InfoRecordAction.CREATE

        # 4. Vorhanden -> auf inhaltliche Gleichheit pruefen.
        if self._info_record_is_unchanged(position, record):
            return InfoRecordAction.UNCHANGED
        return InfoRecordAction.UPDATE

    def info_record_blocking_reasons(self, position: OfferPosition) -> list[str]:
        """Welche Vorbedingung fuer einen Infosatz fehlt?"""
        reasons: list[str] = []
        if not position.material_number:
            reasons.append("keine Materialnummer")
        if not position.vendor_number:
            reasons.append("kein Lieferant zugeordnet")
        if position.price is None:
            reasons.append("kein Preis erkannt")
        return reasons

    def _info_record_is_unchanged(self, position: OfferPosition,
                                  record: SapInfoRecord) -> bool:
        """Entspricht der Infosatz bereits dem Angebot?

        Verglichen wird der *preiseinheitsbereinigte* Preis, die Waehrung, die
        Mengeneinheit der Preiseinheit sowie die Gueltigkeit.  Eine reine
        Umstellung der Preiseinheit (12,40 je 1 ST -> 124,00 je 10 ST) ist
        rechnerisch keine Preisaenderung und fuehrt deshalb nicht zu ME12.
        """
        old = normalized_sap_price(record)
        new = normalized_offer_price(position, self.settings)
        if old is None or new is None or old != new:
            return False
        if self.currency_changed(position, record):
            return False
        if self.uom_changed(position, record):
            return False
        if not record.valid_on(self.effective_date(position)):
            return False
        return True

    # -- Einzelvergleiche (auch von der Validierung genutzt) -------------
    def currency_changed(self, position: OfferPosition,
                         record: SapInfoRecord | None = None) -> bool:
        record = record or position.sap_info_record
        if record is None or not record.exists:
            return False
        default = self.settings.purchasing.currency
        offered = (position.currency or default).upper()
        stored = (record.currency or default).upper()
        return offered != stored

    def uom_changed(self, position: OfferPosition,
                    record: SapInfoRecord | None = None) -> bool:
        record = record or position.sap_info_record
        if record is None or not record.exists:
            return False
        default = self.settings.purchasing.order_unit
        offered = normalize_uom(position.uom or default)
        stored = normalize_uom(record.order_unit or default)
        return offered != stored

    def price_unit_changed(self, position: OfferPosition,
                           record: SapInfoRecord | None = None) -> bool:
        record = record or position.sap_info_record
        if record is None or not record.exists:
            return False
        default = self.settings.purchasing.price_unit or 1
        offered = position.price_unit or default
        stored = record.price_unit or 1
        return int(offered) != int(stored)

    def effective_date(self, position: OfferPosition) -> date:
        """Stichtag des Vergleichs: das gewuenschte Gueltig-ab, sonst heute."""
        return position.valid_from or date.today()

    # ------------------------------------------------------------------
    # Orderbuch
    # ------------------------------------------------------------------
    def _source_list_action(self, position: OfferPosition) -> SourceListAction:
        source_list = position.sap_source_list

        if source_list is None and not position.sap_loaded:
            return SourceListAction.NONE
        if self.source_list_blocking_reasons(position):
            return SourceListAction.BLOCKED
        if source_list is None or not source_list.was_read:
            return SourceListAction.NONE

        entry = self._entry_for_vendor(position)
        if entry is None:
            return SourceListAction.CREATE
        if self.source_list_update_reasons(position, entry):
            return SourceListAction.UPDATE
        return SourceListAction.UNCHANGED

    def source_list_blocking_reasons(self, position: OfferPosition) -> list[str]:
        reasons: list[str] = []
        if not position.material_number:
            reasons.append("keine Materialnummer")
        if not position.vendor_number:
            reasons.append("kein Lieferant zugeordnet")
        if not position.plant:
            reasons.append("kein Werk angegeben")
        return reasons

    def _entry_for_vendor(self, position: OfferPosition) -> SourceListEntry | None:
        source_list = position.sap_source_list
        if source_list is None:
            return None
        entries = source_list.entries_for_vendor(position.vendor_number)
        if not entries:
            return None
        # Bevorzugt den am Stichtag gueltigen Eintrag, sonst den ersten.
        day = self.effective_date(position)
        for entry in entries:
            if entry.valid_on(day):
                return entry
        return entries[0]

    def source_list_update_reasons(self, position: OfferPosition,
                                   entry: SourceListEntry | None = None) -> list[str]:
        """Warum muss ein vorhandener Orderbucheintrag angefasst werden?"""
        entry = entry if entry is not None else self._entry_for_vendor(position)
        if entry is None:
            return []
        workflow = self.settings.workflow
        reasons: list[str] = []

        if entry.blocked:
            reasons.append("Eintrag ist gesperrt")

        day = self.effective_date(position)
        target_valid_to = self.settings.parsed_source_list_valid_to()
        if not entry.valid_on(day):
            reasons.append("Gueltigkeit passt nicht zum gewuenschten Termin")
        elif entry.valid_to and target_valid_to and entry.valid_to < target_valid_to:
            reasons.append("Gueltigkeit laeuft aus")

        if (entry.mrp_indicator or "") != (workflow.source_list_mrp_indicator or ""):
            reasons.append("Dispokennzeichen weicht ab")

        if workflow.source_list_reference_contract and position.do_contract:
            if not entry.agreement:
                reasons.append("Kontrakt soll als Vereinbarung eingetragen werden")

        return reasons

    # ------------------------------------------------------------------
    # Darstellung
    # ------------------------------------------------------------------
    def price_delta_text(self, position: OfferPosition) -> str:
        """Preisaenderung als Text, z. B. ``+3,63 %`` oder ``–1,20 %``.

        Leerer String, wenn keine Aussage moeglich ist.
        """
        percent = price_change_percent(position, self.settings)
        if percent is None:
            return ""
        if percent > 0:
            sign = "+"
        elif percent < 0:
            sign = "–"          # Gedankenstrich, wie in der Oberflaeche
        else:
            sign = "±"
        return f"{sign}{format_decimal(abs(percent), 2)} %"

    def describe_change(self, position: OfferPosition) -> dict[str, str]:
        """Darstellung fuer die Detailansicht einer Position."""
        record = position.sap_info_record
        currency = position.currency or self.settings.purchasing.currency

        old_price = "-"
        if record is not None and record.exists and record.price is not None:
            old_price = f"{format_decimal(record.price)} {record.currency or currency}".strip()

        new_price = "-"
        if position.price is not None:
            new_price = f"{format_decimal(position.price)} {currency}".strip()

        price_unit = position.price_unit or self.settings.purchasing.price_unit or 1
        uom = position.uom or self.settings.purchasing.order_unit
        unit_text = f"{price_unit} {uom}".strip() or "-"

        return {
            "Alter Preis": old_price,
            "Neuer Preis": new_price,
            "Änderung": self.price_delta_text(position) or "-",
            "Preiseinheit": unit_text,
            "Gültig ab": format_date(self.effective_date(position)),
            "Orderbuch": self.source_list_text(position),
        }

    def info_record_text(self, position: OfferPosition) -> str:
        """Einzeiler zur geplanten Infosatzaktion."""
        action = position.info_record_action
        if action is InfoRecordAction.CREATE:
            return "kein Infosatz vorhanden – wird neu angelegt"
        if action is InfoRecordAction.UPDATE:
            delta = self.price_delta_text(position)
            return f"Infosatz wird geändert{f' ({delta})' if delta else ''}"
        if action is InfoRecordAction.UNCHANGED:
            return "Infosatz entspricht bereits dem Angebot – keine Änderung notwendig"
        if action is InfoRecordAction.BLOCKED:
            reasons = ", ".join(self.info_record_blocking_reasons(position))
            return f"nicht möglich: {reasons}" if reasons else "nicht möglich"
        return "SAP-Stand noch nicht gelesen"

    def source_list_text(self, position: OfferPosition) -> str:
        """Einzeiler zur geplanten Orderbuchaktion (Detailansicht)."""
        action = position.source_list_action
        if action is SourceListAction.CREATE:
            return "kein Eintrag für diesen Lieferanten – wird neu angelegt"
        if action is SourceListAction.UPDATE:
            reasons = self.source_list_update_reasons(position)
            detail = ", ".join(reasons)
            return f"Eintrag wird angepasst ({detail})" if detail else "Eintrag wird angepasst"
        if action is SourceListAction.UNCHANGED:
            return "Lieferant vorhanden – keine Änderung notwendig"
        if action is SourceListAction.BLOCKED:
            reasons = ", ".join(self.source_list_blocking_reasons(position))
            return f"nicht möglich: {reasons}" if reasons else "nicht möglich"
        return "Orderbuch noch nicht gelesen"
