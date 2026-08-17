"""Detailansicht einer Angebotsposition.

Zeigt drei Bloecke nebeneinander bzw. untereinander:

    Angebot            -- alle erkannten Werte, editierbar
    SAP Ist-Zustand    -- was heute in SAP steht (nur lesend)
    Geplante Aenderung -- was passieren wird, inkl. Prozentangabe

Darunter die Hinweise/Warnungen der Position, die der Anwender bewusst
quittieren kann.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models.enums import FieldOrigin, IssueSeverity
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
from .style import SEVERITY_COLOR, STATUS_STYLE, Colors, badge_style

logger = logging.getLogger(__name__)


class _Field(QLineEdit):
    """Eingabefeld mit Herkunftsanzeige."""

    def __init__(self, key: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.setClearButtonEnabled(True)

    def mark_origin(self, origin: FieldOrigin) -> None:
        tips = {
            FieldOrigin.UNCERTAIN: ("Unsicher erkannt – bitte pruefen", Colors.AMBER_BG),
            FieldOrigin.MISSING: ("Nicht erkannt – bitte ergaenzen", Colors.SURFACE),
            FieldOrigin.MANUAL: ("Manuell erfasst", Colors.SURFACE),
            FieldOrigin.MAPPED: ("Ueber Zuordnung ergaenzt", Colors.ACCENT_LIGHT),
            FieldOrigin.DEFAULT: ("Voreinstellung", Colors.SURFACE),
            FieldOrigin.EXTRACTED: ("Aus dem Angebot erkannt", Colors.SURFACE),
        }
        tip, background = tips.get(origin, ("", Colors.SURFACE))
        self.setToolTip(tip)
        self.setStyleSheet(f"QLineEdit {{ background: {background}; }}")


class PositionDetails(QWidget):
    """Detail- und Bearbeitungsbereich einer Position."""

    positionChanged = Signal(object)          # OfferPosition
    requestVendorAssignment = Signal(object)
    requestReloadSap = Signal(object)
    issueAcknowledged = Signal(object, str)   # (position, code)

    def __init__(self, comparison_service=None, parent=None) -> None:
        super().__init__(parent)
        self.comparison = comparison_service
        self._position: OfferPosition | None = None
        self._loading = False
        self._fields: dict[str, _Field] = {}

        self._build()
        self.set_position(None)

    # ------------------------------------------------------------------
    # Aufbau
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Kopfzeile mit Bezeichnung und Status
        header = QFrame()
        header.setObjectName("Card")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        self.title_label = QLabel("Keine Position ausgewaehlt")
        self.title_label.setObjectName("Heading")
        self.status_badge = QLabel("")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.title_label, 1)
        header_layout.addWidget(self.status_badge, 0)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        layout.addWidget(self._build_offer_group())

        columns = QHBoxLayout()
        columns.setSpacing(10)
        columns.addWidget(self._build_sap_group(), 1)
        columns.addWidget(self._build_change_group(), 1)
        layout.addLayout(columns)

        layout.addWidget(self._build_document_group())
        layout.addWidget(self._build_issue_group())
        layout.addStretch(1)

    def _add_field(self, form: QFormLayout, key: str, label: str,
                   placeholder: str = "") -> _Field:
        field = _Field(key)
        field.setPlaceholderText(placeholder)
        field.editingFinished.connect(lambda k=key: self._commit(k))
        self._fields[key] = field
        caption = QLabel(label)
        caption.setObjectName("FieldLabel")
        form.addRow(caption, field)
        return field

    def _build_offer_group(self) -> QGroupBox:
        group = QGroupBox("Angebot")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)

        left = QFormLayout()
        left.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_field(left, "position_number", "Position")
        self._add_field(left, "material_number", "Material", "SAP-Materialnummer")
        self._add_field(left, "vendor_material_number", "Lieferantenmaterial")
        self._add_field(left, "description", "Beschreibung")
        self._add_field(left, "remarks", "Bemerkung")

        middle = QFormLayout()
        middle.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_field(middle, "quantity", "Menge")
        self._add_field(middle, "uom", "Mengeneinheit")
        self._add_field(middle, "price", "Preis")
        self._add_field(middle, "price_unit", "Preiseinheit")
        self._add_field(middle, "currency", "Waehrung")

        right = QFormLayout()
        right.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_field(right, "min_order_qty", "Mindestmenge")
        self._add_field(right, "lead_time_days", "Lieferzeit (Tage)")
        self._add_field(right, "valid_from", "Gueltig ab", "TT.MM.JJJJ")

        vendor_row = QHBoxLayout()
        self.vendor_label = QLabel("– nicht zugeordnet –")
        self.vendor_button = QPushButton("Zuordnen ...")
        self.vendor_button.clicked.connect(self._request_vendor)
        vendor_row.addWidget(self.vendor_label, 1)
        vendor_row.addWidget(self.vendor_button, 0)
        caption = QLabel("SAP-Lieferant")
        caption.setObjectName("FieldLabel")
        right.addRow(caption, vendor_row)

        self.org_label = QLabel("")
        self.org_label.setObjectName("SubHeading")
        right.addRow(QLabel(""), self.org_label)

        grid.addLayout(left, 0, 0)
        grid.addLayout(middle, 0, 1)
        grid.addLayout(right, 0, 2)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)

        source = QLabel("")
        source.setObjectName("SubHeading")
        source.setWordWrap(True)
        self.source_label = source
        grid.addWidget(source, 1, 0, 1, 3)
        return group

    def _build_sap_group(self) -> QGroupBox:
        group = QGroupBox("SAP Ist-Zustand")
        layout = QVBoxLayout(group)
        self.sap_info_label = QLabel("SAP-Daten wurden noch nicht geladen.")
        self.sap_info_label.setTextFormat(Qt.TextFormat.RichText)
        self.sap_info_label.setWordWrap(True)
        self.sap_info_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.sap_info_label.setSizePolicy(QSizePolicy.Policy.Preferred,
                                          QSizePolicy.Policy.MinimumExpanding)
        layout.addWidget(self.sap_info_label, 1)

        self.reload_button = QPushButton("SAP-Daten fuer diese Position neu lesen")
        self.reload_button.clicked.connect(
            lambda: self._position and self.requestReloadSap.emit(self._position))
        layout.addWidget(self.reload_button)
        return group

    def _build_change_group(self) -> QGroupBox:
        group = QGroupBox("Geplante Aenderung")
        layout = QVBoxLayout(group)
        self.change_label = QLabel("–")
        self.change_label.setTextFormat(Qt.TextFormat.RichText)
        self.change_label.setWordWrap(True)
        self.change_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.change_label, 1)
        return group

    def _build_document_group(self) -> QGroupBox:
        group = QGroupBox("SAP-Aktionen fuer diese Position")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(18)

        self.check_info = QCheckBox("Infosatz pflegen (ME11/ME12)")
        self.check_source = QCheckBox("Orderbuch pflegen, Lieferant aktiv (ME01)")
        self.check_contract = QCheckBox("Mengenkontrakt (ME31K)")
        self.check_order = QCheckBox("Bestellung als Abruf (ME21N)")
        for index, box in enumerate((self.check_info, self.check_source,
                                     self.check_contract, self.check_order)):
            box.stateChanged.connect(self._commit_actions)
            layout.addWidget(box, index // 2, index % 2)

        form = QFormLayout()
        self._add_field(form, "contract_quantity", "Kontrakt-Zielmenge")
        self._add_field(form, "order_quantity", "Bestellmenge (Abruf)")
        self._add_field(form, "delivery_date", "Lieferdatum", "TT.MM.JJJJ")
        layout.addLayout(form, 2, 0, 1, 2)

        self.document_label = QLabel("")
        self.document_label.setObjectName("SubHeading")
        self.document_label.setWordWrap(True)
        layout.addWidget(self.document_label, 3, 0, 1, 2)
        return group

    def _build_issue_group(self) -> QGroupBox:
        group = QGroupBox("Hinweise und Warnungen")
        layout = QVBoxLayout(group)
        self.issue_container = QWidget()
        self.issue_layout = QVBoxLayout(self.issue_container)
        self.issue_layout.setContentsMargins(0, 0, 0, 0)
        self.issue_layout.setSpacing(4)
        layout.addWidget(self.issue_container)

        self.detail_box = QPlainTextEdit()
        self.detail_box.setReadOnly(True)
        self.detail_box.setMaximumHeight(90)
        self.detail_box.setVisible(False)
        layout.addWidget(self.detail_box)
        return group

    # ------------------------------------------------------------------
    # Anzeige
    # ------------------------------------------------------------------
    def set_position(self, position: OfferPosition | None) -> None:
        self._position = position
        self._loading = True
        try:
            enabled = position is not None
            for field in self._fields.values():
                field.setEnabled(enabled)
            self.vendor_button.setEnabled(enabled)
            self.reload_button.setEnabled(enabled)
            for box in (self.check_info, self.check_source, self.check_contract,
                        self.check_order):
                box.setEnabled(enabled)

            if position is None:
                self.title_label.setText("Keine Position ausgewaehlt")
                self.status_badge.setText("")
                self.status_badge.setStyleSheet("")
                for field in self._fields.values():
                    field.clear()
                self.sap_info_label.setText("–")
                self.change_label.setText("–")
                self.source_label.setText("")
                self.document_label.setText("")
                self._clear_issues()
                return

            self.title_label.setText(position.summary_line())
            colour, background, symbol = STATUS_STYLE.get(
                position.status, (Colors.GREY, Colors.GREY_BG, "○"))
            self.status_badge.setText(f"{symbol}  {position.status.label}")
            self.status_badge.setStyleSheet(badge_style(colour, background))

            self._fill_fields(position)
            self._fill_vendor(position)
            self._fill_sap(position)
            self._fill_change(position)
            self._fill_actions(position)
            self._fill_issues(position)
        finally:
            self._loading = False

    def _fill_fields(self, position: OfferPosition) -> None:
        values = {
            "position_number": position.position_number,
            "material_number": position.material_number,
            "vendor_material_number": position.vendor_material_number,
            "description": position.description,
            "remarks": position.remarks,
            "quantity": format_decimal(position.quantity, 3),
            "uom": position.uom,
            "price": format_decimal(position.price, 4).rstrip("0").rstrip(",")
                     if position.price is not None else "",
            "price_unit": str(position.price_unit or ""),
            "currency": position.currency,
            "min_order_qty": format_decimal(position.min_order_qty, 3),
            "lead_time_days": str(position.lead_time_days or ""),
            "valid_from": format_date(position.valid_from),
            "contract_quantity": format_decimal(position.contract_quantity, 3),
            "order_quantity": format_decimal(position.order_quantity, 3),
            "delivery_date": format_date(position.delivery_date),
        }
        for key, field in self._fields.items():
            field.setText(values.get(key, ""))
            field.mark_origin(position.field_origins.get(key, FieldOrigin.EXTRACTED
                                                         if values.get(key) else
                                                         FieldOrigin.MISSING))

        source = []
        if position.source_kind:
            source.append(f"Quelle: {position.source_kind.label}")
        if position.source_hint:
            source.append(position.source_hint)
        if position.raw_text:
            source.append(f"Originaltext: „{position.raw_text[:160]}“")
        self.source_label.setText("   •   ".join(source))

    def _fill_vendor(self, position: OfferPosition) -> None:
        if position.vendor_number:
            text = position.vendor_number
            if position.vendor_exists is False:
                text += "  (in SAP nicht gefunden)"
            self.vendor_label.setText(text)
            self.vendor_label.setStyleSheet(
                f"color: {Colors.RED};" if position.vendor_exists is False else "")
        else:
            self.vendor_label.setText("– nicht zugeordnet –")
            self.vendor_label.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")
        self.org_label.setText(
            f"Einkaufsorg. {position.purchasing_org or '–'}   •   Werk {position.plant or '–'}")

    def _fill_sap(self, position: OfferPosition) -> None:
        rows: list[str] = []
        record = position.sap_info_record
        if record is None or not record.was_read:
            rows.append("<i>Infosatz: noch nicht gelesen</i>")
        elif record.read_error:
            rows.append(f"<span style='color:{Colors.RED}'>Infosatz: {record.read_error}</span>")
        else:
            for key, value in record.summary().items():
                rows.append(f"<b>{key}:</b> {value}")

        rows.append("<hr>")
        source_list = position.sap_source_list
        if source_list is None or not source_list.was_read:
            rows.append("<i>Orderbuch: noch nicht gelesen</i>")
        elif source_list.read_error:
            rows.append(f"<span style='color:{Colors.RED}'>Orderbuch: {source_list.read_error}</span>")
        elif not source_list.exists:
            rows.append("<b>Orderbuch:</b> kein Eintrag vorhanden")
        else:
            rows.append(f"<b>Orderbuch:</b> {len(source_list.entries)} Eintrag/Eintraege")
            for entry in source_list.entries[:6]:
                mark = "→ " if entry.vendor_number == position.vendor_number else "&nbsp;&nbsp;&nbsp;"
                rows.append(f"{mark}{entry.display()}")

        if position.material_exists is False:
            rows.insert(0, f"<span style='color:{Colors.RED}'><b>Material in SAP nicht "
                           f"vorhanden</b></span>")
        elif position.sap_material_description:
            rows.insert(0, f"<b>Materialtext (SAP):</b> {position.sap_material_description}")

        self.sap_info_label.setText("<br>".join(rows))

    def _fill_change(self, position: OfferPosition) -> None:
        if self.comparison is not None:
            try:
                data = self.comparison.describe_change(position)
                rows = [f"<b>{key}:</b> {value}" for key, value in data.items()]
                self.change_label.setText("<br>".join(rows) or "–")
                return
            except Exception as exc:  # noqa: BLE001 - Anzeige darf nie abstuerzen
                logger.debug("describe_change nicht verfuegbar: %s", exc)

        rows: list[str] = []
        old = position.old_price
        rows.append(f"<b>Alter Preis:</b> {format_decimal(old) + ' ' + (position.sap_info_record.currency if position.sap_info_record else '') if old is not None else '–'}")
        rows.append(f"<b>Neuer Preis:</b> "
                    f"{format_decimal(position.price)} {position.currency}"
                    if position.price is not None else "<b>Neuer Preis:</b> –")
        percent = position.price_change_percent
        if percent is not None:
            colour = Colors.RED if abs(percent) >= 30 else (
                Colors.AMBER if abs(percent) >= 10 else Colors.GREEN)
            sign = "+" if percent > 0 else ""
            rows.append(f"<b>Aenderung:</b> <span style='color:{colour}'>"
                        f"{sign}{format_decimal(percent)} %</span>")
        rows.append(f"<b>Preiseinheit:</b> {position.price_unit or '–'} {position.uom}")
        rows.append(f"<b>Gueltig ab:</b> {format_date(position.valid_from) or '–'}")
        rows.append(f"<b>Infosatz:</b> {position.info_record_action.label}")
        rows.append(f"<b>Orderbuch:</b> {position.source_list_action.label}")
        self.change_label.setText("<br>".join(rows))

    def _fill_actions(self, position: OfferPosition) -> None:
        self.check_info.setChecked(position.do_info_record)
        self.check_source.setChecked(position.do_source_list)
        self.check_contract.setChecked(position.do_contract)
        self.check_order.setChecked(position.do_purchase_order)

        created = []
        if position.created_info_record:
            created.append(f"Infosatz {position.created_info_record}")
        if position.created_contract:
            created.append(f"Kontrakt {position.created_contract}")
        if position.created_purchase_order:
            created.append(f"Bestellung {position.created_purchase_order}")
        self.document_label.setText("Angelegt: " + ", ".join(created) if created else "")

    def _clear_issues(self) -> None:
        while self.issue_layout.count():
            item = self.issue_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.detail_box.setVisible(False)
        self.detail_box.clear()

    def _fill_issues(self, position: OfferPosition) -> None:
        self._clear_issues()
        if not len(position.issues):
            label = QLabel("Keine Hinweise – Position ist unauffaellig.")
            label.setObjectName("SubHeading")
            self.issue_layout.addWidget(label)
            return

        details: list[str] = []
        for issue in position.issues:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)

            colour = SEVERITY_COLOR.get(issue.severity, Colors.TEXT_MUTED)
            marker = QLabel("■")
            marker.setStyleSheet(f"color: {colour};")
            text = QLabel(issue.message + (" (quittiert)" if issue.acknowledged else ""))
            text.setWordWrap(True)
            if issue.acknowledged:
                text.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
            layout.addWidget(marker, 0)
            layout.addWidget(text, 1)

            if issue.blocking and not issue.acknowledged and \
                    issue.severity is not IssueSeverity.ERROR:
                button = QPushButton("Trotzdem freigeben")
                button.clicked.connect(
                    lambda _=False, code=issue.code: self._acknowledge(code))
                layout.addWidget(button, 0)
            self.issue_layout.addWidget(row)

            if issue.detail:
                details.append(f"[{issue.code}] {issue.detail}")

        if details:
            self.detail_box.setPlainText("\n".join(details))
            self.detail_box.setVisible(True)

    # ------------------------------------------------------------------
    # Bearbeitung
    # ------------------------------------------------------------------
    def _acknowledge(self, code: str) -> None:
        if self._position is None:
            return
        for issue in self._position.issues:
            if issue.code == code:
                issue.acknowledged = True
        self.issueAcknowledged.emit(self._position, code)
        self.positionChanged.emit(self._position)

    def _request_vendor(self) -> None:
        if self._position is not None:
            self.requestVendorAssignment.emit(self._position)

    def _commit_actions(self) -> None:
        if self._loading or self._position is None:
            return
        position = self._position
        position.do_info_record = self.check_info.isChecked()
        position.do_source_list = self.check_source.isChecked()
        position.do_contract = self.check_contract.isChecked()
        position.do_purchase_order = self.check_order.isChecked()
        self.positionChanged.emit(position)

    def _commit(self, key: str) -> None:
        """Ein Feld uebernehmen.  Ungueltige Eingaben werden zurueckgesetzt."""
        if self._loading or self._position is None:
            return
        position = self._position
        field = self._fields[key]
        text = field.text().strip()

        decimal_fields = {"quantity", "price", "min_order_qty", "contract_quantity",
                          "order_quantity"}
        int_fields = {"price_unit", "lead_time_days"}
        date_fields = {"valid_from", "delivery_date"}

        try:
            if key in decimal_fields:
                value: Decimal | None = None
                if text:
                    value = parse_decimal(text)
                    if value is None:
                        self._reject(field, "Bitte eine Zahl eingeben, z. B. 12,85")
                        return
                setattr(position, key, value)
            elif key in int_fields:
                if text:
                    number = parse_int(text)
                    if number is None or number <= 0:
                        self._reject(field, "Bitte eine ganze Zahl groesser 0 eingeben")
                        return
                    setattr(position, key, number)
                else:
                    setattr(position, key, None)
            elif key in date_fields:
                if text:
                    day: date | None = parse_date(text)
                    if day is None:
                        self._reject(field, "Bitte ein Datum im Format TT.MM.JJJJ eingeben")
                        return
                    setattr(position, key, day)
                else:
                    setattr(position, key, None)
            elif key == "material_number":
                position.material_number = normalize_material_number(text)
            elif key == "uom":
                position.uom = normalize_uom(text)
            elif key == "currency":
                position.currency = text.upper()[:5]
            else:
                setattr(position, key, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Eingabe %s=%r konnte nicht uebernommen werden: %s", key, text, exc)
            self._reject(field, "Die Eingabe konnte nicht uebernommen werden")
            return

        position.mark_manual(key)
        field.mark_origin(FieldOrigin.MANUAL)
        self.positionChanged.emit(position)

    def _reject(self, field: _Field, message: str) -> None:
        field.setStyleSheet(f"QLineEdit {{ background: {Colors.RED_BG}; }}")
        field.setToolTip(message)
        self.set_position(self._position)   # Originalwert wiederherstellen

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Anzeige nach externen Aenderungen auffrischen."""
        self.set_position(self._position)

    @property
    def position(self) -> OfferPosition | None:
        return self._position
