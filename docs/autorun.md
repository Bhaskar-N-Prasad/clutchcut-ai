# Autorun: prepare your stream after OBS closes (Windows, opt-in)

Optional. Without it you run `prepare.py` yourself. With it, ClutchCut notices that you've finished streaming,
waits for YouTube to finish processing the VOD, **asks you**, and then prepares the stream so it's ready for
editing when you sit down with Claude Code.

## Set up once
1. Run `setup.ps1` as usual.
2. Copy `autorun.example.json` to `autorun.json` (git-ignored) and set:
   - `channel`: your YouTube channel URL, e.g. `https://www.youtube.com/@yourchannel`
   - `obs`: path to `obs64.exe`, if it's not in `C:\Program Files\obs-studio\`
   - `language`: your spoken language code, or `auto`
   - `prepare_args`: extra `prepare.py` flags, e.g. `["--asr-model", "large-v3"]`
3. Start OBS with **`launch_obs.cmd`** instead of your usual shortcut. To keep using a desktop or taskbar
   shortcut, make one that points at `launch_obs.cmd`.

## What happens
1. OBS opens as usual. ClutchCut waits in the background using **no CPU and no GPU**: it sleeps on OBS's
   process handle, and Windows wakes it when OBS closes.
2. When OBS **and** every game or emulator are closed, it looks at your channel's streams with yt-dlp. No API
   key or Google account is needed. It waits until YouTube has finished processing the stream, checking after
   2, 5, 10, 15, 20 and 30 minutes, then every 30 minutes. It waits for the 1080p version for up to 3 hours.
3. A window asks:
   - **Start now**: prepare the stream.
   - **Later**: ask again in 30 min, 1, 3 or 8 hours. Closing the window counts as Later.
   - **Skip this stream**: never ask about it again.

   In the same window you can:
   - tick which game profiles to scan the HUD for (guessed from the title)
   - skip a warm-up, e.g. `9:00`
   - type **moments you remember**, e.g. `1:23:00 clutch, 45:00 funny revive`. These are saved to
     `streams/<name>/user_moments.json` as must-check candidates. If you write `1:23`, both 1 min 23 s and
     1 h 23 min are kept.
4. After **Start now**, a console window runs:
   1. `prepare.py`
   2. `hud_scan.py` for each ticked game
   3. `refine_kills.py` where the profile supports it

   It writes `streams/<name>/READY.md`, a summary for the editing conversation, and shows a message when done.
5. Open Claude Code in the repo and say *"make the edits for streams/<name>, read READY.md first"*.

## It stays out of your way while gaming
- Nothing heavy runs while OBS, a known game or an emulator is open. The list is `BUSY_PROCESSES` in
  `scripts/common.py`; add your own with `CLUTCHCUT_BUSY="mygame.exe"`.
- The question window never pops up over a game.
- If you start a game **while it's preparing**, the work is **frozen** right away (no CPU or GPU work, no
  download) and resumes when the game closes. It keeps its GPU memory while frozen, about 4 GB during
  transcription.
- The pipeline runs at below-normal priority, and the waiter at idle priority.
- Games with kernel anti-cheat may not let it open their process handle. It then checks again once a minute.

## Nothing is installed
No service, scheduled task, startup entry or updater. If you don't use `launch_obs.cmd`, nothing runs.
Nothing is uploaded anywhere.

| Where | What |
|---|---|
| `autorun/autorun.log` | what it did and when |
| `autorun/state.json` | streams already handled, and ones you said "Later" to |
| `autorun/jobs/` | one small file per stream you started |
| `autorun/dryrun/` | test runs only, safe to delete |
| `streams/<name>/` | the prepared stream, `user_moments.json`, `autorun.log`, `READY.md` |

## Other commands
- `check_latest_stream.cmd`: if you streamed without `launch_obs.cmd`, or restarted before answering "Later".
- Stop a running pipeline by closing its window. Running it again continues where it stopped.
- Test without doing anything heavy:
  `.venv\Scripts\python.exe scripts\autorun.py watch --dry-run --url <youtube link>`

## Limits
- Windows only (it uses Windows process APIs to wait and to freeze work).
- Tested on one machine with OBS streaming to YouTube. Other platforms (Twitch, Kick) aren't supported yet. The
  YouTube lookup is in `find_new_stream()` / `video_info()` if you want to add one.
- Only use it on your own channel; YouTube's terms apply to downloading.
