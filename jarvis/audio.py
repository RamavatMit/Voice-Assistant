"""Microphone, automatic gain, Silero voice-activity detection, wake word and utterance recording."""
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import logging
from typing import Callable

import numpy as np
import sounddevice as sd
from openwakeword.vad import VAD

from . import config
from .events import HUB

log = logging.getLogger("jarvis.audio")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speculative-stt")


def rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) if chunk.size else 0.0


class Mic:
    """16 kHz mono int16 microphone stream read in 80 ms chunks."""

    def __init__(self):
        self.name = sd.query_devices(kind="input")["name"]
        self.stream = sd.InputStream(samplerate=config.SAMPLE_RATE, channels=1, dtype="int16", blocksize=config.CHUNK)
        self.stream.start()

    def read(self) -> np.ndarray:
        data, _ = self.stream.read(config.CHUNK)
        chunk = data.flatten()
        HUB.set_level(rms(chunk))  # drives the HUD orb
        return chunk

    def flush(self) -> None:
        """Drop audio buffered while busy (speaking, thinking) so it is never treated as a command."""
        available = self.stream.read_available
        if available > 0:
            self.stream.read(available)

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


class InputLevel:
    """Slow automatic gain: scales VAD input so the recent loud level reaches AGC_TARGET_RMS (no per-chunk pumping)."""

    def __init__(self):
        self.history: deque = deque(maxlen=125)  # ~10 s

    def update(self, level: float) -> None:
        self.history.append(level)

    @property
    def gain(self) -> float:
        if len(self.history) < 5:
            return config.AGC_MAX_GAIN / 2
        loud = max(float(np.percentile(self.history, 90)), 1.0)
        return float(np.clip(config.AGC_TARGET_RMS / loud, 1.0, config.AGC_MAX_GAIN))

    def apply(self, chunk: np.ndarray) -> np.ndarray:
        return np.clip(chunk.astype(np.float32) * self.gain, -32768, 32767).astype(np.int16)


class SpeechDetector:
    """Silero neural VAD (~1 ms per chunk) with hysteresis; robust to fans, typing and room noise."""

    def __init__(self):
        self.vad = VAD()
        self.level = InputLevel()
        self.in_speech = False

    def reset(self) -> None:
        self.vad.reset_states()
        self.in_speech = False

    def is_speech(self, chunk: np.ndarray) -> bool:
        self.level.update(rms(chunk))
        prob = float(self.vad.predict(self.level.apply(chunk), frame_size=config.CHUNK // 2))
        self.in_speech = prob >= (config.VAD_CONTINUE if self.in_speech else config.VAD_START)
        return self.in_speech


def record_utterance(
    mic: Mic,
    detector: SpeechDetector,
    pre_roll: list[np.ndarray] | None = None,
    no_speech_sec: float = config.NO_SPEECH_SEC,
    transcribe: Callable[[np.ndarray], str | None] | None = None,
    is_complete: Callable[[str | None], bool] | None = None,
) -> tuple[np.ndarray, str | None]:
    """Record one utterance; return (audio, transcript-if-already-known).

    Smart endpointing: after SILENCE_SEC of silence the audio is transcribed in the background while listening
    continues. A finished sentence stops immediately (its transcript is reused); an unfinished one ("...that",
    "...khol ke") may pause up to MAX_PAUSE_SEC. Returns an empty array when no real speech was heard.
    """
    detector.reset()
    pre: deque = deque(pre_roll or [], maxlen=config.PRE_ROLL_CHUNKS)
    chunks: list[np.ndarray] = []
    speech = silence = read = 0
    base_silence = short_silence = int(config.SILENCE_SEC / config.CHUNK_SEC)
    max_pause = int(config.MAX_PAUSE_SEC / config.CHUNK_SEC)
    keep_tail = 3
    speculation: Future | None = None
    speculative_len = 0
    unfinished: tuple[int, str | None] | None = None  # (audio length, transcript) of an "unfinished" speculation
    transcript = None

    def trimmed() -> list[np.ndarray]:
        return chunks[:len(chunks) - max(0, silence - keep_tail)]

    while read < config.MAX_UTTERANCE_SEC / config.CHUNK_SEC:
        if HUB.interrupted():  # Stop pressed, or the user clicked/typed an answer in the HUD
            return np.array([], dtype=np.int16), None
        chunk = mic.read()
        read += 1
        if detector.is_speech(chunk):
            if not chunks:
                chunks.extend(pre)
            chunks.append(chunk)
            speech += 1
            silence = 0
            short_silence = base_silence
            speculation = unfinished = None  # user kept talking: speculative transcripts are stale
            continue
        if not chunks:
            pre.append(chunk)
            if read * config.CHUNK_SEC >= no_speech_sec:
                break
            continue
        chunks.append(chunk)
        silence += 1
        if transcribe is None:
            if silence >= short_silence:
                break
            continue
        if silence >= short_silence and speculation is None:
            audio = np.concatenate(trimmed())
            speculative_len = audio.size
            speculation = _executor.submit(transcribe, audio)
        if speculation is not None and speculation.done():
            result, speculation = speculation.result(), None
            if is_complete is None or is_complete(result):
                transcript = result
                break
            short_silence = max_pause + 1  # unfinished sentence: only the max pause ends it now
            unfinished = (speculative_len, result)
            log.info("Sentence sounds unfinished (%r) - waiting", result)
        if silence >= max_pause:
            break

    if speech * config.CHUNK_SEC < config.MIN_SPEECH_SEC:
        return np.array([], dtype=np.int16), None
    audio = np.concatenate(trimmed())
    if transcript is None and audio.size == speculative_len:
        # The pause timed out with no new speech: the audio is identical to what was already transcribed
        if speculation is not None:
            transcript = speculation.result()
        elif unfinished is not None:
            transcript = unfinished[1]
    return audio, transcript
