# Copyright (c) 2021 by xfangfang. All Rights Reserved.
# Modifications for Macast-Glass.
#
# Qt 自绘托盘菜单，替换 Windows 下的 pystray 原生命令菜单。
# 目标：Win10 系统级亚克力模糊 + 级联子菜单 + 矢量图标 + 深浅色主题。
#
# Win10 实测要点：
#   1. DWMWA_SYSTEMBACKDROP_TYPE 是 Win11 22621+ 专属，Win10 必须用
#      SetWindowCompositionAttribute
#   2. ACRYLICBLURBEHIND(4) 对 layered window 只有透明没有模糊，必须用
#      BLURBEHIND(3)
#   3. GradientColor 是 ABGR(0xAABBGGRR)，不是 ARGB，写反会发蓝
#   4. 图标必须按 size×dpr 出图再超采样，否则 150% 缩放下会被拉伸糊掉
#   5. 纯文字菜单（如播放器列表）不能预留图标列，否则左边空一大片

import os
import sys
import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger("glass_menu")
logger.setLevel(logging.INFO)

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer
from PySide6.QtGui import (QPainter, QPainterPath, QColor, QPen, QFont,
                           QPixmap, QFontMetrics, QIcon, QGuiApplication,
                           QCursor, QFontDatabase)
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon
from PySide6.QtSvg import QSvgRenderer

from .utils import Setting, SettingProperty


# ---------------------------------------------------------------- Win32 亚克力

class _ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
WCA_ACCENT_POLICY = 19


def _to_abgr(c: QColor) -> int:
    return ((c.alpha() << 24) | (c.blue() << 16) |
            (c.green() << 8) | c.red()) & 0xFFFFFFFF


def apply_acrylic(hwnd: int, tint: QColor, acrylic: bool = False) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        policy = _ACCENTPOLICY()
        policy.AccentState = (ACCENT_ENABLE_ACRYLICBLURBEHIND if acrylic
                              else ACCENT_ENABLE_BLURBEHIND)
        policy.AccentFlags = 0
        policy.GradientColor = _to_abgr(tint)
        policy.AnimationId = 0

        data = _WINCOMPATTRDATA()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(policy)

        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(_WINCOMPATTRDATA)]
        fn.restype = wintypes.BOOL
        return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception as e:
        logger.error("apply_acrylic failed: {}".format(e))
        return False


# ---------------------------------------------------------------- 主题

class DarkTheme:
    TINT = QColor(20, 20, 24, 208)
    TEXT = QColor(255, 255, 255)
    TEXT_DIM = QColor(255, 255, 255, 182)
    TEXT_OFF = QColor(255, 255, 255, 92)
    HOVER = QColor(255, 255, 255, 30)
    SEP = QColor(255, 255, 255, 44)
    BORDER = QColor(255, 255, 255, 38)
    ACCENT = QColor(76, 217, 100)
    ICON = "#FBFBFD"
    TITLE_SUFFIX = QColor(255, 255, 255, 205)   # 标题后半段，略淡于主机名
    CHEVRON = QColor(255, 255, 255, 110)


class LightTheme:
    TINT = QColor(252, 252, 255, 208)
    TEXT = QColor(15, 15, 19)
    TEXT_DIM = QColor(0, 0, 0, 192)
    TEXT_OFF = QColor(0, 0, 0, 96)
    HOVER = QColor(0, 0, 0, 26)
    SEP = QColor(0, 0, 0, 42)
    BORDER = QColor(0, 0, 0, 28)
    ACCENT = QColor(10, 132, 255)
    ICON = "#1D1D22"
    TITLE_SUFFIX = QColor(0, 0, 0, 195)
    CHEVRON = QColor(0, 0, 0, 110)


THEMES = {"dark": DarkTheme, "light": LightTheme}
T = DarkTheme


def system_prefers_light() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return bool(value)
    except Exception:
        return False


