import logging
import multiprocessing as mp
import os
import subprocess
import sys
import threading
from gettext import gettext as _

import gi
import requests
import setproctitle
import yt_dlp

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango
from pydbus import SessionBus

from hidamari.commons import (
    AUTOSTART_DESKTOP_PATH,
    CONFIG_KEY_BLUR_RADIUS,
    CONFIG_KEY_DATA_SOURCE,
    CONFIG_KEY_FIRST_TIME,
    CONFIG_KEY_MODE,
    CONFIG_KEY_MUTE,
    CONFIG_KEY_MUTE_WHEN_MAXIMIZED,
    CONFIG_KEY_PAUSE_WHEN_MAXIMIZED,
    CONFIG_KEY_STATIC_WALLPAPER,
    CONFIG_KEY_VOLUME,
    CONFIG_PATH,
    DBUS_NAME_SERVER,
    LOGGER_NAME,
    MODE_STREAM,
    MODE_VIDEO,
    MODE_WEBPAGE,
    PROJECT,
    TRANSLATION_DOMAIN,
    VIDEO_WALLPAPER_DIR,
)
from hidamari.gui.gui_utils import (
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    apply_thumbnail_async,
    debounce,
)
from hidamari.monitor import Monitors
from hidamari.utils import (
    ConfigUtil,
    get_video_paths,
    init_translations,
    is_gnome,
    is_wayland,
    setup_autostart,
)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(LOGGER_NAME)

APP_ID = f"{PROJECT}.gui"
APP_UI_RESOURCE_PATH = "/io/jeffshee/Hidamari/control.ui"


