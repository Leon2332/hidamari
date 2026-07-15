"""X11 helpers for wallpaper windows under GTK4.

GTK4 removed ``set_type_hint(DESKTOP)``, ``move()``, and related window
manager hints. Hidamari still forces ``GDK_BACKEND=x11`` for VLC embedding,
so we apply the equivalent hints through libX11 after the surface is mapped.

GTK4 widgets also no longer each own an X window. VLC needs a real X11
drawable, so we create a child X window under the Gtk toplevel for embedding.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkX11, Gtk

from hidamari.commons import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

# X11 constants
_PropModeReplace = 0
_XA_ATOM = 4
_XA_CARDINAL = 6
_InputOutput = 1
_CWBackPixel = 1 << 1
_CWEventMask = 1 << 11
_CWOverrideRedirect = 1 << 9
_CWColormap = 1 << 13
_ExposureMask = 1 << 15
_StructureNotifyMask = 1 << 17


class _XSizeHints(ctypes.Structure):
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


class _XSetWindowAttributes(ctypes.Structure):
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


_PPosition = 1 << 2
_PSize = 1 << 3
_PMinSize = 1 << 4
_PMaxSize = 1 << 5


def _load_x11():
    try:
        return ctypes.CDLL("libX11.so.6")
    except OSError:
        try:
            return ctypes.CDLL("libX11.so")
        except OSError as e:
            logger.error("[X11] Could not load libX11: %s", e)
            return None


# Prefer GDK's X Display so windows we create are valid for clients (VLC)
# that use the same X connection. A separate XOpenDisplay() can produce XIDs
# that VLC rejects as "bad X11 window" under XWayland.
_x11_lib = None


def _x11_conn():
    """Return (libX11, Display*) using GDK's live X connection when possible."""
    global _x11_lib
    if _x11_lib is None:
        _x11_lib = _load_x11()
        if _x11_lib is None:
            return None, None
        _bind_x11(_x11_lib)

    display_ptr = None
    gdk_display = Gdk.Display.get_default()
    if isinstance(gdk_display, GdkX11.X11Display):
        try:
            # gi wraps this as an xlib.Display; cast to a raw pointer for ctypes.
            xdisp = gdk_display.get_xdisplay()
            display_ptr = ctypes.cast(hash(xdisp) if False else int(xdisp) if False else None, c_void_p)
        except Exception:
            display_ptr = None
        # Robust extraction of the underlying pointer from the GI wrapper.
        if display_ptr is None or not display_ptr.value:
            try:
                # PyGObject exposes .__gpointer__ / capsule; fall back to c_void_p from address.
                addr = int(gdk_display.get_xdisplay())
                display_ptr = c_void_p(addr)
            except Exception:
                try:
                    from gi.repository import xlib as _xlib  # noqa: F401

                    raw = gdk_display.get_xdisplay()
                    # Object is a GI void wrapper; use ctypes addressof via int()
                    display_ptr = ctypes.cast(raw.__gpointer__ if hasattr(raw, "__gpointer__") else id(raw), c_void_p)
                except Exception as e:
                    logger.debug("[X11] Could not extract GDK xdisplay pointer: %s", e)
                    display_ptr = None

    if display_ptr is None or not getattr(display_ptr, "value", display_ptr):
        # Last resort: open our own connection and keep it for the process life.
        if not hasattr(_x11_conn, "_fallback"):
            _x11_conn._fallback = _x11_lib.XOpenDisplay(None)
        display_ptr = _x11_conn._fallback
        if not display_ptr:
            logger.warning("[X11] XOpenDisplay failed")
            return _x11_lib, None

    # Normalize to c_void_p
    if not isinstance(display_ptr, c_void_p):
        display_ptr = c_void_p(int(display_ptr))
    return _x11_lib, display_ptr


