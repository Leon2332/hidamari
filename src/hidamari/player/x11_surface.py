"""Pure X11 desktop surface for VLC wallpaper embedding.

GTK4 creates 32-bit ARGB windows. VLC's xcb_x11 output can fail to paint
(white / glitched surface under GNOME XWayland). Creating a
depth-24 top-level X window with classic desktop WM hints matches what the
old GTK3 DrawingArea path provided.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p

from hidamari.commons import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

_PropModeReplace = 0
_XA_ATOM = 4
_XA_CARDINAL = 6
_InputOutput = 1
_CWBackPixel = 1 << 1
_CWBorderPixel = 1 << 3
_CWOverrideRedirect = 1 << 9
_CWEventMask = 1 << 11
_CWColormap = 1 << 13
_ExposureMask = 1 << 15
_StructureNotifyMask = 1 << 17
_ButtonPressMask = 1 << 2
_ButtonReleaseMask = 1 << 3
_SubstructureRedirectMask = 1 << 20
_SubstructureNotifyMask = 1 << 19

_PPosition = 1 << 2
_PSize = 1 << 3
_PMinSize = 1 << 4
_PMaxSize = 1 << 5


class _XSizeHints(Structure):
    _fields_ = [
        ("flags", c_long),
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("min_width", c_int),
        ("min_height", c_int),
        ("max_width", c_int),
        ("max_height", c_int),
        ("width_inc", c_int),
        ("height_inc", c_int),
        ("min_aspect_x", c_int),
        ("min_aspect_y", c_int),
        ("max_aspect_x", c_int),
        ("max_aspect_y", c_int),
        ("base_width", c_int),
        ("base_height", c_int),
        ("win_gravity", c_int),
    ]


class _XSetWindowAttributes(Structure):
    _fields_ = [
        ("background_pixmap", c_ulong),
        ("background_pixel", c_ulong),
        ("border_pixmap", c_ulong),
        ("border_pixel", c_ulong),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("event_mask", c_long),
        ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int),
        ("colormap", c_ulong),
        ("cursor", c_ulong),
    ]


class _XClientMessageEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("window", c_ulong),
        ("message_type", c_ulong),
        ("format", c_int),
        ("data", c_long * 5),
    ]


_x11 = None
_display = None


def _lib():
    global _x11, _display
    if _x11 is None:
        try:
            _x11 = ctypes.CDLL("libX11.so.6")
        except OSError:
            _x11 = ctypes.CDLL("libX11.so")
        x = _x11
        x.XOpenDisplay.restype = c_void_p
        x.XOpenDisplay.argtypes = [c_char_p]
        x.XDefaultRootWindow.restype = c_ulong
        x.XDefaultRootWindow.argtypes = [c_void_p]
        x.XDefaultScreen.restype = c_int
        x.XDefaultScreen.argtypes = [c_void_p]
        x.XDefaultVisual.restype = c_void_p
        x.XDefaultVisual.argtypes = [c_void_p, c_int]
        x.XDefaultDepth.restype = c_int
        x.XDefaultDepth.argtypes = [c_void_p, c_int]
        x.XDefaultColormap.restype = c_ulong
        x.XDefaultColormap.argtypes = [c_void_p, c_int]
        x.XBlackPixel.restype = c_ulong
        x.XBlackPixel.argtypes = [c_void_p, c_int]
        x.XCreateSimpleWindow.restype = c_ulong
        x.XCreateSimpleWindow.argtypes = [
            c_void_p,
            c_ulong,
            c_int,
            c_int,
            c_uint,
            c_uint,
            c_uint,
            c_ulong,
            c_ulong,
        ]
        x.XChangeWindowAttributes.argtypes = [c_void_p, c_ulong, c_ulong, c_void_p]
        x.XMapWindow.argtypes = [c_void_p, c_ulong]
        x.XUnmapWindow.argtypes = [c_void_p, c_ulong]
        x.XDestroyWindow.argtypes = [c_void_p, c_ulong]
        x.XMoveResizeWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint]
        x.XLowerWindow.argtypes = [c_void_p, c_ulong]
        x.XFlush.argtypes = [c_void_p]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XInternAtom.restype = c_ulong
        x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        x.XChangeProperty.argtypes = [
            c_void_p,
            c_ulong,
            c_ulong,
            c_ulong,
            c_int,
            c_int,
            c_void_p,
            c_int,
        ]
        x.XSetWMNormalHints.argtypes = [c_void_p, c_ulong, POINTER(_XSizeHints)]
        x.XStoreName.argtypes = [c_void_p, c_ulong, c_char_p]
        x.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, c_void_p]
        x.XClearWindow.argtypes = [c_void_p, c_ulong]
    if _display is None:
        _display = _x11.XOpenDisplay(None)
        if not _display:
            raise RuntimeError("XOpenDisplay failed — is DISPLAY set / XWayland running?")
    return _x11, _display


def _set_atom_prop(x11, display, xid, name: bytes, values: list[int]):
    atom = x11.XInternAtom(display, name, False)
    arr = (c_ulong * len(values))(*values)
    x11.XChangeProperty(
        display,
        c_ulong(xid),
        atom,
        _XA_ATOM if name != b"_NET_WM_DESKTOP" else _XA_CARDINAL,
        32,
        _PropModeReplace,
        ctypes.cast(arr, c_void_p),
        len(values),
    )


def _send_state(x11, display, root, xid, action, a1, a2=0):
    state = x11.XInternAtom(display, b"_NET_WM_STATE", False)
    ev = _XClientMessageEvent()
    ev.type = 33
    ev.send_event = 1
    ev.display = display
    ev.window = c_ulong(xid)
    ev.message_type = state
    ev.format = 32
    ev.data[0] = action
    ev.data[1] = a1
    ev.data[2] = a2
    ev.data[3] = 1
    ev.data[4] = 0
    mask = _SubstructureRedirectMask | _SubstructureNotifyMask
    x11.XSendEvent(display, c_ulong(root), False, mask, byref(ev))


class X11DesktopSurface:
    """A monitor-sized X11 desktop window suitable for VLC ``set_xwindow``."""

    def __init__(self, name: str, x: int, y: int, width: int, height: int):
        self.name = name
        self.x = int(x)
        self.y = int(y)
        self.width = int(width)
        self.height = int(height)
        self.xid = None
        self._create()

    def _create(self):
        x11, display = _lib()
        screen = x11.XDefaultScreen(display)
        root = x11.XDefaultRootWindow(display)
        depth = x11.XDefaultDepth(display, screen)  # typically 24, not GTK's 32
        black = x11.XBlackPixel(display, screen)

        # Managed (NOT override-redirect) depth-24 window.
        # Override-redirect on GNOME/XWayland paints *above* all Wayland apps.
        # DESKTOP + BELOW keeps us under other windows as a wallpaper layer.
        self.xid = int(
            x11.XCreateSimpleWindow(
                display,
                c_ulong(root),
                self.x,
                self.y,
                c_uint(max(1, self.width)),
                c_uint(max(1, self.height)),
                0,
                black,
                black,
            )
        )
        if not self.xid:
            raise RuntimeError("XCreateSimpleWindow failed for wallpaper surface")

        attrs = _XSetWindowAttributes()
        attrs.event_mask = (
            _ExposureMask | _StructureNotifyMask | _ButtonPressMask | _ButtonReleaseMask
        )
        x11.XChangeWindowAttributes(display, c_ulong(self.xid), c_ulong(_CWEventMask), byref(attrs))

        x11.XStoreName(display, c_ulong(self.xid), b"hidamari-wallpaper")

        # Size/position hints (WM may still inset for the panel).
        hints = _XSizeHints()
        hints.flags = _PPosition | _PSize | _PMinSize | _PMaxSize
        hints.x = self.x
        hints.y = self.y
        hints.width = hints.min_width = hints.max_width = self.width
        hints.height = hints.min_height = hints.max_height = self.height
        x11.XSetWMNormalHints(display, c_ulong(self.xid), byref(hints))

        # Motif: no decorations
        motif = x11.XInternAtom(display, b"_MOTIF_WM_HINTS", False)
        motif_data = (c_ulong * 5)(2, 0, 0, 0, 0)
        x11.XChangeProperty(
            display,
            c_ulong(self.xid),
            motif,
            motif,
            32,
            _PropModeReplace,
            ctypes.cast(motif_data, c_void_p),
            5,
        )

        # Desktop type + sticky across workspaces
        type_desktop = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DESKTOP", False)
        _set_atom_prop(x11, display, self.xid, b"_NET_WM_WINDOW_TYPE", [type_desktop])
        _set_atom_prop(x11, display, self.xid, b"_NET_WM_DESKTOP", [0xFFFFFFFF])

        atom_below = x11.XInternAtom(display, b"_NET_WM_STATE_BELOW", False)
        atom_skip_tb = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", False)
        atom_skip_pg = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", False)
        atom_sticky = x11.XInternAtom(display, b"_NET_WM_STATE_STICKY", False)
        _set_atom_prop(
            x11,
            display,
            self.xid,
            b"_NET_WM_STATE",
            [atom_below, atom_skip_tb, atom_skip_pg, atom_sticky],
        )

        x11.XMapWindow(display, c_ulong(self.xid))
        x11.XSync(display, False)

        # Client messages after map (GNOME often ignores pre-map state props).
        _send_state(x11, display, root, self.xid, 1, atom_below, atom_skip_tb)
        _send_state(x11, display, root, self.xid, 1, atom_skip_pg, atom_sticky)
        atom_fs = x11.XInternAtom(display, b"_NET_WM_STATE_FULLSCREEN", False)
        atom_above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", False)
        _send_state(x11, display, root, self.xid, 0, atom_fs)
        _send_state(x11, display, root, self.xid, 0, atom_above)
        x11.XLowerWindow(display, c_ulong(self.xid))
        x11.XFlush(display)

        logger.info(
            "[X11Surface] Created DESKTOP+BELOW wallpaper xid=%s depth=%s geom=%sx%s+%s+%s name=%s",
            self.xid,
            depth,
            self.width,
            self.height,
            self.x,
            self.y,
            self.name,
        )

    def restack(self):
        """Re-assert DESKTOP/BELOW stacking (new windows or the shell can restack us)."""
        if not self.xid:
            return
        x11, display = _lib()
        root = x11.XDefaultRootWindow(display)
        x11.XMoveResizeWindow(
            display,
            c_ulong(self.xid),
            self.x,
            self.y,
            c_uint(max(1, self.width)),
            c_uint(max(1, self.height)),
        )
        atom_below = x11.XInternAtom(display, b"_NET_WM_STATE_BELOW", False)
        atom_skip_tb = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", False)
        atom_skip_pg = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", False)
        atom_sticky = x11.XInternAtom(display, b"_NET_WM_STATE_STICKY", False)
        atom_fs = x11.XInternAtom(display, b"_NET_WM_STATE_FULLSCREEN", False)
        atom_above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", False)
        type_desktop = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DESKTOP", False)
        _set_atom_prop(x11, display, self.xid, b"_NET_WM_WINDOW_TYPE", [type_desktop])
        _set_atom_prop(
            x11,
            display,
            self.xid,
            b"_NET_WM_STATE",
            [atom_below, atom_skip_tb, atom_skip_pg, atom_sticky],
        )
        _send_state(x11, display, root, self.xid, 1, atom_below, atom_skip_tb)
        _send_state(x11, display, root, self.xid, 1, atom_skip_pg, atom_sticky)
        _send_state(x11, display, root, self.xid, 0, atom_fs)
        _send_state(x11, display, root, self.xid, 0, atom_above)
        x11.XLowerWindow(display, c_ulong(self.xid))
        x11.XFlush(display)

    def show(self):
        x11, display = _lib()
        if self.xid:
            x11.XMapWindow(display, c_ulong(self.xid))
            self.restack()

    def present(self):
        self.show()

    def resize_to(self, width, height, x=None, y=None):
        self.width = int(width)
        self.height = int(height)
        if x is not None:
            self.x = int(x)
        if y is not None:
            self.y = int(y)
        if not self.xid:
            return
        self.restack()

    def destroy(self):
        if not self.xid:
            return
        x11, display = _lib()
        try:
            x11.XDestroyWindow(display, c_ulong(self.xid))
            x11.XFlush(display)
        except Exception as e:
            logger.warning("[X11Surface] destroy: %s", e)
        self.xid = None

    def get_xid(self) -> int | None:
        return self.xid
