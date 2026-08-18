"""Angebotsdokument als Anlage am SAP-Objekt (GOS).

Warum
=====
Ein halbes Jahr nach der Preispflege fragt jemand: "Warum steht hier dieser
Preis?"  Haengt das Angebot als Anlage am Infosatz bzw. am Beleg, ist die
Frage in zehn Sekunden beantwortet.  Sonst beginnt die Suche im Postfach.

Wie SAP das macht
=================
Ueber die Dienste zum Objekt (GOS): Werkzeugleiste links oben in der
Anzeige-/Aenderungstransaktion -> "Anlage erstellen" -> Dateiauswahl.

DIE ENTSCHEIDENDE EINSCHRAENKUNG -- BITTE LESEN
===============================================
Der Dateiauswahl-Dialog ist in den meisten Systemen ein Fenster des
**Betriebssystems** und kein SAP-GUI-Objekt.  SAP GUI Scripting kann ihn dort
nicht ansprechen; ``findById`` findet schlicht nichts.  Es gibt Systeme, die
stattdessen den SAP-eigenen Dateidialog verwenden (``wnd[1]`` mit einem
Pfadfeld) -- dort funktioniert die Automatisierung.

Diese Anwendung geht deshalb ehrlich vor:

* Die Navigation bis zum Dateidialog wird automatisiert.
* Ist das Pfadfeld als SAP-GUI-Element ansprechbar, wird der Pfad eingetragen
  und bestaetigt -> Erfolg.
* Ist es das NICHT, wird **kein Erfolg gemeldet**.  Das Ergebnis ist eine
  ehrliche Teilmeldung mit dem vollstaendigen Dateipfad, damit der Anwender
  die Anlage in zehn Sekunden von Hand nachzieht.

Lieber eine ehrliche Teilmeldung als ein falsches Haekchen im Protokoll: ein
Prueffpfad, den es nicht gibt, ist schlimmer als gar keiner, weil sich
niemand mehr darum kuemmert.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..models.document_plan import AttachmentRef
from ..models.enums import ResultState
from ..models.results import ActionResult
from .connection import SapBusinessError, SapError, SapPopupError
from .interfaces import AttachmentServiceBase, WriteContext

logger = logging.getLogger(__name__)

#: Aktionsschluessel der Ergebnisobjekte
ACTION = "attachment"

#: Objektarten, an die eine Anlage gehaengt werden kann
OBJECT_LABELS: dict[str, str] = {
    "info_record": "Infosatz",
    "source_list": "Orderbuch",
    "contract": "Mengenkontrakt",
    "purchase_order": "Bestellung",
    "vendor": "Lieferantenstamm",
}

#: Anzeigetransaktion je Objektart -- dort liegt die GOS-Werkzeugleiste
_DISPLAY_TRANSACTION: dict[str, str] = {
    "info_record": "info_record_display",
    "source_list": "source_list_display",
    "contract": "contract_display",
    "purchase_order": "purchase_order_display",
    "vendor": "vendor_display",
}


def object_label(object_kind: str) -> str:
    return OBJECT_LABELS.get(object_kind, object_kind)


def is_enabled(settings, object_kind: str) -> bool:
    """Ist die Anlage fuer diese Objektart eingeschaltet?"""
    attachments = getattr(settings, "attachments", None)
    if attachments is None:
        return False
    schalter = {
        "info_record": "attach_to_info_record",
        "source_list": "attach_to_source_list",
        "contract": "attach_to_contract",
        "purchase_order": "attach_to_purchase_order",
        "vendor": "attach_to_vendor",
    }.get(object_kind, "")
    return bool(schalter) and bool(getattr(attachments, schalter, False))


def check_attachment(attachment: AttachmentRef | None, settings) -> str:
    """Darf diese Datei ueberhaupt angehaengt werden?

    Liefert einen Klartextgrund oder "" (in Ordnung).  Es wird nie geraten:
    fehlt die Datei oder ist sie zu gross, sagt die Anwendung das, statt
    stillschweigend nichts zu tun.
    """
    if attachment is None:
        return ("Es ist kein Angebotsdokument hinterlegt -- es kann nichts "
                "angehaengt werden.")
    if attachment.error:
        return attachment.error
    if not attachment.path:
        return ("Es ist kein Angebotsdokument hinterlegt -- es kann nichts "
                "angehaengt werden.")
    if not attachment.available:
        return (f"Die Datei '{attachment.path}' ist nicht (mehr) auffindbar -- "
                "es wurde nichts angehaengt.")

    grenze = int(getattr(getattr(settings, "attachments", None),
                         "max_attachment_mb", 0) or 0)
    if grenze > 0 and attachment.size_mb > grenze:
        return (f"Die Datei '{attachment.display_name or attachment.path}' ist "
                f"{attachment.size_mb:.1f} MB gross und ueberschreitet die "
                f"eingestellte Grenze von {grenze} MB -- es wurde nichts "
                f"angehaengt.")
    return ""


def manual_hint(attachment: AttachmentRef, object_kind: str, object_key: str,
                reason: str) -> str:
    """Ehrliche Teilmeldung inklusive vollstaendigem Dateipfad."""
    return (f"Anlage NICHT angehaengt ({object_label(object_kind)} "
            f"{object_key}): {reason} Bitte von Hand anhaengen "
            f"(Dienste zum Objekt -> Anlage erstellen). Datei: "
            f"{attachment.path or '(unbekannt)'}")


# ---------------------------------------------------------------------------
# Zusammenfassung als Zusatzanlage
# ---------------------------------------------------------------------------

def build_summary_lines(offer, positions) -> list[str]:
    """Kurze Textzusammenfassung der uebernommenen Werte.

    Bewusst schlicht und in Klartext: Sie soll neben dem Originalangebot
    haengen und in fuenf Sekunden lesbar sein.
    """
    from ..utils.parsing import format_date, format_decimal

    zeilen = [
        "Uebernahme aus Lieferantenangebot",
        f"Erzeugt am:      {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"Lieferant:       {getattr(offer, 'vendor_name', '') or '-'} "
        f"({getattr(offer, 'vendor_number', '') or '-'})",
        f"Angebotsnummer:  {getattr(offer, 'offer_number', '') or '-'}",
        f"Angebotsdatum:   {format_date(getattr(offer, 'offer_date', None)) or '-'}",
        f"Quelle:          {getattr(offer, 'source_label', '') or '-'}",
        "",
        "Positionen:",
    ]
    for position in positions or []:
        preis = (f"{format_decimal(position.price)} {position.currency}"
                 if position.price is not None else "-")
        zeilen.append(
            f"  {position.material_number or position.display_name}: {preis}"
            f" / PE {position.price_unit or 1} {position.uom}"
            f"{'  ab ' + format_date(position.valid_from) if position.valid_from else ''}")
    return zeilen


def write_summary_file(settings, base_name: str, lines: list[str]) -> AttachmentRef:
    """Zusammenfassung als Textdatei ablegen und als Anlage zurueckgeben."""
    ordner = Path(settings.home) / "anlagen"
    sicher = "".join(c if c.isalnum() or c in "-_" else "_" for c in base_name) or "angebot"
    ziel = ordner / f"Uebernahme_{sicher}_{datetime.now():%Y%m%d_%H%M%S}.txt"
    try:
        ordner.mkdir(parents=True, exist_ok=True)
        ziel.write_text("\r\n".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("Zusammenfassung konnte nicht abgelegt werden: %s", exc)
        return AttachmentRef(error=f"Zusammenfassung konnte nicht abgelegt werden: {exc}")
    return AttachmentRef(path=str(ziel), display_name=ziel.name)


# ---------------------------------------------------------------------------
# Echtbetrieb
# ---------------------------------------------------------------------------

class SapAttachmentService(AttachmentServiceBase):
    """Anlage ueber die Dienste zum Objekt (GOS) anhaengen."""

    write_operation = "attachment_write"

    def attach(self, attachment: AttachmentRef | None, object_kind: str,
               object_key: str, context: WriteContext) -> ActionResult:
        started = self._now_ms()
        label = object_label(object_kind)

        problem = check_attachment(attachment, self.settings)
        if problem:
            # Kein Erfolg, aber auch kein Fehler der eigentlichen Aktion:
            # der Preis ist gepflegt, nur der Prueffpfad fehlt.
            return self._result(ACTION, ResultState.SKIPPED, problem,
                                started_ms=started)
        assert attachment is not None                     # durch check_attachment

        if not object_key:
            return self._result(
                ACTION, ResultState.SKIPPED,
                f"Anlage NICHT angehaengt: SAP hat keine Nummer zum {label} "
                f"gemeldet -- es ist unklar, an welches Objekt sie gehoert.",
                started_ms=started)

        if context.dry_run:
            return self._result(
                ACTION, ResultState.SIMULATED,
                f"Anlage '{attachment.display_name}' wuerde an {label} "
                f"{object_key} gehaengt.", started_ms=started)

        try:
            self.selectors.ensure_ready(self.write_operation)
        except Exception as exc:  # SelectorNotVerifiedError
            return self._result(
                ACTION, ResultState.SKIPPED,
                manual_hint(attachment, object_kind, object_key,
                            "Die SAP-Feld-IDs der Maske 'attachment' sind "
                            "ungeprueft, deshalb wurde nichts angehaengt."),
                detail=str(exc), started_ms=started)

        connection = self.connection
        if connection is None:
            return self._result(
                ACTION, ResultState.SKIPPED,
                manual_hint(attachment, object_kind, object_key,
                            "Es besteht keine SAP-Verbindung."),
                started_ms=started)

        registry = self.selectors
        messages: list[str] = []
        try:
            connection.allow_write = True
            transaction = getattr(self.settings.transactions,
                                  _DISPLAY_TRANSACTION.get(object_kind, ""), "")
            if transaction:
                connection.ensure_transaction(transaction)
                messages.append(f"Objekt in {transaction} geoeffnet")

            # 1. Dienste zum Objekt oeffnen, 2. "Anlage erstellen" waehlen.
            connection.press_button(registry.id_for("attachment", "gos_toolbar"),
                                    expect_write=True)
            connection.press_button(
                registry.id_for("attachment", "gos_create_attachment"),
                expect_write=True)

            # 3. Dateidialog.  Genau hier liegt die Grenze des Verfahrens.
            pfad_feld = registry.id_for("attachment", "file_dialog_path")
            if not connection.exists(pfad_feld):
                # Kein SAP-GUI-Element -> es ist ein Windows-Fenster.  Es wird
                # KEIN Erfolg gemeldet und auch nichts blind bestaetigt.
                logger.info("Dateiauswahl ist kein SAP-GUI-Element -- Anlage bleibt "
                            "manuell (%s %s).", label, object_key)
                return self._result(
                    ACTION, ResultState.SKIPPED,
                    manual_hint(attachment, object_kind, object_key,
                                "Der Dateiauswahl-Dialog ist ein Fenster des "
                                "Betriebssystems und laesst sich per SAP GUI "
                                "Scripting nicht bedienen."),
                    transaction=transaction, started_ms=started,
                    sap_messages=messages)

            connection.set_text(pfad_feld, attachment.path)
            if registry.has("attachment", "file_dialog_name"):
                namens_feld = registry.id_for("attachment", "file_dialog_name")
                if connection.exists(namens_feld):
                    connection.set_text(namens_feld, attachment.display_name)
            connection.press_button(registry.id_for("attachment", "file_dialog_ok"),
                                    expect_write=True)

            status = connection.read_status()
            if status.is_error:
                raise SapBusinessError(status.text, status.message_id, status.number)
            if status.text:
                messages.append(status.display())

            popup = connection.detect_popup()
            if popup is not None:
                # Popups werden nie automatisch bestaetigt.
                raise SapPopupError(
                    "Nach dem Anhaengen erschien ein unerwartetes Fenster.",
                    popup_text=popup.get("text", ""), title=popup.get("title", ""))

            return self._result(
                ACTION, ResultState.SUCCESS,
                f"Angebot '{attachment.display_name}' an {label} {object_key} "
                f"angehaengt", transaction=transaction, document_number=object_key,
                new_value=attachment.display_name, started_ms=started,
                sap_messages=messages)
        except SapPopupError as exc:
            return self._result(
                ACTION, ResultState.SKIPPED,
                manual_hint(attachment, object_kind, object_key,
                            f"Unerwartetes SAP-Fenster: {exc.title or 'ohne Titel'}."),
                detail=exc.popup_text, started_ms=started, sap_messages=messages)
        except SapError as exc:
            return self._result(
                ACTION, ResultState.SKIPPED,
                manual_hint(attachment, object_kind, object_key, exc.message),
                detail=getattr(exc, "detail", ""), started_ms=started,
                sap_messages=messages)
        finally:
            if connection is not None:
                connection.allow_write = False
                try:
                    connection.leave_transaction()
                except SapError:
                    pass


__all__ = [
    "ACTION",
    "OBJECT_LABELS",
    "SapAttachmentService",
    "build_summary_lines",
    "check_attachment",
    "is_enabled",
    "manual_hint",
    "object_label",
    "write_summary_file",
]
