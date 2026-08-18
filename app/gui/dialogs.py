"""Dialoge der Anwendung.

Grundsatz: so wenige Dialoge wie moeglich.  Es gibt genau vier Stellen, an
denen ein Dialog wirklich hilft:

* Vorschau vor der Verarbeitung (bewusste Freigabe)
* Ergebnisuebersicht danach
* Fehlerdetails (technisches nur auf Wunsch aufklappbar)
* Zuordnungen, die der Anwender entscheiden muss (Lieferant, Session)
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..models.offer_position import OfferPosition
from ..models.results import BatchSummary
from ..models.sap_vendor import VendorMasterPlan
from .style import RESULT_COLOR, Colors, badge_style

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vorschau
# ---------------------------------------------------------------------------

class PreviewDialog(QDialog):
    """Was passiert gleich in SAP?  Letzte bewusste Freigabe."""

    def __init__(self, preview, dry_run: bool, mock: bool, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Verarbeitung vorbereiten")
        self.setMinimumWidth(640)
        self._build(dry_run, mock)

    def _build(self, dry_run: bool, mock: bool) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        heading = QLabel(self._heading_text())
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        badges = QHBoxLayout()
        if dry_run:
            badge = QLabel("DRY RUN – es wird nichts geschrieben")
            badge.setStyleSheet(badge_style(Colors.BLUE, Colors.BLUE_BG))
            badges.addWidget(badge)
        if mock:
            badge = QLabel("Testsystem (Mock-SAP)")
            badge.setStyleSheet(badge_style(Colors.BLUE, Colors.BLUE_BG))
            badges.addWidget(badge)
        if not dry_run and not mock:
            badge = QLabel("ECHTES SAP – es wird geschrieben")
            badge.setStyleSheet(badge_style(Colors.RED, Colors.RED_BG))
            badges.addWidget(badge)
        badge = QLabel("Kein Nachrichtenversand an Lieferanten")
        badge.setStyleSheet(badge_style(Colors.GREEN, Colors.GREEN_BG))
        badges.addWidget(badge)
        badges.addStretch(1)
        layout.addLayout(badges)

        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setFont(QFont("Consolas", 10))
        body.setPlainText("\n".join(self._lines()))
        body.setMinimumHeight(240)
        layout.addWidget(body, 1)

        order = QLabel("Reihenfolge: " + " → ".join(self._chain_order()))
        order.setObjectName("SubHeading")
        layout.addWidget(order)

        self.confirm_box: QCheckBox | None = None
        blocking = list(getattr(self.preview, "blocking", []) or [])
        if blocking:
            warning = QLabel("Folgende Punkte sollten Sie vorher pruefen:\n• "
                             + "\n• ".join(blocking[:8]))
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {Colors.AMBER};")
            layout.addWidget(warning)
            self.confirm_box = QCheckBox("Ich habe die Hinweise geprueft und moechte fortfahren")
            layout.addWidget(self.confirm_box)

        buttons = QDialogButtonBox()
        self.start_button = QPushButton("Simulation starten" if dry_run
                                        else "Verarbeitung starten")
        self.start_button.setObjectName("Primary")
        self.start_button.setDefault(True)
        cancel = QPushButton("Abbrechen")
        buttons.addButton(self.start_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self.confirm_box is not None:
            self.start_button.setEnabled(False)
            self.confirm_box.toggled.connect(self.start_button.setEnabled)

    def _heading_text(self) -> str:
        count = getattr(self.preview, "positions_selected", 0)
        return f"{count} Position(en) ausgewaehlt"

    def _lines(self) -> list[str]:
        lines = list(getattr(self.preview, "lines", []) or [])
        if lines:
            return lines
        return ["Keine Detailangaben verfuegbar."]

    def _chain_order(self) -> list[str]:
        order = list(getattr(self.preview, "chain_order", []) or [])
        labels = {"info_record": "Infosatz", "contract": "Mengenkontrakt",
                  "source_list": "Orderbuch", "purchase_order": "Bestellung"}
        return [labels.get(step, step) for step in order] or ["Infosatz", "Mengenkontrakt",
                                                              "Orderbuch", "Bestellung"]


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

class ResultDialog(QDialog):
    """Ergebnisuebersicht nach der Verarbeitung."""

    def __init__(self, summary: BatchSummary, parent=None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.setWindowTitle("Ergebnis der Verarbeitung")
        self.resize(760, 560)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        heading = QLabel(self.summary.headline())
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        if self.summary.aborted:
            warning = QLabel(f"Der Lauf wurde angehalten: {self.summary.abort_reason}")
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {Colors.RED}; font-weight: 600;")
            layout.addWidget(warning)

        documents = self.summary.created_documents()
        if documents:
            created = QLabel("Angelegte Belege:  " + "   •   ".join(documents))
            created.setWordWrap(True)
            created.setStyleSheet(f"color: {Colors.GREEN}; font-weight: 600;")
            layout.addWidget(created)

        tree = QTreeWidget()
        tree.setHeaderLabels(["Position / Aktion", "Ergebnis", "Beleg", "Dauer"])
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.setAlternatingRowColors(True)
        for result in self.summary.results:
            parent_item = QTreeWidgetItem([result.label, result.state.label, "", ""])
            colour = RESULT_COLOR.get(result.state, Colors.TEXT)
            parent_item.setForeground(1, Qt.GlobalColor.black)
            font = parent_item.font(0)
            font.setBold(True)
            parent_item.setFont(0, font)
            parent_item.setText(1, f"{result.state.symbol} {result.state.label}")
            parent_item.setToolTip(1, colour)
            for action in result.actions:
                child = QTreeWidgetItem([
                    action.action_label,
                    f"{action.state.symbol} {action.message}",
                    action.document_number,
                    f"{action.duration_ms} ms" if action.duration_ms else "",
                ])
                if action.detail:
                    child.setToolTip(1, action.detail)
                parent_item.addChild(child)
            tree.addTopLevelItem(parent_item)
        tree.expandAll()
        layout.addWidget(tree, 1)

        note = QLabel("Alle Aktionen wurden in der Historie protokolliert.")
        note.setObjectName("SubHeading")
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        self.copy_button = QPushButton("Als Text kopieren")
        self.copy_button.clicked.connect(self._copy)
        close = QPushButton("Schliessen")
        close.setObjectName("Primary")
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(close, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        lines = [self.summary.headline(), ""]
        for result in self.summary.results:
            lines.append(result.display_block())
            lines.append("")
        QApplication.clipboard().setText("\n".join(lines))
        self.copy_button.setText("Kopiert ✓")


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------

def show_error(parent, title: str, message: str, detail: str = "") -> None:
    """Verstaendliche Fehlermeldung; technische Details nur auf Wunsch."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def ask_yes_no(parent, title: str, message: str, detail: str = "") -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setInformativeText(detail)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    yes = box.button(QMessageBox.StandardButton.Yes)
    no = box.button(QMessageBox.StandardButton.No)
    yes.setText("Ja")
    no.setText("Nein")
    return box.exec() == QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------------------
