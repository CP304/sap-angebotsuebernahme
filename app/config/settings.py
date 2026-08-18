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

    # -- Kontierung der Bestellung --------------------------------------
    #: Kontierungstyp: "" = Lagerbestellung (kein Kontierungsbild),
    #: "K" = Kostenstelle, "F" = Auftrag, "A" = Anlage ...
    account_assignment_category: str = ""
    #: Vorbelegung, wenn kontiert wird (leer = Anwender traegt ein)
    default_cost_center: str = ""
    default_gl_account: str = ""
    #: Bei kontierten Positionen ist das Sachkonto in vielen Systemen Pflicht
    require_gl_account_when_assigned: bool = True


@dataclass
class ConditionTypes:
    """Konditionsarten des Infosatzes.

    Bisher wurde nur der Bruttopreis geschrieben.  Ein Angebot enthaelt aber
    regelmaessig mehr: Rabatte, Zu- und Abschlaege, Frachtkosten.  Diese
    gehoeren als *eigene* Konditionen in den Infosatz -- nicht in den
    Nettopreis eingerechnet, sonst ist spaeter nicht mehr nachvollziehbar,
    woraus der Preis entstand.

    TODO: kundenspezifisch pruefen.  Die hier eingetragenen Konditionsarten
    sind die SAP-Standardauslieferung; in vielen Systemen kommen eigene
    Z-Konditionen dazu.
    """

    gross_price: str = "PB00"          # Bruttopreis
    discount_percent: str = "RA01"     # Rabatt in Prozent
    discount_absolute: str = "RB00"    # Abschlag absolut
    surcharge_percent: str = "ZA01"    # Zuschlag in Prozent (haeufig Z-Kondition)
    surcharge_absolute: str = "ZB00"   # Zuschlag absolut
    freight_percent: str = "FRA1"      # Frachtkosten in Prozent
    freight_absolute: str = "FRB1"     # Frachtkosten absolut
    cash_discount: str = "SKTO"        # Skonto

    #: Zusatzkonditionen ueberhaupt schreiben
    write_additional_conditions: bool = True
    #: Alternative: Rabatte in den Nettopreis einrechnen statt eigener
    #: Kondition.  Einfacher, aber die Herkunft des Preises geht verloren --
    #: deshalb standardmaessig aus.
    fold_discounts_into_net_price: bool = False
    #: Mehr Zusatzkonditionen als hier zugelassen werden abgeschnitten
    #: (mit Protokolleintrag)
    max_additional_conditions: int = 8


@dataclass
class CurrencySettings:
    """Fremdwaehrungsangebote.

    Grundsatz: Es wird **nie** ein umgerechneter Betrag nach SAP geschrieben.
    SAP fuehrt eigene Kurse; ein hier umgerechneter Wert waere eine zweite,
    abweichende Wahrheit.  Der Kurs dient ausschliesslich dazu, den Vergleich
    "alter Preis gegen neuer Preis" ueberhaupt sinnvoll zu machen, wenn das
    Angebot in einer anderen Waehrung kommt als der Infosatz.
    """

    #: Hauswaehrung (Vergleichsbasis)
    company_currency: str = "EUR"
    #: Fuer den Vergleich umrechnen.  Aus = Fremdwaehrung wird nur angezeigt,
    #: die Prozentangabe entfaellt.
    convert_for_comparison: bool = True
    #: Manuell gepflegte Kurse: wie viel Hauswaehrung ist eine Einheit der
    #: Fremdwaehrung wert (1 USD = 0,92 EUR)
    exchange_rates: dict[str, str] = field(default_factory=lambda: {
        "USD": "0.92", "CHF": "1.06", "GBP": "1.17", "PLN": "0.23",
        "CZK": "0.040", "SEK": "0.088", "DKK": "0.134", "NOK": "0.086",
    })
    #: Datum der Kurspflege (TT.MM.JJJJ) -- alte Kurse sind gefaehrlich
    rate_date: str = ""
    #: Warnen, wenn die Kurse aelter sind als
    max_rate_age_days: int = 30
    #: Bei Fremdwaehrung immer warnen, auch mit gueltigem Kurs
    warn_on_foreign_currency: bool = True


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
    vendor_create: str = "XK01"
    vendor_change: str = "XK02"
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

    # -- Ruecklese-Pruefung ---------------------------------------------
    #: Nach dem Sichern den Satz erneut lesen und gegen den Sollwert
    #: vergleichen.  Die Statusleiste allein ist kein Beweis: sie meldet
    #: "gesichert", auch wenn ein Feld gar nicht uebernommen wurde.
    verify_after_write: bool = True
    #: Zulaessige Abweichung beim Preisvergleich (Rundung im SAP-Bild)
    verify_price_tolerance: Decimal = Decimal("0.005")
    #: Schlaegt die Ruecklese-Pruefung fehl, gilt die Position als
    #: fehlerhaft (True) oder nur als warnungsbehaftet (False).
    verify_failure_is_error: bool = True


