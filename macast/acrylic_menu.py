# Copyright (c) 2021 by xfangfang. All Rights Reserved.
"""Acrylic tray-menu backend for Windows, based on PySide6.

Replaces the pystray backend with a frameless, per-monitor-DPI-aware,
acrylic-blurred cascading menu that follows the system dark/light theme
(or a user-selected theme persisted in Setting).
"""
import math
import os
import sys
import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import Qt, QPoint, QRect, QObject, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase,
                           QGuiApplication, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

from .utils import Setting, SettingProperty

logger = logging.getLogger("acrylic_menu")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Win32 DWM acrylic
# ---------------------------------------------------------------------------

WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class _WindowCompositionAttribData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _abgr(a, r, g, b):
    return (a << 24) | (b << 16) | (g << 8) | r


def enable_acrylic(hwnd, dark):
    """Apply acrylic blur to a window. Returns True on success."""
    try:
        policy = _AccentPolicy()
        policy.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
        policy.AccentFlags = 0
        policy.GradientColor = _abgr_color(dark)
        policy.AnimationId = 0
        data = _WindowCompositionAttribData()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.addressof(policy)
        data.SizeOfData = ctypes.sizeof(policy)
        ok = ctypes.windll.user32.SetWindowCompositionAttribute(
            wintypes.HWND(hwnd), ctypes.byref(data))
        if not ok:
            policy.AccentState = ACCENT_ENABLE_BLURBEHIND
            ctypes.windll.user32.SetWindowCompositionAttribute(
                wintypes.HWND(hwnd), ctypes.byref(data))
        # rounded corners on Windows 11
        try:
            corner = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(corner), ctypes.sizeof(corner))
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("enable_acrylic failed: %s", e)
        return False


def _abgr_color(dark):
    # tint painted by DWM itself; keep alpha low, real tint drawn by us
    if dark:
        return _abgr(0x30, 0x1C, 0x1C, 0x21)
    return _abgr(0x30, 0xF4, 0xF4, 0xF8)


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

THEME_SYSTEM = 0
THEME_DARK = 1
THEME_LIGHT = 2


def _system_is_dark():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except Exception:
        return True


def current_theme_is_dark():
    mode = Setting.get(SettingProperty.MenuTheme, THEME_SYSTEM)
    if mode == THEME_DARK:
        return True
    if mode == THEME_LIGHT:
        return False
    return _system_is_dark()


class Palette:
    def __init__(self, dark):
        self.dark = dark
        if dark:
            self.bg = QColor(28, 28, 33, 168)
            self.border = QColor(255, 255, 255, 38)
            self.text = QColor(242, 242, 245)
            self.text_dim = QColor(242, 242, 245, 128)
            self.hover = QColor(255, 255, 255, 33)
            self.sep = QColor(255, 255, 255, 31)
            self.icon = QColor(242, 242, 245, 225)
            self.check = QColor(76, 217, 100)
        else:
            self.bg = QColor(244, 244, 248, 185)
            self.border = QColor(0, 0, 0, 20)
            self.text = QColor(29, 29, 31)
            self.text_dim = QColor(29, 29, 31, 122)
            self.hover = QColor(0, 0, 0, 20)
            self.sep = QColor(0, 0, 0, 26)
            self.icon = QColor(29, 29, 31, 168)
            self.check = QColor(10, 132, 255)


# ---------------------------------------------------------------------------
# Icons (16x16 logical, line style)
# ---------------------------------------------------------------------------

ICON_MAP_TEXT = {
    # key or text -> icon name
    "p": "cast", "q": "quit", "c": "link",
    "Stop Cast": "cast", "停止接受投屏": "cast",
    "Start Cast": "cast", "开始接受投屏": "cast",
    "Setting": "gear", "设置": "gear",
    "Quit": "quit", "退出": "quit",
    "Renderers": "play", "选择播放器": "play",
    "Protocols": "wifi", "选择协议": "wifi",
    "Menubar Icon": "tray", "状态栏图标": "tray",
    "Menu Theme": "palette", "菜单主题": "palette",
    "Follow System": "monitor", "跟随系统": "monitor",
    "Dark": "moon", "深色": "moon",
    "Light": "sun", "浅色": "sun",
    "Auto Check Updates": "refresh", "自动检查更新": "refresh",
    "Start At Login": "power", "开机自启": "power",
    "Open Config Directory": "folder", "打开配置目录": "folder",
    "Advanced Setting": "sliders", "高级设置": "sliders",
    "Check For Updates": "update", "检查更新": "update",
    "Help": "help", "帮助": "help",
    "Copy Video URI": "link", "复制视频链接": "link",
}


