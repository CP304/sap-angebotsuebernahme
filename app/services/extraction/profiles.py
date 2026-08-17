"""Lieferantenprofile -- das Gedaechtnis der Angebotserkennung.

Jeder Lieferant hat sein eigenes Angebotsformat.  Ein :class:`VendorProfile`
merkt sich, *wo* bei diesem Lieferanten welche Information steht: welche
Spaltenueberschrift welches Feld ist, welche Zeilen zu ueberspringen sind,
welches Dezimalformat gilt.

Ganz bewusst wird **nie ein Wert** gespeichert (kein Preis, keine Menge) --
nur die Struktur.  Ein Profil kann deshalb nie dazu fuehren, dass ein falscher
Preis erfunden wird; im schlimmsten Fall zeigt es auf die falsche Spalte, und
das sieht der Anwender sofort.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from ...models.offer import Offer
from ...utils.parsing import normalize_vendor_number, normalize_whitespace, similarity
from ..readers.base import RawDocument

logger = logging.getLogger(__name__)

__all__ = [
    "VendorProfile",
    "VendorProfileStore",
    "InMemoryProfileStore",
    "JsonProfileStore",
    "fingerprint",
    "match_profile",
    "apply_profile",
    "vendor_key_for",
    "new_profile",
    "document_headers",
    "MATCH_THRESHOLD",
]

#: Ab diesem Score gilt ein Profil als passend
MATCH_THRESHOLD = 0.55

#: Anzahl Textanker, die in den Fingerabdruck einfliessen
_ANCHOR_COUNT = 8


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------

@dataclass
class VendorProfile:
    """Gelerntes Erkennungsprofil eines Lieferanten."""

    profile_id: str = ""
    #: Lieferantennummer (bevorzugt) oder normalisierter Name
    vendor_key: str = ""
    vendor_name: str = ""
    email_domains: list[str] = field(default_factory=list)
    match_fingerprints: list[str] = field(default_factory=list)
    #: Spaltenueberschrift (normalisiert) -> Positionsfeld
    column_map: dict[str, str] = field(default_factory=dict)
    #: Spaltenueberschrift (normalisiert) -> ``material_number`` bzw.
    #: ``vendor_material_number``.
    #:
    #: Die wichtigste gelernte Information ueberhaupt: bei welchem Lieferanten
    #: steht in "Artikelnummer" *unsere* Nummer und bei welchem *seine*.  Hat
    #: der Anwender das einmal korrigiert, gilt es beim naechsten Angebot
    #: dieses Lieferanten automatisch -- und schlaegt jede Inhaltsheuristik.
    material_column_role: dict[str, str] = field(default_factory=dict)
    #: Kopffeld -> Regex mit einer Fanggruppe
    header_regexes: dict[str, str] = field(default_factory=dict)
    #: Regex-Muster fuer Zeilen, die keine Position sind
    skip_patterns: list[str] = field(default_factory=list)
    #: Freie Hinweise zur Tabellenstruktur (z. B. {"header_row": 3})
    table_hints: dict = field(default_factory=dict)
    decimal_style: str = "auto"          # "de" | "en" | "auto"
    default_uom: str = ""
    default_price_unit: int = 0
    sample_count: int = 0                # wie oft wurde dieses Profil genutzt
    success_count: int = 0               # ohne Korrektur uebernommen
    correction_count: int = 0            # vom Anwender korrigiert
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    notes: str = ""
    #: Regelvorschlaege mit Bestaetigungszaehler ("column_map:preis netto=price" -> 1)
    pending_rules: dict[str, int] = field(default_factory=dict)
    #: Zaehler der bereits aktiven Regeln (Nachvollziehbarkeit/Rueckbau)
    rule_counters: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def label(self) -> str:
        return self.vendor_name or self.vendor_key or self.profile_id

    @property
    def reliability(self) -> float:
        """Anteil der Importe, die ohne Korrektur durchliefen (0..1)."""
        total = self.success_count + self.correction_count
        return round(self.success_count / total, 3) if total else 0.0

    def touch(self) -> None:
        self.updated_at = datetime.now()

    def add_fingerprint(self, value: str, limit: int = 12) -> None:
        if value and value not in self.match_fingerprints:
            self.match_fingerprints.append(value)
            del self.match_fingerprints[:-limit]
            self.touch()

    def add_domain(self, domain: str) -> None:
        domain = (domain or "").lower().strip()
        if domain and domain not in self.email_domains:
            self.email_domains.append(domain)
            self.touch()

    def reset_learning(self) -> None:
        """Alles Gelernte verwerfen -- das Profil bleibt bestehen."""
        self.column_map.clear()
        self.material_column_role.clear()
        self.header_regexes.clear()
        self.skip_patterns.clear()
        self.pending_rules.clear()
        self.rule_counters.clear()
        self.decimal_style = "auto"
        self.touch()
        logger.info("Profil %s zurueckgesetzt", self.profile_id)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VendorProfile":
        payload = dict(data)
        for key in ("created_at", "updated_at"):
            value = payload.get(key)
            if isinstance(value, str):
                try:
                    payload[key] = datetime.fromisoformat(value)
                except ValueError:
                    payload[key] = datetime.now()
        known = {f for f in cls.__dataclass_fields__}          # noqa: SLF001
        return cls(**{k: v for k, v in payload.items() if k in known})


@runtime_checkable
class VendorProfileStore(Protocol):
    """Ablage der Profile -- Datenbank, Datei oder Speicher."""

    def load_profiles(self) -> list[VendorProfile]:
        ...

    def save_profile(self, profile: VendorProfile) -> None:
        ...

    def delete_profile(self, profile_id: str) -> None:
        ...


class InMemoryProfileStore:
    """Standardablage: haelt Profile nur zur Laufzeit.

    Damit funktioniert die Erkennung auch ohne Datenbank -- gelernt wird dann
    nur innerhalb einer Sitzung.
    """

    def __init__(self, profiles: Iterable[VendorProfile] = ()) -> None:
        self._profiles: dict[str, VendorProfile] = {p.profile_id: p for p in profiles}

    def load_profiles(self) -> list[VendorProfile]:
        return list(self._profiles.values())

    def save_profile(self, profile: VendorProfile) -> None:
        if not profile.profile_id:
            profile.profile_id = _new_profile_id(profile)
        profile.touch()
        self._profiles[profile.profile_id] = profile
        logger.debug("Profil gespeichert (Speicher): %s", profile.profile_id)

    def delete_profile(self, profile_id: str) -> None:
        if self._profiles.pop(profile_id, None) is not None:
            logger.info("Profil geloescht: %s", profile_id)

    def __len__(self) -> int:
        return len(self._profiles)


class JsonProfileStore:
    """Profile als JSON-Datei ablegen (eine Datei, ein Verzeichnis)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_profiles(self) -> list[VendorProfile]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Profildatei nicht lesbar (%s): %s", self.path, exc)
            return []
        profiles: list[VendorProfile] = []
        for entry in data if isinstance(data, list) else []:
            try:
                profiles.append(VendorProfile.from_dict(entry))
            except (TypeError, ValueError) as exc:
                logger.warning("Profil uebersprungen (ungueltig): %s", exc)
        return profiles

    def save_profile(self, profile: VendorProfile) -> None:
        if not profile.profile_id:
            profile.profile_id = _new_profile_id(profile)
        profile.touch()
        profiles = {p.profile_id: p for p in self.load_profiles()}
        profiles[profile.profile_id] = profile
        self._write(list(profiles.values()))

    def delete_profile(self, profile_id: str) -> None:
        profiles = [p for p in self.load_profiles() if p.profile_id != profile_id]
        self._write(profiles)

    def _write(self, profiles: list[VendorProfile]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps([p.to_dict() for p in profiles], indent=2,
                                 ensure_ascii=False)
            self.path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logger.error("Profile konnten nicht gespeichert werden (%s): %s",
                         self.path, exc)


def _new_profile_id(profile: VendorProfile) -> str:
    base = profile.vendor_key or profile.vendor_name or "lieferant"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:24] or "lieferant"
    return f"{slug}-{stamp[-8:]}"


