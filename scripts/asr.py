"""Speech recognition: Whisper large-v3 by default, or a per-language fine-tune from asr_models.json.

`--asr-model auto` (the default in prepare.py) looks the stream's language up in asr_models.json. A Hugging Face
Whisper fine-tune listed there is converted once to CTranslate2 (into models/whisper/<name>-ct2) and run in
batches over short voice-activity chunks. Languages without an entry use large-v3, which also gives word
timestamps. Add your own language after testing candidates with asr_compare.py.
"""
import json
import os
import shutil
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import MODELS, REPO, fmt_ts, run, setup_cuda_dlls  # noqa: E402

SR = 16000
DEFAULT_MODEL = "large-v3"
TABLE = Path(os.environ.get("CLUTCHCUT_ASR_MODELS", REPO / "asr_models.json"))


def normalize_language(code):
    """'auto' / '' / None -> None (Whisper detects it); anything else is passed to Whisper as-is."""
    code = (code or "").strip().lower()
    return None if code in ("", "auto", "detect") else code


def asr_table():
    """{language code: Hugging Face repo or local model path} from asr_models.json (keys starting with _ ignored)."""
    if not TABLE.exists():
        return {}
    data = json.loads(TABLE.read_text(encoding="utf-8"))
    out = {}
    for code, entry in data.items():
        if code.startswith("_"):
            continue
        out[code.lower()] = entry["model"] if isinstance(entry, dict) else entry
    return out


def convert(repo):
    """Convert a Hugging Face Whisper fine-tune to CTranslate2 once; returns the local model folder.

    Needs `pip install transformers` (only the first time per model)."""
    target = MODELS / "whisper" / (repo.rstrip("/").split("/")[-1] + "-ct2")
    if (target / "model.bin").exists():
        return target
    exe = "ct2-transformers-converter" + (".exe" if os.name == "nt" else "")
    conv = Path(sys.executable).parent / exe
    conv = str(conv) if conv.exists() else shutil.which("ct2-transformers-converter")
    if not conv:
        raise RuntimeError("ct2-transformers-converter not found (it comes with ctranslate2).")
    try:
        import transformers  # noqa: F401
    except ImportError as e:
        raise RuntimeError("Converting a fine-tune needs `pip install transformers`.") from e
    target.parent.mkdir(parents=True, exist_ok=True)
    run([conv, "--model", repo, "--output_dir", str(target), "--quantization", "float16",
         "--copy_files", "preprocessor_config.json"])
    if not (target / "tokenizer.json").exists():
        from transformers import WhisperTokenizerFast
        WhisperTokenizerFast.from_pretrained(repo).save_pretrained(str(target / "_tok"))
        shutil.copy(target / "_tok" / "tokenizer.json", target / "tokenizer.json")
        shutil.rmtree(target / "_tok")
    return target


def resolve_model(name, language):
    """(model to load, batched?) for --asr-model `name`: 'auto' uses asr_models.json, else name as given."""
    if name != "auto":
        return name, False
    repo = asr_table().get(language or "")
    if not repo:
        return DEFAULT_MODEL, False
    if Path(repo).exists():
        return repo, True
    try:
        return str(convert(repo)), True
    except Exception as e:  # conversion needs transformers and a download; don't lose the whole run over it
        print(f"  Could not prepare {repo} ({str(e)[:160]}); using {DEFAULT_MODEL}.", flush=True)
        return DEFAULT_MODEL, False


def load_model(model_name):
    setup_cuda_dlls()
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model_name, device="cuda", compute_type="float16", download_root=str(MODELS / "whisper"))
    except Exception as e:
        print(f"  GPU unavailable for Whisper ({str(e)[:120]}), using CPU (slow).")
        return WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(MODELS / "whisper"))


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def wav_chunks(path, seconds=600):
    with wave.open(str(path), "rb") as w:
        offset = 0.0
        while True:
            frames = w.readframes(SR * seconds)
            if not frames:
                break
            yield offset, np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            offset += seconds


def detect_language(audio, model_name=DEFAULT_MODEL):
    """Language code of the speech, from the first 10 minutes that contain any."""
    model = load_model(model_name)
    try:
        for _, chunk in wav_chunks(audio):
            segments, info = model.transcribe(chunk[: SR * 120], vad_filter=True, without_timestamps=True)
            if any(True for _ in segments):
                print(f"  detected language: {info.language} (p={info.language_probability:.2f}); "
                      f"pass --language to override", flush=True)
                return info.language
        return None
    finally:
        del model


def transcribe(audio, model_name, language=None, task="transcribe", label=""):
    """Whole-stream transcription with word timestamps, 10 minutes at a time.

    language: a Whisper language code, or None to detect it on the first chunk and keep it after that."""
    model = load_model(model_name)
    segs = []
    detected = None
    for offset, chunk in wav_chunks(audio):
        segments, info = model.transcribe(chunk, language=language, task=task,
                                          word_timestamps=(task == "transcribe"),
                                          vad_filter=True, condition_on_previous_text=False,
                                          vad_parameters={"min_silence_duration_ms": 400})
        segments = list(segments)
        if language is None and segments:
            detected = info.language
            language = info.language  # lock it in for the remaining chunks
            print(f"  detected language: {info.language} (p={info.language_probability:.2f}); "
                  f"pass --language to override", flush=True)
        for s in segments:
            segs.append({
                "start": round(offset + s.start, 2), "end": round(offset + s.end, 2), "text": s.text.strip(),
                "words": [{"s": round(offset + w.start, 2), "e": round(offset + w.end, 2), "w": w.word.strip()}
                          for w in (s.words or [])],
            })
        print(f"  {label}...{fmt_ts(offset + len(chunk) / SR)}", flush=True)
    del model
    return {"model": str(model_name), "language": language, "detected": detected is not None, "task": task,
            "segments": segs}


def transcribe_batched(audio, model_name, language, label=""):
    """Short voice-activity chunks (up to 8 s), transcribed in batches: much faster, and each chunk keeps its
    own start/end. Used for fine-tunes, many of which were trained without timestamps; there are no word times."""
    import soundfile as sf
    from faster_whisper import BatchedInferencePipeline

    model = load_model(model_name)
    pipe = BatchedInferencePipeline(model=model)
    data, _ = sf.read(str(audio), dtype="float32")  # an array, not a path: a path hit a PyAV incompatibility
    segs = []
    step = SR * 1800  # 30 minutes at a time keeps memory flat
    for start in range(0, len(data), step):
        offset = start / SR
        found, _ = pipe.transcribe(data[start:start + step], language=language, batch_size=16, chunk_length=8,
                                   without_timestamps=True,
                                   vad_parameters={"min_silence_duration_ms": 300, "max_speech_duration_s": 8})
        segs += [{"start": round(offset + s.start, 2), "end": round(offset + s.end, 2), "text": s.text.strip(),
                  "words": []} for s in found if s.text.strip()]
        print(f"  {label}...{fmt_ts(offset + min(step, len(data) - start) / SR)}", flush=True)
    del pipe, model
    return {"model": str(model_name), "language": language, "detected": False, "task": "transcribe",
            "segments": segs}
