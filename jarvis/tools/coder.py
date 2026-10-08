"""Code assistant: writes complete programs to the code workspace and opens them in VS Code (never executes them)."""
import json
import os
import re
import shutil
import subprocess

from . import tool
from .. import config, llm

SYSTEM = (
    "You are a senior software engineer. Write complete, working, well-structured code for the task in ONE file. "
    'Respond ONLY with JSON: {"filename": "short_name.ext", "content": "<full file>", "summary": "<one spoken sentence>"}. '
    "Put brief usage instructions in a comment at the top. No markdown fences inside content."
)


def _safe_path(filename: str) -> str:
    """Sanitized, non-existing path inside CODE_WORKSPACE (no traversal, never overwrites)."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "")).lstrip(".") or "solution.txt"
    os.makedirs(config.CODE_WORKSPACE, exist_ok=True)
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(config.CODE_WORKSPACE, name), 1
    while os.path.exists(path):
        path, n = os.path.join(config.CODE_WORKSPACE, f"{stem}_{n}{ext}"), n + 1
    return path


@tool("Write a complete program/script/web page for a coding task, save it and open it in VS Code.",
      {"task": {"type": "string", "description": "Full detailed description of what to build"},
       "language": {"type": "string"}}, required=["task"], slow=True)
def write_code(ctx, task: str, language: str = "") -> str:
    prompt = f"Task: {task}" + (f"\nLanguage: {language}" if language else "")
    message = llm.chat(config.CODER_MODELS, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                       json_mode=True, timeout=90, max_wait=30)
    result = json.loads(message.content)
    content = str(result.get("content") or "").strip()
    if not content:
        return "ERROR: the model returned an empty file"
    path = _safe_path(str(result.get("filename") or ""))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content + "\n")
    editor = shutil.which("code")
    subprocess.Popen([editor, "--reuse-window", path] if editor else ["notepad.exe", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Saved {path} and opened it for review. {result.get('summary', '')}"