def get_theme_mode() -> str:
    """兼容旧版写进配置的整数（0=auto / 1=dark / 2=light）。"""
    try:
        v = Setting.get(SettingProperty.MenuTheme, "auto")
    except Exception:
        return "auto"
    if isinstance(v, int):
        return {0: "auto", 1: "dark", 2: "light"}.get(v, "auto")
    if v in ("auto", "dark", "light"):
        return v
    return "auto"


def apply_theme_mode(mode=None):
    """mode: auto / dark / light。auto 跟随系统。"""
    global T
    if mode is not None:
        try:
            Setting.set(SettingProperty.MenuTheme, mode)
        except Exception as e:
            logger.error("save theme mode failed: {}".format(e))
    name = mode or get_theme_mode()
    if name == "auto":
        name = "light" if system_prefers_light() else "dark"
    new_theme = THEMES.get(name, DarkTheme)
    # 只有真的换主题才清图标缓存，否则每次弹菜单都白重绘一遍
    if new_theme is not T:
        _pix_cache.clear()
    T = new_theme
    return T


# ---------------------------------------------------------------- 矢量图标

_ICON_TPL = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
             'fill="none" stroke="{c}" stroke-width="1.95" stroke-linecap="round" '
             'stroke-linejoin="round">{body}</svg>')

_ICON_BODY = {
    "cast": '<path d="M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/>'
            '<path d="M2 12a9 9 0 0 1 8 8"/><path d="M2 16a5 5 0 0 1 4 4"/>'
            '<line x1="2" y1="20" x2="2.01" y2="20"/>',
    # 三角形视觉面积比同尺寸的方/圆小，画大一档才和其他图标等重
    "play": '<polygon points="5 3.6 20.4 12 5 20.4" fill="{c}" stroke="none"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.63.28 1.16.85 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "close": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/>'
            '<path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
            '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>'
            '<line x1="12" y1="20" x2="12.01" y2="20"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>'
             '<circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    "refresh": '<polyline points="1 4 1 10 7 10"/>'
               '<path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    "sliders": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
               '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
               '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
               '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
               '<line x1="17" y1="16" x2="23" y2="16"/>',
    "power": '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "help": '<circle cx="12" cy="12" r="10"/>'
            '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
            '<line x1="12" y1="17" x2="12.01" y2="17"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "palette": '<circle cx="13.5" cy="6.5" r="1"/><circle cx="17.5" cy="10.5" r="1"/>'
               '<circle cx="8.5" cy="7.5" r="1"/><circle cx="6.5" cy="12.5" r="1"/>'
               '<path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 '
               '0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 '
               '1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>',
}

# 菜单文案 -> 图标。长词条在前，避免「检查更新」先命中「自动检查更新」。
_LABEL_ICON = [
    ("自动检查更新", "refresh"), ("auto check", "refresh"),
    ("检查更新", "download"), ("check for update", "download"),
    ("打开配置目录", "folder"), ("open config", "folder"),
    ("高级设置", "sliders"), ("advanced setting", "sliders"),
    ("状态栏图标", "image"), ("menubar icon", "image"), ("任务栏图标", "image"),
    ("菜单主题", "palette"), ("menu theme", "palette"),
    ("选择播放器", "play"), ("renderers", "play"),
    ("选择协议", "wifi"), ("protocols", "wifi"),
    ("开机自启", "power"), ("start at login", "power"),
    ("复制", "copy"), ("copy", "copy"),
    ("停止", "cast"), ("stop cast", "cast"),
    ("开始", "play"), ("start cast", "play"),
    ("设置", "gear"), ("setting", "gear"),
    ("帮助", "help"), ("help", "help"),
    ("退出", "close"), ("quit", "close"),
]


def icon_key_for(label: str):
    low = (label or "").strip().lower()
    for name, key in _LABEL_ICON:
        if name in low:
            return key
    return None


_pix_cache = {}


