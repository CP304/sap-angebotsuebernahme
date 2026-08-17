"""Zentrale Konfiguration.

Grundsatz: Keine fachlich relevanten Werte im Code hart verdrahten.  Alles,
was sich je Kunde/Mandant/Anwender unterscheiden kann, steht hier und wird als
JSON neben der Datenbank abgelegt.

Speicherort (Windows):  ``%APPDATA%\\SAP-Angebotsuebernahme\\settings.json``
Ueberschreibbar per Umgebungsvariable ``SAP_ANGEBOT_HOME``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APP_NAME = "SAP-Angebotsuebernahme"


def app_home() -> Path:
    """Basisverzeichnis fuer Datenbank, Logs und Konfiguration."""
    override = os.environ.get("SAP_ANGEBOT_HOME")
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / APP_NAME


# ---------------------------------------------------------------------------
# Teilbereiche
# ---------------------------------------------------------------------------

@dataclass
class PurchasingDefaults:
    """Einkaufsorganisatorische Vorbelegung."""

    purchasing_org: str = "1000"
    purchasing_group: str = "100"
    plant: str = "1000"
    currency: str = "EUR"
    order_unit: str = "ST"
    price_unit: int = 1
    #: Belegart Mengenkontrakt (MK) bzw. Wertkontrakt (WK)
    contract_document_type: str = "MK"
    #: Belegart Bestellung
    purchase_order_document_type: str = "NB"
    #: Laufzeit eines neuen Kontrakts in Monaten
    contract_duration_months: int = 12
    #: Standard-Lieferzeit fuer neue Bestellungen (Tage ab heute), wenn das
    #: Angebot keine Lieferzeit nennt
    default_delivery_days: int = 14
    #: Gueltigkeitsende fuer neue Infosatz-Konditionen.
    #: Hauseigener Platzhalter -- bewusst *nicht* 31.12.9999.
    info_record_valid_to: str = "31.12.2099"


@dataclass
class Thresholds:
    """Grenzwerte fuer die Plausibilitaetspruefung."""

    price_warn_percent: Decimal = Decimal("10")
    price_error_percent: Decimal = Decimal("30")
    #: Ab dieser Abweichung ist eine explizite Bestaetigung noetig
    price_confirm_percent: Decimal = Decimal("30")
    #: Absolute Preisobergrenze zur Tippfehlererkennung (0 = aus)
    max_absolute_price: Decimal = Decimal("100000")
    #: Mindestaehnlichkeit fuer automatische Lieferantenzuordnung
    vendor_match_threshold: Decimal = Decimal("0.88")
    #: Ab hier wird ein Vorschlag angezeigt (aber nicht automatisch gesetzt)
    vendor_suggest_threshold: Decimal = Decimal("0.60")
    #: Warnen, wenn Angebot aelter als X Tage
    offer_age_warn_days: int = 90


@dataclass
class Transactions:
    """Verwendete SAP-Transaktionen -- kundenspezifisch anpassbar."""

    info_record_display: str = "ME13"
    info_record_create: str = "ME11"
    info_record_change: str = "ME12"
    source_list_maintain: str = "ME01"
    source_list_display: str = "ME03"
    contract_create: str = "ME31K"
    contract_change: str = "ME32K"
    contract_display: str = "ME33K"
    purchase_order_create: str = "ME21N"
    purchase_order_change: str = "ME22N"
    purchase_order_display: str = "ME23N"
    material_display: str = "MM03"
    vendor_display: str = "XK03"
    vendor_list: str = "MKVZ"


@dataclass
class SapRuntime:
    """Laufzeitverhalten des GUI-Scriptings."""

    #: Sekunden, die auf ein Element gewartet wird, bevor abgebrochen wird
    element_timeout_s: float = 10.0
    #: Pollintervall beim Warten auf Elemente
    poll_interval_s: float = 0.15
    #: Wiederholungen bei sporadischen COM-Fehlern
    retry_count: int = 2
    retry_delay_s: float = 0.5
    #: Wartezeit nach einem Transaktionsstart (nur Fallback, primaer wird auf
    #: Elemente gewartet)
    transaction_settle_s: float = 0.2
    #: Index der zu verwendenden Verbindung/Session (0 = erste)
    connection_index: int = 0
    session_index: int = 0
    #: Popups niemals blind bestaetigen
    auto_confirm_popups: bool = False
    #: Statusleiste nach jedem Schritt auslesen (kostet Zeit, hilft aber sehr)
    read_status_bar: bool = True
    #: SAP-GUI-Scripting-Warnhinweis unterdruecken ist Sache des Basis-Teams
    fail_on_scripting_disabled: bool = True
    #: Infosatzpreis ueber das Konditionsbild pflegen (noetig fuer Gueltig-bis)
    #: statt nur ueber das Feld "Nettopreis" im EKorg-Bild
    info_record_price_via_conditions: bool = True
    #: Nach dem Einstieg pruefen, ob Material/Lieferant in der Maske stehen
    verify_context_before_write: bool = True
    #: Maximale Anzahl Zeilen, die in einem Table-Control durchsucht werden
    max_table_rows: int = 200


@dataclass
class ExtractionSettings:
    """Steuerung der regelbasierten Angebotserkennung."""

    #: Erkennungsspalten in Excel-Dateien (Kleinschreibung, Teiltreffer)
    column_aliases: dict[str, list[str]] = field(default_factory=lambda: {
        "position_number": ["pos", "pos.", "position", "positionsnummer", "item", "lfd", "nr", "nr."],
        "material_number": ["material", "materialnummer", "matnr", "sap-material",
                            "sap material", "artikelnummer kunde", "kundenartikelnummer",
                            "ihre artikelnr", "ihre artikelnummer", "teilenummer"],
        "vendor_material_number": ["lieferantenmaterial", "lief.-material", "artikelnummer",
                                   "artikel-nr", "artikelnr", "art.-nr", "sachnummer",
                                   "bestellnummer", "hersteller-nr", "unsere artikelnummer",
                                   "vendor part", "part number", "part no"],
        "description": ["bezeichnung", "beschreibung", "benennung", "text", "artikelbezeichnung",
                        "kurztext", "description", "material description"],
        "quantity": ["menge", "anzahl", "stueckzahl", "stückzahl", "qty", "quantity", "bedarf"],
        "uom": ["me", "einheit", "mengeneinheit", "meh", "uom", "unit", "verpackungseinheit"],
        "price": ["preis", "ep", "einzelpreis", "netto", "nettopreis", "stueckpreis",
                  "stückpreis", "preis/einheit", "unit price", "price", "vk", "ek-preis"],
        "price_unit": ["preiseinheit", "pe", "peh", "per", "price unit", "je"],
        "currency": ["waehrung", "währung", "wkz", "currency", "curr"],
        "min_order_qty": ["mindestmenge", "mindestbestellmenge", "min. menge", "mbm",
                          "moq", "min order", "mindestabnahme"],
        "lead_time_days": ["lieferzeit", "liefertermin tage", "wiederbeschaffungszeit",
                           "wbz", "lead time", "lieferfrist"],
        "valid_from": ["gueltig ab", "gültig ab", "preis gueltig ab", "valid from", "ab"],
        "remarks": ["bemerkung", "bemerkungen", "hinweis", "anmerkung", "remark", "notes",
                    "kommentar"],
    })
    #: Zeilen mit weniger als so vielen erkannten Feldern werden verworfen
    min_fields_per_position: int = 2
    #: Freitextzeilen in E-Mails/PDFs auswerten (Positionstabelle nicht erkannt)
    enable_freetext_positions: bool = True
    #: Anhaenge einer E-Mail mitverarbeiten
    process_email_attachments: bool = True
    #: Dateiendungen, die als Angebotsanhang gelten
    attachment_extensions: list[str] = field(
        default_factory=lambda: [".pdf", ".xlsx", ".xlsm", ".xls", ".csv"]
    )
    #: Maximale Anhangsgroesse in MB
    max_attachment_mb: int = 25
    #: Signatur-/Disclaimer-Bloecke in Mails abschneiden
    strip_email_signature: bool = True


@dataclass
class WorkflowDefaults:
    """Der "Komplettvorgang": aus einem Angebot alles in einem Rutsch.

    Reihenfolge ist bewusst fest verdrahtet und *nicht* frei sortierbar:

      1. Infosatz    -- Preis muss stehen, bevor Belege darauf zugreifen
      2. Kontrakt    -- liefert die Belegnummer fuer Schritt 3 und 4
      3. Orderbuch   -- kann den Kontrakt als Vereinbarung referenzieren
      4. Bestellung  -- wird als Abruf mit Bezug auf den Kontrakt angelegt

    Wer die Reihenfolge aendert, verliert den Belegbezug.
    """

    #: Welche Schritte der Komplettvorgang standardmaessig anhakt
    chain_info_record: bool = True
    chain_source_list: bool = True
    chain_contract: bool = True
    chain_purchase_order: bool = True

    # -- Infosatz --------------------------------------------------------
    #: Gueltig-bis-Platzhalter fuer Infosatzkonditionen
    info_record_valid_to: str = "31.12.2099"

    # -- Orderbuch -------------------------------------------------------
    #: Orderbucheintrag bis zu diesem Datum anlegen
    source_list_valid_to: str = "31.12.2099"
    #: "Lieferant aktiv setzen": Sperrkennzeichen aus, Dispo-Kennzeichen an
    source_list_set_active: bool = True
    #: Dispokennzeichen ("1" = Orderbuch fuer Dispo relevant, "" = keins)
    source_list_mrp_indicator: str = "1"
    #: Feste Bezugsquelle setzen (Vorsicht: verdraengt andere Lieferanten)
    source_list_set_fixed: bool = False
    #: Angelegten Kontrakt als Vereinbarung im Orderbuch eintragen
    source_list_reference_contract: bool = True

    # -- Kontrakt --------------------------------------------------------
    #: Laufzeitende; leer = aus Laufzeit in Monaten berechnen
    contract_valid_to: str = "31.12.2099"

    # -- Bestellung ------------------------------------------------------
    #: Bestellung mit Bezug auf den soeben angelegten Kontrakt anlegen
    purchase_order_from_contract: bool = True
    #: Abrufmenge: "percent" (Anteil der Kontraktmenge), "absolute", "full"
    call_off_mode: str = "percent"
    call_off_percent: Decimal = Decimal("20")
    call_off_quantity: Decimal | None = None
    #: Abrufmenge auf ganze Einheiten runden
    call_off_round_to_integer: bool = True

    # -- Nachrichten (Sicherheit!) --------------------------------------
    #: Nachrichtenfindung in Kontrakt/Bestellung aktiv unterdruecken.
    #: Es soll ausdruecklich NICHTS an den Lieferanten hinausgehen.
    suppress_output_messages: bool = True
    #: Wenn die Nachrichten nicht nachweislich entfernt werden konnten,
    #: wird der Beleg NICHT gesichert.  Nur bewusst abschaltbar.
    abort_if_messages_present: bool = True


@dataclass
class UiSettings:
    """Oberflaeche und Bedienung."""

    theme: str = "hell"                    # "hell" | "dunkel"
    font_size: int = 10
    table_row_height: int = 26
    confirm_before_write: bool = True
    remember_window_state: bool = True
    recent_files: list[str] = field(default_factory=list)
    max_recent_files: int = 12
    #: Positionen nach dem Import automatisch selektieren
    autoselect_after_import: bool = True
    #: Standardhaken je Position
    default_do_info_record: bool = True
    default_do_source_list: bool = False
    default_do_contract: bool = False
    default_do_purchase_order: bool = False


@dataclass
class Settings:
    """Gesamtkonfiguration der Anwendung."""

    purchasing: PurchasingDefaults = field(default_factory=PurchasingDefaults)
    thresholds: Thresholds = field(default_factory=Thresholds)
    transactions: Transactions = field(default_factory=Transactions)
    sap: SapRuntime = field(default_factory=SapRuntime)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    workflow: WorkflowDefaults = field(default_factory=WorkflowDefaults)
    ui: UiSettings = field(default_factory=UiSettings)

    #: Betriebsart
    dry_run: bool = True
    use_mock_sap: bool = True

    #: Pfade (leer = Standard unterhalb von app_home())
    database_path: str = ""
    log_path: str = ""
    selectors_path: str = ""

    # ------------------------------------------------------------------
    # Pfade
    # ------------------------------------------------------------------
    @property
    def home(self) -> Path:
        return app_home()

    @property
    def db_file(self) -> Path:
        if self.database_path:
            return Path(self.database_path).expanduser()
        return self.home / "historie.sqlite3"

    @property
    def log_dir(self) -> Path:
        if self.log_path:
            return Path(self.log_path).expanduser()
        return self.home / "logs"

    @property
    def selectors_file(self) -> Path:
        if self.selectors_path:
            return Path(self.selectors_path).expanduser()
        return self.home / "sap_selectors.json"

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = path or (app_home() / "settings.json")
        settings = cls()
        if not target.exists():
            logger.info("Keine Konfiguration gefunden, Standardwerte werden verwendet (%s)", target)
            return settings
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Konfiguration konnte nicht gelesen werden (%s): %s", target, exc)
            return settings
        try:
            _apply_dict(settings, data)
        except Exception as exc:  # noqa: BLE001 - defekte Datei darf nicht killen
            logger.error("Konfiguration teilweise ungueltig: %s", exc)
        logger.info("Konfiguration geladen: %s", target)
        return settings

    def save(self, path: Path | None = None) -> Path:
        target = path or self.settings_file
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, ensure_ascii=False, default=_json_default)
        target.write_text(payload, encoding="utf-8")
        logger.info("Konfiguration gespeichert: %s", target)
        return target

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Abgeleitete Werte
    # ------------------------------------------------------------------
    def parsed_info_record_valid_to(self):
        """Gueltig-bis fuer Infosaetze als ``date`` (oder ``None``)."""
        from ..utils.parsing import parse_date

        return parse_date(self.workflow.info_record_valid_to
                          or self.purchasing.info_record_valid_to)

    def parsed_source_list_valid_to(self):
        from ..utils.parsing import parse_date

        return parse_date(self.workflow.source_list_valid_to)

    def parsed_contract_valid_to(self):
        from ..utils.parsing import parse_date

        return parse_date(self.workflow.contract_valid_to)

    def add_recent_file(self, path: str) -> None:
        recents = [r for r in self.ui.recent_files if r != path]
        recents.insert(0, path)
        self.ui.recent_files = recents[: self.ui.max_recent_files]


# ---------------------------------------------------------------------------
# Hilfsfunktionen fuer die (De-)Serialisierung
# ---------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Nicht serialisierbar: {type(value)!r}")


def _coerce(value: Any, target_type: Any) -> Any:
    """Wert aus JSON auf den Typ des Dataclass-Felds bringen."""
    if target_type is Decimal or target_type == "Decimal":
        return Decimal(str(value))
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "ja", "yes", "on")
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _apply_dict(instance: Any, data: dict) -> None:
    """JSON-Daten rekursiv auf eine Dataclass-Instanz anwenden.

    Unbekannte Schluessel werden ignoriert, damit aeltere/neuere Dateien nicht
    zum Absturz fuehren.
    """
    type_hints = {f.name: f.type for f in fields(instance)}
    for key, value in data.items():
        if key not in type_hints:
            logger.debug("Unbekannter Konfigurationsschluessel wird ignoriert: %s", key)
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_dict(current, value)
            continue
        hint = type_hints[key]
        hint_text = hint if isinstance(hint, str) else getattr(hint, "__name__", str(hint))
        if value is None:
            # Optionale Felder duerfen explizit auf None gesetzt werden,
            # Pflichtfelder behalten ihren Standardwert.
            if "None" in hint_text or "Optional" in hint_text:
                setattr(instance, key, None)
            continue
        try:
            if "Decimal" in hint_text:
                setattr(instance, key, Decimal(str(value)))
            elif hint_text == "bool":
                setattr(instance, key, _coerce(value, bool))
            elif hint_text == "int":
                setattr(instance, key, int(value))
            elif hint_text == "float":
                setattr(instance, key, float(value))
            elif hint_text == "str":
                setattr(instance, key, str(value))
            else:
                setattr(instance, key, value)
        except (TypeError, ValueError) as exc:
            logger.warning("Konfigurationswert '%s' ungueltig (%s) -- Standard bleibt aktiv", key, exc)
