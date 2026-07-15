import logging
import multiprocessing as mp
import sys
from abc import abstractmethod

import gi
import setproctitle

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk
from pydbus import SessionBus

from hidamari.commons import DBUS_NAME_PLAYER, LOGGER_NAME, PROJECT
from hidamari.utils import gnome_desktop_icon_workaround

logger = logging.getLogger(LOGGER_NAME)

APP_ID = f"{PROJECT}.player"


class DummyWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class BasePlayer(Gtk.Application):
    """
    <node>
    <interface name='io.github.jeffshee.hidamari.player'>
        <property name="mode" type="s" access="read"/>
        <property name="data_source" type="s" access="readwrite"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <method name='pause_playback'/>
        <method name='start_playback'/>
        <method name='quit_player'/>
    </interface>
    </node>
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            **kwargs,
        )
        setproctitle.setproctitle(mp.current_process().name)
        self.windows = dict()
        self._monitor_signal_ids = []
        self._monitor_detect()

    def _monitor_detect(self):
        display = Gdk.Display.get_default()
        monitors = display.get_monitors()

        for i in range(monitors.get_n_items()):
            monitor = monitors.get_item(i)
            if monitor not in self.windows:
                self.windows[monitor] = None
            handler_id = monitor.connect("notify::geometry", self._on_geometry_changed)
            self._monitor_signal_ids.append((monitor, handler_id))

        monitors.connect("items-changed", self._on_monitors_changed)

    def new_window(self, gdk_monitor):
        # Override here for different window
        # NOTE: Don't forget to set the application=self, otherwise the application will quit immediately lol
        return DummyWindow(application=self)

    def _on_geometry_changed(self, monitor, *_args):
        logger.info("[Player] geometry-changed")
        window = self.windows.get(monitor)
        if window is None:
            return
        rect = monitor.get_geometry()
        if hasattr(window, "resize_to"):
            # Pure X11 surfaces take absolute position + size.
            try:
                window.resize_to(rect.width, rect.height, rect.x, rect.y)
            except TypeError:
                window.resize_to(rect.width, rect.height)
        elif hasattr(window, "width"):
            window.width = rect.width
            window.height = rect.height

    def _on_monitors_changed(self, model, position, removed, added):
        logger.info("[Player] monitors-changed (pos=%s removed=%s added=%s)", position, removed, added)
        # Rebuild the monitor map from the live list model.
        current = set()
        for i in range(model.get_n_items()):
            monitor = model.get_item(i)
            current.add(monitor)
            if monitor not in self.windows:
                self.windows[monitor] = None
                handler_id = monitor.connect("notify::geometry", self._on_geometry_changed)
                self._monitor_signal_ids.append((monitor, handler_id))

        for monitor in list(self.windows.keys()):
            if monitor not in current:
                window = self.windows.pop(monitor)
                if window is not None:
                    window.destroy()

        self.do_activate()

    def do_startup(self):
        Gtk.Application.do_startup(self)

    def do_activate(self):
        for monitor in list(self.windows.keys()):
            if not self.windows[monitor]:
                window = self.new_window(monitor)
                self.windows[monitor] = window
            win = self.windows[monitor]
            if hasattr(win, "present"):
                win.present()
            elif hasattr(win, "show"):
                win.show()
        # Workaround for DING extension
        gnome_desktop_icon_workaround()

    @property
    @abstractmethod
    def mode(self):
        pass

    @property
    @abstractmethod
    def data_source(self):
        pass

    @data_source.setter
    def data_source(self, data_source):
        pass

    @property
    @abstractmethod
    def volume(self):
        pass

    @volume.setter
    def volume(self, volume):
        pass

    @property
    @abstractmethod
    def is_mute(self):
        pass

    @is_mute.setter
    def is_mute(self, is_mute):
        pass

    @property
    @abstractmethod
    def is_playing(self):
        pass

    @abstractmethod
    def pause_playback(self):
        pass

    @abstractmethod
    def start_playback(self):
        pass

    def quit_player(self):
        self.quit()


def main():
    bus = SessionBus()
    app = BasePlayer()
    try:
        bus.publish(DBUS_NAME_PLAYER, app)
    except RuntimeError as e:
        logger.error(e)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
