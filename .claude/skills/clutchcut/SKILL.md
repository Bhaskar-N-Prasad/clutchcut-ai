---
name: clutchcut
description: Turn a long game-stream VOD (YouTube link or video file) into edited vertical Shorts, meme edits and beat-synced kill montages. Use when the user asks to clip a stream, make shorts/highlights/montages, or run ClutchCut.
argument-hint: <youtube-url-or-video-path> [stream-name] [game] [language]
---

# ClutchCut AI

You are the director. A local vision model (Qwen3-VL via Ollama) and cheap HUD scanners do the watching;
you read their notes, look closely at candidate moments, decide, and write edit recipes the scripts render.

Paths are relative to the repository root (the folder holding `scripts/`). Python: `.venv/Scripts/python.exe`
on Windows, `.venv/bin/python` elsewhere. Each stream gets `streams/<name>/` (e.g. `2026-01-05-valorant`; ask
or derive from the date/title). Read `README.md` and `preferences.md` first (if `preferences.md` is missing,
copy `preferences.example.md` and ask the user to fill in game, spoken language and caption language).

**Don't disturb gaming:** the scripts refuse to run while OBS, an emulator or a known game is open. Never pass
`--force` unless the user asks. Ollama runs only during `local_watch.py`.

**Language:** never assume one. Use the language in `preferences.md`, or let Whisper auto-detect
(`--language auto`, the default) and confirm the detected code (in `streams/<name>/settings.json`) with the user
if captions depend on it. Transcripts don't say who is speaking and mishear lines; ask when a caption depends on it.

**Already prepared by autorun?** If `streams/<name>/READY.md` exists, read it first and skip steps 1-2: it
lists what ran, the HUD-scan summaries and the time offset. `user_moments.json` holds moments the streamer
remembered: treat every one as a must-check candidate (check both readings of an "ambiguous" time).

## 1. Prepare (background; about 70 min for a 2-hour stream on a 16 GB GPU, mostly download + Whisper)
`scripts/prepare.py "<url-or-file>" --name <name> [--game <profile>] [--language <code>]`:
download, separate voices from game sound (Demucs), sound events (PANNs), transcript + rough English
translation (Whisper large-v3, or the per-language fine-tune in `asr_models.json` with the default
`--asr-model auto`), 1 fps frames, 10-minute segments. Every step is cached; re-running resumes.
Fine-tune transcripts have chunk times but no word times: time captions with `speech_times.py`.
If transcripts look like garbage for the user's language, offer `scripts/asr_compare.py` to test fine-tunes.
Only download videos the user owns or has permission to use.

## 2. Find moments cheaply (seconds, no GPU)
If the game has a profile (`scripts/hud_scan.py --list`): `scripts/hud_scan.py --name <name>`. It writes e.g.
`knocks.json` (PUBG Mobile squad panel) or `kills.json` (Valorant kill feed). Start from these.

## 3. Local watch (optional, slow: hours for a full stream)
`scripts/local_watch.py --name <name> [--only seg03,seg07]`: Qwen watches segments and writes `watch_log/`
and `digest.md`. Prefer watching only the segments around HUD events, or asking about single moments:
`scripts/local_watch.py --name <name> --ask --at 1:02:10 --question "..."`.

## 4. Shortlist and zoom (you)
1. Read the HUD events, `digest.md` (if any), `preferences.md` and `transcript.txt` around promising times.
2. Shortlist ~12 moments; merge ones that are really one moment.
3. `scripts/zoom.py --name <name> --at t1,t2,...`, then read each `zoom/<id>/notes.md` and `sheet_*.jpg`
   (2 fps, times burned in). Check what actually happens on screen before you caption it.
4. Time captions to real speech with `scripts/speech_times.py --name <name> --at a-b`, not segment times.

## 5. Write recipes and render
- Plain clips: `streams/<name>/picks.json` -> `scripts/render_clips.py --name <name>` (format in its docstring).
- Meme / compilation edits: `streams/<name>/edit_<x>.json` -> `scripts/make_edit.py --name <name> --edit edit_<x>.json`.
- Kill montages: `scripts/refine_kills.py` for exact kill times, then `scripts/beat_montage.py --spec ... --out ...`.
- Music: only tracks in `music/` that the user has the right to use; add the credit line to the description.
After rendering, grab a frame (`ffmpeg -ss 5 -i out.mp4 -frames:v 1 check.jpg`) and look at it.
- Call to action: if `preferences.md` has one, add its end-of-Short text to the last ~1.5 s of the final beat
  (small, below the action, never covering the punchline).
- For each finished edit, write `streams/<name>/edits/<out>.description.txt` for the user to paste when they
  upload: a title (under 60 characters), 2-4 lines of description, the description lines from preferences.md
  (subscribe link, project link), the music credit line from `music/LICENSES.txt`, and 3-5 relevant hashtags
  (include #shorts for vertical edits). Never upload anything yourself.

## 6. Learn
The user reviews (`streams/<name>/review.html` or the edit itself) and gives feedback. Record durable lessons
in `preferences.md` under "Learned from feedback", dated, and apply them next time.

## After a job
Confirm nothing is left running (Windows: `Get-Process ollama*, python, ffmpeg -ErrorAction SilentlyContinue`).
