"""Shared helpers for the ClutchCut scripts."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Everything lives inside one folder: models, tools, streams. By default that's the repository itself;
# set CLUTCHCUT_HOME to keep the (large) data somewhere else, e.g. another drive.
REPO = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("CLUTCHCUT_HOME", REPO)).expanduser().resolve()
STREAMS = ROOT / "streams"
MODELS = ROOT / "models"
TOOLS = ROOT / "tools"
OLLAMA_HOST = os.environ.get("CLUTCHCUT_OLLAMA_HOST", "127.0.0.1:11434")
os.environ.setdefault("HF_HOME", str(MODELS / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(MODELS / "torch"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # the Xet downloader hung on the dev machine; plain HTTPS works

try:
    import numba  # noqa: F401
except (ImportError, OSError) as _e:
    # Windows Smart App Control can block numba's DLL ("An Application Control policy has blocked this file").
    # librosa and PANNs only use numba for speed, so fall back to a plain-Python stand-in (slower, same results).
    for _m in [m for m in sys.modules if m == "numba" or m.startswith("numba.")]:
        del sys.modules[_m]
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_compat"))
    print(f"Note: numba unavailable ({str(_e)[:80]}); using the slower pure-Python fallback.", file=sys.stderr)

# Processes that mean "you're gaming or streaming right now": don't compete for the GPU. Add your own with
# CLUTCHCUT_BUSY="game.exe,other.exe".
BUSY_PROCESSES = {
    "obs64.exe": "OBS", "obs32.exe": "OBS", "streamlabs obs.exe": "Streamlabs",
    "androidemulator.exe": "Gameloop", "androidemulatoren.exe": "Gameloop", "androidemulatorex.exe": "Gameloop",
    "appmarket.exe": "Gameloop", "hd-player.exe": "BlueStacks", "memu.exe": "MEmu", "nox.exe": "Nox",
    "dnplayer.exe": "LDPlayer", "scrcpy.exe": "scrcpy (phone mirroring)",
    "valorant-win64-shipping.exe": "Valorant", "valorant.exe": "Valorant", "tslgame.exe": "PUBG",
}
for _exe in filter(None, os.environ.get("CLUTCHCUT_BUSY", "").lower().split(",")):
    BUSY_PROCESSES[_exe.strip()] = _exe.strip()


if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))  # so scripts can `import games`


def stream_dir(name):
    """streams/<name>/ for one stream."""
    if not name:
        sys.exit("Pass --name <stream-name> (the folder under streams/).")
    return STREAMS / name


def stream_settings(out):
    """Per-stream settings prepare.py saved (game, language), so later steps don't need them repeated."""
    return load_json(Path(out) / "settings.json", {}) or {}


def ensure_not_gaming(force=False):
    """Refuse to start heavy GPU work while a game, emulator or OBS is running (Windows only)."""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout.lower()
    except OSError:
        return
    found = sorted({label for exe, label in BUSY_PROCESSES.items() if f'"{exe}"' in out})
    if found and not force:
        sys.exit(f"Stopped: {', '.join(found)} is running. ClutchCut uses the GPU heavily and would cause lag.\n"
                 f"Close it and run again (or pass --force if you really want to).")


