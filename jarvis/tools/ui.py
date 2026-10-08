"""Windows desktop agent: operates ANY app (Settings, File Explorer, Office, Notepad, installers, dialogs...).

Perception: the target window's UI Automation tree (buttons, menus, fields, values) read in ~0.1 s; when an app exposes
            few controls (Electron/canvas apps) a screenshot is added so the vision model can point at things.
Safety:     destructive/commit clicks (delete, uninstall, send, pay...) need a spoken "yes"; password fields are never typed.
"""
import base64
import io
import logging
import re
import time

import comtypes
import comtypes.client
import pyautogui
import win32gui
from PIL import ImageGrab

from . import desktop, loop, tool
from .. import config

log = logging.getLogger("jarvis.ui")

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

CONTROL_TYPES = {getattr(UIA, n): n[4:-13] for n in dir(UIA) if n.startswith("UIA_") and n.endswith("ControlTypeId")}
SKIP_UNNAMED = {"Pane", "Group", "Custom", "Window", "TitleBar", "ScrollBar", "Thumb", "Separator", "ToolBar", "Image",
                "Header", "StatusBar", "Text", "List", "Tree", "Table", "DataGrid", "MenuBar", "Tab"}
RISKY = re.compile(r"\b(delete|remove|uninstall|format|erase|reset|wipe|send|pay|purchase|buy|place order|submit|"
                   r"sign out|log ?out|shut ?down|restart|discard|don.?t save|overwrite|permanently)\b", re.I)
MAX_ELEMENTS = 150
SPARSE = 15  # fewer named controls than this -> add a screenshot

SYSTEM = """You operate Windows desktop applications for your user ("Sir") to complete a task.
Each turn you get the task, previous actions, the open windows, and the FOREGROUND window's controls as
"[id] Type "name" = value" (from Windows UI Automation). Sometimes a screenshot is attached too.
Rules:
- Act with tools. Use ids from the CURRENT list only. Prefer click/type by id; use click_point (screenshot coordinates
  0-1000) only for things missing from the list.
- If the task needs another app, use open_app or switch_window first.
- Keyboard shortcuts are often fastest (ctrl+s, ctrl+f, alt+f4, win+r, menus via alt).
- Read results: "NOTHING CHANGED" or "BLOCKED" means try a different way.
- Missing information -> ask_user (one short friendly question). Never invent personal data or type passwords.
- Finish when done. The summary is SPOKEN: 1-2 natural sentences, no ids."""

DESKTOP_TOOLS = [
    loop.fn("click", "Click a control.", {"id": {"type": "integer"}, "double": {"type": "boolean"}, "right": {"type": "boolean"}}, ["id"]),
    loop.fn("click_point", "Click a point on the screenshot (x, y normalized 0-1000).",
            {"x": {"type": "integer"}, "y": {"type": "integer"}, "double": {"type": "boolean"}}, ["x", "y"]),
    loop.fn("type", "Type text (into control id if given, else into the focused field); enter=true presses Enter after.",
            {"text": {"type": "string"}, "id": {"type": "integer"}, "enter": {"type": "boolean"}}, ["text"]),
    loop.fn("press_keys", "Press a key or shortcut, e.g. enter, ctrl+s, alt+f4, tab.", {"keys": {"type": "string"}}),
    loop.fn("scroll", "Scroll (over control id if given).", {"direction": {"type": "string", "enum": ["up", "down"]}, "id": {"type": "integer"}}, ["direction"]),
    loop.fn("open_app", "Open an installed app.", {"name": {"type": "string"}}),
    loop.fn("switch_window", "Bring an open window to the front.", {"name": {"type": "string"}}),
]

_uia = None
_elements: dict[int, dict] = {}  # id -> control info from the latest observation


def _automation():
    global _uia
    if _uia is None:
        _uia = comtypes.CoCreateInstance(UIA.CUIAutomation._reg_clsid_, interface=UIA.IUIAutomation,
                                         clsctx=comtypes.CLSCTX_INPROC_SERVER)
    return _uia


def _target_window() -> tuple[int, str]:
    """The window that has focus (falls back to the top of the z-order); never Jarvis's own console."""
    windows = desktop.top_windows()
    if not windows:
        raise RuntimeError("no open windows")
    focused = win32gui.GetForegroundWindow()
    return next(((h, t) for h, t, _ in windows if h == focused), (windows[0][0], windows[0][1]))


