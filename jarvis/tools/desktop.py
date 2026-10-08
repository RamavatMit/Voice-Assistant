"""Windows tools: apps, windows, typing, keys, PowerShell commands, system controls, timers, folders.

Everything is generic: apps are found via the Start menu and each program's own file description, never by a hardcoded list.
"""
import ctypes
import difflib
import json
import os
import re
import subprocess
import threading
import time

import psutil
import pyautogui
import pyperclip
import win32api
import win32con
import win32gui
import win32process

from . import tool
from .. import config, tts

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort any automation

# Never closed by voice: Windows itself, security, and the processes Jarvis runs in
PROTECTED = {"explorer", "csrss", "winlogon", "wininit", "lsass", "services", "svchost", "smss", "dwm", "system",
             "registry", "python", "pythonw", "py", "conhost", "windowsterminal", "openconsole", "cmd", "powershell",
             "taskmgr", "msmpeng", "fontdrvhost", "sihost", "ctfmon", "searchhost", "startmenuexperiencehost", "lockapp"}
DANGEROUS_SYSTEM_ACTIONS = {"shutdown", "restart", "sleep", "sign_out"}
FOLDERS = {"downloads": "~/Downloads", "documents": "~/Documents", "desktop": "~/Desktop", "pictures": "~/Pictures",
           "music": "~/Music", "videos": "~/Videos", "home": "~", "code": config.CODE_WORKSPACE}

_installed_apps: list[dict] | None = None


def _similarity(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if re.search(rf"\b{re.escape(a)}\b", b):
        return 0.9
    if a.replace(" ", "") in b.replace(" ", ""):
        return 0.8
    return difflib.SequenceMatcher(None, a, b).ratio()


def _installed() -> list[dict]:
    """Installed Win32 + Store apps from the Start menu (cached)."""
    global _installed_apps
    if _installed_apps is None:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=15)
        data = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else []
        _installed_apps = data if isinstance(data, list) else [data]
    return _installed_apps


@tool("Open any installed Windows application by name.", {"name": {"type": "string"}})
def open_app(ctx, name: str) -> str:
    global _installed_apps
    for attempt in range(2):
        ranked = sorted(_installed(), key=lambda a: (_similarity(name, a.get("Name", "")), -len(a.get("Name", ""))), reverse=True)
        if ranked and _similarity(name, ranked[0].get("Name", "")) >= 0.75:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{ranked[0]['AppID']}"])
            return f"Opened {ranked[0]['Name']}"
        _installed_apps = None  # maybe just installed: refresh once
    return f"ERROR: '{name}' is not installed. Closest: {[a.get('Name') for a in ranked[:3]]}."


def _exe_description(path: str) -> str:
    """The program's own display name (e.g. Code.exe -> 'Visual Studio Code')."""
    try:
        lang, codepage = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        return win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription") or ""
    except Exception:
        return ""


