"""Zusammenbau der Anwendung (Komposition).

An genau einer Stelle wird entschieden, welche konkreten Klassen verwendet
werden.  GUI und Services kennen einander nur ueber ihre Schnittstellen.

Faellt eine Komponente aus (fehlende Abhaengigkeit, defekte Datei), startet die
Anwendung trotzdem -- mit klarer Meldung, was gerade nicht geht.  Ein Werkzeug,
das sich beim Start wortlos verabschiedet, hilft im Einkaufsalltag niemandem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config.settings import Settings
from .sap.gateway import SapGateway
from .sap.selectors import SelectorRegistry

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Alle zusammengebauten Dienste."""

    settings: Settings
    selectors: SelectorRegistry
    gateway: SapGateway
    repository: Any = None
    mapping: Any = None
    import_service: Any = None
    comparison: Any = None
    validation: Any = None
    preview: Any = None
    batch_factory: Any = None
    undo: Any = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Form, die das Hauptfenster erwartet."""
        return {
            "settings": self.settings,
            "selectors": self.selectors,
            "gateway": self.gateway,
            "repository": self.repository,
            "mapping": self.mapping,
            "import": self.import_service,
            "comparison": self.comparison,
            "validation": self.validation,
            "preview": self.preview,
            "batch_factory": self.batch_factory,
            "undo": self.undo,
            "problems": self.problems,
        }


class RepositoryProfileStore:
    """Adapter: Lieferantenprofile in der SQLite-Datenbank ablegen.

    Die Erkennung kennt nur das Protokoll ``VendorProfileStore``; wie und wo
    gespeichert wird, entscheidet diese Klasse.
    """

    def __init__(self, repository, profile_class) -> None:
        self.repository = repository
        self.profile_class = profile_class

    def load_profiles(self) -> list:
        profiles = []
        try:
            rows = self.repository.load_profiles()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lieferantenprofile nicht lesbar: %s", exc)
            return []
        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                import json
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            try:
                profiles.append(self._to_profile(row, payload))
            except Exception as exc:  # noqa: BLE001 - ein defektes Profil kippt nicht alle
                logger.warning("Profil %s konnte nicht geladen werden: %s",
                               row.get("profile_id"), exc)
        return profiles

    def _to_profile(self, row: dict, payload: dict):
        cls = self.profile_class
        if hasattr(cls, "from_dict"):
            data = dict(payload)
            data.setdefault("profile_id", row.get("profile_id", ""))
            data.setdefault("vendor_key", row.get("vendor_key", ""))
            data.setdefault("vendor_name", row.get("vendor_name", ""))
            data.setdefault("sample_count", row.get("sample_count", 0))
            data.setdefault("success_count", row.get("success_count", 0))
            data.setdefault("correction_count", row.get("correction_count", 0))
            return cls.from_dict(data)

        profile = cls()
        for key, value in payload.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        for key in ("profile_id", "vendor_key", "vendor_name", "sample_count",
                    "success_count", "correction_count"):
            if key in row and hasattr(profile, key):
                setattr(profile, key, row[key])
        return profile

    def save_profile(self, profile) -> None:
        payload = profile.to_dict() if hasattr(profile, "to_dict") else \
            {k: v for k, v in vars(profile).items()}
        try:
            self.repository.save_profile(
                profile_id=getattr(profile, "profile_id", ""),
                vendor_key=getattr(profile, "vendor_key", ""),
                vendor_name=getattr(profile, "vendor_name", ""),
                payload=payload,
                sample_count=getattr(profile, "sample_count", 0),
                success_count=getattr(profile, "success_count", 0),
                correction_count=getattr(profile, "correction_count", 0),
            )
        except TypeError:
            # Aeltere/abweichende Signatur -> positional versuchen
            self.repository.save_profile(
                getattr(profile, "profile_id", ""),
                getattr(profile, "vendor_key", ""),
                getattr(profile, "vendor_name", ""),
                payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Profil konnte nicht gespeichert werden: %s", exc)

    def delete_profile(self, profile_id: str) -> None:
        try:
            self.repository.delete_profile(profile_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Profil konnte nicht geloescht werden: %s", exc)


def build_services(settings: Settings) -> Services:
    """Alle Dienste aufbauen.  Fehlende Teile werden gemeldet, nicht verschwiegen."""
    settings.ensure_dirs()
    selectors = SelectorRegistry.load(settings.selectors_file)
    gateway = SapGateway(settings, selectors)
    services = Services(settings=settings, selectors=selectors, gateway=gateway)

    # -- Datenbank ------------------------------------------------------
    try:
        from .database.repository import Repository

        services.repository = Repository(settings.db_file)
        logger.info("Datenbank bereit: %s", settings.db_file)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Datenbank konnte nicht geoeffnet werden")
        services.problems.append(
            f"Historie und Zuordnungen sind nicht verfuegbar ({exc}).")

    if services.repository is not None:
        try:
            from .database.mapping_store import MappingStore

            if hasattr(MappingStore, "from_settings"):
                services.mapping = MappingStore.from_settings(services.repository, settings)
            else:
                services.mapping = MappingStore(services.repository)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Zuordnungen nicht verfuegbar")
            services.problems.append(f"Zuordnungen sind nicht verfuegbar ({exc}).")

    # -- Angebotserkennung ---------------------------------------------
    try:
        from .services.offer_import_service import OfferImportService

        profile_store = None
        if services.repository is not None:
            try:
                from .services.extraction.profiles import VendorProfile

                profile_store = RepositoryProfileStore(services.repository, VendorProfile)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Profilspeicher nicht verfuegbar: %s", exc)
        services.import_service = OfferImportService(settings, profile_store)
        logger.info("Angebotserkennung bereit.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Angebotserkennung konnte nicht geladen werden")
        services.problems.append(f"Angebote koennen nicht eingelesen werden ({exc}).")

    # -- Fachlogik ------------------------------------------------------
    try:
        from .services.comparison_service import ComparisonService
        from .services.validation_service import ValidationService

        services.comparison = ComparisonService(settings)
        services.validation = ValidationService(settings)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vergleich/Pruefung nicht verfuegbar")
        services.problems.append(f"Vergleich und Pruefung sind nicht verfuegbar ({exc}).")

    try:
        from .services.preview_service import PreviewService

        services.preview = PreviewService()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vorschau nicht verfuegbar")
        services.problems.append(f"Die Vorschau ist nicht verfuegbar ({exc}).")

    try:
        from .services.batch_service import BatchProcessor

        def batch_factory() -> Any:
            return BatchProcessor(gateway, settings, services.comparison,
                                  services.validation)

        services.batch_factory = batch_factory
    except Exception as exc:  # noqa: BLE001
        logger.exception("Verarbeitung nicht verfuegbar")
        services.problems.append(f"Die SAP-Verarbeitung ist nicht verfuegbar ({exc}).")

    try:
        from .services.undo_service import UndoService

        services.undo = UndoService()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Undo nicht verfuegbar: %s", exc)

    # -- Hinweise zur Umgebung -----------------------------------------
    if not settings.use_mock_sap:
        try:
            import win32com.client  # noqa: F401,PLC0415
        except ImportError:
            services.problems.append(
                "Das Modul 'pywin32' fehlt – ohne pywin32 ist kein SAP GUI Scripting "
                "moeglich (pip install pywin32). Das Testsystem funktioniert weiterhin.")

    return services
