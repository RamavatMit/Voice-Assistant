"""Natural-sounding voice: Microsoft neural voices (edge-tts, free) with streaming playback and a phrase cache.

Falls back to the offline Windows SAPI voice if the neural voice is unreachable.
"""
import asyncio
import logging
import threading

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd

from . import config
from .events import HUB

log = logging.getLogger("jarvis.tts")

RATE = 24000
HOLD_BACK = 2304  # samples kept until the next decode: the last mp3 frame of a partial buffer may be incomplete
LOCK = threading.Lock()  # one voice at a time (replies, acknowledgements, timer reminders)
_cache: dict[str, np.ndarray] = {}


def _decode(mp3: bytes) -> np.ndarray:
    pcm = miniaudio.decode(bytes(mp3), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=RATE)
    return np.frombuffer(pcm.samples, dtype=np.int16)


def _speech_end(samples: np.ndarray) -> int:
    """Index just after the last audible sample (the service pads ~1.3 s of silence we don't want to wait through)."""
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > 300)
    return min(len(samples), int(loud[-1]) + RATE // 7) if loud.size else 0


async def _stream(text: str, out: sd.OutputStream | None) -> np.ndarray:
    """Download speech and start playing it before the whole sentence has arrived."""
    mp3, played, decoded_at = bytearray(), 0, 0
    samples = np.array([], dtype=np.int16)
    async for chunk in edge_tts.Communicate(text, config.VOICE, rate=config.VOICE_RATE).stream():
        if chunk["type"] != "audio":
            continue
        mp3.extend(chunk["data"])
        if out is not None and len(mp3) - decoded_at >= 4000:
            decoded_at = len(mp3)
            try:
                samples = _decode(mp3)
            except miniaudio.DecodeError:
                continue
            ready = min(len(samples) - HOLD_BACK, _speech_end(samples))  # never play trailing silence early
            if ready > played:
                _write(out, samples[played:ready])
                played = ready
    samples = _decode(mp3)
    samples = samples[:_speech_end(samples)]
    if out is not None and len(samples) > played:
        _write(out, samples[played:])
    return samples


class _Stopped(Exception):
    pass


def _write(out: sd.OutputStream, samples: np.ndarray) -> None:
    """Play in 0.2 s slices so the Stop button cuts speech off almost instantly."""
    for start in range(0, len(samples), RATE // 5):
        if HUB.stop.is_set():
            raise _Stopped
        out.write(samples[start:start + RATE // 5])


def _play(samples: np.ndarray) -> None:
    with sd.OutputStream(samplerate=RATE, channels=1, dtype="int16") as out:
        try:
            _write(out, samples)
        except _Stopped:
            out.abort()


def _offline(text: str) -> None:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()  # SAPI is a COM object; safe from any thread
    try:
        win32com.client.Dispatch("SAPI.SpVoice").Speak(text)
    finally:
        pythoncom.CoUninitialize()


def speak(text: str) -> None:
    """Speak and block until finished."""
    with LOCK:
        if text in _cache:
            _play(_cache[text])
            return
        try:
            with sd.OutputStream(samplerate=RATE, channels=1, dtype="int16") as out:
                try:
                    samples = asyncio.run(asyncio.wait_for(_stream(text, out), timeout=12))
                except _Stopped:
                    out.abort()  # discard queued audio
                    return
            if len(text) <= 40:
                _cache[text] = samples
        except Exception as e:
            log.warning("Neural voice unavailable (%s); using offline voice", str(e)[:120])
            _offline(text)


def prefetch(phrases: list[str]) -> None:
    """Synthesize short stock phrases in the background so they play instantly later."""
    def run():
        for phrase in phrases:
            if phrase not in _cache:
                try:
                    _cache[phrase] = asyncio.run(asyncio.wait_for(_stream(phrase, None), timeout=12))
                except Exception:
                    return  # offline: speak() will fall back when needed
    threading.Thread(target=run, daemon=True).start()
