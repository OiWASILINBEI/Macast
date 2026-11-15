# Copyright (c) 2021 by xfangfang. All Rights Reserved.

import os
import re
import sys
import time
import json
import platform
import cherrypy
import logging
import threading
import requests
import pyperclip
import gettext
import importlib

from .utils import SettingProperty, SETTING_DIR, notify_error, format_class_name
from .gui import App, MenuItem, Platform
from .protocol import DLNAProtocol
from .server import Service
from .utils import RENDERER_DIR, PROTOCOL_DIR, Setting
from macast_renderer.mpv import MPVRenderer

logger = logging.getLogger("main")
logger.setLevel(logging.DEBUG)
_ = gettext.gettext


class MacastPlugin:

    def __init__(self, path, title="None", plugin_instance=None, platform='none'):
        # path is allowed to be set to None only when renderer is macast default plugin
        self.path = path
        self.title = title
        self.plugin_class = None
        self.plugin_instance = plugin_instance
        self.platform = platform
        if path:
            try:
                self.load_from_file(path)
            except Exception as e:
                cherrypy.engine.publish('app_notify', 'ERROR', 'Custom plugin load error.')
                logger.error(str(e))

    def get_info(self):
        props = ['protocol', 'title', 'renderer', 'platform', 'version', 'author', 'desc']
        res = {'default': False}
        for i in props:
            res[i] = getattr(self, i, '')
        if getattr(self, 'renderer', None) is not None:
            res['type'] = 'renderer'
        if getattr(self, 'protocol', None) is not None:
            res['type'] = 'protocol'
        if self.path is None:
            res['default'] = True
            res['desc'] = 'Macast default plugin'
            res['version'] = Setting.version
        return res

    def get_instance(self):
        if self.plugin_instance is None and self.plugin_class is not None:
            self.plugin_instance = self.plugin_class()

        return self.plugin_instance

    def check(self):
        """ Check if this renderer can run on your device
        """
        if self.plugin_class is None:
            return False
        if sys.platform in self.platform:
            return True

        logger.error("{} support platform: {}".format(self.title, self.platform))
        logger.error("{} is not suit for this system.".format(self.title))
        return False

    def load_from_file(self, path):
        base_name = os.path.basename(path)[:-3]
        with open(path, 'r', encoding='utf-8') as f:
            renderer_file = f.read()
            metadata = re.findall("<macast.(.*?)>(.*?)</macast", renderer_file)
            print("<Load Plugin from {}".format(base_name))
            for key, value in metadata:
                print('%-10s: %s' % (key, value))
                setattr(self, key, str(value))
        if hasattr(self, 'renderer'):
            module = importlib.import_module(f'{RENDERER_DIR}.{base_name}')
            print(f'Load plugin {self.renderer} done />\n')
            self.plugin_class = getattr(module, self.renderer, None)
        elif hasattr(self, 'protocol'):
            module = importlib.import_module(f'{PROTOCOL_DIR}.{base_name}')
            print(f'Load plugin {self.protocol} done />\n')
            self.plugin_class = getattr(module, self.protocol, None)
        else:
            logger.error(f"Cannot find any plugin in {base_name}")
            return


