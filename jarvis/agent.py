"""The brain: a tool-calling agent loop with short conversation memory.

One user command -> LLM decides tool calls -> tools run (safety-gated) -> results go back -> LLM speaks a short reply.
Multi-step commands ("open notepad and type hello") and follow-ups ("now close it") fall out naturally.
"""
import logging
import re
import time

from . import config, llm, tools
from .events import HUB

log = logging.getLogger("jarvis.agent")

# Phrases that claim the assistant is doing/did something on the PC (English + Hinglish)
CLAIMS_ACTION = re.compile(r"\b(open(ed|ing)|play(ing|ed)|start(ed|ing)|launch(ed|ing)|clos(ed|ing)|typ(ed|ing)|"
                           r"search(ed|ing)|turn(ed|ing) (on|off)|set (it|the|your)|chala (raha|rahi|diya|di)|khol (raha|diya|di)|"
                           r"band (kar|ho)|laga (diya|raha)|kar (diya|raha|rahi)|ho gaya)\b", re.I)

SYSTEM = """You are Jarvis, the user's personal assistant living in their Windows laptop. You talk like a warm, sharp,
easy-going human friend - natural, relaxed, brief - and you get things done on the computer for them.
Current local time: {now}.

Doing things (use tools - act, don't describe):
- Apps: open_app / close_app / switch_to_window. Anything inside a desktop app (settings, menus, dialogs, File Explorer,
  Office, installers) -> desktop_task with the full goal.
- Web (Chrome): open_url / web_search to open pages; anything that needs clicking, typing or reading on a website
  (play a song or video, book, shop, fill forms, compare, read results) -> browser_task with the full goal.
- PC questions and jobs (files, folders, disk, network, processes, installing tools, git) -> run_command (PowerShell).
- Building programs/scripts/pages -> write_code. Text into the focused window -> type_text. Shortcuts -> press_keys.
- Volume, brightness, media, lock, battery level, screenshot, power -> system_control (not run_command).
  Reminders -> set_timer.
- Seeing the screen -> look_at_screen / screen_click.
- Multi-step requests: do the steps in order; you see each result before the next step.
- web_search / open_url only open pages - you can't see them. Never invent what they say; read them with
  look_at_screen or browser_task if the user asked a question.
- If a tool returns ERROR, immediately DO the obvious alternative yourself without asking (e.g. app not installed ->
  open its website); only explain if there is no sensible alternative.

Talking:
- Your replies are SPOKEN. 1-2 short sentences, plain words, no markdown, lists, URLs or ids.
- Sound human: contractions, natural variety, a little warmth or light humour when it fits. Call the user "Sir" now
  and then, not in every sentence. Don't announce tool names or say "I have executed".
- Mirror the user's language: English, Hindi or Hinglish (Hinglish in Roman script). Speech-to-text may misspell
  names - use common sense.
- If a request is ambiguous or missing something important (which file? which contact? what time?), ask ONE short
  question instead of guessing - the user's answer comes as the next message. For small details, pick a sensible default.
- Chit-chat and knowledge questions: just answer, briefly and naturally. Follow-ups refer to earlier turns."""


class Agent:
    def __init__(self, ctx: tools.Context):
        self.ctx = ctx
        self.history: list[dict] = []  # completed turns: user message, tool exchanges, final reply

    def _trimmed_history(self) -> list[dict]:
        """Keep the last HISTORY_TURNS user turns; truncate old tool outputs to save tokens."""
        starts = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        recent = self.history[starts[-config.HISTORY_TURNS]:] if len(starts) > config.HISTORY_TURNS else self.history
        return [m | {"content": m["content"][:300]} if m["role"] == "tool" else m for m in recent]

    def handle(self, text: str) -> str:
        """Process one user utterance; returns the reply to speak."""
        system = {"role": "system", "content": SYSTEM.format(now=time.strftime("%A %d %B %Y, %I:%M %p"))}
        turn: list[dict] = [{"role": "user", "content": text}]
        nudge: list[dict] = []  # one-off correction, never stored in memory
        reply = ""
        for _ in range(config.MAX_AGENT_STEPS):
            if HUB.stop.is_set():
                reply = "Okay, stopped."
                break
            HUB.set_state("thinking")
            try:
                message = llm.chat(config.BRAIN_MODELS, [system, *self._trimmed_history(), *turn, *nudge],
                                   tools=tools.schemas(), temperature=0.3, max_wait=25,
                                   on_wait=lambda s: s > 3 and self.ctx.say("Give me a second."))
            except llm.AllModelsFailed as e:
                log.error("Brain unavailable: %s", e)
                reply = "Hmm, I can't reach my AI services right now. Could you check the internet and try again in a bit?"
                break
            calls = message.tool_calls or []
            if not calls:
                reply = (message.content or "").strip() or "Done."
                acted = any(m["role"] == "tool" for m in turn)
                if not acted and not nudge and CLAIMS_ACTION.search(reply):
                    # The model said it is doing something but called no tool -> nothing happened. Ask it once to act.
                    log.info("Reply claims an action without a tool call: %r", reply)
                    nudge = [{"role": "assistant", "content": reply},
                             {"role": "user", "content": "(system check, not from the user) You described an action but "
                              "called no tool, so nothing happened. If the user asked for an action, call the tool(s) now. "
                              "Otherwise reply with your answer again."}]
                    continue
                break
            nudge = []
            # model_dump keeps provider extras (Gemini's thought_signature must be echoed back verbatim)
            turn.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [c.model_dump(exclude_none=True) for c in calls]})
            for call in calls:
                result = tools.execute(call.function.name, call.function.arguments, self.ctx)
                turn.append({"role": "tool", "tool_call_id": call.id, "content": result})
        else:
            reply = "I've done what I could, Sir."
        turn.append({"role": "assistant", "content": reply})
        self.history.extend(turn)
        HUB.emit("reply", text=reply)
        return reply
