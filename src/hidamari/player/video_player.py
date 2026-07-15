import ctypes
import glob
import logging
import os
import pathlib
import random
import subprocess
import sys
import time
from threading import Timer

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
import vlc
from gi.repository import Gdk, Gio, GLib, Gtk
from PIL import Image, ImageFilter
from pydbus import SessionBus

from hidamari.commons import (
    CONFIG_DIR,
    CONFIG_KEY_DATA_SOURCE,
    CONFIG_KEY_FADE_DURATION_SEC,
    CONFIG_KEY_FADE_INTERVAL,
    CONFIG_KEY_MODE,
    CONFIG_KEY_MUTE,
    CONFIG_KEY_MUTE_WHEN_MAXIMIZED,
    CONFIG_KEY_PAUSE_WHEN_MAXIMIZED,
    CONFIG_KEY_STATIC_WALLPAPER,
    CONFIG_KEY_VOLUME,
    DBUS_NAME_PLAYER,
    LOGGER_NAME,
    MODE_STREAM,
    MODE_VIDEO,
)
from hidamari.player.base_player import BasePlayer
from hidamari.player.x11_surface import X11DesktopSurface
from hidamari.player.x11_window import is_primary_monitor
from hidamari.utils import (
    ActiveHandler,
    ConfigUtil,
    is_flatpak,
    is_gnome,
    is_wayland,
)
from hidamari.yt_utils import get_best_audio, get_formats, get_optimal_video

logger = logging.getLogger(LOGGER_NAME)

if is_wayland():
    # TODO: Window event monitoring for GNOME Wayland is broken
    class WindowHandler:
        def __init__(self, _: callable):
            pass

        def cleanup(self):
            pass
else:
    from hidamari.utils import WindowHandler


class Fade:
    def __init__(self):
        self.timer = None
        self.is_active = False

    def start(
        self,
        cur,
        target,
        step,
        fade_interval,
        update_callback: callable = None,
        complete_callback: callable = None,
    ):
        # Cancel any existing timer first
        self.cancel()
        self.is_active = True
        self._fade_step(cur, target, step, fade_interval, update_callback, complete_callback)

    def _fade_step(self, cur, target, step, fade_interval, update_callback, complete_callback):
        if not self.is_active:
            return

        new_cur = cur + step
        if (step < 0 and new_cur <= target) or (step > 0 and new_cur >= target):
            new_cur = target
            if update_callback:
                update_callback(int(new_cur))
            if complete_callback:
                complete_callback()
            self.is_active = False
        else:
            if update_callback:
                update_callback(int(new_cur))
            self.timer = Timer(
                fade_interval,
                self._fade_step,
                args=[new_cur, target, step, fade_interval, update_callback, complete_callback],
            )
            self.timer.daemon = True  # Make timer daemon to prevent blocking shutdown
            self.timer.start()

    def cancel(self):
        self.is_active = False
        if self.timer:
            self.timer.cancel()
            self.timer = None