def top_windows() -> list[tuple[int, str, int]]:
    """Visible top-level windows as (hwnd, title, pid), excluding Jarvis's own console."""
    console = ctypes.windll.kernel32.GetConsoleWindow()
    found = []

    def collect(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if hwnd != console and title and win32gui.IsWindowVisible(hwnd) and not win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            found.append((hwnd, title, win32process.GetWindowThreadProcessId(hwnd)[1]))
    win32gui.EnumWindows(collect, None)
    return found


@tool("Close an application or window gracefully (the app may still ask to save unsaved work).",
      {"name": {"type": "string", "description": "App name or words from the window title"}})
def close_app(ctx, name: str) -> str:
    own = {psutil.Process().pid, *(p.pid for p in psutil.Process().parents())}
    best, targets = 0.0, []
    for hwnd, title, pid in top_windows():
        try:
            proc = psutil.Process(pid)
            exe_name = proc.name().lower().removesuffix(".exe")
            if pid in own or exe_name in PROTECTED:
                continue
            score = max(_similarity(name, exe_name), _similarity(name, _exe_description(proc.exe())), _similarity(name, title))
        except psutil.Error:
            continue
        if score > best + 0.05:
            best, targets = score, [(hwnd, title)]
        elif abs(score - best) <= 0.05:
            targets.append((hwnd, title))
    if best < 0.7:
        return f"ERROR: no open window matches '{name}'"
    for hwnd, _ in targets:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return f"Closed {', '.join(t for _, t in targets)[:200]}"


@tool("Bring an already open window to the front.", {"name": {"type": "string", "description": "App name or title words"}})
def switch_to_window(ctx, name: str) -> str:
    windows = top_windows()
    if not windows:
        return "ERROR: no windows open"
    hwnd, title, _ = max(windows, key=lambda w: _similarity(name, w[1]))
    if _similarity(name, title) < 0.5:
        return f"ERROR: no window matches '{name}'. Open: {[w[1][:40] for w in windows[:10]]}"
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    pyautogui.press("alt")  # Windows only lets the foreground process change focus; a key press grants it
    win32gui.SetForegroundWindow(hwnd)
    return f"Switched to {title}"


@tool("Type text into the currently focused window/field.", {"text": {"type": "string"}})
def type_text(ctx, text: str) -> str:
    previous = pyperclip.paste()
    pyperclip.copy(text)  # paste is instant and Unicode-safe (pyautogui.write drops non-ASCII)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    pyperclip.copy(previous)
    return f"Typed {len(text)} characters"


@tool("Press a key or shortcut, e.g. 'enter', 'ctrl+s', 'alt+tab', 'win+r'.",
      {"keys": {"type": "string"}, "times": {"type": "integer", "description": "repeat count, default 1"}},
      required=["keys"])
def press_keys(ctx, keys: str, times: int = 1) -> str:
    parts = [k.strip().lower().replace("control", "ctrl").replace("windows", "win") for k in keys.split("+")]
    unknown = [k for k in parts if k not in pyautogui.KEYBOARD_KEYS]
    if unknown:
        return f"ERROR: unknown key(s) {unknown}"
    for _ in range(max(1, min(int(times or 1), 50))):
        pyautogui.hotkey(*parts) if len(parts) > 1 else pyautogui.press(parts[0])
    return f"Pressed {keys}"


@tool("Scroll the window under the mouse.", {"direction": {"type": "string", "enum": ["up", "down"]}})
def scroll(ctx, direction: str) -> str:
    pyautogui.scroll(600 if direction == "up" else -600)
    return f"Scrolled {direction}"


# ---- PowerShell -------------------------------------------------------------------------------------------------
_READ_ONLY = re.compile(
    r"^\s*(get-\w+|test-\w+|select(-object)?|sort(-object)?|where(-object)?|measure(-object)?|format-\w+|ft|fl|"
    r"out-string|resolve-dnsname|write-output|echo|dir|ls|gci|gps|gc|cat|type|ipconfig|systeminfo|tasklist|whoami|"
    r"hostname|ping|nslookup|tracert|netstat|where\.exe|\w+\s+--version|python -V|git (status|log|diff|branch))\b",
    re.I)
# Anything that could hide or perform a change: sub-expressions, redirection, call operator, modifying verbs
_MUTATING = re.compile(r"\$\(|`|>|&|@\(|\b(set|new|remove|stop|start|invoke|move|copy|rename|clear|install|uninstall|"
                       r"enable|disable|add|update|restart|suspend|out-file|tee|kill|del|rm|rmdir|erase)\b|"
                       r"\.(delete|kill|write\w*)\(", re.I)
_FORBIDDEN = re.compile(
    r"format-volume|\bformat\s+[a-z]:|diskpart|bcdedit|cipher\s+/w|vssadmin|clear-disk|initialize-disk|"
    r"reg(\.exe)?\s+delete\s+hklm|set-executionpolicy|-enc(odedcommand)?\b|downloadstring|invoke-expression|\biex\b|"
    r"remove-item.*(\\windows|\\program files|\\system32|^\s*c:\\?\s*$)|takeown|icacls.*\/grant|disable-.*defender|"
    r"set-mppreference", re.I)


def is_read_only(command: str) -> bool:
    """True only for plainly read-only commands; everything else needs the user's spoken yes."""
    if _MUTATING.search(command):
        return False
    return all(_READ_ONLY.match(part) for part in re.split(r"[|;\n]", command) if part.strip())


@tool("Run a PowerShell command on this PC (files, folders, system info, network, processes, installs, git, etc.). "
      "Prefer read-only commands to answer questions. Output is returned to you.",
      {"command": {"type": "string"}, "purpose": {"type": "string", "description": "short plain-English purpose, spoken to the user"}},
      confirm_if=lambda a: not is_read_only(a.get("command", "")),
      confirm_text=lambda a: f"Sir, may I run a command to {a.get('purpose') or 'do that'}?")
def run_command(ctx, command: str, purpose: str) -> str:
    if _FORBIDDEN.search(command):
        return "ERROR: refused - this command could damage the system or bypass security."
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True,
                         text=True, timeout=120, cwd=os.path.expanduser("~"), creationflags=subprocess.CREATE_NO_WINDOW)
    output = (res.stdout.strip() + ("\nSTDERR: " + res.stderr.strip() if res.stderr.strip() else ""))[-1500:]
    return f"exit code {res.returncode}\n{output or '(no output)'}"


# ---- System controls ------------------------------------------------------------------------------------------
class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def _powershell(command: str) -> str:
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                         capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:200] or "PowerShell command failed")
    return res.stdout.strip()


