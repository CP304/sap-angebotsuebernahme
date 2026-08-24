"""SAP-Feldnamen in Klartext uebersetzen.

Wozu
----
Eine Aufzeichnung liefert Feld-IDs wie ``EINA-LIFNR`` oder
``MEPO1211-EMATN``.  Wer nicht taeglich in der SAP-Datenbank arbeitet,
liest daraus nichts -- und muss dann raten, welches Feld gemeint ist,
oder mit auffaelligen Testwerten herumprobieren, bis er es weiss.

Das ist unnoetig: die Namen sind nicht kryptisch, sondern nur
abgekuerzt, und sie sind ueber alle SAP-Anlagen hinweg dieselben.
``EINA-LIFNR`` ist die Lieferantennummer in den Grunddaten des
Einkaufsinfosatzes -- immer und ueberall.

Diese Uebersetzung ist reine Lesehilfe.  Sie entscheidet nichts: die
Zuordnung eines Feldes bestaetigt weiterhin der Anwender, und wo hier
nichts steht, wird auch nichts behauptet.
"""

from __future__ import annotations

import re

__all__ = ["beschreibe_feld", "erklaere_feldname", "erklaere_tabelle",
           "FELDNAMEN", "TABELLEN"]

#: Die Datenbanktabelle oder Bildstruktur vor dem Bindestrich.
TABELLEN: dict[str, str] = {
    "EINA": "Einkaufsinfosatz, Grunddaten",
    "EINE": "Einkaufsinfosatz, Daten der Einkaufsorganisation",
    "EORD": "Orderbuch",
    "EKKO": "Einkaufsbeleg, Kopf",
    "EKPO": "Einkaufsbeleg, Position",
    "EKET": "Einkaufsbeleg, Einteilung",
    "KONH": "Konditionen, Kopf",
    "KONP": "Konditionen, Position",
    "LFA1": "Lieferantenstamm, allgemein",
    "LFM1": "Lieferantenstamm, Einkaufsdaten",
    "MARA": "Materialstamm, allgemein",
    "MAKT": "Materialstamm, Kurztexte",
    "MARC": "Materialstamm, Werksdaten",
    "RM06E": "Eingabebild Einkauf",
    "RM06I": "Eingabebild Infosatz",
    "MEPO": "Eingabebild Bestellung",
    "MEPO1211": "Bestellung, Positionstabelle",
    "MEPO1317": "Bestellung, Positionsdetail",
    "MEPOHEADER": "Bestellung, Kopfdaten",
    "MEPOITEM": "Bestellung, Positionsdaten",
    "MEPOACCOUNTING": "Bestellung, Kontierung",
    "RM06P": "Eingabebild Orderbuch",
    "T001W": "Werk, Stammdaten",
}