class PlayerWindow:
    """
    Wallpaper surface: pure X11 desktop window (depth 24) + libVLC embed.

    Not a Gtk window — GTK4's 32-bit ARGB surfaces cause white/glitched video
    with xcb_x11 under GNOME XWayland. The Gtk.Application still owns the
    process main loop via BasePlayer.hold().
    """

    def __init__(self, name, x, y, width, height):
        self.width = width
        self.height = height
        self.name = name
        self.surface = X11DesktopSurface(name, x, y, width, height)
        self.fade = Fade()

        vlc_options = [
            "--no-disable-screensaver",
            "--aout=pulse",
            "--no-video-title-show",
            # Software path: GL/VAAPI into XWayland wallpaper surfaces goes white
            # or RGB-scrambles when other windows move (Mutter compositing).
            "--avcodec-hw=none",
            "--vout=xcb_x11",
            "--no-video-deco",
        ]
        try:
            self.instance = vlc.Instance(vlc_options)
        except (NameError, OSError, AttributeError) as e:
            raise RuntimeError(
                "libVLC is not available. Install VLC packages, e.g. "
                "`sudo apt install vlc` (provides libvlc5 + plugins), "
                "then restart Hidamari."
            ) from e
        if self.instance is None:
            raise RuntimeError(
                "Failed to create a VLC instance. Install VLC (`sudo apt install vlc`) "
                "and ensure libvlc is on the library path."
            )
        self.player = self.instance.media_player_new()
        self.player.video_set_mouse_input(False)
        self.player.video_set_key_input(False)

        xid = self.surface.get_xid()
        if xid is None:
            raise RuntimeError("Wallpaper X11 surface has no XID")
        self.player.set_xwindow(int(xid))
        logger.info("[PlayerWindow] VLC embedded into pure X11 xid=%s", xid)

    def present(self):
        self.surface.present()

    def show(self):
        self.surface.show()

    def play(self):
        xid = self.surface.get_xid()
        if xid is not None:
            self.player.set_xwindow(int(xid))
        self.player.play()

    def play_fade(self, target, fade_duration_sec, fade_interval):
        self.play()
        cur = 0
        steps = max(1.0, float(fade_duration_sec) / max(float(fade_interval), 0.01))
        step = (target - cur) / steps
        self.fade.cancel()
        self.fade.start(
            cur=cur,
            target=target,
            step=step,
            fade_interval=fade_interval,
            update_callback=self.set_volume,
        )

    def is_playing(self):
        return self.player.is_playing()

    def pause(self):
        if self.is_playing():
            self.player.pause()

    def pause_fade(self, fade_duration_sec, fade_interval):
        cur = self.get_volume()
        target = 0
        steps = max(1.0, float(fade_duration_sec) / max(float(fade_interval), 0.01))
        step = (target - cur) / steps
        self.fade.cancel()
        self.fade.start(
            cur=cur,
            target=target,
            step=step,
            fade_interval=fade_interval,
            update_callback=self.set_volume,
            complete_callback=self.pause,
        )

    def volume_fade(self, target, fade_duration_sec, fade_interval):
        cur = self.get_volume()
        steps = max(1.0, float(fade_duration_sec) / max(float(fade_interval), 0.01))
        step = (target - cur) / steps
        self.fade.cancel()
        self.fade.start(
            cur=cur,
            target=target,
            step=step,
            fade_interval=fade_interval,
            update_callback=self.set_volume,
        )

    def media_new(self, *args):
        return self.instance.media_new(*args)

    def set_media(self, *args):
        self.player.set_media(*args)

    def set_volume(self, *args):
        self.player.audio_set_volume(*args)

    def get_volume(self):
        return self.player.audio_get_volume()

    def set_mute(self, is_mute):
        return self.player.audio_set_mute(is_mute)

    def get_position(self):
        return self.player.get_position()

    def set_position(self, *args):
        self.player.set_position(*args)

    def snapshot(self, *args):
        return self.player.video_take_snapshot(*args)

    def centercrop(self, video_width=None, video_height=None):
        if (video_width, video_height) == (None, None):
            video_width, video_height = self.player.video_get_size()
            if video_width == 0 or video_height == 0:
                logger.warning("[CenterCrop] video_get_size is not ready yet")
                return
        logger.debug(f"[CenterCrop] Dimension {video_width}x{video_height}")
        window_ratio = self.width / self.height
        video_ratio = video_width / video_height
        if window_ratio == video_ratio:
            return
        elif video_ratio < window_ratio:
            crop_height = video_width / window_ratio
            top_offset = (video_height - crop_height) / 2
            crop_geometry = (
                f"{int(video_width)}x{int(crop_height + top_offset)}+0+{int(top_offset)}"
            )
        else:
            crop_width = video_height * window_ratio
            left_offset = (video_width - crop_width) / 2
            crop_geometry = (
                f"{int(crop_width + left_offset)}x{int(video_height)}+{int(left_offset)}+0"
            )
        logger.debug(f"[CenterCrop] Crop geometry: {crop_geometry}")
        self.player.video_set_crop_geometry(crop_geometry)

    def add_audio_track(self, audio):
        self.player.add_slave(vlc.MediaSlaveType(1), audio, True)

    def get_name(self):
        return self.name

    def resize_to(self, width, height, x=None, y=None):
        self.width = width
        self.height = height
        self.surface.resize_to(width, height, x, y)
        xid = self.surface.get_xid()
        if xid is not None:
            self.player.set_xwindow(int(xid))

    def destroy(self):
        self.cleanup()

    def cleanup(self):
        self.fade.cancel()
        try:
            if self.player:
                self.player.stop()
                self.player.release()
                self.player = None
            if self.instance:
                self.instance.release()
                self.instance = None
        except Exception as e:
            logger.warning("[PlayerWindow] VLC cleanup: %s", e)
        if self.surface:
            self.surface.destroy()
            self.surface = None