class ControlPanel(Adw.Application):
    def __init__(self, version, *args, **kwargs):
        super().__init__(
            *args,
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            **kwargs,
        )
        setproctitle.setproctitle(mp.current_process().name)

        # Follow the system light/dark preference (libadwaita).
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)

        self.builder = Gtk.Builder()
        self.builder.set_translation_domain(TRANSLATION_DOMAIN)
        self.builder.add_from_resource(APP_UI_RESOURCE_PATH)

        self.version = version
        self.window = None
        self.server = None
        self.video_flow = None
        self.video_paths = []
        self.selected_video_path = None
        self.all_key = "all"
        self.selected_html_file = None
        self.context_menu = None

        self.is_autostart = os.path.isfile(AUTOSTART_DESKTOP_PATH)

        self._connect_server()
        self._load_config()
        self._connect_ui_signals()

        self.monitors = Monitors()
        video_paths = self.config[CONFIG_KEY_DATA_SOURCE]
        for monitor in self.monitors.get_monitors():
            if monitor in video_paths:
                self.monitors.get_monitor(monitor).set_wallpaper(video_paths[monitor])
            else:
                self.monitors.get_monitor(monitor).set_wallpaper(video_paths["Default"])

    def _connect_ui_signals(self):
        # GTK4 Builder no longer supports connect_signals(); wire handlers here.
        self.builder.get_object("AdjustmentVolume").connect(
            "value-changed", self.on_volume_changed
        )
        self.builder.get_object("AdjustmentBlur").connect(
            "value-changed", self.on_blur_radius_changed
        )
        streaming = self.builder.get_object("StreamingEntry")
        streaming.connect("activate", self.on_streaming_activate)
        streaming.connect("icon-press", self.on_streaming_activate)
        webpage = self.builder.get_object("WebPageEntry")
        webpage.connect("activate", self.on_web_page_activate)
        webpage.connect("icon-press", self.on_web_page_activate)
        self.builder.get_object("FileChooser").connect("clicked", self.on_choose_html_file)

    def _connect_server(self):
        try:
            self.server = SessionBus().get(DBUS_NAME_SERVER)
        except GLib.Error:
            logger.error("[GUI] Couldn't connect to server")

    def _load_config(self):
        self.config = ConfigUtil().load()

    def _save_config(self):
        ConfigUtil().save(self.config)

    @debounce(1)
    def _save_config_delay(self):
        self._save_config()

    def do_startup(self):
        Adw.Application.do_startup(self)

        actions = [
            (
                "local_video_dir",
                lambda *_: subprocess.run(["xdg-open", os.path.realpath(VIDEO_WALLPAPER_DIR)]),
            ),
            ("local_video_refresh", self._reload_icon_view),
            ("local_video_apply", self.on_local_video_apply),
            ("local_web_page_apply", self.on_local_web_page_apply),
            ("play_pause", self.on_play_pause),
            ("feeling_lucky", self.on_feeling_lucky),
            (
                "config",
                lambda *_: subprocess.run(["xdg-open", os.path.realpath(CONFIG_PATH)]),
            ),
            ("about", self.on_about),
            ("quit", self.on_quit),
        ]

        for action_name, handler in actions:
            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", handler)
            self.add_action(action)

        statefuls = [
            ("mute", self.config[CONFIG_KEY_MUTE], self.on_mute),
            ("autostart", self.is_autostart, self.on_autostart),
            (
                "static_wallpaper",
                self.config[CONFIG_KEY_STATIC_WALLPAPER],
                self.on_static_wallpaper,
            ),
            (
                "pause_when_maximized",
                self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED],
                self.on_pause_when_maximized,
            ),
            (
                "mute_when_maximized",
                self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED],
                self.on_mute_when_maximized,
            ),
        ]

        for action_name, state, handler in statefuls:
            action = Gio.SimpleAction.new_stateful(
                action_name, None, GLib.Variant.new_boolean(state)
            )
            action.connect("change-state", handler)
            self.add_action(action)

        if is_wayland():
            self.builder.get_object("TogglePauseWhenMaximized").set_visible(False)
            self.builder.get_object("ToggleMuteWhenMaximized").set_visible(False)

        if not is_gnome():
            self.builder.get_object("ToggleStaticWallpaper").set_visible(False)
            self.builder.get_object("LabelBlurRadius").set_visible(False)
            self.builder.get_object("SpinBlurRadius").set_visible(False)

        self._reload_all_widgets()

    def do_activate(self):
        if self.window is None:
            self.window = self.builder.get_object("ApplicationWindow")
            self.window.set_application(self)
            self.window.set_title("Hidamari")
            self._setup_video_flow()
        self.window.present()

        if self.server is None:
            self._show_error(_("Couldn't connect to server"))

        if self.config[CONFIG_KEY_FIRST_TIME]:
            self._show_welcome()
            self.config[CONFIG_KEY_FIRST_TIME] = False
            self._save_config()

    def _setup_video_flow(self):
        self.video_flow = self.builder.get_object("VideoFlowBox")
        self.video_flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.video_flow.connect("child-activated", self.on_video_child_activated)
        self.video_flow.connect("selected-children-changed", self.on_video_selection_changed)

    def _show_alert(self, heading, body, response_label=None):
        if response_label is None:
            response_label = _("OK")
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response("ok", response_label)
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self.window)

    def _show_welcome(self):
        self._show_alert(
            _("Welcome to Hidamari 🤗"),
            _(
                "Quickstart for adding local videos:\n"
                " ・Click the folder icon to open the Hidamari folder\n"
                " ・Put your videos there\n"
                " ・Click the refresh button"
            ),
        )

    def _show_error(self, error):
        self._show_alert(_("Oops!"), str(error))

    def _ensure_set_monitor_action(self):
        if self.lookup_action("set_monitor") is None:
            action = Gio.SimpleAction.new("set_monitor", GLib.VariantType.new("s"))
            action.connect("activate", self.on_set_monitor_action)
            self.add_action(action)

    def _build_monitor_menu(self, parent):
        menu = Gio.Menu()
        for monitor_name in self.monitors.get_monitors():
            menu.append(
                _("Set For {monitor}").format(monitor=monitor_name),
                f"app.set_monitor::{monitor_name}",
            )
        menu.append(_("Set For All"), f"app.set_monitor::{self.all_key}")

        self._ensure_set_monitor_action()
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(parent)
        popover.set_has_arrow(True)
        popover.set_autohide(True)
        return popover

    def _popup_monitor_menu(self, parent, x=None, y=None):
        if self.context_menu is not None:
            self.context_menu.popdown()
            self.context_menu.unparent()
            self.context_menu = None

        self.context_menu = self._build_monitor_menu(parent)
        if x is not None and y is not None:
            rect = Gdk.Rectangle()
            rect.x = int(x)
            rect.y = int(y)
            rect.width = 1
            rect.height = 1
            self.context_menu.set_pointing_to(rect)
        self.context_menu.popup()

    def on_local_video_apply(self, *_args):
        if not self.selected_video_path:
            self._show_alert(
                _("No Video Selected"),
                _("There are no video selected.\nPlease choose one first."),
            )
            return
        # Anchor the monitor chooser to the Apply button (not the view center).
        apply_btn = self.builder.get_object("ButtonApply")
        self._popup_monitor_menu(apply_btn)

    def on_set_monitor_action(self, _action, param):
        key = param.get_string()
        video_path = self.selected_video_path
        if not video_path:
            return
        logger.info(f"[GUI] Local Video Set To {video_path} For Monitor {key}")
        self.config[CONFIG_KEY_MODE] = MODE_VIDEO
        paths = self.config[CONFIG_KEY_DATA_SOURCE]

        if key == self.all_key:
            for name, monitor in self.monitors.get_monitors().items():
                paths[name] = video_path
                monitor.set_wallpaper(video_path)
            target_monitor = self.all_key
        else:
            paths[key] = video_path
            self.monitors.get_monitor(key).set_wallpaper(video_path)
            target_monitor = key

        paths["Default"] = video_path
        self.config[CONFIG_KEY_DATA_SOURCE] = paths
        self._save_config()
        if self.server is not None:
            # Server expects a concrete monitor name; use first monitor for "all".
            if target_monitor == self.all_key:
                names = list(self.monitors.get_monitors().keys())
                target_monitor = names[0] if names else "Default"
            # Restarting the player is slow; never block the GTK main loop.
            self._call_server_async("video", video_path, target_monitor)

    def _call_server_async(self, method_name, *args):
        """Invoke a server D-Bus method off the UI thread so Apply stays responsive."""
        server = self.server
        if server is None:
            return

        def worker():
            try:
                getattr(server, method_name)(*args)
            except Exception as exc:
                logger.error("[GUI] server.%s failed: %s", method_name, exc)
                err_text = str(exc)

                def notify():
                    self._show_error(
                        _("Failed to apply wallpaper:\n{error}").format(error=err_text)
                    )
                    return False

                GLib.idle_add(notify)

        threading.Thread(target=worker, daemon=True).start()

    def on_choose_html_file(self, *_args):
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Choose HTML file"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(self.builder.get_object("filefilter1"))
        dialog.set_filters(filters)
        dialog.set_default_filter(self.builder.get_object("filefilter1"))
        dialog.open(self.window, None, self._on_html_file_chosen)

    def _on_html_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is None:
            return
        self.selected_html_file = file
        button = self.builder.get_object("FileChooser")
        button.set_label(file.get_basename() or _("Choose HTML file…"))

    def on_local_web_page_apply(self, *_args):
        if self.selected_html_file is None:
            self._show_error(_("Please choose a HTML file"))
            return
        file_path = self.selected_html_file.get_path()
        logger.info(f"[GUI] Local Webpage: {file_path}")
        self.config[CONFIG_KEY_MODE] = MODE_WEBPAGE
        self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = file_path
        self._save_config()
        if self.server is not None:
            self._call_server_async("webpage", file_path)

    def on_play_pause(self, *_):
        if self.server is None:
            return
        prev_state = self.server.is_paused_by_user
        self.server.is_paused_by_user = not prev_state
        if not prev_state:
            self.server.pause_playback()
        else:
            self.server.start_playback()

    def on_feeling_lucky(self, *_):
        if self.server is not None:
            self.server.feeling_lucky()

    def set_mute_toggle_icon(self):
        toggle_icon = self.builder.get_object("ToggleMuteIcon")
        volume, is_mute = self.config[CONFIG_KEY_VOLUME], self.config[CONFIG_KEY_MUTE]
        if volume == 0 or is_mute:
            icon_name = "audio-volume-muted-symbolic"
        elif volume < 30:
            icon_name = "audio-volume-low-symbolic"
        elif volume < 60:
            icon_name = "audio-volume-medium-symbolic"
        else:
            icon_name = "audio-volume-high-symbolic"
        toggle_icon.set_from_icon_name(icon_name)

    def set_scale_volume_sensitive(self):
        scale = self.builder.get_object("ScaleVolume")
        scale.set_sensitive(not self.config[CONFIG_KEY_MUTE])

    def set_spin_blur_radius_sensitive(self):
        spin = self.builder.get_object("SpinBlurRadius")
        spin.set_sensitive(self.config[CONFIG_KEY_STATIC_WALLPAPER])

    def on_volume_changed(self, adjustment):
        self.config[CONFIG_KEY_VOLUME] = int(adjustment.get_value())
        logger.info(f"[GUI] Volume: {self.config[CONFIG_KEY_VOLUME]}")
        self._save_config_delay()
        if self.server is not None:
            self.server.volume = self.config[CONFIG_KEY_VOLUME]
        self.set_mute_toggle_icon()

    def on_blur_radius_changed(self, adjustment):
        self.config[CONFIG_KEY_BLUR_RADIUS] = int(adjustment.get_value())
        logger.info(f"[GUI] Blur radius: {self.config[CONFIG_KEY_BLUR_RADIUS]}")
        self._save_config_delay()
        if self.server is not None:
            self.server.blur_radius = self.config[CONFIG_KEY_BLUR_RADIUS]

    def on_mute(self, action, state):
        action.set_state(state)
        self.config[CONFIG_KEY_MUTE] = bool(state)
        logger.info(f"[GUI] {action.get_name()}: {state}")
        self._save_config()
        if self.server is not None:
            self.server.is_mute = self.config[CONFIG_KEY_MUTE]
        self.set_mute_toggle_icon()
        self.set_scale_volume_sensitive()

    def on_autostart(self, action, state):
        action.set_state(state)
        self.is_autostart = bool(state)
        logger.info(f"[GUI] {action.get_name()}: {state}")
        setup_autostart(state)

    def on_static_wallpaper(self, action, state):
        action.set_state(state)
        self.config[CONFIG_KEY_STATIC_WALLPAPER] = bool(state)
        logger.info(f"[GUI] {action.get_name()}: {state}")
        self._save_config()
        if self.server is not None:
            self.server.is_static_wallpaper = self.config[CONFIG_KEY_STATIC_WALLPAPER]
        self.set_spin_blur_radius_sensitive()

    def on_pause_when_maximized(self, action, state):
        action.set_state(state)
        self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED] = bool(state)
        logger.info(f"[GUI] {action.get_name()}: {state}")
        self._save_config()
        if self.server is not None:
            self.server.is_pause_when_maximized = self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED]

    def on_mute_when_maximized(self, action, state):
        action.set_state(state)
        self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED] = bool(state)
        logger.info(f"[GUI] {action.get_name()}: {state}")
        self._save_config()
        if self.server is not None:
            self.server.is_mute_when_maximized = self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]

    def on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Hidamari",
            application_icon="io.github.jeffshee.Hidamari",
            developer_name="Jeff Shee",
            version=self.version,
            developers=[
                "Jeff Shee https://github.com/jeffshee",
                "All Contributors https://github.com/jeffshee/hidamari/graphs/contributors",
            ],
            artists=["Freepik https://www.freepik.com"],
            copyright="Copyright © 2022 Jeff Shee",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/jeffshee/hidamari",
            issue_url="https://github.com/jeffshee/hidamari/issues",
            comments=_(
                "Video wallpaper for Linux. Written in Python. 🐍\n\n"
                "Hidamari 日溜まり 【ひだまり】 (n) sunny spot; exposure to the sun"
            ),
        )
        about.present(self.window)

    def _check_url(self, url):
        try:
            response = requests.get(url)
        except requests.exceptions.RequestException as e:
            logger.error(f"[GUI] Failed to access {url}. Error:\n{e}")
            self._show_error(_("Failed to access {url}. Error:\n{error}").format(url=url, error=e))
            return False
        if response.status_code >= 400:
            logger.error(f"[GUI] Failed to access {url}. Error code: {response.status_code}")
            self._show_error(
                _("Failed to access {url}. Error code: {code}").format(
                    url=url, code=response.status_code
                )
            )
            return False
        return True

    def _check_yt_dlp(self, raw_url):
        try:
            with yt_dlp.YoutubeDL({"noplaylist": True}) as ydl:
                ydl.extract_info(raw_url, download=False)
        except yt_dlp.utils.DownloadError as e:
            s = " ".join(str(e).split(" ")[1:])
            logger.error(f"[GUI] Failed to stream {raw_url}. Error:\n{s}")
            self._show_error(
                _("Failed to stream {url}. Error:\n{error}").format(url=raw_url, error=s)
            )
            return False
        return True

    def on_streaming_activate(self, entry: Gtk.Entry, *_):
        url = entry.get_text()
        if not self._check_yt_dlp(url):
            return
        logger.info(f"[GUI] Streaming: {url}")
        self.config[CONFIG_KEY_MODE] = MODE_STREAM
        self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = url
        self._save_config()
        if self.server is not None:
            self._call_server_async("stream", url)

    def on_web_page_activate(self, entry: Gtk.Entry, *_):
        url = entry.get_text()
        if not self._check_url(url):
            return
        logger.info(f"[GUI] Webpage: {url}")
        self.config[CONFIG_KEY_MODE] = MODE_WEBPAGE
        self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = url
        self._save_config()
        if self.server is not None:
            self._call_server_async("webpage", url)

    def on_video_selection_changed(self, flowbox):
        selected = flowbox.get_selected_children()
        if not selected:
            self.selected_video_path = None
            return
        child = selected[0]
        self.selected_video_path = getattr(child, "video_path", None)

    def on_video_child_activated(self, _flowbox, child):
        # Double-click / activate: select and open monitor menu near the card.
        self.video_flow.select_child(child)
        self.selected_video_path = getattr(child, "video_path", None)
        self._popup_monitor_menu(child)

    def on_video_card_right_click(self, gesture, _n_press, x, y, child):
        self.video_flow.select_child(child)
        self.selected_video_path = getattr(child, "video_path", None)
        self._popup_monitor_menu(child, x, y)

    def on_quit(self, *_):
        if self.server is not None:
            try:
                self.server.quit()
            except GLib.Error:
                pass
        self.quit()

    def _reload_all_widgets(self):
        self._reload_icon_view()
        self.set_mute_toggle_icon()
        self.set_scale_volume_sensitive()
        self.set_spin_blur_radius_sensitive()

        mute_action = self.lookup_action("mute")
        if mute_action is not None:
            mute_action.set_state(GLib.Variant.new_boolean(self.config[CONFIG_KEY_MUTE]))

        autostart_action = self.lookup_action("autostart")
        if autostart_action is not None:
            autostart_action.set_state(GLib.Variant.new_boolean(self.is_autostart))

        scale_volume = self.builder.get_object("ScaleVolume")
        adjustment_volume = self.builder.get_object("AdjustmentVolume")
        adjustment_volume.handler_block_by_func(self.on_volume_changed)
        scale_volume.set_value(self.config[CONFIG_KEY_VOLUME])
        adjustment_volume.handler_unblock_by_func(self.on_volume_changed)

        spin_blur_radius = self.builder.get_object("SpinBlurRadius")
        adjustment_blur = self.builder.get_object("AdjustmentBlur")
        adjustment_blur.handler_block_by_func(self.on_blur_radius_changed)
        spin_blur_radius.set_value(self.config[CONFIG_KEY_BLUR_RADIUS])
        adjustment_blur.handler_unblock_by_func(self.on_blur_radius_changed)

    def _make_video_card(self, video_path):
        """Build a FlowBox child: fixed-size thumbnail + filename label."""
        picture = Gtk.Picture()
        picture.set_size_request(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(False)
        # Placeholder while the real frame loads.
        picture.set_paintable(
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).lookup_icon(
                "video-x-generic-symbolic",
                None,
                64,
                1,
                Gtk.TextDirection.NONE,
                Gtk.IconLookupFlags.PRELOAD,
            )
        )
        apply_thumbnail_async(video_path, picture)

        label = Gtk.Label(
            label=os.path.basename(video_path),
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.CENTER,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=28,
            xalign=0.5,
        )
        label.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(picture)
        box.append(label)

        # FlowBoxChild holds the card and selection highlight.
        child = Gtk.FlowBoxChild()
        child.set_child(box)
        child.video_path = video_path  # noqa: simple attr stash
        child.set_size_request(THUMBNAIL_WIDTH + 24, THUMBNAIL_HEIGHT + 56)

        gesture = Gtk.GestureClick.new()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self.on_video_card_right_click, child)
        child.add_controller(gesture)

        return child

    def _reload_icon_view(self, *_):
        # Keep the action name; now reloads the FlowBox card grid.
        if self.video_flow is None:
            self.video_flow = self.builder.get_object("VideoFlowBox")

        # Clear previous children.
        while True:
            child = self.video_flow.get_child_at_index(0)
            if child is None:
                break
            self.video_flow.remove(child)

        self.video_paths = get_video_paths()
        self.selected_video_path = None
        for video_path in self.video_paths:
            self.video_flow.append(self._make_video_card(video_path))


def _find_gresource(pkgdatadir):
    """Locate hidamari.gresource: the launcher-provided prefix first, then the
    standard XDG data dirs (so `python -m hidamari` works for any install)."""
    candidates = [os.path.join(pkgdatadir, "hidamari.gresource")]
    candidates += [
        os.path.join(d, "hidamari", "hidamari.gresource")
        for d in (GLib.get_user_data_dir(), *GLib.get_system_data_dirs())
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def main(version="devel", pkgdatadir="/usr/share/hidamari", localedir="/usr/share/locale"):
    init_translations(localedir)
    gresource = _find_gresource(pkgdatadir)
    if gresource is None:
        logger.error("[GUI] Couldn't find hidamari.gresource. Is Hidamari installed?")
        return
    Gio.Resource.load(gresource)._register()
    display = Gdk.Display.get_default()
    Gtk.IconTheme.get_for_display(display).add_resource_path("/io/jeffshee/Hidamari/icons")

    app = ControlPanel(version)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
