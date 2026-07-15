import gettext
import json
import locale
import logging
import os
from pprint import pformat

import pydbus
from gi.repository import Gio, GLib

from hidamari.commons import (
    AUTOSTART_DESKTOP_CONTENT,
    AUTOSTART_DESKTOP_CONTENT_FLATPAK,
    AUTOSTART_DESKTOP_PATH,
    AUTOSTART_DIR,
    CONFIG_DIR,
    CONFIG_KEY_DATA_SOURCE,
    CONFIG_KEY_MUTE_WHEN_MAXIMIZED,
    CONFIG_PATH,
    CONFIG_TEMPLATE,
    CONFIG_VERSION,
    LOGGER_NAME,
    MODE_VIDEO,
    TRANSLATION_DOMAIN,
    VIDEO_WALLPAPER_DIR,
)

logger = logging.getLogger(LOGGER_NAME)


def init_translations(localedir):
    """Bind the gettext text domain for the current process.

    The forkserver children (GUI, systray) don't inherit the launcher's gettext
    setup, so each entry point binds it itself. We bind at both the C-library
    level (for GtkBuilder/.ui strings) and the Python level (for _()); missing
    catalogs simply fall back to the original strings, so this can't crash.
    """
    try:
        locale.bindtextdomain(TRANSLATION_DOMAIN, localedir)
        locale.textdomain(TRANSLATION_DOMAIN)
    except (AttributeError, OSError) as e:
        logger.debug("[i18n] C locale bind skipped: %s", e)
    gettext.bindtextdomain(TRANSLATION_DOMAIN, localedir)
    gettext.textdomain(TRANSLATION_DOMAIN)


