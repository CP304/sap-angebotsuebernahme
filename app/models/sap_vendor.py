"""Abbild eines SAP-Lieferantenstammsatzes (XK01/XK02/XK03)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SapVendorRecord:
    """Ist-Zustand eines Lieferanten in SAP.

    ``exists=False`` bedeutet: In SAP wurde zur Lieferantennummer kein Satz
    gefunden.  ``read_at is None`` bedeutet dagegen: Es wurde noch gar nicht
    gelesen -- diese beiden Faelle duerfen nie vermischt werden (siehe
    :class:`~app.models.sap_info_record.SapInfoRecord`).
    """

    vendor_number: str = ""
    exists: bool = False

    # Adresse
    name: str = ""
    name2: str = ""              # Adresszusatz
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = ""
    region: str = ""
    language: str = ""
    search_term: str = ""
    telephone: str = ""
    email: str = ""

    # Einkaufsorganisationsdaten
    purchasing_org: str = ""
    currency: str = ""
    payment_terms: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    purchasing_group: str = ""

    # Status
    blocked: bool = False
    tax_number: str = ""
    vat_id: str = ""

    # Meta
    read_at: datetime | None = None
    read_error: str = ""

    @property
    def was_read(self) -> bool:
        return self.read_at is not None

    def summary(self) -> dict[str, str]:
        if not self.was_read:
            return {"Lieferant": "noch nicht gelesen"}
        if not self.exists:
            return {"Lieferant": "nicht vorhanden"}
        return {
            "Lieferant": self.vendor_number or "vorhanden",
            "Name": self.name or "-",
            "Adresszusatz": self.name2 or "-",
            "Strasse": self.street or "-",
            "PLZ/Ort": f"{self.postal_code} {self.city}".strip() or "-",
            "Land": self.country or "-",
            "Region": self.region or "-",
            "Sprache": self.language or "-",
            "Telefon": self.telephone or "-",
            "E-Mail": self.email or "-",
            "Einkaufsorganisation": self.purchasing_org or "-",
            "Waehrung": self.currency or "-",
            "Zahlungsbedingung": self.payment_terms or "-",
            "Incoterm": f"{self.incoterm} {self.incoterm_location}".strip() or "-",
            "Einkaeufergruppe": self.purchasing_group or "-",
            "Gesperrt": "ja" if self.blocked else "nein",
            "Steuernummer": self.tax_number or "-",
            "USt-IdNr.": self.vat_id or "-",
        }


@dataclass
class VendorMasterPlan:
    """Werte, die fuer einen Lieferanten geschrieben werden sollen.

    Alle Felder duerfen leer sein -- die Vorschau laesst den Anwender
    ergaenzen, es wird NIE eine Adresse erfunden.  ``existing_vendor_number``
    leer = Neuanlage (XK01), gesetzt = Aenderung (XK02).
    """

    existing_vendor_number: str = ""

    name: str = ""
    name2: str = ""
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = ""
    region: str = ""
    language: str = ""
    search_term: str = ""
    telephone: str = ""
    email: str = ""

    purchasing_org: str = ""
    currency: str = ""
    payment_terms: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    purchasing_group: str = ""

    tax_number: str = ""
    vat_id: str = ""

    reference_offer: str = ""
    error: str = ""
    document_number: str = ""

    @property
    def is_change(self) -> bool:
        return bool(self.existing_vendor_number)
