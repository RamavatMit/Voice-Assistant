@echo off
rem Jarvis launcher. Double-click to start, or pass options:
rem   start_jarvis.bat --calibrate   (tune the wake word for your microphone)
rem   start_jarvis.bat --debug       (show live wake-word scores)
title Jarvis Voice Assistant
cd /d "%~dp0"
python main.py %*
pause