def is_gnome():
    """
    Check if current DE is GNOME or not.
    On Ubuntu 20.04, $XDG_CURRENT_DESKTOP = ubuntu:GNOME
    On Fedora 34, $XDG_CURRENT_DESKTOP = GNOME
    Hence we do the detection by looking for the word "gnome"
    """
    return "gnome" in str(os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()


def is_wayland():
    """
    Check if current session is Wayland or not.
    $XDG_SESSION_TYPE = x11 | wayland
    """
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def is_flatpak():
    """
    Check if Hidamari is a Flatpak
    Reference:
    https://gitlab.gnome.org/jrb/crosswords/-/blob/master/src/crosswords-init.c#L179
    """
    return os.path.isfile("/.flatpak-info")


def setup_autostart(autostart):
    if is_flatpak():
        """
        Use portal to autostart for Flatpak
        Documentation:
        https://libportal.org/method.Portal.request_background.html
        https://libportal.org/method.Portal.request_background_finish.html
        """

        import gi

        gi.require_version("Xdp", "1.0")
        from gi.repository import Xdp

        xdp = Xdp.Portal.new()

        # Request Autostart
        xdp.request_background(
            None,  # parent
            "Autostart Hidamari in background",  # reason
            ["hidamari", "-b"],  # commandline
            Xdp.BackgroundFlags.AUTOSTART if autostart else Xdp.BackgroundFlags.NONE,  # flags
            None,  # cancellable
            lambda portal, result, user_data: logger.debug(
                f"[Utils] autostart={autostart}, request_background sucess={portal.request_background_finish(result)}"
            ),  # callback
            None,  # user_data
        )

    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    logger.debug(f"[Utils] autostart={autostart}, path={AUTOSTART_DESKTOP_PATH}")
    if autostart:
        with open(AUTOSTART_DESKTOP_PATH, mode="w") as f:
            if is_flatpak():
                # Write files to the sandbox as well, for the following reasons:
                # (1) So that we know if autostart is enabled by looking the file in sandbox
                # (2) Acts as a fallback in case the portal doesn't work
                f.write(AUTOSTART_DESKTOP_CONTENT_FLATPAK)
            else:
                f.write(AUTOSTART_DESKTOP_CONTENT)
    else:
        if os.path.isfile(AUTOSTART_DESKTOP_PATH):
            os.remove(AUTOSTART_DESKTOP_PATH)


def get_video_paths():
    file_list = []
    for filename in os.listdir(VIDEO_WALLPAPER_DIR):
        filepath = os.path.join(VIDEO_WALLPAPER_DIR, filename)
        file = Gio.file_new_for_path(filepath)
        info = file.query_info("standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
        mime_type = info.get_content_type()
        if "video" in mime_type:
            file_list.append(filepath)
    return sorted(file_list)


"""
GNOME extension utils
"""


def gnome_extension_is_enabled(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    info: dict = gnome_ext.GetExtensionInfo(extension_name)
    return info["state"] == 1  # ENABLE = 1


def gnome_extension_set_enable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.EnableExtension(extension_name)
    return success


def gnome_extension_set_disable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.DisableExtension(extension_name)
    return success


def gnome_extension_is_installed(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    installed: dict = gnome_ext.ListExtensions()
    return extension_name in installed.keys()


def gnome_desktop_icon_workaround():
    """
    Workaround for GNOME desktop icon extensions not displaying the icons on top of Hidamari.
    Call this right after the wallpaper is shown.
    """
    if not is_gnome():
        return
    extension_list = [
        "ding@rastersoft.com",
        "desktopicons-neo@darkdemon",
        "gtk4-ding@smedius.gitlab.com",
        "zorin-desktop-icons@zorinos.com",
    ]
    for ext in extension_list:
        # Check if installed and enabled
        if gnome_extension_is_installed(ext) and gnome_extension_is_enabled(ext):
            # Reload the extension
            logger.info(f"[Utils] Apply workaround for {ext}")
            gnome_extension_set_disable(ext)
            gnome_extension_set_enable(ext)


"""
Handlers
"""


class ActiveHandler:
    """
    Handler for monitoring screen lock
    GNOME:
    https://gitlab.gnome.org/GNOME/gnome-shell/-/blob/main/data/dbus-interfaces/org.gnome.ScreenSaver.xml
    Cinamon:
    https://github.com/linuxmint/cinnamon-screensaver/blob/master/libcscreensaver/org.cinnamon.ScreenSaver.xml
    Freedesktop:
    https://github.com/KDE/kscreenlocker/blob/master/dbus/org.freedesktop.ScreenSaver.xml
    """

    def __init__(self, on_active_changed: callable):
        self.session_bus = pydbus.SessionBus()
        self.proxies = []
        self.signal_subscriptions = []

        screensaver_list = [
            "org.gnome.ScreenSaver",
            "org.cinnamon.ScreenSaver",
            "org.freedesktop.ScreenSaver",
        ]
        for s in screensaver_list:
            try:
                proxy = self.session_bus.get(s)
                # Store proxy reference to prevent garbage collection
                self.proxies.append(proxy)
                subscription = proxy.ActiveChanged.connect(on_active_changed)
                self.signal_subscriptions.append((proxy, subscription))
            except GLib.Error:
                pass

    def cleanup(self):
        """Cleanup signal subscriptions"""
        # pydbus has no disconnect; connections drop when the proxies are GC'd
        self.signal_subscriptions.clear()
        self.proxies.clear()


class EndSessionHandler:
    """
    Handler for monitoring end session
    References:
    https://github.com/backloop/gendsession

    PrepareForShutdown() signal from logind is not handled
    https://gitlab.gnome.org/GNOME/gnome-shell/-/issues/787
    """

    def __init__(self, on_end_session: callable):
        self.on_end_session = on_end_session

        if is_gnome():
            session_bus = pydbus.SessionBus()
            proxy = session_bus.get("org.gnome.SessionManager")
            client_id = proxy.RegisterClient("", "")
            self.session_client = session_bus.get("org.gnome.SessionManager", client_id)
            self.session_client.QueryEndSession.connect(self.__query_end_session_handler_gnome)
            self.session_client.EndSession.connect(self.__end_session_handler_gnome)
        else:
            system_bus = pydbus.SystemBus()
            proxy = system_bus.get(".login1")
            proxy.PrepareForShutdown.connect(self.__end_session_handler)

    def __end_session_response_gnome(self, ok=True):
        if ok:
            self.session_client.EndSessionResponse(True, "")
        else:
            self.session_client.EndSessionResponse(False, "Not ready")

    def __query_end_session_handler_gnome(self, flags):
        # Ignore flags, always agree on the QueryEndSesion
        self.__end_session_response_gnome(True)

    def __end_session_handler_gnome(self, flags):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()
        self.__end_session_response_gnome(True)

    def __end_session_handler(self, *_):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()


class WindowHandler:
    """
    Handler for monitoring maximized/fullscreen windows on X11 via EWMH.

    Implemented with libX11 (ctypes) instead of libwnck so it works inside
    GTK4 processes without pulling Gtk 3.0. Polls every 500ms.
    """

    _POLL_MS = 500

    def __init__(self, on_window_state_changed: callable):
        import ctypes
        from ctypes import POINTER, byref, c_char_p, c_int, c_ulong, c_void_p

        self.on_window_state_changed = on_window_state_changed
        self.prev_state = None
        self._source_id = None
        self._ctypes = ctypes
        self._byref = byref
        self._c_ulong = c_ulong
        self._c_int = c_int
        self._c_void_p = c_void_p
        self._c_char_p = c_char_p
        self._POINTER = POINTER

        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
        except OSError:
            try:
                self._x11 = ctypes.CDLL("libX11.so")
            except OSError as e:
                logger.error("[WindowHandler] libX11 unavailable: %s", e)
                self._x11 = None
                return

        x11 = self._x11
        x11.XOpenDisplay.restype = c_void_p
        x11.XOpenDisplay.argtypes = [c_char_p]
        x11.XDefaultRootWindow.restype = c_ulong
        x11.XDefaultRootWindow.argtypes = [c_void_p]
        x11.XInternAtom.restype = c_ulong
        x11.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        x11.XGetWindowProperty.restype = c_int
        x11.XGetWindowProperty.argtypes = [
            c_void_p,
            c_ulong,
            c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            c_int,
            c_ulong,
            POINTER(c_ulong),
            POINTER(c_int),
            POINTER(c_ulong),
            POINTER(c_ulong),
            POINTER(c_void_p),
        ]
        x11.XFree.argtypes = [c_void_p]
        x11.XCloseDisplay.argtypes = [c_void_p]

        self._display = x11.XOpenDisplay(None)
        if not self._display:
            logger.error("[WindowHandler] XOpenDisplay failed")
            self._x11 = None
            return

        self._root = x11.XDefaultRootWindow(self._display)
        self._XA_ATOM = 4
        self._AnyPropertyType = 0
        self._atom_client_list = x11.XInternAtom(self._display, b"_NET_CLIENT_LIST", False)
        self._atom_wm_state = x11.XInternAtom(self._display, b"_NET_WM_STATE", False)
        self._atom_max_horz = x11.XInternAtom(
            self._display, b"_NET_WM_STATE_MAXIMIZED_HORZ", False
        )
        self._atom_max_vert = x11.XInternAtom(
            self._display, b"_NET_WM_STATE_MAXIMIZED_VERT", False
        )
        self._atom_fullscreen = x11.XInternAtom(
            self._display, b"_NET_WM_STATE_FULLSCREEN", False
        )
        self._atom_hidden = x11.XInternAtom(self._display, b"_NET_WM_STATE_HIDDEN", False)
        self._atom_skip_taskbar = x11.XInternAtom(
            self._display, b"_NET_WM_STATE_SKIP_TASKBAR", False
        )
        self._atom_skip_pager = x11.XInternAtom(
            self._display, b"_NET_WM_STATE_SKIP_PAGER", False
        )

        self.eval()
        self._source_id = GLib.timeout_add(self._POLL_MS, self._poll)

    def _get_atom_list(self, window, atom):
        x11 = self._x11
        actual_type = self._c_ulong()
        actual_format = self._c_int()
        nitems = self._c_ulong()
        bytes_after = self._c_ulong()
        prop = self._c_void_p()
        status = x11.XGetWindowProperty(
            self._display,
            self._c_ulong(window),
            self._c_ulong(atom),
            0,
            1024,
            False,
            self._AnyPropertyType,
            self._byref(actual_type),
            self._byref(actual_format),
            self._byref(nitems),
            self._byref(bytes_after),
            self._byref(prop),
        )
        if status != 0 or not prop.value or nitems.value == 0:
            if prop.value:
                x11.XFree(prop)
            return []
        # 32-bit values on X11 property format
        arr_type = self._c_ulong * nitems.value
        values = list(arr_type.from_address(prop.value))
        x11.XFree(prop)
        return values

    def _poll(self):
        self.eval()
        return True  # keep the timeout alive

    def eval(self, *args):
        if self._x11 is None or not getattr(self, "_display", None):
            return

        # TODO: #28 (Wallpaper stops animating on other monitor when app maximized on other)
        is_any_maximized, is_any_fullscreen = False, False
        try:
            clients = self._get_atom_list(self._root, self._atom_client_list)
            for win in clients:
                states = set(self._get_atom_list(win, self._atom_wm_state))
                if self._atom_hidden in states:
                    continue
                # Skip desktop/dock-like surfaces
                if self._atom_skip_taskbar in states and self._atom_skip_pager in states:
                    continue
                if self._atom_fullscreen in states:
                    is_any_fullscreen = True
                if self._atom_max_horz in states and self._atom_max_vert in states:
                    is_any_maximized = True
                if is_any_maximized and is_any_fullscreen:
                    break
        except Exception as e:
            logger.warning("[WindowHandler] EWMH poll failed: %s", e)
            return

        cur_state = {"is_any_maximized": is_any_maximized, "is_any_fullscreen": is_any_fullscreen}
        if self.prev_state is None or self.prev_state != cur_state:
            self.prev_state = cur_state
            self.on_window_state_changed(cur_state)
            logger.debug(f"[WindowHandler] {cur_state}")

    def cleanup(self):
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None
        if getattr(self, "_display", None) and self._x11 is not None:
            try:
                self._x11.XCloseDisplay(self._display)
            except Exception as e:
                logger.warning("[WindowHandler] XCloseDisplay error: %s", e)
            self._display = None


class ConfigUtil:
    def generate_template(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        from hidamari.commons import refresh_config_template_monitors

        refresh_config_template_monitors()
        self.save(CONFIG_TEMPLATE)

    @staticmethod
    def _check(config: dict):
        """Check if the config is valid"""
        is_all_keys_match = all(key in config for key in CONFIG_TEMPLATE)
        is_version_match = config.get("version") == CONFIG_VERSION
        return is_all_keys_match and is_version_match

    def _invalid(self):
        logger.debug("[Config] Invalid. A new config will be generated.")
        self.generate_template()
        return CONFIG_TEMPLATE

    def _migrateV3To4(self, config: dict):
        logger.debug("[Config] Migration from version 3 to 4.")
        curr_data_source = config["data_source"]
        config["data_source"] = CONFIG_TEMPLATE[CONFIG_KEY_DATA_SOURCE]
        config["data_source"]["Default"] = curr_data_source
        config["is_pause_when_maximized"] = config["is_detect_maximized"]
        del config["is_detect_maximized"]
        config["is_mute_when_maximized"] = CONFIG_TEMPLATE[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]
        config["version"] = 4
        # save config file
        self.save(config)

    def _checkMissingMonitors(self, old_config: dict, template: dict):
        # Extract the monitors from both configurations
        old_monitors = old_config.get("data_source", {}).keys()
        template_monitors = template.get("data_source", {}).keys()
        # Find monitors in the template that are not in the old configuration
        missing_monitors = set(template_monitors) - set(old_monitors)
        if len(missing_monitors) > 0:
            logger.warning(
                f"[Config] There are missing {len(missing_monitors)} monitors in config. Creating default one"
            )
            self._createMissingMonitors(missing_monitors, old_config)

    def _createMissingMonitors(self, keys: set, config: dict):
        # we will set to Default new monitor sources
        for key in keys:
            config["data_source"][key] = config["data_source"]["Default"]
        self.save(config)

    def _checkDefaultSource(self, config: dict):
        # Check if the 'Default' source is empty
        default_source = config["data_source"].get("Default", "")
        mode = config.get("mode")
        if mode == MODE_VIDEO and not os.path.isfile(default_source):
            logger.warning(
                "[Config] Default source is empty or not a valid file. Setting to the first on available."
            )

            # Get all values from the 'data_source' dictionary
            values = list(config["data_source"].values())
            # If there are no values in 'data_source', return early
            if not values:
                return

            # Set the 'Default' source to the first value available
            for value in values:
                if len(value) > 0 and os.path.isfile(value):
                    config["data_source"]["Default"] = value
                    self.save(config)
                    break

    def load(self):
        from hidamari.commons import refresh_config_template_monitors

        # Ensure template monitor keys match the current layout (Gdk 4).
        refresh_config_template_monitors()
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                json_str = f.read()
                try:
                    config = json.loads(json_str)
                    # migration to version 4 for data_source type change
                    if config.get("version") <= 3 and CONFIG_VERSION >= 4:
                        self._migrateV3To4(config)
                    self._checkDefaultSource(config)
                    self._checkMissingMonitors(config, CONFIG_TEMPLATE)
                    if self._check(config):
                        logs = []
                        logs.append("--------- Config ---------")
                        logs.append(pformat(config, indent=3))
                        logs.append("--------------------------")
                        logs_str = "\n".join(logs)
                        logger.debug(f"[Config] Loaded {CONFIG_PATH}\n{logs_str}")
                        return config
                except json.decoder.JSONDecodeError:
                    logger.debug("[Config] JSONDecodeError")
        return self._invalid()

    def save(self, config):
        old_config = None
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                json_str = f.read()
                try:
                    old_config = json.loads(json_str)
                    if not self._check(old_config):
                        old_config = None
                except json.decoder.JSONDecodeError:
                    old_config = None
        # Skip if the config is identical
        if old_config == config:
            return
        with open(CONFIG_PATH, "w") as f:
            json_str = json.dumps(config, indent=3)
            print(json_str, file=f)
            logs = []
            logs.append("--------- Config ---------")
            logs.append(pformat(config, indent=3))
            logs.append("--------------------------")
            logs_str = "\n".join(logs)
            logger.debug(f"[Config] Saved {CONFIG_PATH}\n{logs_str}")
