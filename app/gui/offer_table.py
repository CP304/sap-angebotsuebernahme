"""Positionstabelle des Angebots.

Die Tabelle ist das Arbeitsmittel des Einkaeufers: gross, direkt editierbar,
sortierbar, filterbar, mit Tastatur und Copy/Paste bedienbar.

Aufbau
------
* ``OfferTableModel``  -- Datenmodell ueber ``Offer.positions``
* ``OfferFilterProxy`` -- Suche und Statusfilter
* ``OfferTableView``   -- Ansicht mit Kontextmenue, Copy/Paste, Tastaturkuerzeln
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMenu, QTableView

from ..models.enums import FieldOrigin, InfoRecordAction, PositionStatus, SourceListAction
from ..services.extraction.position_kinds import KIND_LABELS
from ..models.offer import Offer
from ..models.offer_position import OfferPosition
from ..utils.parsing import (
    format_date,
    format_decimal,
    normalize_material_number,
    normalize_uom,
    parse_date,
    parse_decimal,
    parse_int,
)
from .style import STATUS_STYLE, Colors

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    title: str
    width: int
    editable: bool = False
    kind: str = "text"      # text|decimal|int|date|check|status|action
    tooltip: str = ""
    align: int = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


#: Spaltenaufbau der Tabelle
COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("selected", "✓", 34, True, "check",
               "Position fuer die Verarbeitung auswaehlen"),
    ColumnSpec("position_number", "Pos.", 56, True, "text"),
    ColumnSpec("material_number", "Material", 110, True, "text",
               "SAP-Materialnummer -- Pflichtfeld fuer alle SAP-Aktionen"),
    ColumnSpec("description", "Beschreibung", 260, True, "text"),
    ColumnSpec("vendor_display", "Lieferant", 130, False, "text",
               "Zugeordneter SAP-Lieferant (Zuordnung in der Detailansicht)"),
    # Einkaufsorganisation und Werk stehen bewusst als eigene Spalten da und
    # nicht nur im Tooltip: Sie entscheiden, WELCHER Infosatz geschrieben
    # wird.  Derselbe Artikel beim selben Lieferanten hat je Werk einen
    # eigenen Satz mit eigenem Preis -- wer die Spalte nicht sieht, merkt
    # eine Verwechslung erst, wenn der Preis im falschen Werk steht.
    ColumnSpec("purchasing_org", "EKorg", 62, True, "text",
               "Einkaufsorganisation. Bestimmt zusammen mit dem Werk, "
               "welche Infosatz-Sicht geschrieben wird."),
    ColumnSpec("plant", "Werk", 62, True, "text",
               "Werk der werksspezifischen Infosatz-Sicht. Leer = nur die "
               "EKorg-Sicht ohne Werksbezug."),
    ColumnSpec("quantity", "Menge", 80, True, "decimal",
               align=int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)),
    ColumnSpec("uom", "ME", 48, True, "text"),
    ColumnSpec("old_price", "Alter Preis", 92, False, "decimal",
               "Preis aus dem SAP-Infosatz",
               align=int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)),
    ColumnSpec("price", "Neuer Preis", 92, True, "decimal",
               "Preis aus dem Angebot",
               align=int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)),
    ColumnSpec("change_percent", "Aenderung %", 104, False, "text",
               "Preisaenderung, bereinigt um die Preiseinheit",
               align=int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)),
    ColumnSpec("price_unit", "PE", 46, True, "int", "Preiseinheit",
               align=int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)),
    ColumnSpec("currency", "Whg", 48, True, "text"),
    ColumnSpec("conditions", "Konditionen", 190, False, "conditions",
               "Erkannte Zusatzkonditionen (Rabatt, Zuschlag, Fracht, Skonto) "
               "-- Einzelheiten in der Detailansicht"),
    ColumnSpec("valid_from", "Gueltig ab", 88, True, "date"),
    ColumnSpec("do_info_record", "Infosatz", 108, True, "action",
               "Infosatz pflegen (ME11/ME12)"),
    ColumnSpec("do_source_list", "Orderbuch", 108, True, "action",
               "Orderbuch pflegen, Lieferant aktiv setzen (ME01)"),
    ColumnSpec("do_contract", "Kontrakt", 92, True, "action",
               "Mengenkontrakt schreiben (ME31K)"),
    ColumnSpec("do_purchase_order", "Bestellung", 96, True, "action",
               "Bestellung als Abruf anlegen (ME21N)"),
    ColumnSpec("actions", "Aktionen", 116, False, "actions",
               "Geplante SAP-Aktionen: IS = Infosatz, OB = Orderbuch, "
               "MK = Mengenkontrakt, BE = Bestellung"),
    ColumnSpec("status", "Status", 170, False, "status"),
)

COLUMN_INDEX = {spec.key: index for index, spec in enumerate(COLUMNS)}

#: Standardansicht -- bewusst knapp.  Alles Weitere steht in der Detailansicht
#: und laesst sich ueber "Ansicht -> Alle Spalten" einblenden.
COMPACT_COLUMNS = (
    "selected", "position_number", "material_number", "description",
    "vendor_display", "quantity", "uom", "old_price", "price", "change_percent",
    "actions", "status",
)

#: Zusatzspalten der ausfuehrlichen Ansicht
DETAILED_ONLY = tuple(spec.key for spec in COLUMNS if spec.key not in COMPACT_COLUMNS)

#: Kuerzel der vier SAP-Aktionen fuer die Sammelspalte
ACTION_BADGES = (
    ("do_info_record", "IS"),
    ("do_source_list", "OB"),
    ("do_contract", "MK"),
    ("do_purchase_order", "BE"),
)

#: Rolle, ueber die die Detailansicht die Position bekommt
POSITION_ROLE = int(Qt.ItemDataRole.UserRole) + 1
UID_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class OfferTableModel(QAbstractTableModel):
    """Datenmodell der Angebotspositionen."""

    #: wird VOR einer Aenderung ausgeloest (fuer Undo-Schnappschuesse)
    aboutToEdit = Signal(str)
    #: (uid, feldname) nach einer Aenderung
    positionEdited = Signal(int, str)
    #: Auswahl/Aktionshaken haben sich geaendert
    selectionChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._offer: Offer | None = None
        self._positions: list[OfferPosition] = []

    # ------------------------------------------------------------------
    # Daten setzen
    # ------------------------------------------------------------------
    def set_offer(self, offer: Offer | None) -> None:
        self.beginResetModel()
        self._offer = offer
        self._positions = list(offer.positions) if offer else []
        self.endResetModel()

    @property
    def offer(self) -> Offer | None:
        return self._offer

    def position_at(self, row: int) -> OfferPosition | None:
        if 0 <= row < len(self._positions):
            return self._positions[row]
        return None

    def row_of_uid(self, uid: int) -> int:
        for row, position in enumerate(self._positions):
            if position.uid == uid:
                return row
        return -1

    def refresh_row(self, uid: int) -> None:
        row = self.row_of_uid(uid)
        if row < 0:
            return
        self.dataChanged.emit(self.index(row, 0),
                              self.index(row, len(COLUMNS) - 1))

    def refresh_all(self) -> None:
        if not self._positions:
            return
        self.dataChanged.emit(self.index(0, 0),
                              self.index(len(self._positions) - 1, len(COLUMNS) - 1))

    # ------------------------------------------------------------------
    # Qt-Schnittstelle
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._positions)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole):
        if orientation is Qt.Orientation.Horizontal:
            spec = COLUMNS[section]
            if role == Qt.ItemDataRole.DisplayRole:
                return spec.title
            if role == Qt.ItemDataRole.ToolTipRole:
                return spec.tooltip or spec.title
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(section + 1)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        spec = COLUMNS[index.column()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        position = self.position_at(index.row())
        if position is None:
            return flags
        # Bereits verarbeitete Positionen nicht mehr veraendern
        locked = position.status in (PositionStatus.DONE, PositionStatus.RUNNING)
        if spec.kind in ("check", "action"):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
            if locked:
                flags &= ~Qt.ItemFlag.ItemIsEnabled
        elif spec.editable and not locked:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        position = self.position_at(index.row())
        if position is None:
            return None
        spec = COLUMNS[index.column()]

        if role == POSITION_ROLE:
            return position
        if role == UID_ROLE:
            return position.uid

        if role == Qt.ItemDataRole.CheckStateRole and spec.kind in ("check", "action"):
            checked = bool(getattr(position, spec.key, False))
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._display(position, spec, role == Qt.ItemDataRole.EditRole)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return spec.align

        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(position, spec)

        if role == Qt.ItemDataRole.BackgroundRole:
            return self._background(position, spec)

        if role == Qt.ItemDataRole.FontRole:
            return self._font(position, spec)

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(position, spec)

        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not index.isValid():
            return False
        position = self.position_at(index.row())
        if position is None:
            return False
        spec = COLUMNS[index.column()]

        if role == Qt.ItemDataRole.CheckStateRole and spec.kind in ("check", "action"):
            self.aboutToEdit.emit(f"{spec.title} umgeschaltet")
            checked = Qt.CheckState(value) == Qt.CheckState.Checked
            setattr(position, spec.key, checked)
            self.dataChanged.emit(self.index(index.row(), 0),
                                  self.index(index.row(), len(COLUMNS) - 1))
            self.selectionChanged.emit()
            self.positionEdited.emit(position.uid, spec.key)
            return True

        if role != Qt.ItemDataRole.EditRole or not spec.editable:
            return False

        text = "" if value is None else str(value).strip()
        self.aboutToEdit.emit(f"{spec.title} geaendert")
        applied = self._apply_edit(position, spec, text)
        if not applied:
            return False
        position.mark_manual(spec.key)
        self.dataChanged.emit(self.index(index.row(), 0),
                              self.index(index.row(), len(COLUMNS) - 1))
        self.positionEdited.emit(position.uid, spec.key)
        return True

    # ------------------------------------------------------------------
    # Darstellung
    # ------------------------------------------------------------------
    def _display(self, position: OfferPosition, spec: ColumnSpec, editing: bool):
        key = spec.key

        if spec.kind in ("check",):
            return None
        if (key == "description" and not editing
                and getattr(position, "position_kind", "material") != "material"):
            # Nicht nur Farbe: auf einem Ausdruck und bei Farbsehschwaeche
            # waere die blaue Zeile unsichtbar.  Beim BEARBEITEN bleibt der
            # reine Text stehen -- sonst schriebe der Anwender das Kuerzel
            # beim naechsten Speichern mit in die Bezeichnung.
            art = KIND_LABELS.get(position.position_kind, position.position_kind)
            return f"[{art}] {position.description or ''}".strip()
        if spec.kind == "actions":
            # Sammelspalte: nur die tatsaechlich geplanten Aktionen anzeigen
            aktiv = [kuerzel for feld, kuerzel in ACTION_BADGES
                     if getattr(position, feld, False)]
            return "  ".join(aktiv) if aktiv else "–"
        if spec.kind == "action":
            return self._action_text(position, key)
        if spec.kind == "conditions":
            return position.condition_display() if position.has_conditions else ""
        if key == "status":
            symbol = STATUS_STYLE.get(position.status, ("", "", "○"))[2]
            text = position.result_text or position.status.label
            return f"{symbol}  {text}"
        if key == "vendor_display":
            if position.vendor_number:
                return position.vendor_number
            return "– nicht zugeordnet –"
        if key == "old_price":
            record = position.sap_info_record
            if record is None or not record.was_read:
                return "?"
            if not record.exists:
                return "–"
            return format_decimal(record.price)
        if key == "change_percent":
            percent = position.price_change_percent
            if percent is None:
                return ""
            sign = "+" if percent > 0 else ""
            return f"{sign}{format_decimal(percent, 2)} %"

        value = getattr(position, key, "")
        if isinstance(value, Decimal):
            if editing:
                return format(value.normalize(), "f").replace(".", ",")
            decimals = 3 if key == "quantity" else 2
            return format_decimal(value, decimals)
        if isinstance(value, date):
            return format_date(value)
        if value is None:
            return ""
        return str(value)

    def _action_text(self, position: OfferPosition, key: str) -> str:
        """Beschriftung der Aktionsspalten: was passiert konkret?"""
        if key == "do_info_record":
            action = position.info_record_action
            if not position.do_info_record:
                return "aus"
            if action is InfoRecordAction.NONE:
                return "offen"
            return action.label
        if key == "do_source_list":
            action = position.source_list_action
            if not position.do_source_list:
                return "aus"
            if action is SourceListAction.NONE:
                return "offen"
            return action.label
        if key == "do_contract":
            if not position.do_contract:
                return "aus"
            return position.created_contract or "anlegen"
        if key == "do_purchase_order":
            if not position.do_purchase_order:
                return "aus"
            return position.created_purchase_order or "Abruf"
        return ""

    def _foreground(self, position: OfferPosition, spec: ColumnSpec):
        if not position.selected and spec.key != "selected":
            return QBrush(QColor(Colors.GREY))
        if spec.key == "status":
            return QBrush(QColor(STATUS_STYLE.get(position.status,
                                                  (Colors.GREY, "", ""))[0]))
        if spec.key == "change_percent":
            percent = position.price_change_percent
            if percent is None:
                return None
            thresholds = None
            if abs(percent) >= 30:
                return QBrush(QColor(Colors.RED))
            if abs(percent) >= 10:
                return QBrush(QColor(Colors.AMBER))
            _ = thresholds
            return QBrush(QColor(Colors.GREEN if percent < 0 else Colors.TEXT))
        if spec.key == "material_number" and not position.material_number:
            return QBrush(QColor(Colors.RED))
        if spec.key == "vendor_display" and not position.vendor_number:
            return QBrush(QColor(Colors.RED))
        if spec.key == "old_price":
            record = position.sap_info_record
            if record is None or not record.was_read:
                return QBrush(QColor(Colors.GREY))
        return None

    def _background(self, position: OfferPosition, spec: ColumnSpec):
        # Unsicher erkannte Felder deutlich hervorheben
        if position.field_origins.get(spec.key) is FieldOrigin.UNCERTAIN:
            return QBrush(QColor(Colors.AMBER_BG))
        # Sonderpositionen (Einmalkosten, Alternativen, Zwischensummen) als
        # ganze Zeile abheben.  Der leere Haken allein genuegt nicht: in einem
        # Angebot mit vierzig Zeilen faellt eine fehlende Markierung nicht auf
        # -- und ausgerechnet dort ist der Fehler am teuersten.
        if getattr(position, "position_kind", "material") != "material":
            return QBrush(QColor(Colors.BLUE_BG))
        if spec.key == "status":
            return QBrush(QColor(STATUS_STYLE.get(position.status, ("", Colors.GREY_BG, ""))[1]))
        if position.status is PositionStatus.ERROR and spec.key in (
                "material_number", "vendor_display", "price"):
            return QBrush(QColor(Colors.RED_BG))
        return None

    def _font(self, position: OfferPosition, spec: ColumnSpec):
        if spec.key == "price" and position.price_changed:
            font = QFont()
            font.setBold(True)
            return font
        if spec.key == "status" and position.status in (PositionStatus.ERROR,
                                                       PositionStatus.RUNNING):
            font = QFont()
            font.setBold(True)
            return font
        return None

    def _tooltip(self, position: OfferPosition, spec: ColumnSpec) -> str:
        parts: list[str] = []
        art = getattr(position, "position_kind", "material")
        if art != "material":
            parts.append(f"{KIND_LABELS.get(art, art)} -- "
                         "nicht vorausgewaehlt, bitte pruefen")
        origin = position.field_origins.get(spec.key)
        if origin is not None:
            parts.append(f"Herkunft: {origin.label}")
        if spec.key == "status" and len(position.issues):
            parts.extend(str(issue) for issue in position.issues)
        if spec.key == "vendor_display" and position.vendor_number:
            parts.append(f"Einkaufsorg. {position.purchasing_org}, Werk {position.plant}")
        if spec.key in ("do_info_record", "do_source_list", "do_contract",
                        "do_purchase_order"):
            parts.append(spec.tooltip)
        if spec.key == "actions":
            geplant = [
                ("Infosatz", position.info_record_action.label
                 if position.do_info_record else ""),
                ("Orderbuch", position.source_list_action.label
                 if position.do_source_list else ""),
                ("Mengenkontrakt", "anlegen" if position.do_contract else ""),
                ("Bestellung", "Abruf anlegen" if position.do_purchase_order else ""),
            ]
            zeilen = [f"{name}: {text}" for name, text in geplant if text]
            parts.append("\n".join(zeilen) if zeilen
                         else "Keine SAP-Aktion fuer diese Position vorgesehen")
        if spec.key == "description" and position.source_hint:
            parts.append(f"Quelle: {position.source_hint}")
        if spec.key == "old_price":
            record = position.sap_info_record
            if record is None or not record.was_read:
                parts.append("SAP-Daten noch nicht geladen")
            elif record.exists:
                parts.append(record.price_display())
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Bearbeitung
    # ------------------------------------------------------------------
    def _apply_edit(self, position: OfferPosition, spec: ColumnSpec, text: str) -> bool:
        key = spec.key
        try:
            if spec.kind == "decimal":
                if not text:
                    setattr(position, key, None)
                    return True
                value = parse_decimal(text)
                if value is None:
                    logger.info("Eingabe %r ist keine gueltige Zahl", text)
                    return False
                setattr(position, key, value)
                return True
            if spec.kind == "int":
                if not text:
                    setattr(position, key, None)
                    return True
                value = parse_int(text)
                if value is None or value <= 0:
                    return False
                setattr(position, key, value)
                return True
            if spec.kind == "date":
                if not text:
                    setattr(position, key, None)
                    return True
                value = parse_date(text)
                if value is None:
                    return False
                setattr(position, key, value)
                return True
            # Textfelder mit fachlicher Normalisierung
            if key == "material_number":
                position.material_number = normalize_material_number(text)
                return True
            if key == "uom":
                position.uom = normalize_uom(text)
                return True
            if key == "currency":
                position.currency = text.upper()[:5]
                return True
            setattr(position, key, text)
            return True
        except Exception as exc:  # noqa: BLE001 - Eingabefehler nie eskalieren
            logger.warning("Eingabe konnte nicht uebernommen werden (%s = %r): %s",
                           key, text, exc)
            return False

    # ------------------------------------------------------------------
    # Massenaktionen
    # ------------------------------------------------------------------
    def set_all_selected(self, selected: bool) -> None:
        self.aboutToEdit.emit("Alle aus-/abgewaehlt")
        for position in self._positions:
            position.selected = selected
        self.refresh_all()
        self.selectionChanged.emit()

    def select_where(self, predicate, label: str = "Auswahl geaendert") -> int:
        """Nur die Positionen auswaehlen, die das Praedikat erfuellen."""
        self.aboutToEdit.emit(label)
        count = 0
        for position in self._positions:
            hit = bool(predicate(position))
            position.selected = hit
            count += int(hit)
        self.refresh_all()
        self.selectionChanged.emit()
        return count

    def set_action_for_selected(self, key: str, value: bool) -> int:
        """Aktionshaken (Infosatz/Orderbuch/Kontrakt/Bestellung) massenweise setzen."""
        self.aboutToEdit.emit("Aktionen geaendert")
        count = 0
        for position in self._positions:
            if not position.selected:
                continue
            if position.status in (PositionStatus.DONE, PositionStatus.RUNNING):
                continue
            setattr(position, key, value)
            count += 1
        self.refresh_all()
        self.selectionChanged.emit()
        return count

    def apply_value_to_selected(self, key: str, text: str) -> int:
        """Einen Wert auf alle ausgewaehlten Positionen anwenden."""
        spec = COLUMNS[COLUMN_INDEX[key]]
        self.aboutToEdit.emit(f"{spec.title} fuer Auswahl gesetzt")
        count = 0
        for position in self._positions:
            if not position.selected:
                continue
            if self._apply_edit(position, spec, text):
                position.mark_manual(key)
                count += 1
        self.refresh_all()
        return count

    def remove_positions(self, uids: list[int]) -> int:
        if not uids or self._offer is None:
            return 0
        self.aboutToEdit.emit("Positionen entfernt")
        self.beginResetModel()
        keep = [p for p in self._positions if p.uid not in set(uids)]
        removed = len(self._positions) - len(keep)
        self._offer.positions = keep
        self._positions = keep
        self.endResetModel()
        self.selectionChanged.emit()
        return removed

    def add_empty_position(self) -> OfferPosition | None:
        """Position manuell ergaenzen (kommt vor, wenn das Angebot lueckenhaft ist)."""
        if self._offer is None:
            return None
        from ..models.enums import SourceKind

        self.aboutToEdit.emit("Position ergaenzt")
        position = OfferPosition(source_kind=SourceKind.MANUAL,
                                 source_hint="manuell ergaenzt")
        position.currency = self._offer.currency
        position.valid_from = self._offer.valid_from
        position.vendor_number = self._offer.vendor_number
        self.beginInsertRows(QModelIndex(), len(self._positions), len(self._positions))
        self._offer.positions.append(position)
        self._positions = self._offer.positions
        self.endInsertRows()
        self.selectionChanged.emit()
        return position

    # ------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """Kennzahlen fuer die Fussleiste."""
        result = {"gesamt": len(self._positions), "ausgewaehlt": 0, "bereit": 0,
                  "pruefen": 0, "fehler": 0, "geaendert": 0, "verarbeitet": 0}
        for position in self._positions:
            if position.selected:
                result["ausgewaehlt"] += 1
            if position.status is PositionStatus.READY:
                result["bereit"] += 1
            elif position.status is PositionStatus.CHECK:
                result["pruefen"] += 1
            elif position.status is PositionStatus.ERROR:
                result["fehler"] += 1
            elif position.status is PositionStatus.DONE:
                result["verarbeitet"] += 1
            if position.has_changes:
                result["geaendert"] += 1
        return result


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class OfferFilterProxy(QSortFilterProxyModel):
    """Volltextsuche plus Statusfilter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._search = ""
        self._status_filter: set[PositionStatus] = set()
        self._only_changed = False
        self._only_selected = False
        self._only_special = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def _refresh(self) -> None:
        """Filter neu auswerten.

        ``invalidate()`` statt ``invalidateFilter()``/``invalidateRowsFilter()``:
        beide sind in neueren Qt-Versionen abgekuendigt.
        """
        self.invalidate()

    def set_search(self, text: str) -> None:
        self._search = text.strip().lower()
        self._refresh()

    def set_status_filter(self, statuses: set[PositionStatus]) -> None:
        self._status_filter = statuses
        self._refresh()

    def set_only_changed(self, active: bool) -> None:
        self._only_changed = active
        self._refresh()

    def set_only_selected(self, active: bool) -> None:
        self._only_selected = active
        self._refresh()

    def set_only_special(self, active: bool) -> None:
        """Nur Sonderpositionen zeigen (Einmalkosten, Alternativen, Summen).

        Bei einem Angebot mit vielen Zeilen ist das der schnellste Weg, die
        Entscheidungen abzuarbeiten, die das Werkzeug bewusst dem Anwender
        ueberlassen hat.
        """
        self._only_special = active
        self._refresh()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, OfferTableModel):
            return True
        position = model.position_at(source_row)
        if position is None:
            return False
        if self._status_filter and position.status not in self._status_filter:
            return False
        if self._only_changed and not position.has_changes:
            return False
        if self._only_selected and not position.selected:
            return False
        if (self._only_special
                and getattr(position, "position_kind", "material") == "material"):
            return False
        if self._search:
            haystack = " ".join(str(x) for x in (
                position.position_number, position.material_number,
                position.vendor_material_number, position.description,
                position.vendor_number, position.remarks, position.uom,
                position.result_text,
            )).lower()
            if self._search not in haystack:
                return False
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        """Zahlen und Datumsangaben fachlich sortieren, nicht als Text."""
        model = self.sourceModel()
        if not isinstance(model, OfferTableModel):
            return super().lessThan(left, right)
        spec = COLUMNS[left.column()]
        a = model.position_at(left.row())
        b = model.position_at(right.row())
        if a is None or b is None:
            return super().lessThan(left, right)

        def sort_key(position: OfferPosition):
            if spec.key == "change_percent":
                return position.price_change_percent or Decimal(-9999)
            if spec.key == "old_price":
                return position.old_price or Decimal(-1)
            if spec.key == "status":
                return position.status.value
            if spec.key == "vendor_display":
                return position.vendor_number
            value = getattr(position, spec.key, "")
            if value is None:
                return Decimal(-1) if spec.kind in ("decimal", "int") else ""
            if isinstance(value, bool):
                return int(value)
            return value

        try:
            return sort_key(a) < sort_key(b)
        except TypeError:
            return str(sort_key(a)) < str(sort_key(b))


