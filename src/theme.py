#!/usr/bin/env python3
"""CoreSplit visual system: palette, typography, stylesheet and icon painting.

The look is a quiet dark "ink and amber" scheme: near-black surfaces, one warm
accent for the game and one cool accent for background work. Nothing saturated
or glowing; the colour is only used where it carries meaning.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtCore import QRectF, Qt

ASSET_DIR = Path(__file__).resolve().parent / "assets"
CHECK_SVG = ASSET_DIR / "check.svg"
DOT_SVG = ASSET_DIR / "dot.svg"

# -- palette ---------------------------------------------------------------- #

INK = "#0e1014"
SURFACE = "#15181e"
SURFACE_2 = "#1a1e26"
SURFACE_3 = "#20252e"
LINE = "#262c37"
LINE_SOFT = "#1e232c"

TEXT = "#e9ecf1"
TEXT_2 = "#9aa3b2"
TEXT_3 = "#6b7484"

AMBER = "#dda85e"
AMBER_DIM = "#8a6a3f"
AMBER_TEXT = "#eec38a"

BLUE = "#6f92c9"
BLUE_DIM = "#44587a"
BLUE_TEXT = "#a6bcdf"

GRAY = "#4b5464"
GRAY_TEXT = "#8b94a4"

DANGER = "#d97a7a"

SANS = "Noto Sans"
MONO = "DejaVu Sans Mono"

# -- QColor helpers --------------------------------------------------------- #

def q(value: str, alpha: int | None = None) -> QColor:
    color = QColor(value)
    if alpha is not None:
        color.setAlpha(alpha)
    return color


AMBER_C = q(AMBER)
BLUE_C = q(BLUE)
GRAY_C = q(GRAY)
TEXT_C = q(TEXT)
TEXT_2_C = q(TEXT_2)
TEXT_3_C = q(TEXT_3)
INK_C = q(INK)
SURFACE_C = q(SURFACE)
LINE_C = q(LINE)


def tint(color: QColor, alpha: int) -> QColor:
    out = QColor(color)
    out.setAlpha(alpha)
    return out


ROLE_ACCENT = {"game": AMBER_C, "test": BLUE_C, "other": GRAY_C}
ROLE_TEXT = {"game": AMBER_TEXT, "test": BLUE_TEXT, "other": GRAY_TEXT}
ROLE_LABEL = {"game": "Game", "test": "Tests", "other": "Unpinned"}


# -- typography ------------------------------------------------------------- #

def font(size: float, weight=QFont.Normal, mono: bool = False, spacing: float = 0.0) -> QFont:
    face = QFont(MONO if mono else SANS)
    face.setPointSizeF(size)
    face.setWeight(weight)
    if spacing:
        face.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return face


def caps_font(size: float = 8.0, weight=QFont.DemiBold, spacing: float = 1.3) -> QFont:
    return font(size, weight, spacing=spacing)


# -- stylesheet ------------------------------------------------------------- #

_QSS = Template("""
QWidget { color: $TEXT; font-family: "$SANS"; font-size: 10pt; }
QMainWindow, QWidget#Root { background: $INK; }
QWidget#ScrollContent, QWidget#ScrollViewport { background: transparent; }
QScrollArea { background: transparent; border: none; }

QLabel { background: transparent; }
QLabel#Tagline { color: $TEXT_3; font-size: 9pt; }
QLabel#Hint { color: $TEXT_3; font-size: 9pt; }
QLabel#CardTitle { font-size: 11pt; font-weight: 600; }
QLabel#Status { color: $TEXT_3; font-size: 9pt; }
QLabel#ChipGame { background: rgba(221,168,94,0.13); color: $AMBER_TEXT; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 600; }
QLabel#ChipTest { background: rgba(111,146,201,0.14); color: $BLUE_TEXT; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 600; }

QFrame#Card { background: $SURFACE; border: 1px solid $LINE_SOFT; border-radius: 16px; }
QFrame#Divider { background: $LINE_SOFT; border: none; min-height: 1px; max-height: 1px; }

