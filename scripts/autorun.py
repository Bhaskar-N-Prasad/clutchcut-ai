"""ClutchCut autorun (Windows): after your OBS stream ends, find the VOD on YouTube, ask you, then prepare it.

Set it up once in autorun.json (copy autorun.example.json): your channel URL, and OBS's path if it isn't in the
usual place. Then start OBS with launch_obs.cmd instead of your usual shortcut. See docs/autorun.md.

  autorun.py launch      start OBS, wait idle until OBS (and any game) closes, then do what 'watch' does
  autorun.py watch       find your newest stream, wait for YouTube to finish processing it, ask you, start
         [--since 12h]       only streams that started after this (12h, 2d, 2026-11-10, or a unix time)
         [--url URL]         use this video instead of looking on the channel
         [--dry-run]         check and ask for real, but only print what would run; writes to autorun/dryrun/
         [--answer start|later|skip --moments TEXT --start 9:00 --games valorant,pubg]   no window
  autorun.py run --job autorun/jobs/<name>.json   the pipeline itself (opens in its own window after you say Start)
  autorun.py pending     ask again about streams you answered "Later" to (for example after a restart)

Nothing heavy runs while a game, an emulator or OBS is open: waiting uses Windows process handles (no polling),
and if you start a game while the pipeline runs, the pipeline is frozen until the game closes. Nothing is installed:
no service, scheduled task or startup entry. Everything it writes stays in autorun/ and streams/. Nothing is uploaded.
"""
import argparse
import contextlib
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import BUSY_PROCESSES, REPO, ROOT, STREAMS, fmt_ts, load_json, save_json, tools  # noqa: E402

import games  # noqa: E402

SCRIPTS = REPO / "scripts"
AUTO = ROOT / "autorun"
STATE = AUTO / "state.json"
LOG = AUTO / "autorun.log"
CONFIG_FILE = Path(os.environ.get("CLUTCHCUT_AUTORUN_CONFIG", ROOT / "autorun.json"))
OBS_CANDIDATES = [r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
                  r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe"]

WANT_HEIGHT = 1080            # wait for YouTube's HD version...
ACCEPT_LOWRES_AFTER = 3 * 3600  # ...but take whatever exists after 3 hours
NO_STREAM_GIVE_UP = 60 * 60   # nothing live or new on the channel an hour after OBS closed: you didn't stream
GIVE_UP = 14 * 3600
DELAYS = [120, 300, 600, 900, 1200, 1800]  # between YouTube checks, then every 30 min

NORMAL_PRIORITY = 0x20
IDLE_PRIORITY = 0x40
BELOW_NORMAL = 0x4000
NO_WINDOW = 0x08000000
NEW_CONSOLE = 0x10
QUIET = IDLE_PRIORITY | NO_WINDOW


def config():
    """autorun.json: {"channel": "https://www.youtube.com/@yourchannel", "obs": "...obs64.exe", "language": "auto",
    "prepare_args": []}. CLUTCHCUT_CHANNEL / CLUTCHCUT_OBS override it."""
    cfg = load_json(CONFIG_FILE, {}) or {}
    if os.environ.get("CLUTCHCUT_CHANNEL"):
        cfg["channel"] = os.environ["CLUTCHCUT_CHANNEL"]
    if os.environ.get("CLUTCHCUT_OBS"):
        cfg["obs"] = os.environ["CLUTCHCUT_OBS"]
    if not cfg.get("obs"):
        cfg["obs"] = next((p for p in OBS_CANDIDATES if Path(p).exists()), OBS_CANDIDATES[0])
    return cfg


def python_exe(windowed=False):
    """The project's Python (console python.exe for the pipeline window, pythonw.exe for the silent waiter)."""
    base = Path(sys.executable).parent
    want = "pythonw.exe" if windowed else "python.exe"
    return str(base / want) if (base / want).exists() else sys.executable


# ---------------------------------------------------------------- Windows: processes, waiting, pausing

if os.name == "nt":
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                           wintypes.DWORD]
    k32.WaitForMultipleObjects.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    SYNCHRONIZE, SUSPEND_RESUME, INFINITE = 0x00100000, 0x0800, 0xFFFFFFFF

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]


