#!/usr/bin/env python3
"""CoreSplit GUI: a quiet dark interface built around one status card.

Everything visual is custom painted where the stock widget look would show:
the mode selector is a sliding segmented control, the CPU view is a small
topology diagram, and the process table draws its own rows and role chips.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QEasingCurve, QModelIndex, QObject, QPointF, QRectF, QSize,
    Qt, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QFont, QFontMetrics, QIcon, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStyle,
    QStyledItemDelegate, QSystemTrayIcon, QTableView, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
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


# --------------------------------------------------------------------------- #
# process table
# --------------------------------------------------------------------------- #

class ProcessModel(QAbstractTableModel):
    HEADERS = ["PID", "Process", "CPU", "Role", "Affinity", "Threads", "Scope"]
    WIDTHS = [80, 250, 64, 104, 124, 70, 230]
    NUMERIC = (0, 2, 5)
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
            return self.HEADERS[section]
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
                return str(row["pid"])
            if column == 1:
                return row["name"]
            if column == 2:
                return f"{row['cpu']:.1f}"
            if column == 3:
                return th.ROLE_LABEL.get(row["role"], row["role"])
            if column == 4:
                return row["affinity"]
            if column == 5:
                return str(row["threads"])
            if column == 6:
                return row["scope"]
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
    """Paints rows without gridlines: tinted hover, role chips, leading dot."""

    def __init__(self):
        super().__init__()
        self.sans = th.font(9.6)
        self.mono = th.font(8.8, mono=True)
        self.chip = th.font(8.2, QFont.DemiBold)
        self.pad = 12

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(th.tint(th.AMBER_C, 34))
            painter.drawRoundedRect(rect.adjusted(4, 2, -4, -2), 7, 7)
        elif hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(th.tint(th.TEXT_C, 9))
            painter.drawRoundedRect(rect.adjusted(4, 2, -4, -2), 7, 7)

        column = index.column()
        role = index.data(ProcessModel.RoleKind)
        text = index.data(Qt.DisplayRole) or ""

        if column == 3:
            self._chip(painter, rect, text, role)
        elif column == 1:
            self._name(painter, rect, text, role)
        else:
            mono = column in (0, 2, 4, 5)
            align = Qt.AlignRight if column in (0, 2, 5) else Qt.AlignLeft
            color = th.TEXT_2_C if column == 6 else th.TEXT_C
            if column == 4:
                color = th.TEXT_2_C
            self._text(painter, rect, text, mono, align, color)
        painter.restore()

    def _text(self, painter, rect, text, mono, align, color):
        painter.setFont(self.mono if mono else self.sans)
        painter.setPen(QPen(color))
        inner = rect.adjusted(self.pad, 0, -self.pad, 0)
        metrics = QFontMetrics(painter.font())
        elided = metrics.elidedText(text, Qt.ElideRight, int(inner.width()))
        painter.drawText(inner, int(align | Qt.AlignVCenter), elided)

    def _chip(self, painter, rect, text, role):
        accent = th.ROLE_ACCENT.get(role, th.GRAY_C)
        painter.setFont(self.chip)
        metrics = QFontMetrics(painter.font())
        width = metrics.horizontalAdvance(text) + 20
        chip = QRectF(rect.left() + self.pad, rect.center().y() - 10, width, 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(th.tint(accent, 40 if role != "other" else 22))
        painter.drawRoundedRect(chip, 10, 10)
        painter.setPen(QPen(QColor(th.ROLE_TEXT.get(role, th.GRAY_TEXT))))
        painter.drawText(chip, int(Qt.AlignCenter), text)

    def _name(self, painter, rect, text, role):
        accent = th.ROLE_ACCENT.get(role, th.GRAY_C)
        dot_x = rect.left() + self.pad
        if role != "other":
            painter.setPen(Qt.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(dot_x + 3, rect.center().y()), 3.0, 3.0)
        painter.setFont(self.sans)
        painter.setPen(QPen(th.TEXT_C))
        inner = rect.adjusted(self.pad + 14, 0, -8, 0)
        metrics = QFontMetrics(painter.font())
        elided = metrics.elidedText(text, Qt.ElideRight, int(inner.width()))
        painter.drawText(inner, int(Qt.AlignLeft | Qt.AlignVCenter), elided)


# --------------------------------------------------------------------------- #
# mode selector
# --------------------------------------------------------------------------- #

class SegmentedControl(QWidget):
    """Three-way selector with a sliding indicator."""

    changed = Signal(str)
    OPTIONS = (("off", "Off"), ("smart", "Smart"), ("hard", "Hard"))

    def __init__(self):
        super().__init__()
        self.setFixedHeight(42)
        self.setMinimumWidth(312)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._index = 0
        self._pos = 0.0
        self._hover = -1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
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
                # No point animating a control the user cannot see yet.
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
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        width = rect.width() / len(self.OPTIONS)
        return QRectF(rect.left() + index * width, rect.top(), width, rect.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(th.LINE_C, 1))
        painter.setBrush(th.q(th.SURFACE_2))
        painter.drawRoundedRect(track, 11, 11)

        key = self.mode()
        accent = {"off": th.q(th.SURFACE_3), "smart": th.BLUE_C, "hard": th.AMBER_C}[key]
        inner = self._segment_rect(self._pos).adjusted(3, 3, -3, -3)
        painter.setPen(QPen(th.tint(th.TEXT_C, 20), 1) if key == "off" else Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(inner, 8, 8)

        label_font = th.font(9.4, QFont.DemiBold)
        painter.setFont(label_font)
        for index, (option_key, label) in enumerate(self.OPTIONS):
            segment = self._segment_rect(index)
            if index == self._index:
                color = QColor("#17120a") if option_key != "off" else th.TEXT_C
            elif index == self._hover:
                color = th.TEXT_C
            else:
                color = th.TEXT_3_C
            painter.setPen(QPen(color))
            painter.drawText(segment, int(Qt.AlignCenter), label)
        painter.end()

    def _index_at(self, x: float) -> int:
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
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
# cpu topology diagram
# --------------------------------------------------------------------------- #

class CpuStrip(QWidget):
    """Each core is one pill; its two halves are the SMT threads.

    Grouping by CCD (rather than by CPU number) matters: on this machine a CCD is
    0-5 plus 12-17, so a numeric strip would interleave the two halves.
    """

    def __init__(self):
        super().__init__()
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._blocks: list[tuple[str, QColor, list[list[int]]]] = []

    def set_data(self, ccds, siblings, game, test):
        self._blocks = []
        for index, group in enumerate(ccds):
            cores, seen = [], set()
            for cpu in group:
                key = tuple(siblings.get(cpu, [cpu]))
                if key in seen:
                    continue
                seen.add(key)
                cores.append([c for c in key if c in group] or [cpu])
            members = set(sum(cores, []))
            if members and members <= set(game):
                role, color = "GAME", th.AMBER_C
            elif members and members <= set(test):
                role, color = "TESTS", th.BLUE_C
            else:
                role, color = "SHARED", th.TEXT_3_C
            self._blocks.append((f"CCD {index + 1} · {role}", color, cores))
        self._game = set(game)
        self._test = set(test)
        self.update()

    def _color(self, cpu) -> QColor:
        if cpu in self._game:
            return th.AMBER_C
        if cpu in self._test:
            return th.BLUE_C
        return th.GRAY_C

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if not self._blocks:
            painter.end()
            return

        width = self.width()
        gap = 26.0
        block_w = (width - gap * (len(self._blocks) - 1)) / len(self._blocks)
        label_h, pill_h = 15.0, 32.0
        top = label_h + 4

        for block_index, (label, color, cores) in enumerate(self._blocks):
            x0 = block_index * (block_w + gap)
            painter.setFont(th.caps_font(7.6, spacing=1.2))
            painter.setPen(QPen(color))
            painter.drawText(QRectF(x0, 0, block_w, label_h),
                             int(Qt.AlignLeft | Qt.AlignVCenter), label)

            inner_gap = 5.0
            pill_w = (block_w - inner_gap * (len(cores) - 1)) / max(1, len(cores))
            for core_index, core in enumerate(cores):
                self._pill(painter, x0 + core_index * (pill_w + inner_gap), top, pill_w, pill_h, core)
        painter.end()

    def _pill(self, painter, x, y, width, height, core):
        rect = QRectF(x, y, width, height)
        path = QPainterPath()
        path.addRoundedRect(rect, 7, 7)

        painter.save()
        painter.setClipPath(path)
        if len(core) >= 2:
            half = width / 2
            painter.fillRect(QRectF(x, y, half, height), self._color(core[0]))
            painter.fillRect(QRectF(x + half, y, half, height), self._color(core[1]))
        else:
            painter.fillRect(rect, self._color(core[0]))

        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
        sheen.setColorAt(0.55, QColor(255, 255, 255, 0))
        painter.fillRect(rect, sheen)
        painter.restore()

        if len(core) >= 2:
            painter.setPen(QPen(th.INK_C, 1))
            painter.drawLine(QPointF(x + width / 2, y + 3), QPointF(x + width / 2, y + height - 3))

        number_font = th.font(7.2 if width > 30 else 6.4, QFont.DemiBold, mono=True)
        painter.setFont(number_font)
        if len(core) >= 2:
            half = width / 2
            self._number(painter, QRectF(x, y, half, height), core[0])
            self._number(painter, QRectF(x + half, y, half, height), core[1])
        else:
            self._number(painter, rect, core[0])

    def _number(self, painter, rect, cpu):
        painter.setPen(QPen(th.tint(th.INK_C, 210)))
        painter.drawText(rect, int(Qt.AlignCenter), str(cpu))


# --------------------------------------------------------------------------- #
# status card
# --------------------------------------------------------------------------- #

class StatBlock(QWidget):
    def __init__(self, label: str, color: QColor):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.value = QLabel("0")
        self.value.setFont(th.font(18, QFont.DemiBold))
        self.value.setStyleSheet(f"color: {color.name()}; background: transparent;")
        self.value.setAlignment(Qt.AlignRight)
        caption = caps(label, th.TEXT_3, 7.4)
        caption.setAlignment(Qt.AlignRight)
        layout.addWidget(self.value)
        layout.addWidget(caption)

    def set_value(self, number: int):
        self.value.setText(str(number))


class StatusCard(QFrame):
    def __init__(self, config):
        super().__init__()
        self.setObjectName("Card")
        self.cfg = config

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(18)

        left = QVBoxLayout()
        left.setSpacing(3)
        self.mode_caps = caps("Idle", th.TEXT_3, 7.8)
        self.title = QLabel("No game detected")
        self.title.setFont(th.font(20, QFont.DemiBold))
        self.subtitle = QLabel("")
        self.subtitle.setFont(th.font(9.4))
        self.subtitle.setStyleSheet(f"color: {th.TEXT_2}; background: transparent;")
        left.addWidget(self.mode_caps)
        left.addWidget(self.title)
        left.addWidget(self.subtitle)
        top.addLayout(left, 1)

        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.game_stat = StatBlock("on game", th.AMBER_C)
        self.test_stat = StatBlock("on tests", th.BLUE_C)
        stats.addWidget(self.game_stat)
        stats.addWidget(self.test_stat)
        top.addLayout(stats)
        root.addLayout(top)

        self.strip = CpuStrip()
        root.addWidget(self.strip)
        root.addWidget(divider())

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.auto_check = QCheckBox("Engage automatically when a game starts")
        self.auto_check.setChecked(bool(config["auto_enable"]))
        bottom.addWidget(self.auto_check)

        bottom.addStretch(1)
        bottom.addWidget(QLabel("using"))
        self.auto_mode = QComboBox()
        self.auto_mode.addItem("Hard split", "hard")
        self.auto_mode.addItem("Smart split", "smart")
        index = self.auto_mode.findData(config["auto_mode"])
        self.auto_mode.setCurrentIndex(max(0, index))
        self.auto_mode.setMinimumWidth(132)
        bottom.addWidget(self.auto_mode)
        root.addLayout(bottom)

    def set_snapshot(self, snap, topo):
        mode = snap["mode"]
        game = snap.get("game")
        title_text = {
            "off": "Idle",
            "smart": "Smart split",
            "hard": "Hard split",
        }.get(mode, "Idle")
        caps_color = {"off": th.TEXT_3, "smart": th.BLUE_TEXT, "hard": th.AMBER_TEXT}.get(mode, th.TEXT_3)
        state = "active" if game else "standby"
        self.mode_caps.setText(f"{title_text} · {state}".upper())
        self.mode_caps.setStyleSheet(f"color: {caps_color}; background: transparent;")

        if game:
            self.title.setText(game["name"])
            self.subtitle.setText(f"pid {game['pid']}  ·  detected via {game['reason']}")
        else:
            self.title.setText("No game detected")
            self.subtitle.setText(
                "Everything shares all threads" if mode == "off"
                else "Waiting for a game to start"
            )

        counts = snap["counts"]
        self.game_stat.set_value(counts.get("game", 0))
        self.test_stat.set_value(counts.get("test", 0))
        self.strip.set_data(
            topo.ccds, topo.siblings,
            eng.parse_cpulist(snap["game_cpus"]),
            eng.parse_cpulist(snap["test_cpus"]),
        )


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self, engine, bridge, config):
        super().__init__()
        self.engine = engine
        self.bridge = bridge
        self.cfg = config
        self.setWindowTitle("CoreSplit")
        self.resize(1140, 820)
        self.setMinimumSize(980, 700)

        self._build()
        bridge.snapshot.connect(self._on_snapshot)
        bridge.log.connect(self._append_log)

    # -- construction ------------------------------------------------------ #

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(30, 24, 30, 0)
        outer.setSpacing(18)

        outer.addLayout(self._header())
        self.card = StatusCard(self.cfg)
        self.card.auto_check.toggled.connect(self._auto_toggled)
        self.card.auto_mode.currentIndexChanged.connect(self._auto_mode_changed)
        outer.addWidget(self.card)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._overview_tab(), "Overview")
        self.tabs.addTab(self._advanced_tab(), "Advanced")
        outer.addWidget(self.tabs, 1)

        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self._status_label = QLabel("Starting…")
        self._status_label.setObjectName("Status")
        bar.addWidget(self._status_label, 1)
        bar.setContentsMargins(30, 4, 30, 8)

    def _header(self):
        row = QHBoxLayout()
        row.setSpacing(20)

        left = QVBoxLayout()
        left.setSpacing(1)
        wordmark = QLabel("CoreSplit")
        wordmark.setFont(th.font(15.5, QFont.DemiBold, spacing=0.2))
        tagline = QLabel("One CCD for the game, one for everything else")
        tagline.setObjectName("Tagline")
        left.addWidget(wordmark)
        left.addWidget(tagline)
        row.addLayout(left)
        row.addStretch(1)

        self.segments = SegmentedControl()
        self.segments.changed.connect(self._user_set_mode)
        row.addWidget(self.segments)
        return row

    def _card(self, title: str = ""):
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        if title:
            layout.addWidget(caps(title))
        return card, layout

    def _overview_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search processes")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(260)
        self.filter.textChanged.connect(self._filter_changed)
        toolbar.addWidget(self.filter)

        self.show_all = QCheckBox("All users")
        self.show_all.toggled.connect(self._show_all_toggled)
        toolbar.addWidget(self.show_all)
        toolbar.addStretch(1)

        self._pin_game_btn = QPushButton("Pin to game")
        self._pin_game_btn.clicked.connect(lambda: self._pin_selected("game"))
        toolbar.addWidget(self._pin_game_btn)

        self._pin_test_btn = QPushButton("Pin to tests")
        self._pin_test_btn.clicked.connect(lambda: self._pin_selected("test"))
        toolbar.addWidget(self._pin_test_btn)

        release = variant(QPushButton("Release"), "ghost")
        release.clicked.connect(self._release_selected)
        toolbar.addWidget(release)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        table_card, table_layout = self._card()
        table_layout.setContentsMargins(10, 8, 10, 10)
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
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setFixedHeight(36)
        for index, width in enumerate(ProcessModel.WIDTHS):
            self.table.setColumnWidth(index, width)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_card)

        log_card, log_layout = self._card("Activity")
        log_layout.setSpacing(8)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setAcceptRichText(True)
        self.log_view.setFont(th.font(8.8, mono=True))
        self.log_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        log_layout.addWidget(self.log_view, 1)
        splitter.addWidget(log_card)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)
        table_card.setMinimumHeight(150)
        log_card.setMinimumHeight(90)
        log_card.setMaximumHeight(210)
        splitter.setSizes([480, 150])
        layout.addWidget(splitter, 1)
        return page

    def _advanced_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("ScrollViewport")

        content = QWidget()
        content.setObjectName("ScrollContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 12, 14)
        layout.setSpacing(14)

        split_card, split_layout = self._card("CPU split")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)

        grid.addWidget(caps("Game CPUs", th.TEXT_2, 7.6), 0, 0)
        grid.addWidget(caps("Test CPUs", th.TEXT_2, 7.6), 0, 1)
        self.game_cpus = QLineEdit(self.cfg["game_cpus"])
        self.test_cpus = QLineEdit(self.cfg["test_cpus"])
        self.game_cpus.setPlaceholderText("e.g. 0-5,12-17")
        self.test_cpus.setPlaceholderText("e.g. 6-11,18-23")
        self.game_cpus.textChanged.connect(self._update_split_hint)
        self.test_cpus.textChanged.connect(self._update_split_hint)
        grid.addWidget(self.game_cpus, 1, 0)
        grid.addWidget(self.test_cpus, 1, 1)

        presets = QHBoxLayout()
        presets.setSpacing(8)
        for label, cores in (("Balanced 6/6", 6), ("Game-heavy 8/4", 8), ("Test-heavy 4/8", 4)):
            button = variant(QPushButton(label), "ghost")
            button.clicked.connect(lambda _=False, c=cores: self._apply_preset(c))
            presets.addWidget(button)
        presets.addStretch(1)
        grid.addLayout(presets, 2, 0, 1, 2)

        self.split_hint = QLabel("")
        self.split_hint.setObjectName("Hint")
        self.split_hint.setWordWrap(True)
        grid.addWidget(self.split_hint, 3, 0, 1, 2)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        apply_button = variant(QPushButton("Apply split"), "primary")
        apply_button.clicked.connect(self._apply_split)
        apply_row.addWidget(apply_button)
        grid.addLayout(apply_row, 4, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        split_layout.addLayout(grid)
        layout.addWidget(split_card)

        options_card, options_layout = self._card("Behaviour")
        self.renice_check = QCheckBox("Lower priority of test processes")
        self.renice_check.setChecked(bool(self.cfg["smart_renice"]))
        self.renice_check.toggled.connect(self._options_changed)
        options_layout.addWidget(self.renice_check)

        self.ioprio_check = QCheckBox("Deprioritise test disk I/O")
        self.ioprio_check.setChecked(bool(self.cfg["smart_ioprio"]))
        self.ioprio_check.toggled.connect(self._options_changed)
        options_layout.addWidget(self.ioprio_check)

        self.autostart_check = QCheckBox("Start CoreSplit at login")
        self.autostart_check.setChecked(bool(self.cfg["autostart"]))
        self.autostart_check.toggled.connect(self._autostart_toggled)
        options_layout.addWidget(self.autostart_check)

        self.guard_check = QCheckBox("Stop agents from taking focus while a game runs")
        self.guard_check.setChecked(bool(self.cfg["focus_guard"]))
        self.guard_check.toggled.connect(self._guard_toggled)
        options_layout.addWidget(self.guard_check)

        self.guard_hint = QLabel("")
        self.guard_hint.setObjectName("Hint")
        self.guard_hint.setWordWrap(True)
        options_layout.addWidget(self.guard_hint)
        layout.addWidget(options_card)

        tools_card, tools_layout = self._card("Tools")
        first = QHBoxLayout()
        first.setSpacing(8)
        steam_button = variant(QPushButton("Copy Steam launch option"), "ghost")
        steam_button.clicked.connect(self._copy_steam_option)
        first.addWidget(steam_button)
        wrapper_button = variant(QPushButton("Install testcpus wrapper"), "ghost")
        wrapper_button.clicked.connect(self._install_wrapper)
        first.addWidget(wrapper_button)
        first.addStretch(1)
        tools_layout.addLayout(first)

        second = QHBoxLayout()
        second.setSpacing(8)
        load_button = variant(QPushButton("Run 10 s test load"), "ghost")
        load_button.clicked.connect(self._run_test_load)
        second.addWidget(load_button)
        reset_button = variant(QPushButton("Reset all pins"), "danger")
        reset_button.clicked.connect(self._reset_all)
        second.addWidget(reset_button)
        second.addStretch(1)
        tools_layout.addLayout(second)

        self.tools_hint = QLabel("")
        self.tools_hint.setObjectName("Hint")
        self.tools_hint.setWordWrap(True)
        tools_layout.addWidget(self.tools_hint)
        layout.addWidget(tools_card)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        return page

    # -- engine -> ui ------------------------------------------------------ #

    def _on_snapshot(self, snap):
        self.segments.set_mode(snap["mode"])
        self.card.set_snapshot(snap, self.engine.topo)
        self.model.set_rows(snap["procs"])
        self._update_split_hint()

        counts = snap["counts"]
        self._status_label.setText(
            f"{len(snap['procs'])} processes  ·  {counts.get('pinned', 0)} pinned  ·  "
            f"game {snap['game_cpus']}  ·  tests {snap['test_cpus']}"
        )
        self.setWindowIcon(th.make_icon(snap["mode"]))

        if self.log_view.document().isEmpty() and snap["logs"]:
            for line in snap["logs"]:
                self._append_log(line)

    def _append_log(self, line):
        match = re.match(r"^\[(\d\d:\d\d:\d\d)\]\s*(.*)$", line)
        if match:
            body = (f'<span style="color:{th.TEXT_3}">{match.group(1)}</span>'
                    f'&nbsp;&nbsp;<span style="color:{th.TEXT_2}">{html.escape(match.group(2))}</span>')
        else:
            body = f'<span style="color:{th.TEXT_2}">{html.escape(line)}</span>'
        self.log_view.append(body)

    # -- user actions ------------------------------------------------------ #

    def _user_set_mode(self, mode):
        self.engine.set_mode(mode, manual=True)

    def _auto_toggled(self, checked):
        self.cfg["auto_enable"] = bool(checked)
        self.cfg.save()

    def _auto_mode_changed(self):
        self.cfg["auto_mode"] = self.card.auto_mode.currentData()
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
            self.guard_hint.setText("Agent windows may take focus from the game.")
            self.guard_hint.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
            return
        if getattr(self, "_guard_ready", False):
            self.guard_hint.setText(
                "Agent windows are blocked from taking focus while a game is protected. "
                "Your own window switches still work."
            )
            self.guard_hint.setStyleSheet(f"color: {th.TEXT_3}; background: transparent;")
        else:
            self.guard_hint.setText(
                "The KWin script is installed but CoreSplit's focus service is not "
                "reachable, so the guard is inactive."
            )
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

    def _selected_pids(self):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [pid for pid in (self.model.pid_at(row) for row in rows) if pid]

    def _pin_selected(self, role):
        pids = self._selected_pids()
        if not pids:
            self._status_label.setText("Select one or more processes first")
            return
        self.engine.request("manual_pin", pids=pids, role=role)
        self._status_label.setText(f"Pinned {len(pids)} process(es) to the {role} CCD")

    def _release_selected(self):
        pids = self._selected_pids()
        if not pids:
            self._status_label.setText("Select one or more processes first")
            return
        self.engine.request("manual_release", pids=pids)
        self._status_label.setText(f"Released {len(pids)} process(es)")

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
        self.tools_hint.setText("Running 10 s of load on the test CCD…")
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
        reset = QAction("Reset all pins", menu)
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
