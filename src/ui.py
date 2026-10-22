#!/usr/bin/env python3
"""CoreSplit GUI: one calm stage built around the chip itself.

The window answers a single question — *is the machine split right now?* —
with a large visualisation of the CPU that splits into its two halves when a
mode engages and heals back into one when it doesn't. Mode selection sits
directly beneath it; processes and settings live one quiet nav click away.
Everything visual is custom painted where the stock widget look would show.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QEasingCurve, QModelIndex, QObject, QPointF, QRectF, QSize,
    Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QFontMetrics, QIcon,
    QPainter, QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QStackedLayout,
    QStackedWidget, QStyle, QStyledItemDelegate, QSystemTrayIcon, QTableView,
    QTextEdit, QVBoxLayout, QWidget,
)

import engine as eng
import theme as th


# --------------------------------------------------------------------------- #
# bridge: engine thread -> Qt main thread
# --------------------------------------------------------------------------- #

class Bridge(QObject):
    snapshot = Signal(dict)
    log = Signal(str)
    notice = Signal(str, str)
    # Raised from the IPC thread; connected to the window with a queued
    # connection so the GUI call actually lands on the main thread.
    show_requested = Signal()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def caps(text: str, color: str = th.TEXT_3, size: float = 8.0) -> QLabel:
    label = QLabel(text.upper())
    label.setFont(th.caps_font(size))
    label.setStyleSheet(f"color: {color}; background: transparent;")
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.NoFrame)
    return line


def variant(button: QPushButton, name: str) -> QPushButton:
    button.setProperty("variant", name)
    return button


def fmt_range(spec: str) -> str:
    """0-5,12-17 -> 0–5 · 12–17, for display only."""
    return " · ".join(part.strip().replace("-", "–") for part in spec.split(",") if part.strip())


# --------------------------------------------------------------------------- #
# switch: an animated pill toggle
# --------------------------------------------------------------------------- #

class Switch(QWidget):
    """A small pill switch with QCheckBox's essential API surface."""

    toggled = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setFixedSize(42, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._checked = False
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        """Programmatic change: moves the thumb without emitting toggled."""
        checked = bool(checked)
        if checked == self._checked:
            self._pos = 1.0 if checked else 0.0
            self.update()
            return
        self._checked = checked
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    def _flip(self):
        self._checked = not self._checked
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if self._checked else 0.0)
        self._anim.start()
        self.toggled.emit(self._checked)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self._flip()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return):
            self._flip()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = QRectF(0, 0, 42, 24)
        if self._checked or self._pos > 0.0:
            fill = th.mix(th.q(th.SURFACE_3), th.AMBER_C, self._pos)
            border = th.mix(th.LINE_C, th.AMBER_C, self._pos)
        else:
            fill = th.q(th.SURFACE_3)
            border = th.LINE_C
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        x = 3.0 + self._pos * (42 - 24 - 3)
        thumb = th.mix(th.q("#8d97a8"), th.q("#221708"), self._pos)
        painter.setPen(Qt.NoPen)
        painter.setBrush(thumb)
        painter.drawEllipse(QPointF(x + 9, 12), 8.0, 8.0)
        painter.end()


# --------------------------------------------------------------------------- #
# mode selector
# --------------------------------------------------------------------------- #