def processes():
    """[(pid, parent_pid, exe_name_lowercase)] from one cheap snapshot."""
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out, e = [], PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile.lower()))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def busy():
    """{pid: label} for every game, emulator or OBS running now (common.BUSY_PROCESSES + CLUTCHCUT_BUSY)."""
    return {pid: BUSY_PROCESSES[name] for pid, _, name in processes() if name in BUSY_PROCESSES}


def wait_until_clear(say=print):
    """Block until no game, emulator or OBS runs. Sleeps on the process handles: no CPU while you play."""
    announced = None
    while True:
        b = busy()
        if not b:
            return
        labels = ", ".join(sorted(set(b.values())))
        if labels != announced:
            say(f"Waiting for {labels} to close (doing nothing until then).")
            announced = labels
        handles = [h for h in (k32.OpenProcess(SYNCHRONIZE, False, pid) for pid in list(b)[:64]) if h]
        if handles:
            arr = (wintypes.HANDLE * len(handles))(*handles)
            k32.WaitForMultipleObjects(len(handles), arr, False, INFINITE)
            for h in handles:
                k32.CloseHandle(h)
            time.sleep(10)  # games often restart a helper process right after closing; let that settle
        else:
            time.sleep(60)  # protected process (anti-cheat) we may not open: look again in a minute


def process_tree(pid):
    procs = processes()
    tree, grew = {pid}, True
    while grew:
        grew = False
        for p, parent, _ in procs:
            if parent in tree and p not in tree:
                tree.add(p)
                grew = True
    return tree


def freeze(pid, on):
    fn = ntdll.NtSuspendProcess if on else ntdll.NtResumeProcess
    for p in process_tree(pid):
        h = k32.OpenProcess(SUSPEND_RESUME, False, p)
        if h:
            fn(h)
            k32.CloseHandle(h)


def set_own_priority(cls):
    k32.SetPriorityClass(wintypes.HANDLE(-1), cls)


def message_box(text, title="ClutchCut"):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x40000 | 0x10000)  # info, topmost, foreground


# ---------------------------------------------------------------- small helpers

_log_lock = threading.Lock()


def log(msg, also=None):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with _log_lock:
        try:
            print(line, flush=True)
        except (OSError, ValueError, AttributeError):
            pass
        for path in filter(None, [LOG, also]):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def hms(text):
    """'1:23:45', '45:00', '90' -> seconds. Raises ValueError."""
    text = str(text).strip() or "0"
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}", text):
        raise ValueError(text)
    return sum(int(p) * 60 ** k for k, p in enumerate(reversed(text.split(":"))))


def parse_since(text):
    text = str(text).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hd])", text)
    if m:
        return time.time() - float(m[1]) * (3600 if m[2] == "h" else 86400)
    if re.fullmatch(r"\d{9,}", text):
        return float(text)
    return datetime.fromisoformat(text).timestamp()


def guess_games(title):
    """Game profiles named in the title; empty if none (then no HUD scan runs unless you tick one)."""
    return games.guess_from_title(title)


def state():
    return load_json(STATE, {"handled": {}, "later": {}})


def save_state(s):
    AUTO.mkdir(parents=True, exist_ok=True)
    save_json(STATE, s)


def already_done(video_id):
    """True if this video was handled before, by autorun or by hand."""
    if video_id in state()["handled"]:
        return True
    for d in STREAMS.glob("*") if STREAMS.exists() else []:
        if d.name.endswith(video_id) or video_id in json.dumps(load_json(d / "meta.json", {}) or {}):
            return True
    return False


# ---------------------------------------------------------------- YouTube (yt-dlp only, no API keys)

