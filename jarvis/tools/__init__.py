"""Tool registry: every capability the brain can use, with its JSON schema and safety policy.

Safety is enforced here, centrally, not by the LLM:
  - tools marked `confirm=True` (or matching `confirm_if`) always get a spoken yes/no first
  - every tool runs inside try/except and returns a short result string the brain can reason about
"""
import json
import logging
from dataclasses import dataclass
from typing import Callable

from .. import config
from ..events import HUB

log = logging.getLogger("jarvis.tools")


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list
    fn: Callable
    confirm: bool = False
    confirm_if: Callable[[dict], bool] | None = None
    confirm_text: Callable[[dict], str] | None = None
    slow: bool = False  # takes several seconds: Jarvis acknowledges first so silence never feels like a hang

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": self.required}}}


REGISTRY: dict[str, Tool] = {}


def tool(description: str, params: dict | None = None, required: list | None = None, confirm: bool = False,
         confirm_if: Callable[[dict], bool] | None = None, confirm_text: Callable[[dict], str] | None = None,
         slow: bool = False):
    """Register a function as a tool. `params` maps name -> JSON schema; all are required unless `required` is given."""
    def register(fn):
        props = params or {}
        REGISTRY[fn.__name__] = Tool(fn.__name__, description, props, list(props) if required is None else required,
                                     fn, confirm, confirm_if, confirm_text, slow)
        return fn
    return register


def schemas() -> list[dict]:
    return [t.schema for t in REGISTRY.values()]


class Context:
    """What tools may use to interact with the user (voice or keyboard)."""

    def __init__(self, voice):
        self.voice = voice

    def say(self, text: str) -> None:
        self.voice.say(text)

    def acknowledge(self) -> None:
        self.voice.acknowledge()

    def ask(self, question: str) -> str | None:
        return self.voice.ask(question)

    def confirm(self, question: str) -> bool:
        return self.voice.confirm(question)


def describe_call(name: str, args: dict) -> str:
    shown = ", ".join(f"{v}" for v in args.values() if v not in (None, ""))
    return f"{name.replace('_', ' ')} {shown}".strip()


def execute(name: str, raw_args: str | dict, ctx: Context) -> str:
    """Run a tool call from the LLM and return a compact result string (never raises)."""
    entry = REGISTRY.get(name)
    if entry is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return "ERROR: tool arguments were not valid JSON"
    args = {k: v for k, v in args.items() if k in entry.params}
    missing = [p for p in entry.required if args.get(p) in (None, "")]
    if missing:
        return f"ERROR: missing required argument(s): {', '.join(missing)}"

    if config.CONFIRM_ALL or entry.confirm or (entry.confirm_if and entry.confirm_if(args)):
        question = entry.confirm_text(args) if entry.confirm_text else f"Sir, should I {describe_call(name, args)}?"
        if not ctx.confirm(question):
            return "CANCELLED: the user said no. Do not retry."
    elif entry.slow:
        ctx.acknowledge()
    if HUB.stop.is_set():
        return "CANCELLED: the user pressed stop."

    log.info("TOOL %s %s", name, args)
    label = describe_call(name, args)[:90]
    HUB.set_state("working", label)
    HUB.emit("tool", name=name, label=label, phase="start")
    try:
        result = entry.fn(ctx, **args)
    except Exception as e:
        log.info("Tool %s failed", name, exc_info=True)  # traceback goes to jarvis.log, not the console
        result = f"ERROR: {type(e).__name__}: {e}"
    result = str(result if result is not None else "done")
    log.info("TOOL %s -> %s", name, result[:300])
    HUB.emit("tool", name=name, label=label, phase="end", ok=not result.startswith("ERROR"), result=result[:160])
    return result[:2000]


# Import tool modules so they register themselves
from . import desktop, web, ui, vision, coder  # noqa: E402,F401
