"""Wake word detection tuned to the user's own voice.

Three layers, cheapest first:
  1. openWakeWord "hey jarvis" (pretrained, local).
  2. A personal verifier trained on the user's own recordings (`--calibrate`): a small logistic regression on the
     same audio embeddings, so pronunciations the pretrained model scores at 0.01-0.2 can still trigger.
  3. Speech-to-text confirmation: a "maybe" score while someone is talking -> Whisper checks the words really
     contain "Jarvis" (fuzzy). Also catches one-breath commands ("Hey Jarvis open notepad").
"""
from collections import deque
import difflib
import glob
import logging
import os
import pickle
import time
import wave

import numpy as np
import openwakeword
from openwakeword.custom_verifier_model import train_verifier_model
from openwakeword.model import Model

from . import audio, config, speech

log = logging.getLogger("jarvis.wake")

MODEL = "hey_jarvis"
PROFILE_DIR = os.path.join(config.ROOT, "voice_profile")
VERIFIER_PATH = os.path.join(PROFILE_DIR, "verifier.pkl")
NEGATIVE_PHRASES = ["What's the weather like today?", "Hey, how are you doing?", "Open the notepad please.",
                    "Play some music for me."]


def prepare(chunk: np.ndarray) -> np.ndarray:
    """Same preprocessing at runtime and in training: noise gate + gentle boost of quiet speech."""
    level = audio.rms(chunk)
    if level < 15.0:
        return np.zeros_like(chunk)
    peak = float(np.abs(chunk).max())
    if 50.0 < peak < 8000.0:
        return np.clip(chunk.astype(np.float32) * min(8000.0 / peak, 4.0), -32768, 32767).astype(np.int16)
    return chunk


def _base_model() -> Model:
    openwakeword.utils.download_models(model_names=[MODEL])
    return Model(wakeword_models=[MODEL], inference_framework="onnx")


class WakeDetector:
    """Per-chunk wake score (max over 3 frames). Uses the personal verifier when calibration enabled it."""

    def __init__(self):
        settings = config.load_settings()
        calibrated = "base_hits" in settings  # ignore thresholds from the old (v1) calibration format
        self.threshold = float(settings["wake_threshold"]) if calibrated else config.WAKE_THRESHOLD
        self.personal = bool(settings.get("verifier_enabled")) and os.path.exists(VERIFIER_PATH)
        openwakeword.utils.download_models(model_names=[MODEL])
        kwargs = {"custom_verifier_models": {MODEL: VERIFIER_PATH}, "custom_verifier_threshold": 0.005} if self.personal else {}
        self.model = Model(wakeword_models=[MODEL], inference_framework="onnx", **kwargs)
        self.recent: deque = deque(maxlen=3)

    def score(self, chunk: np.ndarray) -> float:
        prediction = self.model.predict(prepare(chunk))
        self.recent.append(float(max(prediction.values())) if prediction else 0.0)
        return max(self.recent)

    def reset(self) -> None:
        self.model.reset()
        self.recent.clear()


def find_wake_word(text: str) -> tuple[bool, str]:
    """Does a transcript contain 'Jarvis' (tolerating mishearings like 'Jervis', 'Javis')? Returns (found, rest)."""
    words = text.split()
    for i, word in enumerate(words):
        clean = speech.normalize(word).replace(" ", "")
        if clean and difflib.SequenceMatcher(None, clean, "jarvis").ratio() >= 0.72:
            return True, " ".join(words[i + 1:]).lstrip(",.!? ").strip()
    return False, ""


# ---- Calibration: record the user's voice, train + validate a personal verifier ---------------------------------

def _save_wav(path: str, samples: np.ndarray) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(config.SAMPLE_RATE)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)


def _frames(model: Model, samples: np.ndarray) -> tuple[list[float], list[np.ndarray]]:
    """Base score + embedding features for every 80 ms frame of a clip (runtime preprocessing)."""
    model.reset()
    scores, feats = [], []
    for i in range(0, len(samples) - config.CHUNK + 1, config.CHUNK):
        prediction = model.predict(prepare(samples[i:i + config.CHUNK]))
        scores.append(float(prediction[MODEL]))
        feats.append(model.preprocessor.get_features(model.model_inputs[MODEL]))
    return scores, feats


def _speech_end(samples: np.ndarray) -> int | None:
    """Frame index where speech ends (Silero), i.e. where the whole phrase sits inside the model's 1.3 s window."""
    detector = audio.SpeechDetector()
    detector.reset()
    last = None
    for i in range(0, len(samples) - config.CHUNK + 1, config.CHUNK):
        if detector.is_speech(samples[i:i + config.CHUNK]):
            last = i // config.CHUNK
    return last


def _positive_features(scores: list[float], feats: list[np.ndarray], end: int | None) -> list[np.ndarray]:
    """The few frames right after the phrase ends (or around the peak score) carry the wake-word pattern."""
    center = end + 2 if end is not None else int(np.argmax(scores))
    return [feats[i] for i in range(center - 2, center + 3) if 0 <= i < len(feats)]


