"""Compare speech-recognition models on a sample of your own audio, to pick one for your language.

Usage:
  python asr_compare.py --name <stream-name> --start 0:30:00 [--dur 360] [--language hi] [--models ...]
  python asr_compare.py --audio sample.wav --out bench/ [--language hi] [--models ...]

--models is comma-separated: Whisper names (large-v3, large-v3-turbo, medium), Hugging Face Whisper fine-tunes
("org/name", converted to CTranslate2 once into models/whisper/<name>-ct2; needs `pip install transformers`),
or local CTranslate2 folders. Default: large-v3, large-v3-turbo and the asr_models.json entry for --language.
--prompt / --prompt-file adds a run of large-v3 with an initial prompt: a short list of words your group actually
says (slang, game terms, in your script) can nudge Whisper toward them.

Separates voices from game sound first (Demucs), then writes <out>/<setup>.txt with one timestamped line per
segment and prints the time each setup took. Read them side by side; if a fine-tune clearly wins, add it to
asr_models.json so `prepare.py --asr-model auto` uses it. Pick a stretch with lots of team talk over gameplay.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fmt_ts, parse_ts, run, stream_dir, stream_settings, tools  # noqa: E402
from asr import SR, asr_table, convert, load_model, normalize_language  # noqa: E402


def transcribe(audio, model, language, prompt=None, batched=False, beam=5):
    import soundfile as sf

    m = load_model(str(model))
    data, _ = sf.read(str(audio), dtype="float32")  # an array, not a path: a path hit a PyAV incompatibility
    if batched:
        from faster_whisper import BatchedInferencePipeline
        segs, _ = BatchedInferencePipeline(model=m).transcribe(
            data, language=language, batch_size=16, chunk_length=8, without_timestamps=True,
            vad_parameters={"min_silence_duration_ms": 300, "max_speech_duration_s": 8})
    else:
        segs, _ = m.transcribe(data, language=language, beam_size=beam, initial_prompt=prompt,
                               vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
                               condition_on_previous_text=False, no_speech_threshold=0.5,
                               compression_ratio_threshold=2.2)
    out = [(s.start, s.text.strip()) for s in segs if s.text.strip()]
    del m
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", help="any audio/video file (instead of --name)")
    ap.add_argument("--out", help="output folder (default with --name: streams/<name>/bench)")
    ap.add_argument("--name", help="use a prepared stream's separated voices")
    ap.add_argument("--start", default="0", help="with --name: where the sample starts, e.g. 0:30:00")
    ap.add_argument("--dur", type=float, default=360, help="with --name: sample length in seconds")
    ap.add_argument("--language", default=None, help="Whisper language code (default: the stream's, else auto)")
    ap.add_argument("--models", help="comma-separated models to compare (see above)")
    ap.add_argument("--prompt", help="initial prompt for an extra large-v3 run")
    ap.add_argument("--prompt-file", help="read the prompt from a UTF-8 text file")
    args = ap.parse_args()
    if not args.audio and not args.name:
        sys.exit("Pass --audio <file> or --name <stream>.")

    ffmpeg, _, _ = tools()
    offset = 0.0
    if args.name:
        src = stream_dir(args.name)
        language = normalize_language(args.language or stream_settings(src).get("language"))
        out = Path(args.out) if args.out else src / "bench"
        out.mkdir(parents=True, exist_ok=True)
        offset = parse_ts(args.start, 0)
        voice = out / "voice.wav"
        if not voice.exists():
            run([ffmpeg, "-y", "-v", "error", "-ss", str(offset), "-t", str(args.dur), "-i", str(src / "voice16.wav"),
                 "-ac", "1", "-ar", str(SR), str(voice)])
    else:
        from prepare import separate
        language = normalize_language(args.language)
        out = Path(args.out or "bench")
        sep = out / "sep"
        sep.mkdir(parents=True, exist_ok=True)
        if not (sep / "voice16.wav").exists():
            run([ffmpeg, "-y", "-v", "error", "-i", args.audio, "-ac", "2", "-ar", "44100", str(sep / "audio44.wav")])
            separate(sep)
            (sep / "audio44.wav").unlink()
        voice = sep / "voice16.wav"

    names = args.models.split(",") if args.models else (
        ["large-v3", "large-v3-turbo"] + ([asr_table()[language]] if language in asr_table() else []))
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip() if args.prompt_file else args.prompt

    setups = []
    for name in (n.strip() for n in names if n.strip()):
        fine_tune = "/" in name and not Path(name).exists()
        model = convert(name) if fine_tune else name
        tag = Path(name).name if Path(name).exists() else name.split("/")[-1]
        setups.append((tag, model, None, fine_tune or Path(name).exists()))
        if name == "large-v3" and prompt:
            setups.append((tag + "_prompt", model, prompt, False))
    print(f"Language: {language or 'auto-detect'}; sample: {voice}")
    for tag, model, pr, batched in setups:
        if (out / f"{tag}.txt").exists():
            print(f"{tag}: already done")
            continue
        t0 = time.time()
        lines = transcribe(voice, model, language, pr, batched)
        text = "\n".join(f"[{fmt_ts(offset + s)}] {t}" for s, t in lines)
        (out / f"{tag}.txt").write_text(text or "(nothing recognised)", encoding="utf-8")
        print(f"{tag}: {len(lines)} lines in {time.time() - t0:.0f}s", flush=True)
    print(f"Done: {out}")


if __name__ == "__main__":
    main()
