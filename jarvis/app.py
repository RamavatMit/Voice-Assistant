"""The assistant core: wake word -> listen -> agent -> spoken reply -> follow-up conversation.

Everything (mic, wake detection, agent, Chrome/desktop automation) runs on ONE worker thread, because Playwright and
Windows UI Automation are single-threaded. Typed commands, push-to-talk and HUD clicks arrive through `HUB.commands`
and are handled between 80 ms mic chunks.
"""
from collections import deque
import logging
from logging.handlers import RotatingFileHandler
import queue
import sys
import threading
import time

import numpy as np

from . import audio, config, hotkey, llm, speech, wake
from .agent import Agent
from .events import HUB
from .tools import Context

log = logging.getLogger("jarvis")

DISMISS = {"stop", "nothing", "no", "no thanks", "that s all", "thats all", "that is all", "never mind", "nevermind",
           "cancel", "bas", "kuch nahi", "thank you jarvis", "thanks jarvis"}
RING_CHUNKS = int(2.0 / config.CHUNK_SEC)  # 2 s of recent audio: pre-roll and speech-to-text wake confirmation


def setup_logging(debug: bool) -> None:
    file_handler = RotatingFileHandler(config.LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("  [%(name)s] %(message)s"))
    console.setLevel(logging.INFO if debug else logging.WARNING)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers[:] = [file_handler, console]
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.stdout.reconfigure(errors="replace")


class Assistant:
    def __init__(self, debug: bool = False):
        self.debug = debug

    # ---- conversation ------------------------------------------------------------------------------------------
    def _handle(self, text: str) -> str:
        HUB.stop.clear()
        started = time.perf_counter()
        try:
            reply = self.agent.handle(text)
        except Exception:
            log.exception("Agent crashed on %r", text)
            HUB.emit("error", text="Something went wrong (details in jarvis.log)")
            reply = "Oops, something went wrong on my side. Mind trying that again?"
        log.info("Handled in %.2fs", time.perf_counter() - started)
        return reply

    def _converse(self, text: str) -> None:
        """Handle a spoken command, then keep listening briefly for follow-ups (no wake word needed)."""
        while text:
            reply = self._handle(text)
            self.voice.say(reply)
            if HUB.stop.is_set():
                return
            self.voice.beep(700, 50)
            # after a question, give the user time to think like a person would
            text = self.voice.listen(no_speech_sec=8.0 if reply.rstrip().endswith("?") else config.FOLLOW_UP_SEC)
            if text and speech.normalize(text) in DISMISS:
                return

    def _session(self, pre_roll: list | None) -> None:
        """Woken up (wake word, hotkey or orb click): listen for a command and converse."""
        HUB.stop.clear()
        self.voice.beep()
        text = self.voice.listen(pre_roll=pre_roll)
        if text:
            self._converse(text)
        elif not HUB.interrupted():
            self.voice.beep(400, 120)  # low tone: heard nothing usable

    def _confirm_by_speech(self, ring: list) -> None:
        """'Maybe' wake score: let Whisper check the words actually contain 'Jarvis' (and grab a one-breath command)."""
        rest_samples, _ = audio.record_utterance(self.mic, self.detector, None, no_speech_sec=1.2)
        text = speech.transcribe(np.concatenate(ring + ([rest_samples] if rest_samples.size else [])), keep_wake=True)
        found, command = wake.find_wake_word(text or "")
        if not found:
            log.info("Wake candidate rejected by speech-to-text: %r", text)
            return
        log.info("Wake word confirmed by speech-to-text: %r", text)
        if command:
            HUB.stop.clear()
            HUB.emit("heard", text=command)
            self._converse(command)
        else:
            self._session(None)

    def _commands(self) -> None:
        """Handle everything the HUD / hotkeys queued up."""
        while True:
            try:
                kind, value = HUB.commands.get_nowait()
            except queue.Empty:
                return
            if kind == "text" and value:
                HUB.emit("heard", text=value, typed=True)
                self.voice.say(self._handle(value))
            elif kind == "talk" and self.mic and not HUB.mic_muted:
                self._session(None)
            elif kind == "mute_mic":
                HUB.mic_muted = bool(value)
                if self.mic and not HUB.mic_muted:
                    self.mic.flush()
            elif kind == "mute_voice":
                HUB.voice_muted = bool(value)
            HUB.set_state("muted" if HUB.mic_muted else "idle")

    # ---- main loop ---------------------------------------------------------------------------------------------
    def run(self, use_mic: bool = True) -> None:
        import pythoncom
        pythoncom.CoInitialize()  # Windows UI Automation / COM on this worker thread
        try:
            self.mic = audio.Mic() if use_mic else None
        except Exception as e:  # no / busy microphone: keep working with typed commands
            log.error("Microphone unavailable (%s) - typed commands only", e)
            HUB.emit("error", text="Microphone unavailable - you can still type commands")
            self.mic = None
        self.detector = audio.SpeechDetector()
        self.voice = speech.Voice(self.mic, self.detector)
        self.agent = Agent(Context(self.voice))
        threading.Thread(target=llm.warm_up, daemon=True).start()
        if self.mic is None:  # HUD without microphone: typed commands only
            HUB.set_state("muted")
            while True:
                self._commands()
                time.sleep(0.05)

        detector = wake.WakeDetector()
        hotkey.start()
        mode = "personal voice model" if detector.personal else "standard model"
        print(f"Microphone: {self.mic.name} | wake threshold {detector.threshold:.2f} ({mode})"
              + ("" if config.load_settings() else " | run --calibrate to tune it to your voice"))
        print("Say 'Hey Jarvis' or press Ctrl+Alt+J... (Ctrl+Alt+X stops, Ctrl+C quits)")
        ring: deque = deque(maxlen=RING_CHUNKS)
        voiced: deque = deque(maxlen=10)  # Silero verdicts for the last 0.8 s
        last_check, peak, peak_level, last_print = 0.0, 0.0, 0.0, time.time()
        HUB.set_state("idle")
        try:
            while True:
                if not HUB.commands.empty():
                    self._commands()
                    detector.reset(), ring.clear(), self.mic.flush()
                if HUB.mic_muted:
                    time.sleep(0.1)
                    continue
                chunk = self.mic.read()
                ring.append(chunk)
                voiced.append(self.detector.is_speech(chunk))
                score = detector.score(chunk)
                if self.debug:
                    peak, peak_level = max(peak, score), max(peak_level, audio.rms(chunk))
                    if time.time() - last_print >= 1.0:
                        print(f"peak score {peak:.2f} {'#' * int(peak * 20):<20} (trigger {detector.threshold:.2f}) | "
                              f"peak level {peak_level:5.0f} | VAD gain x{self.detector.level.gain:.1f}")
                        peak, peak_level, last_print = 0.0, 0.0, time.time()
                if score < config.WAKE_MAYBE or not any(voiced):
                    continue
                for _ in range(6):  # the score rises over several frames: act on its peak (<= 0.5 s), not its first frame
                    if score >= detector.threshold:
                        break
                    chunk = self.mic.read()
                    ring.append(chunk)
                    voiced.append(self.detector.is_speech(chunk))
                    score = max(score, detector.score(chunk))
                if score >= detector.threshold:
                    log.info("Wake word (%.2f)", score)
                    self._session(list(ring)[-config.PRE_ROLL_CHUNKS:])
                elif time.time() - last_check > 3.0:  # protect the free speech-to-text quota
                    last_check = time.time()
                    log.info("Wake candidate (%.2f) - confirming with speech-to-text", score)
                    self._confirm_by_speech(list(ring))
                else:
                    continue
                detector.reset(), voiced.clear(), ring.clear(), self.mic.flush()
                HUB.set_state("idle")
        finally:
            self.mic.close()


