import logging
import multiprocessing as mp
import threading
from gettext import gettext as _

import setproctitle
from gi.repository import GLib
from pydbus import SessionBus

from hidamari.commons import DBUS_NAME_SERVER, LOGGER_NAME, MODE_VIDEO, MODE_WEBPAGE, PROJECT
from hidamari.utils import init_translations

logger = logging.getLogger(LOGGER_NAME)

APP_INDICATOR_ID = PROJECT
APP_INDICATOR_ICON = "io.github.jeffshee.Hidamari"

# Reuse SessionBus instance to avoid creating multiple connections
_session_bus = None


def get_session_bus():
    """Get or create a singleton SessionBus instance"""
    global _session_bus
    if _session_bus is None:
        _session_bus = SessionBus()
    return _session_bus


def connect():
    # Connect to server using singleton SessionBus
    bus = get_session_bus()
    try:
        server = bus.get(DBUS_NAME_SERVER)
        return server
    except GLib.Error:
        logger.error("[Menu] Couldn't connect to server")
    return None


def on_item_show():
    server = connect()
    if server:
        server.show_gui()


def on_item_mute():
    server = connect()
    if server:
        prev_state = server.is_mute
        server.is_mute = not prev_state


def on_item_pause():
    server = connect()
    if server:
        prev_state = server.is_paused_by_user
        server.is_paused_by_user = not prev_state
        if not prev_state:
            server.pause_playback()
        else:
            server.start_playback()


def on_item_reload():
    server = connect()
    if server:
        server.reload()


def on_item_lucky():
    server = connect()
    if server:
        server.feeling_lucky()


def on_item_quit():
    server = connect()
    if server:
        server.quit()


def start_action(f: callable):
    """Use this function to execute callback (for not blocking the UI)"""
    t = threading.Thread(target=f)
    t.start()


def _menu_entries(mode):
    entries = [
        (_("Show Hidamari"), on_item_show),
        (_("Toggle Mute Audio"), on_item_mute),
    ]
    if mode != MODE_WEBPAGE:
        entries.append((_("Toggle Play/Pause"), on_item_pause))
    entries.extend(
        [
            (_("Reload"), on_item_reload),
            (_("I'm Feeling Lucky"), on_item_lucky),
            (_("Quit Hidamari"), on_item_quit),
        ]
    )
    return entries


def build_menu(mode, parent):
    """Build a GTK4 PopoverMenu for wallpaper windows (GTK4 process)."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    menu = Gio.Menu()
    action_group = Gio.SimpleActionGroup()

    for idx, (label, handler) in enumerate(_menu_entries(mode)):
        action_name = f"item{idx}"
        menu.append(label, f"ctx.{action_name}")
        action = Gio.SimpleAction.new(action_name, None)
        action.connect("activate", lambda *_a, h=handler: start_action(h))
        action_group.add_action(action)

    parent.insert_action_group("ctx", action_group)
    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.set_parent(parent)
    popover.set_has_arrow(False)
    # Keep a reference so the menu isn't GC'd.
    parent._hidamari_context_menu = popover
    return popover


def popup_menu_at(popover, widget, x, y):
    """Position and show a PopoverMenu at widget-local coordinates."""
    import gi

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = 1
    rect.height = 1
    popover.set_pointing_to(rect)
    popover.popup()


def show_systray_icon(mode, localedir="/usr/share/locale"):
    """System tray lives in its own process and still uses GTK3 + AppIndicator.

    AppIndicator menus are Gtk.Menu (GTK3). Importing GTK3 here keeps the tray
    isolated from the GTK4/libadwaita GUI and player processes.
    """
    setproctitle.setproctitle(mp.current_process().name)
    init_translations(localedir)

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import Gtk

    menu = Gtk.Menu()
    for label, handler in _menu_entries(mode):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_a, h=handler: start_action(h))
        menu.append(item)
    menu.show_all()

    indicator = AppIndicator.Indicator.new(
        id=APP_INDICATOR_ID,
        icon_name=APP_INDICATOR_ICON,
        category=AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
    )
    indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    indicator.set_menu(menu)
    logger.info("[Systray] Ready")
    Gtk.main()


if __name__ == "__main__":
    show_systray_icon(MODE_VIDEO)
