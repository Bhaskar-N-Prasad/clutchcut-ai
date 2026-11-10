@echo off
rem Starts OBS. After OBS (and any game) closes, ClutchCut finds the stream on YouTube and ASKS you before doing anything.
rem It waits without using CPU or GPU, and installs nothing. Set your channel in autorun.json first. See docs\autorun.md.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\autorun.py" launch