def _number(value, default: int, low: int, high: int) -> int:
    match = re.search(r"\d+", str(value or ""))
    return max(low, min(high, int(match.group()) if match else default))


SYSTEM_ACTIONS = ["volume_up", "volume_down", "set_volume", "mute", "play_pause", "next_track", "previous_track",
                  "brightness_up", "brightness_down", "set_brightness", "show_desktop", "minimize_window",
                  "maximize_window", "lock", "screenshot", "battery", "shutdown", "restart", "cancel_shutdown",
                  "sleep", "sign_out"]


@tool("Laptop controls. value = percent for set_volume/set_brightness, steps of 2% for volume_up/down (default 5), "
      "percent change for brightness_up/down (default 20).",
      {"action": {"type": "string", "enum": SYSTEM_ACTIONS}, "value": {"type": "integer"}}, required=["action"],
      confirm_if=lambda a: a.get("action") in DANGEROUS_SYSTEM_ACTIONS,
      confirm_text=lambda a: f"Sir, do you really want me to {a['action'].replace('_', ' ')} the laptop?")
def system_control(ctx, action: str, value: int | None = None) -> str:
    if action in ("volume_up", "volume_down"):
        pyautogui.press("volumeup" if action == "volume_up" else "volumedown", presses=_number(value, 5, 1, 50), interval=0.01)
    elif action == "set_volume":
        pyautogui.press("volumedown", presses=50, interval=0.005)
        pyautogui.press("volumeup", presses=round(_number(value, 50, 0, 100) / 2), interval=0.005)
    elif action == "mute":
        pyautogui.press("volumemute")
    elif action in ("play_pause", "next_track", "previous_track"):
        pyautogui.press({"play_pause": "playpause", "next_track": "nexttrack", "previous_track": "prevtrack"}[action])
    elif action in ("brightness_up", "brightness_down", "set_brightness"):
        if action == "set_brightness":
            level = _number(value, 50, 0, 100)
        else:
            current = int(_powershell("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness") or 50)
            delta = _number(value, 20, 1, 100)
            level = max(0, min(100, current + (delta if action == "brightness_up" else -delta)))
        _powershell("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
                    f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}}")
        return f"Brightness set to {level}%"
    elif action == "show_desktop":
        pyautogui.hotkey("win", "d")
    elif action == "minimize_window":
        pyautogui.hotkey("win", "down")
    elif action == "maximize_window":
        pyautogui.hotkey("win", "up")
    elif action == "lock":
        ctypes.windll.user32.LockWorkStation()
    elif action == "screenshot":
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"jarvis_{time.strftime('%Y%m%d_%H%M%S')}.png")
        pyautogui.screenshot().save(path)
        return f"Screenshot saved to {path}"
    elif action == "battery":
        status = _PowerStatus()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        if status.BatteryLifePercent == 255:
            return "No battery detected"
        return f"Battery {status.BatteryLifePercent}%, {'charging' if status.ACLineStatus == 1 else 'on battery'}"
    elif action in ("shutdown", "restart"):
        subprocess.run(["shutdown", "/s" if action == "shutdown" else "/r", "/t", "30"], check=True)
        return f"{action.title()} in 30 seconds (say 'cancel shutdown' to stop)"
    elif action == "cancel_shutdown":
        subprocess.run(["shutdown", "/a"], capture_output=True)
    elif action == "sign_out":
        subprocess.run(["shutdown", "/l"], check=True)
    elif action == "sleep":
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=True)
    return f"{action} done"


def _announce(message: str) -> None:
    import winsound
    for _ in range(3):
        winsound.Beep(1200, 150)
    print(f"\n[REMINDER] {message}")
    tts.speak(f"Hey, just a reminder: {message}")


@tool("Set a timer/reminder. For a clock time, compute seconds from the current time.",
      {"seconds": {"type": "integer"}, "message": {"type": "string"}})
def set_timer(ctx, seconds: int, message: str) -> str:
    seconds = int(seconds)
    if not 0 < seconds <= 24 * 3600:
        return "ERROR: timer must be between 1 second and 24 hours"
    timer = threading.Timer(seconds, _announce, args=(message,))
    timer.daemon = True
    timer.start()
    return f"Reminder '{message}' set for {seconds // 60} min {seconds % 60} s from now"


@tool("Open a common folder in File Explorer.", {"folder": {"type": "string", "enum": list(FOLDERS)}})
def open_folder(ctx, folder: str) -> str:
    path = os.path.abspath(os.path.expanduser(FOLDERS[folder]))
    os.makedirs(path, exist_ok=True)
    os.startfile(path)  # a directory -> opens Explorer (never executes anything)
    return f"Opened {path}"