def _bind_x11(x11):
    x11.XOpenDisplay.restype = c_void_p
    x11.XOpenDisplay.argtypes = [c_char_p]
    x11.XDefaultRootWindow.restype = c_ulong
    x11.XDefaultRootWindow.argtypes = [c_void_p]
    x11.XDefaultScreen.restype = c_int
    x11.XDefaultScreen.argtypes = [c_void_p]
    x11.XDefaultVisual.restype = c_void_p
    x11.XDefaultVisual.argtypes = [c_void_p, c_int]
    x11.XDefaultDepth.restype = c_int
    x11.XDefaultDepth.argtypes = [c_void_p, c_int]
    x11.XDefaultColormap.restype = c_ulong
    x11.XDefaultColormap.argtypes = [c_void_p, c_int]
    x11.XBlackPixel.restype = c_ulong
    x11.XBlackPixel.argtypes = [c_void_p, c_int]
    x11.XCreateWindow.restype = c_ulong
    x11.XCreateWindow.argtypes = [
        c_void_p,
        c_ulong,
        c_int,
        c_int,
        c_uint,
        c_uint,
        c_uint,
        c_int,
        c_uint,
        c_void_p,
        c_ulong,
        POINTER(_XSetWindowAttributes),
    ]
    x11.XMapWindow.argtypes = [c_void_p, c_ulong]
    x11.XDestroyWindow.argtypes = [c_void_p, c_ulong]
    x11.XMoveResizeWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint]
    x11.XLowerWindow.argtypes = [c_void_p, c_ulong]
    x11.XRaiseWindow.argtypes = [c_void_p, c_ulong]
    x11.XFlush.argtypes = [c_void_p]
    x11.XSync.argtypes = [c_void_p, c_int]
    x11.XCloseDisplay.argtypes = [c_void_p]
    x11.XInternAtom.restype = c_ulong
    x11.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
    x11.XChangeProperty.argtypes = [
        c_void_p,
        c_ulong,
        c_ulong,
        c_ulong,
        c_int,
        c_int,
        c_void_p,
        c_int,
    ]
    x11.XSetWMNormalHints.argtypes = [c_void_p, c_ulong, POINTER(_XSizeHints)]
    x11.XSelectInput.argtypes = [c_void_p, c_ulong, c_long]


def get_xid(widget: Gtk.Widget) -> int | None:
    """Return the X11 window id for a realized GTK widget, or None."""
    native = widget.get_native()
    if native is None:
        return None
    surface = native.get_surface()
    if not isinstance(surface, GdkX11.X11Surface):
        return None
    return surface.get_xid()


def is_primary_monitor(monitor: Gdk.Monitor) -> bool:
    """GTK4 removed Gdk.Monitor.is_primary(); compare against the primary monitor."""
    display = monitor.get_display()
    primary = display.get_primary_monitor()
    if primary is not None:
        return monitor == primary
    # Fallback: treat the first monitor as primary.
    monitors = display.get_monitors()
    return monitors.get_n_items() > 0 and monitors.get_item(0) == monitor


def create_embed_subwindow(parent_xid: int, width: int, height: int) -> int | None:
    """Create a black child X window suitable for VLC ``set_xwindow`` embedding."""
    x11, display = _x11_conn()
    if x11 is None or not display:
        return None

    screen = x11.XDefaultScreen(display)
    visual = x11.XDefaultVisual(display, screen)
    depth = x11.XDefaultDepth(display, screen)
    black = x11.XBlackPixel(display, screen)

    attrs = _XSetWindowAttributes()
    attrs.background_pixel = black
    attrs.event_mask = _ExposureMask | _StructureNotifyMask
    attrs.colormap = x11.XDefaultColormap(display, screen)
    valuemask = _CWBackPixel | _CWEventMask | _CWColormap

    child = x11.XCreateWindow(
        display,
        c_ulong(parent_xid),
        0,
        0,
        c_uint(max(1, int(width))),
        c_uint(max(1, int(height))),
        0,
        depth,
        _InputOutput,
        visual,
        valuemask,
        byref(attrs),
    )
    if not child:
        logger.warning("[X11] XCreateWindow failed")
        return None

    x11.XMapWindow(display, child)
    x11.XSync(display, False)
    logger.info(
        "[X11] Embed subwindow parent=%s child=%s %sx%s",
        parent_xid,
        child,
        width,
        height,
    )
    return int(child)