# --------------------------------------------------------------------------
# Fingerabdruck
# --------------------------------------------------------------------------

def document_headers(document: RawDocument) -> list[str]:
    """Vermutliche Spaltenueberschriften des groessten Tabellenblocks."""
    block = document.largest_table()
    if block is None:
        return []
    normalized = block.normalized()
    best: list[str] = []
    best_score = 0
    for row in normalized.rows[:6]:
        texts = [_norm(cell) for cell in row if _norm(cell)]
        # Ueberschriften sind Text, keine Zahlen
        score = sum(1 for t in texts if not re.fullmatch(r"[\d.,%-]+", t))
        if score > best_score:
            best, best_score = texts, score
    return best


def _text_anchors(document: RawDocument, count: int = _ANCHOR_COUNT) -> list[str]:
    """Wiederkehrende Textanker (Briefkopfzeilen, Beschriftungen).

    Zahlen werden entfernt -- der Anker soll das *Formular* beschreiben, nicht
    den Inhalt eines einzelnen Angebots.
    """
    anchors: list[str] = []
    for line in (document.text or "").splitlines():
        text = _norm(line)
        if len(text) < 4 or len(text) > 60:
            continue
        text = re.sub(r"\d+", "", text).strip(" .-:/")
        text = normalize_whitespace(text)
        if len(text) < 4 or text in anchors:
            continue
        anchors.append(text)
        if len(anchors) >= count:
            break
    return anchors