class ModeSelector(QWidget):
    """Three-way selector with a sliding thumb. The one primary control."""

    changed = Signal(str)
    OPTIONS = (("off", "Off"), ("smart", "Smart"), ("hard", "Hard"))

    def __init__(self):
        super().__init__()
        self.setFixedSize(380, 48)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._index = 0
        self._pos = 0.0
        self._hover = -1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def mode(self) -> str:
        return self.OPTIONS[self._index][0]

    def set_mode(self, mode: str):
        for index, (key, _) in enumerate(self.OPTIONS):
            if key != mode:
                continue
            if index == self._index:
                return
            if not self.isVisible():
                self._index = index
                self._pos = float(index)
                self.update()
                return
            self._anim.stop()
            self._anim.setStartValue(float(self._pos))
            self._anim.setEndValue(float(index))
            self._anim.start()
            self._index = index
            return

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    def _segment_rect(self, index) -> QRectF:
        rect = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        width = rect.width() / len(self.OPTIONS)
        return QRectF(rect.left() + index * width, rect.top(), width, rect.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(th.LINE_C, 1))
        painter.setBrush(th.q(th.SURFACE))
        painter.drawRoundedRect(track, 22, 22)

        key = self.mode()
        accent = {"off": th.q("#262d3a"), "smart": th.BLUE_C, "hard": th.AMBER_C}[key]
        thumb = self._segment_rect(self._pos)
        painter.setPen(QPen(th.q("#3d4655"), 1) if key == "off" else Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(thumb, 18, 18)

        label_font = th.font(10.2, QFont.DemiBold)
        painter.setFont(label_font)
        for index, (option_key, label) in enumerate(self.OPTIONS):
            segment = self._segment_rect(index)
            if index == self._index:
                color = th.q("#221708") if option_key != "off" else th.TEXT_C
            elif index == self._hover:
                color = th.TEXT_C
            else:
                color = th.TEXT_3_C
            painter.setPen(QPen(color))
            painter.drawText(segment, int(Qt.AlignCenter), label)
        painter.end()

    def _index_at(self, x: float) -> int:
        rect = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        width = rect.width() / len(self.OPTIONS)
        return max(0, min(len(self.OPTIONS) - 1, int((x - rect.left()) // width)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            index = self._index_at(event.position().x())
            if index != self._index:
                self._index = index
                self._pos = float(index)
                self.update()
                self.changed.emit(self.OPTIONS[index][0])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            step = -1 if event.key() == Qt.Key_Left else 1
            index = max(0, min(len(self.OPTIONS) - 1, self._index + step))
            if index != self._index:
                self._index = index
                self._pos = float(index)
                self.update()
                self.changed.emit(self.OPTIONS[index][0])
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# split stage: the chip, drawn as it splits and heals
# --------------------------------------------------------------------------- #

class SplitStage(QWidget):
    """One panel when everything is shared; two when a split is engaged.

    Panel widths are proportional to the physical cores on each side, so the
    8/4 preset reads at a glance. The transition between the two states is the
    whole point: the machine visibly comes apart and back together.
    """

    HEIGHT = 196

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._game_cores: list[list[int]] = []
        self._other_cores: list[list[int]] = []
        self._game_range = ""
        self._other_range = ""
        self._all_range = ""
        self._total = 0
        self._pos = 0.0
        self._target = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def set_data(self, topo, game_cpus, test_cpus, active: bool):
        game = eng.parse_cpulist(game_cpus)
        test = eng.parse_cpulist(test_cpus)
        self._game_cores = [core for core in topo.physical_cores() if set(core) & game]
        self._other_cores = [core for core in topo.physical_cores() if core not in self._game_cores]
        self._total = len(self._game_cores) + len(self._other_cores)
        self._game_range = fmt_range(eng.format_cpulist(sorted(game)))
        self._other_range = fmt_range(eng.format_cpulist(sorted(test)))
        self._all_range = fmt_range(eng.format_cpulist(topo.all_cpus))
        target = 1.0 if active else 0.0
        if target != self._target:
            self._target = target
            if not self.isVisible():
                self._pos = target
                self.update()
                return
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(target)
            self._anim.start()

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    # -- painting ---------------------------------------------------------- #

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        if not self._total:
            painter.end()
            return

        p = self._pos
        width = self.width()
        height = float(self.HEIGHT)
        radius = 24.0

        game_fill = th.mix(th.q(th.SURFACE), th.tint(th.AMBER_C, 30), p)
        game_border = th.mix(th.LINE_C, th.tint(th.AMBER_C, 90), p)
        other_fill = th.mix(th.q(th.SURFACE), th.tint(th.BLUE_C, 24), p)
        other_border = th.mix(th.LINE_C, th.tint(th.BLUE_C, 74), p)
        shared_fill = th.q(th.SURFACE)
        shared_border = th.LINE_C

        if p < 0.995:
            # Shared state: a single quiet panel holding every core.
            alpha = 1.0 - p
            rect = QRectF(0, 0, width, height)
            self._panel(
                painter, rect, radius, shared_fill, shared_border,
                label="ALL CORES · SHARED", label_alpha=alpha * 0.9,
                count=f"{self._total} cores", count_alpha=alpha,
                sub=self._all_range, sub_alpha=alpha * 0.8,
                cores=self._game_cores + self._other_cores,
                dot_color=th.q("#5b6474"), label_color=th.TEXT_3_C,
            )

        if p > 0.005:
            gap = 34.0 * p
            game_n = max(1, len(self._game_cores))
            other_n = max(1, len(self._other_cores))
            usable = width - gap
            game_w = usable * game_n / self._total
            game_rect = QRectF(0, 0, game_w, height)
            other_rect = QRectF(game_w + gap, 0, usable - game_w, height)
            fade = max(0.0, (p - 0.25) / 0.75)  # text arrives as the gap opens

            self._panel(
                painter, game_rect, radius, game_fill, game_border,
                label="GAME", label_alpha=fade, label_color=th.mix(th.TEXT_3_C, th.q(th.AMBER_TEXT), p),
                count=f"{game_n} cores", count_alpha=fade,
                sub=self._game_range, sub_alpha=fade * 0.85,
                cores=self._game_cores,
                dot_color=th.mix(th.q("#5b6474"), th.AMBER_C, p),
            )
            self._panel(
                painter, other_rect, radius, other_fill, other_border,
                label="EVERYTHING ELSE", label_alpha=fade, label_color=th.mix(th.TEXT_3_C, th.q(th.BLUE_TEXT), p),
                count=f"{other_n} cores", count_alpha=fade,
                sub=self._other_range, sub_alpha=fade * 0.85,
                cores=self._other_cores,
                dot_color=th.mix(th.q("#5b6474"), th.BLUE_C, p),
            )
        painter.end()

    def _panel(self, painter, rect: QRectF, radius, fill, border, *, label, label_alpha,
               label_color=None, count, count_alpha, sub, sub_alpha, cores, dot_color):
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        pad = 24.0
        inner_w = rect.width() - pad * 2
        if inner_w < 40:
            return

        label_color = label_color or th.TEXT_3_C
        lc = QColor(label_color)
        lc.setAlphaF(max(0.0, label_alpha * 0.95))
        painter.setFont(th.caps_font(7.8, spacing=1.5))
        painter.setPen(QPen(lc))
        painter.drawText(QRectF(rect.left() + pad, rect.top() + 18, inner_w, 14),
                         int(Qt.AlignLeft | Qt.AlignVCenter), label)

        cc = QColor(th.TEXT_C)
        cc.setAlphaF(max(0.0, count_alpha))
        painter.setFont(th.font(15.5, QFont.DemiBold))
        painter.setPen(QPen(cc))
        painter.drawText(QRectF(rect.left() + pad, rect.top() + 34, inner_w, 24),
                         int(Qt.AlignLeft | Qt.AlignVCenter), count)

        sc = QColor(th.TEXT_3_C)
        sc.setAlphaF(max(0.0, sub_alpha))
        painter.setFont(th.font(8.2, mono=True))
        painter.setPen(QPen(sc))
        painter.drawText(QRectF(rect.left() + pad, rect.top() + 58, inner_w, 14),
                         int(Qt.AlignLeft | Qt.AlignVCenter), sub)

        # One dot per physical core, sized to fit the panel.
        n = max(1, len(cores))
        gap = 9.0
        dot = max(6.0, min(13.0, (inner_w - gap * (n - 1)) / n))
        cy = rect.bottom() - 24.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot_color)
        x = rect.left() + pad
        for _core in cores:
            painter.drawEllipse(QPointF(x + dot / 2, cy), dot / 2, dot / 2)
            x += dot + gap


# --------------------------------------------------------------------------- #
# process table
# --------------------------------------------------------------------------- #

class ProcessModel(QAbstractTableModel):
    HEADERS = ["Process", "CPU", "Threads", "Role", "Affinity"]
    WIDTHS = [340, 84, 92, 132, 150]
    NUMERIC = (1, 2)
    RoleKind = Qt.UserRole + 1

    def __init__(self):
        super().__init__()
        self._rows: list[dict] = []
        self._visible: list[dict] = []
        self._filter = ""
        self._show_all = False

    def set_rows(self, rows):
        self._rows = rows
        self._refresh_view()

    def set_filter(self, text):
        self._filter = text.strip().lower()
        self._refresh_view()

    def set_show_all(self, show_all):
        self._show_all = bool(show_all)
        self._refresh_view()

    def _refresh_view(self):
        self.beginResetModel()
        rows = self._rows if self._show_all else [r for r in self._rows if r["own"]]
        if self._filter:
            needle = self._filter
            rows = [
                row for row in rows
                if needle in row["name"].lower()
                or needle in row["scope"].lower()
                or needle == str(row["pid"])
                or needle in row["role"]
            ]
        self._visible = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.HEADERS[section].upper()
        if role == Qt.TextAlignmentRole:
            side = Qt.AlignRight if section in self.NUMERIC else Qt.AlignLeft
            return int(side | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._visible[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            if column == 0:
                return row["name"]
            if column == 1:
                return f"{row['cpu']:.1f}"
            if column == 2:
                return str(row["threads"])
            if column == 3:
                return th.ROLE_LABEL.get(row["role"], row["role"])
            if column == 4:
                return fmt_range(row["affinity"]) if row["affinity"] != "?" else "?"
        elif role == self.RoleKind:
            return row["role"]
        elif role == Qt.TextAlignmentRole and column in self.NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ToolTipRole:
            return (f"pid {row['pid']}   threads {row['threads']}\n"
                    f"affinity {row['affinity']}\nscope {row['scope'] or '-'}")
        return None

    def pid_at(self, row):
        if 0 <= row < len(self._visible):
            return self._visible[row]["pid"]
        return None


class ProcDelegate(QStyledItemDelegate):
    """Quiet rows: the name carries a pid sub-line, roles are a dot and a word."""

    def __init__(self):
        super().__init__()
        self.sans = th.font(9.8)
        self.sub = th.font(7.9)
        self.mono = th.font(8.8, mono=True)
        self.role_font = th.font(8.8, QFont.DemiBold)
        self.pad = 16

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(th.tint(th.AMBER_C, 30))
            painter.drawRoundedRect(rect.adjusted(6, 3, -6, -3), 9, 9)
        elif hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(th.tint(th.TEXT_C, 8))
            painter.drawRoundedRect(rect.adjusted(6, 3, -6, -3), 9, 9)

        column = index.column()
        role = index.data(ProcessModel.RoleKind)
        text = index.data(Qt.DisplayRole) or ""

        if column == 0:
            self._name(painter, rect, index, role)
        elif column == 3:
            self._role(painter, rect, text, role)
        else:
            align = Qt.AlignRight if column in ProcessModel.NUMERIC else Qt.AlignLeft
            color = th.TEXT_2_C if column == 4 else th.TEXT_C
            self._text(painter, rect, text, column in (1, 2, 4), align, color)
        painter.restore()

    def _text(self, painter, rect, text, mono, align, color):
        painter.setFont(self.mono if mono else self.sans)
        painter.setPen(QPen(color))
        inner = rect.adjusted(self.pad, 0, -self.pad, 0)
        metrics = QFontMetrics(painter.font())
        elided = metrics.elidedText(text, Qt.ElideRight, int(inner.width()))
        painter.drawText(inner, int(align | Qt.AlignVCenter), elided)

    def _role(self, painter, rect, text, role):
        accent = th.ROLE_ACCENT.get(role, th.GRAY_C)
        text_color = th.ROLE_TEXT.get(role, th.GRAY_TEXT) if role != "other" else th.TEXT_3
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        dot_x = rect.left() + self.pad + 3.5
        painter.drawEllipse(QPointF(dot_x, rect.center().y()), 3.5, 3.5)
        painter.setFont(self.role_font)
        painter.setPen(QPen(QColor(text_color)))
        inner = rect.adjusted(self.pad + 12, 0, -self.pad, 0)
        metrics = QFontMetrics(painter.font())
        elided = metrics.elidedText(text, Qt.ElideRight, int(inner.width()))
        painter.drawText(inner, int(Qt.AlignLeft | Qt.AlignVCenter), elided)

    def _name(self, painter, rect, index, role):
        name = index.data(Qt.DisplayRole) or ""
        pid_line = ""
        row = index.row()
        model = index.model()
        if model is not None and 0 <= row < model.rowCount():
            pid_line = f"pid {model._visible[row]['pid']}"
            scope = model._visible[row]["scope"]
            if scope:
                pid_line += f" · {scope}"
        painter.setFont(self.sans)
        painter.setPen(QPen(th.TEXT_C))
        top = QRectF(rect.left() + self.pad, rect.top() + 6, rect.width() - self.pad - 12, 17)
        metrics = QFontMetrics(self.sans)
        painter.drawText(top, int(Qt.AlignLeft | Qt.AlignVCenter),
                         metrics.elidedText(name, Qt.ElideRight, int(top.width())))
        if pid_line:
            painter.setFont(self.sub)
            painter.setPen(QPen(th.TEXT_3_C))
            bottom = QRectF(rect.left() + self.pad, rect.top() + 22, rect.width() - self.pad - 12, 13)
            metrics = QFontMetrics(self.sub)
            painter.drawText(bottom, int(Qt.AlignLeft | Qt.AlignVCenter),
                             metrics.elidedText(pid_line, Qt.ElideMiddle, int(bottom.width())))


# --------------------------------------------------------------------------- #
# settings row
# --------------------------------------------------------------------------- #

class SettingRow(QWidget):
    """Title and description on the left, control on the right."""

    def __init__(self, title: str, description: str, control: QWidget | None = None):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("SettingTitle")
        text.addWidget(title_label)
        if description:
            desc_label = QLabel(description)
            desc_label.setObjectName("SettingDesc")
            desc_label.setWordWrap(True)
            text.addWidget(desc_label)
        layout.addLayout(text, 1)
        if control is not None:
            layout.addWidget(control, 0, Qt.AlignTop)


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #

MODE_DESCRIPTIONS = {
    "off": "Everything shares every core. Nothing is pinned.",
    "smart": "Recognised test and agent processes are parked on the other side.",
    "hard": "Everything except the game is parked on the other side.",
}


class MainWindow(QMainWindow):
    def __init__(self, engine, bridge, config):
        super().__init__()
        self.engine = engine
        self.bridge = bridge
        self.cfg = config
        self.setWindowTitle("CoreSplit")
        self.resize(1000, 700)
        self.setMinimumSize(880, 640)

        self._build()
        bridge.snapshot.connect(self._on_snapshot)
        bridge.log.connect(self._append_log)

    # -- construction ------------------------------------------------------ #

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addLayout(self._header())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._status_page())
        self.stack.addWidget(self._processes_page())
        self.stack.addWidget(self._settings_page())
        outer.addWidget(self.stack, 1)

    def _header(self):
        row = QHBoxLayout()
        row.setContentsMargins(28, 18, 20, 14)
        row.setSpacing(4)

        wordmark = QLabel("CoreSplit")
        wordmark.setObjectName("Wordmark")
        row.addWidget(wordmark)
        row.addStretch(1)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for index, name in enumerate(("Status", "Processes", "Settings")):
            button = variant(QPushButton(name), "nav")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            self._nav_group.addButton(button, index)
            row.addWidget(button)
        return row

    # -- page 1: status ---------------------------------------------------- #

    def _status_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(40, 8, 40, 26)
        outer.setSpacing(0)

        column = QVBoxLayout()
        column.setSpacing(0)

        self.eyebrow = caps("Starting…", th.TEXT_3, 8.4)
        self.eyebrow.setAlignment(Qt.AlignCenter)
        column.addWidget(self.eyebrow)
        column.addSpacing(10)

        self.title = QLabel("CoreSplit")
        self.title.setFont(th.font(27, QFont.DemiBold))
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("background: transparent;")
        column.addWidget(self.title)
        column.addSpacing(8)

        self.subtitle = QLabel("")
        self.subtitle.setFont(th.font(9.6))
        self.subtitle.setStyleSheet(f"color: {th.TEXT_2}; background: transparent;")
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setWordWrap(True)
        column.addWidget(self.subtitle)

        column.addStretch(1)

        stage_holder = QHBoxLayout()
        stage_holder.addStretch(1)
        self.stage = SplitStage()
        stage_holder.addWidget(self.stage, 12)
        stage_holder.addStretch(1)
        column.addLayout(stage_holder)

        column.addStretch(1)

        selector_holder = QHBoxLayout()
        selector_holder.addStretch(1)
        self.segments = ModeSelector()
        self.segments.changed.connect(self._user_set_mode)
        selector_holder.addWidget(self.segments)
        selector_holder.addStretch(1)
        column.addLayout(selector_holder)
        column.addSpacing(12)

        self.mode_desc = QLabel(MODE_DESCRIPTIONS["off"])
        self.mode_desc.setFont(th.font(9.2))
        self.mode_desc.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        self.mode_desc.setAlignment(Qt.AlignCenter)
        column.addWidget(self.mode_desc)

        column.addStretch(1)

        auto_row = QHBoxLayout()
        auto_row.addStretch(1)
        auto_row.setSpacing(10)
        self.auto_check = Switch()
        self.auto_check.setChecked(bool(self.cfg["auto_enable"]))
        auto_label = QLabel("Engage automatically when a game starts")
        auto_label.setFont(th.font(9.4))
        auto_label.setStyleSheet(f"color: {th.TEXT_2}; background: transparent;")
        auto_row.addWidget(self.auto_check)
        auto_row.addWidget(auto_label)

        using = QLabel("using")
        using.setFont(th.font(9.0))
        using.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        auto_row.addSpacing(6)
        auto_row.addWidget(using)
        self.auto_mode = QComboBox()
        self.auto_mode.addItem("Hard split", "hard")
        self.auto_mode.addItem("Smart split", "smart")
        index = self.auto_mode.findData(self.cfg["auto_mode"])
        self.auto_mode.setCurrentIndex(max(0, index))
        self.auto_mode.setCursor(Qt.PointingHandCursor)
        auto_row.addWidget(self.auto_mode)
        auto_row.addStretch(1)
        column.addLayout(auto_row)

        column.addSpacing(14)

        self.recent = QLabel("")
        self.recent.setFont(th.font(8.4))
        self.recent.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        self.recent.setAlignment(Qt.AlignCenter)
        column.addWidget(self.recent)

        outer.addLayout(column, 1)

        self.auto_check.toggled.connect(self._auto_toggled)
        self.auto_mode.currentIndexChanged.connect(self._auto_mode_changed)
        return page

    # -- page 2: processes ------------------------------------------------- #

    def _processes_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 4, 28, 22)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(12)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search processes")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(250)
        self.filter.textChanged.connect(self._filter_changed)
        toolbar.addWidget(self.filter)

        self.show_all = QCheckBox("All users")
        self.show_all.toggled.connect(self._show_all_toggled)
        toolbar.addWidget(self.show_all)
        toolbar.addStretch(1)

        self.proc_message = QLabel("")
        self.proc_message.setFont(th.font(8.8))
        self.proc_message.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        toolbar.addWidget(self.proc_message)
        self._proc_message_timer = QTimer(self)
        self._proc_message_timer.setSingleShot(True)
        self._proc_message_timer.timeout.connect(lambda: self.proc_message.setText(""))

        self._pin_game_btn = variant(QPushButton("Pin to game"), "ghost")
        self._pin_game_btn.clicked.connect(lambda: self._pin_selected("game"))
        toolbar.addWidget(self._pin_game_btn)

        self._pin_test_btn = variant(QPushButton("Pin to tests"), "ghost")
        self._pin_test_btn.clicked.connect(lambda: self._pin_selected("test"))
        toolbar.addWidget(self._pin_test_btn)

        release = variant(QPushButton("Release"), "ghost")
        release.clicked.connect(self._release_selected)
        toolbar.addWidget(release)
        layout.addLayout(toolbar)

        self.model = ProcessModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(ProcDelegate())
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setFixedHeight(34)
        for index, width in enumerate(ProcessModel.WIDTHS):
            self.table.setColumnWidth(index, width)

        # A soft empty state instead of a silent void.
        table_holder = QWidget()
        stack = QStackedLayout(table_holder)
        stack.setStackingMode(QStackedLayout.StackAll)
        stack.addWidget(self.table)
        self.empty_hint = QLabel("No processes match")
        self.empty_hint.setFont(th.font(9.4))
        self.empty_hint.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.empty_hint.setVisible(False)
        stack.addWidget(self.empty_hint)
        stack.setCurrentWidget(self.table)
        layout.addWidget(table_holder, 1)

        self.model.modelReset.connect(
            lambda: self.empty_hint.setVisible(self.model.rowCount() == 0)
        )
        return page

    # -- page 3: settings -------------------------------------------------- #

    def _settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("ScrollViewport")

        content = QWidget()
        content.setObjectName("ScrollContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(40, 6, 40, 26)
        layout.setSpacing(6)

        # -- the split ------------------------------------------------------ #
        layout.addWidget(caps("The split", th.TEXT_3, 8.2))
        layout.addSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.addWidget(caps("Game keeps", th.TEXT_2, 7.6), 0, 0)
        grid.addWidget(caps("Everything else gets", th.TEXT_2, 7.6), 0, 1)
        self.game_cpus = QLineEdit(self.cfg["game_cpus"])
        self.test_cpus = QLineEdit(self.cfg["test_cpus"])
        self.game_cpus.setPlaceholderText("e.g. 0-5,12-17")
        self.test_cpus.setPlaceholderText("e.g. 6-11,18-23")
        self.game_cpus.textChanged.connect(self._update_split_hint)
        self.test_cpus.textChanged.connect(self._update_split_hint)
        grid.addWidget(self.game_cpus, 1, 0)
        grid.addWidget(self.test_cpus, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        presets_row = QHBoxLayout()
        presets_row.setSpacing(8)
        for label, cores in (("Balanced 6/6", 6), ("Game-heavy 8/4", 8), ("Test-heavy 4/8", 4)):
            button = variant(QPushButton(label), "ghost")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, c=cores: self._apply_preset(c))
            presets_row.addWidget(button)
        presets_row.addStretch(1)
        apply_button = variant(QPushButton("Apply split"), "primary")
        apply_button.setCursor(Qt.PointingHandCursor)
        apply_button.clicked.connect(self._apply_split)
        presets_row.addWidget(apply_button)
        layout.addLayout(presets_row)

        self.split_hint = QLabel("")
        self.split_hint.setObjectName("Hint")
        self.split_hint.setWordWrap(True)
        layout.addSpacing(2)
        layout.addWidget(self.split_hint)

        layout.addSpacing(18)
        layout.addWidget(divider())
        layout.addSpacing(14)

        # -- behaviour ------------------------------------------------------ #
        layout.addWidget(caps("Behaviour", th.TEXT_3, 8.2))
        layout.addSpacing(4)

        self.renice_check = Switch()
        self.renice_check.setChecked(bool(self.cfg["smart_renice"]))
        self.renice_check.toggled.connect(self._options_changed)
        layout.addWidget(SettingRow(
            "Lower priority of test processes",
            "Reniced, so parked work never wins a scheduling fight with the game.",
            self.renice_check))

        self.ioprio_check = Switch()
        self.ioprio_check.setChecked(bool(self.cfg["smart_ioprio"]))
        self.ioprio_check.toggled.connect(self._options_changed)
        layout.addWidget(SettingRow(
            "Deprioritise test disk I/O",
            "Parked processes wait behind the game for disk access too.",
            self.ioprio_check))

        self.autostart_check = Switch()
        self.autostart_check.setChecked(bool(self.cfg["autostart"]))
        self.autostart_check.toggled.connect(self._autostart_toggled)
        layout.addWidget(SettingRow(
            "Start CoreSplit at login",
            "Hidden in the tray, ready before the first game launches.",
            self.autostart_check))

        self.guard_check = Switch()
        self.guard_check.setChecked(bool(self.cfg["focus_guard"]))
        self.guard_check.toggled.connect(self._guard_toggled)
        layout.addWidget(SettingRow(
            "Focus guard",
            "Agent windows are bounced before they can steal focus from a game. "
            "Your own alt-tab still works.",
            self.guard_check))

        self.guard_hint = QLabel("")
        self.guard_hint.setObjectName("Hint")
        self.guard_hint.setWordWrap(True)
        left_indent = QHBoxLayout()
        left_indent.addSpacing(0)
        left_indent.addWidget(self.guard_hint, 1)
        layout.addLayout(left_indent)

        layout.addSpacing(14)
        layout.addWidget(divider())
        layout.addSpacing(14)

        # -- tools ---------------------------------------------------------- #
        layout.addWidget(caps("Tools", th.TEXT_3, 8.2))
        layout.addSpacing(4)

        steam_button = variant(QPushButton("Copy"), "ghost")
        steam_button.setCursor(Qt.PointingHandCursor)
        steam_button.clicked.connect(self._copy_steam_option)
        layout.addWidget(SettingRow(
            "Steam launch option",
            "The exact taskset line for this split, ready to paste into a game's "
            "launch options.",
            steam_button))

        wrapper_button = variant(QPushButton("Install"), "ghost")
        wrapper_button.setCursor(Qt.PointingHandCursor)
        wrapper_button.clicked.connect(self._install_wrapper)
        layout.addWidget(SettingRow(
            "testcpus wrapper",
            "Run a one-off command on the test side from any shell: "
            "testcpus cargo test",
            wrapper_button))

        load_button = variant(QPushButton("Run"), "ghost")
        load_button.setCursor(Qt.PointingHandCursor)
        load_button.clicked.connect(self._run_test_load)
        layout.addWidget(SettingRow(
            "Test load",
            "Ten seconds of load on the test side. Watch the game's frame time "
            "stay flat — that is the split working.",
            load_button))

        reset_button = variant(QPushButton("Release all"), "danger")
        reset_button.setCursor(Qt.PointingHandCursor)
        reset_button.clicked.connect(self._reset_all)
        layout.addWidget(SettingRow(
            "Release all pins",
            "Unpin every process, even if the app was killed uncleanly.",
            reset_button))

        self.tools_hint = QLabel("")
        self.tools_hint.setObjectName("Hint")
        self.tools_hint.setWordWrap(True)
        layout.addSpacing(2)
        layout.addWidget(self.tools_hint)

        layout.addSpacing(18)
        layout.addWidget(divider())
        layout.addSpacing(14)

        # -- activity ------------------------------------------------------- #
        layout.addWidget(caps("Activity", th.TEXT_3, 8.2))
        layout.addSpacing(8)
        log_panel = QFrame()
        log_panel.setStyleSheet(
            f"background: {th.SURFACE}; border: 1px solid {th.LINE_SOFT}; border-radius: 14px;")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(14, 10, 14, 10)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setAcceptRichText(True)
        self.log_view.setFont(th.font(8.6, mono=True))
        self.log_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.log_view.setFixedHeight(170)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_panel)

        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        return page

    # -- engine -> ui ------------------------------------------------------ #

    def _on_snapshot(self, snap):
        mode = snap["mode"]
        self.segments.set_mode(mode)
        self.stage.set_data(
            self.engine.topo, snap["game_cpus"], snap["test_cpus"],
            active=mode in ("smart", "hard"),
        )
        self._update_status_text(snap)
        self.model.set_rows(snap["procs"])
        self._update_split_hint()
        self.setWindowIcon(th.make_icon(mode))

        if self.log_view.document().isEmpty() and snap["logs"]:
            for line in snap["logs"]:
                self._append_log(line)

    def _update_status_text(self, snap):
        mode = snap["mode"]
        game = snap.get("game")
        counts = snap["counts"]

        if mode == "off":
            self.eyebrow.setText("OFF · EVERYTHING SHARED")
            self.eyebrow.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
            self.title.setText("Nothing is pinned")
            self.subtitle.setText(
                f"All {self.engine.topo.core_count(self.engine.topo.all_cpus)} physical cores "
                "serve whatever you run.")
        else:
            word = "HARD SPLIT" if mode == "hard" else "SMART SPLIT"
            accent = th.AMBER_TEXT if mode == "hard" else th.BLUE_TEXT
            if game:
                self.eyebrow.setText(f"{word} · PROTECTING")
                self.eyebrow.setStyleSheet(f"color: {accent}; background: transparent;")
                self.title.setText(game["name"])
                n = counts.get("game", 0)
                self.subtitle.setText(
                    f"{n} process{'es' if n != 1 else ''} on the game side · "
                    f"detected via {game['reason']}")
            else:
                self.eyebrow.setText(f"{word} · STANDBY")
                self.eyebrow.setStyleSheet(f"color: {accent}; background: transparent;")
                self.title.setText("Waiting for a game")
                if snap.get("auto"):
                    self.subtitle.setText("The split engages the moment a game starts.")
                else:
                    self.subtitle.setText("Start a game and CoreSplit will follow.")

        self.mode_desc.setText(MODE_DESCRIPTIONS.get(mode, MODE_DESCRIPTIONS["off"]))

    def _append_log(self, line):
        match = re.match(r"^\[(\d\d:\d\d:\d\d)\]\s*(.*)$", line)
        if match:
            stamp, body_text = match.group(1), match.group(2)
            body = (f'<span style="color:{th.TEXT_3}">{stamp}</span>'
                    f'&nbsp;&nbsp;<span style="color:{th.TEXT_2}">{html.escape(body_text)}</span>')
            self.recent.setText(f"{stamp}  ·  {body_text}")
        else:
            body = f'<span style="color:{th.TEXT_2}">{html.escape(line)}</span>'
            self.recent.setText(line)
        self.log_view.append(body)

    # -- user actions ------------------------------------------------------ #

    def _user_set_mode(self, mode):
        self.engine.set_mode(mode, manual=True)

    def _auto_toggled(self, checked):
        self.cfg["auto_enable"] = bool(checked)
        self.cfg.save()

    def _auto_mode_changed(self):
        self.cfg["auto_mode"] = self.auto_mode.currentData()
        self.cfg.save()

    def _options_changed(self):
        self.cfg["smart_renice"] = self.renice_check.isChecked()
        self.cfg["smart_ioprio"] = self.ioprio_check.isChecked()
        self.cfg.save()
        self.engine.request("apply_config")

    def _guard_toggled(self, checked):
        self.cfg["focus_guard"] = bool(checked)
        self.cfg.save()
        import guard
        guard.set_enabled(bool(checked))
        if checked:
            self.engine.attach_publisher(guard.Publisher())
        else:
            self.engine.attach_publisher(None)
        self.set_guard_status(bool(checked) and guard.enabled())

    def set_guard_status(self, ready: bool):
        self._guard_ready = bool(ready)
        self._refresh_guard_hint()

    def _refresh_guard_hint(self):
        if not hasattr(self, "guard_hint"):
            return
        if not self.cfg["focus_guard"]:
            self.guard_hint.setText("Off — agent windows may take focus from the game.")
            self.guard_hint.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
            return
        if getattr(self, "_guard_ready", False):
            self.guard_hint.setText(
                "On — agent windows are bounced while a game is protected.")
            self.guard_hint.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        else:
            self.guard_hint.setText(
                "The KWin script is installed but CoreSplit's focus service is not "
                "reachable, so the guard is inactive.")
            self.guard_hint.setStyleSheet(f"color: {th.AMBER_TEXT}; background: transparent;")

    def _autostart_toggled(self, checked):
        self.cfg["autostart"] = bool(checked)
        self.cfg.save()
        path = Path.home() / ".config/autostart/coresplit.desktop"
        if checked:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry_text(), encoding="utf-8")
            self.tools_hint.setText(f"Autostart enabled: {path}")
        else:
            try:
                path.unlink()
            except OSError:
                pass
            self.tools_hint.setText("Autostart disabled.")

    def _filter_changed(self, text):
        self.model.set_filter(text)

    def _show_all_toggled(self, checked):
        self.model.set_show_all(checked)

    def _flash(self, text):
        self.proc_message.setText(text)
        self._proc_message_timer.start(5000)

    def _selected_pids(self):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [pid for pid in (self.model.pid_at(row) for row in rows) if pid]

    def _pin_selected(self, role):
        pids = self._selected_pids()
        if not pids:
            self._flash("Select one or more processes first")
            return
        self.engine.request("manual_pin", pids=pids, role=role)
        self._flash(f"Pinned {len(pids)} to the {role} side")

    def _release_selected(self):
        pids = self._selected_pids()
        if not pids:
            self._flash("Select one or more processes first")
            return
        self.engine.request("manual_release", pids=pids)
        self._flash(f"Released {len(pids)} process(es)")

    def _apply_preset(self, game_cores):
        game, test = self.engine.topo.split_by_core_count(game_cores)
        self.game_cpus.setText(eng.format_cpulist(game))
        self.test_cpus.setText(eng.format_cpulist(test))
        self._update_split_hint()

    def _validate_split(self):
        all_cpus = set(self.engine.topo.all_cpus)
        game = eng.parse_cpulist(self.game_cpus.text())
        test = eng.parse_cpulist(self.test_cpus.text())
        if not game or not test:
            return None, "Both CPU sets must be non-empty."
        if not game <= all_cpus or not test <= all_cpus:
            return None, f"CPU numbers must be within 0-{max(all_cpus)}."
        if game & test:
            return None, f"Game and test sets overlap on {eng.format_cpulist(game & test)}."
        return (game, test), ""

    def _update_split_hint(self):
        if not hasattr(self, "split_hint"):
            return
        split, error = self._validate_split()
        if error:
            self.split_hint.setText(error)
            self.split_hint.setStyleSheet(f"color: {th.DANGER}; background: transparent;")
            return
        game, test = split
        notes = [f"{self.engine.topo.core_count(game)} physical cores for the game, "
                 f"{self.engine.topo.core_count(test)} for everything else."]
        for label, cpus in (("game", game), ("test", test)):
            split_smt = self.engine.topo.smt_split(cpus)
            if split_smt:
                notes.append(f"The {label} set splits SMT siblings on "
                             f"{eng.format_cpulist(split_smt)}, so those cores are shared.")
        colour = th.AMBER_TEXT if len(notes) > 1 else th.TEXT_3
        self.split_hint.setText(" ".join(notes))
        self.split_hint.setStyleSheet(f"color: {colour}; background: transparent;")

    def _apply_split(self):
        split, error = self._validate_split()
        if error:
            QMessageBox.warning(self, "Invalid split", error)
            return
        game, test = split
        self.cfg["game_cpus"] = eng.format_cpulist(game)
        self.cfg["test_cpus"] = eng.format_cpulist(test)
        self.cfg.save()
        self.engine.request("apply_config")
        self.engine.request("reset")
        self.tools_hint.setText("Split applied and pins released; choose a mode to re-engage.")

    def _copy_steam_option(self):
        self.cfg.ensure_split(self.engine.topo)
        text = eng.steam_launch_option(self.cfg)
        QApplication.clipboard().setText(text)
        self.tools_hint.setText(f"Copied: {text}")

    def _install_wrapper(self):
        self.cfg.ensure_split(self.engine.topo)
        path = Path.home() / ".local/bin/testcpus"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(eng.testcpus_script(self.cfg, self.engine.topo), encoding="utf-8")
        path.chmod(0o755)
        self.tools_hint.setText(f"Wrapper written to {path}")

    def _run_test_load(self):
        self.tools_hint.setText("Running 10 s of load on the test side…")
        QApplication.setOverrideCursor(Qt.BusyCursor)
        try:
            count = eng.run_test_load(self.cfg, self.engine.topo, 10)
        finally:
            QApplication.restoreOverrideCursor()
        self.tools_hint.setText(
            f"Finished: {count} busy loops on {self.cfg['test_cpus']}. "
            "Watch the game's frame time while it runs."
        )

    def _reset_all(self):
        self.engine.request("reset")
        self.tools_hint.setText("All pins released.")

    def raise_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# --------------------------------------------------------------------------- #
# tray
# --------------------------------------------------------------------------- #

class TrayController(QObject):
    def __init__(self, app, window, engine, config=None):
        super().__init__()
        self.app = app
        self.window = window
        self.engine = engine
        self.cfg = config
        self.tray = QSystemTrayIcon(th.make_icon("off"), app)
        self.tray.setToolTip("CoreSplit — idle")

        menu = QMenu()
        self._actions = {}
        group = QActionGroup(menu)
        group.setExclusive(True)
        for mode, label in (("off", "Off"), ("smart", "Smart split"), ("hard", "Hard split")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(mode == "off")
            action.triggered.connect(lambda _=False, m=mode: self.engine.set_mode(m, manual=True))
            group.addAction(action)
            menu.addAction(action)
            self._actions[mode] = action
        menu.addSeparator()
        self.guard_action = QAction("Focus guard", menu)
        self.guard_action.setCheckable(True)
        if self.cfg is not None:
            self.guard_action.setChecked(bool(self.cfg["focus_guard"]))
        self.guard_action.triggered.connect(self._toggle_guard)
        menu.addAction(self.guard_action)
        menu.addSeparator()
        show = QAction("Show window", menu)
        show.triggered.connect(self._show)
        menu.addAction(show)
        reset = QAction("Release all pins", menu)
        reset.triggered.connect(lambda: self.engine.request("reset"))
        menu.addAction(reset)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.show()

    def _toggle_guard(self, checked):
        if self.cfg is None:
            return
        import guard
        self.cfg["focus_guard"] = bool(checked)
        self.cfg.save()
        guard.set_enabled(bool(checked))
        if checked:
            self.engine.attach_publisher(guard.Publisher())
        else:
            self.engine.attach_publisher(None)
        self.window.guard_check.setChecked(bool(checked))
        self.window.set_guard_status(bool(checked) and guard.enabled())

    def _activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show()

    def _show(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _quit(self):
        self.window.hide()
        self.engine.stop()
        self.tray.hide()
        self.app.quit()

    def update(self, mode):
        for key, action in self._actions.items():
            action.setChecked(key == mode)
        self.tray.setIcon(th.make_icon(mode))
        self.tray.setToolTip({
            "off": "CoreSplit — idle",
            "smart": "CoreSplit — smart split",
            "hard": "CoreSplit — hard split",
        }.get(mode, "CoreSplit"))


# --------------------------------------------------------------------------- #
# exports used by coresplit.py
# --------------------------------------------------------------------------- #

def apply_theme(app):
    th.apply(app)


def make_icon(mode: str = "off") -> QIcon:
    return th.make_icon(mode)


def desktop_entry_text() -> str:
    launcher = Path.home() / ".local/bin/coresplit"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CoreSplit\n"
        "Comment=Keep games and background test workloads on separate CPU cores\n"
        f"Exec={launcher}\n"
        "Icon=coresplit\n"
        "Terminal=false\n"
        "Categories=Utility;System;\n"
        "StartupWMClass=CoreSplit\n"
    )