class MacastPluginManager:

    def __init__(self, renderer_default, protocol_default):
        sys.path.append(SETTING_DIR)
        self.create_plugin_dir(RENDERER_DIR)
        self.create_plugin_dir(PROTOCOL_DIR)
        self.renderer_list = [renderer_default]
        self.renderer_list += self.load_macast_plugin(RENDERER_DIR)
        self.protocol_list = [protocol_default]
        self.protocol_list += self.load_macast_plugin(PROTOCOL_DIR)

    def get_renderer(self, name):
        plugin = self.get_plugin_from_list(self.renderer_list, name)
        Setting.set(SettingProperty.Macast_Renderer, plugin.title)
        return plugin.get_instance()

    def get_protocol(self, name):
        plugin = self.get_plugin_from_list(self.protocol_list, name)
        Setting.set(SettingProperty.Macast_Protocol, plugin.title)
        return plugin.get_instance()

    def get_info(self):
        res = []
        for r in self.renderer_list:
            res.append(r.get_info())
        for p in self.protocol_list:
            res.append(p.get_info())
        return res

    @staticmethod
    def get_plugin_from_list(plugin_list, title) -> MacastPlugin:
        for i in plugin_list:
            if title == i.title:
                print("using plugin: {}".format(title))
                return i
        else:
            print("using default plugin")
            return plugin_list[0]

    @staticmethod
    def load_macast_plugin(path: str):
        plugin_path = os.path.join(SETTING_DIR, path)
        if not os.path.exists(plugin_path):
            return
        plugin_list = []
        plugins = os.listdir(plugin_path)
        plugins = filter(lambda s: s.endswith('.py') and s != '__init__.py', plugins)
        for plugin in plugins:
            path = os.path.join(plugin_path, plugin)
            plugin_config = MacastPlugin(path)
            if plugin_config.check():
                plugin_list.append(plugin_config)
        return plugin_list

    @staticmethod
    @notify_error('Cannot create custom plugin dir.')
    def create_plugin_dir(path):
        custom_module_path = os.path.join(SETTING_DIR, path)
        if not os.path.exists(custom_module_path):
            os.makedirs(custom_module_path)
        init_file_path = os.path.join(custom_module_path, '__init__.py')
        if not os.path.exists(init_file_path):
            open(init_file_path, 'a').close()