def fingerprint(document: RawDocument) -> str:
    """Stabiler Fingerabdruck des *Layouts* eines Dokuments.

    Es fliessen nur Strukturmerkmale ein: Spaltenueberschriften,
    Absenderdomaene und wiederkehrende Textanker.  Preise, Mengen und
    Belegnummern bleiben bewusst aussen vor, damit derselbe Lieferant beim
    naechsten Angebot denselben Fingerabdruck erzeugt.
    """
    parts: list[str] = []
    headers = document_headers(document)
    if headers:
        parts.append("H:" + "|".join(sorted(headers)))
    if document.email is not None and document.email.sender_domain:
        parts.append("D:" + document.email.sender_domain)
    anchors = _text_anchors(document)
    if anchors:
        parts.append("A:" + "|".join(anchors))
    if not parts:
        parts.append("K:" + str(document.source_kind.value))
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"fp1:{digest}"


def _norm(text: object) -> str:
    value = normalize_whitespace(text).lower()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = re.sub(r"[^a-z0-9/. \-]", " ", value)
    return normalize_whitespace(value).strip(" .-/")


# --------------------------------------------------------------------------
# Zuordnung
# --------------------------------------------------------------------------

def vendor_key_for(offer: Offer, document: RawDocument | None = None) -> str:
    """Schluessel eines Lieferanten: Nummer bevorzugt, sonst Name/Domaene."""
    if offer.vendor_number:
        return normalize_vendor_number(offer.vendor_number)
    if offer.vendor_name:
        return _norm(offer.vendor_name)
    if document is not None and document.email is not None:
        return document.email.sender_domain
    return ""


