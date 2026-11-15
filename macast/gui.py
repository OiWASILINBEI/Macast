# Copyright (c) 2021 by xfangfang. All Rights Reserved.
# Modifications for Macast-Glass:
#   Windows 分支改为 Qt 自绘托盘菜单（glass_menu.GlassTray），
#   以获得系统级亚克力模糊。macOS(rumps) 与 Linux(pystray) 分支保持不变。

import sys
import logging
import subprocess
import webbrowser
from enum import Enum
from .utils import Setting

if sys.platform == 'darwin':
    import rumps
elif sys.platform == 'win32':
    from .glass_menu import GlassTray
else:
    import pystray
    from PIL import Image

logger = logging.getLogger("gui")
logger.setLevel(logging.INFO)


class Platform(Enum):
    Darwin = 0
    Win32 = 1
    Others = 2


class MenuItem:
    def __init__(self, text, callback=None, checked=None, enabled=True,
                 children=None, data=None, key=None):
        self.view = None
        if sys.platform == 'darwin':
            self.platform = Platform.Darwin
        elif sys.platform == 'win32':
            self.platform = Platform.Win32
        else:
            self.platform = Platform.Others

        self._text = text
        self.callback = callback
        self.children = children
        self._checked = checked
        self._enabled = enabled
        self.data = data
        self.id = text
        self.key = key

    @property
    def text(self):
        return self._text

    @property
    def checked(self):
        return self._checked

    @property
    def enabled(self):
        return self._enabled

    @text.setter
    def text(self, value):
        self._text = value
        if self.view is None:
            return
        if self.platform == Platform.Darwin:
            self.view.title = self._text

    @checked.setter
    def checked(self, value):
        self._checked = value
        if self.view is None:
            return
        if self.platform == Platform.Darwin:
            self.view.state = 1 if self._checked else 0

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        if self.view is None:
            return
        if self.platform == Platform.Darwin:
            self.view.set_callback(
                self._rumpsCallback if self._enabled else None,
                self.key)

    def items(self):
        return [] if self.children is None else self.children

    def _pystrayCallback(self, app, item):
        self.callback(self)

    def _rumpsCallback(self, item):
        self.callback(self)


