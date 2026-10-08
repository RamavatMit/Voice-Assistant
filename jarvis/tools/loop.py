"""Shared observe -> decide -> act loop used by the Chrome agent and the Windows desktop agent.

Each agent supplies only how it sees (`observe`) and how it acts (`act`); this loop handles the model calls,
action history, "nothing changed" detection, blocking of repeated failures, questions to the user and finishing.
"""
import json
import logging
import time
from typing import Callable

from .. import config, llm
from ..events import HUB

log = logging.getLogger("jarvis.agentloop")


def fn(name: str, description: str, params: dict, required: list | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": params, "required": list(params) if required is None else required}}}


COMMON_TOOLS = [
    fn("wait", "Wait for the screen to update (loading).", {"seconds": {"type": "number"}}),
    fn("ask_user", "Ask the user a short question and get their spoken answer.", {"question": {"type": "string"}}),
    fn("finish", "End the task.", {"success": {"type": "boolean"}, "summary": {"type": "string"}}),
]
NO_CHANGE = (" -> BUT NOTHING CHANGED (no effect: check for an unselected suggestion, an error message, a dialog or a "
             "required field; do not repeat the same action)")
BLOCKED = ("BLOCKED: this exact action already failed or had no effect twice. Choose a DIFFERENT action "
           "(another element, keyboard keys, another route) or finish with success=false.")


def run(ctx, goal: str, system: str, tools: list, observe: Callable, act: Callable, max_steps: int) -> str:
    """observe() -> (state_key, text_view, image_data_url | None); act(name, args) -> (result, may_continue_batch)."""
    history: list[str] = []
    failures: dict[str, int] = {}
    last_keys: list[str] = []
    previous = None
    for step in range(1, max_steps + 1):
        if HUB.stop.is_set():
            return "Okay, I stopped there."
        try:
            state, view, image = observe()
        except Exception as e:
            history.append(f"(could not read the screen: {str(e)[:120]})")
            time.sleep(1)
            continue
        if history and state == previous:
            history[-1] += NO_CHANGE
            for key in last_keys:
                failures[key] = failures.get(key, 0) + 1
        previous, last_keys = state, []

        text = (f"TASK: {goal}\nNow: {time.strftime('%A %d %B %Y, %I:%M %p')}\n\nPREVIOUS ACTIONS:\n"
                + ("\n".join(history[-20:]) or "(none)") + f"\n\n{view}")
        content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image}}] if image else text
        try:
            message = llm.chat(config.WEB_MODELS, [{"role": "system", "content": system}, {"role": "user", "content": content}],
                               tools=tools + COMMON_TOOLS, max_wait=30)
        except llm.AllModelsFailed as e:
            return f"ERROR: AI models unavailable right now ({str(e)[:150]})"
        calls = message.tool_calls or []
        if not calls:
            history.append(f"{step}. (no action; you said {(message.content or '')[:120]!r}) -> act with a tool")
            continue

        for call in calls[:3]:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "finish":
                summary = args.get("summary") or "Done."
                log.info("Agent finished (success=%s): %s", args.get("success"), summary)
                return summary if args.get("success", True) else f"I couldn't complete it: {summary}"
            key = f"{name}:{json.dumps(args, sort_keys=True)}"
            last_keys.append(key)
            may_continue = False
            if failures.get(key, 0) >= 2 and name not in ("wait", "ask_user"):
                result = BLOCKED
            elif name == "wait":
                time.sleep(min(float(args.get("seconds") or 2), 8))
                result = "waited"
            elif name == "ask_user":
                answer = ctx.ask(args.get("question") or "How should I continue?")
                result = f"user answered: {answer!r}" if answer else "user did not answer"
            else:
                try:
                    result, may_continue = act(name, args)
                except Exception as e:
                    failures[key] = failures.get(key, 0) + 1
                    result = f"ERROR: {str(e).splitlines()[0][:200]}"
            log.info("step %d: %s %s -> %s", step, name, args, result)
            history.append(f"{step}. {name} {json.dumps(args, ensure_ascii=False)} -> {result}")
            HUB.emit("step", step=step, text=f"{name.replace('_', ' ')}: {result[:100]}")
            if result.startswith("CANCELLED"):
                return "I stopped before the final step, as you asked. Everything is ready for you on screen."
            if not may_continue:
                break
    return "I couldn't finish within the step limit; I've left everything where I stopped."