def icon_pixmap(key: str, size: int = 21, color: str = "#E8E8EC",
                dpr: float = 1.0, scale: int = 4) -> QPixmap:
    """按物理像素出图：size 逻辑 px，实际 size×dpr 物理 px。"""
    dpr = max(1.0, float(dpr))
    ck = (key, size, color, round(dpr, 2), scale)
    if ck in _pix_cache:
        return _pix_cache[ck]
    body = _ICON_BODY.get(key)
    if not body:
        return QPixmap()
    svg = _ICON_TPL.format(c=color, body=body.format(c=color))
    renderer = QSvgRenderer(svg.encode("utf-8"))

    px = max(1, int(round(size * dpr)))
    ss = px * scale
    big = QPixmap(ss, ss)
    big.fill(Qt.transparent)
    p = QPainter(big)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(p, QRectF(0, 0, ss, ss))
    p.end()

    pm = big.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _pix_cache[ck] = pm
    return pm


def _pick_family() -> str:
    """优先鸿蒙 Medium，找不到就退回雅黑。"""
    try:
        fams = set(QFontDatabase.families())
    except Exception:
        return "Microsoft YaHei UI"
    for name in ("HarmonyOS Sans SC Medium", "HarmonyOS Sans SC",
                 "Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
        if name in fams:
            return name
    return "sans-serif"


# ---------------------------------------------------------------- 菜单数据

def convert_menu(menu_items):
    """把 macast 的 [MenuItem|None] 树转成渲染用的 dict 列表。"""
    out = []
    for it in menu_items or []:
        if it is None:
            out.append({"type": "sep"})
            continue
        children = getattr(it, "children", None)
        node = {
            "type": "item",
            "label": it.text,
            "enabled": it.enabled,
            "checked": bool(it.checked),
            "ref": it,
        }
        if children:
            node["sub"] = convert_menu(children)
        out.append(node)
    return out


def _squash_seps(nodes):
    out = []
    for n in nodes:
        if n.get("type") == "sep":
            if not out or out[-1].get("type") == "sep":
                continue
        out.append(n)
    while out and out[-1].get("type") == "sep":
        out.pop()
    return out


def _promote_header(nodes):
    """前两项都是禁用项时合并成标题区（版本号 + IP）。"""
    if (len(nodes) >= 2
            and nodes[0].get("type") == "item"
            and nodes[1].get("type") == "item"
            and not nodes[0].get("enabled", True)
            and not nodes[1].get("enabled", True)):
        header = {
            "type": "header",
            "title": nodes[0]["label"],
            "sub": nodes[1]["label"],
            "ref": None,
        }
        # 保留紧跟标题的分隔线：这里是给它画线的唯一位置，
        # 之前顺手删掉，标题区和第一项就直接贴在一起了
        # 先拼上标题再做压线：单独压 rest 会把紧跟标题的那条线当“开头多余”删掉
        return _squash_seps([header] + nodes[2:])
    return _squash_seps(nodes)


# ---------------------------------------------------------------- 菜单窗口

class GlassMenu(QWidget):
    WIDTH = 270
    ROW_H = 40
    PAD = 7
    RADIUS = 12
    HEADER_H = 50
    SEP_H = 13
    ICON = 21
    GAP = 14

    _open_chain = []

    def __init__(self, nodes, anchor=None, parent_item_rect=None, parent=None):
        # 不用 Qt.Popup：它在 layered window 上首击会被系统拿去激活窗口，
        # 表现就是「菜单看得见但点不动」。Qt.Tool 是普通窗口，点击正常进。
        super().__init__(parent,
                         Qt.Tool | Qt.FramelessWindowHint |
                         Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.items = _promote_header(nodes)
        self.child = None
        self.hover_index = -1
        self._rows = []
        self._pending_row = (0, 0)

        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.setInterval(160)
        self._open_timer.timeout.connect(self._open_submenu)

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

        fam = _pick_family()
        hint = QFont.HintingPreference.PreferFullHinting

        def mk(px, weight):
            f = QFont(fam)
            f.setPixelSize(px)
            f.setWeight(weight)
            f.setHintingPreference(hint)
            f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            # 不加负字距：字号 14 + 负字距会让每个字落在半像素上，边缘发灰发毛
            return f

        self._font = mk(14, QFont.Weight.Normal)
        self._font_name = mk(17, QFont.Weight.Medium)
        self._font_bold = mk(15, QFont.Weight.Medium)
        self._font_sub = mk(12, QFont.Weight.Normal)

        self._measure()
        # 全是纯文字的菜单不预留图标列，否则左边空一大片
        self._any_icon = any(n.get("icon") for n in self.items
                             if n["type"] == "item")
        if not self._any_icon:
            self._any_icon = any(icon_key_for(n.get("label", ""))
                                 for n in self.items if n["type"] == "item")
        self.resize(self.WIDTH, self._total_h)

        if anchor is not None:
            self.move(anchor)
        elif parent_item_rect is not None:
            self.move(self._cascade_pos(parent_item_rect))

    # ---- 布局 ----

    def _measure(self):
        self._rows = []
        y = self.PAD
        first_item_done = False
        for i, it in enumerate(self.items):
            if it["type"] == "header":
                h = self.HEADER_H
            elif it["type"] == "sep":
                h = self.SEP_H
            else:
                h = self.ROW_H
            dy = 0
            if it["type"] == "item" and not first_item_done:
                dy = 1
                first_item_done = True
            self._rows.append((y + dy, y + h + dy, i))
            y += h
        self._total_h = y + self.PAD

    def _row_at(self, pos):
        for y0, y1, i in self._rows:
            if y0 <= pos.y() < y1 and self.items[i]["type"] == "item":
                return i, y0, y1
        return -1, 0, 0

    def _cascade_pos(self, parent_rect=None) -> QPoint:
        """标准级联：子菜单贴在父菜单右侧，纵向对齐触发它的那一行。

        - 右边放不下 → 翻到父菜单左侧
        - 下方放不下 → 上推，底边与父菜单底边齐平（两列共用一条底线）
        - 会压住已打开的其他菜单 → 向上让位

        坐标是**屏幕坐标**（Qt.Tool 虽然带 parent，move 仍按全局位置生效）。
        """
        par = self.parent()
        if par is None:
            return QPoint(0, 0)

        pw = par.width()
        mw, mh = self.WIDTH, self._total_h
        scr = (par.screen() or QGuiApplication.primaryScreen())
        scr = scr.availableGeometry() if scr else QRect(0, 0, 1920, 1080)
        pg = QRect(par.mapToGlobal(QPoint(0, 0)), par.size())

        # 横向：优先贴在父菜单右侧；右边真的放不下才翻到左侧。
        # 不继承父级的展开方向 —— 否则根菜单贴屏幕右边时，
        # 二级向左、三级也跟着向左，一路跑到屏幕最左边，离父级老远。
        x = pg.left() + pw + 4
        if x + mw > scr.right() + 1:
            x = pg.left() - mw - 4

        # 翻到左边后仍然出界，就贴着屏幕左边缘（宁可窄一点也不叠）
        if x < scr.left():
            x = scr.left()

        # 纵向：对齐触发它的那一行（标准级联）。
        # 父菜单本身很长、下面放不下时整体上推，让底边贴屏幕底；
        # 这个副作用就是"两列底线齐平"，看起来像平行。
        y = (parent_rect.top() - self.PAD) if parent_rect is not None else pg.top()
        if y + mh > scr.bottom() + 1:
            # 下方放不下：优先与父菜单底边齐平，两列共用同一条底线。
            # 父菜单底边和"屏幕可用区底边"不是同一个值（托盘上方还留着 8px 间距），
            # 所以不能直接用 scr.bottom()，否则会差几个像素，看着就是没对齐。
            y = pg.top() + pg.height() - mh
            if y + mh > scr.bottom() + 1:
                y = scr.bottom() - mh + 1
        if y < scr.top():
            y = scr.top()

        # 避开已经打开的菜单。三种情况都靠这里处理：
        # 1) 根菜单贴着右边、二级被翻到左侧，三级回到右侧时压住根菜单
        # 2) 三级比可见空间高，向下会盖住根菜单
        # 3) 只差几像素就贴上 —— 视觉上像压在一起，所以按 GAP 留出安全距离
        GAP = 12
        for _ in range(4):
            rect = QRect(int(x), int(y), mw, mh)
            hit = None
            for m in GlassMenu._open_chain:
                if m is self or not m.isVisible():
                    continue
                g = QRect(m.mapToGlobal(QPoint(0, 0)), m.size())
                # 只按上下扩边判断：左右本来就有 4px 级联间隙，
                # 扩左右会把父菜单也算进来，导致自己躲自己
                if rect.adjusted(0, -GAP, 0, GAP).intersects(g):
                    hit = g
                    break
            if hit is None:
                break
            up = hit.top() - mh - GAP
            down = hit.bottom() + 1 + GAP
            if up >= scr.top():                       # 往上延展，不压下面
                y = up
            elif down + mh <= scr.bottom() + 1:       # 上面出界才向下
                y = down
            else:                                     # 上下都没地方，保持原位
                break

        return QPoint(int(x), int(y))

    # ---- 生命周期 ----

    def showEvent(self, e):
        super().showEvent(e)
        GlassMenu._open_chain.append(self)
        QTimer.singleShot(0, self._apply_acrylic)
        # 强化激活：Popup 没焦点时首击会被拿去"激活窗口"而不是点中菜单项
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.PopupFocusReason)

    def _apply_acrylic(self):
        try:
            apply_acrylic(int(self.winId()), T.TINT, acrylic=False)
        except Exception as e:
            logger.error("apply acrylic error: {}".format(e))

    def closeEvent(self, e):
        if self in GlassMenu._open_chain:
            GlassMenu._open_chain.remove(self)
        if self.child:
            self.child.close()
            self.child = None
        super().closeEvent(e)

    @classmethod
    def close_all(cls):
        for m in list(cls._open_chain):
            m.close()
        cls._open_chain.clear()

    # ---- 交互 ----

    def mouseMoveEvent(self, e):
        idx, y0, y1 = self._row_at(e.position().toPoint())
        if idx != self.hover_index:
            self.hover_index = idx
            self.update()
            if self.child:
                self.child.close()
                self.child = None
            self._open_timer.stop()
            if idx >= 0 and self.items[idx].get("sub"):
                self._pending_row = (y0, y1)
                self._open_timer.start()
        super().mouseMoveEvent(e)

    def _open_submenu(self):
        idx = self.hover_index
        if idx < 0 or self.child:
            return
        node = self.items[idx]
        if not node.get("sub"):
            return
        y0, y1 = self._pending_row
        rect = QRect(self.x(), int(self.y() + y0), self.width(), int(y1 - y0))
        self.child = GlassMenu(node["sub"], parent_item_rect=rect, parent=self)
        self.child.show()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        idx, _, _ = self._row_at(e.position().toPoint())
        if idx < 0:
            return
        node = self.items[idx]
        if node.get("sub") or not node.get("enabled", True):
            return

        ref = node.get("ref")
        # checked 不是 None 的是开关类（勾选项），点完不关菜单，
        # 否则连改两项就得重开一次，没法用
        is_toggle = ref is not None and getattr(ref, "checked", None) is not None

        if ref is not None and getattr(ref, "callback", None):
            try:
                ref.callback(ref)
            except Exception as ex:
                logger.error("menu callback error: {}".format(ex))

        if is_toggle:
            for m in list(GlassMenu._open_chain):
                m.update()
        else:
            GlassMenu.close_all()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            GlassMenu.close_all()
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        # 失焦后等一下再判断，鼠标可能正移向子菜单
        QTimer.singleShot(160, self._maybe_close)

    @staticmethod
    def _cursor_inside_any() -> bool:
        cur = QCursor.pos()
        for m in list(GlassMenu._open_chain):
            if not m.isVisible():
                continue
            g = QRect(m.mapToGlobal(QPoint(0, 0)), m.size())
            if g.contains(cur):
                return True
        return False

    def _maybe_close(self):
        if GlassMenu._cursor_inside_any():
            return
        for m in list(GlassMenu._open_chain):
            if m.isVisible() and m.isActiveWindow():
                return
        GlassMenu.close_all()

    def leaveEvent(self, e):
        self._open_timer.stop()
        super().leaveEvent(e)

    # ---- 绘制 ----

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)

        p.setClipPath(path)
        p.fillPath(path, T.TINT)

        if self.hover_index >= 0:
            for y0, y1, i in self._rows:
                if i == self.hover_index:
                    hov = QRectF(self.PAD, y0 + 1,
                                 self.width() - self.PAD * 2, y1 - y0 - 2)
                    hp = QPainterPath()
                    hp.addRoundedRect(hov, 8, 8)
                    p.fillPath(hp, T.HOVER)
                    break

        for y0, y1, i in self._rows:
            it = self.items[i]
            if it["type"] == "header":
                self._draw_header(p, it, y0)
            elif it["type"] == "sep":
                cy = (y0 + y1) / 2.0
                if i > 0 and self.items[i - 1]["type"] == "header":
                    cy += 3
                p.setPen(QPen(T.SEP, 1))
                p.drawLine(QPoint(self.PAD + 14, int(cy)),
                           QPoint(self.width() - self.PAD - 14, int(cy)))
            else:
                self._draw_item(p, it, y0, y1)

        p.setClipping(False)
        p.setPen(QPen(T.BORDER, 1))
        p.drawPath(path)
        p.end()

    def _text_at(self, p, font, color, x, top, height, text):
        """按整数基线绘制：半像素基线上抗锯齿会糊成灰边。"""
        p.setFont(font)
        p.setPen(color)
        fm = QFontMetrics(font)
        baseline = int(round(top + (height - fm.height()) / 2.0 + fm.ascent()))
        p.drawText(QPoint(int(round(x)), baseline), text)

    def _draw_header(self, p, it, y0):
        # 标题分两段：主机名 16px，其余（- Macast-Glass v1.0）15px
        title = it["title"] or ""
        sep = " - "
        idx = title.find(sep)
        top, height = y0 + 5, 22
        x = self.PAD + 14

        if idx > 0:
            head, rest = title[:idx], title[idx:]
            fm_big = QFontMetrics(self._font_name)
            base = int(round(top + (height - fm_big.height()) / 2.0 + fm_big.ascent()))
            p.setFont(self._font_name)
            p.setPen(T.TEXT)
            p.drawText(QPoint(x, base), head)
            x += fm_big.horizontalAdvance(head) + 7   # 主机名与后半段之间多留点空隙
            fm_norm = QFontMetrics(self._font_bold)
            base_n = int(round(top + (height - fm_norm.height()) / 2.0 + fm_norm.ascent()))
            p.setFont(self._font_bold)
            p.setPen(T.TITLE_SUFFIX)
            p.drawText(QPoint(x, base_n), rest)
        else:
            self._text_at(p, self._font_bold, T.TEXT, x, top, height, title)

        self._text_at(p, self._font_sub, T.TEXT_DIM,
                      self.PAD + 14, y0 + 30, 18, it["sub"])

    def _draw_item(self, p, it, y0, y1):
        cy = (y0 + y1) / 2.0
        x = self.PAD + 14
        # 状态直接读 MenuItem 本体，不用转换时的快照——
        # 否则点了开关项后勾选不会变
        ref = it.get("ref")
        if ref is not None:
            enabled = bool(getattr(ref, "enabled", True))
            checked = bool(getattr(ref, "checked", False))
        else:
            enabled = bool(it.get("enabled", True))
            checked = bool(it.get("checked", False))

        key = it.get("icon") or icon_key_for(it.get("label", ""))
        if key:
            pm = icon_pixmap(key, self.ICON, color=T.ICON,
                             dpr=self.devicePixelRatioF())
            if not pm.isNull():
                p.setOpacity(1.0 if enabled else 0.45)
                target = QRectF(x, cy - self.ICON / 2.0, self.ICON, self.ICON)
                p.drawPixmap(target, pm, QRectF(pm.rect()))
                p.setOpacity(1.0)
        if self._any_icon:
            x += self.ICON + self.GAP

        right = self.width() - self.PAD - 12
        if it.get("sub"):
            self._draw_chevron(p, right - 6, cy)
            right -= 24
        elif checked:
            self._draw_tick(p, right - 7, cy)
            right -= 24

        label_w = right - x
        if label_w <= 10:
            return
        elided = QFontMetrics(self._font).elidedText(
            it["label"], Qt.ElideRight, int(label_w))
        self._text_at(p, self._font, T.TEXT if enabled else T.TEXT_OFF,
                      x, y0, y1 - y0, elided)

    def _draw_chevron(self, p, cx, cy):
        p.setPen(QPen(T.CHEVRON, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        path = QPainterPath()
        path.moveTo(cx - 2, cy - 4)
        path.lineTo(cx + 3, cy)
        path.lineTo(cx - 2, cy + 4)
        p.drawPath(path)

    def _draw_tick(self, p, cx, cy):
        p.setPen(QPen(T.ACCENT, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        path = QPainterPath()
        path.moveTo(cx - 5, cy)
        path.lineTo(cx - 1, cy + 4)
        path.lineTo(cx + 6, cy - 5)
        p.drawPath(path)


# ---------------------------------------------------------------- 托盘

class GlassTray:
    """用 QSystemTrayIcon + 自绘菜单替代 pystray。"""


    def __init__(self, name, icon_path, menu_getter):
        self.name = name
        self.icon_path = icon_path
        self.menu_getter = menu_getter
        self.qapp = QApplication.instance()
        if self.qapp is None:
            self.qapp = QApplication(sys.argv)
        self._menu = None          # 必须持引用，否则局部变量被回收后窗口消失
        apply_theme_mode()
        self.tray = QSystemTrayIcon(self._make_icon(icon_path))
        self.tray.setToolTip(name)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

    @staticmethod
    def _make_icon(path) -> QIcon:
        """多尺寸喂给 QIcon，让系统按 DPI 挑，避免缩放糊。"""
        icon = QIcon()
        pm = QPixmap(path)
        if pm.isNull():
            return icon
        for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            if s <= max(pm.width(), 1):
                icon.addPixmap(pm.scaled(s, s, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        icon.addPixmap(pm)
        return icon

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Context, QSystemTrayIcon.Trigger):
            self.popup()

    def popup(self):
        GlassMenu.close_all()
        try:
            nodes = _promote_header(convert_menu(self.menu_getter()))
        except Exception as e:
            logger.error("build menu failed: {}".format(e))
            return
        if not nodes:
            return

        apply_theme_mode()   # 每次弹出前重新解析 auto
        m = GlassMenu(nodes)
        mw, mh = m.width(), m.height()

        cur = QCursor.pos()
        scr = QGuiApplication.screenAt(cur) or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else QRect(0, 0, 1920, 1080)

        g = self.tray.geometry()
        if g.isValid() and g.width() > 0:
            x = g.center().x() - mw // 2
            y = g.top() - mh - 8
        else:
            x = cur.x() - mw + 10
            y = cur.y() - mh - 10

        x = max(avail.left() + 4, min(x, avail.right() - mw - 4))
        y = max(avail.top() + 4, min(y, avail.bottom() - mh - 4))

        # 对齐到物理像素网格：150% 缩放下落在半像素上，整块窗口
        # 会被 DWM 重采样，文字边缘就毛了
        dpr = scr.devicePixelRatio() if scr else 1.0
        if dpr and dpr > 1.0:
            x = round(x * dpr) / dpr
            y = round(y * dpr) / dpr

        self._menu = m
        m.move(int(x) if float(x).is_integer() else x,
               int(y) if float(y).is_integer() else y)

        # 延迟一下再 show，否则托盘右键的 release 会立刻把 popup 关掉
        QTimer.singleShot(60, m.show)

    def set_icon(self, path):
        self.icon_path = path
        self.tray.setIcon(self._make_icon(path))

    def notify(self, title, message):
        try:
            self.tray.showMessage(title, message,
                                  QSystemTrayIcon.Information, 3000)
        except Exception as e:
            logger.error("notify failed: {}".format(e))

    def exec_(self):
        return self.qapp.exec()

    def stop(self):
        self.tray.hide()
        self.qapp.quit()
