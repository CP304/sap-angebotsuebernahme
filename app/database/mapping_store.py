"""Fachliche Schicht ueber den Zuordnungstabellen.

Aufgabe: Aus dem, was im Angebot steht (Lieferantenname, Maildomain,
Artikelnummer des Lieferanten, Beschreibungstext), die *eigenen* SAP-
Schluessel ermitteln -- und dabei ehrlich bleiben.

Der wichtigste Grundsatz des Projekts gilt auch hier: **Es wird nie geraten.**
Eine automatische Zuordnung erfolgt ausschliesslich bei

* einem ausdruecklichen Hinweis aus dem Angebot,
* einer exakten, frueher bestaetigten Zuordnung oder
* einer Aehnlichkeit oberhalb des Schwellwerts, die zudem eindeutig ist.

In allen anderen Faellen liefert die Aufloesung ``unresolved`` und dazu die
Kandidaten -- der Einkaeufer entscheidet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..utils.parsing import (
    normalize_material_number,
    normalize_vendor_number,
    normalize_whitespace,
    similarity,
)
from .repository import Repository

logger = logging.getLogger(__name__)

__all__ = [
    "MappingStore",
    "MappingCandidate",
    "VendorResolution",
    "MaterialResolution",
    "DEFAULT_AUTO_THRESHOLD",
    "DEFAULT_SUGGEST_THRESHOLD",
]

#: Ab dieser Aehnlichkeit darf automatisch zugeordnet werden.
DEFAULT_AUTO_THRESHOLD = 0.88
#: Ab dieser Aehnlichkeit wird ein Vorschlag angezeigt (nie automatisch gesetzt).
DEFAULT_SUGGEST_THRESHOLD = 0.60
#: Liegen die zwei besten Treffer so dicht beieinander, gilt die Lage als
#: uneindeutig -- dann wird trotz hoher Aehnlichkeit nichts automatisch gesetzt.
AMBIGUITY_MARGIN = 0.03


@dataclass
class MappingCandidate:
    """Ein Vorschlag zur Auswahl durch den Anwender."""

    number: str                 # SAP-Lieferanten- bzw. Materialnummer
    label: str = ""             # Name bzw. Beschreibung
    score: float = 0.0          # 0.0 - 1.0
    match_type: str = ""        # 'name' | 'domain' | 'vendor_material' | 'text' ...
    match_value: str = ""       # gespeicherter Suchwert
    mapping_id: int = 0
    use_count: int = 0

    @property
    def percent(self) -> int:
        """Aehnlichkeit als ganze Prozent -- fuer die Anzeige."""
        return int(round(self.score * 100))

    def display(self) -> str:
        text = f"{self.number} {self.label}".strip()
        return f"{text} ({self.percent} %)" if self.score else text


@dataclass
class VendorResolution:
    """Ergebnis der Lieferantenaufloesung."""

    vendor_number: str = ""
    vendor_name: str = ""
    source: str = "unresolved"   # 'hint' | 'mapping_domain' | 'mapping_name' | 'mapping_fuzzy' | 'unresolved'
    confidence: float = 0.0
    candidates: list[MappingCandidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.vendor_number)

    @property
    def needs_decision(self) -> bool:
        """Muss der Anwender ran?  (nichts zugeordnet, aber Vorschlaege da)"""
        return not self.resolved and bool(self.candidates)

    @property
    def source_label(self) -> str:
        return {
            "hint": "Nummer stand im Angebot",
            "mapping_domain": "ueber Maildomain zugeordnet",
            "mapping_name": "ueber gespeicherten Namen zugeordnet",
            "mapping_fuzzy": "ueber Namensaehnlichkeit zugeordnet",
            "unresolved": "nicht zugeordnet",
        }.get(self.source, self.source)


@dataclass
class MaterialResolution:
    """Ergebnis der Materialaufloesung."""

    material_number: str = ""
    description: str = ""
    source: str = "unresolved"   # 'hint' | 'mapping_vendor_material' | 'mapping_text' | 'mapping_fuzzy' | 'unresolved'
    confidence: float = 0.0
    candidates: list[MappingCandidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.material_number)

    @property
    def needs_decision(self) -> bool:
        return not self.resolved and bool(self.candidates)

    @property
    def source_label(self) -> str:
        return {
            "hint": "Materialnummer stand im Angebot",
            "mapping_vendor_material": "ueber Lieferantenartikelnummer zugeordnet",
            "mapping_text": "ueber gespeicherten Text zugeordnet",
            "mapping_fuzzy": "ueber Textaehnlichkeit zugeordnet",
            "unresolved": "nicht zugeordnet",
        }.get(self.source, self.source)


class MappingStore:
    """Lernendes Gedaechtnis fuer Lieferanten- und Materialzuordnungen."""

    def __init__(
        self,
        repository: Repository,
        auto_threshold: float = DEFAULT_AUTO_THRESHOLD,
        suggest_threshold: float = DEFAULT_SUGGEST_THRESHOLD,
        max_candidates: int = 5,
    ) -> None:
        self.repository = repository
        self.auto_threshold = float(auto_threshold)
        self.suggest_threshold = float(suggest_threshold)
        self.max_candidates = int(max_candidates)

    @classmethod
    def from_settings(cls, repository: Repository, settings) -> "MappingStore":
        """Schwellwerte aus der Konfiguration uebernehmen."""
        thresholds = getattr(settings, "thresholds", None)
        auto = float(getattr(thresholds, "vendor_match_threshold", DEFAULT_AUTO_THRESHOLD))
        suggest = float(getattr(thresholds, "vendor_suggest_threshold", DEFAULT_SUGGEST_THRESHOLD))
        return cls(repository, auto_threshold=auto, suggest_threshold=suggest)

    # ==================================================================
    # Lieferant
    # ==================================================================
    def resolve_vendor(
        self,
        vendor_name: str,
        email_domain: str = "",
        vendor_number_hint: str = "",
    ) -> VendorResolution:
        """Lieferantennummer bestimmen.

        Reihenfolge: ausdruecklicher Hinweis aus dem Angebot -> Domain-
        zuordnung -> exakte Namenszuordnung -> Aehnlichkeitssuche ->
        ``unresolved``.
        """
        name = normalize_whitespace(vendor_name)

        # 1. Der Lieferant hat seine SAP-Nummer selbst genannt (z. B. im Briefkopf).
        hint = normalize_vendor_number(vendor_number_hint)
        if hint:
            known = self._vendor_name_for_number(hint)
            return VendorResolution(
                vendor_number=hint,
                vendor_name=known or name,
                source="hint",
                confidence=1.0,
            )

        # 2. Maildomain -- der zuverlaessigste gelernte Schluessel.
        domain_entry = self._find_domain_mapping(email_domain)
        if domain_entry:
            return VendorResolution(
                vendor_number=domain_entry["vendor_number"],
                vendor_name=domain_entry.get("vendor_name") or name,
                source="mapping_domain",
                confidence=float(domain_entry.get("confidence") or 1.0),
            )

        # 3. Exakt gespeicherter Name.
        if name:
            entry = self.repository.find_vendor_mapping("name", name)
            if entry:
                return VendorResolution(
                    vendor_number=entry["vendor_number"],
                    vendor_name=entry.get("vendor_name") or name,
                    source="mapping_name",
                    confidence=float(entry.get("confidence") or 1.0),
                )

        # 4. Aehnlichkeit ueber alle bekannten Namen.
        candidates = self._vendor_candidates(name)
        best = candidates[0] if candidates else None
        if best and best.score >= self.auto_threshold and self._is_unambiguous(candidates):
            # Nutzung mitzaehlen, damit haeufig genutzte Zuordnungen sichtbar werden
            self.repository.find_vendor_mapping(best.match_type, best.match_value)
            return VendorResolution(
                vendor_number=best.number,
                vendor_name=best.label or name,
                source="mapping_fuzzy",
                confidence=best.score,
                candidates=candidates,
            )

        # 5. Nichts Belastbares -- nur Vorschlaege liefern.
        return VendorResolution(
            vendor_name=name,
            source="unresolved",
            confidence=best.score if best else 0.0,
            candidates=candidates,
        )

    def _find_domain_mapping(self, email_domain: str) -> dict | None:
        """Zuerst die vollstaendige Adresse, dann die Domain versuchen."""
        raw = normalize_whitespace(email_domain).strip("<>").lower()
        if not raw:
            return None
        if "@" in raw:
            entry = self.repository.find_vendor_mapping("email", raw)
            if entry:
                return entry
            raw = raw.rsplit("@", 1)[1]
        return self.repository.find_vendor_mapping("domain", raw)

    def _vendor_name_for_number(self, vendor_number: str) -> str:
        """Bereits bekannten Klarnamen zu einer Lieferantennummer suchen."""
        for entry in self.repository.all_vendor_mappings():
            if entry.get("vendor_number") == vendor_number and entry.get("vendor_name"):
                return str(entry["vendor_name"])
        return ""

    def _vendor_candidates(self, name: str) -> list[MappingCandidate]:
        """Aehnlichkeitsvorschlaege ueber alle gespeicherten Namen."""
        if not name:
            return []
        best_per_vendor: dict[str, MappingCandidate] = {}
        for entry in self.repository.all_vendor_mappings():
            if entry.get("match_type") not in ("name", "email", "domain", "vat_id"):
                continue
            # Gegen den gespeicherten Suchwert *und* den Klarnamen vergleichen
            texts = [str(entry.get("vendor_name") or "")]
            if entry.get("match_type") == "name":
                texts.append(str(entry.get("match_value") or ""))
            score = max((similarity(name, text) for text in texts if text), default=0.0)
            if score < self.suggest_threshold:
                continue
            number = str(entry.get("vendor_number") or "")
            candidate = MappingCandidate(
                number=number,
                label=str(entry.get("vendor_name") or entry.get("match_value") or ""),
                score=score,
                match_type=str(entry.get("match_type") or ""),
                match_value=str(entry.get("match_value") or ""),
                mapping_id=int(entry.get("id") or 0),
                use_count=int(entry.get("use_count") or 0),
            )
            previous = best_per_vendor.get(number)
            if previous is None or candidate.score > previous.score:
                best_per_vendor[number] = candidate

        candidates = sorted(best_per_vendor.values(), key=lambda c: (-c.score, c.number))
        return candidates[: self.max_candidates]

    @staticmethod
    def _is_unambiguous(candidates: list[MappingCandidate]) -> bool:
        """Zwei praktisch gleich gute Treffer auf *verschiedene* Nummern
        bedeuten: der Anwender muss entscheiden."""
        if len(candidates) < 2:
            return True
        first, second = candidates[0], candidates[1]
        if first.number == second.number:
            return True
        return (first.score - second.score) >= AMBIGUITY_MARGIN

    def remember_vendor(
        self,
        vendor_name: str,
        vendor_number: str,
        email_domain: str = "",
        vat_id: str = "",
        confidence: float = 1.0,
        created_by: str = "",
    ) -> list[int]:
        """"Diese Zuordnung fuer zukuenftige Angebote speichern."

        Es werden alle verfuegbaren Schluessel gelernt (Name, Domain,
        USt-IdNr.), damit dasselbe Angebot beim naechsten Mal auch dann
        erkannt wird, wenn nur eines der Merkmale vorliegt.
        """
        number = normalize_vendor_number(vendor_number)
        name = normalize_whitespace(vendor_name)
        if not number:
            logger.warning("Zuordnung ohne Lieferantennummer wird nicht gespeichert")
            return []
        ids: list[int] = []
        if name:
            ids.append(self.repository.save_vendor_mapping(
                "name", name, number, name, confidence, created_by))
        domain = normalize_whitespace(email_domain).strip("<>").lower()
        if domain:
            match_type = "email" if "@" in domain else "domain"
            ids.append(self.repository.save_vendor_mapping(
                match_type, domain, number, name, confidence, created_by))
        if normalize_whitespace(vat_id):
            ids.append(self.repository.save_vendor_mapping(
                "vat_id", vat_id, number, name, confidence, created_by))
        return [identifier for identifier in ids if identifier]

    def forget_vendor(self, mapping_id: int) -> bool:
        return self.repository.delete_vendor_mapping(mapping_id)

    def list_vendors(self, match_type: str = "") -> list[dict]:
        return self.repository.all_vendor_mappings(match_type)

    # ==================================================================
    # Material
    # ==================================================================
    def resolve_material(
        self,
        vendor_number: str,
        vendor_material_number: str,
        description: str = "",
    ) -> MaterialResolution:
        """Eigene Materialnummer zu einer Angebotsposition bestimmen.

        Reihenfolge: Lieferantenartikelnummer -> gespeicherter Text ->
        Textaehnlichkeit -> ``unresolved``.
        """
        vendor = normalize_vendor_number(vendor_number)
        article = normalize_material_number(vendor_material_number)
        text = normalize_whitespace(description)

        if article:
            entry = self.repository.find_material_mapping(vendor, "vendor_material", article)
            if entry:
                return MaterialResolution(
                    material_number=entry["material_number"],
                    description=entry.get("description") or text,
                    source="mapping_vendor_material",
                    confidence=float(entry.get("confidence") or 1.0),
                )

        if text:
            entry = self.repository.find_material_mapping(vendor, "text", text)
            if entry:
                return MaterialResolution(
                    material_number=entry["material_number"],
                    description=entry.get("description") or text,
                    source="mapping_text",
                    confidence=float(entry.get("confidence") or 1.0),
                )

        candidates = self._material_candidates(vendor, article, text)
        best = candidates[0] if candidates else None
        if best and best.score >= self.auto_threshold and self._is_unambiguous(candidates):
            self.repository.find_material_mapping(vendor, best.match_type, best.match_value)
            return MaterialResolution(
                material_number=best.number,
                description=best.label or text,
                source="mapping_fuzzy",
                confidence=best.score,
                candidates=candidates,
            )

        return MaterialResolution(
            description=text,
            source="unresolved",
            confidence=best.score if best else 0.0,
            candidates=candidates,
        )

    def _material_candidates(
        self, vendor_number: str, article: str, text: str
    ) -> list[MappingCandidate]:
        """Vorschlaege aus den Zuordnungen dieses Lieferanten (plus globale)."""
        if not article and not text:
            return []
        best_per_material: dict[str, MappingCandidate] = {}
        for entry in self.repository.all_material_mappings(vendor_number):
            stored_value = str(entry.get("match_value") or "")
            stored_text = str(entry.get("description") or "")
            score = 0.0
            if entry.get("match_type") == "vendor_material" and article:
                score = max(score, similarity(article, stored_value))
            if text:
                for candidate_text in (stored_text, stored_value):
                    if candidate_text:
                        score = max(score, similarity(text, candidate_text))
            if score < self.suggest_threshold:
                continue
            number = str(entry.get("material_number") or "")
            candidate = MappingCandidate(
                number=number,
                label=stored_text or stored_value,
                score=score,
                match_type=str(entry.get("match_type") or ""),
                match_value=stored_value,
                mapping_id=int(entry.get("id") or 0),
                use_count=int(entry.get("use_count") or 0),
            )
            previous = best_per_material.get(number)
            if previous is None or candidate.score > previous.score:
                best_per_material[number] = candidate

        candidates = sorted(best_per_material.values(), key=lambda c: (-c.score, c.number))
        return candidates[: self.max_candidates]

    def remember_material(
        self,
        vendor_number: str,
        material_number: str,
        vendor_material_number: str = "",
        description: str = "",
        confidence: float = 1.0,
    ) -> list[int]:
        """Materialzuordnung merken (Artikelnummer und/oder Text)."""
        material = normalize_material_number(material_number)
        if not material:
            logger.warning("Zuordnung ohne Materialnummer wird nicht gespeichert")
            return []
        vendor = normalize_vendor_number(vendor_number)
        text = normalize_whitespace(description)
        ids: list[int] = []
        if normalize_material_number(vendor_material_number):
            ids.append(self.repository.save_material_mapping(
                vendor, "vendor_material", vendor_material_number, material, text, confidence))
        if text:
            ids.append(self.repository.save_material_mapping(
                vendor, "text", text, material, text, confidence))
        return [identifier for identifier in ids if identifier]

    def forget_material(self, mapping_id: int) -> bool:
        return self.repository.delete_material_mapping(mapping_id)

    def list_materials(self, vendor_number: str = "") -> list[dict]:
        return self.repository.all_material_mappings(vendor_number)
