"""Always-on-top HUD: a draggable 3D orb + status panel in a screen corner (pywebview / WebView2).

The window has ONE fixed size; switching between orb / compact / full only changes its visible shape with a Win32
window region (a circle or a rounded rectangle anchored to the bottom-right). Outside the shape the window is
invisible and clicks pass through. (Resizing is avoided on purpose: WebView2 can keep showing a stale frame after a
large shrink, and it ignores per-pixel transparency.)
JS polls `Bridge.poll()` ~10x/s for events from the core (`events.HUB`); user actions go back through the hub's
command / answer queues, so the GUI never touches the assistant's worker thread.
"""
import ctypes
import ctypes.wintypes
import os
import threading

import webview
import win32con
import win32gui

from .. import config
from ..events import HUB

# Per-monitor DPI awareness, declared before anything else (pyautogui would otherwise switch modes mid-run and
# pywebview's coordinate maths would then place the window off-screen). Geometry is handled in physical pixels below.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = "Jarvis HUD"
WIDTH, HEIGHT = 380, 540  # logical (DPI-independent) pixels
# visible area per mode, (left, top, width, height) in logical px - must match hud.css
MODES = {"orb": (WIDTH - 120, HEIGHT - 120, 120, 120), "compact": (0, HEIGHT - 150, WIDTH, 150), "full": (0, 0, WIDTH, HEIGHT)}
RADIUS = 28
MARGIN = 16


class Bridge:
    """Methods callable from JavaScript as `pywebview.api.<name>(...)`. (Private `_` attributes are not exposed.)"""

    def __init__(self):
        self._window = None
        self.mode = config.load_settings().get("hud_mode", "compact")

    # core <-> UI
    def poll(self, since: int) -> dict:
        return {"events": HUB.since(int(since)), "level": HUB.level, "state": HUB.state,
                "mic_muted": HUB.mic_muted, "voice_muted": HUB.voice_muted}

    def send(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            HUB.answers.put(text) if HUB.state == "question" else HUB.commands.put(("text", text))

    def talk(self) -> None:
        HUB.commands.put(("talk", None))

    def answer(self, yes: bool) -> None:
        HUB.answers.put(bool(yes))

    def stop(self) -> None:
        HUB.stop.set()

    def mute_mic(self, muted: bool) -> None:
        HUB.commands.put(("mute_mic", bool(muted)))

    def mute_voice(self, muted: bool) -> None:
        HUB.commands.put(("mute_voice", bool(muted)))

    # window
    def get_mode(self) -> str:
        return self.mode

    def set_mode(self, mode: str) -> str:
        if mode in MODES:
            self.mode = mode
            _shape(mode)
            config.save_settings({"hud_mode": mode})
        return self.mode

    def move_by(self, dx: float, dy: float) -> None:
        """Drag: dx/dy arrive in CSS pixels; the window is positioned in physical pixels."""
        hwnd = _hwnd()
        scale = _scale(hwnd)
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        _place(hwnd, left + round(dx * scale), top + round(dy * scale))

    def save_position(self) -> None:
        _, _, right, bottom = win32gui.GetWindowRect(_hwnd())
        config.save_settings({"hud_corner_px": [right, bottom]})

    def close(self) -> None:
        HUB.stop.set()
        self._window.destroy()


def _hwnd() -> int:
    return win32gui.FindWindow(None, TITLE)


def _scale(hwnd: int) -> float:
    return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0 if hwnd else 1.0


def _work_area() -> tuple[int, int, int, int]:
    """Screen area without the taskbar, in physical pixels."""
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def _place(hwnd: int, x: int, y: int) -> None:
    """Position + size the window in exact physical pixels (independent of pywebview's DPI handling)."""
    scale = _scale(hwnd)
    width, height = round(WIDTH * scale), round(HEIGHT * scale)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, int(x), int(y), width, height, win32con.SWP_NOACTIVATE)


def _initial_place(hwnd: int) -> None:
    """Bottom-right corner of the work area, or where the user last dragged it (kept on screen)."""
    scale = _scale(hwnd)
    width, height = round(WIDTH * scale), round(HEIGHT * scale)
    left, top, right, bottom = _work_area()
    margin = round(MARGIN * scale)
    corner = config.load_settings().get("hud_corner_px", [right - margin, bottom - margin])
    x = min(max(corner[0], left + round(140 * scale)), right) - width
    y = min(max(corner[1], top + round(140 * scale)), bottom) - height
    _place(hwnd, x, y)


def _shape(mode: str) -> None:
    """Make only the mode's area visible (and clickable): a circle for the orb, a rounded rectangle for panels."""
    hwnd = _hwnd()
    if not hwnd:
        return
    # pywebview enables Windows 11 "Mica" in dark mode; Mica paints the window's FULL rectangle behind the region,
    # so the clipped-away area would show a blurred backdrop instead of the desktop. Backdrop type 1 = none.
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(ctypes.c_int(1)), 4)
    scale = _scale(hwnd)
    left, top, width, height = (round(v * scale) for v in MODES[mode])
    gdi = ctypes.windll.gdi32
    if mode == "orb":
        region = gdi.CreateEllipticRgn(left, top, left + width + 1, top + height + 1)
    else:
        radius = round(RADIUS * 2 * scale)
        region = gdi.CreateRoundRectRgn(left, top, left + width + 1, top + height + 1, radius, radius)
    ctypes.windll.user32.SetWindowRgn(hwnd, region, True)


def run(start_core) -> None:
    """Open the HUD on the main thread; `start_core` launches the assistant worker thread."""
    bridge = Bridge()
    window = webview.create_window(
        TITLE, url=os.path.join(HERE, "index.html"), js_api=bridge, width=WIDTH, height=HEIGHT,
        x=0, y=0, min_size=(WIDTH, HEIGHT), frameless=True, on_top=True,
        easy_drag=False, shadow=False, resizable=False, background_color="#060a16")
    bridge._window = window
    window.events.loaded += lambda: _shape(bridge.mode)
    HUB.ui_attached = True

    def started():
        window.events.shown.wait(15)  # pywebview applies its own position when showing; place ours afterwards
        _initial_place(_hwnd())
        _shape(bridge.mode)
        threading.Thread(target=start_core, daemon=True, name="jarvis-core").start()

    webview.start(started, gui="edgechromium", private_mode=False, storage_path=os.path.join(config.ROOT, ".hud_cache"))