def resize_embed_subwindow(xid: int, width: int, height: int) -> None:
    x11, display = _x11_conn()
    if x11 is None or not display or not xid:
        return
    x11.XMoveResizeWindow(
        display, c_ulong(xid), 0, 0, c_uint(max(1, int(width))), c_uint(max(1, int(height)))
    )
    x11.XFlush(display)


def destroy_embed_subwindow(xid: int) -> None:
    if not xid:
        return
    x11, display = _x11_conn()
    if x11 is None or not display:
        return
    x11.XDestroyWindow(display, c_ulong(xid))
    x11.XFlush(display)


def configure_desktop_window(window: Gtk.Window, x: int, y: int, width: int, height: int) -> None:
    """Position a window as an undecorated desktop/wallpaper surface on X11."""
    window.set_decorated(False)
    window.set_resizable(False)
    # Never use Gtk fullscreen — Mutter then sets _NET_WM_STATE_FULLSCREEN on
    # the DESKTOP window, which breaks stacking and compositing under Wayland.
    if hasattr(window, "set_fullscreened"):
        window.set_fullscreened(False)
    window.unfullscreen()
    window.set_default_size(width, height)
    window.set_size_request(width, height)

    # Avoid GTK drawing a theme background over the video.
    provider = Gtk.CssProvider()
    provider.load_from_string(
        "window.wallpaper { background: #000000; } "
        "window.wallpaper > * { background: transparent; }"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    window.add_css_class("wallpaper")

    def _apply(*_args):
        apply_desktop_hints(window, x, y, width, height)
        # Re-assert stacking after the compositor settles (GNOME/XWayland).
        from gi.repository import GLib

        GLib.timeout_add(200, lambda: (apply_desktop_hints(window, x, y, width, height), False)[1])
        GLib.timeout_add(1000, lambda: (apply_desktop_hints(window, x, y, width, height), False)[1])

    # Apply once the native X surface exists.
    if window.get_mapped():
        _apply()
    else:
        window.connect("map", _apply)


class _XClientMessageEvent(ctypes.Structure):
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


def _send_net_wm_state(x11, display, root, xid, action, atom1, atom2=0):
    """Send a _NET_WM_STATE client message (add/remove/toggle)."""
    _NET_WM_STATE = x11.XInternAtom(display, b"_NET_WM_STATE", False)
    event = _XClientMessageEvent()
    event.type = 33  # ClientMessage
    event.serial = 0
    event.send_event = 1
    event.display = display
    event.window = c_ulong(xid)
    event.message_type = _NET_WM_STATE
    event.format = 32
    event.data[0] = action  # 0=remove, 1=add, 2=toggle
    event.data[1] = atom1
    event.data[2] = atom2
    event.data[3] = 1  # source: application
    event.data[4] = 0

    # XSendEvent(display, w, propagate, mask, event)
    x11.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, c_void_p]
    mask = (1 << 20) | (1 << 19)  # SubstructureRedirectMask | SubstructureNotifyMask
    x11.XSendEvent(display, c_ulong(root), False, mask, ctypes.byref(event))