class Macast(App):
    if sys.platform == 'win32':
        ICON_MAP = ['assets/icon.ico',
                    'assets/menu_light_large.png',
                    'assets/menu_dark_large.png']
    else:
        ICON_MAP = ['assets/icon.png',
                    'assets/menu_light.png',
                    'assets/menu_dark.png']

    def __init__(self, renderer, protocol, lang=gettext.gettext):
        global _
        _ = lang
        # menu items
        self.toggle_menuitem = None
        self.setting_menuitem = None
        self.quit_menuitem = None
        self.ip_menuitem = None
        self.version_menuitem = None
        self.auto_check_update_menuitem = None
        self.start_at_login_menuitem = None
        self.menubar_icon_menuitem = None
        self.check_update_menuitem = None
        self.about_menuitem = None
        self.open_config_menuitem = None
        self.renderer_menuitem = None
        self.protocol_menuitem = None
        self.advanced_menuitem = None
        self.menu_theme_menuitem = None

        self.plugin_manager = MacastPluginManager(
            MacastPlugin(None, format_class_name(renderer), renderer, 'darwin,win32,linux'),
            MacastPlugin(None, format_class_name(protocol), protocol, 'darwin,win32,linux'))

        cherrypy.engine.subscribe('get_plugin_info', self.plugin_manager.get_info)

        # setting items
        self.setting_start_at_login = None
        self.setting_check = None
        self.setting_menubar_icon = 0
        self.setting_renderer = ''
        self.setting_protocol = ''
        self.init_setting()

        # init service
        self.service = Service(self.plugin_manager.get_renderer(self.setting_renderer),
                               self.plugin_manager.get_protocol(self.setting_protocol))

        icon_path = os.path.join(os.path.dirname(__file__), Macast.ICON_MAP[self.setting_menubar_icon])
        template = None if self.setting_menubar_icon == 0 else True
        self.copy_menuitem = None
        # 源码运行时 sys.executable 是 python.exe，写注册表自启没有意义，
        # 所以只在打包后真正启用；这里记下来给菜单和点击回调用
        self.is_source_run = (sys.platform == 'win32' and
                             "python" in os.path.basename(sys.executable).lower())
        # 协议/播放器切换是耗时操作，连点两次会让 stop/start 交错，
        # 用一把锁把切换串行化，最终状态以最后一次选择为准
        self._switch_lock = threading.Lock()
        super(Macast, self).__init__("Macast-Glass",
                                     icon_path,
                                     self.build_app_menu(),
                                     template
                                     )
        cherrypy.engine.subscribe('start', self.service_start)
        cherrypy.engine.subscribe('stop', self.service_stop)
        cherrypy.engine.subscribe('renderer_start', self.renderer_start)
        cherrypy.engine.subscribe('renderer_av_stop', self.renderer_av_stop)
        cherrypy.engine.subscribe('renderer_av_uri', self.renderer_av_uri)
        cherrypy.engine.subscribe('ssdp_update_ip', self.update_service_ip)
        cherrypy.engine.subscribe('app_notify', self.notification)
        self.start_cast()
        logger.debug("Macast APP started")

    @staticmethod
    def menu_title():
        """菜单标题：电脑名 - 软件名 v版本号"""
        return "{} - {} v{}".format(platform.node(), "Macast-Glass", Setting.get_version())

    def build_app_menu(self):
        # Macast-Glass：设备名 + IP 提到根菜单当标题区（效果稿结构），
        # 原版是塞在「设置」子菜单最上面两项。
        ip_text = "/".join([ip for ip, _ in Setting.peek_ip()])
        port = Setting.get_port()
        self.version_menuitem = MenuItem(self.menu_title(), enabled=False)
        self.ip_menuitem = MenuItem("{}:{}".format(ip_text, port), enabled=False)

        self.toggle_menuitem = MenuItem(_("Stop Cast"), self.on_toggle_service_click, key="p")
        self.setting_menuitem = MenuItem(_("Setting"), children=self.build_setting_menu())
        self.quit_menuitem = MenuItem(_("Quit"), self.quit, key="q")
        return [
            self.version_menuitem,
            self.ip_menuitem,
            None,
            self.toggle_menuitem,
            self.setting_menuitem,
            self.quit_menuitem
        ]

    def build_setting_menu(self):
        self.auto_check_update_menuitem = MenuItem(_("Auto Check Updates"),
                                                   self.on_auto_check_update_click,
                                                   checked=self.setting_check)
        self.start_at_login_menuitem = MenuItem(_("Start At Login"),
                                                self.on_start_at_login_click,
                                                checked=self.setting_start_at_login)

        renderer_names = [r.title for r in self.plugin_manager.renderer_list]
        renderer_select = []
        if len(renderer_names) > 1:
            self.renderer_menuitem = MenuItem(_("Renderers"),
                                              children=App.build_menu_item_group(renderer_names,
                                                                                 self.on_renderer_change_click))
            renderer_select = [self.renderer_menuitem]
            for i in self.renderer_menuitem.children:
                if i.text == self.setting_renderer:
                    i.checked = True
                    break
            else:
                self.renderer_menuitem.children[0].checked = True

        protocol_names = [r.title for r in self.plugin_manager.protocol_list]
        protocol_select = []
        if len(protocol_names) > 1:
            self.protocol_menuitem = MenuItem(_("Protocols"),
                                              children=App.build_menu_item_group(protocol_names,
                                                                                 self.on_protocol_change_click))
            protocol_select = [self.protocol_menuitem]
            for i in self.protocol_menuitem.children:
                if i.text == self.setting_protocol:
                    i.checked = True
                    break
            else:
                self.protocol_menuitem.children[0].checked = True

        platform_options = []
        """To judge whether Macast was launched by scripts or by packaged app.
        """
        if sys.platform == 'darwin' and sys.executable.endswith("Contents/MacOS/python"):
            platform_options = [self.start_at_login_menuitem]
            # Reset StartAtLogin to prevent the user from turning off
            # this option from the system settings
            Setting.set_start_at_login(self.setting_start_at_login)
        elif sys.platform == 'win32':
            platform_options = [self.start_at_login_menuitem]
            if not self.is_source_run:
                Setting.set_start_at_login(self.setting_start_at_login)
        if sys.platform == 'darwin':
            self.menubar_icon_menuitem = MenuItem(_("Menubar Icon"),
                                                  children=App.build_menu_item_group([
                                                      _("AppIcon"),
                                                      _("Pattern"),
                                                  ], self.on_menubar_icon_change_click))
        else:
            self.menubar_icon_menuitem = MenuItem(_("Menubar Icon"),
                                                  children=App.build_menu_item_group([
                                                      _("AppIcon"),
                                                      _("PatternLight"),
                                                      _("PatternDark"),
                                                  ], self.on_menubar_icon_change_click))
        # Macast-Glass：菜单主题（仅 Windows，Qt 自绘菜单才有意义）
        menu_theme_select = []
        if sys.platform == 'win32':
            self.menu_theme_menuitem = MenuItem(
                _("Menu Theme"),
                children=App.build_menu_item_group(
                    [_("Follow System"), _("Dark"), _("Light")],
                    self.on_menu_theme_change_click))
            try:
                from .glass_menu import get_theme_mode
                idx = {"auto": 0, "dark": 1, "light": 2}.get(get_theme_mode(), 0)
                self.menu_theme_menuitem.children[idx].checked = True
            except Exception as e:
                logger.error(e)
            menu_theme_select = [self.menu_theme_menuitem]

        self.open_config_menuitem = MenuItem(_("Open Config Directory"), self.on_open_config_click)
        self.advanced_menuitem = MenuItem(_("Advanced Setting"),
                                          lambda _: self.open_browser('http://127.0.0.1:{}'.format(Setting.get_port())))
        self.check_update_menuitem = MenuItem(_("Check For Updates"), self.on_check_click)
        self.about_menuitem = MenuItem(_("Help"), self.on_about_click)

        self.menubar_icon_menuitem.items()[self.setting_menubar_icon].checked = True
        player_settings = self.service.renderer.renderer_setting.build_menu()
        if len(player_settings) > 0:
            player_settings.append(None)

        return renderer_select + \
               protocol_select + \
               [None] + \
               player_settings + \
               [self.menubar_icon_menuitem] + \
               menu_theme_select + \
               [None, self.auto_check_update_menuitem,
                self.open_config_menuitem, self.advanced_menuitem] + \
               platform_options + \
               [None, self.check_update_menuitem, self.about_menuitem]

    def init_setting(self):
        self.setting_start_at_login = Setting.get(SettingProperty.StartAtLogin, 0)
        self.setting_check = Setting.get(SettingProperty.CheckUpdate, 1)
        self.setting_menubar_icon = Setting.get(SettingProperty.MenubarIcon, 1 if sys.platform == 'darwin' else 0)
        self.setting_renderer = Setting.get(SettingProperty.Macast_Renderer, 'MPV')
        self.setting_protocol = Setting.get(SettingProperty.Macast_Protocol, 'DLNA')
        if self.setting_check:
            threading.Thread(target=self.check_update,
                             kwargs={
                                 'verbose': False
                             },
                             daemon=True,
                             name="CHECKUPDATE_THREAD").start()

    def stop_cast(self):
        self.service.stop()

    def start_cast(self):
        self.service.run_async()

    def check_update(self, verbose=True):
        release_url = 'https://github.com/xfangfang/Macast/releases/latest'
        api_url = 'https://api.github.com/repos/xfangfang/Macast/releases/latest'
        try:
            res = json.loads(requests.get(api_url).text)
            online_version = re.findall(r'(\d+\.*\d+)', res['tag_name'])[0]

            logger.info("tag_name: {}".format(res['tag_name']))

            if float(Setting.get_version()) < float(online_version):
                self.dialog(_("Macast New Update {}").format(res['tag_name']),
                            lambda: self.open_browser(release_url),
                            ok="Update")
            else:
                if verbose:
                    self.notification("Macast", _("You're up to date."))
        except Exception as e:
            logger.error("get update info error: {}".format(e))

    # The followings are the callback function of program event

    def update_service_status(self):
        if Setting.is_service_running():
            self.toggle_menuitem.text = _('Stop Cast')
        else:
            self.toggle_menuitem.text = _('Start Cast')
        self.update_menu()

    def service_start(self):
        """This function is called every time the DLNA service is started.
        Displays a notification reminding the user that the service has started
        """
        logger.info("service_start")
        if self.platform is Platform.Win32:
            msg = _("running at task bar")
        elif self.platform is Platform.Darwin:
            msg = _("running at menu bar")
        else:
            msg = _("running at desktop panel")
        if self.platform == Platform.Darwin:
            self.notification(_("Macast is hidden"), msg, sound=False)
        else:
            # Pystray may fail to send notifications due to incomplete initialization
            # during the startup, so wait a moment
            threading.Thread(target=lambda: (
                time.sleep(2),
                self.notification(_("Macast is hidden"), msg, sound=False),
            )).start()
        self.update_service_status()

    def service_stop(self):
        """This function is called every time the DLNA service is stopped.
        """
        logger.info("service_stop")
        self.update_service_status()

    def update_service_ip(self):
        """When the IP or port of the device changes,
        call this function to refresh the device address on the menu
        """
        logger.info("ssdp_update_ip")
        if self.ip_menuitem is not None:
            ip_text = "/".join([ip for ip, _ in Setting.peek_ip()])
            port = Setting.get_port()
            self.ip_menuitem.text = "{}:{}".format(ip_text, port)
        self.version_menuitem.text = self.menu_title()
        self.update_menu()

    def renderer_av_stop(self):
        logger.info("renderer_av_stop")
        if self.copy_menuitem:
            self.remove_menu_item_by_id(self.copy_menuitem.id)
        self.copy_menuitem = None

    def renderer_start(self):
        pass

    def renderer_av_uri(self, uri):
        logger.info("renderer_av_uri: " + uri)
        if self.copy_menuitem is not None:
            self.copy_menuitem.callback = lambda _: pyperclip.copy(uri)
            return
        self.copy_menuitem = MenuItem(
            _("Copy Video URI"),
            key="c",
            callback=lambda _: pyperclip.copy(uri))
        self.append_menu_item_after(self.toggle_menuitem.id, self.copy_menuitem)

    # The followings are the callback function of menu click

    def on_protocol_change_click(self, item):
        protocol_config = self.plugin_manager.protocol_list[item.data]
        Setting.set(SettingProperty.Macast_Protocol, protocol_config.title)
        self.setting_protocol = protocol_config.title
        # 先刷勾选，服务启停丢后台——stop/start 是阻塞的，压在界面线程上
        # 会卡两三秒，表现成「点了没反应」
        if self.protocol_menuitem is not None:
            for i in self.protocol_menuitem.items():
                i.checked = False
        item.checked = True
        threading.Thread(target=self._apply_protocol,
                         args=(protocol_config,),
                         daemon=True,
                         name="PROTOCOL_SWITCH").start()

    def _apply_protocol(self, protocol_config):
        with self._switch_lock:
            self._do_apply_protocol(protocol_config)

    def _do_apply_protocol(self, protocol_config):
        try:
            self.stop_cast()
            # todo 生成新的 uuid
            self.service.protocol = protocol_config.get_instance()
            self.start_cast()
            cherrypy.engine.publish(
                'app_notify', _('Info'),
                _('Change Protocol to {}.').format(protocol_config.title))
        except Exception as e:
            logger.error("apply protocol failed: {}".format(e))

    def on_renderer_change_click(self, item):
        renderer_config = self.plugin_manager.renderer_list[item.data]
        Setting.set(SettingProperty.Macast_Renderer, renderer_config.title)
        self.setting_renderer = renderer_config.title
        if self.renderer_menuitem is not None:
            for i in self.renderer_menuitem.items():
                i.checked = False
        item.checked = True
        # 实例化播放器插件也不一定快，同样丢后台
        threading.Thread(target=self._apply_renderer,
                         args=(renderer_config,),
                         daemon=True,
                         name="RENDERER_SWITCH").start()

    def _apply_renderer(self, renderer_config):
        with self._switch_lock:
            self._do_apply_renderer(renderer_config)

    def _do_apply_renderer(self, renderer_config):
        try:
            self.service.renderer = renderer_config.get_instance()
            cherrypy.engine.publish(
                'app_notify', _('Info'),
                _('Change Renderer to {}.').format(renderer_config.title))
        except Exception as e:
            logger.error("apply renderer failed: {}".format(e))

    def on_open_config_click(self, item):
        self.open_directory(SETTING_DIR)

    def on_check_click(self, item):
        threading.Thread(target=self.check_update,
                         daemon=True,
                         name="CHECKUPDATE_M_THREAD").start()

    def on_auto_check_update_click(self, item):
        item.checked = not item.checked
        Setting.set(SettingProperty.CheckUpdate,
                    1 if item.checked else 0)

    def on_start_at_login_click(self, item):
        if self.is_source_run:
            item.checked = not item.checked
            self.notification(_("Info"),
                              _("Source mode: this only takes effect after packaging."))
            return
        res = Setting.set_start_at_login(not item.checked)
        if res[0] == 0:
            item.checked = not item.checked
            Setting.set(SettingProperty.StartAtLogin,
                        1 if item.checked else 0)
        else:
            self.notification(_("Error"), _(res[1]))

    def on_about_click(self, _):
        self.open_browser('http://127.0.0.1:{}?page=4'.format(Setting.get_port()))

    def on_toggle_service_click(self, item):
        if Setting.is_service_running():
            self.stop_cast()
        else:
            self.start_cast()

    def on_menubar_icon_change_click(self, item):
        for i in self.menubar_icon_menuitem.items():
            i.checked = False
        item.checked = True
        Setting.set(SettingProperty.MenubarIcon, item.data)
        icon_path = os.path.join(os.path.dirname(__file__), Macast.ICON_MAP[item.data])
        template = None if item.data == 0 else True
        self.update_icon(icon_path, template)

    def on_menu_theme_change_click(self, item):
        """Macast-Glass: 切换菜单主题（跟随系统 / 深色 / 浅色）。"""
        modes = ["auto", "dark", "light"]
        mode = modes[item.data] if 0 <= getattr(item, "data", 0) < len(modes) else "auto"
        try:
            from .glass_menu import apply_theme_mode
            apply_theme_mode(mode)
        except Exception as e:
            logger.error(e)
        if self.menu_theme_menuitem is not None:
            for i in self.menu_theme_menuitem.items():
                i.checked = False
        item.checked = True
        self.update_menu()

    def _menu_for_tray(self):
        """Windows 托盘每次弹出前调用：重建设置菜单，
        保证播放器子菜单项和各项勾选都是最新的。"""
        try:
            if self.setting_menuitem is not None:
                self.setting_menuitem.children = self.build_setting_menu()
        except Exception as e:
            logger.error("rebuild setting menu failed: {}".format(e))
        return self.menu

    def quit(self, item):
        if Setting.is_service_running():
            self.stop_cast()
        super(Macast, self).quit(item)


def gui(renderer=None, protocol=None, lang=gettext.gettext):
    if renderer is None:
        renderer = MPVRenderer(lang, Setting.mpv_default_path)
    if protocol is None:
        protocol = DLNAProtocol()
    Macast(renderer, protocol, lang).start()


def cli(renderer=None, protocol=None):
    if renderer is None:
        renderer = MPVRenderer(path=Setting.mpv_default_path)
    if protocol is None:
        protocol = DLNAProtocol()
    Service(renderer, protocol).run()
