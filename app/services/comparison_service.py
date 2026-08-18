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
* Fremdwaehrung: Kommt das Angebot in einer anderen Waehrung als der Infosatz,
  wird fuer die **Prozentangabe** ueber den gepflegten Kurs umgerechnet -- und
  nur dafuer.  Nach SAP geht immer der Originalbetrag in der Originalwaehrung.
  Fehlt der Kurs oder ist die Umrechnung abgeschaltet, bleibt die Prozentangabe
  leer (``None``); es wird kein Vergleich erfunden.
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
from .currency_service import ConversionResult, CurrencyService

logger = logging.getLogger(__name__)

__all__ = [
    "ComparisonService",
    "comparison_currency",
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


def offer_currency(position: OfferPosition, settings: Settings) -> str:
    """Waehrung des Angebots (ersatzweise die Vorbelegung des Einkaufs)."""
    return (position.currency or settings.purchasing.currency or "").upper()


def comparison_currency(position: OfferPosition, settings: Settings) -> str:
    """Waehrung, in der verglichen wird.

    Das ist die Waehrung des vorhandenen Infosatzes -- denn gegen diesen Wert
    wird gemessen.  Gibt es keinen Infosatz, gilt die Hauswaehrung.
    """
    record = position.sap_info_record
    if record is not None and record.exists and record.currency:
        return record.currency.upper()
    return (settings.currency.company_currency or settings.purchasing.currency or "").upper()


def offer_price_in_comparison_currency(position: OfferPosition,
                                       settings: Settings) -> Decimal | None:
    """Angebotspreis je Einheit, umgerechnet in die Vergleichswaehrung.

    ``None``, wenn kein Preis vorliegt, die Umrechnung abgeschaltet ist oder
    kein Kurs hinterlegt wurde.  Der Wert dient ausschliesslich dem Vergleich
    und wird niemals in die Position zurueckgeschrieben.
    """
    new = normalized_offer_price(position, settings)
    if new is None:
        return None
    quelle = offer_currency(position, settings)
    ziel = comparison_currency(position, settings)
    if not quelle or not ziel or quelle == ziel:
        return new
    if not settings.currency.convert_for_comparison:
        return None
    return CurrencyService(settings).convert(new, quelle, ziel)


def price_change_percent(position: OfferPosition, settings: Settings) -> Decimal | None:
    """Preisaenderung in Prozent, preiseinheits- und waehrungsbereinigt.

    ``None``, wenn kein Vergleich moeglich ist: kein Infosatz, kein Preis, ein
    alter Preis von 0 -- oder eine Fremdwaehrung ohne Kurs.  Der Aufrufer muss
    "keine Aenderung" und "nicht vergleichbar" unterscheiden koennen, deshalb
    wird im Zweifel nichts geliefert statt einer 0.
    """
    old = normalized_sap_price(position.sap_info_record)
    new = offer_price_in_comparison_currency(position, settings)
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
        self.currency = CurrencyService(settings)

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
        if self.currency_changed(position, record):
            # Ein Waehrungswechsel ist IMMER eine Aenderung -- auch dann, wenn
            # der umgerechnete Betrag zufaellig gleich hoch ist.  Im Infosatz
            # stuende sonst weiter die alte Waehrung.
            return False
        if old is None or new is None or old != new:
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

    # -- Fremdwaehrung ---------------------------------------------------
    def is_foreign_currency(self, position: OfferPosition) -> bool:
        """Weicht die Angebotswaehrung von der Vergleichswaehrung ab?"""
        quelle = offer_currency(position, self.settings)
        ziel = comparison_currency(position, self.settings)
        return bool(quelle and ziel and quelle != ziel)

    def conversion_for(self, position: OfferPosition) -> ConversionResult | None:
        """Umrechnung des Angebotspreises fuer den Vergleich.

        ``None``, wenn gar keine Fremdwaehrung im Spiel ist.  Sonst immer ein
        ``ConversionResult`` -- auch dann, wenn nicht umgerechnet werden konnte;
        dessen ``note`` sagt in Klartext, woran es lag.
        """
        if not self.is_foreign_currency(position):
            return None
        return self.currency.conversion(
            position.price,
            offer_currency(position, self.settings),
            comparison_currency(position, self.settings),
        )

    def comparison_note(self, position: OfferPosition) -> str:
        """Kurzer Hinweis fuer die Tabelle (leer, wenn nichts zu sagen ist)."""
        conversion = self.conversion_for(position)
        if conversion is None:
            return ""
        if not conversion.ok:
            return conversion.note
        hinweis = conversion.note
        if self.currency.is_stale():
            alter = self.currency.rate_age_days()
            hinweis = f"{hinweis} – Kurs ist {alter} Tage alt".strip(" –")
        return hinweis

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

        conversion = self.conversion_for(position)
        if conversion is not None and conversion.ok and position.price is not None:
            new_price = f"{new_price} ({conversion.note})"

        price_unit = position.price_unit or self.settings.purchasing.price_unit or 1
        uom = position.uom or self.settings.purchasing.order_unit
        unit_text = f"{price_unit} {uom}".strip() or "-"

        aenderung = self.price_delta_text(position) or "-"
        if conversion is not None:
            if conversion.ok:
                if aenderung != "-":
                    aenderung = f"{aenderung} (umgerechnet)"
            else:
                # Kein Kurs oder Umrechnung abgeschaltet: es wird ausdruecklich
                # keine Prozentzahl erfunden.
                aenderung = conversion.note

        beschreibung = {
            "Alter Preis": old_price,
            "Neuer Preis": new_price,
            "Änderung": aenderung,
            "Preiseinheit": unit_text,
            "Gültig ab": format_date(self.effective_date(position)),
            "Orderbuch": self.source_list_text(position),
        }
        if conversion is not None:
            beschreibung["Währung"] = (
                f"Angebot in {conversion.currency}, Vergleich in "
                f"{conversion.target_currency} – nach SAP wird der "
                f"Originalbetrag in {conversion.currency} geschrieben.")

        # Zusatzkonditionen: Rabatt, Zu-/Abschlag, Fracht, Skonto.  Nur
        # anzeigen, wenn tatsaechlich welche erkannt wurden bzw. im
        # Bestandssatz stehen -- sonst nur eine unnoetige Zeile.
        angebot_konditionen = (position.condition_display(self.settings.conditions)
                               if position.has_conditions else "")
        sap_konditionen = (record.conditions_display(self.settings.conditions.gross_price)
                          if record is not None and record.exists else "")
        if angebot_konditionen or sap_konditionen:
            zeile = angebot_konditionen or "keine erkannt"
            if not self.settings.conditions.write_additional_conditions and angebot_konditionen:
                zeile += "  ⚠ werden nicht geschrieben (Einstellungen)"
            beschreibung["Konditionen (Angebot)"] = zeile
            beschreibung["Konditionen (SAP heute)"] = sap_konditionen or "keine"
        return beschreibung

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