def ytdlp_json(args):
    _, _, ytdlp = tools()
    p = subprocess.run([ytdlp, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, creationflags=QUIET)
    if p.returncode:
        raise RuntimeError(" ".join(p.stderr.strip().splitlines()[-3:])[:400])
    return json.loads(p.stdout)


def video_info(url):
    return ytdlp_json(["--no-playlist", "-J", url])


def vod_status(info):
    live = info.get("live_status")
    if live in ("is_live", "is_upcoming"):
        return "still live"
    if live == "post_live" or not info.get("duration"):
        return "processing"
    heights = [f.get("height") or 0 for f in info.get("formats") or [] if f.get("vcodec") not in (None, "none")]
    return "ready" if max(heights, default=0) >= WANT_HEIGHT else "low-res"


def channel_url():
    url = (config().get("channel") or "").rstrip("/")
    if not url:
        sys.exit(f"No channel set. Copy autorun.example.json to {CONFIG_FILE} and put your channel URL in it "
                 f"(e.g. https://www.youtube.com/@yourchannel), or pass --url.")
    return url


def find_new_stream(since):
    """Newest stream on the channel that started after `since` and wasn't handled yet, or None."""
    entries = ytdlp_json(["--flat-playlist", "--playlist-end", "4", "-J", channel_url() + "/streams"]).get("entries") or []
    for e in entries[:3]:
        vid = e.get("id")
        if not vid or already_done(vid):
            continue
        info = video_info(f"https://www.youtube.com/watch?v={vid}")
        started = info.get("release_timestamp") or info.get("timestamp") or 0
        if started and started < since:
            return None  # the list is newest first, so everything after is older too
        return info
    return None


def wait_for_vod(since, url=None, dry_run=False):
    """Find the stream and wait (gently) until YouTube has finished processing it. Returns its info or None."""
    t0, delays, info = time.time(), iter(DELAYS), None
    while True:
        wait_until_clear(log)  # not even a web request while you play
        try:
            info = video_info(url) if url else find_new_stream(since)
            status = vod_status(info) if info else "no new stream yet"
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001  network hiccup, or YouTube says "processing"
            status = f"not available yet ({str(e)[:160]})"
        if info:
            url = info.get("webpage_url") or url
        log(f"YouTube check: {info.get('title') if info else 'channel'} -> {status}")
        waited = time.time() - t0
        if status == "ready" or (status == "low-res" and waited > ACCEPT_LOWRES_AFTER):
            return info
        if dry_run:
            log(f"[dry run] would keep checking (next in {DELAYS[0] // 60} min)." if status != "low-res"
                else "[dry run] only low-res formats yet; would wait for HD up to 3 h.")
            return info
        if not url and waited > NO_STREAM_GIVE_UP:
            log("No new stream showed up on the channel. Stopping.")
            return None
        if waited > GIVE_UP:
            log("Gave up: YouTube still hasn't finished processing.")
            message_box(f"YouTube still hasn't finished processing your stream after {GIVE_UP // 3600} hours.\n"
                        f"Run check_latest_stream.cmd later to try again.\n\n{url}")
            return None
        time.sleep(next(delays, 1800))


# ---------------------------------------------------------------- "moments you remember"

def parse_moments(text, duration, start):
    """'1:23 clutch, 45:00 funny revive' -> list of moments with stream and vod times.

    '1:23' could mean 1 min 23 s or 1 h 23 min; when both fit inside the stream, both are kept."""
    colon = re.compile(r"(?<![\d:])(\d{1,2}(?::\d{2}){1,2})(?![\d:])")
    words = re.compile(r"\b(\d+)\s*h(?:\s*(\d+)\s*m(?:in)?)?|\b(\d+)\s*(?:m|min|mins|minutes)\b", re.I)
    found = []
    for piece in filter(None, (p.strip() for p in re.split(r"[,;\n]+", text or ""))):
        hits = list(colon.finditer(piece)) or list(words.finditer(piece))
        if not hits:
            found.append({"said": piece, "note": piece, "stream_t": None})
            continue
        for k, m in enumerate(hits):
            def clean(s):
                s = re.sub(r"\s+", " ", s).strip(" -:@.")
                return re.sub(r"^(?:(?:at|around|near|and|then)\b\s*)+|(?:\s*\b(?:at|around|near|and|then))+$", "",
                              s, flags=re.I).strip(" -:@.")
            end = hits[k + 1].start() if k + 1 < len(hits) else len(piece)
            begin = hits[k - 1].end() if k else 0
            note = clean(piece[m.end():end]) or clean(piece[begin:m.start()]) or piece
            if m.re is colon:
                t = hms(m[1])
                alt = None
                parts = m[1].split(":")
                if len(parts) == 2:
                    a = int(parts[0]) * 3600 + int(parts[1]) * 60
                    alt = a if a <= (duration or 0) + 60 else None
            elif m[3]:
                t, alt = int(m[3]) * 60, None
            else:
                t, alt = int(m[1]) * 3600 + int(m[2] or 0) * 60, None
            item = {"said": m[0], "note": note, "stream_t": t, "vod_t": t - start}
            if alt is not None:
                item.update(ambiguous=True, alt_stream_t=alt, alt_vod_t=alt - start)
            if t - start < 0:
                item["before_skipped_start"] = True
            found.append(item)
    return found


def write_moments(folder, text, duration, start):
    data = {
        "raw": text or "",
        "skipped_start_s": start,
        "how_to_use": "Must-check candidates for picking. stream_t = time on YouTube; vod_t = time in vod.mp4 and "
                      "every other file here (the warm-up was cut). 'ambiguous' means '1:23' may be 1:23 or 1:23:00; "
                      "check both. Moments with stream_t null had no time: search the transcript.",
        "moments": parse_moments(text, duration, start),
    }
    folder.mkdir(parents=True, exist_ok=True)
    save_json(folder / "user_moments.json", data)
    return data


# ---------------------------------------------------------------- the question window

def ask(info, prefill=None, snapshot=None):
    """Start now / Later / Skip, plus the moments box. Returns a dict."""
    import tkinter as tk
    from tkinter import ttk
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    prefill = prefill or {}
    title = info.get("title") or "(untitled stream)"
    when = info.get("release_timestamp") or info.get("timestamp")
    when = datetime.fromtimestamp(when).strftime("%a %d %b, %H:%M") if when else "recently"
    res = {"action": "later", "later_min": 60}

    root = tk.Tk()
    root.title("ClutchCut: your stream ended")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    f = ttk.Frame(root, padding=16)
    f.grid()
    ttk.Label(f, text=title, font=("Segoe UI", 13, "bold"), wraplength=520).grid(row=0, column=0, columnspan=4, sticky="w")
    ttk.Label(f, text=f"{fmt_ts(info.get('duration') or 0)} long  ·  streamed {when}  ·  {info.get('webpage_url', '')}",
              foreground="#555").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))
    ttk.Label(f, text="Prepare it for editing now? It downloads the stream and uses the GPU for about an hour.\n"
                      "If you open a game or OBS meanwhile, it freezes itself until you close it.",
              wraplength=520).grid(row=2, column=0, columnspan=4, sticky="w")

    profiles = games.available()
    chosen = prefill.get("games", guess_games(title))
    game_vars = {g: tk.BooleanVar(value=g in chosen) for g in sorted(profiles)}
    gf = ttk.Frame(f)
    gf.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
    ttk.Label(gf, text="HUD scan:").pack(side="left")
    for g, var in game_vars.items():
        ttk.Checkbutton(gf, text=profiles[g].TITLE.split(":")[0], variable=var).pack(side="left", padx=6)

    sf = ttk.Frame(f)
    sf.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
    ttk.Label(sf, text="Skip the warm-up: start from").pack(side="left")
    start_var = tk.StringVar(value=prefill.get("start", "0:00"))
    ttk.Entry(sf, textvariable=start_var, width=9).pack(side="left", padx=6)
    ttk.Label(sf, text="(h:mm:ss on YouTube)", foreground="#555").pack(side="left")

    ttk.Label(f, text="Any moments you remember that should be included?", font=("Segoe UI", 10, "bold")
              ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 2))
    box = tk.Text(f, width=64, height=4, wrap="word", font=("Segoe UI", 10))
    box.grid(row=6, column=0, columnspan=4, sticky="we")
    box.insert("1.0", prefill.get("moments", ""))
    ttk.Label(f, text="Rough YouTube times are fine, e.g.  1:23:00 clutch, 45:00 funny revive",
              foreground="#555").grid(row=7, column=0, columnspan=4, sticky="w")
    err = ttk.Label(f, text="", foreground="#b00020")
    err.grid(row=8, column=0, columnspan=4, sticky="w")

    def collect(action):
        try:
            hms(start_var.get())
        except ValueError:
            err.config(text="The warm-up time should look like 9:00 or 0:09:00.")
            return
        res.update(action=action, games=[g for g, v in game_vars.items() if v.get()], start=start_var.get().strip(),
                   moments=box.get("1.0", "end").strip(), later_min=later_choices[later_var.get()])
        root.destroy()

    later_choices = {"in 30 min": 30, "in 1 hour": 60, "in 3 hours": 180, "in 8 hours": 480}
    later_var = tk.StringVar(value="in 1 hour")
    bf = ttk.Frame(f)
    bf.grid(row=9, column=0, columnspan=4, sticky="e", pady=(12, 0))
    go = ttk.Button(bf, text="Start now", command=lambda: collect("start"))
    go.pack(side="left", padx=4)
    ttk.Button(bf, text="Later:", command=lambda: collect("later")).pack(side="left", padx=(16, 2))
    ttk.Combobox(bf, textvariable=later_var, values=list(later_choices), width=10, state="readonly").pack(side="left")
    ttk.Button(bf, text="Skip this stream", command=lambda: collect("skip")).pack(side="left", padx=(16, 0))
    root.protocol("WM_DELETE_WINDOW", lambda: collect("later"))  # closing the window = ask again later
    root.after(150, lambda: (root.lift(), root.focus_force(), box.focus_set()))

    if snapshot:  # used to test what the window looks like
        def grab():
            from PIL import ImageGrab
            root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(snapshot)
            root.destroy()
        root.after(1200, grab)
    root.mainloop()
    return res


