"""System-wide hotkeys via Win32 RegisterHotKey (works in every app, no extra dependency).

Ctrl+Alt+J  push-to-talk (start listening without the wake word)
Ctrl+Alt+X  stop whatever Jarvis is doing / saying
"""
import ctypes
import ctypes.wintypes
import logging
import threading

from .events import HUB

log = logging.getLogger("jarvis.hotkey")

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312
HOTKEYS = {1: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("J"), "talk"),
           2: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("X"), "stop")}


def _listen() -> None:
    user32 = ctypes.windll.user32
    for hotkey_id, (mods, key, name) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hotkey_id, mods, key):
            log.warning("Hotkey for '%s' is already taken by another app", name)
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
            action = HOTKEYS[msg.wParam][2]
            if action == "stop":
                HUB.stop.set()
            else:
                HUB.commands.put(("talk", None))


def start() -> None:
    threading.Thread(target=_listen, daemon=True, name="hotkeys").start()
