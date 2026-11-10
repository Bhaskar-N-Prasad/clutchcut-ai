"""Stage 1: turn a stream VOD into everything needed to "watch" it.

Usage:
  python prepare.py <youtube-url-or-file> --name <short-name> [--language auto|en|hi|kn|es|...]
                    [--game pubg_mobile|valorant] [--force] [--sheets]

--language is any Whisper language code; the default "auto" lets Whisper detect it from the first
10 minutes of speech and keeps it for the rest (mixed-language talk can otherwise flip between chunks).
--asr-model auto (default) uses a per-language fine-tune listed in asr_models.json if there is one
(e.g. Kannada), else Whisper large-v3. See scripts/asr.py and scripts/asr_compare.py.

Creates streams/<name>/ with (each step cached, so re-running continues where it stopped):
  vod.mp4                      the stream
  settings.json                game and language for this stream, read by the later steps
  voice16.wav / game16.wav     your team's voices and the game sound, separated (Demucs)
  sound_events.json            per-second laughter / shouting / gunfire / explosion scores (PANNs)
  transcript.json              speech in the spoken language (word timestamps with large-v3; fine-tunes
                               give one start/end per short chunk instead)
  transcript_en.json           rough English translation from Whisper (skipped when the speech is English)
  transcript.txt               both, side by side, easy to read
  frames/000001.jpg ...        one frame per second, 960px wide
  segments.json                10-minute segments with the frame times to watch
  sheets/                      (only with --sheets) contact sheets for Claude to watch directly
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (MODELS, compose_sheet, ensure_not_gaming, fmt_ts, load_json,  # noqa: E402
                    probe_duration, run, save_json, stream_dir, tools)
from asr import (SR, asr_table, detect_language, normalize_language, read_wav,  # noqa: E402,F401
                 resolve_model, transcribe, transcribe_batched)

VOICE_CLASSES = {
    "laugh": ["Laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle", "Baby laughter"],
    "shout": ["Shout", "Yell", "Screaming", "Battle cry", "Children shouting", "Whoop"],
}
GAME_CLASSES = {
    "gun": ["Gunshot, gunfire", "Machine gun", "Fusillade", "Cap gun", "Artillery fire"],
    "explosion": ["Explosion", "Boom", "Burst, pop"],
}


def step(msg):
    print(f"\n== {msg}", flush=True)


# ---------------------------------------------------------------- download / audio

def download(src, out, start=0):
    """start: skip this many seconds of a YouTube stream; all later times count from there."""
    vod = out / "vod.mp4"
    if vod.exists():
        return vod
    ffmpeg, _, ytdlp = tools()
    p = Path(src)
    if p.exists():
        step("Copying local video")
        if p.suffix.lower() == ".mp4":
            import shutil
            shutil.copy2(p, vod)
        else:
            run([ffmpeg, "-y", "-i", str(p), "-c", "copy", str(vod)])
        return vod
    step("Downloading VOD (a 2-hour stream takes a while)")
    # Section downloads fail while YouTube is still processing a stream, so fetch it all and cut here.
    full = out / "full.mp4" if start else vod
    if not full.exists():
        run([ytdlp, "--no-playlist", "-o", str(full), "--no-part",
             "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
             "--merge-output-format", "mp4", src])
    if start:
        run([ffmpeg, "-y", "-ss", str(start), "-i", str(full), "-c", "copy", str(vod)])
        full.unlink()
    try:
        meta = json.loads(run([ytdlp, "--no-playlist", "-J", src]).stdout)
        save_json(out / "meta.json", {**{k: meta.get(k) for k in ("title", "channel", "duration", "webpage_url", "upload_date")},
                                      "starts_at": start})
    except Exception:
        pass
    if not vod.exists():
        sys.exit("Download finished but vod.mp4 is missing. Check the link.")
    return vod


def extract_audio(out):
    ffmpeg, _, _ = tools()
    a44 = out / "audio44.wav"
    if not a44.exists():
        step("Extracting audio")
        run([ffmpeg, "-y", "-i", "vod.mp4", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "audio44.wav"], cwd=out)
    return a44


def separate(out):
    """Split voices from game sound with Demucs, 5 minutes at a time to keep RAM low."""
    voice, game = out / "voice16.wav", out / "game16.wav"
    if voice.exists() and game.exists():
        return voice, game
    step("Separating voices from game sound (Demucs on GPU)")
    import soundfile as sf
    import torch
    import torchaudio.functional as AF
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = get_model("htdemucs").to(dev).eval()
    vi = model.sources.index("vocals")
    a44 = out / "audio44.wav"
    tmp_v, tmp_g = voice.with_suffix(".part.wav"), game.with_suffix(".part.wav")
    with sf.SoundFile(a44) as f, \
            sf.SoundFile(tmp_v, "w", SR, 1, "PCM_16") as fv, sf.SoundFile(tmp_g, "w", SR, 1, "PCM_16") as fg:
        total = f.frames / f.samplerate
        done = 0.0
        while True:
            data = f.read(44100 * 300, dtype="float32", always_2d=True)
            if not len(data):
                break
            wav = torch.from_numpy(data.T.copy()).to(dev)
            ref = wav.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                src = apply_model(model, ((wav - mean) / std)[None], device=dev, split=True, overlap=0.1,
                                  progress=False)[0]
            src = src * std + mean
            vocals = src[vi].mean(0)
            rest = (src.sum(0) - src[vi]).mean(0)
            fv.write(AF.resample(vocals, 44100, SR).clamp(-1, 1).cpu().numpy())
            fg.write(AF.resample(rest, 44100, SR).clamp(-1, 1).cpu().numpy())
            done += len(data) / 44100
            print(f"  ...{fmt_ts(done)} / {fmt_ts(total)}", flush=True)
    tmp_v.replace(voice)
    tmp_g.replace(game)
    del model
    torch.cuda.empty_cache()
    return voice, game



# ---------------------------------------------------------------- sound events

def _panns_files():
    """PANNs looks for its files in <home>/panns_data and would try `wget` (absent on Windows).
    Keep them inside models/ and point the library there."""
    home = MODELS / "panns_home"
    data = home / "panns_data"
    data.mkdir(parents=True, exist_ok=True)
    files = {
        "class_labels_indices.csv": "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv",
        "Cnn14_DecisionLevelMax.pth": "https://zenodo.org/record/3987831/files/Cnn14_DecisionLevelMax_mAP%3D0.385.pth?download=1",
    }
    for name, url in files.items():
        if not (data / name).exists():
            print(f"  downloading {name}...", flush=True)
            urllib.request.urlretrieve(url, data / (name + ".part"))
            (data / (name + ".part")).replace(data / name)
    import os
    os.environ["USERPROFILE"] = str(home)  # Path.home() for this process only
    return data / "Cnn14_DecisionLevelMax.pth"


def sound_events(out, voice, game, n_sec):
    path = out / "sound_events.json"
    if path.exists():
        return load_json(path)
    step("Detecting laughs, shouts, gunfire, explosions (PANNs on GPU)")
    ckpt = _panns_files()
    import torch
    import torchaudio.functional as AF
    from panns_inference import SoundEventDetection, labels

    sed = SoundEventDetection(checkpoint_path=str(ckpt), device="cuda" if torch.cuda.is_available() else "cpu")

    def per_second(audio, groups):
        idx = {g: [labels.index(c) for c in names if c in labels] for g, names in groups.items()}
        res = {g: np.zeros(n_sec) for g in groups}
        chunk = SR * 60
        for start in range(0, len(audio), chunk):
            seg = torch.from_numpy(audio[start:start + chunk])
            seg32 = AF.resample(seg, SR, 32000).numpy()[None, :]
            fw = sed.inference(seg32)[0]  # (frames at 100/s, 527)
            secs = fw.shape[0] // 100
            if secs == 0:
                continue
            fw = fw[: secs * 100].reshape(secs, 100, -1).max(axis=1)
            s0 = start // SR
            for g, ids in idx.items():
                vals = fw[:, ids].max(axis=1)
                end = min(n_sec, s0 + secs)
                res[g][s0:end] = vals[: end - s0]
        return res

    v = read_wav(voice)
    g = read_wav(game)
    ev = per_second(v, VOICE_CLASSES)
    ev.update(per_second(g, GAME_CLASSES))
    # Voice loudness in dB per second.
    n = min(n_sec, len(v) // SR)
    vv = v[: n * SR].reshape(n, SR)
    db = np.full(n_sec, -90.0)
    db[:n] = 20 * np.log10(np.sqrt((vv * vv).mean(axis=1)) + 1e-6)
    ev["voice_db"] = db
    data = {k: np.round(x, 3).tolist() for k, x in ev.items()}
    save_json(path, data)
    del sed
    torch.cuda.empty_cache()
    return data


# ---------------------------------------------------------------- transcript (models: asr.py)


def transcripts(out, voice, asr_name, language, translate):
    tk, te = out / "transcript.json", out / "transcript_en.json"
    if not tk.exists():
        if language is None and asr_name == "auto" and asr_table():
            # A per-language model may exist, so find out the language before choosing the model.
            step("Detecting the spoken language")
            language = detect_language(voice)
        model, batched = resolve_model(asr_name, language)
        step(f"Transcribing speech ({Path(model).name}, language: {language or 'auto-detect'})")
        if batched:
            save_json(tk, transcribe_batched(voice, model, language, label="speech "))
        else:
            save_json(tk, transcribe(voice, model, language=language, label="speech "))
    spoken = load_json(tk)
    if translate and spoken.get("language") == "en":
        translate = False  # already English
    if translate and not te.exists():
        step("Rough English translation (Whisper translate)")
        save_json(te, transcribe(voice, "large-v3", language=spoken.get("language"), task="translate",
                                 label="english "))
    en = load_json(te, {"segments": []})
    lines = [f"--- Speech ({spoken.get('language') or 'unknown'}, Whisper {spoken.get('model')}) ---"]
    for s in spoken["segments"]:
        lines.append(f"[{fmt_ts(s['start'])}] {s['text']}")
    if en["segments"]:
        lines.append("\n--- English (rough, Whisper translate) ---")
        for s in en["segments"]:
            lines.append(f"[{fmt_ts(s['start'])}] {s['text']}")
    (out / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    return spoken, en


# ---------------------------------------------------------------- frames / segments / sheets

def extract_frames(out):
    fdir = out / "frames"
    marker = fdir / ".done"
    if marker.exists():
        return fdir
    step("Extracting one frame per second (960px)")
    fdir.mkdir(exist_ok=True)
    ffmpeg, _, _ = tools()
    args = ["-i", "vod.mp4", "-an", "-vf", "fps=1,scale=960:-2", "-q:v", "3", "frames/%06d.jpg"]
    try:
        run([ffmpeg, "-y", "-hwaccel", "cuda", *args], cwd=out)
    except RuntimeError:
        run([ffmpeg, "-y", *args], cwd=out)
    marker.write_text("ok")
    return fdir


def frame_path(out, t):
    """Frame N (1-based) of the fps=1 extraction shows second N-1."""
    return out / "frames" / f"{int(t) + 1:06d}.jpg"


def build_segments(out, events, n_sec, seg_len, quiet_step):
    laugh, shout = np.array(events["laugh"]), np.array(events["shout"])
    gun, expl = np.array(events["gun"]), np.array(events["explosion"])
    db = np.array(events["voice_db"])
    loud = db > (np.median(db[db > -80]) + 12 if (db > -80).any() else 0)
    active = (laugh > 0.2) | (shout > 0.2) | (gun > 0.3) | (expl > 0.3) | loud
    # Keep a few seconds around anything active.
    k = np.ones(7)
    active = np.convolve(active.astype(float), k, mode="same") > 0
    segs = []
    for i, s in enumerate(range(0, n_sec, seg_len)):
        e = min(n_sec, s + seg_len)
        times = [t for t in range(s, e) if (active[t] or (t - s) % quiet_step == 0) and frame_path(out, t).exists()]
        segs.append({"id": f"seg{i + 1:02d}", "start": s, "end": e, "frames": times,
                     "active_seconds": int(active[s:e].sum())})
    save_json(out / "segments.json", segs)
    return segs


def make_sheets(out, segments, per_sheet=16, cols=4, tile_w=384):
    """Contact sheets for Claude to watch directly (fallback when the local model isn't good enough)."""
    sdir = out / "sheets"
    sdir.mkdir(exist_ok=True)
    count = 0
    for seg in segments:
        for j in range(0, len(seg["frames"]), per_sheet):
            target = sdir / f"{seg['id']}_{j // per_sheet + 1:02d}.jpg"
            if not target.exists():
                times = seg["frames"][j:j + per_sheet]
                compose_sheet([(frame_path(out, t), fmt_ts(t)) for t in times], target, cols, tile_w)
            count += 1
    print(f"  {count} sheets in {sdir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="YouTube link or path to a video file")
    ap.add_argument("--name", required=True, help="short name for this stream, e.g. 2026-10-29-valorant")
    ap.add_argument("--language", default=None,
                    help="spoken language as a Whisper code (en, hi, kn, ta, es, pt, ja, ...) or 'auto' "
                         "(default, or CLUTCHCUT_LANGUAGE)")
    ap.add_argument("--game", default=None, help="game profile for HUD scanning (see hud_scan.py --list)")
    ap.add_argument("--asr-model", default="auto",
                    help="'auto' (per-language model from asr_models.json, else large-v3), a Whisper model name, "
                         "or a path to a CTranslate2 model")
    ap.add_argument("--no-translate", action="store_true", help="skip Whisper's rough English translation")
    ap.add_argument("--segment-minutes", type=int, default=10)
    ap.add_argument("--quiet-step", type=int, default=4, help="seconds between frames in quiet stretches")
    ap.add_argument("--sheets", action="store_true", help="also build contact sheets for Claude to watch")
    ap.add_argument("--force", action="store_true", help="run even if a game/OBS is open")
    ap.add_argument("--start", default="0", help="skip the stream before this time, e.g. 9:00")
    args = ap.parse_args()

    ensure_not_gaming(args.force)
    import os
    out = stream_dir(args.name)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Working folder: {out}")
    settings = load_json(out / "settings.json", {}) or {}
    if args.language is not None or "language" not in settings:
        settings["language"] = args.language or os.environ.get("CLUTCHCUT_LANGUAGE", "auto")
    if args.game:
        import games
        settings["game"] = games.get(args.game).NAME
    save_json(out / "settings.json", settings)
    language = normalize_language(settings["language"])

    timings = load_json(out / "timings.json", {})

    def timed(label, fn, *a):
        t0 = time.time()
        res = fn(*a)
        timings[label] = round(timings.get(label, 0) + time.time() - t0)
        save_json(out / "timings.json", timings)
        return res

    start = sum(float(p) * 60 ** k for k, p in enumerate(reversed(args.start.split(":"))))
    timed("download", download, args.src, out, int(start))
    n_sec = int(probe_duration(out / "vod.mp4")) + 1
    print(f"Stream length: {fmt_ts(n_sec)}")
    timed("extract_audio", extract_audio, out)
    voice, game = timed("separate_voices", separate, out)
    events = timed("sound_events", sound_events, out, voice, game, n_sec)
    spoken, _ = timed("transcribe", transcripts, out, voice, args.asr_model, language, not args.no_translate)
    if spoken.get("language") and settings.get("language") in (None, "auto"):
        settings["detected_language"] = spoken["language"]
        save_json(out / "settings.json", settings)
    timed("frames", extract_frames, out)
    segs = build_segments(out, events, n_sec, args.segment_minutes * 60, args.quiet_step)
    total = sum(len(s["frames"]) for s in segs)
    print(f"\n{len(segs)} segments, {total} frames to watch "
          f"({sum(s['active_seconds'] for s in segs) // 60} min of action).")
    if args.sheets:
        step("Building contact sheets")
        make_sheets(out, segs)
    (out / "audio44.wav").unlink(missing_ok=True)  # large and no longer needed
    print("\nPrepare done.")


if __name__ == "__main__":
    main()