QPushButton { background: $SURFACE_2; color: $TEXT; border: 1px solid $LINE; border-radius: 9px; padding: 8px 15px; font-size: 9.5pt; }
QPushButton:hover { background: $SURFACE_3; border-color: #333b48; }
QPushButton:pressed { background: $SURFACE; }
QPushButton:disabled { color: $TEXT_3; }
QPushButton[variant="primary"] { background: $AMBER; color: #191307; border: none; font-weight: 600; }
QPushButton[variant="primary"]:hover { background: #e8b979; }
QPushButton[variant="ghost"] { background: transparent; color: $TEXT_2; border: 1px solid $LINE; }
QPushButton[variant="ghost"]:hover { background: $SURFACE_2; color: $TEXT; }
QPushButton[variant="danger"] { background: transparent; border: 1px solid #3d2a2c; color: $DANGER; }
QPushButton[variant="danger"]:hover { background: #241a1c; }

QLineEdit { background: $SURFACE_2; border: 1px solid $LINE; border-radius: 9px; padding: 8px 12px; selection-background-color: $AMBER; selection-color: #191307; }
QLineEdit:focus { border-color: $AMBER_DIM; background: $SURFACE_3; }
QLineEdit::placeholder { color: $TEXT_3; }

QComboBox { background: $SURFACE_2; border: 1px solid $LINE; border-radius: 9px; padding: 7px 12px; color: $TEXT; }
QComboBox:hover { border-color: #333b48; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: none; }
QComboBox QAbstractItemView { background: $SURFACE_2; border: 1px solid $LINE; border-radius: 8px; padding: 4px; outline: none; selection-background-color: $SURFACE_3; selection-color: $TEXT; }

QCheckBox { color: $TEXT_2; font-size: 9.5pt; spacing: 9px; background: transparent; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px; border: 1px solid $LINE; background: $SURFACE_2; }
QCheckBox::indicator:hover { border-color: $AMBER_DIM; }
QCheckBox::indicator:checked { background: $AMBER; border-color: $AMBER; image: url($CHECK); }

QTabWidget::pane { border: none; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: $TEXT_3; padding: 10px 2px; margin-right: 30px; border: none; border-bottom: 2px solid transparent; font-size: 9.5pt; font-weight: 600; }
QTabBar::tab:hover:!selected { color: $TEXT_2; }
QTabBar::tab:selected { color: $TEXT; border-bottom: 2px solid $AMBER; }

QTableView { background: transparent; border: none; gridline-color: transparent; selection-background-color: transparent; alternate-background-color: transparent; outline: none; }
QHeaderView { background: transparent; }
QHeaderView::section { background: transparent; color: $TEXT_3; border: none; border-bottom: 1px solid $LINE; padding: 9px 12px; font-size: 8.2pt; font-weight: 600; }
QTableCornerButton::section { background: transparent; border: none; }

QTextEdit, QPlainTextEdit { background: transparent; border: none; color: $TEXT_2; selection-background-color: rgba(221,168,94,0.22); }

QScrollBar:vertical { background: transparent; width: 9px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #333a46; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #414a59; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 0 2px; }
QScrollBar::handle:horizontal { background: #333a46; border-radius: 4px; min-width: 32px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; background: none; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }

QSplitter::handle { background: transparent; height: 14px; }
QSplitter::handle:hover { background: $LINE_SOFT; }

QStatusBar { background: transparent; border-top: 1px solid $LINE_SOFT; color: $TEXT_3; }
QStatusBar::item { border: none; }

QMenu { background: $SURFACE_2; border: 1px solid $LINE; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 8px 30px 8px 12px; border-radius: 7px; color: $TEXT; font-size: 9.5pt; }
QMenu::item:selected { background: $SURFACE_3; }
QMenu::separator { height: 1px; background: $LINE; margin: 6px 8px; }
QMenu::indicator { width: 15px; height: 15px; }
QMenu::indicator:checked { image: url($DOT); }

QToolTip { background: $SURFACE_3; color: $TEXT; border: 1px solid $LINE; border-radius: 7px; padding: 6px 9px; }
""")


def stylesheet() -> str:
    return _QSS.substitute(
        INK=INK, SURFACE=SURFACE, SURFACE_2=SURFACE_2, SURFACE_3=SURFACE_3,
        LINE=LINE, LINE_SOFT=LINE_SOFT, TEXT=TEXT, TEXT_2=TEXT_2, TEXT_3=TEXT_3,
        AMBER=AMBER, AMBER_DIM=AMBER_DIM, AMBER_TEXT=AMBER_TEXT,
        BLUE=BLUE, BLUE_TEXT=BLUE_TEXT, GRAY=GRAY, GRAY_TEXT=GRAY_TEXT,
        DANGER=DANGER, SANS=SANS, MONO=MONO,
        CHECK=str(CHECK_SVG), DOT=str(DOT_SVG),
    )


def apply(app):
    """Give the app a predictable base style, then layer our sheet on top."""
    app.setStyle("Fusion")
    base = font(10)
    app.setFont(base)
    app.setStyleSheet(stylesheet())


# -- icon ------------------------------------------------------------------- #

def _icon_pixmap(mode: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    pad = size * 0.09
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.26
    gap = max(1.0, size * 0.03)

    left = AMBER_C if mode != "off" else GRAY_C
    right = BLUE_C if mode != "off" else GRAY_C

    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.setClipPath(clip)
    painter.setPen(Qt.NoPen)
    painter.setBrush(left)
    painter.drawRect(QRectF(rect.left(), rect.top(), rect.width() / 2 - gap / 2, rect.height()))
    painter.setBrush(right)
    painter.drawRect(QRectF(rect.center().x() + gap / 2, rect.top(),
                            rect.width() / 2 - gap / 2, rect.height()))

    sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    sheen.setColorAt(0.0, QColor(255, 255, 255, 46))
    sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
    painter.fillRect(rect, sheen)
    painter.setClipping(False)

    if mode == "hard":
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 190), max(1.0, size * 0.025)))
        painter.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), radius, radius)

    painter.end()
    return pixmap


def make_icon(mode: str = "off") -> QIcon:
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_icon_pixmap(mode, size))
    return icon