def match_profile(profiles: Iterable[VendorProfile], document: RawDocument,
                  offer: Offer | None = None) -> tuple[VendorProfile | None, float]:
    """Bestes Profil zu einem Dokument finden.

    Score-Anteile:
      * 0.55 Absenderdomaene (eine bekannte Domaene *ist* der Lieferant)
      * 0.45 Lieferantenschluessel (Nummer bzw. Name)
      * 0.30 exakter Layout-Fingerabdruck
      * 0.25 Uebereinstimmung der Spaltenueberschriften
    """
    document_fingerprint = fingerprint(document)
    headers = set(document_headers(document))
    domain = document.email.sender_domain if document.email is not None else ""
    key = vendor_key_for(offer, document) if offer is not None else ""

    best: VendorProfile | None = None
    best_score = 0.0
    for profile in profiles:
        score = 0.0
        if domain and domain in profile.email_domains:
            # Die Absenderdomaene ist der belastbarste Hinweis, den es gibt --
            # sie allein genuegt, damit ein Profil greift.
            score += 0.55
        elif key and profile.vendor_key and key == profile.vendor_key:
            score += 0.45
        elif key and profile.vendor_key and similarity(key, profile.vendor_key) >= 0.9:
            score += 0.3

        if document_fingerprint in profile.match_fingerprints:
            score += 0.30

        if headers and profile.column_map:
            known = set(profile.column_map)
            overlap = len(headers & known) / max(len(headers | known), 1)
            score += 0.25 * overlap

        if score > best_score:
            best, best_score = profile, round(score, 3)

    if best is not None and best_score >= MATCH_THRESHOLD:
        logger.info("Profil '%s' passt zu %s (Score %.2f)", best.label,
                    document.display_name, best_score)
        return best, best_score
    if best is not None:
        logger.debug("Bestes Profil '%s' erreicht nur Score %.2f -- nicht verwendet",
                     best.label, best_score)
    return None, best_score


def new_profile(offer: Offer, document: RawDocument) -> VendorProfile:
    """Frisches Profil aus einem Import ableiten (noch ohne gelernte Regeln)."""
    profile = VendorProfile(
        vendor_key=vendor_key_for(offer, document),
        vendor_name=offer.vendor_name,
        sample_count=1,
    )
    profile.profile_id = _new_profile_id(profile)
    profile.add_fingerprint(fingerprint(document))
    if document.email is not None and document.email.sender_domain:
        profile.add_domain(document.email.sender_domain)
    logger.info("Neues Lieferantenprofil angelegt: %s", profile.label)
    return profile


def apply_profile(profile: VendorProfile, document: RawDocument,
                  offer: Offer) -> list[str]:
    """Gelernte Kopfregeln und Vorgaben eines Profils anwenden.

    Die Spaltenzuordnung wirkt direkt im :class:`TableExtractor` (dieser
    bekommt das Profil uebergeben); hier werden die Kopffelder und die
    Vorbelegungen gesetzt.  Rueckgabe: Protokollzeilen.
    """
    from ...models.enums import FieldOrigin       # lokal: vermeidet Zyklus
    from ...utils.parsing import parse_date

    notes: list[str] = [f"Lieferantenprofil '{profile.label}' angewendet "
                        f"(Zuverlaessigkeit {profile.reliability:.0%}, "
                        f"{profile.sample_count} Importe)."]
    text = document.all_text()

    for name, pattern in profile.header_regexes.items():
        if getattr(offer, name, "") not in (None, ""):
            continue
        try:
            match = re.search(pattern, text, re.I | re.M)
        except re.error as exc:
            notes.append(f"Gelernte Regel fuer '{name}' ist ungueltig und wird "
                         f"ignoriert: {exc}")
            continue
        if not match:
            continue
        raw = normalize_whitespace(match.group(1) if match.lastindex else match.group(0))
        if not raw:
            continue
        value: object = raw
        if name in ("offer_date", "valid_from", "valid_to"):
            value = parse_date(raw)
            if value is None:
                continue
        offer.set_field(name, value, FieldOrigin.MAPPED)
        notes.append(f"Kopffeld '{name}' ueber gelernte Profilregel erkannt: '{raw}'")

    if profile.vendor_name and not offer.vendor_name:
        offer.set_field("vendor_name", profile.vendor_name, FieldOrigin.MAPPED)
        notes.append(f"Lieferantenname aus dem Profil ergaenzt: {profile.vendor_name}")
    if profile.vendor_key and not offer.vendor_number and profile.vendor_key.isdigit():
        offer.set_field("vendor_number", profile.vendor_key, FieldOrigin.MAPPED)
        notes.append(f"Lieferantennummer aus dem Profil ergaenzt: {profile.vendor_key}")

    for note in notes:
        offer.add_note(note)
    return notes