class App:
    def __init__(self, name, icon, menu, template=True):
        self.name = name
        self.icon = icon
        self.app = None
        self.menu = menu
        self.menuDict = {}
        self.template = template
        if sys.platform == 'darwin':
            self.platform = Platform.Darwin
            self.init_platform_darwin()
        elif sys.platform == 'win32':
            self.platform = Platform.Win32
            self.init_platform_win32()
        else:
            self.platform = Platform.Others
            self.init_platform_others()

        if self.platform == Platform.Darwin:
            self.app = rumps.App(self.name,
                                 icon=self.icon,
                                 menu=self._build_menu_rumps(self.menu),
                                 template=self.template,
                                 quit_button=None)
            rumps.debug_mode(True)
        elif self.platform == Platform.Win32:
            # Qt 自绘菜单：每次弹出时向子类要一次最新的菜单树
            self.app = GlassTray(self.name, self.icon, self._menu_for_tray)
        else:
            self.app = pystray.Icon(self.name,
                                    Image.open(self.icon),
                                    menu=pystray.Menu(
                                        lambda:
                                        self._build_menu_pystray(self.menu)))

    def init_platform_darwin(self):
        pass

    def _menu_for_tray(self):
        """Windows 托盘每次弹出前调用。子类可覆盖以刷新动态内容。"""
        return self.menu

    def init_platform_win32(self):
        pass

    def init_platform_others(self):
        pass

    def _build_menu_rumps(self, menu):
        items = []
        for item in menu:
            if item is None:
                items.append(None)
            elif item.children is not None:
                menu_item = rumps.MenuItem(item.text)
                items.append([menu_item, self._build_menu_rumps(item.children)])
            else:
                items.append(self._build_menu_item_rumps(item))
        return items

    def _build_menu_item_rumps(self, item):
        callback = item._rumpsCallback if item.enabled else None
        menu_item = rumps.MenuItem(item.text, callback, item.key)
        menu_item.state = 1 if item.checked else 0
        item.view = menu_item
        return menu_item

    def _build_menu_pystray(self, menu):
        items = []
        for item in menu:
            if item is None:
                items.append(pystray.Menu.SEPARATOR)
            elif item.children is not None and len(item.children) > 0:
                menu_item = pystray.MenuItem(
                    item.text, pystray.Menu(
                        *self._build_menu_pystray(item.children)))
                items.append(menu_item)
            else:
                menu_item = pystray.MenuItem(lambda i: i.view.text,
                                             item._pystrayCallback,
                                             lambda i: True if i.view.checked
                                             else None,
                                             enabled=lambda i: i.view.enabled)
                item.view = menu_item
                menu_item.view = item
                items.append(menu_item)
        return items

    def update_icon(self, icon, template=True):
        self.icon = icon
        if self.platform == Platform.Darwin:
            self.app.template = template
            self.app.icon = self.icon
        elif self.platform == Platform.Win32:
            self.app.set_icon(self.icon)
        else:
            self.app.icon = Image.open(self.icon)

    def update_menu(self):
        """ refresh current menu
            only windows and linux needed
        """
        if self.platform == Platform.Others:
            self.app.update_menu()

    def set_menu(self, menu):
        self.menu = menu
        if self.platform == Platform.Darwin:
            self.app.menu.clear()
            self.app.menu = self._build_menu_rumps(menu)
        elif self.platform == Platform.Win32:
            # 自绘菜单每次弹出时重建，这里无需动作
            pass
        else:
            self.app.menu = pystray.Menu(lambda: self._build_menu_pystray(menu))

    def _find_menu_item_index_by_id(self, id):
        #  TODO find all items
        for i, item in enumerate(self.menu):
            if item is not None and item.id is not None and item.id == id:
                return i
        logger.error("Canot find id:{}.".format(id))
        return -1

    def append_menu_item_after(self, id, menu_item):
        if self.platform == Platform.Darwin:
            self.app.menu.insert_after(id, self._build_menu_item_rumps(menu_item))
        else:
            index = self._find_menu_item_index_by_id(id)
            if index != -1:
                self.menu.insert(index + 1, menu_item)
                self.update_menu()

    def append_menu_item_before(self, id, menu_item):
        if self.platform == Platform.Darwin:
            self.app.menu.insert_before(id, self._build_menu_item_rumps(menu_item))
        else:
            index = self._find_menu_item_index_by_id(id)
            if index != -1:
                self.menu.insert(index, menu_item)
                self.update_menu()

    def remove_menu_item_by_id(self, id):
        if self.platform == Platform.Darwin:
            if id in self.app.menu:
                self.app.menu.pop(id)
        else:
            index = self._find_menu_item_index_by_id(id)
            if index != -1:
                self.menu.pop(index)
                self.update_menu()

    def start(self):
        if self.platform == Platform.Win32:
            return self.app.exec_()
        self.app.run()

    def quit(self, _):
        if self.platform == Platform.Darwin:
            rumps.quit_application()
        elif self.platform == Platform.Win32:
            self.app.stop()
        else:
            try:
                self.app.remove_notification()
            except NotImplementedError as e:
                pass
            self.app.stop()

    def alert(self, content):
        if self.platform == Platform.Darwin:
            rumps.alert(content)
        else:
            self.notification(content, "Macast")

    def notification(self, title, content, sound=True):
        if self.platform == Platform.Darwin:
            rumps.notification(title, "", content, sound=sound)
        elif self.platform == Platform.Win32:
            self.app.notify(title, content)
        else:
            try:
                self.app.notify(message=content, title=title)
            except NotImplementedError as e:
                pass

    def dialog(self, content, callback=None, cancel="Cancel", ok="Ok"):
        if self.platform == Platform.Darwin:
            try:
                res = Setting.system_shell(
                    ['osascript',
                     '-e',
                     'display dialog "{}" buttons {{"{}","{}"}}'.format(
                         content, cancel, ok)
                     ])
                if ok in res[1] and callback:
                    callback()
            except Exception as e:
                self.notification("Error", "Cannot access System Events")
                logger.error(e)
                callback()
        else:
            self.notification("Macast", content)
            if callback:
                callback()

    def get_env(self):
        # https://github.com/pyinstaller/pyinstaller/issues/3668#issuecomment-742547785
        env = Setting.get_system_env()
        toDelete = []
        for (k, v) in env.items():
            if k != 'PATH' and 'tmp' in v:
                toDelete.append(k)
        for k in toDelete:
            env.pop(k, None)
        return env

    def open_browser(self, url):
        if self.platform == Platform.Darwin:
            subprocess.Popen(['open', url])
        elif self.platform == Platform.Win32:
            webbrowser.open(url)
        else:
            subprocess.Popen(["xdg-open", url], env=self.get_env())

    def open_directory(self, path):
        if self.platform == Platform.Darwin:
            subprocess.Popen(['open', path])
        elif self.platform == Platform.Win32:
            subprocess.Popen(['explorer.exe', path])
        else:
            subprocess.Popen(["xdg-open", path], env=self.get_env())

    @staticmethod
    def build_menu_item_group(titles, callback):
        """批量创建单选组。checked 必须显式给 False——
        否则为 None 时会被当成「无状态命令项」，点一下就关菜单，
        没法连续切换。"""
        items = []
        for index, title in enumerate(titles):
            item = MenuItem(title, callback, data=index, checked=False)
            items.append(item)
        return items
