"""Wake-word report on YOUR recordings (made by `python main.py --calibrate`), without re-recording.

    python tests/eval_wake.py

For every recorded clip: standard model score, personal-model score (leave-one-out), and whether the
speech-to-text confirmation would hear "Jarvis" - i.e. which layer catches each of your attempts.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis import app, config, speech, wake  # noqa: E402


def main() -> None:
    app.setup_logging(False)
    if not glob.glob(os.path.join(wake.PROFILE_DIR, "pos_*.wav")):
        sys.exit(f"No recordings in {wake.PROFILE_DIR} - run: python main.py --calibrate")
    report = wake.train_and_validate()
    positives = sorted(glob.glob(os.path.join(wake.PROFILE_DIR, "pos_*.wav")))
    print(f"{'clip':10} {'standard':>9} {'personal':>9}  speech-to-text heard")
    stt_hits = 0
    for path, base, personal in zip(positives, report["base_scores"], report["verifier_scores"]):
        text = speech.transcribe(wake._load_wav(path), keep_wake=True) or ""
        found = wake.find_wake_word(text)[0]
        stt_hits += found
        print(f"{os.path.basename(path):10} {base:9.2f} {personal:9.2f}  {'YES' if found else 'no ':3} {text!r}")
    print(f"\nInstant trigger: standard {report['base_hits']} | personal {report['verifier_hits']} "
          f"| speech-to-text confirmation {stt_hits}/{len(positives)}")
    print(f"Other speech max personal score: {report['negative_max']} (must stay below the threshold "
          f"{report['wake_threshold']})")
    settings = config.load_settings()
    print(f"Currently using: {'personal' if settings.get('verifier_enabled') else 'standard'} model, "
          f"threshold {settings.get('wake_threshold', config.WAKE_THRESHOLD)}, clipping {settings.get('clipping', 'n/a')}")


if __name__ == "__main__":
    main()