# ---------------------------------------------------------------- watch: find, wait, ask, start

def job_for(info, answer, dry_run):
    vid = info["id"]
    chosen = [games.get(g).NAME for g in answer.get("games") or []]
    tag = "mix" if len(chosen) > 1 else (chosen[0] if chosen else "stream")
    name = f"{'dryrun-' if dry_run else ''}{tag}-{vid}"
    cfg = config()
    return {"name": name, "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}", "id": vid,
            "title": info.get("title"), "duration": info.get("duration"), "games": chosen,
            "start": answer.get("start") or "0", "language": cfg.get("language", "auto"),
            "prepare_args": cfg.get("prepare_args", []), "asked_at": time.time()}


def handle(info, answer, dry_run=False):
    s = state()
    vid = info["id"]
    if answer["action"] == "skip":
        log(f"Skipped by you: {info.get('title')}")
        if not dry_run:
            s["handled"][vid] = "skipped"
            s["later"].pop(vid, None)
            save_state(s)
        return "skip"
    if answer["action"] == "later":
        return "later"
    job = job_for(info, answer, dry_run)
    folder = (AUTO / "dryrun" / job["name"]) if dry_run else (STREAMS / job["name"])
    moments = write_moments(folder, answer.get("moments"), info.get("duration"), hms(job["start"]))
    log(f"Saved {len(moments['moments'])} remembered moment(s) to {folder / 'user_moments.json'}")
    jobs = AUTO / ("dryrun" if dry_run else "jobs")
    jobs.mkdir(parents=True, exist_ok=True)
    job_path = jobs / f"{job['name']}.json"
    save_json(job_path, job)
    if dry_run:
        log("[dry run] would open a new window running:")
        for label, cmd in pipeline_steps(job):
            log(f"    {label}: {' '.join(map(str, cmd))}")
        log(f"[dry run] then write {STREAMS / job['name'] / 'READY.md'}")
        return "start"
    s["handled"][vid] = f"started {job['name']}"
    s["later"].pop(vid, None)
    save_state(s)
    subprocess.Popen([python_exe(), str(Path(__file__).resolve()), "run", "--job", str(job_path)], cwd=REPO,
                     creationflags=NEW_CONSOLE | NORMAL_PRIORITY)
    log(f"Started the pipeline for {job['name']} in a new window.")
    return "start"


