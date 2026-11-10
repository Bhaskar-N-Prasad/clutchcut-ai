# Licences: code, dependencies, models, music

ClutchCut ships **only its own code**. Every model, tool and music track is downloaded by you, onto your
machine, from its original source, under its own licence. Nothing below is legal advice; check the linked
terms yourself before you publish videos made with it, especially commercially.

## ClutchCut's own code
Proposed: **MIT** (see `LICENSE`). It's short, matches the licences of most of the stack (faster-whisper,
Demucs, PANNs, Ollama are MIT), and lets anyone reuse the scripts. **Apache-2.0** is the alternative if you
want an explicit patent grant and a NOTICE file; it's equally compatible with everything here.

## Python packages
Checked from the installed package metadata (versions used in development):

| Package | Version | Licence |
|---|---|---|
| faster-whisper | 1.2.1 | MIT |
| CTranslate2 | 4.8.4 | MIT |
| demucs | 4.1.0 | MIT |
| panns-inference | 0.1.1 | MIT |
| torchlibrosa | 0.1.0 | MIT |
| librosa | 1.0.0 | ISC |
| soundfile | 0.14.0 | BSD-3-Clause |
| Pillow | 13.0.0 | MIT-CMU (HPND) |
| NumPy | 2.5.4 | BSD-3-Clause (+ bundled permissive parts) |
| ollama (Python client) | 0.6.3 | MIT |
| transformers (optional, only to convert fine-tunes) | — | Apache-2.0 |
| PyTorch / torchaudio | 2.11.0 | BSD-3-Clause |
| nvidia-cublas-cu12, nvidia-cudnn-cu12 | 12.9 / 9.27 | NVIDIA proprietary (free to use; don't redistribute the DLLs) |

## Models (downloaded on first use, never bundled)

| Model | Used for | Licence | Notes |
|---|---|---|---|
| Whisper large-v3 (OpenAI), via the CTranslate2 conversion `Systran/faster-whisper-large-v3` | transcription, rough translation | MIT (OpenAI's Whisper release); the Hugging Face model card for `openai/whisper-large-v3` lists Apache-2.0 | Both permissive. |
| `ARTPARK-IISc/whisper-medium-vaani-kannada` (only when the language is Kannada and `--asr-model auto`) | Kannada transcription | **Check the model card on Hugging Face** (not verified here) | Converted locally to CTranslate2; the converted copy stays in your `models/`. Any model you add to `asr_models.json` brings its own licence. |
| Demucs `htdemucs` (Meta) | separating voices from game sound | MIT (Demucs repository, which publishes the weights) | Check the Demucs README if you plan commercial use of the weights. |
| PANNs `Cnn14_DecisionLevelMax` (Kong et al.) | laughter / shouting / gunfire / explosion scores | Code MIT; weights from Zenodo record 3987831 — **check the licence shown on that record** | Class labels CSV is from Google's AudioSet ontology (CC BY 4.0). |
| Qwen3-VL-8B via Ollama (`qwen3-vl:8b`) | optional local "watching" | Apache-2.0 (Qwen3 series) | Ollama itself is MIT. Check the model's page on ollama.com for the exact tag you pull. |

## Command-line tools (installed separately, called as external programs)

| Tool | Licence | Notes |
|---|---|---|
| ffmpeg | LGPL-2.1+; most Windows builds (e.g. Gyan's) are GPL-3.0 because they include GPL encoders such as x264 | ClutchCut only runs ffmpeg as a separate program and doesn't distribute it, so its licence doesn't change ClutchCut's. |
| yt-dlp | Unlicense (public domain) | The licence is permissive, but **YouTube's Terms of Service still apply**: only download streams you own or have permission to use. |

## Music: bring your own, credit it
No tracks are included, and `music/` is git-ignored. Put tracks you have the right to use in `music/` and
point edit recipes at them (`"music": "music/track.mp3"`). Keep a `music/LICENSES.txt` next to them with each
track's source and licence, so you can copy the credit into the video description.

Two sources the author used:

- **Kevin MacLeod (incompetech.com), CC BY 4.0.** Free to use, including on monetised videos, **if you
  credit him**. A typical credit line for the description:
  ```
  "Track Title" Kevin MacLeod (incompetech.com)
  Licensed under Creative Commons: By Attribution 4.0 License
  http://creativecommons.org/licenses/by/4.0/
  ```
- **NCS (NoCopyrightSounds).** Free for YouTube/Twitch content under their usage policy, **with the credit
  block shown on each track's page** (artist, track, "Music provided by NoCopyrightSounds", links). Read the
  current policy on ncs.io; some uses (e.g. ads, putting the track on streaming platforms) are excluded.

Other options: YouTube Audio Library, Pixabay Music, or your own tracks. Whatever you use, check the terms on
the day you download, keep the licence text, and credit as required.

The impact sounds (thuds, bass hits) in `make_edit.py` and `beat_montage.py` are synthesised by ffmpeg from a
formula, so there's nothing to license.