def icon_for(item):
    if item.key in ICON_MAP_TEXT:
        return ICON_MAP_TEXT[item.key]
    return ICON_MAP_TEXT.get(item.text, "dot")


def _pen(painter, color, width=1.6):
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)


def draw_icon(painter, name, rect, color):
    """Draw a 16x16 line icon inside QRect rect."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    x, y, s = rect.x(), rect.y(), rect.width()
    painter.translate(x, y)
    painter.scale(s / 16.0, s / 16.0)
    _pen(painter, color)
    p = QPainterPath()
    if name == "cast":
        p.moveTo(2, 13.5)
        p.quadTo(2.0, 9.5, 5.0, 8.2)
        painter.drawPath(p)
        p2 = QPainterPath(); p2.moveTo(2, 10.5)
        p2.quadTo(2.0, 6.2, 6.0, 5.0)
        painter.drawPath(p2)
        p3 = QPainterPath(); p3.moveTo(2, 7.5)
        p3.quadTo(2.0, 3.2, 7.2, 2.2)
        painter.drawPath(p3)
        painter.setBrush(color)
        painter.drawEllipse(QPoint(int(2.6), int(13.8)), 1.3, 1.3)
        rect_path = QPainterPath()
        rect_path.addRoundedRect(7.5, 6.5, 7.5, 5.5, 1.2, 1.2)
        painter.drawPath(rect_path)
    elif name == "gear":
        painter.drawEllipse(QPoint(8, 8), 2.2, 2.2)
        for i in range(8):
            a = math.pi / 4 * i
            x1 = 8 + 3.6 * math.cos(a); y1 = 8 + 3.6 * math.sin(a)
            x2 = 8 + 5.4 * math.cos(a); y2 = 8 + 5.4 * math.sin(a)
            painter.drawLine(QPoint(int(x1), int(y1)), QPoint(int(x2), int(y2)))
    elif name == "quit":
        painter.drawLine(QPoint(4, 4), QPoint(12, 12))
        painter.drawLine(QPoint(12, 4), QPoint(4, 12))
    elif name == "play":
        p.moveTo(5, 3.5); p.lineTo(12.5, 8); p.lineTo(5, 12.5); p.closeSubpath()
        painter.setBrush(color)
        painter.drawPath(p)
    elif name == "wifi":
        p.moveTo(2.5, 6.5); p.quadTo(8, 2.5, 13.5, 6.5); painter.drawPath(p)
        p2 = QPainterPath(); p2.moveTo(4.5, 9.5)
        p2.quadTo(8, 6.8, 11.5, 9.5); painter.drawPath(p2)
        painter.setBrush(color)
        painter.drawEllipse(QPoint(8, 12.5), 1.4, 1.4)
    elif name == "tray":
        painter.drawEllipse(QPoint(8, 8), 5.5, 5.5)
        painter.setBrush(color)
        painter.drawEllipse(QPoint(8, 8), 1.8, 1.8)
    elif name == "palette":
        painter.drawEllipse(QPoint(8, 8), 5.5, 5.5)
        painter.setBrush(color)
        for cx, cy in [(5.5, 6), (8, 4.8), (11, 6)]:
            painter.drawEllipse(QPoint(int(cx), int(cy)), 0.9, 0.9)
    elif name == "monitor":
        p.addRoundedRect(2.5, 3.5, 11, 8, 1.4, 1.4)
        painter.drawPath(p)
        painter.drawLine(QPoint(6, 13.5), QPoint(10, 13.5))
        painter.drawLine(QPoint(8, 11.5), QPoint(8, 13.5))
    elif name == "moon":
        p.moveTo(11.5, 2.5)
        p.cubicTo(6.5, 3.5, 4.5, 8.5, 7.5, 12)
        p.cubicTo(5.5, 11.5, 3.5, 9.5, 3.8, 6.5)
        p.cubicTo(4.2, 4.0, 7.5, 2.0, 11.5, 2.5)
        painter.drawPath(p)
    elif name == "sun":
        painter.drawEllipse(QPoint(8, 8), 2.8, 2.8)
        for i in range(8):
            a = math.pi / 4 * i
            painter.drawLine(QPoint(int(8 + 4.2 * math.cos(a)), int(8 + 4.2 * math.sin(a))),
                             QPoint(int(8 + 5.8 * math.cos(a)), int(8 + 5.8 * math.sin(a))))
    elif name == "refresh":
        p.moveTo(13, 6); p.quadTo(11, 3, 8, 3)
        p.quadTo(4, 3, 3, 8); painter.drawPath(p)
        p2 = QPainterPath(); p2.moveTo(3, 10)
        p2.quadTo(5, 13, 8, 13); p2.quadTo(12, 13, 13, 8)
        painter.drawPath(p2)
        p3 = QPainterPath(); p3.moveTo(13, 3.2); p3.lineTo(13, 6.4); p3.lineTo(10, 6.4)
        painter.drawPath(p3)
        p4 = QPainterPath(); p4.moveTo(3, 12.8); p4.lineTo(3, 9.6); p4.lineTo(6, 9.6)
        painter.drawPath(p4)
    elif name == "power":
        p.moveTo(5, 5); p.quadTo(3, 6.8, 3, 9)
        p.quadTo(3, 13.5, 8, 13.5); p.quadTo(13, 13.5, 13, 9)
        p.quadTo(13, 6.8, 11, 5); painter.drawPath(p)
        painter.drawLine(QPoint(8, 2.5), QPoint(8, 8))
    elif name == "folder":
        p.addRoundedRect(2.5, 4.5, 11, 8.5, 1.4, 1.4)
        painter.drawPath(p)
        p2 = QPainterPath(); p2.moveTo(2.5, 6.5)
        p2.lineTo(6, 6.5); p2.lineTo(7.2, 4.8); p2.lineTo(9.5, 4.8)
        painter.drawPath(p2)
    elif name == "sliders":
        for i, yy in enumerate((4.5, 8, 11.5)):
            painter.drawLine(QPoint(2.5, int(yy)), QPoint(13.5, int(yy)))
        painter.setBrush(color)
        for cx, yy in [(6, 4.5), (10, 8), (5, 11.5)]:
            painter.drawEllipse(QPoint(cx, int(yy)), 1.4, 1.4)
    elif name == "update":
        painter.drawLine(QPoint(8, 13), QPoint(8, 3.5))
        p.moveTo(4.5, 7); p.lineTo(8, 3); p.lineTo(11.5, 7)
        painter.drawPath(p)
    elif name == "help":
        p.moveTo(6, 5.8); p.quadTo(6.2, 3.5, 8.2, 3.5)
        p.quadTo(10.5, 3.5, 10.5, 6); p.quadTo(10.5, 7.8, 8, 8.8)
        p.lineTo(8, 10.2); painter.drawPath(p)
        painter.setBrush(color)
        painter.drawEllipse(QPoint(8, 12.8), 1.0, 1.0)
    elif name == "link":
        p.addRoundedRect(2.5, 6.5, 5, 3.4, 1.7, 1.7)
        painter.drawPath(p)
        p2 = QPainterPath(); p2.addRoundedRect(8.5, 6.5, 5, 3.4, 1.7, 1.7)
        painter.drawPath(p2)
        painter.drawLine(QPoint(7.5, 8.2), QPoint(8.5, 8.2))
    else:  # dot
        painter.setBrush(color)
        painter.drawEllipse(QPoint(8, 8), 2.0, 2.0)
    painter.restore()


def draw_check(painter, rect, color):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 16.0, rect.width() / 16.0)
    _pen(painter, color, 2.0)
    p = QPainterPath()
    p.moveTo(3.5, 8.5); p.lineTo(6.8, 11.8); p.lineTo(12.5, 4.5)
    painter.drawPath(p)
    painter.restore()


def draw_chevron(painter, rect, color):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 16.0, rect.width() / 16.0)
    _pen(painter, color, 1.8)
    p = QPainterPath()
    p.moveTo(6.5, 4); p.lineTo(10.5, 8); p.lineTo(6.5, 12)
    painter.drawPath(p)
    painter.restore()


# ---------------------------------------------------------------------------
# Menu window
# ---------------------------------------------------------------------------

ROW_H = 38
PAD = 6
ICON_SZ = 18
COL_MIN = 264
COL_MAX = 320
RADIUS = 10


class AcrylicMenuWindow(QWidget):
    """Single-window cascading menu: submenus open as extra columns."""

    closed = Signal()

    def __init__(self, get_items, parent=None):
        super().__init__(parent)
        self._get_items = get_items
        self.columns = []          # list of item lists
        self.hover = (-1, -1)      # (col, row)
        self.col_widths = []
        self.dark = current_theme_is_dark()
        self.palette = Palette(self.dark)
        self._acrylic_ok = False
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)

    # -- layout --------------------------------------------------------
    def _rebuild(self):
        fm = self.fontMetrics()
        self.col_widths = []
        for col in self.columns:
            w = COL_MIN
            for it in col:
                if it is None:
                    continue
                t = fm.horizontalAdvance(it.text)
                extra = 12 + ICON_SZ + 10 + t + 18 + 12
                w = max(w, min(COL_MAX, extra))
            self.col_widths.append(w)
        total_w = sum(self.col_widths)
        max_rows = max((len(c) for c in self.columns), default=0)
        h = PAD * 2 + max_rows * ROW_H
        self.setFixedSize(total_width(self.col_widths), h)

    # -- popup ---------------------------------------------------------
    def popup_at(self, pos):
        self.columns = [list(self._get_items())]
        self.hover = (-1, -1)
        self.dark = current_theme_is_dark()
        self.palette = Palette(self.dark)
        self._rebuild()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(pos.x(), geo.right() - self.width() - 4)
        y = min(pos.y(), geo.bottom() - self.height() - 4)
        x = max(x, geo.left() + 4)
        y = max(y, geo.top() + 4)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self._acrylic_ok = enable_acrylic(int(self.winId()), self.dark)

    # -- events --------------------------------------------------------
    def event(self, ev):
        if ev.type() == ev.Type.WindowDeactivate:
            self.close()
            return True
        return super().event(ev)

    def closeEvent(self, ev):
        self.closed.emit()
        super().closeEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(ev)

    def _row_rect(self, col, row):
        x = sum(self.col_widths[:col]) + PAD
        y = PAD + row * ROW_H
        return QRect(x, y, self.col_widths[col] - PAD * 2, ROW_H)

    def _hit(self, pos):
        x = 0
        for ci, w in enumerate(self.col_widths):
            if x <= pos.x() < x + w:
                row = (pos.y() - PAD) // ROW_H
                if 0 <= row < len(self.columns[ci]):
                    return (ci, int(row))
                return (ci, -1)
            x += w
        return (-1, -1)

    def mouseMoveEvent(self, ev):
        hit = self._hit(ev.pos())
        if hit != self.hover:
            old = self.hover
            self.hover = hit
            ci, row = hit
            # open/close submenu columns
            if ci >= 0:
                self.columns = self.columns[:ci + 1]
                item = self._item_at(ci, row)
                if item is not None and item.children:
                    self.columns.append(list(item.items()))
                self._rebuild()
            self.update()

    def mousePressEvent(self, ev):
        ci, row = self._hit(ev.pos())
        item = self._item_at(ci, row)
        if item is None:
            return
        if not item.enabled:
            return
        if item.children:
            return  # handled by hover
        self.close()
        try:
            if item.callback:
                item.callback(item)
        except Exception as e:
            logger.error("menu callback error: %s", e)

    def leaveEvent(self, ev):
        self.hover = (-1, -1)
        self.update()
        super().leaveEvent(ev)

    def _item_at(self, col, row):
        if col < 0 or col >= len(self.columns):
            return None
        if row < 0 or row >= len(self.columns[col]):
            return None
        return self.columns[col][row]

    # -- painting ------------------------------------------------------
    def paintEvent(self, ev):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.font())
        fm = painter.fontMetrics()
        # panel
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1,
                            RADIUS, RADIUS)
        painter.fillPath(path, self.palette.bg)
        pen = QPen(self.palette.border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawPath(path)
        # columns
        for ci, col in enumerate(self.columns):
            if ci > 0:
                x = sum(self.col_widths[:ci])
                painter.setPen(QPen(self.palette.sep))
                painter.drawLine(x, PAD + 2, x, self.height() - PAD)
            for ri, it in enumerate(col):
                rect = self._row_rect(ci, ri)
                if it is None:
                    cy = rect.center().y()
                    painter.setPen(QPen(self.palette.sep))
                    painter.drawLine(rect.left() + 6, cy, rect.right() - 6, cy)
                    continue
                hovered = self.hover == (ci, ri) and it.enabled
                if hovered:
                    hp = QPainterPath()
                    hp.addRoundedRect(rect.x(), rect.y(), rect.width(),
                                      rect.height(), 7, 7)
                    painter.fillPath(hp, self.palette.hover)
                text_color = self.palette.text if it.enabled \
                    else self.palette.text_dim
                icon_color = self.palette.icon if it.enabled \
                    else self.palette.text_dim
                icon_rect = QRect(rect.x() + 10,
                                  rect.y() + (ROW_H - ICON_SZ) // 2,
                                  ICON_SZ, ICON_SZ)
                if it.checked:
                    draw_check(painter, icon_rect, self.palette.check)
                else:
                    draw_icon(painter, icon_for(it), icon_rect, icon_color)
                tx = icon_rect.right() + 10
                painter.setPen(text_color)
                trect = QRect(tx, rect.y(),
                              rect.right() - tx - (24 if it.children else 8),
                              rect.height())
                text = fm.elidedText(it.text, Qt.ElideRight, trect.width())
                painter.drawText(trect, Qt.AlignVCenter | Qt.AlignLeft, text)
                if it.children:
                    chev = QRect(rect.right() - 22,
                                 rect.y() + (ROW_H - 14) // 2, 14, 14)
                    draw_chevron(painter, chev, self.palette.text_dim)
        painter.end()


def total_width(widths):
    return sum(widths)


# ---------------------------------------------------------------------------
# Tray app (drop-in replacement for pystray.Icon)
# ---------------------------------------------------------------------------

class _SignalBridge(QObject):
    notify = Signal(str, str)
    show_menu = Signal()


class AcrylicTrayApp:
    """Mimics the pystray.Icon interface used by gui.App."""

    def __init__(self, name, icon, gui_app):
        self.name = name
        self.icon_path = icon
        self.gui_app = gui_app   # gui.App instance, provides .menu
        self._qt = None
        self._tray = None
        self._bridge = None
        self._menu_win = None

    # -- pystray-compatible API ---------------------------------------
    def run(self):
        app = QApplication.instance()
        if app is None:
            app = QApplication([self.name])
        self._qt = app
        app.setQuitOnLastWindowClosed(False)
        self._load_font(app)
        self._bridge = _SignalBridge()
        self._bridge.notify.connect(self._show_notify)
        self._bridge.show_menu.connect(self._popup_menu)
        self._tray = QSystemTrayIcon(self._make_icon(), parent=app)
        self._tray.setToolTip(self.name)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()
        app.exec()

    def stop(self):
        if self._tray:
            self._tray.hide()
        if self._qt:
            self._qt.quit()

    def notify(self, message="", title="Macast"):
        if self._bridge is not None:
            self._bridge.notify.emit(str(title), str(message))

    def remove_notification(self):
        pass

    def update_menu(self):
        pass  # menu is rebuilt every time it pops up

    def set_icon(self, icon_path):
        self.icon_path = icon_path
        if self._tray:
            self._tray.setIcon(self._make_icon())

    # -- internals -----------------------------------------------------
    def _make_icon(self):
        tray_png = Setting.get_base_path('macast/assets/tray.png')
        for c in (tray_png, self.icon_path):
            if c and os.path.exists(c):
                pix = QPixmap(c)
                if not pix.isNull():
                    return QIcon(pix.scaled(
                        64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return QIcon()

    def _load_font(self, app):
        families = []
        for fname in ("MiSans-Regular.ttf", "MiSans-Regular.otf"):
            fpath = Setting.get_base_path('macast/assets/fonts/' + fname)
            if os.path.exists(fpath):
                fid = QFontDatabase.addApplicationFont(fpath)
                if fid != -1:
                    families = list(QFontDatabase.applicationFontFamilies(fid))
                    break
        families += ["Microsoft YaHei UI", "Segoe UI", "sans-serif"]
        font = QFont()
        font.setFamilies(families)
        font.setPointSizeF(10.5)
        app.setFont(font)

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Context, QSystemTrayIcon.Trigger):
            self._popup_menu()

    def _popup_menu(self):
        if self._menu_win is not None:
            self._menu_win.close()
            return
        win = AcrylicMenuWindow(lambda: self.gui_app.menu)
        win.closed.connect(self._on_menu_closed)
        self._menu_win = win
        win.popup_at(QCursor.pos())

    def _on_menu_closed(self):
        self._menu_win = None

    def _show_notify(self, title, message):
        if self._tray:
            self._tray.showMessage(title, message,
                                   QSystemTrayIcon.Information, 5000)