def read_controls(hwnd: int) -> list[dict]:
    uia = _automation()
    request = uia.CreateCacheRequest()
    for prop in (UIA.UIA_NamePropertyId, UIA.UIA_ControlTypePropertyId, UIA.UIA_BoundingRectanglePropertyId,
                 UIA.UIA_IsOffscreenPropertyId, UIA.UIA_IsEnabledPropertyId, UIA.UIA_ValueValuePropertyId,
                 UIA.UIA_IsPasswordPropertyId, UIA.UIA_ToggleToggleStatePropertyId):
        request.AddProperty(prop)
    found = uia.ElementFromHandle(hwnd).FindAllBuildCache(UIA.TreeScope_Descendants, uia.CreateTrueCondition(), request)
    controls = []
    for i in range(found.Length):
        el = found.GetElement(i)
        rect = el.CachedBoundingRectangle
        if el.CachedIsOffscreen or rect.right - rect.left < 2 or rect.bottom - rect.top < 2:
            continue
        kind = CONTROL_TYPES.get(el.CachedControlType, "Control")
        name = (el.CachedName or "").strip()
        if not name and kind in SKIP_UNNAMED:
            continue
        try:
            value = el.GetCachedPropertyValue(UIA.UIA_ValueValuePropertyId)
            toggle = el.GetCachedPropertyValue(UIA.UIA_ToggleToggleStatePropertyId)
        except Exception:
            value, toggle = None, None
        controls.append({"type": kind, "name": name[:80], "value": str(value)[:60] if isinstance(value, str) and value else "",
                         "on": {0: "off", 1: "on"}.get(toggle, ""), "enabled": bool(el.CachedIsEnabled),
                         "password": bool(el.GetCachedPropertyValue(UIA.UIA_IsPasswordPropertyId)),
                         "center": ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)})
        if len(controls) >= MAX_ELEMENTS:
            break
    return controls


def _screenshot() -> str:
    image = ImageGrab.grab().convert("RGB")
    image.thumbnail((1280, 1280))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=75)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _observe():
    time.sleep(0.4)  # let the UI react to the previous action
    hwnd, title = _target_window()
    controls = read_controls(hwnd)
    _elements.clear()
    lines = []
    for i, c in enumerate(controls, 1):
        _elements[i] = c
        extra = (f" = {c['value']!r}" if c["value"] else "") + (f" [{c['on']}]" if c["on"] else "") + \
                ("" if c["enabled"] else " [disabled]") + (" [password]" if c["password"] else "")
        lines.append(f"[{i}] {c['type']} {c['name']!r}{extra}")
    others = [t[:50] for _, t, _ in desktop.top_windows()[1:12]]
    named = sum(1 for c in controls if c["name"])
    view = (f"OPEN WINDOWS: {others}\nFOREGROUND WINDOW: {title!r}\nCONTROLS:\n" + ("\n".join(lines) or "(none exposed)"))
    return (title, tuple(lines)), view, _screenshot() if named < SPARSE else None


def _act(ctx, name: str, args: dict) -> tuple[str, bool]:
    if name in ("click", "type", "scroll") and args.get("id") is not None:
        control = _elements.get(int(args["id"]))
        if control is None:
            return f"ERROR: no control with id {args['id']} in the current list", False
    else:
        control = None

    if name == "click":
        if RISKY.search(control["name"]) and not ctx.confirm(f"Sir, this will {control['name'][:60]}. Should I go ahead?"):
            return "CANCELLED by user", False
        x, y = control["center"]
        pyautogui.click(x, y, clicks=2 if args.get("double") else 1, button="right" if args.get("right") else "left")
        return f"clicked {control['type']} {control['name']!r}", False
    if name == "click_point":
        width, height = pyautogui.size()
        x, y = int(args["x"]) * width // 1000, int(args["y"]) * height // 1000
        pyautogui.click(x, y, clicks=2 if args.get("double") else 1)
        return f"clicked at ({x}, {y})", False
    if name == "type":
        if control is not None:
            if control["password"]:
                ctx.say("That's a password field, so please type it yourself.")
                ctx.confirm("Just say yes when you're done.")
                return "user typed the password manually", False
            pyautogui.click(*control["center"])
        desktop.type_text(ctx, args["text"])
        if args.get("enter"):
            pyautogui.press("enter")
        return f"typed {args['text'][:60]!r}", not args.get("enter")
    if name == "press_keys":
        return desktop.press_keys(ctx, args["keys"]), False
    if name == "scroll":
        if control is not None:
            pyautogui.moveTo(*control["center"])
        return desktop.scroll(ctx, args["direction"]), False
    if name == "open_app":
        before = win32gui.GetForegroundWindow()
        result = desktop.open_app(ctx, args["name"])
        for _ in range(25):  # wait (max 5 s) until the new app's window takes focus
            time.sleep(0.2)
            if win32gui.GetForegroundWindow() != before:
                break
        time.sleep(0.5)  # let its controls finish loading
        return result, False
    if name == "switch_window":
        return desktop.switch_to_window(ctx, args["name"]), False
    return f"ERROR: unknown action {name}", False


@tool("Operate any Windows desktop app step by step: change settings, use menus and dialogs, fill forms in apps, "
      "work in File Explorer / Office / Notepad / installers. (For websites use browser_task.)",
      {"goal": {"type": "string", "description": "Full task with every detail the user gave"}}, slow=True)
def desktop_task(ctx, goal: str) -> str:
    return loop.run(ctx, goal, SYSTEM, DESKTOP_TOOLS, _observe, lambda name, args: _act(ctx, name, args),
                    config.MAX_DESKTOP_STEPS)
