"""Einkaufsinfosatz ueber ME11/ME12/ME13.

Ablauf beim Schreiben (bewusst konservativ):

    1. Einstieg in ME13 (Anzeige) -> existiert der Infosatz?
    2. Je nach Ergebnis ME11 (anlegen) oder ME12 (aendern)
    3. Schluesselfelder in der Maske gegen die erwarteten Werte pruefen
    4. Werte setzen
    5. Sichern und Statusleiste auswerten

Es wird nie blind Enter gedrueckt und nie geschrieben, wenn der Kontext nicht
zweifelsfrei stimmt.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal

from ..models.enums import ResultState
from ..models.offer_position import OfferPosition
from ..models.results import ActionResult
from ..models.sap_info_record import SapCondition, SapInfoRecord
from ..utils.parsing import format_date, parse_date, parse_decimal, parse_int
from .connection import (
    SapBusinessError,
    SapElementNotFoundError,
    SapError,
    SapPopupError,
)
from .interfaces import InfoRecordServiceBase, WriteContext

logger = logging.getLogger(__name__)

#: Meldungen, die "kein Infosatz vorhanden" bedeuten koennen.  Die Liste ist
#: bewusst offen gehalten -- entscheidend ist zusaetzlich, ob das Folgebild
#: erreicht wurde.
_NOT_FOUND_HINTS = (
    "nicht vorhanden", "existiert nicht", "does not exist", "not maintained",
    "kein infosatz", "no info record",
)

_DOC_NUMBER = re.compile(r"\b(\d{8,12})\b")


# ---------------------------------------------------------------------------
# Ruecklese-Pruefung
# ---------------------------------------------------------------------------
#
# Warum es sie gibt: Die Statusleiste meldet "Infosatz gesichert", auch wenn
# ein einzelnes Feld gar nicht uebernommen wurde -- weil es nicht eingabe-
# bereit war, weil SAP still gerundet hat oder weil auf Detailebene eine
# Berechtigung fehlte.  Erst der erneute Blick ueber die Anzeigetransaktion
# ist ein Beweis.  Echtbetrieb und Testsystem verwenden dieselbe Pruefung,
# damit sich beide identisch verhalten.

def _decimal_display(value: Decimal | None) -> str:
    from ..utils.parsing import format_decimal
    return format_decimal(value) if value is not None else "kein Wert"


def verify_info_record_write(record: SapInfoRecord, position: OfferPosition,
                             context, settings) -> tuple[bool, list[str]]:
    """Gelesenen Infosatz gegen den Sollzustand vergleichen.

    Liefert ``(in_ordnung, meldungen)``.  Die Meldungen sind Klartext fuer den
    Einkaeufer -- inklusive Hinweis, wo er in SAP nachsehen kann.
    """
    transaktion = settings.transactions.info_record_display
    if record.read_error:
        return False, [f"Ruecklese-Pruefung nicht moeglich: {record.read_error}"]
    if not record.exists:
        return False, [f"Ruecklese-Pruefung: SAP meldet 'gesichert', der Infosatz ist "
                       f"aber nicht auffindbar -- bitte in {transaktion} pruefen."]

    abweichungen: list[str] = []

    # -- Preis (mit Toleranz, weil SAP im Bild rundet) -------------------
    toleranz = Decimal(str(settings.sap.verify_price_tolerance or 0))
    erwartet = position.price
    if erwartet is not None:
        if record.price is None:
            abweichungen.append(f"SAP hat keinen Preis gespeichert, erwartet war "
                                f"{_decimal_display(erwartet)}")
        elif abs(record.price - erwartet) > toleranz:
            abweichungen.append(f"SAP hat {_decimal_display(record.price)} gespeichert, "
                                f"erwartet war {_decimal_display(erwartet)}")

    # -- Preiseinheit ----------------------------------------------------
    if position.price_unit and record.price_unit and \
            int(record.price_unit) != int(position.price_unit):
        abweichungen.append(f"Preiseinheit {record.price_unit} statt {position.price_unit}")

    # -- Waehrung --------------------------------------------------------
    if position.currency and record.currency and \
            record.currency.upper() != position.currency.upper():
        abweichungen.append(f"Waehrung {record.currency} statt {position.currency}")

    # -- Gueltig bis -----------------------------------------------------
    erwartetes_ende = getattr(context, "valid_to", None)
    if erwartetes_ende and record.valid_to and record.valid_to != erwartetes_ende:
        abweichungen.append(f"Gueltig bis {format_date(record.valid_to)} statt "
                            f"{format_date(erwartetes_ende)}")

    if abweichungen:
        return False, ["Ruecklese-Pruefung fehlgeschlagen: " + "; ".join(abweichungen)
                       + f" -- bitte in {transaktion} pruefen."]

    bestaetigt = (f"{_decimal_display(record.price)} "
                  f"{record.currency or position.currency}").strip()
    if record.price_unit:
        bestaetigt += f" / {record.price_unit}"
    meldungen = [f"Ruecklese-Pruefung: Preis {bestaetigt} bestaetigt"]
    if record.valid_to:
        meldungen.append(f"Ruecklese-Pruefung: gueltig bis {format_date(record.valid_to)}")
    return True, meldungen


def verify_source_list_write(source_list, position: OfferPosition,
                             settings) -> tuple[bool, list[str]]:
    """Orderbuch nach dem Sichern erneut lesen und pruefen.

    Entscheidend sind zwei Dinge, die in der Statusleiste nicht auftauchen:
    Ist der Lieferant wirklich *aktiv* (nicht gesperrt) und ist eine
    Gueltigkeit gesetzt?
    """
    transaktion = settings.transactions.source_list_display
    if getattr(source_list, "read_error", ""):
        return False, [f"Ruecklese-Pruefung nicht moeglich: {source_list.read_error}"]

    gesucht = (position.vendor_number or "").lstrip("0")
    treffer = [e for e in getattr(source_list, "entries", [])
               if (e.vendor_number or "").lstrip("0") == gesucht]
    # SAP erlaubt mehrere Zeilen je Lieferant (verschiedene Gueltigkeits-
    # zeitraeume).  Bewertet wird die heute gueltige; gibt es keine, die
    # erste.  Blind die erste zu nehmen waere falsch.
    heute = date.today()
    eintrag = next((e for e in treffer if e.valid_on(heute)), None)
    if eintrag is None:
        eintrag = next(iter(treffer), None)
    if eintrag is None:
        return False, [f"Ruecklese-Pruefung: Lieferant {position.vendor_number} steht "
                       f"nach dem Sichern nicht im Orderbuch -- bitte in "
                       f"{transaktion} pruefen."]

    abweichungen: list[str] = []
    if settings.workflow.source_list_set_active and eintrag.blocked:
        abweichungen.append("der Lieferant ist weiterhin gesperrt")
    if not eintrag.valid_to:
        abweichungen.append("es ist kein Gueltigkeitsende gesetzt")
    if abweichungen:
        return False, ["Ruecklese-Pruefung fehlgeschlagen: " + "; ".join(abweichungen)
                       + f" -- bitte in {transaktion} pruefen."]
    return True, [f"Ruecklese-Pruefung: Lieferant {position.vendor_number} aktiv, "
                  f"gueltig bis {format_date(eintrag.valid_to)}"]


def scale_levels_for(position: OfferPosition, settings) -> tuple[list[tuple[Decimal, Decimal]],
                                                                 list[str]]:
    """Zu schreibende Staffelstufen ermitteln (inklusive Kappung).

    Mehr Stufen als ``max_scale_levels`` werden abgeschnitten -- aber niemals
    stillschweigend: die Kappung steht als Meldung im Ergebnis.
    """
    stufen = position.sorted_scales()
    meldungen: list[str] = []
    grenze = max(1, int(settings.workflow.max_scale_levels or 1))
    if len(stufen) > grenze:
        entfallen = stufen[grenze:]
        meldungen.append(
            f"Staffel gekappt: {len(stufen)} Stufen erkannt, nur {grenze} geschrieben "
            f"(Einstellung 'max_scale_levels'). Nicht geschrieben: "
            + "; ".join(f"ab {_decimal_display(m)} -> {_decimal_display(p)}"
                        for m, p in entfallen))
        stufen = stufen[:grenze]
    return stufen, meldungen


#: Feld-IDs, ohne die eine Staffel nicht gepflegt werden kann
SCALE_SELECTOR_KEYS = ("scale_quantity_cell", "scale_amount_cell")


def unverified_scale_selectors(registry) -> list[str]:
    """Welche Staffel-Feld-IDs sind (noch) nicht geprueft?

    Leere Liste = die Staffel darf geschrieben werden.  Echtbetrieb und
    Testsystem verwenden dieselbe Pruefung.
    """
    offen: list[str] = []
    for key in SCALE_SELECTOR_KEYS:
        if not registry.has("info_record_conditions", key):
            offen.append(key)
            continue
        if not registry.get("info_record_conditions", key).verified:
            offen.append(key)
    return offen


def scale_not_written_message(offen: list[str], settings) -> str:
    """Einheitlicher Klartext, wenn die Staffel nicht geschrieben wurde."""
    return ("Staffel nicht geschrieben: Feld-IDs ungeprueft ("
            + ", ".join("info_record_conditions." + key for key in offen)
            + "). Es wurde nur der Grundpreis gepflegt -- die Staffel bitte in "
            + f"{settings.transactions.info_record_change} von Hand nachtragen.")


class SapInfoRecordService(InfoRecordServiceBase):
    """Echte Infosatzpflege ueber SAP GUI Scripting."""

    read_operation = "info_record_read"
    write_operation = "info_record_write"

    # ==================================================================
    # Lesen
    # ==================================================================
    def read(self, material_number: str, vendor_number: str, purchasing_org: str,
             plant: str) -> SapInfoRecord:
        record = SapInfoRecord(
            material_number=material_number, vendor_number=vendor_number,
            purchasing_org=purchasing_org, plant=plant,
        )
        connection = self.connection
        if connection is None:
            record.read_error = "Keine SAP-Verbindung."
            return record

        try:
            self.selectors.ensure_ready(self.read_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            record.read_error = str(exc)
            record.read_at = datetime.now()
            return record

        try:
            connection.ensure_transaction(self.settings.transactions.info_record_display)
            self._fill_initial_screen(material_number, vendor_number, purchasing_org, plant)
            connection.send_vkey(0)

            status = connection.read_status()
            if status.is_error:
                if any(hint in status.text.lower() for hint in _NOT_FOUND_HINTS):
                    record.exists = False
                    record.read_at = datetime.now()
                    logger.info("Kein Infosatz fuer %s/%s: %s", material_number,
                                vendor_number, status.text)
                    return record
                raise SapBusinessError(status.text, status.message_id, status.number)

            popup = connection.detect_popup()
            if popup is not None:
                raise SapPopupError("Beim Lesen des Infosatzes erschien ein Fenster.",
                                    popup_text=popup.get("text", ""),
                                    title=popup.get("title", ""))

            record.exists = True
            self._read_general_screen(record)
            self._read_purchasing_screen(record)
            if self.settings.sap.info_record_price_via_conditions:
                self._read_conditions(record)
            record.read_at = datetime.now()
        except SapError as exc:
            record.read_error = exc.message
            record.read_at = datetime.now()
            logger.warning("Infosatz %s/%s konnte nicht gelesen werden: %s",
                           material_number, vendor_number, exc.message)
        finally:
            self._leave_quietly()
        return record

    # ==================================================================
    # Schreiben
    # ==================================================================
    def write(self, position: OfferPosition, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        connection = self.connection

        problem = self._validate(position)
        if problem:
            return self._result("info_record", ResultState.FAILED, problem,
                                started_ms=started)

        existing = position.sap_info_record
        if existing is None or not existing.was_read:
            existing = self.read(position.material_number, position.vendor_number,
                                 position.purchasing_org, position.plant)
            position.sap_info_record = existing
        if existing.read_error:
            return self._result("info_record", ResultState.FAILED,
                                f"SAP-Ist-Zustand nicht lesbar: {existing.read_error}",
                                started_ms=started)

        transaction = (self.settings.transactions.info_record_change if existing.exists
                       else self.settings.transactions.info_record_create)
        old_value = existing.price_display() if existing.exists else "kein Infosatz"
        new_value = self._new_value_text(position, context)

        if context.dry_run:
            return self._result(
                "info_record", ResultState.SIMULATED,
                f"{'aendern' if existing.exists else 'neu anlegen'} ({transaction})",
                transaction=transaction, old_value=old_value, new_value=new_value,
                document_number=existing.info_record_number, started_ms=started,
            )

        try:
            self.selectors.ensure_ready(self.write_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result("info_record", ResultState.FAILED,
                                "Schreiben gesperrt: SAP-Feld-IDs sind nicht geprueft.",
                                detail=str(exc), started_ms=started)

        if connection is None:
            return self._result("info_record", ResultState.FAILED,
                                "Keine SAP-Verbindung.", started_ms=started)

        messages: list[str] = []
        try:
            connection.allow_write = True
            connection.ensure_transaction(transaction)
            self._fill_initial_screen(position.material_number, position.vendor_number,
                                      position.purchasing_org, position.plant)
            connection.send_vkey(0)
            connection.raise_on_error_status("Einstieg Infosatz")
            self._verify_context(position)

            # Allgemeine Daten -> EKorg-Daten
            self._advance_to_purchasing_screen(position)
            self._write_purchasing_fields(position, context, messages)

            if self.settings.sap.info_record_price_via_conditions:
                self._write_conditions(position, context, messages)

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
            number = self._extract_number(status.text) or existing.info_record_number
            position.created_info_record = number

            # -- Beweis statt Statusleiste ------------------------------
            if self.settings.sap.verify_after_write:
                geprueft = self.read(position.material_number, position.vendor_number,
                                     position.purchasing_org, position.plant)
                in_ordnung, hinweise = verify_info_record_write(
                    geprueft, position, context, self.settings)
                messages.extend(hinweise)
                if not in_ordnung:
                    if self.settings.sap.verify_failure_is_error:
                        return self._result(
                            "info_record", ResultState.FAILED, hinweise[0],
                            transaction=transaction, document_number=number,
                            old_value=old_value, new_value=new_value,
                            started_ms=started, sap_messages=messages)
                    messages.append("Hinweis: Es wurde gesichert, die Ruecklese-Pruefung "
                                    "ist aber nicht sauber -- bitte nachsehen.")

            return self._result(
                "info_record", ResultState.SUCCESS,
                f"Infosatz {'geaendert' if existing.exists else 'angelegt'}",
                transaction=transaction, document_number=number, old_value=old_value,
                new_value=new_value, started_ms=started, sap_messages=messages,
            )
        except SapPopupError as exc:
            return self._result("info_record", ResultState.FAILED,
                                f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}",
                                transaction=transaction, detail=exc.popup_text,
                                started_ms=started, sap_messages=messages)
        except SapBusinessError as exc:
            return self._result("info_record", ResultState.FAILED, exc.message,
                                transaction=transaction,
                                detail=f"{exc.message_id} {exc.number}".strip(),
                                started_ms=started, sap_messages=messages)
        except SapError as exc:
            return self._result("info_record", ResultState.FAILED, exc.message,
                                transaction=transaction, detail=exc.detail,
                                started_ms=started, sap_messages=messages)
        finally:
            if connection is not None:
                connection.allow_write = False
                self._leave_quietly()

    # ==================================================================
    # Teilschritte
    # ==================================================================
    def _fill_initial_screen(self, material: str, vendor: str, purchasing_org: str,
                             plant: str) -> None:
        connection = self.connection
        registry = self.selectors
        connection.set_text(registry.id_for("info_record_initial", "vendor"), vendor, wait=True)
        connection.set_text(registry.id_for("info_record_initial", "material"), material)
        connection.set_text(registry.id_for("info_record_initial", "purchasing_org"),
                            purchasing_org)
        if plant and registry.has("info_record_initial", "plant"):
            connection.set_text(registry.id_for("info_record_initial", "plant"), plant)
        # Infosatzart "Normal" -- nur, wenn das Feld konfiguriert ist
        if registry.has("info_record_initial", "category_standard"):
            element_id = registry.id_for("info_record_initial", "category_standard")
            if connection.exists(element_id):
                try:
                    connection.select_element(element_id)
                except SapError:
                    logger.debug("Infosatzart konnte nicht gesetzt werden (%s)", element_id)

    def _verify_context(self, position: OfferPosition) -> None:
        """Vor dem Schreiben pruefen, dass wirklich der richtige Satz offen ist.

        Wenn SAP z. B. wegen fehlender Berechtigung auf einem anderen Bild
        stehen bleibt, darf auf keinen Fall geschrieben werden.
        """
        if not self.settings.sap.verify_context_before_write:
            return
        connection = self.connection
        registry = self.selectors

        checks = (
            ("vendor", position.vendor_number, "Lieferant"),
            ("material", position.material_number, "Material"),
        )
        for key, expected, label in checks:
            if not registry.has("info_record_initial", key):
                continue
            element_id = registry.id_for("info_record_initial", key)
            if not connection.exists(element_id):
                continue                     # Bild bereits gewechselt -- in Ordnung
            actual = connection.read_text(element_id)
            if not actual:
                continue
            if actual.lstrip("0").upper() != str(expected).lstrip("0").upper():
                raise SapBusinessError(
                    f"Sicherheitsabbruch: In SAP steht {label} '{actual}', erwartet "
                    f"wurde '{expected}'. Es wurde nichts geaendert."
                )

    def _advance_to_purchasing_screen(self, position: OfferPosition) -> None:
        """Vom Einstieg bis zum Bild "Einkaufsorganisationsdaten 1".

        TODO: kundenspezifische SAP-GUI-ID / Bildfolge pruefen -- je nach
        Customizing liegt zwischen Einstieg und EKorg-Bild noch das Bild
        "Allgemeine Daten".  Es wird solange Enter gedrueckt, bis das
        Preisfeld sichtbar ist (maximal 3 Schritte, danach Abbruch).
        """
        connection = self.connection
        price_id = self.selectors.id_for("info_record_purchasing", "net_price")
        for step in range(3):
            if connection.exists(price_id):
                return
            connection.ensure_no_popup()
            connection.send_vkey(0)
            connection.raise_on_error_status("Navigation zum EKorg-Bild")
            logger.debug("Bildwechsel %d Richtung EKorg-Daten", step + 1)
        if not connection.exists(price_id):
            raise SapElementNotFoundError(
                "Das Bild 'Einkaufsorganisationsdaten' wurde nicht erreicht.",
                detail=f"Erwartetes Feld: {price_id}",
            )

    def _write_purchasing_fields(self, position: OfferPosition, context: WriteContext,
                                 messages: list[str]) -> None:
        connection = self.connection
        registry = self.selectors

        def maybe(key: str, value) -> None:
            if value in (None, "") or not registry.has("info_record_purchasing", key):
                return
            element_id = registry.id_for("info_record_purchasing", key)
            if connection.exists(element_id):
                connection.set_text(element_id, str(value))

        if not self.settings.sap.info_record_price_via_conditions:
            maybe("net_price", self._decimal_text(position.price))
        maybe("currency", position.currency)
        maybe("price_unit", position.price_unit)
        maybe("order_unit", position.uom)
        maybe("lead_time", position.lead_time_days)
        maybe("min_quantity", self._decimal_text(position.min_order_qty))
        maybe("purchasing_group", self.settings.purchasing.purchasing_group)
        if position.vendor_material_number and registry.has("info_record_general",
                                                            "vendor_material"):
            element_id = registry.id_for("info_record_general", "vendor_material")
            if connection.exists(element_id):
                connection.set_text(element_id, position.vendor_material_number)
        messages.append("EKorg-Daten gesetzt")

    def _write_conditions(self, position: OfferPosition, context: WriteContext,
                          messages: list[str]) -> None:
        """Preis mit Gueltigkeitszeitraum ueber das Konditionsbild pflegen.

        Nur so laesst sich das hauseigene "gueltig bis 31.12.2099" setzen.
        """
        connection = self.connection
        registry = self.selectors

        button_key = "conditions_button"
        if registry.has("info_record_purchasing", button_key):
            element_id = registry.id_for("info_record_purchasing", button_key)
            if connection.exists(element_id):
                connection.press_button(element_id)
            else:
                connection.send_vkey(0)
        else:
            connection.send_vkey(0)
        connection.raise_on_error_status("Wechsel ins Konditionsbild")

        valid_from = context.valid_from or date.today()
        valid_to = context.valid_to

        if registry.has("info_record_conditions", "valid_from"):
            element_id = registry.id_for("info_record_conditions", "valid_from")
            if connection.exists(element_id):
                connection.set_text(element_id, format_date(valid_from))
        if valid_to and registry.has("info_record_conditions", "valid_to"):
            element_id = registry.id_for("info_record_conditions", "valid_to")
            if connection.exists(element_id):
                connection.set_text(element_id, format_date(valid_to))

        # Konditionsbetrag in der ersten Zeile der Konditionstabelle
        amount_key = "condition_amount_cell"
        if registry.has("info_record_conditions", amount_key) and position.price is not None:
            element_id = registry.id_for("info_record_conditions", amount_key, row=0)
            if connection.exists(element_id):
                connection.set_text(element_id, self._decimal_text(position.price))
                if registry.has("info_record_conditions", "condition_per_cell") and position.price_unit:
                    per_id = registry.id_for("info_record_conditions", "condition_per_cell", row=0)
                    if connection.exists(per_id):
                        connection.set_text(per_id, str(position.price_unit))
            else:
                # Fallback: Preis im EKorg-Bild
                logger.warning("Konditionstabelle nicht gefunden -- Preis bleibt im EKorg-Bild.")
                messages.append("Hinweis: Konditionstabelle nicht gefunden")
        connection.send_vkey(0)
        connection.raise_on_error_status("Konditionen setzen")
        messages.append(f"Konditionen gueltig {format_date(valid_from)} – "
                        f"{format_date(valid_to) or 'offen'}")

        self._write_scales(position, messages)

    def _write_scales(self, position: OfferPosition, messages: list[str]) -> None:
        """Mengenstaffel im Konditionsbild pflegen.

        Sind die Staffel-Feld-IDs nicht geprueft, wird die Staffel NICHT
        geschrieben.  Der Grundpreis steht dann trotzdem korrekt im Satz --
        aber der Anwender erfaehrt es, statt es spaeter im Beleg zu merken.
        """
        if not self.settings.workflow.info_record_write_scales:
            return
        if not position.has_scales:
            return

        registry = self.selectors
        connection = self.connection
        ungeprueft = unverified_scale_selectors(registry)
        if ungeprueft:
            messages.append(scale_not_written_message(ungeprueft, self.settings))
            logger.warning("Staffel fuer %s/%s uebersprungen: ungepruefte Feld-IDs",
                           position.material_number, position.vendor_number)
            return

        stufen, hinweise = scale_levels_for(position, self.settings)
        messages.extend(hinweise)

        if registry.has("info_record_conditions", "scales_button"):
            element_id = registry.id_for("info_record_conditions", "scales_button")
            if connection.exists(element_id):
                connection.press_button(element_id)
                connection.raise_on_error_status("Wechsel ins Staffelbild")

        geschrieben = 0
        for row, (menge, preis) in enumerate(stufen):
            mengen_id = registry.id_for("info_record_conditions", "scale_quantity_cell",
                                        row=row)
            betrag_id = registry.id_for("info_record_conditions", "scale_amount_cell",
                                        row=row)
            if not connection.exists(mengen_id) or not connection.exists(betrag_id):
                messages.append(f"Staffel unvollstaendig: Zeile {row + 1} ist in SAP nicht "
                                f"vorhanden -- bitte in "
                                f"{self.settings.transactions.info_record_change} pruefen.")
                break
            connection.set_text(mengen_id, self._decimal_text(menge))
            connection.set_text(betrag_id, self._decimal_text(preis))
            geschrieben += 1

        connection.send_vkey(0)
        connection.raise_on_error_status("Staffel setzen")
        if geschrieben:
            messages.append(f"Mengenstaffel mit {geschrieben} Stufe(n) gepflegt: "
                            f"{position.scale_display()}")

    # -- Lesehilfen ----------------------------------------------------
    def _read_general_screen(self, record: SapInfoRecord) -> None:
        connection = self.connection
        registry = self.selectors
        if registry.has("info_record_general", "vendor_material"):
            record.vendor_material_number = connection.read_text(
                registry.id_for("info_record_general", "vendor_material"))
        if registry.has("info_record_general", "vendor_material_text"):
            record.vendor_material_text = connection.read_text(
                registry.id_for("info_record_general", "vendor_material_text"))

    def _read_purchasing_screen(self, record: SapInfoRecord) -> None:
        connection = self.connection
        registry = self.selectors
        price_id = registry.id_for("info_record_purchasing", "net_price")
        for _ in range(3):
            if connection.exists(price_id):
                break
            connection.send_vkey(0)
            status = connection.read_status()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)

        def text(key: str) -> str:
            if not registry.has("info_record_purchasing", key):
                return ""
            return connection.read_text(registry.id_for("info_record_purchasing", key))

        record.price = parse_decimal(text("net_price"))
        record.currency = text("currency")
        record.price_unit = parse_int(text("price_unit"))
        record.order_unit = text("order_unit")
        record.lead_time_days = parse_int(text("lead_time"))
        record.min_order_qty = parse_decimal(text("min_quantity"))
        record.standard_qty = parse_decimal(text("standard_quantity"))
        record.purchasing_group = text("purchasing_group")
        record.tax_code = text("tax_code")
        record.incoterm = text("incoterm")
        record.incoterm_location = text("incoterm_location")

    def _read_conditions(self, record: SapInfoRecord) -> None:
        """Gueltigkeit und Konditionszeilen lesen (best effort)."""
        connection = self.connection
        registry = self.selectors
        if not registry.has("info_record_conditions", "valid_from"):
            return
        try:
            button_key = "conditions_button"
            if registry.has("info_record_purchasing", button_key):
                element_id = registry.id_for("info_record_purchasing", button_key)
                if connection.exists(element_id):
                    connection.press_button(element_id)
            valid_from_id = registry.id_for("info_record_conditions", "valid_from")
            if not connection.exists(valid_from_id):
                return
            record.valid_from = parse_date(connection.read_text(valid_from_id))
            if registry.has("info_record_conditions", "valid_to"):
                record.valid_to = parse_date(
                    connection.read_text(registry.id_for("info_record_conditions", "valid_to")))

            if not registry.has("info_record_conditions", "condition_type_cell"):
                return
            for row in range(8):
                type_id = registry.id_for("info_record_conditions", "condition_type_cell", row=row)
                if not connection.exists(type_id):
                    break
                condition_type = connection.read_text(type_id)
                if not condition_type:
                    break
                amount = None
                if registry.has("info_record_conditions", "condition_amount_cell"):
                    amount = parse_decimal(connection.read_text(
                        registry.id_for("info_record_conditions", "condition_amount_cell", row=row)))
                record.conditions.append(SapCondition(
                    condition_type=condition_type, amount=amount,
                    currency=record.currency, price_unit=record.price_unit,
                    uom=record.order_unit, valid_from=record.valid_from,
                    valid_to=record.valid_to,
                ))
        except SapError as exc:
            logger.debug("Konditionen konnten nicht gelesen werden: %s", exc)

    # -- Kleinkram -----------------------------------------------------
    def _validate(self, position: OfferPosition) -> str:
        if not position.material_number:
            return "Keine Materialnummer -- Infosatz kann nicht gepflegt werden."
        if not position.vendor_number:
            return "Kein SAP-Lieferant zugeordnet."
        if not position.purchasing_org:
            return "Keine Einkaufsorganisation angegeben."
        if position.price is None:
            return "Kein Preis erkannt."
        if position.price < 0:
            return "Negativer Preis -- Pflege abgelehnt."
        if not position.currency:
            return "Keine Waehrung angegeben."
        limit = self.settings.thresholds.max_absolute_price
        if limit and position.price > limit:
            return (f"Preis {position.price} liegt ueber der konfigurierten Obergrenze "
                    f"({limit}). Bitte pruefen.")
        return ""

    def _new_value_text(self, position: OfferPosition, context: WriteContext) -> str:
        text = f"{position.price} {position.currency}"
        if position.price_unit:
            text += f" / {position.price_unit} {position.uom}".rstrip()
        if context.valid_from:
            text += f", ab {format_date(context.valid_from)}"
        if context.valid_to:
            text += f" bis {format_date(context.valid_to)}"
        if position.has_scales:
            text += f" (Staffel: {position.scale_display()})"
        return text

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str:
        """SAP erwartet die Eingabe im Benutzerformat; wir liefern deutsches
        Format mit Komma, da die Anwendung fuer DE-Anwender gebaut ist.

        TODO: Falls die SAP-Anmeldung ein anderes Dezimalformat verwendet
        (SU3 -> Festwerte -> Dezimaldarstellung), hier umstellen.
        """
        if value is None:
            return ""
        return format(value.normalize(), "f").replace(".", ",")

    @staticmethod
    def _extract_number(text: str) -> str:
        match = _DOC_NUMBER.search(text or "")
        return match.group(1) if match else ""

    def _leave_quietly(self) -> None:
        try:
            if self.connection is not None:
                self.connection.leave_transaction()
        except SapError:
            pass
