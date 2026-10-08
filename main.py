"""Jarvis - voice assistant for Windows laptop automation.

    python main.py              voice + always-on-top HUD (orb, status, text box)
    python main.py --no-hud     voice in the console only
    python main.py --text       type commands in the console
    python main.py --calibrate  record your voice and train your personal wake word
    python main.py --debug      verbose logs + live wake-word scores
"""
import argparse
import logging

from jarvis import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis voice assistant")
    parser.add_argument("--no-hud", action="store_true", help="console only, no on-screen widget")
    parser.add_argument("--text", action="store_true", help="type commands in the console")
    parser.add_argument("--calibrate", action="store_true", help="train the wake word on your voice")
    parser.add_argument("--debug", action="store_true", help="verbose logs and live wake-word scores")
    args = parser.parse_args()
    app.setup_logging(args.debug)
    try:
        if args.calibrate:
            app.calibrate()
        elif args.text:
            app.run_text()
        elif args.no_hud:
            app.run_voice(args.debug)
        else:
            try:
                from jarvis import hud
            except Exception as e:  # pywebview / WebView2 unavailable
                logging.getLogger("jarvis").warning("HUD unavailable (%s); running in the console", e)
                app.run_voice(args.debug)
                return
            hud.run(lambda: app.Assistant(args.debug).run())
    except KeyboardInterrupt:
        print("\nGoodbye, Sir.")


if __name__ == "__main__":
    main()
