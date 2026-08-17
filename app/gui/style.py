"""Farben, Schriften und Stylesheet der Oberflaeche.

Ziel ist eine ruhige, sachliche Einkaufsanwendung -- keine Entwickleroptik,
keine grellen Farben.  Statusfarben folgen dem Ampelprinzip und sind auch bei
Rot-Gruen-Sehschwaeche durch Symbol und Text unterscheidbar.
"""

from __future__ import annotations

from ..models.enums import IssueSeverity, PositionStatus, ResultState


class Colors:
    """Zentrale Farbpalette (helles Thema)."""

    BACKGROUND = "#f5f6f8"
    SURFACE = "#ffffff"
    BORDER = "#d6d9de"
    TEXT = "#1f2328"
    TEXT_MUTED = "#5b6470"
    ACCENT = "#1f5fa9"
    ACCENT_LIGHT = "#e8f0fa"

    GREEN = "#2e7d32"
    GREEN_BG = "#e9f5ea"
    AMBER = "#b26a00"
    AMBER_BG = "#fff5e0"
    RED = "#c0392b"
    RED_BG = "#fdecea"
    BLUE = "#1565c0"
    BLUE_BG = "#e5eefb"
    GREY = "#8a929c"
    GREY_BG = "#f0f1f3"


#: Statusfarbe (Vordergrund, Hintergrund, Symbol) je Positionsstatus
STATUS_STYLE: dict[PositionStatus, tuple[str, str, str]] = {
    PositionStatus.READY: (Colors.GREEN, Colors.GREEN_BG, "●"),
    PositionStatus.CHECK: (Colors.AMBER, Colors.AMBER_BG, "▲"),
    PositionStatus.ERROR: (Colors.RED, Colors.RED_BG, "■"),
    PositionStatus.RUNNING: (Colors.BLUE, Colors.BLUE_BG, "▶"),
    PositionStatus.NOT_SELECTED: (Colors.GREY, Colors.GREY_BG, "○"),
    PositionStatus.DONE: (Colors.GREEN, Colors.GREEN_BG, "✓"),
    PositionStatus.SKIPPED: (Colors.GREY, Colors.GREY_BG, "○"),
}

SEVERITY_COLOR: dict[IssueSeverity, str] = {
    IssueSeverity.INFO: Colors.BLUE,
    IssueSeverity.WARNING: Colors.AMBER,
    IssueSeverity.ERROR: Colors.RED,
}

RESULT_COLOR: dict[ResultState, str] = {
    ResultState.SUCCESS: Colors.GREEN,
    ResultState.SIMULATED: Colors.BLUE,
    ResultState.FAILED: Colors.RED,
    ResultState.SKIPPED: Colors.GREY,
}


def status_label(status: PositionStatus) -> str:
    symbol = STATUS_STYLE.get(status, (Colors.GREY, Colors.GREY_BG, "○"))[2]
    return f"{symbol}  {status.label}"


def build_stylesheet(font_size: int = 10) -> str:
    """Anwendungsweites Stylesheet."""
    c = Colors
    return f"""
    QWidget {{
        font-family: "Segoe UI", "Noto Sans", sans-serif;
        font-size: {font_size}pt;
        color: {c.TEXT};
    }}
    QMainWindow, QDialog {{ background: {c.BACKGROUND}; }}

    QGroupBox {{
        background: {c.SURFACE};
        border: 1px solid {c.BORDER};
        border-radius: 6px;
        margin-top: 14px;
        padding: 10px 10px 8px 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {c.TEXT_MUTED};
    }}

    QFrame#Card {{
        background: {c.SURFACE};
        border: 1px solid {c.BORDER};
        border-radius: 6px;
    }}
    QLabel#Heading {{ font-size: {font_size + 3}pt; font-weight: 600; }}
    QLabel#SubHeading {{ color: {c.TEXT_MUTED}; }}
    QLabel#FieldLabel {{ color: {c.TEXT_MUTED}; }}

    QToolBar {{
        background: {c.SURFACE};
        border-bottom: 1px solid {c.BORDER};
        padding: 6px;
        spacing: 6px;
    }}
    QToolBar QToolButton {{
        padding: 6px 10px;
        border-radius: 4px;
    }}
    QToolBar QToolButton:hover {{ background: {c.ACCENT_LIGHT}; }}
    QToolBar QToolButton:checked {{ background: {c.ACCENT_LIGHT}; color: {c.ACCENT}; }}

    QPushButton {{
        background: {c.SURFACE};
        border: 1px solid {c.BORDER};
        border-radius: 4px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ background: {c.ACCENT_LIGHT}; }}
    QPushButton:disabled {{ color: {c.GREY}; background: {c.GREY_BG}; }}
    QPushButton#Primary {{
        background: {c.ACCENT};
        border: 1px solid {c.ACCENT};
        color: white;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: #17518f; }}
    QPushButton#Primary:disabled {{ background: {c.GREY}; border-color: {c.GREY}; color: #eeeeee; }}
    QPushButton#Danger {{ color: {c.RED}; border-color: {c.RED}; }}

    QTableView {{
        background: {c.SURFACE};
        alternate-background-color: #fafbfc;
        border: 1px solid {c.BORDER};
        border-radius: 4px;
        gridline-color: #e8eaed;
        selection-background-color: {c.ACCENT_LIGHT};
        selection-color: {c.TEXT};
    }}
    QHeaderView::section {{
        background: #eef0f3;
        border: none;
        border-right: 1px solid {c.BORDER};
        border-bottom: 1px solid {c.BORDER};
        padding: 6px 8px;
        font-weight: 600;
    }}

    QTabWidget::pane {{
        border: 1px solid {c.BORDER};
        border-radius: 4px;
        background: {c.SURFACE};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        padding: 8px 16px;
        border: 1px solid transparent;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        color: {c.TEXT_MUTED};
    }}
    QTabBar::tab:selected {{
        background: {c.SURFACE};
        border-color: {c.BORDER};
        border-bottom-color: {c.SURFACE};
        color: {c.TEXT};
        font-weight: 600;
    }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
        background: {c.SURFACE};
        border: 1px solid {c.BORDER};
        border-radius: 4px;
        padding: 5px 7px;
        selection-background-color: {c.ACCENT_LIGHT};
        selection-color: {c.TEXT};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QDateEdit:focus, QPlainTextEdit:focus {{ border-color: {c.ACCENT}; }}
    QLineEdit:read-only {{ background: {c.GREY_BG}; color: {c.TEXT_MUTED}; }}

    QProgressBar {{
        border: 1px solid {c.BORDER};
        border-radius: 4px;
        background: {c.SURFACE};
        text-align: center;
        height: 18px;
    }}
    QProgressBar::chunk {{ background: {c.ACCENT}; border-radius: 3px; }}

    QStatusBar {{ background: {c.SURFACE}; border-top: 1px solid {c.BORDER}; }}
    QStatusBar::item {{ border: none; }}

    QToolTip {{
        background: #2b3138;
        color: #ffffff;
        border: none;
        padding: 6px 8px;
    }}

    QSplitter::handle {{ background: {c.BORDER}; }}
    QSplitter::handle:horizontal {{ width: 4px; }}
    QSplitter::handle:vertical {{ height: 4px; }}
    """


def badge_style(color: str, background: str) -> str:
    """Stylesheet fuer eine farbige Statusplakette."""
    return (f"background: {background}; color: {color}; border: 1px solid {color};"
            f"border-radius: 9px; padding: 2px 10px; font-weight: 600;")
