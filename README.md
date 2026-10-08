# Jarvis — AI Voice Assistant for Windows & Chrome

Say **"Hey Jarvis"** and talk naturally (English, Hindi or Hinglish). Jarvis operates your whole laptop — any
Windows app, any website in Chrome, PowerShell — asks you when something is unclear, and answers in a natural
human voice. Runs on **free tiers** (Groq + Gemini) plus local models. **No app- or site-specific code**: the same
generic agents work everywhere.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
playwright install chromium
```

1. Copy `.env.example` to `.env` and add your free keys ([Groq](https://console.groq.com/keys), [Gemini](https://aistudio.google.com/apikey)).
2. Teach Jarvis your voice (~1.5 minutes: 8× "Hey Jarvis", 4 other sentences, 5 s of silence):

```bash
python main.py --calibrate
```

3. Start Jarvis (or double-click `start_jarvis.bat`):

```bash
python main.py
```

| Mode | Command |
|---|---|
| Voice + on-screen HUD (default) | `python main.py` |
| Voice, console only | `python main.py --no-hud` |
| Type commands in the console | `python main.py --text` |
| Verbose logs + live wake-word scores | `python main.py --debug` |
| Train the wake word on your voice | `python main.py --calibrate` |

## The HUD (always on screen)

A small always-on-top widget in the bottom-right corner, with a live 3D orb (WebGL) whose colour and motion show what
Jarvis is doing: **blue** detecting · **cyan** listening (ripples with your voice) · **violet** thinking ·
**amber** working (shows the current action, e.g. "Chrome agent · step 3 · clicked Search") · **green** speaking ·
**orange** waiting for your answer · **grey** mic off.

- **Compact** (default): orb + status + current activity + a text box to type commands.
- **Full**: adds the conversation, a step-by-step activity log, **Yes / No buttons** for confirmations, and
  Mic / Voice / Stop toggles. **Orb**: just the orb.
- Click the orb = talk · double-click = resize · drag the orb or header to move (position is remembered).
- Hotkeys anywhere in Windows: **Ctrl+Alt+J** talk, **Ctrl+Alt+X** stop whatever Jarvis is doing or saying.
- Questions can be answered by voice, by clicking Yes/No, or by typing.

## What it can do

| You say | How Jarvis does it (generic) |
|---|---|
| "Open Notepad and type hello" / "close Chrome" / "switch to VS Code" | Start-menu app lookup, window messages, typing |
| "Turn on dark mode", "rename this file in Explorer", "save this as report in Word" | **Desktop agent**: reads any app's controls via Windows UI Automation (+ screenshot vision when an app exposes few) and clicks/types step by step |
| "Play lofi on YouTube", "book a bus from Jaipur to Delhi tomorrow", "find the cheapest headphones on Amazon" | **Chrome agent**: reads any web page's accessibility snapshot and clicks/types step by step in Jarvis's Chrome |
| "How much free space is on C?", "what's my IP", "install requests with pip", "make a folder called projects" | **run_command** (PowerShell) — read-only commands run directly, anything that changes the PC asks first |
| "Make a Python snake game" | Writes a complete file to `~/Documents/jarvis_code`, opens it in VS Code (never runs it) |
| "What's 15% of 2400?", "tell me a joke", "what's the weather in Jaipur?" | Answers directly, or searches and reads the result off the screen |
| "Volume 40", "mute", "brightness 70", "battery?", "screenshot", "lock", "remind me in 10 minutes to drink water" | System controls and timers |
| "Shut down" / "restart" | Asks first; 30 s delay; "cancel shutdown" stops it |
| "Send a message to my friend" | Asks a natural follow-up question ("Which friend, and what should I say?") |

**Conversation:** after each reply a soft beep means Jarvis is still listening (~4 s, ~8 s after a question) — just keep
talking, no wake word. It remembers the last few turns ("open notepad" → "now close it"). Say "stop" or "that's all" to end.
For slow jobs it says "On it" first so silence never feels like a hang.

## Architecture

```
mic ─► auto-gain ─► openWakeWord ─► Silero VAD + smart endpointing ─► Groq Whisper (STT)
                                                                             │
       natural voice ◄── edge-tts neural voice ◄── Brain: tool-calling agent ◄┘  (memory, asks questions)
                                                        │
        ┌──────────────────┬───────────────────────────┼─────────────────────┬──────────────────┐
   Windows tools      Desktop agent               Chrome agent           PowerShell          vision / coder
 apps, keys, volume,  UI Automation tree +      Playwright AI snapshot   run_command with    Gemini screen read,
 timers, folders      screenshot, any app       of any page, any site    safety rules        code writer
                              └───────── shared observe → decide → act loop (tools/loop.py) ─┘