class VideoPlayer(BasePlayer):
    """
    <node>
    <interface name='io.github.jeffshee.hidamari.player'>
        <property name="mode" type="s" access="read"/>
        <property name="data_source" type="s" access="readwrite"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <property name="is_paused_by_user" type="b" access="readwrite"/>
        <method name='reload_config'/>
        <method name='pause_playback'/>
        <method name='start_playback'/>
        <method name='quit_player'/>
    </interface>
    </node>
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize X11 threads so VLC can use hardware decoding.
        # `libX11.so.6` fix for Fedora 33
        x11 = None
        for lib in ["libX11.so", "libX11.so.6"]:
            try:
                x11 = ctypes.cdll.LoadLibrary(lib)
            except OSError:
                pass
            if x11 is not None:
                x11.XInitThreads()
                break

        self.config = None
        self.reload_config()

        # Static wallpaper (currently for GNOME only)
        if is_gnome():
            self.original_wallpaper_uri = None
            self.original_wallpaper_uri_dark = None
            if is_flatpak():
                try:
                    self.original_wallpaper_uri = subprocess.check_output(
                        "flatpak-spawn --host gsettings get org.gnome.desktop.background picture-uri",
                        shell=True,
                        encoding="UTF-8",
                    )
                    self.original_wallpaper_uri_dark = subprocess.check_output(
                        "flatpak-spawn --host gsettings get org.gnome.desktop.background picture-uri-dark",
                        shell=True,
                        encoding="UTF-8",
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(f"[StaticWallpaper] {e}")
            else:
                gso = Gio.Settings.new("org.gnome.desktop.background")
                self.original_wallpaper_uri = gso.get_string("picture-uri")
                self.original_wallpaper_uri_dark = gso.get_string("picture-uri-dark")

        # Handler should be created after everything initialized
        self.active_handler, self.window_handler = None, None
        self.is_any_maximized, self.is_any_fullscreen = False, False
        self.is_paused_by_user = False

    def new_window(self, gdk_monitor):
        rect = gdk_monitor.get_geometry()
        model = gdk_monitor.get_model() or "monitor"
        return PlayerWindow(model, rect.x, rect.y, rect.width, rect.height)

    def do_activate(self):
        try:
            # Keep Gtk.Application alive without any Gtk windows.
            self.hold()
            super().do_activate()
            # Periodically re-lower wallpaper under newly mapped clients.
            GLib.timeout_add_seconds(2, self._keep_wallpaper_below)
            GLib.timeout_add(150, self._start_media)
        except RuntimeError as e:
            logger.error("[VideoPlayer] %s", e)
            GLib.idle_add(self.quit)
            return

    def _keep_wallpaper_below(self):
        for window in self.windows.values():
            if window is not None and getattr(window, "surface", None) is not None:
                try:
                    window.surface.restack()
                except Exception:
                    pass
        return True  # repeat

    def _start_media(self):
        try:
            self.data_source = self.config[CONFIG_KEY_DATA_SOURCE]
        except RuntimeError as e:
            logger.error("[VideoPlayer] %s", e)
            GLib.idle_add(self.quit)
        except Exception as e:
            logger.exception("[VideoPlayer] Failed to start media: %s", e)
        return False

    def _on_monitors_changed(self, model, position, removed, added):
        super()._on_monitors_changed(model, position, removed, added)
        self.monitor_sync()

    def _on_active_changed(self, active):
        if active:
            self.pause_playback()
        else:
            if self._should_playback_start():
                self.start_playback()
            else:
                self.pause_playback()

    def _on_window_state_changed(self, state):
        self.is_any_maximized, self.is_any_fullscreen = (
            state["is_any_maximized"],
            state["is_any_fullscreen"],
        )
        logger.info(
            f"is_any_maximized: {self.is_any_maximized}, is_any_fullscreen: {self.is_any_fullscreen}"
        )

        if self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED]:
            if self._should_playback_start():
                self.start_playback()
            else:
                self.pause_playback()
        elif self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]:
            for monitor, window in self.windows.items():
                if not is_primary_monitor(monitor):
                    continue
                if self.is_any_fullscreen or self.is_any_maximized:
                    window.volume_fade(
                        target=0,
                        fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                        fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL],
                    )
                else:
                    window.volume_fade(
                        target=self.volume,
                        fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                        fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL],
                    )

    def _should_playback_start(self):
        if self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED] and (
            self.is_any_maximized or self.is_any_fullscreen
        ):
            return False
        if self.is_paused_by_user:
            return False
        return True

    @property
    def mode(self):
        return self.config[CONFIG_KEY_MODE]

    @property
    def data_source(self):
        return self.config[CONFIG_KEY_DATA_SOURCE]

    @data_source.setter
    def data_source(self, data_source):
        self.config[CONFIG_KEY_DATA_SOURCE] = data_source

        if self.mode == MODE_VIDEO:
            # Get the dimension of the video
            video_width, video_height = {}, {}
            try:
                for monitor, video in data_source.items():
                    # fallback to Default video
                    if len(video) == 0:
                        video = data_source["Default"]
                    dimension = subprocess.check_output(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=width,height",
                            "-of",
                            "csv=s=x:p=0",
                            video,
                        ],
                        shell=False,
                        encoding="UTF-8",
                    ).replace("\n", "")
                    dimension = dimension.split("x")
                    video_width[monitor] = int(dimension[0])
                    video_height[monitor] = int(dimension[1])
            except subprocess.CalledProcessError:
                for monitor, _video in data_source.items():
                    video_width.setdefault(monitor, None)
                    video_height.setdefault(monitor, None)

            for monitor, window in self.windows.items():
                source = (
                    data_source[monitor.get_model()]
                    if monitor.get_model() in data_source
                    and len(data_source[monitor.get_model()]) != 0
                    else data_source["Default"]
                )
                logger.info(f"Setting source {source} to {monitor.get_model()}")
                media = window.media_new(source)
                """
                This loops the media itself. Using -R / --repeat and/or -L / --loop don't seem to work. However,
                based on reading, this probably only repeats 65535 times, which is still a lot of time, but might
                cause the program to stop playback if it's left on for a very long time.
                """
                media.add_option("input-repeat=65535")
                # Prevent awful ear-rape with multiple instances.
                if not is_primary_monitor(monitor):
                    media.add_option("no-audio")
                window.set_media(media)
                window.set_position(0.0)
                if (
                    monitor.get_model() not in data_source
                    or len(data_source[monitor.get_model()]) == 0
                ):
                    window.centercrop(video_width["Default"], video_height["Default"])
                else:
                    window.centercrop(
                        video_width[monitor.get_model()], video_height[monitor.get_model()]
                    )

        elif self.mode == MODE_STREAM:
            source = data_source["Default"]
            formats = get_formats(source)
            max_height = (
                max(self.windows, key=lambda m: m.get_geometry().height).get_geometry().height
            )
            video_url, video_width, video_height = get_optimal_video(formats, max_height)
            audio_url = get_best_audio(formats)

            for monitor, window in self.windows.items():
                media = window.media_new(video_url)
                media.add_option("input-repeat=65535")
                window.set_media(media)
                if is_primary_monitor(monitor):
                    window.add_audio_track(audio_url)
                else:
                    # `get_optimal_video` now might return video with audio.
                    media.add_option("no-audio")
                window.set_position(0.0)
                window.centercrop(video_width, video_height)
        else:
            raise ValueError("Invalid mode")

        self.volume = self.config[CONFIG_KEY_VOLUME]
        self.is_mute = self.config[CONFIG_KEY_MUTE]
        self.start_playback()

        # Everything is initialized. Create handlers if haven't (singleton pattern).
        if not self.active_handler:
            self.active_handler = ActiveHandler(self._on_active_changed)
        if not self.window_handler and not is_wayland():
            # Only create WindowHandler on X11, not Wayland
            self.window_handler = WindowHandler(self._on_window_state_changed)

        if self.config[CONFIG_KEY_STATIC_WALLPAPER] and self.mode == MODE_VIDEO:
            self.set_static_wallpaper()
        else:
            self.set_original_wallpaper()

    @property
    def volume(self):
        return self.config[CONFIG_KEY_VOLUME]

    @volume.setter
    def volume(self, volume):
        self.config[CONFIG_KEY_VOLUME] = volume
        for monitor in self.windows:
            if is_primary_monitor(monitor):
                self.windows[monitor].set_volume(volume)

    @property
    def is_mute(self):
        return self.config[CONFIG_KEY_MUTE]

    @is_mute.setter
    def is_mute(self, is_mute):
        self.config[CONFIG_KEY_MUTE] = is_mute
        for monitor, window in self.windows.items():
            if is_primary_monitor(monitor):
                window.set_mute(is_mute)

    @property
    def is_playing(self):
        return not self.is_paused_by_user

    def pause_playback(self):
        for _monitor, window in self.windows.items():
            window.pause_fade(
                fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL],
            )

    def start_playback(self):
        if self._should_playback_start():
            for _monitor, window in self.windows.items():
                window.play_fade(
                    target=self.volume,
                    fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                    fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL],
                )

    def monitor_sync(self):
        primary_monitor = None
        for monitor, _window in self.windows.items():
            if is_primary_monitor(monitor):
                primary_monitor = monitor
                break
        if primary_monitor:
            for monitor, window in self.windows.items():
                if monitor == primary_monitor:
                    continue
                # `set_position()` method require the playback to be enabled before calling
                window.play()
                window.set_position(self.windows[primary_monitor].get_position())
                window.play() if self.windows[primary_monitor].is_playing() else window.pause()

    def set_static_wallpaper(self):
        # Currently for GNOME only
        if not is_gnome():
            return
        # Get the duration of the video
        try:
            duration = float(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        self.data_source["Default"],
                    ],
                    shell=False,
                )
            )
        except subprocess.CalledProcessError:
            duration = 0
        # Find the golden ratio
        ss = time.strftime("%H:%M:%S", time.gmtime(duration / 3.14))
        # Extract the frame
        static_wallpaper_path = os.path.join(
            CONFIG_DIR, f"static-{random.randint(0, 999999):06d}.png"
        )
        ret = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                ss,
                "-i",
                self.data_source["Default"],
                "-vframes",
                "1",
                static_wallpaper_path,
            ],
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        if ret.returncode == 0 and os.path.isfile(static_wallpaper_path):
            blur_wallpaper = Image.open(static_wallpaper_path)
            blur_wallpaper = blur_wallpaper.filter(
                ImageFilter.GaussianBlur(self.config["static_wallpaper_blur_radius"])
            )
            blur_wallpaper.save(static_wallpaper_path)
            static_wallpaper_uri = pathlib.Path(static_wallpaper_path).resolve().as_uri()
            if is_flatpak():
                try:
                    subprocess.run(
                        [
                            "flatpak-spawn",
                            "--host",
                            "gsettings",
                            "set",
                            "org.gnome.desktop.background",
                            "picture-uri",
                            static_wallpaper_uri,
                        ],
                        shell=False,
                    )
                    subprocess.run(
                        [
                            "flatpak-spawn",
                            "--host",
                            "gsettings",
                            "set",
                            "org.gnome.desktop.background",
                            "picture-uri-dark",
                            static_wallpaper_uri,
                        ],
                        shell=False,
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(f"[StaticWallpaper] {e}")
            else:
                gso = Gio.Settings.new("org.gnome.desktop.background")
                gso.set_string("picture-uri", static_wallpaper_uri)
                gso.set_string("picture-uri-dark", static_wallpaper_uri)

    def set_original_wallpaper(self):
        # Currently for GNOME only
        if not is_gnome():
            return
        if is_flatpak():
            try:
                if self.original_wallpaper_uri is not None:
                    subprocess.run(
                        [
                            "flatpak-spawn",
                            "--host",
                            "gsettings",
                            "set",
                            "org.gnome.desktop.background",
                            "picture-uri",
                            self.original_wallpaper_uri,
                        ],
                        shell=False,
                    )
                if self.original_wallpaper_uri_dark is not None:
                    subprocess.run(
                        [
                            "flatpak-spawn",
                            "--host",
                            "gsettings",
                            "set",
                            "org.gnome.desktop.background",
                            "picture-uri-dark",
                            self.original_wallpaper_uri,
                        ],
                        shell=False,
                    )
            except subprocess.CalledProcessError as e:
                logger.error(f"[StaticWallpaper] {e}")
        else:
            gso = Gio.Settings.new("org.gnome.desktop.background")
            gso.set_string("picture-uri", self.original_wallpaper_uri)
            gso.set_string("picture-uri-dark", self.original_wallpaper_uri_dark)
        # Purge the generated static wallpaper (and leftover if any)
        for f in glob.glob(os.path.join(CONFIG_DIR, "static-*.png")):
            os.remove(f)

    def reload_config(self):
        self.config = ConfigUtil().load()

    def quit_player(self):
        self.set_original_wallpaper()

        # Cleanup handlers
        if self.active_handler:
            self.active_handler.cleanup()
            self.active_handler = None

        if self.window_handler:
            self.window_handler.cleanup()
            self.window_handler = None

        # Cleanup all windows
        for _monitor, window in self.windows.items():
            if window:
                window.cleanup()

        super().quit_player()


def main():
    bus = SessionBus()
    app = VideoPlayer()
    try:
        bus.publish(DBUS_NAME_PLAYER, app)
    except RuntimeError as e:
        logger.error(e)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