def ask_until_answered(info, args, prefill=None):
    prefill = prefill or {}
    while True:
        wait_until_clear(log)  # never pop a window over your game
        if args.answer:
            answer = {"action": args.answer, "moments": args.moments or "", "start": args.start or "0:00",
                      "games": args.games.split(",") if args.games else guess_games(info.get("title")),
                      "later_min": 60}
        else:
            answer = ask(info, prefill, snapshot=args.snapshot)
        log(f"Your answer: {answer['action']}" + (f" (ask again in {answer['later_min']} min)"
                                                  if answer["action"] == "later" else ""))
        result = handle(info, answer, args.dry_run)
        if result != "later" or args.dry_run or args.answer:
            return result
        s = state()
        s["later"][info["id"]] = {"url": info.get("webpage_url"), "title": info.get("title"),
                                  "prefill": {k: answer.get(k) for k in ("games", "start", "moments")}}
        save_state(s)
        prefill = s["later"][info["id"]]["prefill"]
        time.sleep(answer["later_min"] * 60)


def cmd_watch(args):
    since = parse_since(args.since)
    log(f"Looking for a stream that started after {datetime.fromtimestamp(since):%Y-%m-%d %H:%M}"
        + (f" ({args.url})" if args.url else f" on {channel_url()}/streams") + (" [dry run]" if args.dry_run else ""))
    info = wait_for_vod(since, args.url, args.dry_run)
    if not info:
        return
    log(f"Found: {info.get('title')} ({fmt_ts(info.get('duration') or 0)}). Asking you.")
    ask_until_answered(info, args)