def apply_desktop_hints(window: Gtk.Window, x: int, y: int, width: int, height: int) -> None:
    xid = get_xid(window)
    if xid is None:
        logger.warning("[X11] No XID for wallpaper window; skipping desktop hints")
        return

    x11 = _load_x11()
    if x11 is None:
        return
    _bind_x11(x11)

    display = x11.XOpenDisplay(None)
    if not display:
        logger.warning("[X11] XOpenDisplay failed")
        return

    try:
        root = x11.XDefaultRootWindow(display)

        # Keep the window fixed at the monitor geometry.
        hints = _XSizeHints()
        hints.flags = _PPosition | _PSize | _PMinSize | _PMaxSize
        hints.x = int(x)
        hints.y = int(y)
        hints.width = int(width)
        hints.height = int(height)
        hints.min_width = hints.max_width = int(width)
        hints.min_height = hints.max_height = int(height)
        x11.XSetWMNormalHints(display, c_ulong(xid), byref(hints))

        x11.XMoveResizeWindow(
            display, c_ulong(xid), int(x), int(y), c_uint(int(width)), c_uint(int(height))
        )

        # _NET_WM_WINDOW_TYPE = DESKTOP
        type_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE", False)
        desktop_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DESKTOP", False)
        atom_val = c_ulong(desktop_atom)
        x11.XChangeProperty(
            display,
            c_ulong(xid),
            type_atom,
            _XA_ATOM,
            32,
            _PropModeReplace,
            ctypes.cast(byref(atom_val), c_void_p),
            1,
        )

        # Stick to all desktops.
        desktop_num_atom = x11.XInternAtom(display, b"_NET_WM_DESKTOP", False)
        all_desktops = c_ulong(0xFFFFFFFF)
        x11.XChangeProperty(
            display,
            c_ulong(xid),
            desktop_num_atom,
            _XA_CARDINAL,
            32,
            _PropModeReplace,
            ctypes.cast(byref(all_desktops), c_void_p),
            1,
        )

        # Window state: below normal windows, skip taskbar/pager; clear FULLSCREEN.
        # (GTK4 often stamps FULLSCREEN when the window is monitor-sized.)
        _NET_WM_STATE_ADD = 1
        _NET_WM_STATE_REMOVE = 0
        atom_below = x11.XInternAtom(display, b"_NET_WM_STATE_BELOW", False)
        atom_skip_taskbar = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", False)
        atom_skip_pager = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", False)
        atom_fullscreen = x11.XInternAtom(display, b"_NET_WM_STATE_FULLSCREEN", False)
        atom_above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", False)
        atom_maximized_horz = x11.XInternAtom(display, b"_NET_WM_STATE_MAXIMIZED_HORZ", False)
        atom_maximized_vert = x11.XInternAtom(display, b"_NET_WM_STATE_MAXIMIZED_VERT", False)

        _send_net_wm_state(x11, display, root, xid, _NET_WM_STATE_REMOVE, atom_fullscreen)
        _send_net_wm_state(
            x11, display, root, xid, _NET_WM_STATE_REMOVE, atom_maximized_horz, atom_maximized_vert
        )
        _send_net_wm_state(x11, display, root, xid, _NET_WM_STATE_REMOVE, atom_above)
        _send_net_wm_state(
            x11, display, root, xid, _NET_WM_STATE_ADD, atom_below, atom_skip_taskbar
        )
        _send_net_wm_state(x11, display, root, xid, _NET_WM_STATE_ADD, atom_skip_pager)

        # Also set the property directly as a belt-and-braces fallback.
        state_atoms = (c_ulong * 3)(atom_below, atom_skip_taskbar, atom_skip_pager)
        state_atom = x11.XInternAtom(display, b"_NET_WM_STATE", False)
        x11.XChangeProperty(
            display,
            c_ulong(xid),
            state_atom,
            _XA_ATOM,
            32,
            _PropModeReplace,
            ctypes.cast(state_atoms, c_void_p),
            3,
        )

        # Below normal windows.
        x11.XLowerWindow(display, c_ulong(xid))
        x11.XFlush(display)
        logger.debug(
            "[X11] Desktop window configured xid=%s geom=%sx%s+%s+%s",
            xid,
            width,
            height,
            x,
            y,
        )
    finally:
        x11.XCloseDisplay(display)