def run_voice(debug: bool) -> None:
    """Console-only voice mode (no HUD)."""
    Assistant(debug).run()


def run_text() -> None:
    """Keyboard mode: type commands (replies are spoken and printed). Great for testing without a mic."""
    voice = speech.Voice(None, None)
    agent = Agent(Context(voice))
    print("Text mode - type a command (empty line to quit).")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            return
        HUB.stop.clear()
        try:
            voice.say(agent.handle(text))
        except Exception:
            log.exception("Agent crashed on %r", text)
            print("Jarvis: Oops, something went wrong (see jarvis.log).")


def calibrate() -> None:
    """Record the user's voice, train + validate a personal wake-word model, save settings."""
    mic = audio.Mic()
    print(f"Microphone: {mic.name}\nThis takes about a minute and a half. Speak at your normal volume and distance.\n")
    try:
        report = wake.calibrate(mic)
    finally:
        mic.close()
    clipped = wake.clipping_ratio()
    print(f"\nStandard model caught {report['base_hits']} | your personal model caught {report['verifier_hits']}")
    print(f"Standard scores: {report['base_scores']}\nPersonal scores: {report['verifier_scores']} "
          f"(other speech max {report['negative_max']})")
    print(f"Using the {'personal' if report['verifier_enabled'] else 'standard'} model, threshold {report['wake_threshold']}")
    if clipped > 0.002:
        print(f"!! {clipped:.1%} of your voice samples are clipped (distorted). Lower the mic level a little or turn "
              "off Microphone Boost in Realtek Audio Console, then calibrate again.")
    config.save_settings(report | {"calibrated_at": time.strftime("%Y-%m-%d %H:%M"), "clipping": round(clipped, 4)})
    print(f"Saved to {config.SETTINGS_PATH}. Recordings are in {wake.PROFILE_DIR}.")
