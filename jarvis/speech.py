"""Speech-to-text (Groq Whisper), text-to-speech (Windows SAPI) and voice/keyboard dialogue helpers."""
import io
import logging
import random
import wave

import numpy as np

from . import audio, config, llm, tts
from .events import HUB

log = logging.getLogger("jarvis.speech")

# Vocabulary hint: biases Whisper toward the wake word ("Hey joggies" -> "Hey Jarvis": 2/8 -> 8/8 on fast/echoey
# clips, 0 false hits on other speech) and toward romanized Hinglish instead of phonetic English ("call K" for "khol ke")
WHISPER_PROMPT = "Hey Jarvis, open the app. Hey Jarvis, search the web. App khol ke likh do, gaana chalao, band karo, kholo."
ACKS = ["On it.", "Sure, give me a moment.", "Okay, working on it.", "Got it, one sec."]
RETRY_PHRASES = ["Sorry, I didn't quite catch that. Could you say it again?", "Sorry, was that a yes or a no?"]
_PROMPT_PHRASES = None
# Whisper hallucinates these on silence / noise
_HALLUCINATIONS = {"", "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
                   "bye", "okay", "so", "uh", "um", "hmm", "please subscribe", "subtitles by the amara org community"}
_DANGLING = {"and", "or", "but", "to", "for", "the", "a", "an", "that", "with", "of", "in", "on", "then", "which", "so",
             "because", "about", "from", "into", "is", "my", "this", "it", "aur", "ke", "ki", "ka", "ko", "se", "mein", "par"}
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "proceed", "go ahead", "do it", "haan", "ha", "han",
        "haan ji", "ji", "yes please", "confirm", "confirmed", "affirmative", "please do", "alright", "all right", "kar do"}
_NO = {"no", "nope", "nah", "cancel", "stop", "don t", "dont", "do not", "nahi", "na", "mat karo", "abort", "negative", "no thanks"}



def normalize(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (text or "").lower()).split())


def is_complete(transcript: str | None) -> bool:
    """Does a speculative transcript look like a finished sentence? (Whisper writes '...' for trailing speech.)"""
    text = (transcript or "").strip()
    if not text or text.endswith(("...", "…", ",", "-", "—")):
        return False
    words = normalize(text).split()
    return bool(words) and words[-1] not in _DANGLING


def strip_wake_phrase(text: str) -> str:
    words = text.strip().split()
    while words and normalize(words[0]) in ("hey", "hi", "ok", "okay", "jarvis", "jarvis s"):
        words.pop(0)
    return " ".join(words)


def yes_no(text: str) -> bool | None:
    clean = normalize(text)
    for prefix in ("uh ", "um ", "oh ", "ah "):
        clean = clean.removeprefix(prefix)
    if clean in _YES or clean.split()[:1] in (["yes"], ["haan"]):
        return True
    if clean in _NO or clean.split()[:1] in (["no"], ["nahi"]):
        return False
    return None


def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(config.SAMPLE_RATE)
        wf.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def transcribe(samples: np.ndarray, keep_wake: bool = False) -> str | None:
    """Transcribe audio; returns None for silence, noise hallucinations, prompt echoes and low-confidence garbage."""
    global _PROMPT_PHRASES
    if samples.size == 0:
        return None
    try:
        result = llm.transcribe(to_wav(samples), WHISPER_PROMPT)
    except llm.AllModelsFailed as e:
        log.error("Speech-to-text failed: %s", e)
        return None
    segments = getattr(result, "segments", None) or []
    if segments:
        get = (lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k, None))
        no_speech = max(float(get(s, "no_speech_prob") or 0) for s in segments)
        logprob = float(np.mean([float(get(s, "avg_logprob") or 0) for s in segments]))
        if (no_speech > 0.6 and logprob < -0.5) or logprob < -1.2:
            log.info("Rejected low-confidence transcript %r (no_speech=%.2f logprob=%.2f)", result.text, no_speech, logprob)
            return None
    text = (result.text or "").strip()
    text = text if keep_wake else strip_wake_phrase(text)
    if normalize(text) in _HALLUCINATIONS:
        return None
    if _PROMPT_PHRASES is None:
        _PROMPT_PHRASES = [normalize(p) for p in WHISPER_PROMPT.replace(".", ",").split(",") if p.strip()]
    if sum(p in normalize(text) for p in _PROMPT_PHRASES) >= 3:
        return None  # Whisper echoed its prompt on near-silent audio
    return text or None


class Voice:
    """Talks to the user: speaks with the neural voice, listens via mic, and accepts HUD clicks/typing as answers."""

    def __init__(self, mic: "audio.Mic | None", detector: "audio.SpeechDetector | None"):
        self.mic = mic
        self.detector = detector
        tts.prefetch([*ACKS, *RETRY_PHRASES])

    def say(self, text: str) -> None:
        if not text or not text.strip():
            return
        print(f"Jarvis: {text}")
        HUB.emit("say", text=text)
        if not HUB.voice_muted and not HUB.stop.is_set():
            HUB.set_state("speaking", text)
            tts.speak(text)
        if self.mic:
            self.mic.flush()  # never hear ourselves

    def acknowledge(self) -> None:
        """Short human acknowledgement before a slow job, so silence never feels like a hang."""
        self.say(random.choice(ACKS))

    def beep(self, frequency: int = 1000, ms: int = 80) -> None:
        if self.mic:
            import winsound
            winsound.Beep(frequency, ms)

    def listen(self, no_speech_sec: float = config.NO_SPEECH_SEC, pre_roll=None) -> str | None:
        """Record one utterance and return its transcript (None if nothing intelligible was said)."""
        if self.mic is None:
            if HUB.ui_attached:
                return None  # HUD-only mode: typed input arrives as commands/answers instead
            try:
                return input("You: ").strip() or None
            except EOFError:
                return None
        if HUB.mic_muted:
            return None
        HUB.set_state("listening")
        samples, text = audio.record_utterance(self.mic, self.detector, pre_roll, no_speech_sec, transcribe, is_complete)
        if samples.size and text is None:
            text = transcribe(samples)
        if text:
            print(f"You: {text}")
            HUB.emit("heard", text=text)
        return text

    def _answer(self, question: str, kind: str):
        """Ask once; return the first answer: a HUD click (bool), typed text, or speech (str). None if none."""
        HUB.clear_answers()
        self.say(question)
        HUB.emit("question", text=question, qtype=kind)
        HUB.set_state("question", question)
        spoken = self.listen(no_speech_sec=8.0)
        clicked = HUB.take_answer()
        if clicked is None and not spoken and HUB.ui_attached and (self.mic is None or HUB.mic_muted):
            clicked = HUB.take_answer(timeout=60)  # no mic: wait for a click / typed answer
        HUB.emit("question_done")
        return clicked if clicked is not None else spoken

    def ask(self, question: str) -> str | None:
        """Ask a free-form question (e.g. passenger name) and return the answer."""
        for attempt in range(2):
            answer = self._answer(question if attempt == 0 else RETRY_PHRASES[0], "ask")
            if answer and not isinstance(answer, bool):
                return str(answer)
            if HUB.stop.is_set():
                break
        return None

    def confirm(self, question: str) -> bool:
        """Yes/no confirmation (spoken or clicked). Anything unclear counts as NO (safe default)."""
        for attempt in range(2):
            answer = self._answer(question if attempt == 0 else RETRY_PHRASES[1], "confirm")
            decision = answer if isinstance(answer, bool) else yes_no(answer or "")
            if decision is not None:
                return decision
            if HUB.stop.is_set():
                break
        return False
