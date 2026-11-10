# ClutchCut AI

Turn long game-stream VODs into edited Shorts and kill montages, on your own GPU, with
[Claude Code](https://claude.com/claude-code) as the director.

Claude can't watch a 2-hour video. So ClutchCut gives it eyes: local models and cheap HUD scanners turn the
stream into text, numbers and contact sheets; Claude reads those, zooms in on the moments that matter, and
writes edit recipes; Python + ffmpeg render them; you review, and your feedback becomes the next run's rules.

Works with any spoken language Whisper supports (auto-detected by default). Game-specific HUD reading is
pluggable; PUBG Mobile/BGMI and Valorant profiles are included.

> **Status:** a personal tool, published as-is. Tested on **one machine**: Windows 11, an RTX 5060 Ti (16 GB),
> 1080p streams. Expect rough edges anywhere else.

## See it in action

ClutchCut was built for, and is used on, the YouTube channel
**[iNviSi Gaming](https://www.youtube.com/@invisigaming8559?sub_confirmation=1)**: PUBG Mobile/BGMI and
Valorant streams with squad banter in Kannada and English. These were made with this repo:

| Meme edit (`make_edit.py`) | PUBG montage (`beat_montage.py`) | Valorant montage (`beat_montage.py`) |
|---|---|---|
| [![Not a Single Strong Character](https://img.youtube.com/vi/e_e-_Hmcn7U/hqdefault.jpg)](https://www.youtube.com/shorts/e_e-_Hmcn7U) | [![Squad Wipe to Chicken Dinner](https://img.youtube.com/vi/9FuZewvEnn4/hqdefault.jpg)](https://www.youtube.com/shorts/9FuZewvEnn4) | [![Migraine](https://img.youtube.com/vi/9O-2numHORc/hqdefault.jpg)](https://www.youtube.com/watch?v=9O-2numHORc) |
| [Not a Single Strong Character](https://www.youtube.com/shorts/e_e-_Hmcn7U): the whole squad's fails, with titles, zooms, freezes and a running death count | [Squad Wipe to Chicken Dinner](https://www.youtube.com/shorts/9FuZewvEnn4): knocks found from the squad-panel health bars | [Migraine](https://www.youtube.com/watch?v=9O-2numHORc): every kill on a beat, with a snap zoom, slow motion and a bass hit |

They're not perfect: a few cuts still land slightly early or late against the action or the beat. Timing is
the main thing being worked on (see [Limits](#limits-honest-list)).

**If you like the edits, [subscribe to the channel](https://www.youtube.com/@invisigaming8559?sub_confirmation=1).**
It's the easiest way to support the project and see what the next version can do.

## Setup (Windows + NVIDIA)

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

That creates `.venv\`, installs PyTorch (CUDA 12.8) and the packages, installs ffmpeg + yt-dlp via winget
if missing, downloads a portable Ollama into `tools\ollama\` and pulls `qwen3-vl:8b` into `models\ollama\`.
Whisper, Demucs and PANNs weights download on first use. Skip the vision model with `-SkipVision`.
Then copy `preferences.example.md` to `preferences.md` and fill it in.

Linux/macOS: not tested. The scripts avoid Windows-only calls where possible (fonts, process checks, ffmpeg
lookup all fall back), so `python -m venv .venv && pip install torch torchaudio && pip install -r requirements.txt`
plus ffmpeg, yt-dlp and Ollama on PATH should get you most of the way. Reports welcome.

## Use

Open this folder in Claude Code and run the bundled skill:

```
/clutchcut <YouTube link or video file>
```

Or drive the scripts yourself:

```bash
python scripts/prepare.py "<url-or-file>" --name 2026-01-05-valorant --game valorant --language auto
python scripts/hud_scan.py --name 2026-01-05-valorant          # kills/deaths from the kill feed, seconds
python scripts/zoom.py --name 2026-01-05-valorant --at 1:02:10,1:15:40
python scripts/make_edit.py --name 2026-01-05-valorant --edit edit_meme.json
```

`--language` takes any Whisper code (`en`, `hi`, `kn`, `ta`, `es`, `pt`, `ja`, …) or `auto` (the default,
also settable with `CLUTCHCUT_LANGUAGE`). Auto-detect runs on the first 10 minutes of speech and then stays
fixed, because mixed-language banter can otherwise flip languages between chunks. A rough English translation
is added unless the speech is already English.

**Per-language speech models.** `--asr-model auto` (the default) checks [`asr_models.json`](asr_models.json)
for the stream's language. If a Whisper fine-tune is listed, it's converted to CTranslate2 once and run in fast
batches over short voice chunks; otherwise Whisper large-v3 is used. One entry ships today: Kannada →
`ARTPARK-IISc/whisper-medium-vaani-kannada`. On a 6-minute sample of noisy game voice chat, it produced readable
sentences where large-v3 mostly didn't, and it ran about 16× faster. Fine-tunes give one timestamp per short
chunk rather than per word.

To add your language, compare candidates on a stretch of your own team talk, then add the winner to the table:

```bash
python scripts/asr_compare.py --name <stream> --start 0:30:00 --language hi --models large-v3,org/whisper-hindi-x
```

Every script has `--help` with the file formats in its docstring.

**Hands-off after streaming (Windows, optional).** Start OBS with `launch_obs.cmd` instead of your usual
shortcut. When OBS and your game close, ClutchCut finds the new VOD on your YouTube channel and waits until it
has finished processing. It then asks you whether to prepare it now, later or never, and lets you jot down
moments you remember. If you say yes, it runs prepare + HUD scans and leaves a `READY.md` for the editing
session. It uses no CPU while you play, freezes itself if you start a game mid-run, installs nothing and uploads
nothing. Set your channel in `autorun.json` first; see [docs/autorun.md](docs/autorun.md).

## How it works

```
 VOD ──► prepare.py ─────────────────────────────────────────────────────────┐
         yt-dlp download                                                     │
         Demucs: voices ⟂ game sound                                         │
         PANNs: per-second laugh / shout / gunfire / explosion               │  streams/<name>/
         faster-whisper: transcript (large-v3 or a per-language fine-tune)  │  (text, numbers,
         ffmpeg: 1 frame per second                                          │   frames)
                                                                             │
 frames ─► hud_scan.py  (game profile: squad health bars, kill feed) ────────┤  seconds, no GPU
 frames ─► local_watch.py (optional: Qwen3-VL-8B in Ollama writes notes) ────┤  hours
                                                                             ▼
                      Claude Code (the director)
                      reads events, notes, transcript, preferences.md
                      zoom.py: 2 fps contact sheets + word-timed transcript around each candidate
                      speech_times.py: when people actually speak, for caption timing
                      writes recipes: picks.json / edit_*.json / montage spec
                                                                             │
                                                                             ▼
 render: render_clips.py (plain clips + review page)                         
         make_edit.py    (vertical meme edits: zoom, freeze + thud, fades, text, arrows, HUD enlarge, counters)
         refine_kills.py + beat_montage.py (kill montage: snap zoom on the kill, slow motion, shake, bass hit,
                                            every kill on a beat)
                                                                             │
                                                                             ▼
                      You review ──► feedback ──► preferences.md ──► next run
```

**Why Claude directs and a local model watches.** Sending every frame of a 2-hour stream to a frontier model
is slow and expensive, and most of a stream is looting, menus and walking. The local side does the bulk
"looking" for free; Claude only sees compact digests and the few contact sheets it asks for, and spends its
judgment on what's funny, what's a good play, where to cut and what the caption should say.

**Why HUD scanning.** The first version had the local vision model watch everything: about **7 hours** for one
stream, against a target of about one. A dumb pixel check on the squad panel's health bars listed the knocks
for a whole stream in about **16 seconds**. Game HUDs are designed to be read at a glance, so they're the cheapest
reliable signal there is. The vision model is now optional and best used on specific moments
(`local_watch.py --ask` or `--only`).

**Human in the loop.** Nothing is uploaded. Every output is a file you watch first. `render_clips.py` writes
a `review.html` with keep/reject boxes and notes that you paste back to Claude; for edits you just tell Claude
what's wrong. Claude writes the lasting lessons into `preferences.md` (start points too early, transitions too
long, fonts too heavy, captions must match what's on screen…), and reads it before every run. Early edits took
several rounds of corrections; this file is what made later ones need fewer.

## Real numbers (one stream, one machine)

A 1 h 54 min Valorant stream, RTX 5060 Ti 16 GB, Windows 11:

| Step | Time |
|---|---|
| Download (yt-dlp, 1080p) | 29.6 min (depends on your connection) |
| Voice separation (Demucs htdemucs, GPU) | 1.9 min |
| Sound events (PANNs, GPU) | 11 s |
| Transcript + rough English translation (faster-whisper large-v3, GPU) | 32.6 min |
| 1 fps frames (ffmpeg) | 4.9 min |
| **Prepare total** | **about 69 min** (with large-v3; a batched fine-tune is much faster, untimed on a full stream) |
| HUD scan (kill feed / squad panel) | seconds, CPU only |
| Full local watch with Qwen3-VL-8B (early version, every segment) | about 7 hours |

Disk: `models/` was about 16 GB on the dev machine (Whisper, Demucs, PANNs, Qwen3-VL and some experiments);
each stream needs room for the VOD, two 16 kHz WAVs and one JPEG per second in `streams/<name>/`.
VRAM: each GPU step loads and unloads its own model, so they never share the 16 GB at once. Smaller GPUs
will likely need `--asr-model large-v3-turbo` or `medium` and a smaller vision model; this has not been tested.

## Limits (honest list)

- Tested on one GPU, one OS, 1080p 16:9 sources. The game profiles were measured on one creator's HUD
  settings; other resolutions, HUD scales or colour-blind modes need re-measuring.
- Whisper struggles with fast, overlapping, slang-heavy gaming talk, especially in lower-resource languages and
  code-switched speech. Transcripts don't identify speakers. Language-specific fine-tunes vary a lot: for
  Kannada, one (BuzzASR) did worse than large-v3 and another (Vaani) did much better. Test with
  `asr_compare.py` before trusting one. The Vaani result is from a single 6-minute sample.
- The local vision model can hallucinate kills and once looped on its own output (`num_predict` now caps it).
  Treat its notes as leads, not facts.
- Claude's taste needs your feedback. First edits will be wrong in ways only you can see.
- **Timing isn't perfect yet.** A few cuts, captions and kill zooms land slightly early or late: kill-feed
  rows can appear up to about half a second after the shot, beat tracking can pick the wrong beat, and captions
  depend on speech timestamps that Whisper sometimes gets wrong. Check each edit before uploading. Improving
  this is the current focus.
- Rendering defaults to NVENC and falls back to x264 on the CPU.
- Only download streams you own or have rights to; YouTube's terms apply.

## Troubleshooting

- **"An Application Control policy has blocked this file" (numba):** Windows Smart App Control can block
  numba's compiled DLL, which librosa and PANNs import. ClutchCut detects this and switches to a plain-Python
  stand-in (`scripts/_compat/numba`). Results are the same: the beat times matched in testing, and PANNs ran
  normally. Beat tracking and some audio steps get slower. Nothing to do unless you want the speed back; then
  allow the file or turn Smart App Control off yourself.
- **Captions show boxes instead of letters:** the caption font doesn't cover your script. Set
  `CLUTCHCUT_CAPTION_FONT` (burned-in clip captions) or `CLUTCHCUT_FONT_TEXT` (edit text) to a font that does,
  e.g. a Noto Sans font for your script.
- **Transcripts are nonsense:** check the detected language in `streams/<name>/settings.json` and pass
  `--language` explicitly; then try `asr_compare.py` (see "Per-language speech models").

## Adding a game
See [docs/adding-a-game.md](docs/adding-a-game.md). A profile is one small Python file with a `scan()`
function over the frames.

## Folder layout

| Path | What |
|---|---|
| `scripts/` | the pipeline (`prepare`, `hud_scan`, `local_watch`, `zoom`, `speech_times`, renderers) |
| `games/` | game profiles (HUD scanners) |
| `asr_models.json` | per-language speech models used by `--asr-model auto` (add yours) |
| `launch_obs.cmd`, `check_latest_stream.cmd`, `autorun.example.json` | optional post-stream automation (`scripts/autorun.py`) |
| `.claude/skills/clutchcut/` | the Claude Code skill that runs the pipeline |
| `preferences.example.md` | template for your `preferences.md` (git-ignored) |
| `streams/`, `models/`, `tools/`, `music/` | created locally, git-ignored; set `CLUTCHCUT_HOME` to keep them elsewhere |
| `docs/` | licences, adding a game |

Settings via environment variables: `CLUTCHCUT_HOME`, `CLUTCHCUT_LANGUAGE`, `CLUTCHCUT_ASR_MODELS` (another
table file), `CLUTCHCUT_OLLAMA_HOST`,
`CLUTCHCUT_BUSY` (extra process names that mean "I'm gaming"), `CLUTCHCUT_FONT_TEXT` / `_LABEL` / `_EMOJI`,
`CLUTCHCUT_CAPTION_FONT`.

## Licences
Code: MIT (see [LICENSE](LICENSE)). Models, tools and music have their own licences and are never bundled;
see [docs/licenses.md](docs/licenses.md), including how to source and credit music.

## Author

Made by **Bhaskar N Prasad** ([GitHub](https://github.com/Bhaskar-N-Prasad) ·
[iNviSi Gaming on YouTube](https://www.youtube.com/@invisigaming8559?sub_confirmation=1)).
The story of how it was built, including what failed: [Medium article](TODO-medium-link).
Issues and pull requests are welcome, especially new game profiles and speech models for other languages.