```

| Component | Model (all free) | Measured |
|---|---|---|
| Brain | Groq `qwen/qwen3.8-27b` → `gpt-oss-120b` → Gemini `3.5-flash-lite` → `flash-lite-latest` | 0.3–0.7 s per step |
| Chrome & desktop agents | Gemini `3.5-flash-lite` (light reasoning) → `flash-lite-latest` → Groq `gpt-oss-120b` | ~1.5–2.5 s per step |
| Vision | Gemini `3.5-flash-lite` → `flash-lite-latest` | ~1.8 s |
| Code writer | Groq `gpt-oss-120b` → Gemini `3.5-flash` | |
| Speech-to-text | Groq `whisper-large-v3-turbo` → `whisper-large-v3` | ~0.4 s |
| Voice | Microsoft neural voice via `edge-tts` (streamed; offline Windows voice as fallback) | ~1 s to first word |
| Wake word / VAD | openWakeWord, Silero (local) | instant |

Free tiers are rate-limited (Groq: 8k tokens/min per model). A rate-limited model is put on cooldown and the next one
answers instantly, so the chain behaves like one high-capacity model.

**Wake word, three layers** (`jarvis/wake.py`):
1. openWakeWord "hey jarvis" — instant trigger above the calibrated threshold (acts on the score's peak, not its first frame).
2. Your personal verifier — trained by `--calibrate` on your own recordings (leave-one-out validated; only enabled
   when it beats the standard model), so your pronunciation, mic and room are recognised.
3. Speech-to-text confirmation — a "maybe" score while you speak is checked by Whisper for "Jarvis" (fuzzy:
   Jervis/Javis…). It also catches one-breath commands: "Hey Jarvis, open notepad" runs immediately.

Plus Ctrl+Alt+J and clicking the orb, which always work.

**Threading:** the core (mic, wake word, agent, Chrome/desktop automation) runs on one worker thread — Playwright and
UI Automation are single-threaded — and talks to the HUD only through the thread-safe event hub (`jarvis/events.py`).

Measured on this laptop: brain eval 17/18 then fixed (`tests/eval_agent.py`); YouTube "play lofi" via the generic
Chrome agent in 16 s; Calculator "12 × 34" via the generic desktop agent in 17 s; redbus cheapest-bus lookup in ~50 s.

## Safety

- Payment / "book now" / "place order" / delete / uninstall / send clicks need your spoken **yes**.
- Passwords, OTPs, CVVs, card numbers are **never typed** — Jarvis asks you to type them.
- PowerShell: read-only commands run directly; anything that changes the PC asks first; disk formatting, boot config,
  security bypasses and download-and-execute are refused outright.
- Shutdown / restart / sleep / sign-out ask first. Set `CONFIRM_ALL_ACTIONS=1` to confirm everything.
- Only `http(s)` URLs open; Windows system processes and Jarvis itself can't be closed; generated code is never run.
- Emergency stop: slam the mouse into a screen corner, or `Ctrl+C`.

## Project layout

```
main.py                    entry point (HUD / --no-hud / --text / --calibrate / --debug)
jarvis/config.py           models, voice, paths, tuning constants
jarvis/llm.py              Groq + Gemini client with automatic failover
jarvis/events.py           thread-safe hub between the core and the HUD (events, commands, answers, stop)
jarvis/audio.py            mic, auto-gain, Silero VAD, utterance recording with smart endpointing
jarvis/wake.py             wake word: openWakeWord + personal verifier + speech-to-text confirmation, calibration
jarvis/hotkey.py           global hotkeys (Ctrl+Alt+J talk, Ctrl+Alt+X stop)
jarvis/speech.py           Whisper STT + filters, listening, yes/no, questions (voice, click or typed)
jarvis/tts.py              natural neural voice (streaming, cache, instant stop, offline fallback)
jarvis/agent.py            the brain: tool-calling loop, memory, natural persona
jarvis/app.py              the Assistant core (worker thread), conversation, calibration, text mode
jarvis/hud/__init__.py     HUD window (pywebview), shapes, placement, JS bridge
jarvis/hud/index.html      HUD layout
jarvis/hud/hud.css         HUD styling (per-state colours, compact / full / orb)
jarvis/hud/hud.js          WebGL 3D orb shader, live status, conversation feed, drag & clicks
jarvis/tools/__init__.py   tool registry + central safety gate
jarvis/tools/desktop.py    apps, windows, typing, keys, PowerShell, system controls, timers, folders
jarvis/tools/ui.py         desktop agent (any Windows app via UI Automation + vision)
jarvis/tools/web.py        Chrome control + Chrome agent (any website)
jarvis/tools/loop.py       shared observe → decide → act loop for both agents
jarvis/tools/vision.py     screen click / screen reading
jarvis/tools/coder.py      code writer
tests/test_offline.py      fast offline tests
tests/eval_agent.py        brain accuracy/latency benchmark (tools stubbed)
tests/eval_wake.py         wake-word report on your own recordings (standard vs personal vs speech-to-text)
```

Logs: `jarvis.log`. Settings (wake threshold, HUD mode/position): `jarvis_settings.json`. Your voice recordings and
personal wake model: `voice_profile/`. Jarvis's Chrome profile: `chrome_profile/` (log in to sites once there and they
stay logged in). Old v1 code: `_backup_v1/`.

## Troubleshooting

- **"Hey Jarvis" doesn't trigger:** run `python main.py --calibrate` (new flow, ~1.5 min), then
  `python tests/eval_wake.py` to see which layer catches each of your attempts. `python main.py --debug` shows live
  scores. If calibration reports clipping, lower the mic level / turn off Microphone Boost. Ctrl+Alt+J always works.
- **Voice sounds robotic:** the neural voice needs internet; offline it falls back to the Windows voice.
- **"Can't reach my AI services":** check `.env` keys and internet; details in `jarvis.log`.
- **Chrome agent can't start:** make sure Google Chrome is installed (Edge/Chromium are fallbacks), or run `playwright install chromium`.
