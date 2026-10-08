"""Thread-safe hub between the assistant core (one worker thread) and the HUD / console.

Core -> HUD: `emit()` events (state changes, what was heard, replies, tool activity) + the live mic `level`.
HUD -> core: `commands` (typed text, push-to-talk, mute toggles), `answers` (Yes/No clicks, typed replies to
questions) and the `stop` flag that cancels the current task, speech or listening.
"""
import queue
import threading
import time
from collections import deque


class Hub:
    def __init__(self):
        self._events: deque = deque(maxlen=400)
        self._lock = threading.Lock()
        self._seq = 0
        self.commands: queue.Queue = queue.Queue()
        self.answers: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.level = 0.0  # mic loudness 0..1 for the orb animation
        self.state = "starting"
        self.ui_attached = False  # a HUD is open: questions may also be answered by clicking/typing
        self.mic_muted = False
        self.voice_muted = False

    def emit(self, kind: str, **data) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": kind, "time": time.time(), **data})
            if kind == "state":
                self.state = data["state"]

    def set_state(self, state: str, detail: str = "") -> None:
        """States: idle, listening, thinking, working, speaking, question, error, muted."""
        self.emit("state", state=state, detail=detail)

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def set_level(self, rms: float) -> None:
        self.level = min(1.0, (rms / 2500.0) ** 0.5)

    def interrupted(self) -> bool:
        """Should the current listening stop early (stop pressed, or the user answered/typed in the HUD)?"""
        return self.stop.is_set() or not self.answers.empty() or not self.commands.empty()

    def take_answer(self, timeout: float = 0.0):
        try:
            return self.answers.get(timeout=timeout) if timeout else self.answers.get_nowait()
        except queue.Empty:
            return None

    def clear_answers(self) -> None:
        while self.take_answer() is not None:
            pass


HUB = Hub()
