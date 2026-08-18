"""Detailansicht einer Angebotsposition.

Gliederung
----------
Ganz oben steht, worum es geht (Position, Status) und was passieren soll --
die vier SAP-Aktionen als eine Zeile Ankreuzfelder.  Das ist die eigentliche
Entscheidung und deshalb immer sichtbar.

Alles Weitere liegt in Registerkarten, damit nicht dreissig Felder
gleichzeitig um Aufmerksamkeit konkurrieren:

    Angebot     was im Angebot steht (editierbar)
    SAP heute   was aktuell in SAP steht (nur lesend)
    Aenderung   was sich dadurch aendert
    Hinweise    Warnungen, mit Quittiermoeglichkeit
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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
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
    """Eingabefeld, das seine Herkunft erkennen laesst."""

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

    positionChanged = Signal(object)
    requestVendorAssignment = Signal(object)
    requestVendorMaster = Signal(object)
    requestReloadSap = Signal(object)
    issueAcknowledged = Signal(object, str)

    def __init__(self, comparison_service=None, parent=None, settings=None) -> None:
        super().__init__(parent)
        self.comparison = comparison_service
        #: Die Anlagen-Haken wirken auf ``settings.attachments`` -- die Anlage
        #: ist eine Einstellung des Vorgangs, keine Eigenschaft der Zeile.
        self.settings = settings
        self._position: OfferPosition | None = None
        self._loading = False
        self._fields: dict[str, _Field] = {}

        self._build()
        self._fill_attachments()
        self.set_position(None)

    # ==================================================================
    # Aufbau
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_offer_tab(), "Angebot")
        self.tabs.addTab(self._build_sap_tab(), "SAP heute")
        self.tabs.addTab(self._build_change_tab(), "Aenderung")
        self.tabs.addTab(self._build_issue_tab(), "Hinweise")
        outer.addWidget(self.tabs, 1)

    def _build_header(self) -> QFrame:
        """Titel, Status und die Aktionen -- immer sichtbar.

        Die Aktionen sind in zwei Gruppen getrennt, weil sie fachlich zwei
        verschiedene Dinge sind:

            Stammdaten        betreffen Lieferant bzw. Material als Ganzes
            Einkaufsvorgang   sind Vorgaenge im Einkaufsbeleg-Umfeld

        Innerhalb des Einkaufsvorgangs bleibt die Reihenfolge, wie sie ist --
        sie entspricht der festen Verarbeitungsreihenfolge.
        """
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        zeile = QHBoxLayout()
        self.title_label = QLabel("Keine Position ausgewaehlt")
        self.title_label.setObjectName("Heading")
        self.status_badge = QLabel("")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zeile.addWidget(self.title_label, 1)
        zeile.addWidget(self.status_badge, 0)
        layout.addLayout(zeile)

        # -- Gruppe 1: Stammdaten -------------------------------------
        stammdaten = QHBoxLayout()
        stammdaten.setSpacing(14)
        self.master_group_label = QLabel("Stammdaten")
        self.master_group_label.setObjectName("FieldLabel")
        stammdaten.addWidget(self.master_group_label)

        # Die Materialpruefung (MM03) ist reines Lesen und geschieht bereits
        # beim "SAP-Daten laden".  Sie darf deshalb NICHT als Schreibaktion
        # erscheinen -- hier steht nur ihr Ergebnis.
        self.material_state_label = QLabel("Material: noch nicht geprueft")
        self.material_state_label.setToolTip(
            "Ergebnis der Materialpruefung (MM03). Reines Lesen -- es wird "
            "dabei nichts in SAP geaendert.")
        stammdaten.addWidget(self.material_state_label)

        # Die Lieferantenpflege (XK02) gilt je LIEFERANT, nicht je Position.
        # Ein Haken an jeder Zeile wuerde denselben Stammsatz n-mal anfassen --
        # deshalb bewusst eine Schaltflaeche statt eines Ankreuzfelds.
        self.vendor_master_button = QPushButton("Stammdaten dieses Lieferanten pflegen ...")
        self.vendor_master_button.setToolTip(
            "Aendert den Lieferantenstammsatz (XK02). Gilt einmal je Lieferant, "
            "nicht je Position.")
        self.vendor_master_button.clicked.connect(self._request_vendor_master)
        stammdaten.addWidget(self.vendor_master_button)
        stammdaten.addStretch(1)
        layout.addLayout(stammdaten)

        trenner = QFrame()
        trenner.setFrameShape(QFrame.Shape.HLine)
        trenner.setFrameShadow(QFrame.Shadow.Plain)
        trenner.setStyleSheet(f"color: {Colors.BORDER};")
        layout.addWidget(trenner)

        # -- Gruppe 2: Einkaufsvorgang --------------------------------
        vorgang = QGridLayout()
        vorgang.setHorizontalSpacing(16)
        vorgang.setVerticalSpacing(2)
        self.process_group_label = QLabel("Einkaufsvorgang")
        self.process_group_label.setObjectName("FieldLabel")
        vorgang.addWidget(self.process_group_label, 0, 0)

        self.check_info = QCheckBox("Infosatz")
        self.check_info.setToolTip("ME11 / ME12")
        self.check_source = QCheckBox("Orderbuch")
        self.check_source.setToolTip("ME01 – Lieferant aktiv setzen")
        self.check_contract = QCheckBox("Kontrakt")
        self.check_contract.setToolTip("ME31K – Mengenkontrakt")
        self.check_order = QCheckBox("Bestellung")
        self.check_order.setToolTip("ME21N – Abruf aus dem Kontrakt")

        self.attach_info = self._attachment_box(
            "Infosatz", "Das Angebot haengt nach dem Sichern am Infosatz.")
        self.attach_source = self._attachment_box(
            "Orderbuch", "Viele Systeme lassen am Orderbuch keine Anlage zu.")
        self.attach_contract = self._attachment_box(
            "Kontrakt", "Das Angebot haengt nach dem Sichern am Mengenkontrakt.")
        self.attach_order = self._attachment_box(
            "Bestellung", "Das Angebot haengt nach dem Sichern an der Bestellung.")

        paare = ((self.check_info, self.attach_info),
                 (self.check_source, self.attach_source),
                 (self.check_contract, self.attach_contract),
                 (self.check_order, self.attach_order))
        for spalte, (aktion, anlage) in enumerate(paare, start=1):
            aktion.stateChanged.connect(self._commit_actions)
            anlage.stateChanged.connect(self._commit_attachments)
            vorgang.addWidget(aktion, 0, spalte)
            vorgang.addWidget(anlage, 1, spalte)
        vorgang.setColumnStretch(len(paare) + 1, 1)

        self.document_label = QLabel("")
        self.document_label.setObjectName("SubHeading")
        vorgang.addWidget(self.document_label, 0, len(paare) + 1,
                          Qt.AlignmentFlag.AlignRight)
        layout.addLayout(vorgang)
        return card

    def _attachment_box(self, aktion: str, hinweis: str) -> QCheckBox:
        """Kleines Ankreuzfeld "+ Beleg anhaengen" unter einer Aktion."""
        box = QCheckBox("+ Beleg anhaengen")
        box.setToolTip(f"{aktion}: {hinweis}\n\nAngehaengt wird das eingelesene "
                       f"Angebot (Datei bzw. Mailanhang). Gibt es keine Datei, "
                       f"wird das gemeldet -- es wird nichts erfunden.")
        box.setStyleSheet(f"QCheckBox {{ color: {Colors.TEXT_MUTED}; }}")
        return box

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

    def _build_offer_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 10, 12, 10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(6)

        links = QFormLayout()
        links.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_field(links, "material_number", "Material", "SAP-Materialnummer")
        self._add_field(links, "description", "Bezeichnung")
        self._add_field(links, "vendor_material_number", "Lieferantenmaterial")
        self._add_field(links, "position_number", "Position")
        self._add_field(links, "remarks", "Bemerkung")

        rechts = QFormLayout()
        rechts.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_field(rechts, "price", "Preis")
        self._add_field(rechts, "price_unit", "Preiseinheit")
        self._add_field(rechts, "currency", "Waehrung")
        self._add_field(rechts, "quantity", "Menge")
        self._add_field(rechts, "uom", "Mengeneinheit")
        self._add_field(rechts, "valid_from", "Gueltig ab", "TT.MM.JJJJ")

        grid.addLayout(links, 0, 0)
        grid.addLayout(rechts, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        outer.addLayout(grid)

        # Lieferant als eigene, deutlich sichtbare Zeile
        lieferant = QHBoxLayout()
        beschriftung = QLabel("SAP-Lieferant:")
        beschriftung.setObjectName("FieldLabel")
        self.vendor_label = QLabel("– nicht zugeordnet –")
        self.vendor_button = QPushButton("Zuordnen ...")
        self.vendor_button.clicked.connect(self._request_vendor)
        self.org_label = QLabel("")
        self.org_label.setObjectName("SubHeading")
        lieferant.addWidget(beschriftung)
        lieferant.addWidget(self.vendor_label)
        lieferant.addWidget(self.vendor_button)
        lieferant.addSpacing(16)
        lieferant.addWidget(self.org_label)
        lieferant.addStretch(1)
        outer.addLayout(lieferant)

        # Selten gebrauchte Felder ausklappbar
        self.more_button = QPushButton("Weitere Felder anzeigen")
        self.more_button.setCheckable(True)
        self.more_button.toggled.connect(self._toggle_more)
        outer.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.more_widget = QWidget()
        more_grid = QGridLayout(self.more_widget)
        more_grid.setContentsMargins(0, 0, 0, 0)
        more_grid.setHorizontalSpacing(28)

        links2 = QFormLayout()
        self._add_field(links2, "min_order_qty", "Mindestmenge")
        self._add_field(links2, "lead_time_days", "Lieferzeit (Tage)")

        rechts2 = QFormLayout()
        self._add_field(rechts2, "contract_quantity", "Kontrakt-Zielmenge")
        self._add_field(rechts2, "order_quantity", "Bestellmenge (Abruf)")
        self._add_field(rechts2, "delivery_date", "Lieferdatum", "TT.MM.JJJJ")

        more_grid.addLayout(links2, 0, 0)
        more_grid.addLayout(rechts2, 0, 1)
        more_grid.setColumnStretch(0, 3)
        more_grid.setColumnStretch(1, 2)
        self.more_widget.setVisible(False)
        outer.addWidget(self.more_widget)

        self.source_label = QLabel("")
        self.source_label.setObjectName("SubHeading")
        self.source_label.setWordWrap(True)
        outer.addWidget(self.source_label)
        outer.addStretch(1)
        return page

    def _toggle_more(self, checked: bool) -> None:
        self.more_widget.setVisible(checked)
        self.more_button.setText("Weitere Felder ausblenden" if checked
                                 else "Weitere Felder anzeigen")

    def _build_sap_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sap_info_label = QLabel("SAP-Daten wurden noch nicht geladen.")
        self.sap_info_label.setTextFormat(Qt.TextFormat.RichText)
        self.sap_info_label.setWordWrap(True)
        self.sap_info_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self.sap_info_label)
        layout.addWidget(scroll, 1)

        self.reload_button = QPushButton("SAP-Daten neu lesen")
        self.reload_button.clicked.connect(
            lambda: self._position and self.requestReloadSap.emit(self._position))
        layout.addWidget(self.reload_button, 0, Qt.AlignmentFlag.AlignLeft)
        return page

    def _build_change_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        self.change_label = QLabel("–")
        self.change_label.setTextFormat(Qt.TextFormat.RichText)
        self.change_label.setWordWrap(True)
        self.change_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.change_label, 1)
        return page

    def _build_issue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)

        self.issue_container = QWidget()
        self.issue_layout = QVBoxLayout(self.issue_container)
        self.issue_layout.setContentsMargins(0, 0, 0, 0)
        self.issue_layout.setSpacing(4)
        layout.addWidget(self.issue_container)

        self.detail_box = QPlainTextEdit()
        self.detail_box.setReadOnly(True)
        self.detail_box.setMaximumHeight(110)
        self.detail_box.setVisible(False)
        layout.addWidget(self.detail_box)
        layout.addStretch(1)
        return page

    # ==================================================================
    # Anzeige
    # ==================================================================
    def set_position(self, position: OfferPosition | None) -> None:
        self._position = position
        self._loading = True
        try:
            aktiv = position is not None
            for field in self._fields.values():
                field.setEnabled(aktiv)
            self.vendor_button.setEnabled(aktiv)
            # Die Lieferantenpflege gilt je Lieferant und haengt deshalb NICHT
            # daran, ob gerade eine Position markiert ist.
            self.reload_button.setEnabled(aktiv)
            for box in (self.check_info, self.check_source, self.check_contract,
                        self.check_order):
                box.setEnabled(aktiv)

            if position is None:
                self.material_state_label.setText("Material: keine Position ausgewaehlt")
                self.material_state_label.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
                self.title_label.setText("Keine Position ausgewaehlt")
                self.status_badge.setText("")
                self.status_badge.setStyleSheet("")
                for field in self._fields.values():
                    field.clear()
                self.sap_info_label.setText("–")
                self.change_label.setText("–")
                self.source_label.setText("")
                self.document_label.setText("")
                self.tabs.setTabText(3, "Hinweise")
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
            self._fill_material_state(position)
            self._fill_issues(position)
        finally:
            self._loading = False

    def _fill_fields(self, position: OfferPosition) -> None:
        werte = {
            "position_number": position.position_number,
            "material_number": position.material_number,
            "vendor_material_number": position.vendor_material_number,
            "description": position.description,
            "remarks": position.remarks,
            "quantity": format_decimal(position.quantity, 3),
            "uom": position.uom,
            "price": (format_decimal(position.price, 4).rstrip("0").rstrip(",")
                      if position.price is not None else ""),
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
            field.setText(werte.get(key, ""))
            field.mark_origin(position.field_origins.get(
                key, FieldOrigin.EXTRACTED if werte.get(key) else FieldOrigin.MISSING))

        quelle = []
        if position.source_kind:
            quelle.append(f"Quelle: {position.source_kind.label}")
        if position.source_hint:
            quelle.append(position.source_hint)
        self.source_label.setText("   •   ".join(quelle))
        self.source_label.setToolTip(position.raw_text[:400] if position.raw_text else "")

    def _fill_vendor(self, position: OfferPosition) -> None:
        if position.vendor_number:
            text = position.vendor_number
            if position.vendor_exists is False:
                text += "  (in SAP nicht gefunden)"
            self.vendor_label.setText(text)
            self.vendor_label.setStyleSheet(
                f"color: {Colors.RED};" if position.vendor_exists is False
                else f"color: {Colors.GREEN}; font-weight: 600;")
        else:
            self.vendor_label.setText("– nicht zugeordnet –")
            self.vendor_label.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")
        self.org_label.setText(
            f"EKorg {position.purchasing_org or '–'}  •  Werk {position.plant or '–'}")

    def _fill_sap(self, position: OfferPosition) -> None:
        zeilen: list[str] = []
        record = position.sap_info_record
        if record is None or not record.was_read:
            zeilen.append("<i>Infosatz: noch nicht gelesen</i>")
        elif record.read_error:
            zeilen.append(f"<span style='color:{Colors.RED}'>Infosatz: "
                          f"{record.read_error}</span>")
        else:
            for key, value in record.summary().items():
                zeilen.append(f"<b>{key}:</b> {value}")

        zeilen.append("<hr>")
        source_list = position.sap_source_list
        if source_list is None or not source_list.was_read:
            zeilen.append("<i>Orderbuch: noch nicht gelesen</i>")
        elif source_list.read_error:
            zeilen.append(f"<span style='color:{Colors.RED}'>Orderbuch: "
                          f"{source_list.read_error}</span>")
        elif not source_list.exists:
            zeilen.append("<b>Orderbuch:</b> kein Eintrag vorhanden")
        else:
            zeilen.append(f"<b>Orderbuch:</b> {len(source_list.entries)} Eintrag/Eintraege")
            for entry in source_list.entries[:6]:
                marke = ("→ " if entry.vendor_number == position.vendor_number
                         else "&nbsp;&nbsp;&nbsp;")
                zeilen.append(f"{marke}{entry.display()}")

        if position.material_exists is False:
            zeilen.insert(0, f"<span style='color:{Colors.RED}'><b>Material in SAP "
                             f"nicht vorhanden</b></span>")
        elif position.sap_material_description:
            zeilen.insert(0, f"<b>Materialtext (SAP):</b> "
                             f"{position.sap_material_description}")
        self.sap_info_label.setText("<br>".join(zeilen))

    def _fill_change(self, position: OfferPosition) -> None:
        if self.comparison is not None:
            try:
                daten = self.comparison.describe_change(position)
                self.change_label.setText(
                    "<br>".join(f"<b>{k}:</b> {v}" for k, v in daten.items()) or "–")
                return
            except Exception as exc:  # noqa: BLE001 - Anzeige darf nie abstuerzen
                logger.debug("describe_change nicht verfuegbar: %s", exc)

        zeilen: list[str] = []
        alt = position.old_price
        waehrung = position.sap_info_record.currency if position.sap_info_record else ""
        zeilen.append(f"<b>Alter Preis:</b> "
                      f"{format_decimal(alt) + ' ' + waehrung if alt is not None else '–'}")
        zeilen.append(f"<b>Neuer Preis:</b> "
                      + (f"{format_decimal(position.price)} {position.currency}"
                         if position.price is not None else "–"))
        prozent = position.price_change_percent
        if prozent is not None:
            farbe = (Colors.RED if abs(prozent) >= 30
                     else Colors.AMBER if abs(prozent) >= 10 else Colors.GREEN)
            vorzeichen = "+" if prozent > 0 else ""
            zeilen.append(f"<b>Aenderung:</b> <span style='color:{farbe}'>"
                          f"{vorzeichen}{format_decimal(prozent)} %</span>")
        zeilen.append(f"<b>Preiseinheit:</b> {position.price_unit or '–'} {position.uom}")
        zeilen.append(f"<b>Gueltig ab:</b> {format_date(position.valid_from) or '–'}")
        zeilen.append(f"<b>Infosatz:</b> {position.info_record_action.label}")
        zeilen.append(f"<b>Orderbuch:</b> {position.source_list_action.label}")
        self.change_label.setText("<br>".join(zeilen))

    def _attachment_settings(self):
        """Anlagen-Einstellungen -- notfalls die Auslieferungsvorgaben."""
        einstellungen = getattr(self.settings, "attachments", None)
        if einstellungen is None:
            from ..config.settings import AttachmentSettings
            einstellungen = AttachmentSettings()
            if self.settings is not None:
                self.settings.attachments = einstellungen
        return einstellungen

    def _fill_attachments(self) -> None:
        einstellungen = self._attachment_settings()
        self._loading = True
        try:
            self.attach_info.setChecked(einstellungen.attach_to_info_record)
            self.attach_source.setChecked(einstellungen.attach_to_source_list)
            self.attach_contract.setChecked(einstellungen.attach_to_contract)
            self.attach_order.setChecked(einstellungen.attach_to_purchase_order)
        finally:
            self._loading = False

    def _fill_material_state(self, position: OfferPosition) -> None:
        """Zustandsanzeige der Materialpruefung -- niemals eine Schreibaktion."""
        if not position.material_number:
            text, farbe = "Material: keine Nummer erfasst", Colors.TEXT_MUTED
        elif position.material_exists is None:
            text, farbe = "Material: noch nicht geprueft", Colors.TEXT_MUTED
        elif position.material_exists:
            text, farbe = "Material vorhanden", Colors.GREEN
        else:
            text, farbe = "Material nicht vorhanden", Colors.RED
        self.material_state_label.setText(text)
        self.material_state_label.setStyleSheet(f"color: {farbe};")

    def _fill_actions(self, position: OfferPosition) -> None:
        self.check_info.setChecked(position.do_info_record)
        self.check_source.setChecked(position.do_source_list)
        self.check_contract.setChecked(position.do_contract)
        self.check_order.setChecked(position.do_purchase_order)

        angelegt = []
        if position.created_info_record:
            angelegt.append(f"Infosatz {position.created_info_record}")
        if position.created_contract:
            angelegt.append(f"Kontrakt {position.created_contract}")
        if position.created_purchase_order:
            angelegt.append(f"Bestellung {position.created_purchase_order}")
        self.document_label.setText("Angelegt: " + ", ".join(angelegt) if angelegt else "")

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
        anzahl = len(position.issues)
        schwerste = position.issues.max_severity
        titel = "Hinweise" if not anzahl else f"Hinweise ({anzahl})"
        self.tabs.setTabText(3, titel)
        if schwerste is not None:
            self.tabs.setTabToolTip(3, f"Hoechste Stufe: {schwerste.label}")

        if not anzahl:
            label = QLabel("Keine Hinweise – Position ist unauffaellig.")
            label.setObjectName("SubHeading")
            self.issue_layout.addWidget(label)
            return

        details: list[str] = []
        for issue in position.issues:
            zeile = QWidget()
            layout = QHBoxLayout(zeile)
            layout.setContentsMargins(0, 0, 0, 0)

            farbe = SEVERITY_COLOR.get(issue.severity, Colors.TEXT_MUTED)
            marker = QLabel("■")
            marker.setStyleSheet(f"color: {farbe};")
            text = QLabel(issue.message + (" (quittiert)" if issue.acknowledged else ""))
            text.setWordWrap(True)
            if issue.acknowledged:
                text.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
            layout.addWidget(marker, 0)
            layout.addWidget(text, 1)

            if (issue.blocking and not issue.acknowledged
                    and issue.severity is not IssueSeverity.ERROR):
                button = QPushButton("Trotzdem freigeben")
                button.clicked.connect(
                    lambda _=False, code=issue.code: self._acknowledge(code))
                layout.addWidget(button, 0)
            self.issue_layout.addWidget(zeile)

            if issue.detail:
                details.append(f"[{issue.code}] {issue.detail}")

        if details:
            self.detail_box.setPlainText("\n".join(details))
            self.detail_box.setVisible(True)

    # ==================================================================
    # Bearbeitung
    # ==================================================================
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

    def _request_vendor_master(self) -> None:
        """Lieferantenstammpflege anstossen -- gilt je Lieferant."""
        self.requestVendorMaster.emit(self._position)

    def _commit_attachments(self) -> None:
        """Anlagen-Haken in die Einstellungen uebernehmen.

        Bewusst NICHT an die Position gehaengt: ob das Angebot als Anlage
        mitgeht, ist eine Entscheidung ueber den Vorgang, nicht ueber die
        einzelne Zeile.
        """
        if self._loading:
            return
        einstellungen = self._attachment_settings()
        einstellungen.attach_to_info_record = self.attach_info.isChecked()
        einstellungen.attach_to_source_list = self.attach_source.isChecked()
        einstellungen.attach_to_contract = self.attach_contract.isChecked()
        einstellungen.attach_to_purchase_order = self.attach_order.isChecked()

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

        dezimal = {"quantity", "price", "min_order_qty", "contract_quantity",
                   "order_quantity"}
        ganzzahl = {"price_unit", "lead_time_days"}
        datum = {"valid_from", "delivery_date"}

        try:
            if key in dezimal:
                wert: Decimal | None = None
                if text:
                    wert = parse_decimal(text)
                    if wert is None:
                        self._reject(field, "Bitte eine Zahl eingeben, z. B. 12,85")
                        return
                setattr(position, key, wert)
            elif key in ganzzahl:
                if text:
                    zahl = parse_int(text)
                    if zahl is None or zahl <= 0:
                        self._reject(field, "Bitte eine ganze Zahl groesser 0 eingeben")
                        return
                    setattr(position, key, zahl)
                else:
                    setattr(position, key, None)
            elif key in datum:
                if text:
                    tag: date | None = parse_date(text)
                    if tag is None:
                        self._reject(field, "Bitte ein Datum im Format TT.MM.JJJJ eingeben")
                        return
                    setattr(position, key, tag)
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
            logger.warning("Eingabe %s=%r nicht uebernommen: %s", key, text, exc)
            self._reject(field, "Die Eingabe konnte nicht uebernommen werden")
            return

        position.mark_manual(key)
        field.mark_origin(FieldOrigin.MANUAL)
        self.positionChanged.emit(position)

    def _reject(self, field: _Field, message: str) -> None:
        field.setStyleSheet(f"QLineEdit {{ background: {Colors.RED_BG}; }}")
        field.setToolTip(message)
        self.set_position(self._position)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self.set_position(self._position)

    @property
    def position(self) -> OfferPosition | None:
        return self._position
