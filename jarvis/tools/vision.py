"""Screen understanding with Gemini vision: click a described element, or answer questions about the screen."""
import base64
import io
import json
import re

import pyautogui
from PIL import ImageGrab

from . import tool
from .. import config, llm


def _screenshot_data_url() -> str:
    image = ImageGrab.grab().convert("RGB")
    image.thumbnail((1600, 1600))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _ask_vision(prompt: str, json_mode: bool = False) -> str:
    message = llm.chat(config.VISION_MODELS, [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _screenshot_data_url()}}]}], json_mode=json_mode)
    return (message.content or "").strip()


@tool("Click a visible element on screen described in words (for desktop apps; prefer browser_task for websites).",
      {"target": {"type": "string", "description": "e.g. 'the blue Sign in button', 'Save in the File menu'"},
       "double": {"type": "boolean"}}, required=["target"])
def screen_click(ctx, target: str, double: bool = False) -> str:
    raw = _ask_vision(
        f'Find this UI element in the screenshot: "{target}". Respond ONLY with JSON '
        '{"found": true|false, "box_2d": [ymin, xmin, ymax, xmax]} using coordinates normalized to 0-1000.', json_mode=True)
    match = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(match.group()) if match else {}
    box = data.get("box_2d")
    if not data.get("found") or not isinstance(box, list) or len(box) != 4:
        return f"ERROR: could not find '{target}' on screen"
    ymin, xmin, ymax, xmax = (float(v) for v in box)
    width, height = pyautogui.size()  # normalized coords are resolution- and DPI-independent
    x, y = int((xmin + xmax) / 2000 * width), int((ymin + ymax) / 2000 * height)
    if not (0 < x < width - 1 and 0 < y < height - 1):
        return f"ERROR: '{target}' location is outside the screen"
    pyautogui.moveTo(x, y, duration=0.15)
    pyautogui.doubleClick() if double else pyautogui.click()
    return f"Clicked '{target}' at ({x}, {y})"


@tool("Look at the screen to answer a question about it (read text, describe, find information).",
      {"question": {"type": "string"}})
def look_at_screen(ctx, question: str) -> str:
    return _ask_vision(f"Answer briefly, in at most 3 sentences, about this screenshot: {question}")