def cmd_launch(args):
    mutex = k32.CreateMutexW(None, False, "Local\\ClutchCutAutorunWaiter")
    second = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS: a waiter is already running
    obs = Path(args.obs_exe or config()["obs"])
    launched_at = time.time()
    if not any(name == obs.name.lower() for _, _, name in processes()) or args.obs_exe:
        if not obs.exists():
            message_box(f"OBS not found at:\n{obs}\n\nSet \"obs\" in {CONFIG_FILE}.")
            sys.exit(f"OBS not found at {obs}; set \"obs\" in {CONFIG_FILE}.")
        log(f"Starting {obs}")
        proc = subprocess.Popen([str(obs), *(args.obs_args.split() if args.obs_args else [])], cwd=obs.parent,
                                creationflags=NORMAL_PRIORITY)  # OBS must not inherit our idle priority
    else:
        log("OBS is already running; waiting for it.")
        proc = None
    if second:
        log("Another ClutchCut waiter is already running; this one stops here.")
        return
    if not config().get("channel"):  # pythonw has no console: say it in a window, OBS keeps running
        log(f"No channel set in {CONFIG_FILE}; not waiting for this stream.")
        message_box(f"OBS is starting, but ClutchCut won't look for this stream: no channel is set.\n\n"
                    f"Copy autorun.example.json to\n{CONFIG_FILE}\nand put your YouTube channel URL in it.")
        return
    set_own_priority(IDLE_PRIORITY)
    if proc:
        proc.wait()  # a kernel wait on OBS's handle: zero CPU
    wait_until_clear(log)
    log("OBS closed. Looking for the stream on YouTube.")
    args.since = str(int(launched_at - 15 * 60))
    cmd_watch(args)
    k32.CloseHandle(mutex)


def cmd_pending(args):
    later = state()["later"]
    if not later:
        log("No streams waiting for an answer.")
        return
    for vid, item in list(later.items()):
        try:
            info = video_info(item["url"])
        except Exception as e:  # noqa: BLE001
            log(f"Could not load {item['url']}: {e}")
            continue
        ask_until_answered(info, args, item.get("prefill"))


# ---------------------------------------------------------------- run: the pipeline, in its own window

