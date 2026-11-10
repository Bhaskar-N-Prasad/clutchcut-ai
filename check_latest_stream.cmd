@echo off
rem Use this if you streamed without launch_obs.cmd, or after a restart.
rem It asks about streams you said "Later" to, then looks for your newest stream from the last 2 days,
rem waits for YouTube to finish it, and asks you first.
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autorun.py" pending
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autorun.py" watch --since 2d
pause