# ---------------------------------------------------------------------------
# Ansicht
# ---------------------------------------------------------------------------

class OfferTableView(QTableView):
    """Tabellenansicht mit Kontextmenue, Copy/Paste und Tastaturbedienung."""

    positionActivated = Signal(object)       # OfferPosition
    requestVendorAssignment = Signal(object)
    requestDetails = Signal(object)
    requestRemove = Signal(list)             # list[uid]
    requestFillDown = Signal(str)            # Spaltenschluessel

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._detailed = False
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                             | QAbstractItemView.EditTrigger.EditKeyPressed
                             | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.verticalHeader().setDefaultSectionSize(26)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)

        self._install_shortcuts()

    # ------------------------------------------------------------------
    def apply_column_widths(self) -> None:
        for index, spec in enumerate(COLUMNS):
            self.setColumnWidth(index, spec.width)
        self.set_detailed_columns(self._detailed)

    def set_detailed_columns(self, detailed: bool) -> None:
        """Zwischen knapper Standardansicht und allen Spalten umschalten.

        Die Standardansicht zeigt das, was der Einkaeufer zum Entscheiden
        braucht.  Alles Weitere steht in der Detailansicht -- und laesst sich
        bei Bedarf hier einblenden.
        """
        self._detailed = detailed
        for index, spec in enumerate(COLUMNS):
            sichtbar = detailed or spec.key in COMPACT_COLUMNS
            self.setColumnHidden(index, not sichtbar)

    @property
    def detailed_columns(self) -> bool:
        return self._detailed

    def current_position(self) -> OfferPosition | None:
        index = self.currentIndex()
        if not index.isValid():
            return None
        return index.data(POSITION_ROLE)

    def selected_positions(self) -> list[OfferPosition]:
        positions: list[OfferPosition] = []
        seen: set[int] = set()
        for index in self.selectionModel().selectedIndexes() if self.selectionModel() else []:
            position = index.data(POSITION_ROLE)
            if position is not None and position.uid not in seen:
                seen.add(position.uid)
                positions.append(position)
        return positions

    # ------------------------------------------------------------------
    def _install_shortcuts(self) -> None:
        copy_action = QAction("Kopieren", self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self.copy_selection)
        self.addAction(copy_action)

        paste_action = QAction("Einfuegen", self)
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        paste_action.triggered.connect(self.paste_clipboard)
        self.addAction(paste_action)

        toggle_action = QAction("Auswahl umschalten", self)
        toggle_action.setShortcut(QKeySequence("Space"))
        toggle_action.triggered.connect(self.toggle_selected_rows)
        self.addAction(toggle_action)

        details_action = QAction("Details", self)
        details_action.setShortcut(QKeySequence("Return"))
        details_action.triggered.connect(self._emit_details)
        self.addAction(details_action)

    def _emit_details(self) -> None:
        position = self.current_position()
        if position is not None:
            self.requestDetails.emit(position)

    def toggle_selected_rows(self) -> None:
        positions = self.selected_positions()
        if not positions:
            return
        target = not all(p.selected for p in positions)
        model = self._source_model()
        if model is None:
            return
        model.aboutToEdit.emit("Auswahl umgeschaltet")
        for position in positions:
            position.selected = target
        model.refresh_all()
        model.selectionChanged.emit()

    def _source_model(self) -> OfferTableModel | None:
        model = self.model()
        if isinstance(model, OfferFilterProxy):
            source = model.sourceModel()
            return source if isinstance(source, OfferTableModel) else None
        return model if isinstance(model, OfferTableModel) else None

    # ------------------------------------------------------------------
    def copy_selection(self) -> None:
        """Auswahl als Tabulatortext in die Zwischenablage (Excel-kompatibel)."""
        selection = self.selectionModel()
        if selection is None:
            return
        indexes = sorted(selection.selectedIndexes(),
                         key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        rows: dict[int, dict[int, str]] = {}
        for index in indexes:
            text = index.data(Qt.ItemDataRole.DisplayRole)
            rows.setdefault(index.row(), {})[index.column()] = "" if text is None else str(text)
        lines = []
        for row in sorted(rows):
            columns = rows[row]
            lines.append("\t".join(columns[c] for c in sorted(columns)))
        QApplication.clipboard().setText("\n".join(lines))
        logger.debug("%d Zeile(n) kopiert", len(lines))

    def paste_clipboard(self) -> None:
        """Zwischenablage ab der aktuellen Zelle einfuegen (Block moeglich)."""
        text = QApplication.clipboard().text()
        if not text:
            return
        start = self.currentIndex()
        if not start.isValid():
            return
        model = self.model()
        rows = [line.split("\t") for line in text.replace("\r\n", "\n").split("\n") if line != ""]
        for row_offset, values in enumerate(rows):
            for column_offset, value in enumerate(values):
                index = model.index(start.row() + row_offset,
                                    start.column() + column_offset)
                if not index.isValid():
                    continue
                if not (index.flags() & Qt.ItemFlag.ItemIsEditable):
                    continue
                model.setData(index, value, Qt.ItemDataRole.EditRole)
        logger.info("%d Zeile(n) aus der Zwischenablage eingefuegt", len(rows))

    # ------------------------------------------------------------------
    def _show_context_menu(self, point) -> None:
        index = self.indexAt(point)
        menu = QMenu(self)
        position = index.data(POSITION_ROLE) if index.isValid() else None
        positions = self.selected_positions()

        if position is not None:
            menu.addAction("Details anzeigen",
                           lambda: self.requestDetails.emit(position))
            menu.addAction("SAP-Lieferant zuordnen ...",
                           lambda: self.requestVendorAssignment.emit(position))
            menu.addSeparator()

        if positions:
            count = len(positions)
            actions = menu.addMenu(f"Aktionen fuer {count} Position(en)")
            model = self._source_model()
            if model is not None:
                for key, label in (("do_info_record", "Infosatz pflegen"),
                                   ("do_source_list", "Orderbuch pflegen"),
                                   ("do_contract", "Mengenkontrakt schreiben"),
                                   ("do_purchase_order", "Bestellung anlegen")):
                    submenu = actions.addMenu(label)
                    submenu.addAction("einschalten",
                                      lambda k=key: self._set_action(positions, k, True))
                    submenu.addAction("ausschalten",
                                      lambda k=key: self._set_action(positions, k, False))

            spec = COLUMNS[index.column()] if index.isValid() else None
            if spec is not None and spec.editable and spec.kind not in ("check", "action"):
                menu.addAction(f"'{spec.title}' nach unten ausfuellen",
                               lambda k=spec.key: self.requestFillDown.emit(k))

            menu.addSeparator()
            menu.addAction("Kopieren", self.copy_selection)
            menu.addAction("Einfuegen", self.paste_clipboard)
            menu.addSeparator()
            remove = menu.addAction(f"{count} Position(en) entfernen")
            remove.triggered.connect(
                lambda: self.requestRemove.emit([p.uid for p in positions]))

        if not menu.isEmpty():
            menu.exec(self.viewport().mapToGlobal(point))

    def _set_action(self, positions: list[OfferPosition], key: str, value: bool) -> None:
        model = self._source_model()
        if model is None:
            return
        model.aboutToEdit.emit("Aktionen geaendert")
        for position in positions:
            if position.status in (PositionStatus.DONE, PositionStatus.RUNNING):
                continue
            setattr(position, key, value)
        model.refresh_all()
        model.selectionChanged.emit()