# Lieferantenzuordnung
# ---------------------------------------------------------------------------

class VendorAssignmentDialog(QDialog):
    """SAP-Lieferant zu einem Angebotsnamen zuordnen."""

    #: Sonder-Ergebniscode: Anwender moechte die Stammdaten pflegen (XK02)
    CREATE_REQUESTED = 2

    def __init__(self, vendor_name: str, email_domain: str, candidates: list,
                 current_number: str = "", parent=None, settings=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SAP-Lieferant zuordnen")
        self.setMinimumWidth(560)
        self.vendor_name = vendor_name
        self.email_domain = email_domain
        self.candidates = candidates
        self.settings = settings
        self._build(current_number)

    def _build(self, current_number: str) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(f"Angebot von: <b>{self.vendor_name or '(kein Name erkannt)'}</b>"
                      + (f"<br>Absenderdomain: {self.email_domain}" if self.email_domain else ""))
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self.list = QListWidget()
        for candidate in self.candidates:
            number = getattr(candidate, "vendor_number", "")
            name = getattr(candidate, "name", "")
            score = getattr(candidate, "score", 0.0)
            blocked = getattr(candidate, "blocked", False)
            text = f"{number}   {name}"
            if score:
                text += f"   (Uebereinstimmung {score * 100:.0f} %)"
            if blocked:
                text += "   – GESPERRT"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, number)
            if blocked:
                item.setForeground(Qt.GlobalColor.red)
            self.list.addItem(item)
        if self.candidates:
            self.list.setCurrentRow(0)
            layout.addWidget(QLabel("Vorschlaege:"))
            layout.addWidget(self.list, 1)
        else:
            hint = QLabel("Keine Vorschlaege gefunden – bitte Lieferantennummer eingeben.")
            hint.setObjectName("SubHeading")
            layout.addWidget(hint)

        form = QFormLayout()
        self.number_edit = QLineEdit(current_number)
        self.number_edit.setPlaceholderText("z. B. 0000100234")
        form.addRow("Lieferantennummer:", self.number_edit)
        layout.addLayout(form)
        self.list.currentItemChanged.connect(
            lambda item, _prev: self.number_edit.setText(
                item.data(Qt.ItemDataRole.UserRole) if item else ""))

        self.remember_box = QCheckBox("Diese Zuordnung fuer zukuenftige Angebote speichern")
        self.remember_box.setChecked(True)
        layout.addWidget(self.remember_box)

        self.apply_all_box = QCheckBox("Auf alle Positionen dieses Angebots anwenden")
        self.apply_all_box.setChecked(True)
        layout.addWidget(self.apply_all_box)

        # Lieferanten werden hier NICHT angelegt.  Das ist ein kontrollierter
        # Vorgang (Dublettenpruefung, Compliance, Bankdaten, Kontengruppe,
        # Freigabe) und gehoert nicht in ein Werkzeug, das Preise pflegt.
        # Gibt es keinen Treffer, ist der ehrliche Hinweis besser als eine
        # Schaltflaeche, die still eine Dublette erzeugt.
        self.create_button = QPushButton("Stammdaten dieses Lieferanten pflegen ...")
        self.create_button.setToolTip(
            "Aendert einen bereits vorhandenen Lieferanten (XK02). "
            "Neue Lieferanten werden hier nicht angelegt.")
        self.create_button.setVisible(bool(self.candidates))
        self.create_button.setEnabled(bool(self.candidates))
        self.create_button.clicked.connect(self._request_create)
        layout.addWidget(self.create_button)

        if not self.candidates:
            hinweis = QLabel(
                "Kein passender Lieferant gefunden. Neue Lieferanten werden in "
                "diesem Werkzeug bewusst nicht angelegt — bitte im dafuer "
                "vorgesehenen Prozess in SAP anlegen lassen und danach hier "
                "zuordnen.")
            hinweis.setWordWrap(True)
            hinweis.setStyleSheet(f"color: {Colors.AMBER};")
            layout.addWidget(hinweis)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Zuordnen")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _request_create(self) -> None:
        self.done(self.CREATE_REQUESTED)

    @property
    def vendor_number(self) -> str:
        return self.number_edit.text().strip()

    @property
    def remember(self) -> bool:
        return self.remember_box.isChecked()

    @property
    def apply_to_all(self) -> bool:
        return self.apply_all_box.isChecked()


