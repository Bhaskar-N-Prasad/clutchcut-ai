"""Stage 2: the local vision model (Qwen on your GPU) watches the whole stream and takes notes.

Usage:
  python local_watch.py --name <stream-name> [--model qwen3-vl:8b] [--game valorant] [--only seg03] [--force]
  python local_watch.py --pull [--model qwen3-vl:8b]      # download the model once
  python local_watch.py --ask --name X --at 1:23:40 --question "Did the streamer get a kill?"

Writes streams/<name>/watch_log/segNN.json + segNN.md as each 10-minute segment finishes
(so it can be stopped and resumed), then digest.md: the notes Claude reads to shortlist moments.
Ollama is started for the job and shut down afterwards; nothing stays running.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (ensure_not_gaming, fmt_ts, load_json, ollama_server, parse_ts, save_json,  # noqa: E402,F401
                    stream_dir, stream_settings)

import games  # noqa: E402

DEFAULT_MODEL = "qwen3-vl:8b"
GROUP = 8  # frames per request

GUIDE_HEAD = """You are watching a gaming livestream{game}, recorded from the streamer's screen.
The streamer may be playing with teammates and talking with them{language}."""

GUIDE_TAIL = """Clip-worthy moments are: good plays (multi-kills, clutches, crazy shots), funny team banter (laughing, roasting),
rage/hype reactions (shouting), funny fails/deaths. Score each event 0-10:
9-10 = must-clip (match win, team wipe, clutch, hilarious moment), 6-8 = good clip, 3-5 = notable but ordinary, 0-2 = nothing.
Only report what the frames, sounds or speech actually show. Do not invent kills you cannot see."""


def build_guide(profile, language):
    game = f" of {profile.TITLE.split(':')[0]}" if profile else ""
    lang = f" (spoken language: {language})" if language and language != "auto" else ""
    return "\n".join([GUIDE_HEAD.format(game=game, language=lang), "", games.watch_hints(profile), "", GUIDE_TAIL])


GUIDE = build_guide(None, None)

SCHEMA = """Reply with JSON only:
{"summary": "<one sentence on what happens in this window>",
 "events": [{"time": "<H:MM:SS of the frame where it happens>", "type": "play|banter|rage|fail|death|win|fight|other",
             "what": "<short description with the evidence>", "score": <0-10>}]}
