"""Zentrale Verwaltung aller SAP-GUI-Element-IDs.

WARUM DIESE DATEI EXISTIERT
===========================
SAP-Masken sind kundenspezifisch.  Feld-IDs wie
``wnd[0]/usr/ctxtEINA-LIFNR`` haengen von Release, Customizing, Bildsequenz
und aktivierten Zusatzfeldern ab.  Deshalb steht *keine* dieser IDs im
Programmcode -- alles liegt hier bzw. in der daraus erzeugten JSON-Datei
(``%APPDATA%/SAP-Angebotsuebernahme/sap_selectors.json``) und ist ueber die
GUI-Seite "SAP-Feld-IDs" editierbar.

DAS WICHTIGSTE SICHERHEITSKONZEPT DIESER ANWENDUNG
==================================================
Jeder Selektor traegt ein Kennzeichen ``verified``.

* ``verified = True``  -- vom Anwender am echten System mit dem SAP GUI Script
  Recorder geprueft (oder Teil des SAP-GUI-Frameworks und damit systemweit
  identisch, z. B. ``wnd[0]/tbar[0]/okcd``).
* ``verified = False`` -- **Vorschlag**.  Er entspricht der ueblichen Notation,
  ist aber NICHT geprueft.

Solange fuer einen Vorgang auch nur ein benoetigter Selektor ungeprueft ist,
verweigert die Anwendung im Echtbetrieb jede *schreibende* Aktion.  Lesen und
Dry Run bleiben erlaubt.  Damit kann das Werkzeug nie "auf Verdacht" in ein
Produktivsystem schreiben.

SO UEBERNEHMEN SIE IHRE EIGENEN IDs
===================================
1. In SAP: Alt+F12 -> "Skript-Aufzeichnung und -Wiedergabe" -> Aufzeichnen.
2. Vorgang einmal von Hand durchklicken (z. B. ME12).
3. Aufgezeichnete ``.vbs`` oeffnen -- dort stehen die echten IDs.
4. In dieser Anwendung: Einstellungen -> "SAP-Feld-IDs" -> ID eintragen,
   Haken "geprueft" setzen.  Alternativ direkt die JSON-Datei bearbeiten.
   Die Seite bietet zusaetzlich "VBS-Aufzeichnung importieren" an.

Alle noch offenen Punkte sind im Code und in der JSON mit ``TODO`` markiert.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Kennzeichnung im Beschreibungstext, solange eine ID nicht geprueft ist
TODO_MARKER = "TODO: kundenspezifische SAP-GUI-ID pruefen"


@dataclass
class Selector:
    """Ein einzelnes SAP-GUI-Element."""

    id: str = ""
    description: str = ""
    kind: str = "text"          # text|button|checkbox|combo|tab|table|grid|label|window
    optional: bool = False      # fehlt dieses Element, ist das kein Fehler
    verified: bool = False      # am echten System bestaetigt?

    @property
    def is_usable(self) -> bool:
        return bool(self.id)

    @property
    def needs_check(self) -> bool:
        return bool(self.id) and not self.verified and not self.optional

    def todo_text(self) -> str:
        return f"{TODO_MARKER}: {self.description} (Vorschlag: {self.id or '-'})"


@dataclass
class Screen:
    """Eine Maske/Transaktion mit ihren Elementen."""

    key: str
    title: str
    transaction: str = ""
    note: str = ""
    elements: dict[str, Selector] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Framework-Selektoren: systemweit identisch, daher geprueft
# ---------------------------------------------------------------------------

def _framework() -> dict[str, Selector]:
    """IDs, die zum SAP-GUI-Rahmen gehoeren und nicht kundenspezifisch sind."""
    return {
        "ok_code": Selector("wnd[0]/tbar[0]/okcd", "Kommandofeld (Transaktionseingabe)",
                            "text", verified=True),
        "enter": Selector("wnd[0]/tbar[0]/btn[0]", "Enter", "button", verified=True),
        "save": Selector("wnd[0]/tbar[0]/btn[11]", "Sichern (F11)", "button", verified=True),
        "back": Selector("wnd[0]/tbar[0]/btn[3]", "Zurueck (F3)", "button", verified=True),
        "exit": Selector("wnd[0]/tbar[0]/btn[15]", "Beenden (F15)", "button", verified=True),
        "cancel": Selector("wnd[0]/tbar[0]/btn[12]", "Abbrechen (F12)", "button", verified=True),
        "status_bar": Selector("wnd[0]/sbar", "Statusleiste", "label", verified=True),
        "status_text": Selector("wnd[0]/sbar/pane[0]", "Statusleistentext", "label",
                                optional=True, verified=True),
        "main_window": Selector("wnd[0]", "Hauptfenster", "window", verified=True),
        "popup": Selector("wnd[1]", "Modales Popup", "window", verified=True),
        "popup_text": Selector("wnd[1]/usr/txtMESSTXT1", "Popup-Meldungstext", "label",
                               optional=True, verified=False),
        "popup_yes": Selector("wnd[1]/usr/btnSPOP-OPTION1", "Popup: Ja", "button",
                              optional=True, verified=False),
        "popup_no": Selector("wnd[1]/usr/btnSPOP-OPTION2", "Popup: Nein", "button",
                             optional=True, verified=False),
        "popup_enter": Selector("wnd[1]/tbar[0]/btn[0]", "Popup: Enter", "button",
                                optional=True, verified=True),
        "popup_cancel": Selector("wnd[1]/tbar[0]/btn[12]", "Popup: Abbrechen", "button",
                                 optional=True, verified=True),
    }


# ---------------------------------------------------------------------------
# Vorschlaege je Transaktion -- ALLE ungeprueft (verified=False)
# ---------------------------------------------------------------------------

def _s(sid: str, desc: str, kind: str = "text", optional: bool = False) -> Selector:
    """Ungeprueften Vorschlag erzeugen."""
    return Selector(id=sid, description=desc, kind=kind, optional=optional, verified=False)


def default_screens() -> dict[str, Screen]:
    """Auslieferungszustand der Selektor-Registry.

    Die angegebenen IDs sind *Vorschlaege* in der ueblichen SAP-Notation.  Sie
    ersparen Tipparbeit, sind aber ausdruecklich zu pruefen.
    """
    screens: dict[str, Screen] = {}

    screens["common"] = Screen(
        key="common", title="Allgemein (SAP-GUI-Rahmen)",
        note="Diese IDs gehoeren zum SAP-GUI und sind systemunabhaengig.",
        elements=_framework(),
    )

    # -- Infosatz: Einstiegsbild ME11/ME12/ME13 --------------------------
    screens["info_record_initial"] = Screen(
        key="info_record_initial", title="Infosatz – Einstiegsbild",
        transaction="ME11/ME12/ME13",
        note="Einstieg mit Lieferant, Material, EKorg, Werk und Infosatzart.",
        elements={
            "vendor": _s("wnd[0]/usr/ctxtEINA-LIFNR", "Lieferant"),
            "material": _s("wnd[0]/usr/ctxtEINA-MATNR", "Material"),
            "purchasing_org": _s("wnd[0]/usr/ctxtEINE-EKORG", "Einkaufsorganisation"),
            "plant": _s("wnd[0]/usr/ctxtEINE-WERKS", "Werk", optional=True),
            "info_record_number": _s("wnd[0]/usr/ctxtEINA-INFNR", "Infosatznummer",
                                     optional=True),
            "category_standard": _s("wnd[0]/usr/radRM06I-NORMB", "Infosatzart: Normal",
                                    "radio", optional=True),
            "category_subcontracting": _s("wnd[0]/usr/radRM06I-LOHNB",
                                          "Infosatzart: Lohnbearbeitung", "radio",
                                          optional=True),
            "category_consignment": _s("wnd[0]/usr/radRM06I-KONSI",
                                       "Infosatzart: Konsignation", "radio", optional=True),
        },
    )

    # -- Infosatz: Allgemeine Daten --------------------------------------
    screens["info_record_general"] = Screen(
        key="info_record_general", title="Infosatz – Allgemeine Daten",
        transaction="ME11/ME12/ME13",
        elements={
            "vendor_material": _s("wnd[0]/usr/txtEINA-IDNLF", "Lieferantenmaterialnummer",
                                  optional=True),
            "vendor_material_text": _s("wnd[0]/usr/txtEINA-TXZ01", "Kurztext", optional=True),
            "reminder1": _s("wnd[0]/usr/txtEINA-MAHN1", "Mahnung 1", optional=True),
            "origin_country": _s("wnd[0]/usr/ctxtEINA-URZLA", "Ursprungsland", optional=True),
            "sort_term": _s("wnd[0]/usr/txtEINA-SORTL", "Sortierbegriff", optional=True),
        },
    )

    # -- Infosatz: Einkaufsorganisationsdaten ----------------------------
    screens["info_record_purchasing"] = Screen(
        key="info_record_purchasing", title="Infosatz – EKorg-Daten 1",
        transaction="ME11/ME12/ME13",
        note="Hier stehen Nettopreis, Preiseinheit, Bestellmengeneinheit und Lieferzeit.",
        elements={
            "net_price": _s("wnd[0]/usr/txtEINE-NETPR", "Nettopreis"),
            "currency": _s("wnd[0]/usr/ctxtEINE-WAERS", "Waehrung"),
            "price_unit": _s("wnd[0]/usr/txtEINE-PEINH", "Preiseinheit"),
            "order_unit": _s("wnd[0]/usr/ctxtEINE-BPRME", "Bestellmengeneinheit"),
            "lead_time": _s("wnd[0]/usr/txtEINE-APLFZ", "Planlieferzeit (Tage)", optional=True),
            "min_quantity": _s("wnd[0]/usr/txtEINE-MINBM", "Mindestbestellmenge",
                               optional=True),
            "standard_quantity": _s("wnd[0]/usr/txtEINE-NORBM", "Normalmenge", optional=True),
            "purchasing_group": _s("wnd[0]/usr/ctxtEINE-EKGRP", "Einkaeufergruppe",
                                   optional=True),
            "tax_code": _s("wnd[0]/usr/ctxtEINE-MWSKZ", "Steuerkennzeichen", optional=True),
            "incoterm": _s("wnd[0]/usr/ctxtEINE-INCO1", "Incoterm", optional=True),
            "incoterm_location": _s("wnd[0]/usr/txtEINE-INCO2", "Incoterm-Ort", optional=True),
            "net_price_flag": _s("wnd[0]/usr/chkEINE-NETNO", "Kennzeichen Nettopreis",
                                 "checkbox", optional=True),
            "no_cash_discount": _s("wnd[0]/usr/chkEINE-SKTOF", "Kein Skonto", "checkbox",
                                   optional=True),
            "conditions_button": _s("wnd[0]/tbar[1]/btn[6]", "Schaltflaeche Konditionen",
                                    "button", optional=True),
        },
    )

    # -- Infosatz: Konditionen -------------------------------------------
    screens["info_record_conditions"] = Screen(
        key="info_record_conditions", title="Infosatz – Konditionen",
        transaction="ME11/ME12/ME13",
        note=("Konditionspflege mit Gueltigkeitszeitraum.  Die Tabelle ist je nach "
              "Release ein Table-Control oder ein Grid -- bitte pruefen."),
        elements={
            "valid_from": _s("wnd[0]/usr/ctxtRV13A-DATAB", "Gueltig ab"),
            "valid_to": _s("wnd[0]/usr/ctxtRV13A-DATBI", "Gueltig bis"),
            "condition_table": _s("wnd[0]/usr/tblSAPMV13ATCTRL_A017", "Konditionstabelle",
                                  "table", optional=True),
            "condition_type_cell": _s("wnd[0]/usr/tblSAPMV13ATCTRL_A017/ctxtKOMV-KSCHL[0,%d]",
                                      "Konditionsart (Zeilenplatzhalter %d)", "table",
                                      optional=True),
            "condition_amount_cell": _s("wnd[0]/usr/tblSAPMV13ATCTRL_A017/txtKOMV-KBETR[1,%d]",
                                        "Konditionsbetrag (Zeilenplatzhalter %d)", "table",
                                        optional=True),
            "condition_unit_cell": _s("wnd[0]/usr/tblSAPMV13ATCTRL_A017/ctxtKOMV-KOEIN[2,%d]",
                                      "Konditionswaehrung (Zeilenplatzhalter %d)", "table",
                                      optional=True),
            "condition_per_cell": _s("wnd[0]/usr/tblSAPMV13ATCTRL_A017/txtKOMV-KPEIN[3,%d]",
                                     "Konditions-Preiseinheit (Zeilenplatzhalter %d)",
                                     "table", optional=True),
            "validity_periods_button": _s("wnd[0]/tbar[1]/btn[16]", "Gueltigkeitsperioden",
                                          "button", optional=True),
        },
    )

    # -- Orderbuch ME01 ---------------------------------------------------
    screens["source_list_initial"] = Screen(
        key="source_list_initial", title="Orderbuch – Einstiegsbild",
        transaction="ME01/ME03",
        elements={
            "material": _s("wnd[0]/usr/ctxtEORD-MATNR", "Material"),
            "plant": _s("wnd[0]/usr/ctxtEORD-WERKS", "Werk"),
        },
    )
    screens["source_list_overview"] = Screen(
        key="source_list_overview", title="Orderbuch – Positionsuebersicht",
        transaction="ME01",
        note=("Table-Control mit einer Zeile je Bezugsquelle.  ``%d`` ist der "
              "Zeilenindex (0-basiert, sichtbarer Bereich!)."),
        elements={
            "table": _s("wnd[0]/usr/tblSAPLMEORTC_EORD", "Orderbuchtabelle", "table"),
            "valid_from_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-VDATU[0,%d]",
                                  "Gueltig ab (Zeile %d)", "table"),
            "valid_to_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-BDATU[1,%d]",
                                "Gueltig bis (Zeile %d)", "table"),
            "vendor_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-LIFNR[2,%d]",
                              "Lieferant (Zeile %d)", "table"),
            "fixed_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/chkEORD-FLIFN[6,%d]",
                             "Kennzeichen fest (Zeile %d)", "table", optional=True),
            "blocked_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/chkEORD-NOTKZ[8,%d]",
                               "Kennzeichen gesperrt (Zeile %d)", "table", optional=True),
            "mrp_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-AUTET[9,%d]",
                           "Dispokennzeichen (Zeile %d)", "table", optional=True),
            "agreement_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-EBELN[4,%d]",
                                 "Vereinbarung/Kontrakt (Zeile %d)", "table", optional=True),
            "agreement_item_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-EBELP[5,%d]",
                                      "Vereinbarungsposition (Zeile %d)", "table",
                                      optional=True),
            "purchasing_org_cell": _s("wnd[0]/usr/tblSAPLMEORTC_EORD/ctxtEORD-EKORG[7,%d]",
                                      "Einkaufsorganisation (Zeile %d)", "table",
                                      optional=True),
        },
    )

    # -- Mengenkontrakt ME31K --------------------------------------------
    screens["contract_initial"] = Screen(
        key="contract_initial", title="Kontrakt – Einstiegsbild",
        transaction="ME31K/ME32K/ME33K",
        elements={
            "vendor": _s("wnd[0]/usr/ctxtRM06E-LIFNR", "Lieferant"),
            "document_type": _s("wnd[0]/usr/ctxtRM06E-BSART", "Belegart (MK/WK)"),
            "agreement_date": _s("wnd[0]/usr/ctxtRM06E-VEDAT", "Belegdatum", optional=True),
            "agreement_number": _s("wnd[0]/usr/ctxtRM06E-EVRTN", "Kontraktnummer",
                                   optional=True),
            "purchasing_org": _s("wnd[0]/usr/ctxtEKKO-EKORG", "Einkaufsorganisation"),
            "purchasing_group": _s("wnd[0]/usr/ctxtEKKO-EKGRP", "Einkaeufergruppe"),
            "plant": _s("wnd[0]/usr/ctxtRM06E-WERKS", "Werk", optional=True),
        },
    )
    screens["contract_header"] = Screen(
        key="contract_header", title="Kontrakt – Kopfdaten",
        transaction="ME31K/ME32K",
        elements={
            "valid_from": _s("wnd[0]/usr/ctxtEKKO-KDATB", "Laufzeitbeginn"),
            "valid_to": _s("wnd[0]/usr/ctxtEKKO-KDATE", "Laufzeitende"),
            "target_value": _s("wnd[0]/usr/txtEKKO-KTWRT", "Zielwert", optional=True),
            "currency": _s("wnd[0]/usr/ctxtEKKO-WAERS", "Waehrung", optional=True),
            "payment_terms": _s("wnd[0]/usr/ctxtEKKO-ZTERM", "Zahlungsbedingung",
                                optional=True),
            "incoterm": _s("wnd[0]/usr/ctxtEKKO-INCO1", "Incoterm", optional=True),
            "incoterm_location": _s("wnd[0]/usr/txtEKKO-INCO2", "Incoterm-Ort", optional=True),
            "your_reference": _s("wnd[0]/usr/txtEKKO-IHREZ", "Ihre Referenz (Angebotsnr.)",
                                 optional=True),
        },
    )
    screens["contract_items"] = Screen(
        key="contract_items", title="Kontrakt – Positionsuebersicht",
        transaction="ME31K/ME32K",
        note="Table-Control der Kontraktpositionen; ``%d`` = Zeilenindex.",
        elements={
            "table": _s("wnd[0]/usr/tblSAPMM06ETC_0120", "Positionstabelle", "table"),
            "material_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/ctxtEKPO-EMATN[3,%d]",
                                "Material (Zeile %d)", "table"),
            "target_qty_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/txtEKPO-KTMNG[4,%d]",
                                  "Zielmenge (Zeile %d)", "table"),
            "uom_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/ctxtEKPO-MEINS[5,%d]",
                           "Mengeneinheit (Zeile %d)", "table", optional=True),
            "net_price_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/txtEKPO-NETPR[6,%d]",
                                 "Nettopreis (Zeile %d)", "table"),
            "price_unit_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/txtEKPO-PEINH[7,%d]",
                                  "Preiseinheit (Zeile %d)", "table", optional=True),
            "plant_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/ctxtEKPO-WERKS[9,%d]",
                             "Werk (Zeile %d)", "table", optional=True),
            "short_text_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/txtEKPO-TXZ01[2,%d]",
                                  "Kurztext (Zeile %d)", "table", optional=True),
            "material_group_cell": _s("wnd[0]/usr/tblSAPMM06ETC_0120/ctxtEKPO-MATKL[8,%d]",
                                      "Warengruppe (Zeile %d)", "table", optional=True),
        },
    )

    # -- Bestellung ME21N -------------------------------------------------
    screens["purchase_order"] = Screen(
        key="purchase_order", title="Bestellung – Enjoy-Transaktion",
        transaction="ME21N/ME22N/ME23N",
        note=("ME21N ist eine Enjoy-Transaktion mit dynamischen IDs (Kopf/Position "
              "auf- und zugeklappt).  Diese IDs MUESSEN aufgezeichnet werden."),
        elements={
            "vendor": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0013/ctxtMEPO_TOPLINE-SUPERFIELD",
                         "Lieferant (Kopfzeile)"),
            "document_type": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0013/cmbMEPO_TOPLINE-BSART",
                                "Belegart", "combo", optional=True),
            "document_date": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0013/ctxtMEPO_TOPLINE-BEDAT",
                                "Belegdatum", optional=True),
            "header_tabstrip": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0014/tabsHEADER_DETAIL",
                                  "Kopf-Registerkarten", "tab", optional=True),
            "org_purchasing_org": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0014/tabsHEADER_DETAIL/"
                                     "tabpTABHDT9/ssubTABSTRIPCONTROL2SUB:SAPLMEGUI:1229/"
                                     "ctxtMEPO1229-EKORG", "Einkaufsorganisation"),
            "org_purchasing_group": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0014/tabsHEADER_DETAIL/"
                                       "tabpTABHDT9/ssubTABSTRIPCONTROL2SUB:SAPLMEGUI:1229/"
                                       "ctxtMEPO1229-EKGRP", "Einkaeufergruppe"),
            "org_company_code": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0014/tabsHEADER_DETAIL/"
                                   "tabpTABHDT9/ssubTABSTRIPCONTROL2SUB:SAPLMEGUI:1229/"
                                   "ctxtMEPO1229-BUKRS", "Buchungskreis", optional=True),
            "item_table": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/subSUB2:SAPLMEVIEWS:1100/"
                             "subSUB2:SAPLMEGUI:1211/tblSAPLMEGUITC_1211",
                             "Positionstabelle", "table"),
            "item_material_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/subSUB2:SAPLMEVIEWS:1100/"
                                     "subSUB2:SAPLMEGUI:1211/tblSAPLMEGUITC_1211/"
                                     "ctxtMEPO1211-EMATN[4,%d]", "Material (Zeile %d)", "table"),
            "item_quantity_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/subSUB2:SAPLMEVIEWS:1100/"
                                     "subSUB2:SAPLMEGUI:1211/tblSAPLMEGUITC_1211/"
                                     "txtMEPO1211-MENGE[6,%d]", "Menge (Zeile %d)", "table"),
            "item_uom_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/subSUB2:SAPLMEVIEWS:1100/"
                                "subSUB2:SAPLMEGUI:1211/tblSAPLMEGUITC_1211/"
                                "ctxtMEPO1211-MEINS[7,%d]", "Mengeneinheit (Zeile %d)",
                                "table", optional=True),
            "item_delivery_date_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/"
                                          "subSUB2:SAPLMEVIEWS:1100/subSUB2:SAPLMEGUI:1211/"
                                          "tblSAPLMEGUITC_1211/ctxtMEPO1211-EEIND[9,%d]",
                                          "Lieferdatum (Zeile %d)", "table", optional=True),
            "item_net_price_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/"
                                      "subSUB2:SAPLMEVIEWS:1100/subSUB2:SAPLMEGUI:1211/"
                                      "tblSAPLMEGUITC_1211/txtMEPO1211-NETPR[10,%d]",
                                      "Nettopreis (Zeile %d)", "table", optional=True),
            "item_plant_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/subSUB2:SAPLMEVIEWS:1100/"
                                  "subSUB2:SAPLMEGUI:1211/tblSAPLMEGUITC_1211/"
                                  "ctxtMEPO1211-WERKS[12,%d]", "Werk (Zeile %d)", "table",
                                  optional=True),
            "check_button": _s("wnd[0]/tbar[1]/btn[19]", "Pruefen (Beleg pruefen)",
                               "button", optional=True),
        },
    )

    # -- Bestellung mit Kontraktbezug -------------------------------------
    screens["purchase_order_reference"] = Screen(
        key="purchase_order_reference", title="Bestellung – Bezug zum Kontrakt",
        transaction="ME21N",
        note=("Weg ueber 'Beleguebersicht ein' -> Auswahlvariante 'Kontrakte' oder ueber "
              "Positionsfeld Vereinbarung (EKPO-KONNR).  Bitte den bei Ihnen ueblichen "
              "Weg aufzeichnen."),
        elements={
            "item_agreement_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/"
                                      "subSUB2:SAPLMEVIEWS:1100/subSUB2:SAPLMEGUI:1211/"
                                      "tblSAPLMEGUITC_1211/ctxtMEPO1211-KONNR[24,%d]",
                                      "Vereinbarung/Kontrakt (Zeile %d)", "table"),
            "item_agreement_item_cell": _s("wnd[0]/usr/subSUB0:SAPLMEGUI:0016/"
                                           "subSUB2:SAPLMEVIEWS:1100/subSUB2:SAPLMEGUI:1211/"
                                           "tblSAPLMEGUITC_1211/txtMEPO1211-KTPNR[25,%d]",
                                           "Kontraktposition (Zeile %d)", "table"),
            "document_overview_on": _s("wnd[0]/tbar[1]/btn[9]", "Beleguebersicht ein",
                                       "button", optional=True),
            "selection_variant": _s("wnd[0]/tbar[1]/btn[10]", "Auswahlvariante", "button",
                                    optional=True),
        },
    )

    # -- Nachrichten (Sicherheitsrelevant!) -------------------------------
    screens["messages"] = Screen(
        key="messages", title="Nachrichten (Belegausgabe) – MUSS geprueft werden",
        transaction="ME21N/ME31K",
        note=("SICHERHEIT: Diese Anwendung darf NIE eine Nachricht an den Lieferanten "
              "ausloesen.  Vor dem Sichern werden alle Nachrichtensaetze geloescht und "
              "die Tabelle wird als leer verifiziert.  Sind diese IDs nicht geprueft, "
              "wird im Echtbetrieb NICHT gesichert."),
        elements={
            "messages_menu": _s("wnd[0]/mbar/menu[0]/menu[10]", "Menue Kopf -> Nachrichten",
                                "menu", optional=True),
            "message_table": _s("wnd[0]/usr/tblSAPDV70ATC_NAST3", "Nachrichtentabelle",
                                "table"),
            "message_kind_cell": _s("wnd[0]/usr/tblSAPDV70ATC_NAST3/ctxtNAST-KSCHL[0,%d]",
                                    "Nachrichtenart (Zeile %d)", "table"),
            "message_medium_cell": _s("wnd[0]/usr/tblSAPDV70ATC_NAST3/ctxtNAST-NACHA[2,%d]",
                                      "Medium (Zeile %d)", "table", optional=True),
            "delete_row_button": _s("wnd[0]/usr/btnDELETE", "Zeile loeschen", "button",
                                    optional=True),
            "back_from_messages": _s("wnd[0]/tbar[0]/btn[3]", "Zurueck aus Nachrichten",
                                     "button", optional=True),
        },
    )

    # -- Stammdatenpruefung ------------------------------------------------
    screens["material_display"] = Screen(
        key="material_display", title="Material anzeigen (MM03)",
        transaction="MM03",
        elements={
            "material": _s("wnd[0]/usr/ctxtRMMG1-MATNR", "Material"),
            "select_views_button": _s("wnd[0]/tbar[1]/btn[9]", "Sichten auswaehlen",
                                      "button", optional=True),
            "description": _s("wnd[0]/usr/subSUB1:SAPLMGMM:2004/txtMAKT-MAKTX",
                              "Materialkurztext", optional=True),
            "base_unit": _s("wnd[0]/usr/subSUB1:SAPLMGMM:2004/ctxtMARA-MEINS",
                            "Basismengeneinheit", optional=True),
        },
    )
    screens["vendor_display"] = Screen(
        key="vendor_display", title="Lieferant anzeigen (XK03)",
        transaction="XK03",
        elements={
            "vendor": _s("wnd[0]/usr/ctxtRF02K-LIFNR", "Lieferant"),
            "company_code": _s("wnd[0]/usr/ctxtRF02K-BUKRS", "Buchungskreis", optional=True),
            "purchasing_org": _s("wnd[0]/usr/ctxtRF02K-EKORG", "Einkaufsorganisation",
                                 optional=True),
            "view_address": _s("wnd[0]/usr/chkRF02K-D0110", "Sicht Anschrift", "checkbox",
                               optional=True),
            "name": _s("wnd[0]/usr/subADDRESS:SAPLSZA1:0300/subCOUNTRY_SCREEN:SAPLSZA1:0301/"
                       "txtADDR1_DATA-NAME1", "Name des Lieferanten", optional=True),
        },
    )

    return screens


#: Welche Bildschirme fuer welchen Vorgang zwingend geprueft sein muessen
REQUIRED_SCREENS: dict[str, tuple[str, ...]] = {
    "info_record_read": ("info_record_initial", "info_record_purchasing"),
    "info_record_write": ("info_record_initial", "info_record_purchasing",
                          "info_record_conditions"),
    "source_list_read": ("source_list_initial", "source_list_overview"),
    "source_list_write": ("source_list_initial", "source_list_overview"),
    "contract_write": ("contract_initial", "contract_header", "contract_items", "messages"),
    "purchase_order_write": ("purchase_order", "messages"),
    "purchase_order_from_contract": ("purchase_order", "purchase_order_reference", "messages"),
    "material_read": ("material_display",),
    "vendor_read": ("vendor_display",),
}


class SelectorNotVerifiedError(RuntimeError):
    """Wird geworfen, wenn im Echtbetrieb ungeprueft geschrieben werden soll."""


class SelectorMissingError(KeyError):
    """Ein benoetigter Selektor ist gar nicht konfiguriert."""


class SelectorRegistry:
    """Laedt, speichert und liefert Selektoren."""

    def __init__(self, screens: dict[str, Screen] | None = None) -> None:
        self.screens: dict[str, Screen] = screens or default_screens()

    # -- Zugriff --------------------------------------------------------
    def get(self, screen: str, key: str) -> Selector:
        try:
            return self.screens[screen].elements[key]
        except KeyError as exc:
            raise SelectorMissingError(
                f"Selektor '{screen}.{key}' ist nicht konfiguriert. "
                f"Bitte in den Einstellungen unter 'SAP-Feld-IDs' ergaenzen."
            ) from exc

    def id_for(self, screen: str, key: str, row: int | None = None) -> str:
        """Konkrete Element-ID; ``row`` fuellt einen ``%d``-Platzhalter."""
        selector = self.get(screen, key)
        if not selector.id:
            raise SelectorMissingError(
                f"Fuer '{screen}.{key}' ({selector.description}) ist keine "
                f"SAP-GUI-ID hinterlegt."
            )
        if row is None:
            return selector.id
        if "%d" not in selector.id:
            return selector.id
        return selector.id % row

    def has(self, screen: str, key: str) -> bool:
        selector = self.screens.get(screen, Screen(screen, screen)).elements.get(key)
        return bool(selector and selector.id)

    # -- Pruefstatus ----------------------------------------------------
    def unverified(self, screen_keys: tuple[str, ...] | list[str]) -> list[tuple[str, str, Selector]]:
        """Alle nicht geprueften Pflicht-Selektoren der angegebenen Masken."""
        result: list[tuple[str, str, Selector]] = []
        for screen_key in screen_keys:
            screen = self.screens.get(screen_key)
            if screen is None:
                continue
            for key, selector in screen.elements.items():
                if selector.needs_check or (not selector.id and not selector.optional):
                    result.append((screen_key, key, selector))
        return result

    def is_ready_for(self, operation: str) -> bool:
        screens = REQUIRED_SCREENS.get(operation, ())
        return not self.unverified(screens)

    def ensure_ready(self, operation: str) -> None:
        """Wirft ``SelectorNotVerifiedError``, wenn etwas ungeprueft ist."""
        missing = self.unverified(REQUIRED_SCREENS.get(operation, ()))
        if not missing:
            return
        lines = [f"  - {s}.{k}: {sel.description} (Vorschlag: {sel.id or 'leer'})"
                 for s, k, sel in missing[:12]]
        more = "" if len(missing) <= 12 else f"\n  ... und {len(missing) - 12} weitere"
        raise SelectorNotVerifiedError(
            f"Fuer '{operation}' sind {len(missing)} SAP-GUI-IDs noch nicht geprueft.\n"
            f"{chr(10).join(lines)}{more}\n\n"
            f"Bitte in den Einstellungen unter 'SAP-Feld-IDs' pruefen und bestaetigen."
        )

    def verification_summary(self) -> dict[str, tuple[int, int]]:
        """Je Maske: (geprueft, gesamt-pflicht)."""
        summary: dict[str, tuple[int, int]] = {}
        for key, screen in self.screens.items():
            required = [s for s in screen.elements.values() if not s.optional]
            verified = [s for s in required if s.verified and s.id]
            summary[key] = (len(verified), len(required))
        return summary

    def mark_verified(self, screen: str, key: str, verified: bool = True) -> None:
        self.get(screen, key).verified = verified

    def set_id(self, screen: str, key: str, element_id: str) -> None:
        selector = self.get(screen, key)
        if selector.id != element_id:
            selector.id = element_id
            selector.verified = False   # geaenderte ID muss neu bestaetigt werden

    # -- Persistenz -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "_hinweis": ("Feld-IDs mit \"verified\": false sind ungeprueft. Solange sie "
                         "ungeprueft sind, schreibt die Anwendung nicht in SAP."),
            "screens": {
                key: {
                    "title": screen.title,
                    "transaction": screen.transaction,
                    "note": screen.note,
                    "elements": {k: asdict(v) for k, v in screen.elements.items()},
                }
                for key, screen in self.screens.items()
            },
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        logger.info("SAP-Selektoren gespeichert: %s", path)
        return path

    @classmethod
    def load(cls, path: Path) -> "SelectorRegistry":
        registry = cls()
        if not path.exists():
            registry.save(path)
            return registry
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Selektordatei unlesbar (%s): %s -- Standard wird verwendet", path, exc)
            return registry

        for screen_key, screen_data in (data.get("screens") or {}).items():
            screen = registry.screens.get(screen_key)
            if screen is None:
                screen = Screen(key=screen_key, title=screen_data.get("title", screen_key),
                                transaction=screen_data.get("transaction", ""),
                                note=screen_data.get("note", ""))
                registry.screens[screen_key] = screen
            for element_key, element in (screen_data.get("elements") or {}).items():
                if not isinstance(element, dict):
                    continue
                existing = screen.elements.get(element_key)
                selector = Selector(
                    id=str(element.get("id", "")),
                    description=str(element.get("description",
                                                existing.description if existing else "")),
                    kind=str(element.get("kind", existing.kind if existing else "text")),
                    optional=bool(element.get("optional",
                                              existing.optional if existing else False)),
                    verified=bool(element.get("verified", False)),
                )
                screen.elements[element_key] = selector
        logger.info("SAP-Selektoren geladen: %s", path)
        return registry

    # -- Import aus Recorder-Aufzeichnung --------------------------------
    def ids_from_vbs(self, vbs_text: str) -> list[str]:
        """Alle ``findById("...")``-IDs aus einer Recorder-Aufzeichnung ziehen.

        Damit kann der Anwender eine ``.vbs`` einlesen und die IDs per Klick
        den Feldern zuordnen, statt sie abzutippen.
        """
        pattern = re.compile(r'findById\(\s*"([^"]+)"\s*\)')
        seen: list[str] = []
        for match in pattern.finditer(vbs_text):
            element_id = match.group(1)
            if element_id not in seen:
                seen.append(element_id)
        return seen

    def suggest_mapping(self, vbs_ids: list[str]) -> dict[tuple[str, str], str]:
        """Aufgezeichnete IDs den konfigurierten Feldern zuordnen (Vorschlag).

        Zugeordnet wird ueber den technischen Feldnamen am Ende der ID
        (z. B. ``EINE-NETPR``), nicht ueber den kompletten Pfad -- der
        Praefix unterscheidet sich je nach Bildaufbau.
        """
        def tail(element_id: str) -> str:
            base = element_id.split("/")[-1]
            base = re.sub(r"\[[^\]]*\]$", "", base)
            return re.sub(r"^(ctxt|txt|chk|rad|cmb|btn|lbl|tbl|tabs|tabp|ssub|sub)", "",
                          base).upper()

        by_tail: dict[str, str] = {}
        for element_id in vbs_ids:
            by_tail.setdefault(tail(element_id), element_id)

        mapping: dict[tuple[str, str], str] = {}
        for screen_key, screen in self.screens.items():
            for element_key, selector in screen.elements.items():
                if not selector.id:
                    continue
                candidate = by_tail.get(tail(selector.id))
                if candidate and candidate != selector.id:
                    mapping[(screen_key, element_key)] = candidate
        return mapping