# ---------------------------------------------------------------------------
# Lieferantenstammdaten pflegen (XK02)
# ---------------------------------------------------------------------------

class VendorCreateDialog(QDialog):
    """Pflege der wichtigsten Stammdatenfelder eines VORHANDENEN Lieferanten.

    Es werden bewusst nur die im Alltag wirklich noetigen Felder abgefragt --
    alles Weitere pflegt der Einkauf in SAP direkt.  Land und
    Einkaufsorganisation sind mit den Vorgaben aus den Einstellungen
    vorbelegt, aber jederzeit aenderbar.

    Eine Neuanlage ist hier nicht moeglich: Lieferanten anzulegen ist ein
    kontrollierter Vorgang (Dublettenpruefung, Compliance, Bankdaten,
    Kontengruppe, Freigabe) und gehoert nicht in dieses Werkzeug.
    """

    def __init__(self, vendor_name: str, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Lieferantenstammdaten pflegen")
        self.setMinimumWidth(480)
        self._build(vendor_name)

    def _build(self, vendor_name: str) -> None:
        layout = QVBoxLayout(self)

        heading = QLabel("Stammdaten aendern -- wird in SAP gesichert (XK02)")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        hint = QLabel("Leere Felder bleiben unveraendert. Es wird nichts erraten -- "
                      "bitte die Angaben pruefen. Geschrieben wird erst nach "
                      "ausdruecklicher Bestaetigung.")
        hint.setWordWrap(True)
        hint.setObjectName("SubHeading")
        layout.addWidget(hint)

        purchasing = getattr(self.settings, "purchasing", None)

        form = QFormLayout()
        self.name_edit = QLineEdit(vendor_name or "")
        form.addRow("Name*:", self.name_edit)
        self.street_edit = QLineEdit()
        form.addRow("Strasse:", self.street_edit)
        self.postal_code_edit = QLineEdit()
        form.addRow("PLZ:", self.postal_code_edit)
        self.city_edit = QLineEdit()
        form.addRow("Ort:", self.city_edit)
        self.country_edit = QLineEdit(getattr(purchasing, "country", "") or "DE")
        form.addRow("Land*:", self.country_edit)
        self.language_edit = QLineEdit("DE")
        form.addRow("Sprache:", self.language_edit)
        self.purchasing_org_edit = QLineEdit(getattr(purchasing, "purchasing_org", "") or "")
        form.addRow("Einkaufsorganisation:", self.purchasing_org_edit)
        layout.addLayout(form)

        required_hint = QLabel("* Pflichtfeld -- ohne Name und Land wird nicht gespeichert.")
        required_hint.setObjectName("SubHeading")
        layout.addWidget(required_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Aenderung in SAP sichern (XK02)")
        ok.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _confirm(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Angabe fehlt", "Bitte einen Namen eintragen.")
            return
        if not self.country_edit.text().strip():
            QMessageBox.warning(self, "Angabe fehlt", "Bitte ein Land eintragen.")
            return
        confirm = QMessageBox.question(
            self, "Stammdaten pflegen",
            f"Stammdaten von '{self.name_edit.text().strip()}' wirklich in SAP "
            f"aendern (XK02)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.accept()

    def plan(self) -> VendorMasterPlan:
        """Der eingegebene Entwurf als :class:`VendorMasterPlan` (noch nicht geschrieben)."""
        return VendorMasterPlan(
            existing_vendor_number="",
            name=self.name_edit.text().strip(),
            street=self.street_edit.text().strip(),
            postal_code=self.postal_code_edit.text().strip(),
            city=self.city_edit.text().strip(),
            country=self.country_edit.text().strip(),
            language=self.language_edit.text().strip(),
            purchasing_org=self.purchasing_org_edit.text().strip(),
        )


# ---------------------------------------------------------------------------
# SAP-Session
# ---------------------------------------------------------------------------

class SessionDialog(QDialog):
    """Auswahl der zu verwendenden SAP-Session."""

    def __init__(self, sessions: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SAP-Session waehlen")
        self.setMinimumWidth(520)
        self.sessions = sessions
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Es sind mehrere SAP-Sessions offen. Welche soll "
                                "verwendet werden?"))
        self.list = QListWidget()
        for session in sessions:
            item = QListWidgetItem(session.label())
            item.setData(Qt.ItemDataRole.UserRole,
                         (session.connection_index, session.session_index))
            self.list.addItem(item)
        if sessions:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Verwenden")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selection(self) -> tuple[int, int] | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


# ---------------------------------------------------------------------------
# Text einfuegen (E-Mail-Text ohne Datei)
# ---------------------------------------------------------------------------

class PasteTextDialog(QDialog):
    """E-Mail- oder Angebotstext direkt einfuegen.

    Der haeufigste Fall im Alltag: Der Lieferant schreibt die neuen Preise
    einfach in eine Mail.  Der Anwender markiert den Text, kopiert ihn und
    fuegt ihn hier ein.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Angebotstext einfuegen")
        self.resize(720, 520)
        layout = QVBoxLayout(self)

        hint = QLabel("Fuegen Sie hier den Text aus der E-Mail oder dem Angebot ein. "
                      "Auch Tabellen aus Excel oder Outlook funktionieren.")
        hint.setWordWrap(True)
        hint.setObjectName("SubHeading")
        layout.addWidget(hint)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Beispiel:\n\nSehr geehrte Damen und Herren,\n\nzum 01.09.2026 passen wir "
            "unsere Preise wie folgt an:\n\n47110001  Dichtring NBR 40x52x7   12,85 EUR/St\n"
            "47110002  O-Ring Viton 25x3        0,89 EUR/St\n")
        layout.addWidget(self.editor, 1)

        form = QFormLayout()
        self.source_edit = QLineEdit("Eingefuegter Text")
        form.addRow("Bezeichnung der Quelle:", self.source_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Auswerten")
        ok.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def text(self) -> str:
        return self.editor.toPlainText()

    @property
    def source_name(self) -> str:
        return self.source_edit.text().strip() or "Eingefuegter Text"


# ---------------------------------------------------------------------------
# Komplettvorgang konfigurieren
# ---------------------------------------------------------------------------

class ChainDialog(QDialog):
    """"Komplettvorgang": alles auf einmal fuer die ausgewaehlten Positionen.

    Genau der Anwendungsfall aus dem Alltag: Angebot rein, einmal ankreuzen --
    Infosatz bis 2099 pflegen, Orderbuch-Lieferant aktiv setzen, Mengenkontrakt
    schreiben und daraus gleich eine Teilmenge bestellen.
    """

    def __init__(self, settings, position_count: int, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Komplettvorgang")
        self.setMinimumWidth(560)
        self._build(position_count)

    def _build(self, position_count: int) -> None:
        workflow = self.settings.workflow
        layout = QVBoxLayout(self)

        heading = QLabel(f"Komplettvorgang fuer {position_count} ausgewaehlte Position(en)")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)

        self.check_info = QCheckBox("1.  Infosatz pflegen (ME11/ME12)")
        self.check_contract = QCheckBox("2.  Mengenkontrakt schreiben (ME31K)")
        self.check_source = QCheckBox("3.  Orderbuch pflegen, Lieferant aktiv setzen (ME01)")
        self.check_order = QCheckBox("4.  Bestellung als Abruf aus dem Kontrakt (ME21N)")
        self.check_info.setChecked(workflow.chain_info_record)
        self.check_contract.setChecked(workflow.chain_contract)
        self.check_source.setChecked(workflow.chain_source_list)
        self.check_order.setChecked(workflow.chain_purchase_order)
        for box in (self.check_info, self.check_contract, self.check_source,
                    self.check_order):
            card_layout.addWidget(box)
        layout.addWidget(card)

        form = QFormLayout()
        self.valid_to_edit = QLineEdit(workflow.info_record_valid_to)
        self.valid_to_edit.setToolTip("Gueltig-bis-Platzhalter fuer Infosatz und Orderbuch")
        form.addRow("Gueltig bis:", self.valid_to_edit)

        self.call_off_edit = QLineEdit(str(workflow.call_off_percent))
        self.call_off_edit.setToolTip("Anteil der Kontraktmenge, der sofort bestellt wird")
        form.addRow("Abrufmenge (% der Kontraktmenge):", self.call_off_edit)

        self.delivery_edit = QLineEdit()
        self.delivery_edit.setPlaceholderText(
            f"leer = heute + {self.settings.purchasing.default_delivery_days} Tage")
        form.addRow("Lieferdatum:", self.delivery_edit)
        layout.addLayout(form)

        note = QLabel("Die Reihenfolge ist fest: Der Kontrakt muss vor Orderbuch und "
                      "Bestellung stehen, damit beide ihn referenzieren koennen.\n"
                      "Es wird grundsaetzlich keine Nachricht an den Lieferanten "
                      "ausgeloest.")
        note.setWordWrap(True)
        note.setObjectName("SubHeading")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Uebernehmen")
        ok.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_to(self, positions: list[OfferPosition]) -> int:
        """Auswahl auf die Positionen uebertragen; liefert die Anzahl."""
        from ..utils.parsing import parse_date, parse_decimal

        workflow = self.settings.workflow
        workflow.chain_info_record = self.check_info.isChecked()
        workflow.chain_contract = self.check_contract.isChecked()
        workflow.chain_source_list = self.check_source.isChecked()
        workflow.chain_purchase_order = self.check_order.isChecked()

        valid_to = self.valid_to_edit.text().strip()
        if valid_to and parse_date(valid_to):
            workflow.info_record_valid_to = valid_to
            workflow.source_list_valid_to = valid_to

        percent = parse_decimal(self.call_off_edit.text())
        if percent is not None and percent > 0:
            workflow.call_off_percent = percent
            workflow.call_off_mode = "percent"

        delivery = parse_date(self.delivery_edit.text())

        count = 0
        for position in positions:
            position.do_info_record = workflow.chain_info_record
            position.do_source_list = workflow.chain_source_list
            position.do_contract = workflow.chain_contract
            position.do_purchase_order = workflow.chain_purchase_order
            if delivery:
                position.delivery_date = delivery
            if position.do_contract and position.contract_quantity is None:
                position.contract_quantity = position.quantity
            count += 1
        return count