#: Der Feldname hinter dem Bindestrich.  Bewusst nach Sachgebieten
#: geordnet, damit Luecken beim Nachtragen auffallen.
FELDNAMEN: dict[str, str] = {
    # -- Wer und was ---------------------------------------------------
    "LIFNR": "Lieferantennummer (Kreditor)",
    "ELIFN": "Lieferantennummer",
    "FLIEF": "Fertigungslieferant",
    "MATNR": "Materialnummer",
    "EMATN": "Materialnummer",
    "IDNLF": "Materialnummer des Lieferanten",
    "MATKL": "Warengruppe",
    "TXZ01": "Kurztext der Position",
    "MAKTX": "Materialkurztext",
    "INFNR": "Nummer des Einkaufsinfosatzes",
    "ESOKZ": "Infosatzart (Normal, Lohnbearbeitung, Konsignation)",
    "LTSNR": "Lieferantenteilsortiment",
    # -- Organisation --------------------------------------------------
    "EKORG": "Einkaufsorganisation",
    "EKGRP": "Einkaeufergruppe",
    "WERKS": "Werk (Standort)",
    "LGORT": "Lagerort",
    "BUKRS": "Buchungskreis",
    "RESWK": "Lieferndes Werk",
    # -- Mengen und Einheiten ------------------------------------------
    "MENGE": "Menge",
    "KTMNG": "Zielmenge",
    "MEINS": "Basismengeneinheit",
    "BPRME": "Bestellpreismengeneinheit",
    "LMEIN": "Lagermengeneinheit",
    "MINBM": "Mindestbestellmenge",
    "NORBM": "Normalbestellmenge",
    "UEBTO": "Ueberlieferungstoleranz in Prozent",
    "UNTTO": "Unterlieferungstoleranz in Prozent",
    # -- Preis ---------------------------------------------------------
    "NETPR": "Nettopreis",
    "EFFPR": "Effektivpreis",
    "BRTWR": "Bruttowert",
    "NETWR": "Nettowert",
    "KBETR": "Konditionsbetrag",
    "KSCHL": "Konditionsart",
    "PEINH": "Preiseinheit (Preis gilt je ... Einheiten)",
    "KPEIN": "Preiseinheit der Kondition",
    "WAERS": "Waehrung",
    "KTWRT": "Zielwert des Kontrakts",
    "MWSKZ": "Steuerkennzeichen",
    # -- Termine und Gueltigkeit ---------------------------------------
    "DATAB": "Gueltig ab",
    "DATBI": "Gueltig bis",
    "KDATB": "Gueltigkeitsbeginn",
    "KDATE": "Gueltigkeitsende",
    "EINDT": "Liefertermin",
    "EEIND": "Liefertermin",
    "APLFZ": "Planlieferzeit in Tagen",
    "BEDAT": "Belegdatum",
    "ANGDT": "Angebotsfrist ab",
    "BNDDT": "Bindefrist bis",
    # -- Beleg und Kontierung ------------------------------------------
    "BSART": "Belegart",
    "EVRTN": "Nummer des Rahmenvertrags (Kontrakt)",
    "EVRTP": "Position des Rahmenvertrags",
    "EBELN": "Belegnummer",
    "EBELP": "Belegposition",
    "KNTTP": "Kontierungstyp",
    "KOSTL": "Kostenstelle",
    "SAKTO": "Sachkonto",
    "AUFNR": "Auftragsnummer",
    "PSPNR": "PSP-Element",
    "PSTYP": "Positionstyp",
    # -- Infosatzart und Konditionen -----------------------------------
    "NORMB": "Infosatzart: Normal",
    "LOHNB": "Infosatzart: Lohnbearbeitung",
    "KONSI": "Infosatzart: Konsignation",
    "PIPEL": "Infosatzart: Pipeline",
    "KOEIN": "Waehrung der Kondition",
    "KSTBM": "Staffelmenge (ab dieser Menge gilt der Preis)",
    "KSTBW": "Staffelwert",
    "NETNO": "Kennzeichen: Preis ist ein Nettopreis",
    "SKTOF": "Kennzeichen: kein Skonto",
    # -- Lieferbedingungen und Zahlung ---------------------------------
    "INCO1": "Lieferbedingung (Incoterm, z. B. FCA)",
    "INCO2": "Ort zur Lieferbedingung",
    "ZTERM": "Zahlungsbedingung",
    "IHREZ": "Ihre Referenz (beim Lieferanten)",
    "UNSEZ": "Unsere Referenz",
    "MAHN1": "Erste Mahnung nach ... Tagen",
    "MAHN2": "Zweite Mahnung nach ... Tagen",
    "MAHN3": "Dritte Mahnung nach ... Tagen",
    # -- Herkunft und Suche --------------------------------------------
    "URZLA": "Ursprungsland",
    "SORTL": "Sortierbegriff",
    "SORT1": "Suchbegriff",
    "VEDAT": "Belegdatum des Vertrags",
    "VDATU": "Gueltig ab",
    "BDATU": "Gueltig bis",
    "FLIFN": "Kennzeichen: fester Lieferant",
    # -- Beleg- und Kontraktbezug --------------------------------------
    "KONNR": "Nummer des Rahmenvertrags",
    "KTPNR": "Position des Rahmenvertrags",
    "ANLN1": "Anlagennummer",
    "NACHA": "Nachrichtenmedium (Druck, E-Mail, EDI)",
    "SUPERFIELD": "Sammelfeld der Kopfdaten (hier: Lieferant)",
    # -- Lieferantenstamm ----------------------------------------------
    "KTOKK": "Kontengruppe des Lieferanten",
    "NAME1": "Name des Lieferanten",
    "NAME2": "Namenszusatz",
    "STREET": "Strasse",
    "POST_CODE1": "Postleitzahl",
    "CITY1": "Ort (Stadt)",
    "COUNTRY": "Laenderschluessel",
    "REGION": "Region (Bundesland)",
    "LANGU": "Sprachenschluessel",
    "TEL_NUMBER": "Telefonnummer",
    "SMTP_ADDR": "E-Mail-Adresse",
    "STCD1": "Steuernummer",
    "STCEG": "Umsatzsteuer-Identifikationsnummer",
    "SPERM": "Kennzeichen: Lieferant gesperrt",
    "D0110": "Registerkarte Adresse",
    # -- Bedienelemente ohne Dateninhalt --------------------------------
    "OPTION1": "Schaltflaeche im Abfragefenster (Ja)",
    "OPTION2": "Schaltflaeche im Abfragefenster (Nein)",
    # -- Kennzeichen ---------------------------------------------------
    "LOEKZ": "Loeschkennzeichen",
    "AUTET": "Kennzeichen automatische Einteilung",
    "NOTKZ": "Sperrkennzeichen",
    "RELIF": "Kennzeichen fester Lieferant",
    "VRTYP": "Vertragstyp",
}