def pipeline_steps(job):
    name, py, out = job["name"], python_exe(), STREAMS / job["name"]
    one_game = ["--game", job["games"][0]] if len(job["games"]) == 1 else []
    steps = [("prepare", [py, SCRIPTS / "prepare.py", job["url"], "--name", name, "--start", job["start"],
                          "--language", job.get("language") or "auto", *one_game, *job.get("prepare_args", [])])]
    for g in job["games"]:
        profile = games.get(g)
        steps.append((f"hud_scan:{profile.NAME}", [py, SCRIPTS / "hud_scan.py", "--name", name, "--game", profile.NAME]))
        if hasattr(profile, "my_kills"):
            steps.append((f"refine_kills:{profile.NAME}",
                          [py, SCRIPTS / "refine_kills.py", "--game", profile.NAME,
                           "--list", out / f"kill_list_{profile.NAME}.json",
                           "--out", out / f"kill_hits_{profile.NAME}.json", "--sheet", out / f"kill_hits_{profile.NAME}"]))
    return steps


def run_step(label, cmd, logf):
    """Run one step at below-normal priority; freeze it whenever a game/OBS opens. True if it succeeded."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for attempt in range(1, 4):
        wait_until_clear(lambda m: log(m, logf))
        log(f"--- {label} (attempt {attempt})", logf)
        proc = subprocess.Popen([str(c) for c in cmd], cwd=REPO, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=BELOW_NORMAL)
        tail = []

        def pump():
            with open(logf, "a", encoding="utf-8") as f:
                for line in proc.stdout:
                    tail.append(line)
                    del tail[:-30]
                    f.write(line)
                    f.flush()
                    try:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        while proc.poll() is None:
            time.sleep(3)
            b = busy()
            if b and proc.poll() is None:
                labels = ", ".join(sorted(set(b.values())))
                freeze(proc.pid, True)
                log(f"PAUSED: {labels} opened. ClutchCut is frozen (no CPU/GPU use) until it closes.", logf)
                wait_until_clear(lambda m: None)
                freeze(proc.pid, False)
                log("Resumed.", logf)
        reader.join(10)
        if proc.returncode == 0:
            return True
        stopped_for_game = any("Stopped:" in line for line in tail) or busy()
        log(f"{label} exited with code {proc.returncode}.", logf)
        if not stopped_for_game:
            return False
    return False


def kill_list(out, profile):
    """The streamer's own kills from the profile's scan, as refine_kills.py input."""
    kind = getattr(profile, "MY_KILL_TYPE", "you_kill")
    events = load_json(out / profile.OUTPUT, []) or []
    items = [{"video": str(out / "vod.mp4"), "t": e["t"]} for e in events if e.get("type") == kind]
    save_json(out / f"kill_list_{profile.NAME}.json", items)
    return items


def cmd_run(args):
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    job = load_json(args.job)
    out = STREAMS / job["name"]
    out.mkdir(parents=True, exist_ok=True)
    logf = out / "autorun.log"
    ctypes.windll.kernel32.SetConsoleTitleW(f"ClutchCut: {job['title']}")
    log(f"ClutchCut pipeline for: {job['title']}\n  {job['url']}\n  folder: {out}\n"
        f"  Close this window to stop. Re-running continues where it stopped.", logf)
    results = {}
    for label, cmd in pipeline_steps(job):
        if label.startswith("refine_kills:") and not kill_list(out, games.get(label.split(":", 1)[1])):
            results[label] = "skipped (no kills of yours found)"
            continue
        ok = run_step(label, cmd, logf)
        results[label] = "done" if ok else "FAILED (see autorun.log)"
        if label == "prepare" and not ok:
            break
    ready = write_ready(job, out, results)
    log(f"Summary written: {ready}", logf)
    wait_until_clear(lambda m: None)
    failed = [k for k, v in results.items() if v.startswith("FAILED")]
    message_box((f"Something failed: {', '.join(failed)}.\n" if failed else "Your stream is ready for editing.\n")
                + f"\n{job['title']}\n\nSummary: {ready}")
    try:
        input("\nPress Enter to close this window.")
    except (EOFError, OSError):
        pass


def write_ready(job, out, results):
    meta = load_json(out / "meta.json", {}) or {}
    timings = load_json(out / "timings.json", {}) or {}
    settings = load_json(out / "settings.json", {}) or {}
    start = hms(job["start"])
    lines = [f"# Ready for editing: {job['title']}", "",
             f"- Link: {job['url']}",
             f"- Stream length: {fmt_ts(job.get('duration') or meta.get('duration') or 0)}",
             f"- Games (HUD scans): {', '.join(job['games']) or 'none'}",
             f"- Spoken language: {settings.get('detected_language') or settings.get('language') or 'auto'}",
             f"- Warm-up skipped: first {fmt_ts(start)}. **All times in this folder count from there** "
             f"(YouTube time = file time + {fmt_ts(start)}).",
             f"- Prepared: {datetime.now():%Y-%m-%d %H:%M}", "", "## Steps", ""]
    lines += [f"- {k}: {v}" for k, v in results.items()]
    if timings:
        lines.append("- prepare timings (min): " + ", ".join(f"{k} {v / 60:.0f}" for k, v in timings.items()))
    lines += ["", "## Files", ""]
    files = [("vod.mp4", "the stream (warm-up cut)"), ("transcript.txt", "speech, easy to read"),
             ("transcript.json", "speech with timestamps"), ("transcript_en.json", "rough English"),
             ("sound_events.json", "per-second laugh / shout / gun / explosion scores"),
             ("frames", "one frame per second"), ("segments.json", "10-minute segments with frames to watch"),
             ("user_moments.json", "moments you remembered: check these first")]
    for g in job["games"]:
        p = games.get(g)
        files.append((p.OUTPUT, f"{p.TITLE} (hud_scan.py)"))
        if hasattr(p, "my_kills"):
            files.append((f"kill_hits_{p.NAME}.json", "your kills to 0.1 s (refine_kills.py)"))
    for name, what in files:
        if (out / name).exists():
            lines.append(f"- `{name}`: {what}")
    for g in job["games"]:
        p = games.get(g)
        events = load_json(out / p.OUTPUT)
        if events is None or not hasattr(p, "report"):
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p.report(events)
        lines += ["", f"## {p.TITLE}", "", "```", buf.getvalue().strip()[:4000], "```"]
    if len(job["games"]) > 1:
        lines.append("- This stream has several games: each scanner may show a few false hits in the other games' parts.")
    um = load_json(out / "user_moments.json", {}) or {}
    lines += ["", "## Moments you remembered (must-check)", ""]
    if not um.get("moments"):
        lines.append("- none given")
    for m in um.get("moments", []):
        if m.get("stream_t") is None:
            lines.append(f"- \"{m['said']}\" (no time: search the transcript)")
            continue
        first = (f"file time {fmt_ts(m['vod_t'])}" if m["vod_t"] >= 0 else "inside the skipped warm-up")
        alt = f", or file time {fmt_ts(m['alt_vod_t'])} if you meant hours" if m.get("ambiguous") else ""
        lines.append(f"- {m['note']}: {first}{alt} (you said {m['said']})")
    lines += ["", "## Next", "",
              f"Open Claude Code in `{REPO}` and say: *make the edits for streams/{job['name']}, read READY.md first*.",
              "Nothing was uploaded; uploads stay manual."]
    path = out / "READY.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("launch", "watch", "pending"):
        p = sub.add_parser(name)
        p.add_argument("--since", default="12h")
        p.add_argument("--url")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--answer", choices=["start", "later", "skip"])
        p.add_argument("--moments")
        p.add_argument("--start")
        p.add_argument("--games", help="comma-separated game profiles (see hud_scan.py --list)")
        p.add_argument("--snapshot", help=argparse.SUPPRESS)
        p.add_argument("--obs-exe", help=argparse.SUPPRESS)   # for testing the wait with another program
        p.add_argument("--obs-args", help=argparse.SUPPRESS)
    p = sub.add_parser("run")
    p.add_argument("--job", required=True)
    args = ap.parse_args()
    if os.name != "nt":
        sys.exit("autorun.py uses Windows process APIs and only runs on Windows. The rest of ClutchCut doesn't need it.")
    AUTO.mkdir(parents=True, exist_ok=True)
    {"launch": cmd_launch, "watch": cmd_watch, "pending": cmd_pending, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  pythonw has no console: make sure failures are visible in the log
        log(f"ERROR: {type(e).__name__}: {e}")
        raise
