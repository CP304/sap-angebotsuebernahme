"""Befunde (Warnungen/Fehler) zu Angebot und Position."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import IssueSeverity


@dataclass(slots=True)
class Issue:
    """Ein einzelner Befund.

    ``code``   maschinenlesbarer Schluessel (z. B. ``material_missing``)
    ``message`` verstaendlicher Text fuer den Anwender -- kein Stacktrace
    ``detail``  optionale technische Zusatzinfo (aufklappbar in der GUI)
    ``field``   betroffenes Feld, damit die GUI es markieren kann
    ``blocking`` verhindert eine SAP-Schreibaktion
    """

    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING
    detail: str = ""
    field: str = ""
    blocking: bool = False
    acknowledged: bool = False

    def __str__(self) -> str:
        return f"[{self.severity.label}] {self.message}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "detail": self.detail,
            "field": self.field,
            "blocking": self.blocking,
            "acknowledged": self.acknowledged,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        return cls(
            code=data.get("code", ""),
            message=data.get("message", ""),
            severity=IssueSeverity(data.get("severity", "warning")),
            detail=data.get("detail", ""),
            field=data.get("field", ""),
            blocking=bool(data.get("blocking", False)),
            acknowledged=bool(data.get("acknowledged", False)),
        )


@dataclass(slots=True)
class IssueList:
    """Kleine Hilfsklasse, damit Befunde nicht doppelt auflaufen."""

    items: list[Issue] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        for existing in self.items:
            if existing.code == issue.code and existing.field == issue.field:
                # Bestehenden Befund aktualisieren, Quittierung erhalten
                issue.acknowledged = existing.acknowledged
                self.items[self.items.index(existing)] = issue
                return
        self.items.append(issue)

    def clear_generated(self) -> None:
        """Alle nicht vom Anwender quittierten Befunde entfernen."""
        self.items = [i for i in self.items if i.acknowledged]

    @property
    def max_severity(self) -> IssueSeverity | None:
        if not self.items:
            return None
        return max((i.severity for i in self.items), key=lambda s: s.rank)

    @property
    def has_blocking(self) -> bool:
        return any(i.blocking and not i.acknowledged for i in self.items)

    def by_severity(self, severity: IssueSeverity) -> list[Issue]:
        return [i for i in self.items if i.severity is severity]

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)
