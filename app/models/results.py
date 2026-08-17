"""Ergebnisobjekte der SAP-Verarbeitung."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ResultState


@dataclass
class ActionResult:
    """Ergebnis *einer* SAP-Aktion (Infosatz, Orderbuch, Kontrakt, Bestellung)."""

    action: str                      # "info_record" | "source_list" | "contract" | "purchase_order"
    state: ResultState
    message: str = ""
    transaction: str = ""            # tatsaechlich verwendete Transaktion
    document_number: str = ""        # Infosatz-/Kontrakt-/Bestellnummer
    old_value: str = ""
    new_value: str = ""
    detail: str = ""                 # technische Details (aufklappbar)
    duration_ms: int = 0
    sap_messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in (ResultState.SUCCESS, ResultState.SIMULATED)

    @property
    def action_label(self) -> str:
        return {
            "info_record": "Infosatz",
            "source_list": "Orderbuch",
            "contract": "Mengenkontrakt",
            "purchase_order": "Bestellung",
        }.get(self.action, self.action)

    def display(self) -> str:
        text = f"{self.state.symbol} {self.action_label}"
        if self.message:
            text += f": {self.message}"
        if self.document_number:
            text += f" ({self.document_number})"
        return text


@dataclass
class PositionResult:
    """Gesammeltes Ergebnis einer Angebotsposition."""

    position_uid: int
    label: str = ""
    actions: list[ActionResult] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def state(self) -> ResultState:
        if not self.actions:
            return ResultState.SKIPPED
        if any(a.state is ResultState.FAILED for a in self.actions):
            return ResultState.FAILED
        if all(a.state is ResultState.SKIPPED for a in self.actions):
            return ResultState.SKIPPED
        if any(a.state is ResultState.SIMULATED for a in self.actions):
            return ResultState.SIMULATED
        return ResultState.SUCCESS

    @property
    def error_messages(self) -> list[str]:
        return [a.message for a in self.actions if a.state is ResultState.FAILED]

    def display_block(self) -> str:
        lines = [f"Material {self.label}"]
        lines.extend(f"  {a.display()}" for a in self.actions)
        return "\n".join(lines)


@dataclass
class BatchSummary:
    """Gesamtergebnis eines Verarbeitungslaufs."""

    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: list[PositionResult] = field(default_factory=list)
    document_results: list[ActionResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.state in (ResultState.SUCCESS, ResultState.SIMULATED))

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.state is ResultState.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.state is ResultState.SKIPPED)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def created_documents(self) -> list[str]:
        numbers = []
        for action in self.document_results:
            if action.document_number:
                numbers.append(f"{action.action_label} {action.document_number}")
        return numbers

    def headline(self) -> str:
        prefix = "DRY RUN – " if self.dry_run else ""
        return (
            f"{prefix}{self.succeeded} erfolgreich, {self.failed} fehlgeschlagen, "
            f"{self.skipped} uebersprungen ({self.duration_seconds:.1f} s)"
        )