#: Steuerungspraefixe eines Bedienelements -- sie sagen nur, ob es ein
#: Textfeld, ein Ankreuzfeld oder eine Schaltflaeche ist.
_PRAEFIXE = ("ctxt", "txt", "cmbx", "cmb", "chk", "rad", "lbl", "btn",
             "tbl", "tabs", "tabp", "ssub", "sub")


def _kern(bezeichner: str) -> str:
    """Steuerungspraefix und Tabellenindex abstreifen."""
    rest = bezeichner.split("/")[-1]
    for praefix in _PRAEFIXE:
        if rest.lower().startswith(praefix):
            rest = rest[len(praefix):]
            break
    return re.sub(r"\[[^\]]*\]$", "", rest)


def erklaere_feldname(name: str) -> str:
    """Was bedeutet der Feldname hinter dem Bindestrich?"""
    return FELDNAMEN.get(name.upper(), "")


def erklaere_tabelle(name: str) -> str:
    """Woher stammt das Feld -- welche Tabelle oder welches Bild?"""
    gross = name.upper()
    if gross in TABELLEN:
        return TABELLEN[gross]
    # Bildstrukturen tragen eine angehaengte Bildnummer ("MEPO1211").
    ohne_nummer = re.sub(r"\d+$", "", gross)
    return TABELLEN.get(ohne_nummer, "")


def beschreibe_feld(field_id: str) -> str:
    """Eine Feld-ID in einen Satz uebersetzen, den man lesen kann.

    ``wnd[0]/usr/ctxtEINA-LIFNR`` wird zu
    ``"Lieferantennummer (Kreditor) -- Einkaufsinfosatz, Grunddaten"``.

    Ist der Feldname unbekannt, wird wenigstens gesagt, woher er stammt;
    ist auch das unbekannt, bleibt die Antwort leer.  Geraten wird nicht:
    eine erfundene Bedeutung waere schlimmer als gar keine, denn sie
    wuerde ungeprueft bestaetigt.
    """
    kern = _kern(field_id or "")
    if "-" in kern:
        tabelle, _, feld = kern.partition("-")
    else:
        tabelle, feld = "", kern
    bedeutung = erklaere_feldname(feld)
    herkunft = erklaere_tabelle(tabelle) if tabelle else ""
    if bedeutung and herkunft:
        return f"{bedeutung} -- {herkunft}"
    if bedeutung:
        return bedeutung
    if herkunft:
        return f"unbekanntes Feld aus: {herkunft}"
    return ""
