"""Auffang-Workflow 2: Grafisches Anlernen auf dem Angebotsbild.

Warum grafisch?
---------------
Wenn ein PDF weder eine erkennbare Kopfzeile noch ein sauberes Spaltenraster
hat, hilft keine Ueberschriftenanalyse mehr.  Dann ist es am schnellsten, wenn
der Anwender dem Werkzeug einmal *zeigt*, wo was steht -- so wie man es von
klassischen Beleglesern kennt.

Der Ablauf ist bewusst zweistufig, weil zwei verschiedene Fragen zu klaeren
sind:

    Schritt 1: Was ist eine POSITION?
        Der Anwender zieht ein Rechteck um genau eine vollstaendige
        Positionszeile.  Daraus ergeben sich Zeilenhoehe, der senkrechte
        Startpunkt der Positionsliste und der waagerechte Bereich der Tabelle.

    Schritt 2: Was ist eine SPALTE?
        Innerhalb dieser Zeile markiert der Anwender die einzelnen Felder und
        sagt jeweils, was es ist (Material, Menge, Preis ...).  Die
        x-Ausdehnung des Rechtecks definiert die Spalte, nicht nur die Zelle.

    Schritt 3 (freiwillig): Gegenprobe
        Eine zweite Positionszeile markieren.  Damit laesst sich pruefen, ob
        die Spaltenaufteilung auch dort passt, und mehrzeilige Positionen
        werden erkannt.

Ergebnis
--------
1. Die Positionen des aktuellen Angebots -- sofort nutzbar.
2. Eine Zuordnung "Spaltenueberschrift -> Feld", sofern ueber den Spalten
   ueberhaupt Text steht.  Diese wandert ins Lieferantenprofil und wird beim
   naechsten Angebot desselben Lieferanten automatisch angewandt.

Gelernt wird ausschliesslich, WO ein Wert steht -- nie, WELCHER Wert dort
steht.  Das ist derselbe Grundsatz wie in der automatischen Erkennung.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..models.enums import FieldOrigin, SourceKind
from ..models.offer_position import OfferPosition
from ..utils.parsing import (
    normalize_material_number,
    normalize_uom,
    normalize_whitespace,
    parse_date,
    parse_decimal,
    parse_int,
)
from .style import Colors
from .table_import_dialog import ROLES

logger = logging.getLogger(__name__)

#: Rolle fuer "das ist eine ganze Positionszeile"
ROW_ROLE = "__row__"

#: Farben je Rolle, damit die Markierungen unterscheidbar bleiben
_ROLE_COLORS = (
    "#1f5fa9", "#2e7d32", "#b26a00", "#7b1fa2", "#00838f",
    "#c0392b", "#5d4037", "#455a64", "#ad1457", "#33691e",
)


@dataclass
class MarkedRegion:
    """Eine vom Anwender gezogene Markierung (in PDF-Punkten)."""

    role: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0

    @property
    def is_row(self) -> bool:
        return self.role == ROW_ROLE

    def label(self) -> str:
        if self.is_row:
            return "Positionszeile"
        return dict(ROLES).get(self.role, self.role)


@dataclass
class TeachResult:
    """Ergebnis des Anlernens."""

    positions: list[OfferPosition] = field(default_factory=list)
    #: Spaltenueberschrift -> Feldname (fuer das Lieferantenprofil)
    column_map: dict[str, str] = field(default_factory=dict)
    #: Spaltenbereiche als Anteil der Seitenbreite (0..1), fuer spaetere Wiederverwendung
    geometry: dict[str, list[float]] = field(default_factory=dict)
    remember: bool = False
    pages_applied: int = 0


# ---------------------------------------------------------------------------
# Anzeige mit Markierwerkzeug
# ---------------------------------------------------------------------------

class RegionPicker(QWidget):
    """Seitenbild, auf dem der Anwender Rechtecke ziehen kann."""

    regionDrawn = Signal(QRect)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._regions: list[tuple[QRect, str, str]] = []   # (Rechteck, Beschriftung, Farbe)
        self._start: QPoint | None = None
        self._current: QRect | None = None
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setMouseTracking(True)

    def set_page(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.setFixedSize(pixmap.size())
        self.update()

    def set_regions(self, regions: list[tuple[QRect, str, str]]) -> None:
        self._regions = regions
        self.update()

    # -- Maus -----------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()
            self._current = QRect(self._start, self._start)
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self._current = QRect(self._start, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = QRect(self._start, event.position().toPoint()).normalized()
        self._start = None
        self._current = None
        self.update()
        # Winzige Klicks ignorieren (versehentliches Antippen)
        if rect.width() >= 6 and rect.height() >= 4:
            self.regionDrawn.emit(rect)

    # -- Zeichnen -------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)

        for rect, label, colour in self._regions:
            pen = QPen(QColor(colour))
            pen.setWidth(2)
            painter.setPen(pen)
            fill = QColor(colour)
            fill.setAlpha(38)
            painter.fillRect(rect, fill)
            painter.drawRect(rect)

            if label:
                painter.setPen(QPen(QColor("#ffffff")))
                hintergrund = QRect(rect.left(), max(0, rect.top() - 17),
                                    max(70, len(label) * 7 + 10), 16)
                painter.fillRect(hintergrund, QColor(colour))
                painter.drawText(hintergrund.adjusted(4, 0, 0, 0),
                                 int(Qt.AlignmentFlag.AlignVCenter), label)

        if self._current is not None:
            pen = QPen(QColor(Colors.ACCENT))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._current)
        painter.end()


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class TeachDialog(QDialog):
    """Grafisches Anlernen auf einem PDF-Angebot."""

    ZOOM = 2.0      # Renderfaktor: PDF-Punkte -> Bildpunkte

    def __init__(self, pdf_path: str, vendor_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.vendor_name = vendor_name
        self.regions: list[MarkedRegion] = []
        self.result_data = TeachResult()

        self._document = None
        self._page_index = 0
        self._page_count = 0
        self._page_size = (0.0, 0.0)

        self.setWindowTitle("Erkennung anlernen")
        self.resize(1180, 820)
        self._build()
        self._load_document()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.step_label = QLabel()
        self.step_label.setObjectName("Heading")
        self.step_label.setWordWrap(True)
        layout.addWidget(self.step_label)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("SubHeading")
        layout.addWidget(self.hint_label)

        # -- Werkzeugleiste ---------------------------------------------
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Seite:"))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(self._page_changed)
        tools.addWidget(self.page_spin)

        tools.addSpacing(16)
        tools.addWidget(QLabel("Naechste Markierung ist:"))
        self.role_box = QComboBox()
        self.role_box.addItem("Positionszeile (ganze Zeile)", ROW_ROLE)
        for key, label in ROLES:
            if key:
                self.role_box.addItem(label, key)
        self.role_box.setMinimumWidth(230)
        tools.addWidget(self.role_box)

        undo_button = QPushButton("Letzte Markierung entfernen")
        undo_button.clicked.connect(self._remove_last)
        tools.addWidget(undo_button)

        reset_button = QPushButton("Alle entfernen")
        reset_button.clicked.connect(self._reset_regions)
        tools.addWidget(reset_button)

        self.all_pages_box = QCheckBox("Auf alle Seiten anwenden")
        self.all_pages_box.setChecked(True)
        self.all_pages_box.setToolTip(
            "Mehrseitige Angebote verwenden fast immer dasselbe Spaltenraster.")
        tools.addWidget(self.all_pages_box)
        tools.addStretch(1)
        layout.addLayout(tools)

        # -- Bild und Liste ---------------------------------------------
        body = QHBoxLayout()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.picker = RegionPicker()
        self.picker.regionDrawn.connect(self._region_drawn)
        self.scroll.setWidget(self.picker)
        body.addWidget(self.scroll, 1)

        side = QVBoxLayout()
        side.addWidget(QLabel("Markierungen:"))
        self.region_list = QListWidget()
        self.region_list.setMaximumWidth(280)
        side.addWidget(self.region_list, 1)

        self.preview_button = QPushButton("Vorschau der Positionen")
        self.preview_button.clicked.connect(self._preview)
        side.addWidget(self.preview_button)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setMaximumWidth(280)
        side.addWidget(self.preview_label)
        body.addLayout(side)
        layout.addLayout(body, 1)

        # -- Fuss --------------------------------------------------------
        self.remember_box = QCheckBox(
            "Zuordnung fuer zukuenftige Angebote dieses Lieferanten merken")
        self.remember_box.setChecked(bool(self.vendor_name))
        self.remember_box.setEnabled(bool(self.vendor_name))
        layout.addWidget(self.remember_box)

        buttons = QDialogButtonBox()
        self.ok_button = QPushButton("Positionen uebernehmen")
        self.ok_button.setObjectName("Primary")
        self.ok_button.setEnabled(False)
        cancel = QPushButton("Abbrechen")
        buttons.addButton(self.ok_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_step()

    # ------------------------------------------------------------------
    # Dokument
    # ------------------------------------------------------------------
    def _load_document(self) -> None:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            QMessageBox.warning(
                self, "PyMuPDF fehlt",
                "Zum Anlernen auf dem Seitenbild wird PyMuPDF benoetigt.\n\n"
                "Installation:  pip install PyMuPDF")
            self.reject()
            return
        try:
            self._document = fitz.open(self.pdf_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF nicht lesbar (%s): %s", self.pdf_path, exc)
            QMessageBox.warning(self, "PDF nicht lesbar",
                                "Die Datei konnte nicht geoeffnet werden.")
            self.reject()
            return

        self._page_count = self._document.page_count
        self.page_spin.setMaximum(max(1, self._page_count))
        self._render_page(0)

    def _render_page(self, index: int) -> None:
        if self._document is None or not (0 <= index < self._page_count):
            return
        import fitz

        self._page_index = index
        page = self._document[index]
        self._page_size = (page.rect.width, page.rect.height)
        matrix = fitz.Matrix(self.ZOOM, self.ZOOM)
        pix = page.get_pixmap(matrix=matrix)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format.Format_RGB888).copy()
        self.picker.set_page(QPixmap.fromImage(image))
        self._refresh_regions()

    def _page_changed(self, value: int) -> None:
        self._render_page(value - 1)

    def _page_words(self, index: int) -> list[tuple[float, float, float, float, str]]:
        """Woerter der Seite mit Koordinaten (PDF-Punkte)."""
        if self._document is None:
            return []
        try:
            raw = self._document[index].get_text("words") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Text der Seite %d nicht lesbar: %s", index + 1, exc)
            return []
        woerter = []
        for item in raw:
            if len(item) < 5:
                continue
            text = str(item[4]).strip()
            if text:
                woerter.append((float(item[0]), float(item[1]), float(item[2]),
                                float(item[3]), text))
        return woerter

    # ------------------------------------------------------------------
    # Markierungen
    # ------------------------------------------------------------------
    def _region_drawn(self, rect: QRect) -> None:
        role = self.role_box.currentData()
        region = MarkedRegion(
            role=role,
            x0=rect.left() / self.ZOOM, y0=rect.top() / self.ZOOM,
            x1=rect.right() / self.ZOOM, y1=rect.bottom() / self.ZOOM,
            page=self._page_index,
        )

        if not region.is_row and not self._row_regions():
            QMessageBox.information(
                self, "Zuerst die Positionszeile",
                "Markieren Sie bitte zuerst eine vollstaendige Positionszeile.\n\n"
                "Erst dadurch weiss das Werkzeug, wo die Positionsliste beginnt und "
                "wie hoch eine Zeile ist.")
            return

        # Gleiche Rolle nur einmal (ausser Positionszeilen)
        if not region.is_row:
            self.regions = [r for r in self.regions if r.role != region.role]
        self.regions.append(region)
        self._refresh_regions()
        self._advance_role()
        self._update_step()

    def _row_regions(self) -> list[MarkedRegion]:
        return [r for r in self.regions if r.is_row]

    def _field_regions(self) -> list[MarkedRegion]:
        return [r for r in self.regions if not r.is_row]

    def _advance_role(self) -> None:
        """Nach der Zeile automatisch auf die erste Feldrolle springen."""
        if self.role_box.currentData() == ROW_ROLE and len(self._row_regions()) == 1:
            index = self.role_box.findData("material_number")
            if index >= 0:
                self.role_box.setCurrentIndex(index)

    def _remove_last(self) -> None:
        if self.regions:
            self.regions.pop()
            self._refresh_regions()
            self._update_step()

    def _reset_regions(self) -> None:
        self.regions = []
        self._refresh_regions()
        self._update_step()

    def _refresh_regions(self) -> None:
        anzeige: list[tuple[QRect, str, str]] = []
        self.region_list.clear()
        for index, region in enumerate(self.regions):
            colour = ("#c0392b" if region.is_row
                      else _ROLE_COLORS[index % len(_ROLE_COLORS)])
            if region.page == self._page_index:
                rect = QRect(
                    int(region.x0 * self.ZOOM), int(region.y0 * self.ZOOM),
                    int((region.x1 - region.x0) * self.ZOOM),
                    int((region.y1 - region.y0) * self.ZOOM))
                anzeige.append((rect, region.label(), colour))
            item = QListWidgetItem(f"{region.label()}   (Seite {region.page + 1})")
            item.setForeground(QColor(colour))
            self.region_list.addItem(item)
        self.picker.set_regions(anzeige)
        self.ok_button.setEnabled(bool(self._row_regions()) and bool(self._field_regions()))

    def _update_step(self) -> None:
        if not self._row_regions():
            self.step_label.setText("Schritt 1 von 2:  Was ist eine Position?")
            self.hint_label.setText(
                "Ziehen Sie mit der Maus ein Rechteck um <b>eine vollstaendige "
                "Positionszeile</b> – von der Positionsnummer bis zum Preis. "
                "Daraus lernt das Werkzeug, wo die Positionsliste beginnt und wie "
                "hoch eine Zeile ist.")
            self.hint_label.setTextFormat(Qt.TextFormat.RichText)
            return

        felder = self._field_regions()
        if not felder:
            self.step_label.setText("Schritt 2 von 2:  Was steht in welcher Spalte?")
            self.hint_label.setText(
                "Waehlen Sie oben eine Rolle und markieren Sie das passende Feld "
                "<b>innerhalb der eben markierten Zeile</b>. Die Breite des Rechtecks "
                "bestimmt die Spalte. Sinnvolles Minimum: Material und Preis.")
            return

        rollen = ", ".join(sorted(r.label() for r in felder))
        self.step_label.setText(f"{len(felder)} Spalte(n) zugeordnet")
        self.hint_label.setText(
            f"Bereits markiert: {rollen}.<br>Weitere Felder ergaenzen, mit "
            f"„Vorschau“ pruefen oder mit „Positionen uebernehmen“ abschliessen. "
            f"Tipp: Eine <b>zweite Positionszeile</b> markieren ist die beste "
            f"Gegenprobe, ob das Raster wirklich passt.")

    # ------------------------------------------------------------------
    # Auswertung
    # ------------------------------------------------------------------
    def _columns(self) -> dict[str, tuple[float, float]]:
        """Rolle -> waagerechter Bereich (x0, x1) in PDF-Punkten."""
        return {r.role: (r.x0, r.x1) for r in self._field_regions()}

    def _anchor_role(self) -> str:
        """Welche Spalte entscheidet, ob eine Zeile eine Position ist?

        Vorzugsweise die Materialnummer -- sie steht in jeder echten Position.
        Ersatzweise Positionsnummer, Lieferantenartikelnummer oder Preis.
        """
        spalten = self._columns()
        for rolle in ("material_number", "position_number", "vendor_material_number",
                      "price", "description"):
            if rolle in spalten:
                return rolle
        return next(iter(spalten), "")

    def _row_geometry(self) -> tuple[float, float, float]:
        """(Startzeile oben, Zeilenhoehe, Toleranz) aus den markierten Zeilen."""
        zeilen = sorted(self._row_regions(), key=lambda r: r.y0)
        erste = zeilen[0]
        hoehe = max(4.0, erste.y1 - erste.y0)
        if len(zeilen) >= 2:
            abstand = abs(zeilen[1].y0 - zeilen[0].y0)
            if abstand > 1.0:
                hoehe = abstand
        return erste.y0, hoehe, max(2.0, hoehe * 0.35)

    def _example_row_cells(self) -> dict[str, str]:
        """Inhalt der vom Anwender markierten Beispielzeile."""
        spalten = self._columns()
        zeilen = sorted(self._row_regions(), key=lambda r: r.y0)
        if not spalten or not zeilen:
            return {}
        beispiel = zeilen[0]
        woerter = [w for w in self._page_words(beispiel.page)
                   if (w[1] + w[3]) / 2 >= beispiel.y0
                   and (w[1] + w[3]) / 2 <= beispiel.y1]
        if not woerter:
            return {}
        return self._cells_for_line(sorted(woerter, key=lambda w: w[0]), spalten)

    @staticmethod
    def _anchor_matches(wert: str, muster: str) -> bool:
        """Passt der Ankerwert zum Muster der Beispielzeile?

        Ohne diese Pruefung wuerde jeder Fliesstext unterhalb der Tabelle
        ("Preise verstehen sich netto ab Werk ...") als Position durchgehen --
        er faellt geometrisch schliesslich in dieselben Spalten.  Das Muster
        stammt aus der Zeile, die der Anwender selbst gezeigt hat.
        """
        wert = wert.strip()
        if not wert:
            return False
        if " " in wert and muster != "frei":
            return False        # Ankerzellen enthalten nie ganze Saetze
        if muster == "ziffern":
            return wert.replace(".", "").replace("-", "").isdigit()
        if muster == "alphanumerisch":
            return any(zeichen.isdigit() for zeichen in wert)
        return len(wert) <= 60

    def _anchor_pattern(self, anker: str) -> str:
        """Musterklasse der Ankerspalte aus der Beispielzeile ableiten."""
        beispiel = self._example_row_cells().get(anker, "").strip()
        if not beispiel:
            return "frei"
        kern = beispiel.replace(".", "").replace("-", "")
        if kern.isdigit():
            return "ziffern"
        if any(zeichen.isdigit() for zeichen in beispiel):
            return "alphanumerisch"
        return "frei"

    def build_positions(self) -> list[OfferPosition]:
        """Aus Markierungen und Seitentext die Positionen bilden."""
        spalten = self._columns()
        if not spalten or not self._row_regions():
            return []

        anker = self._anchor_role()
        muster = self._anchor_pattern(anker)
        start_y, _hoehe, toleranz = self._row_geometry()
        erste_seite = self._row_regions()[0].page

        seiten = (range(erste_seite, self._page_count) if self.all_pages_box.isChecked()
                  else [erste_seite])

        positions: list[OfferPosition] = []
        for seite in seiten:
            woerter = self._page_words(seite)
            if not woerter:
                continue
            # Auf Folgeseiten beginnt die Tabelle in der Regel weiter oben
            grenze = start_y - toleranz if seite == erste_seite else 0.0
            zeilen = self._cluster_lines([w for w in woerter if w[3] >= grenze], toleranz)

            for zeile in zeilen:
                zellen = self._cells_for_line(zeile, spalten)
                ankertext = zellen.get(anker, "").strip()
                if not self._anchor_matches(ankertext, muster):
                    # Keine Position: entweder Fortsetzungszeile oder Fliesstext
                    # unterhalb der Tabelle.  Nur echte Fortsetzungen anhaengen.
                    beschreibung = zellen.get("description", "").strip()
                    gefuellte = sum(1 for wert in zellen.values() if wert.strip())
                    if (beschreibung and positions and len(beschreibung) > 2
                            and not ankertext and gefuellte <= 2):
                        vorige = positions[-1]
                        vorige.description = normalize_whitespace(
                            f"{vorige.description} {beschreibung}")
                    continue
                if self._ist_summenzeile(zellen):
                    continue
                # Eine echte Position hat mehr als nur die Ankerzelle
                if sum(1 for wert in zellen.values() if wert.strip()) < 2:
                    continue
                position = self._position_from_cells(zellen, seite)
                if position is not None:
                    positions.append(position)

        logger.info("Anlernen: %d Position(en) aus %d Seite(n) gebildet",
                    len(positions), len(list(seiten)))
        return positions

    @staticmethod
    def _cluster_lines(woerter: list[tuple], toleranz: float) -> list[list[tuple]]:
        """Woerter zu Textzeilen zusammenfassen (nach senkrechter Lage)."""
        if not woerter:
            return []
        geordnet = sorted(woerter, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        zeilen: list[list[tuple]] = []
        aktuell = [geordnet[0]]
        referenz = (geordnet[0][1] + geordnet[0][3]) / 2
        for wort in geordnet[1:]:
            mitte = (wort[1] + wort[3]) / 2
            if abs(mitte - referenz) <= toleranz:
                aktuell.append(wort)
            else:
                zeilen.append(sorted(aktuell, key=lambda w: w[0]))
                aktuell = [wort]
                referenz = mitte
        zeilen.append(sorted(aktuell, key=lambda w: w[0]))
        return zeilen

    @staticmethod
    def _cells_for_line(zeile: list[tuple],
                        spalten: dict[str, tuple[float, float]]) -> dict[str, str]:
        """Woerter einer Zeile den markierten Spalten zuordnen."""
        zellen: dict[str, list[str]] = {rolle: [] for rolle in spalten}
        for x0, _y0, x1, _y1, text in zeile:
            mitte = (x0 + x1) / 2
            beste_rolle, beste_ueberdeckung = "", 0.0
            for rolle, (sx0, sx1) in spalten.items():
                if sx0 <= mitte <= sx1:
                    ueberdeckung = 1.0
                else:
                    # Teilweise Ueberlappung zulassen (Zahlen stehen oft rechtsbuendig)
                    links, rechts = max(x0, sx0), min(x1, sx1)
                    breite = max(1e-6, x1 - x0)
                    ueberdeckung = max(0.0, rechts - links) / breite
                if ueberdeckung > beste_ueberdeckung:
                    beste_rolle, beste_ueberdeckung = rolle, ueberdeckung
            if beste_rolle and beste_ueberdeckung >= 0.35:
                zellen[beste_rolle].append(text)
        return {rolle: " ".join(teile) for rolle, teile in zellen.items()}

    @staticmethod
    def _ist_summenzeile(zellen: dict[str, str]) -> bool:
        text = " ".join(zellen.values()).lower()
        return any(wort in text for wort in
                   ("summe", "gesamt", "total", "zwischensumme", "subtotal", "mwst",
                    "vat", "zzgl", "endbetrag", "uebertrag"))

    @staticmethod
    def _position_from_cells(zellen: dict[str, str], seite: int) -> OfferPosition | None:
        position = OfferPosition(
            source_kind=SourceKind.MANUAL,
            source_hint=f"angelernt, Seite {seite + 1}",
            raw_text=" | ".join(v for v in zellen.values() if v)[:300],
        )
        gefuellt = False
        for feld, roh in zellen.items():
            roh = roh.strip()
            if not roh:
                continue
            wert = TeachDialog._konvertiere(feld, roh)
            if wert in (None, ""):
                continue
            # Angelernte Werte gelten als sicher: der Anwender hat die Stelle
            # selbst gezeigt.  Nicht lesbare Werte bleiben trotzdem leer.
            position.set_field(feld, wert, FieldOrigin.MANUAL)
            gefuellt = True
        if not gefuellt:
            return None
        if not (position.material_number or position.vendor_material_number
                or position.description):
            return None
        return position

    @staticmethod
    def _konvertiere(feld: str, roh: str):
        if feld in ("quantity", "price", "min_order_qty"):
            return parse_decimal(roh)
        if feld in ("price_unit", "lead_time_days"):
            wert = parse_int(roh)
            return wert if wert and wert > 0 else None
        if feld == "valid_from":
            return parse_date(roh)
        if feld == "material_number":
            return normalize_material_number(roh)
        if feld == "uom":
            return normalize_uom(roh)
        if feld == "currency":
            return roh.upper()[:5]
        return normalize_whitespace(roh)

    # ------------------------------------------------------------------
    def _header_map(self) -> dict[str, str]:
        """Ueberschriften ueber den markierten Spalten einsammeln.

        Damit profitiert auch die *automatische* Erkennung beim naechsten Mal:
        Aus der Geometrie wird eine ganz normale Spaltenzuordnung
        "Ueberschrift -> Feld", die das Lieferantenprofil versteht.
        """
        spalten = self._columns()
        if not spalten:
            return {}
        start_y, _hoehe, toleranz = self._row_geometry()
        seite = self._row_regions()[0].page
        woerter = [w for w in self._page_words(seite) if w[3] < start_y - toleranz]
        if not woerter:
            return {}

        # Nur die unterste Textzeile oberhalb der ersten Position betrachten --
        # das ist mit grosser Wahrscheinlichkeit die Kopfzeile.
        zeilen = self._cluster_lines(woerter, toleranz)
        if not zeilen:
            return {}
        kopfzeile = zeilen[-1]
        zellen = self._cells_for_line(kopfzeile, spalten)
        return {text.strip(): rolle for rolle, text in zellen.items()
                if text.strip() and len(text.strip()) <= 40}

    def _preview(self) -> None:
        positions = self.build_positions()
        if not positions:
            self.preview_label.setText(
                "Aus den Markierungen liess sich keine Position bilden. "
                "Sitzt die Ankerspalte (Material oder Positionsnummer) richtig?")
            self.preview_label.setStyleSheet(f"color: {Colors.AMBER};")
            return
        zeilen = [f"{len(positions)} Position(en) erkannt:", ""]
        for position in positions[:6]:
            zeilen.append(f"• {position.summary_line()[:60]}")
        if len(positions) > 6:
            zeilen.append(f"... und {len(positions) - 6} weitere")
        self.preview_label.setText("\n".join(zeilen))
        self.preview_label.setStyleSheet(f"color: {Colors.GREEN};")

    def _accept(self) -> None:
        positions = self.build_positions()
        if not positions:
            QMessageBox.information(
                self, "Keine Positionen",
                "Aus den Markierungen liess sich keine Position bilden.\n\n"
                "Pruefen Sie, ob die markierte Spalte (Material bzw. Positions-"
                "nummer) in jeder Zeile gefuellt ist.")
            return
        self.result_data.positions = positions
        self.result_data.column_map = self._header_map()
        self.result_data.remember = self.remember_box.isChecked()
        self.result_data.pages_applied = (self._page_count
                                          if self.all_pages_box.isChecked() else 1)
        breite = self._page_size[0] or 1.0
        self.result_data.geometry = {
            rolle: [x0 / breite, x1 / breite]
            for rolle, (x0, x1) in self._columns().items()
        }
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._document is not None:
            try:
                self._document.close()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