Use "events": [] if nothing notable happens."""


def sound_lines(events, a, b):
    lines = []
    for key, label, thr in (("laugh", "laughter", 0.2), ("shout", "shouting", 0.2),
                            ("gun", "gunfire", 0.3), ("explosion", "explosion", 0.3)):
        secs = [t for t in range(a, min(b + 1, len(events[key]))) if events[key][t] >= thr]
        if secs:
            peak = max(secs, key=lambda t: events[key][t])
            lines.append(f"- {label} at {', '.join(fmt_ts(t) for t in secs[:8])}"
                         f"{' ...' if len(secs) > 8 else ''} (strongest {fmt_ts(peak)}, {events[key][peak]:.2f})")
    return lines or ["- nothing notable"]


def speech_lines(spoken, en, a, b):
    def pick(tr):
        return [f"[{fmt_ts(s['start'])}] {s['text']}" for s in tr["segments"] if s["end"] >= a - 2 and s["start"] <= b + 2]
    out = pick(spoken)
    eng = pick(en)
    if eng:
        out += ["(rough English translation:)"] + eng
    return out or ["(no speech)"]


def ask_model(client, model, prompt, images, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = client.chat(model=model, messages=[{"role": "user", "content": prompt, "images": images}],
                               format="json", think=False, keep_alive="10m",
                               # num_predict caps runaway repetition loops (one reply ran past 2,700 tokens)
                               options={"temperature": 0.2, "num_ctx": 12288, "num_predict": 1200,
                                        "repeat_penalty": 1.1})
            text = resp["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0) if m else text)
        except Exception as e:
            if attempt == retries:
                return {"summary": f"(model error: {str(e)[:100]})", "events": []}
            time.sleep(2)


def watch_segment(client, model, out, seg, events, spoken, en, guide=GUIDE):
    from common import fmt_ts as f
    lang = spoken.get("language") or "auto-detected"
    frames = seg["frames"]
    windows = []
    for i in range(0, len(frames), GROUP):
        times = frames[i:i + GROUP]
        a, b = times[0], times[-1]
        imgs = [str(out / "frames" / f"{t + 1:06d}.jpg") for t in times]
        prompt = "\n".join([
            guide, "",
            "Frames attached, in order: " + ", ".join(f"{k + 1}) {f(t)}" for k, t in enumerate(times)),
            "", "Sounds in this window:", *sound_lines(events, a, b),
            "", f"What was said (language: {lang}, transcribed automatically, may contain errors):",
            *speech_lines(spoken, en, a, b),
            "", SCHEMA,
        ])
        res = ask_model(client, model, prompt, imgs)
        evs = []
        for ev in res.get("events", []) or []:
            if not isinstance(ev, dict):
                continue
            t = parse_ts(ev.get("time"), a)
            t = min(max(t, a), b)
            try:
                score = float(ev.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            evs.append({"t": round(t), "type": str(ev.get("type", "other")), "what": str(ev.get("what", ""))[:300],
                        "score": score})
        windows.append({"start": a, "end": b, "summary": str(res.get("summary", ""))[:300], "events": evs})
    return windows


def write_segment(out, seg, windows, model, secs):
    log = out / "watch_log"
    log.mkdir(exist_ok=True)
    save_json(log / f"{seg['id']}.json", {"segment": seg["id"], "model": model, "seconds": round(secs),
                                          "windows": windows})
    md = [f"# {seg['id']}  {fmt_ts(seg['start'])}-{fmt_ts(seg['end'])}  (watched by {model})", ""]
    for w in windows:
        md.append(f"- {fmt_ts(w['start'])}-{fmt_ts(w['end'])}: {w['summary']}")
        for ev in w["events"]:
            md.append(f"  - **{fmt_ts(ev['t'])}** [{ev['type']} {ev['score']:.0f}/10] {ev['what']}")
    (log / f"{seg['id']}.md").write_text("\n".join(md), encoding="utf-8")


def digest(out):
    """One compact file for Claude: every scored event, plus loud/laugh peaks the model didn't flag."""
    segs = load_json(out / "segments.json")
    events = load_json(out / "sound_events.json")
    all_ev = []
    for s in segs:
        data = load_json(out / "watch_log" / f"{s['id']}.json")
        if data:
            for w in data["windows"]:
                all_ev += [dict(e, window=f"{fmt_ts(w['start'])}-{fmt_ts(w['end'])}") for e in w["events"]]
    all_ev.sort(key=lambda e: e["t"])
    good = [e for e in all_ev if e["score"] >= 5]
    flagged = [e["t"] for e in good]

    # Safety net: strong laughter/shouting/gunfire bursts with no nearby model event.
    peaks = []
    for key, label in (("laugh", "laughter"), ("shout", "shouting"), ("gun", "gunfire burst")):
        vals = events[key]
        order = sorted(range(len(vals)), key=lambda t: vals[t], reverse=True)
        taken = []
        for t in order:
            if vals[t] < 0.35 or len(taken) >= 15:
                break
            if all(abs(t - u) > 30 for u in taken) and all(abs(t - u) > 20 for u in flagged):
                taken.append(t)
                peaks.append((t, label, vals[t]))
    peaks.sort()

    md = ["# Watch digest", f"{len(all_ev)} events noted by the local model, {len(good)} scored 5+.", "",
          "## Top 25 by score"]
    for e in sorted(good, key=lambda e: -e["score"])[:25]:
        md.append(f"- **{fmt_ts(e['t'])}** ({e['t']}s) [{e['type']} {e['score']:.0f}] {e['what']}")
    md += ["", "## All 5+ events in time order"]
    md += [f"- {fmt_ts(e['t'])} [{e['type']} {e['score']:.0f}] {e['what']}" for e in good]
    md += ["", "## Sound peaks the model did not flag (check these too)"]
    md += [f"- {fmt_ts(t)} ({t}s) {label} {v:.2f}" for t, label, v in peaks] or ["- none"]
    (out / "digest.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Digest: {out / 'digest.md'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--game", help="game profile for the prompt's hints; defaults to the stream's settings.json")
    ap.add_argument("--only", help="comma-separated segment ids to (re)watch")
    ap.add_argument("--pull", action="store_true", help="download the model and exit")
    ap.add_argument("--ask", action="store_true", help="ask one question about a moment")
    ap.add_argument("--at", help="time for --ask, e.g. 1:23:40")
    ap.add_argument("--question")
    ap.add_argument("--force", action="store_true", help="run even if a game/OBS is open")
    args = ap.parse_args()

    ensure_not_gaming(args.force)
    import ollama

    with ollama_server() as host:
        client = ollama.Client(host=host)
        if args.pull:
            last = ""
            for p in client.pull(args.model, stream=True):
                status = p.get("status", "")
                if p.get("total"):
                    status += f" {p.get('completed', 0) / p['total']:.0%}"
                if status != last:
                    print(status, flush=True)
                    last = status
            return

        out = stream_dir(args.name)
        segs = load_json(out / "segments.json")
        if not segs:
            sys.exit(f"Run prepare.py first ({out / 'segments.json'} missing).")
        events = load_json(out / "sound_events.json")
        spoken = load_json(out / "transcript.json", {"segments": []})
        en = load_json(out / "transcript_en.json", {"segments": []})
        settings = stream_settings(out)
        guide = build_guide(games.get(args.game or settings.get("game")), spoken.get("language"))

        if args.ask:
            t = int(parse_ts(args.at, 0))
            times = [x for x in range(max(0, t - 6), t + 8, 2) if (out / "frames" / f"{x + 1:06d}.jpg").exists()]
            prompt = "\n".join([guide, "", "Frames: " + ", ".join(fmt_ts(x) for x in times),
                                "Sounds:", *sound_lines(events, times[0], times[-1]),
                                "Speech:", *speech_lines(spoken, en, times[0], times[-1]), "",
                                f"Question: {args.question}",
                                'Reply with JSON: {"answer": "...", "confidence": "low|medium|high"}'])
            print(json.dumps(ask_model(client, args.model, prompt,
                                       [str(out / "frames" / f"{x + 1:06d}.jpg") for x in times]), indent=2))
            return

        only = set(args.only.split(",")) if args.only else None
        todo = [s for s in segs if (only and s["id"] in only) or
                (not only and not (out / "watch_log" / f"{s['id']}.json").exists())]
        print(f"{len(segs) - len(todo)} segments already watched, {len(todo)} to go.")
        t0 = time.time()
        for i, seg in enumerate(todo, 1):
            s0 = time.time()
            windows = watch_segment(client, args.model, out, seg, events, spoken, en, guide)
            write_segment(out, seg, windows, args.model, time.time() - s0)
            n_ev = sum(len(w["events"]) for w in windows)
            eta = (time.time() - t0) / i * (len(todo) - i)
            print(f"  {seg['id']} done in {time.time() - s0:.0f}s, {n_ev} events. ETA {eta / 60:.0f} min", flush=True)
        try:
            client.generate(model=args.model, prompt="", keep_alive=0)  # unload from GPU
        except Exception:
            pass
        digest(out)


if __name__ == "__main__":
    main()
