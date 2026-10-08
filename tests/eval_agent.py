"""Brain evaluation: runs the real agent + real LLMs with tools stubbed out (no side effects on the PC).

    python tests/eval_agent.py                 # current BRAIN_MODELS chain
    python tests/eval_agent.py qwen gpt-oss    # each matching model alone, for comparison
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis import config, tools  # noqa: E402
from jarvis.agent import Agent  # noqa: E402
from jarvis.tools.web import SEARCH_RESULT  # noqa: E402

FAKE_RESULTS = {
    "web_search": lambda a: SEARCH_RESULT.format(query=a.get("query")),
    "look_at_screen": lambda a: "Jaipur: 31°C, sunny",
    "run_command": lambda a: "exit code 0\nName Used(GB) Free(GB)\nC    285.2    190.4",
    "open_app": lambda a: "ERROR: 'spotify' is not installed. Closest: ['Spotify Widget']." if "spotify" in str(a).lower() else f"Opened {a.get('name')}",
    "system_control": lambda a: "Battery 43%, on battery" if a.get("action") == "battery" else f"{a.get('action')} done",
}

# (conversation, check(list of tool calls for the LAST utterance, reply) -> bool)
SCENARIOS = [
    (["open notepad and type hello world"],
     lambda c, r: [n for n, _ in c][:2] == ["open_app", "type_text"] and "hello world" in str(c[1][1]).lower()),
    (["chrome kholo aur youtube pe lofi music chalao"],
     lambda c, r: any(n == "browser_task" and "lofi" in str(a).lower() for n, a in c)),
    (["what is 15 percent of 2400"], lambda c, r: not c and "360" in r),
    (["set volume to 30 percent"], lambda c, r: c == [("system_control", {"action": "set_volume", "value": 30})]),
    (["how much battery do I have"], lambda c, r: c and c[0][0] == "system_control" and ("43" in r or "forty-three" in r.lower() or "forty three" in r.lower())),
    (["open notepad", "now close it"], lambda c, r: c and c[0][0] == "close_app" and "notepad" in str(c[0][1]).lower()),
    (["what is the capital of france", "and of japan?"], lambda c, r: not c and "tokyo" in r.lower()),
    (["book a bus from jaipur to dilni tomorrow on redbus"],
     lambda c, r: c and c[0][0] == "browser_task" and "delhi" in str(c[0][1]).lower() and "redbus" in str(c[0][1]).lower()),
    (["remind me in 10 minutes to drink water"], lambda c, r: c and c[0][0] == "set_timer" and c[0][1].get("seconds") == 600),
    (["make a python snake game"], lambda c, r: c and c[0][0] == "write_code"),
    (["open spotify"], lambda c, r: len(c) >= 2 and c[0][0] == "open_app" and any(n in ("open_url", "web_search", "browser_task") for n, _ in c[1:])),
    (["how much free space is on my C drive"], lambda c, r: c and c[0][0] == "run_command" and "190" in r),
    (["turn on dark mode in windows settings"], lambda c, r: c and c[-1][0] in ("desktop_task", "run_command")),
    (["send a message to my friend"], lambda c, r: not c and r.rstrip().endswith("?")),
    (["what's the weather in jaipur today"],  # must not invent weather it never saw
     lambda c, r: c and c[0][0] in ("web_search", "browser_task", "open_url")
     and ("cloud" not in r.lower() and ("31" in r or "look_at_screen" not in str(c) and "°" not in r))),
    (["shut down my laptop"], lambda c, r: c and c[0][1].get("action") == "shutdown"),
    (["press control s"], lambda c, r: c and c[0][0] == "press_keys" and "s" in str(c[0][1]).lower()),
    (["open my downloads folder"], lambda c, r: c and c[0] == ("open_folder", {"folder": "downloads"})),
]


class SilentVoice:
    def say(self, text): pass
    def acknowledge(self): pass
    def ask(self, q): return "yes"
    def confirm(self, q): return True


def run(models, pace: float = 0.0) -> tuple[int, list[float]]:
    config.BRAIN_MODELS = models
    calls: list = []
    for entry in tools.REGISTRY.values():
        entry.fn = (lambda name: lambda ctx, **a: (calls.append((name, a)), FAKE_RESULTS.get(name, lambda a: "done")(a))[1])(entry.name)
    passed, latencies = 0, []
    for utterances, check in SCENARIOS:
        time.sleep(pace)  # stay inside a single model's free tokens-per-minute budget
        agent = Agent(tools.Context(SilentVoice()))
        for text in utterances:
            calls.clear()
            started = time.perf_counter()
            reply = agent.handle(text)
            latency = time.perf_counter() - started
        latencies.append(latency)
        ok = bool(check(list(calls), reply))
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'} {latency:5.2f}s {utterances[-1][:45]:45} -> {calls} | {reply[:70]!r}")
    return passed, latencies


if __name__ == "__main__":
    filters = sys.argv[1:]
    chains = [[m] for m in config.BRAIN_MODELS if any(f in m[1] for f in filters)] if filters else [config.BRAIN_MODELS]
    for chain in chains:
        print(f"=== {' -> '.join(m[1] for m in chain)}")
        passed, lat = run(chain, pace=25.0 if len(chain) == 1 else 0.0)
        print(f"=== {passed}/{len(SCENARIOS)} passed | median {sorted(lat)[len(lat) // 2]:.2f}s | max {max(lat):.2f}s\n")