@dataclass
class ExtractionSettings:
    """Steuerung der regelbasierten Angebotserkennung."""

    #: Erkennungsspalten in Excel-Dateien (Kleinschreibung, Teiltreffer)
    #:
    #: Besonders heikel sind die beiden Artikelnummern-Felder.  In einem
    #: Lieferantenangebot stehen naemlich *zwei* Nummern nebeneinander:
    #:
    #:   ``material_number``         unsere eigene SAP-Materialnummer.  Der
    #:                               Lieferant nennt sie aus *seiner* Sicht
    #:                               "Kundenartikelnummer", "Ihre Art.-Nr.",
    #:                               "Customer part no." usw.
    #:   ``vendor_material_number``  die Nummer des Lieferanten.  Aus seiner
    #:                               Sicht schlicht "Artikelnummer", "Unsere
    #:                               Art.-Nr.", "Supplier part no." usw.
    #:
    #: Die Beschriftung allein reicht nicht aus ("Artikelnummer" kann beides
    #: sein) -- deshalb entscheidet zusaetzlich der Inhalt der Spalte, siehe
    #: ``own_material_pattern`` weiter unten.
    column_aliases: dict[str, list[str]] = field(default_factory=lambda: {
        "position_number": ["pos", "pos.", "position", "positionsnummer", "item", "lfd", "nr", "nr."],
        # -- UNSERE Materialnummer (so, wie der Lieferant sie nennt) --------
        "material_number": [
            # neutrale/eindeutige Beschriftungen
            "material", "materialnummer", "material-nr", "material nr", "materialnr",
            "matnr", "mat-nr", "mat.-nr", "sap-material", "sap material",
            "sap-materialnummer", "sap-nummer", "sap nr", "teilenummer",
            # "Ihre ..." -- aus Sicht des Lieferanten sind wir der Kunde
            "ihre artikelnummer", "ihre artikelnr", "ihre artikel-nr", "ihre art.-nr",
            "ihre art-nr", "ihre art nr", "ihre artnr", "ihre artikel nr",
            "ihre materialnummer", "ihre material-nr", "ihre mat.-nr", "ihre mat-nr",
            "ihre teilenummer", "ihre sachnummer", "ihre bestellnummer",
            "ihre bestell-nr", "ihre bestellnr", "ihre nummer", "ihre referenz",
            "ihr zeichen", "ihre zeichnungsnummer",
            # "Kunden..." -- ebenfalls wir
            "kundenartikelnummer", "kundenartikelnr", "kunden-artikelnummer",
            "kunden-artikel-nr", "kundenartikel-nr", "kundenartikel nr",
            "artikelnummer kunde", "artikel-nr kunde", "artikelnummer des kunden",
            "kd-art-nr", "kd-artnr", "kd art nr", "kdartnr", "kd-artikelnummer",
            "kd-nr", "kundenmaterial", "kundenmaterialnummer", "kunden-material-nr",
            "kundenmaterial-nr", "kundenteilenummer", "kunden-teile-nr",
            "kundensachnummer", "bestellnummer kunde", "bestell-nr kunde",
            "bestellnummer des kunden", "kundenbestellnummer", "kundenreferenz",
            # englisch
            "customer part no", "customer part number", "customer part",
            "customer part #", "customer article no", "customer article number",
            "customer material", "customer material no", "customer material number",
            "customer item no", "customer item number", "customer ref",
            "customer reference", "customer p/n", "cust part no", "cust. part no",
            "your part number", "your part no", "your part #", "your article no",
            "your material", "your material no", "your item no", "your ref",
            "your reference", "your order no", "your p/n",
        ],
        # -- Nummer DES LIEFERANTEN -----------------------------------------
        "vendor_material_number": [
            # neutrale Beschriftungen (koennen laut Inhalt auch unsere sein!)
            "artikelnummer", "artikel-nr", "artikelnr", "artikel nr", "art.-nr",
            "art-nr", "art nr", "artnr", "artikel", "sachnummer", "sach-nr",
            "sachnr", "typ", "type", "typnummer", "typ-nr", "modell", "modell-nr",
            "bestellnummer", "bestell-nr", "bestellnr", "katalognummer",
            "katalog-nr", "identnummer", "ident-nr", "produktnummer", "produkt-nr",
            "erzeugnisnummer", "zeichnungsnummer",
            # ausdruecklich die des Lieferanten
            "lieferantenmaterial", "lief.-material", "lief-material",
            "lieferantenartikelnummer", "lieferanten-artikelnummer",
            "lieferanten-art-nr", "lieferantennummer artikel", "artikelnummer lieferant",
            "artikel-nr lieferant", "unsere artikelnummer", "unsere artikelnr",
            "unsere artikel-nr", "unsere art.-nr", "unsere art-nr", "unsere art nr",
            "unsere artnr", "unsere materialnummer", "unsere material-nr",
            "unsere sachnummer", "unsere nummer", "unsere bestellnummer",
            "hersteller-nr", "herstellernummer", "hersteller artikelnummer",
            "hersteller-artikelnummer", "hersteller-teilenummer",
            # englisch
            "supplier part no", "supplier part number", "supplier part",
            "supplier material", "supplier material no", "supplier item no",
            "supplier article no", "supplier ref", "vendor part", "vendor part no",
            "vendor part number", "vendor material", "vendor item no",
            "our part number", "our part no", "our part", "our article no",
            "our item no", "our material", "our reference", "our ref",
            "part number", "part no", "part #", "part-no", "partno", "p/n",
            "item no", "item number", "item #", "item code", "article no",
            "article number", "manufacturer part no", "manufacturer part number",
            "mpn", "sku", "model", "model no", "catalogue no", "catalog no",
            "product code", "product no", "product number", "ref no",
        ],
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
    # ------------------------------------------------------------------
    # Erkennung der EIGENEN Materialnummer ueber den Spalteninhalt
    # ------------------------------------------------------------------
    #: Regulaerer Ausdruck, auf den eine *eigene* SAP-Materialnummer passt.
    #:
    #: WICHTIG -- DIESER WERT MUSS AN DEN EIGENEN NUMMERNKREIS ANGEPASST
    #: WERDEN.  Der Auslieferungszustand ``^\d{6,18}$`` trifft rein numerische
    #: Nummern mit 6 bis 18 Stellen; das deckt den haeufigsten Fall ab, weil
    #: SAP-Materialnummern im Standard achtstellig numerisch sind (z. B.
    #: ``47110001``).  Es sind aber ebenso gut alphanumerische Nummernkreise
    #: ueblich.  Beispiele fuer eigene Muster:
    #:
    #:   ``^\d{8}$``               genau 8 Ziffern (klassischer SAP-Standard)
    #:   ``^[0-9]{6,8}$``          6 bis 8 Ziffern
    #:   ``^M-\d{5}$``             Praefix "M-" plus 5 Ziffern
    #:   ``^(?:\d{8}|[A-Z]{2}\d{6})$``  gemischter Nummernkreis
    #:
    #: Das Muster wird ausschliesslich fuer die *Zuordnung* verwendet -- also
    #: fuer die Frage "welche Spalte ist unsere Nummer?".  Es filtert niemals
    #: Werte weg und erfindet nichts: passt nichts, bleibt alles so, wie es im
    #: Angebot steht, und wird als unsicher gekennzeichnet.
    own_material_pattern: str = r"^\d{6,18}$"
    #: Inhaltsbasierte Erkennung ueberhaupt verwenden.  Auf ``False`` zaehlt
    #: allein die Spaltenueberschrift (Verhalten wie ohne Nummernkreiswissen).
    own_material_pattern_active: bool = True
    #: Vertauschte Artikelnummern erkennen und -- deutlich sichtbar -- tauschen.
    #: Steht in "Material" eine Nummer, die nicht auf ``own_material_pattern``
    #: passt, und in "Lieferantenmaterial" eine, die passt, wurden die beiden
    #: Spalten offensichtlich verwechselt.  Getauscht wird nur mit Notiz und
    #: mit ``FieldOrigin.UNCERTAIN`` -- niemals stillschweigend.
    swap_detection: bool = True
    #: Ab welchem Anteil passender Werte gilt eine Spalte als "unsere Nummer"
    own_material_min_ratio: float = 0.7

    #: Zeilen mit weniger als so vielen erkannten Feldern werden verworfen
    min_fields_per_position: int = 2
    #: Freitextzeilen in E-Mails/PDFs auswerten (Positionstabelle nicht erkannt)
    enable_freetext_positions: bool = True
    #: Anhaenge einer E-Mail mitverarbeiten
    process_email_attachments: bool = True
    #: Dateiendungen, die als Angebotsanhang gelten
    #: Was als Angebotsanhang gilt.  Muss zu den Lesern passen -- steht ein
    #: Format hier nicht drin, wird der Anhang stillschweigend ignoriert,
    #: obwohl die Anwendung ihn lesen koennte.
    attachment_extensions: list[str] = field(
        default_factory=lambda: [
            ".pdf", ".xlsx", ".xlsm", ".xltx", ".xls", ".csv", ".tsv",
            ".docx", ".dotx", ".docm", ".odt", ".ott", ".ods", ".ots",
            ".rtf", ".zip", ".txt", ".html", ".htm",
            # Weitergeleitete Mail mit angehaengter Mail -- kommt vor
            ".eml", ".msg",
            # Bilder: nur sinnvoll mit OCR, werden sonst sauber abgelehnt
            ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
        ]
    )
    #: Maximale Anhangsgroesse in MB
    max_attachment_mb: int = 25
    #: Signatur-/Disclaimer-Bloecke in Mails abschneiden
    strip_email_signature: bool = True

    # -- Mail und Anhang gehoeren zusammen ------------------------------
    #: Haeufigster Fall im Alltag: Die Preistabelle steckt im Anhang, die
    #: Ergaenzungen ("gilt ab 01.09.", "Mindestmenge jetzt 500", "Position 30
    #: entfaellt") stehen im Mailtext.  Beides ergibt EIN Angebot.
    merge_email_and_attachment: bool = True
    #: Kopfdaten: Der Mailtext ist meist aktueller als ein mitgeschicktes
    #: Standard-Preisblatt -- bei Widerspruch gewinnt die Mail, der
    #: abweichende Wert wird aber als Hinweis protokolliert.
    email_header_wins_over_attachment: bool = True
    #: Aussagen im Mailtext auf Positionen des Anhangs anwenden
    #: (Preis, Gueltigkeit, Mindestmenge, Lieferzeit, Streichung)
    apply_email_supplements: bool = True
    #: Widersprechen sich Mailtext und Anhang beim Preis, wird der Wert
    #: als unsicher markiert statt einer der beiden Quellen zu glauben
    conflict_marks_uncertain: bool = True
    #: Saetze des Mailtexts, die eine Materialnummer, einen Preis, eine Menge
    #: oder ein Datum nennen, aber zu KEINER Uebernahme gefuehrt haben, als
    #: Warnung ausweisen.
    #:
    #: Das ist die gefaehrlichste Stelle des ganzen Verfahrens: eine Aussage
    #: des Lieferanten, die niemand bemerkt, weil sie stillschweigend unter den
    #: Tisch fiel.  Lieber ein Hinweis zu viel als eine uebergangene Zusage.
    warn_on_unmatched_email_statements: bool = True

    # -- PDF-Layout ------------------------------------------------------
    #: Gezeichnete Tabellenlinien (Vektorgrafik) als Spaltengrenzen verwenden.
    #: Sind Linien vorhanden, sind sie die verlaesslichste Grenze ueberhaupt --
    #: fehlen sie, wird auf das Koordinaten-Clustering zurueckgefallen.
    pdf_use_lattice: bool = True
    #: So viele brauchbare senkrechte Linien muss eine Seite mindestens haben,
    #: damit das Linienverfahren greift
    pdf_min_vertical_lines: int = 3
    #: Anteil der Zeilenhoehe, bis zu dem Woerter noch als eine Zeile gelten.
    #: Ein Lieferant mit sehr engem oder sehr weitem Zeilenabstand kann das im
    #: Profil ueberschreiben (``table_hints["y_tolerance_factor"]``).
    pdf_y_tolerance_factor: float = 0.6
    #: Rasterbreite (PDF-Punkte) fuer die Haeufigkeitsanalyse der Spaltenstarts.
    #: Profil-Ueberschreibung: ``table_hints["x_bin"]``.
    pdf_x_bin: float = 4.0


@dataclass
class OcrSettings:
    """Texterkennung fuer eingescannte Belege.

    OCR ist die einzige Stelle der Anwendung, an der aus einem Bild ein Wert
    wird -- und damit die heikelste.  Eine Erkennung liefert *immer* etwas,
    auch Unsinn; eine ``8`` statt einer ``3`` im Preis ist teurer als eine gar
    nicht erkannte Position.  Deshalb bleibt alles, was aus OCR stammt,
    durchgaengig als unsicher gekennzeichnet, und es wird nichts "korrigiert":
    ein verdaechtiges ``O`` wird gemeldet, nicht stillschweigend zur ``0``.

    Beide Engines sind optionale Zusatzpakete.  Fehlen sie, aendert sich am
    Verhalten der Anwendung nichts -- der Scan wird wie bisher gemeldet, nur
    zusaetzlich mit dem Hinweis, wie sich das nachruesten laesst.
    """

    #: Texterkennung ueberhaupt verwenden, wenn eine Engine vorhanden ist
    enabled: bool = True
    #: Bei erkanntem Scan erst fragen.  OCR dauert (Sekunden je Seite), und
    #: ungefragt Rechenzeit zu verbrauchen ist keine gute Bedienung.
    ask_before_ocr: bool = True
    #: Reihenfolge, in der die Engines probiert werden.
    #: "windows" = Bordmittel (keine Installation, aber keine Konfidenzwerte),
    #: "tesseract" = genauer und mit Konfidenz, braucht aber ein Programm.
    backend_order: list[str] = field(default_factory=lambda: ["windows", "tesseract"])
    #: Erkennungssprache (Kuerzel der Anwendung, z. B. "de")
    language: str = "de"
    #: Aufloesung, mit der PDF-Seiten zum Bild gerendert werden.
    #: Unter 200 dpi wird OCR unbrauchbar, ueber 400 dpi nur noch langsamer.
    dpi: int = 300
    #: Ab hier gilt ein Wort als unsicher und wird gesondert ausgewiesen
    min_confidence: float = 0.60
    #: Schutz gegen 200-Seiten-Scans -- so viele Seiten werden hoechstens erkannt
    max_pages: int = 20
    #: Graustufen/Kontrast vor der Erkennung.  Hilft bei Fotos deutlich.
    preprocess: bool = True


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
    #: Stammdaten (kein Belegvorgang): Lieferantenstamm pflegen (XK02).
    #: Gilt EINMAL je Lieferant, nicht je Position -- sonst wuerde derselbe
    #: Stammsatz n-mal angefasst.  Deshalb standardmaessig aus.
    chain_vendor_master: bool = False

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

    # -- Bestehende Kontrakte weiterverwenden ---------------------------
    #: Vor der Neuanlage pruefen, ob es zu Lieferant/EKorg schon einen
    #: laufenden Mengenkontrakt gibt, und diesen erweitern (ME32K) statt
    #: einen zweiten anzulegen.  Sonst wachsen die Belegnummern endlos.
    contract_reuse_existing: bool = True
    #: Nur Kontrakte beruecksichtigen, die noch mindestens so viele Tage
    #: laufen (ein morgen auslaufender Kontrakt hilft niemandem)
    contract_min_remaining_days: int = 30
    #: Belegart, nach der gesucht wird (leer = wie contract_document_type)
    contract_search_document_type: str = ""

    # -- Doppelte Belege -------------------------------------------------
    #: Keine zweite Bestellung zu einer Angebotsnummer, zu der dieses
    #: Werkzeug schon eine geschrieben hat.  Grundlage ist die eigene
    #: Historie, nicht SAP -- von Hand in SAP angelegte Bestellungen sieht
    #: das Werkzeug also nicht.  Wer bewusst nachbestellen will, macht das
    #: in SAP oder schaltet die Sperre hier ab.
    block_repeated_documents: bool = True

    # -- Mengenstaffeln im Infosatz -------------------------------------
    #: Erkannte Staffelpreise als Konditionsstaffel schreiben statt nur den
    #: ersten Preis zu uebernehmen
    info_record_write_scales: bool = True
    #: Mehr Staffelstufen als hier zugelassen werden abgeschnitten (mit
    #: Protokolleintrag -- stillschweigend verschwinden darf nichts)
    max_scale_levels: int = 6


@dataclass
class AttachmentSettings:
    """Das Angebotsdokument als Anlage am erzeugten SAP-Objekt.

    Warum das wichtig ist: Ein halbes Jahr spaeter fragt jemand "warum steht
    hier dieser Preis?".  Steht das Angebot als Anlage am Infosatz bzw. am
    Beleg, ist die Frage in zehn Sekunden beantwortet -- sonst beginnt die
    Suche im Mailpostfach.

    SAP haengt Dokumente ueber GOS an ("Dienste zum Objekt" ->
    "Anlage erstellen").  Der dabei aufgehende Dateiauswahl-Dialog ist ein
    WINDOWS-Fenster und kein SAP-GUI-Objekt; er laesst sich per GUI-Scripting
    nicht zuverlaessig bedienen.  Deshalb meldet die Anwendung in diesem Fall
    keinen Erfolg, sondern nennt den vollstaendigen Dateipfad zum manuellen
    Anhaengen (siehe ``app.sap.attachment_service``).
    """

    #: Je Aktion: soll das Angebot am erzeugten Objekt haengen?
    attach_to_info_record: bool = True
    #: Orderbuch traegt in vielen Systemen ueberhaupt keine Anlagen --
    #: deshalb standardmaessig aus.
    attach_to_source_list: bool = False
    attach_to_contract: bool = True
    attach_to_purchase_order: bool = True
    #: Lieferantenstamm ist eine Stammdatenpflege je Lieferant, kein
    #: Belegvorgang -- Anlage dort nur auf ausdruecklichen Wunsch.
    attach_to_vendor: bool = False

    #: Die Originaldatei (Angebots-PDF, Preisliste, E-Mail-Anhang) anhaengen
    attach_original_file: bool = True
    #: Zusaetzlich eine kurze Textzusammenfassung der uebernommenen Werte
    attach_summary: bool = False
    #: Groessere Dateien werden abgelehnt (SAP-Ablage, Archivkosten)
    max_attachment_mb: int = 10


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
    #: Mehrere Angebote nacheinander abarbeiten: nach dem Abschluss eines
    #: Angebots automatisch das naechste aus der Liste oeffnen
    auto_advance_queue: bool = True
    #: Beim Oeffnen mehrerer Dateien fragen, ob sie als Stapel oder als ein
    #: gemeinsames Angebot behandelt werden sollen
    ask_batch_or_merge: bool = True
    #: Standardhaken je Position
    default_do_info_record: bool = True
    default_do_source_list: bool = False
    default_do_contract: bool = False
    default_do_purchase_order: bool = False


@dataclass
class Settings:
    """Gesamtkonfiguration der Anwendung."""

    purchasing: PurchasingDefaults = field(default_factory=PurchasingDefaults)
    conditions: ConditionTypes = field(default_factory=ConditionTypes)
    currency: CurrencySettings = field(default_factory=CurrencySettings)
    thresholds: Thresholds = field(default_factory=Thresholds)
    transactions: Transactions = field(default_factory=Transactions)
    sap: SapRuntime = field(default_factory=SapRuntime)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    workflow: WorkflowDefaults = field(default_factory=WorkflowDefaults)
    attachments: AttachmentSettings = field(default_factory=AttachmentSettings)
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
    def unrecognized_dir(self) -> Path:
        """Ablage fuer Angebote, aus denen nichts erkannt wurde.

        Der Arbeits-PC hat keinen Zugang nach draussen.  Damit ein
        Fehlschlag trotzdem etwas Verwertbares hinterlaesst, wird die Datei
        samt Erkennungsprotokoll hier abgelegt -- der Ordner laesst sich
        gesammelt weitergeben, und die Erkennung kann gezielt nachgehaertet
        werden.
        """
        return self.home / "nicht_erkannt"

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