def _clip_score(verifier, scores: list[float], feats: list[np.ndarray]) -> float:
    """Runtime semantics: the verifier decides wherever the base model shows any interest (>= 0.005)."""
    best = 0.0
    for score, feat in zip(scores, feats):
        best = max(best, float(verifier.predict_proba(feat)[0][-1]) if score >= 0.005 else score)
    return best


def train_and_validate() -> dict:
    """Train the personal verifier from voice_profile/ and measure it with leave-one-out validation."""
    model = _base_model()
    positives = [_frames(model, _load_wav(p)) + (_speech_end(_load_wav(p)),) for p in sorted(glob.glob(os.path.join(PROFILE_DIR, "pos_*.wav")))]
    negatives = [_frames(model, _load_wav(p)) for p in sorted(glob.glob(os.path.join(PROFILE_DIR, "neg_*.wav")))]
    if len(positives) < 4 or not negatives:
        raise ValueError("not enough recordings in voice_profile/ - run --calibrate")

    def fit(pos, neg):
        x_pos = [f for s, fs, end in pos for f in _positive_features(s, fs, end)]
        x_neg = [f for s, fs in neg for f in fs]
        x = np.vstack(x_pos + x_neg)
        y = np.array([1] * len(x_pos) + [0] * len(x_neg))
        return train_verifier_model(x, y)

    # leave-one-out: every clip is scored by a verifier that never saw it
    pos_scores = [_clip_score(fit(positives[:i] + positives[i + 1:], negatives), s, fs) for i, (s, fs, _) in enumerate(positives)]
    neg_scores = [_clip_score(fit(positives, negatives[:i] + negatives[i + 1:]), s, fs) for i, (s, fs) in enumerate(negatives)]
    base_pos = [max(s) for s, _, _ in positives]
    base_neg = [max(s) for s, _ in negatives]

    # verifier threshold: catch as many wake words as possible while staying clearly above every non-wake clip
    v_threshold = float(np.clip(max(neg_scores) + 0.15, 0.5, 0.9))
    v_hits = sum(s >= v_threshold for s in pos_scores)
    b_threshold = float(np.clip(max(base_neg) + 0.1, 0.2, 0.6))
    b_hits = sum(s >= b_threshold for s in base_pos)

    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(VERIFIER_PATH, "wb") as f:
        pickle.dump(fit(positives, negatives), f)
    # more wake words caught wins; on a tie, the larger safety margin of the weakest attempt wins
    v_margin = sorted(pos_scores)[len(pos_scores) // 4] - v_threshold
    b_margin = sorted(base_pos)[len(base_pos) // 4] - b_threshold
    use_verifier = (v_hits, v_margin) > (b_hits, b_margin)
    return {"verifier_enabled": use_verifier, "wake_threshold": round(v_threshold if use_verifier else b_threshold, 2),
            "verifier_hits": f"{v_hits}/{len(positives)}", "base_hits": f"{b_hits}/{len(positives)}",
            "base_scores": [round(s, 2) for s in base_pos], "verifier_scores": [round(s, 2) for s in pos_scores],
            "negative_max": round(max(neg_scores), 2)}


def clipping_ratio() -> float:
    """Share of wake-word samples at full scale: high values mean a distorted (too loud / boosted) mic."""
    clips = [_load_wav(p) for p in glob.glob(os.path.join(PROFILE_DIR, "pos_*.wav"))]
    voiced = np.concatenate([c[np.abs(c) > 1000] for c in clips]) if clips else np.array([])
    return float(np.mean(np.abs(voiced.astype(np.int32)) >= 32000)) if voiced.size else 0.0


def calibrate(mic: audio.Mic, wake_takes: int = 8) -> dict:
    """Guided recording session (~1.5 min): wake word x8, four other phrases, room noise; then train + validate."""
    import winsound
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(PROFILE_DIR, "*.wav")):
        os.remove(old)

    def record(seconds: float) -> np.ndarray:
        time.sleep(0.4)
        mic.flush()
        winsound.Beep(1000, 80)
        return np.concatenate([mic.read() for _ in range(int(seconds / config.CHUNK_SEC))])

    print("Room noise: stay quiet for 5 seconds...")
    _save_wav(os.path.join(PROFILE_DIR, "neg_noise.wav"), record(5.0))
    for i in range(1, wake_takes + 1):
        print(f"[{i}/{wake_takes}] After the beep, say 'Hey Jarvis' (vary it a little: normal, softer, faster)...")
        _save_wav(os.path.join(PROFILE_DIR, f"pos_{i:02d}.wav"), record(2.5))
    for i, phrase in enumerate(NEGATIVE_PHRASES, 1):
        print(f"[other speech {i}/{len(NEGATIVE_PHRASES)}] After the beep, say: \"{phrase}\"")
        _save_wav(os.path.join(PROFILE_DIR, f"neg_{i:02d}.wav"), record(3.5))
    print("\nTraining your personal wake-word model...")
    return train_and_validate()
