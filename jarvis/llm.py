"""One OpenAI-compatible client for Groq and Gemini, with automatic failover between free models.

A model that returns 429 (rate limit) / 503 (overloaded) / times out is put on cooldown and skipped,
so the next request goes straight to a healthy model instead of waiting.
"""
import logging
import os
import re
import time

from openai import OpenAI

from . import config

log = logging.getLogger("jarvis.llm")

_clients: dict[str, OpenAI] = {}
_cooldown_until: dict[str, float] = {}


class AllModelsFailed(RuntimeError):
    pass


def client(provider: str) -> OpenAI:
    if provider not in _clients:
        key = os.getenv("GROQ_API_KEY" if provider == "groq" else "GEMINI_API_KEY", "")
        if not key or key.lower().startswith(("your_", "gsk_your_")):
            raise AllModelsFailed(f"{provider.upper()} API key missing in .env")
        url = config.GROQ_URL if provider == "groq" else config.GEMINI_URL
        _clients[provider] = OpenAI(api_key=key, base_url=url, max_retries=0, timeout=30.0)
    return _clients[provider]


def _cooldown_seconds(error: Exception) -> float:
    text = str(error)
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", text)
    if match:
        seconds = float(match.group(2)) / (1000 if match.group(3) == "ms" else 1) + 60 * int(match.group(1) or 0)
        return min(seconds + 0.5, 120.0)
    if "per day" in text or "PerDay" in text:
        return 3600.0
    return 20.0


def _is_transient(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    return status in (408, 409, 429, 500, 502, 503, 504) or status is None  # None: timeout / connection error


def _adapt(messages: list, provider: str) -> list:
    """Make tool-call history portable across providers (the conversation may fail over mid-task).

    Gemini 3 rejects tool calls without a thought_signature -> calls made by Groq get Gemini's documented bypass value.
    Groq doesn't know Gemini's `extra_content` field -> it is stripped.
    """
    adapted = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            if provider == "gemini":
                bypass = {"extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}
                calls = [c if "extra_content" in c else c | bypass for c in m["tool_calls"]]
            else:
                calls = [{k: v for k, v in c.items() if k != "extra_content"} for c in m["tool_calls"]]
            m = m | {"tool_calls": calls}
        adapted.append(m)
    return adapted


def chat(models: list, messages: list, tools: list | None = None, json_mode: bool = False,
         max_wait: float = 0.0, on_wait=None, **params):
    """Return the first successful chat completion message from the model chain.

    If every model is rate-limited and `max_wait` > 0, wait for the soonest cooldown (<= max_wait s) and retry once;
    `on_wait(seconds)` is called first (e.g. to tell the user "give me a second").
    """
    try:
        return _chat_once(models, messages, tools, json_mode, **params)
    except AllModelsFailed:
        soonest = max(min(_cooldown_until.get(m[1], 0) for m in models) - time.time(), 1.0)
        if max_wait <= 0 or soonest > max_wait:
            raise
        log.info("All models rate-limited; waiting %.0fs", soonest)
        if on_wait:
            on_wait(soonest)
        time.sleep(soonest)
        return _chat_once(models, messages, tools, json_mode, **params)


def _chat_once(models, messages, tools, json_mode, timeout: float = 15.0, **params):
    errors = []
    now = time.time()
    available = [m for m in models if _cooldown_until.get(m[1], 0) <= now]
    if not available:
        raise AllModelsFailed("all models are cooling down after rate limits")
    for provider, model, extra in available:
        request = {"model": model, "messages": _adapt(messages, provider), **extra, **params}
        if tools:
            request["tools"] = tools
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            # short timeout: an overloaded model fails over quickly instead of stalling the conversation
            response = client(provider).with_options(timeout=timeout).chat.completions.create(**request)
            log.debug("%s answered in %.2fs", model, time.perf_counter() - started)
            return response.choices[0].message
        except AllModelsFailed:
            raise
        except Exception as e:
            errors.append(f"{model}: {str(e)[:160]}")
            if _is_transient(e):
                _cooldown_until[model] = time.time() + _cooldown_seconds(e)
                log.info("%s unavailable (%s), failing over", model, getattr(e, "status_code", "timeout"))
            else:
                log.warning("%s rejected the request: %s", model, str(e)[:300])
    raise AllModelsFailed("; ".join(errors) or "no models configured")


def transcribe(wav_bytes: bytes, prompt: str):
    """Speech-to-text (verbose JSON with segment confidences) using the STT model chain."""
    errors = []
    for provider, model, _ in config.STT_MODELS:
        if _cooldown_until.get(model, 0) > time.time():
            continue
        try:
            return client(provider).audio.transcriptions.create(
                model=model, file=("speech.wav", wav_bytes, "audio/wav"), prompt=prompt,
                response_format="verbose_json", temperature=0, language="en")
        except Exception as e:
            errors.append(f"{model}: {str(e)[:160]}")
            if _is_transient(e):
                _cooldown_until[model] = time.time() + _cooldown_seconds(e)
    raise AllModelsFailed("; ".join(errors) or "all STT models cooling down")


def warm_up() -> None:
    """Open HTTPS connections early so the first command doesn't pay for TLS handshakes."""
    for provider in ("groq", "gemini"):
        try:
            client(provider).models.list()
        except Exception:
            pass
