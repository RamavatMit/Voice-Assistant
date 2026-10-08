"""Central configuration: paths, model routing and tuning constants.

Model choices were benchmarked on the free tiers (Oct 2026):
  Groq   qwen/qwen3.8-27b      ~0.4s tool calling, 8k tokens/min   -> brain
  Groq   openai/gpt-oss-120b   strongest free coder, own 8k TPM    -> coder + brain fallback
  Gemini gemini-3.5-flash-lite ~1.3s tools / 1.8s vision, 1M ctx   -> web agent, vision, brain fallback
  Groq   whisper-large-v3-turbo ~0.4s, 2000 req/day              -> speech-to-text
Each Groq model has its own rate-limit bucket, so failover multiplies free capacity.
"""
import json
import os

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

SETTINGS_PATH = os.path.join(ROOT, "jarvis_settings.json")
LOG_PATH = os.path.join(ROOT, "jarvis.log")
BROWSER_PROFILE = os.path.join(ROOT, "chrome_profile")  # Jarvis's own Chrome profile: log in once, stays logged in
CODE_WORKSPACE = os.path.abspath(os.path.expanduser(os.getenv("CODE_WORKSPACE") or "~/Documents/jarvis_code"))
CONFIRM_ALL = os.getenv("CONFIRM_ALL_ACTIONS", "0") == "1"
SEARCH_URL = os.getenv("SEARCH_URL", "https://www.google.com/search?q={query}")

# Voice: free Microsoft neural voices (edge-tts). Indian English handles English + Hinglish naturally.
# Others: en-IN-NeerjaNeural (female), en-US-AndrewMultilingualNeural, hi-IN-MadhurNeural (Devanagari Hindi)
VOICE = os.getenv("JARVIS_VOICE", "en-IN-PrabhatNeural")
VOICE_RATE = os.getenv("JARVIS_VOICE_RATE", "+8%")

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# (provider, model, extra request params) tried in order; rate-limited models are skipped until their cooldown ends
BRAIN_MODELS = [
    ("groq", "qwen/qwen3.8-27b", {"reasoning_effort": "none"}),
    ("groq", "openai/gpt-oss-120b", {"reasoning_effort": "low"}),
    ("gemini", "gemini-3.5-flash-lite", {}),
    ("gemini", "gemini-flash-lite-latest", {}),
    ("groq", "openai/gpt-oss-20b", {"reasoning_effort": "low"}),  # extra free capacity (own rate-limit bucket)
]
WEB_MODELS = [  # used by the Chrome and desktop agents; a little reasoning gives noticeably better step decisions
    ("gemini", "gemini-3.5-flash-lite", {"reasoning_effort": "low"}),
    ("gemini", "gemini-flash-lite-latest", {"reasoning_effort": "low"}),
    ("groq", "openai/gpt-oss-120b", {"reasoning_effort": "low"}),
]
VISION_MODELS = [
    ("gemini", "gemini-3.5-flash-lite", {}),
    ("gemini", "gemini-flash-lite-latest", {}),
]
CODER_MODELS = [
    ("groq", "openai/gpt-oss-120b", {"reasoning_effort": "medium"}),
    ("gemini", "gemini-3.5-flash", {}),
    ("gemini", "gemini-3.5-flash-lite", {}),
]
STT_MODELS = [("groq", "whisper-large-v3-turbo", {}), ("groq", "whisper-large-v3", {})]

# Audio
SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms, the frame size openWakeWord expects
CHUNK_SEC = CHUNK / SAMPLE_RATE

WAKE_THRESHOLD = 0.30  # instant trigger; overridden by `--calibrate` (jarvis_settings.json)
WAKE_MAYBE = 0.03  # "maybe" band: scores between this and the threshold are confirmed by speech-to-text
SILENCE_SEC = 0.7  # silence that ends a finished sentence
MAX_PAUSE_SEC = 2.0  # thinking pause allowed when the sentence sounds unfinished
NO_SPEECH_SEC = 5.0  # give up if the user never starts talking
MAX_UTTERANCE_SEC = 20.0
MIN_SPEECH_SEC = 0.25  # shorter voiced audio is noise; never sent to STT
PRE_ROLL_CHUNKS = 3  # audio kept before speech onset so the first syllable isn't clipped
VAD_START, VAD_CONTINUE = 0.5, 0.3  # Silero hysteresis
AGC_TARGET_RMS, AGC_MAX_GAIN = 600.0, 10.0  # quiet laptop mics are boosted before VAD
FOLLOW_UP_SEC = 4.0  # listen this long after a reply for a follow-up without the wake word

# Agent
MAX_AGENT_STEPS = 8
HISTORY_TURNS = 6
MAX_WEB_STEPS = 30
MAX_DESKTOP_STEPS = 20


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(values: dict) -> None:
    settings = load_settings() | values
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