def find_exe(name):
    """Locate an executable even if PATH wasn't refreshed after a winget install."""
    found = shutil.which(name)
    if found:
        return found
    local = TOOLS / name / (name + (".exe" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    if os.environ.get("LOCALAPPDATA") and links.exists():
        return str(links)
    pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if os.environ.get("LOCALAPPDATA") and pkgs.exists():
        for hit in pkgs.rglob(f"{name}.exe"):
            return str(hit)
    sys.exit(f"Could not find {name}. Install it (Windows: winget install Gyan.FFmpeg yt-dlp.yt-dlp; "
             f"macOS: brew install ffmpeg yt-dlp; Linux: your package manager) and retry.")


FFMPEG = None
FFPROBE = None
YTDLP = None


def tools():
    global FFMPEG, FFPROBE, YTDLP
    if FFMPEG is None:
        FFMPEG = find_exe("ffmpeg")
        FFPROBE = find_exe("ffprobe")
        YTDLP = find_exe("yt-dlp")
    return FFMPEG, FFPROBE, YTDLP


def run(cmd, cwd=None, quiet=True):
    """Run a command, raising with its stderr tail on failure."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(map(str, cmd))[:300]}\n{tail}")
    if not quiet:
        print(proc.stdout)
    return proc


_ENCODER = None


def video_encoder(quality=20):
    """ffmpeg video codec args: NVENC on an NVIDIA GPU, otherwise x264 on the CPU (same look, slower)."""
    global _ENCODER
    if _ENCODER is None:
        ffmpeg, _, _ = tools()
        try:
            run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=0.2", "-c:v", "h264_nvenc",
                 "-f", "null", "-"])
            _ENCODER = "nvenc"
            print("Encoding on the GPU (NVENC).")
        except RuntimeError:
            _ENCODER = "x264"
            print("Encoding on the CPU (x264).")
    if _ENCODER == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(quality)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]


FONT_CANDIDATES = {
    # Regular-weight UI font for captions; heavy fonts (Impact, Arial Black) looked too bold in testing.
    "text": ["C:/Windows/Fonts/seguisb.ttf", "/System/Library/Fonts/SFNS.ttf", "/Library/Fonts/Arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"],
    "label": ["C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"],
    "emoji": ["C:/Windows/Fonts/seguiemj.ttf", "/System/Library/Fonts/Apple Color Emoji.ttc",
              "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf"],
}


def find_font(kind):
    """Path to a font file. Override with CLUTCHCUT_FONT_TEXT / _LABEL / _EMOJI."""
    env = os.environ.get(f"CLUTCHCUT_FONT_{kind.upper()}")
    for path in ([env] if env else []) + FONT_CANDIDATES[kind]:
        if path and Path(path).exists():
            return path
    return None


def load_font(kind, size):
    from PIL import ImageFont

    path = find_font(kind)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def probe_duration(path):
    _, ffprobe, _ = tools()
    out = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_size(path):
    _, ffprobe, _ = tools()
    out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
               "-of", "json", str(path)]).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def fmt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(s, default):
    """'1:02:10' / '62:10' / '3730' -> seconds; `default` if it can't be parsed."""
    try:
        parts = [float(p) for p in str(s).strip().split(":")]
    except ValueError:
        return default
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _ollama_up():
    try:
        with urllib.request.urlopen(f"http://{OLLAMA_HOST}/api/version", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def _ollama_exe():
    """The portable Ollama in tools/ollama/ (setup.ps1 puts it there), else one on PATH."""
    portable = TOOLS / "ollama" / ("ollama.exe" if os.name == "nt" else "ollama")
    if portable.exists():
        return str(portable)
    return shutil.which("ollama")


@contextlib.contextmanager
def ollama_server():
    """Run Ollama only for the duration of a job, then shut it down.

    If an Ollama server is already running it is used as-is and left alone. Otherwise one is started with
    its models stored in models/ollama, and when the job ends (or crashes) it is stopped, which also
    unloads the model from the GPU.
    """
    started = None
    if not _ollama_up():
        exe = _ollama_exe()
        if not exe:
            sys.exit("Ollama not found. Run setup.ps1, or install Ollama and make sure `ollama` is on PATH.")
        TOOLS.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, OLLAMA_MODELS=str(MODELS / "ollama"), OLLAMA_HOST=OLLAMA_HOST,
                   OLLAMA_NOPRUNE="1")
        log = open(TOOLS / "ollama-server.log", "ab")
        started = subprocess.Popen([exe, "serve"], env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(60):
            if _ollama_up():
                break
            time.sleep(0.5)
        else:
            started.kill()
            sys.exit(f"Ollama did not start; see {TOOLS / 'ollama-server.log'}")
    try:
        yield f"http://{OLLAMA_HOST}"
    finally:
        if started is not None:
            started.terminate()
            try:
                started.wait(10)
            except subprocess.TimeoutExpired:
                started.kill()
            if os.name == "nt":
                # Ollama spawns a runner child process; make sure none outlives us.
                subprocess.run(["taskkill", "/f", "/im", "ollama_llama_server.exe"], capture_output=True)


def compose_sheet(items, target, cols=4, tile_w=384):
    """Grid of (image_path, label) tiles with the label burned into each corner."""
    from PIL import Image, ImageDraw

    font = load_font("label", 18)
    tiles = []
    for path, label in items:
        im = Image.open(path).convert("RGB")
        im = im.resize((tile_w, int(im.height * tile_w / im.width)))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 8 + 11 * len(label), 24], fill=(0, 0, 0))
        d.text((4, 2), label, fill=(255, 255, 255), font=font)
        tiles.append(im)
    th = tiles[0].height
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w + (cols - 1) * 4, rows * th + (rows - 1) * 4))
    for k, im in enumerate(tiles):
        sheet.paste(im, ((k % cols) * (tile_w + 4), (k // cols) * (th + 4)))
    sheet.save(target, quality=82)


def setup_cuda_dlls():
    """Make pip-installed NVIDIA cuBLAS/cuDNN DLLs visible to ctranslate2 on Windows."""
    if os.name != "nt":
        return
    try:
        import nvidia  # namespace package from nvidia-cublas-cu12 / nvidia-cudnn-cu12
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for sub in Path(base).glob("*/bin"):
            try:
                os.add_dll_directory(str(sub))
            except OSError:
                pass
            os.environ["PATH"] = str(sub) + os.pathsep + os.environ.get("PATH", "")
