"""Stage 3: close-up look at shortlisted moments, for Claude to set exact cut points.

Usage:
  python zoom.py --name <stream-name> --at 1:02:10,1:15:40 [--before 25] [--after 15] [--fps 2]

For each moment writes streams/<name>/zoom/<HhMMmSS>/:
  sheet_01.jpg ...   4x4 grids of frames at --fps with exact times burned in
  notes.md           transcript with word times, rough English, sounds, and the local model's notes
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import compose_sheet, fmt_ts, load_json, parse_ts, run, stream_dir, tools  # noqa: E402
from local_watch import sound_lines  # noqa: E402


def zoom_one(out, t, before, after, fps):
    ffmpeg, _, _ = tools()
    start = max(0, t - before)
    dur = before + after
    zid = fmt_ts(t).replace(":", "m", 1).replace(":", "s") if t >= 3600 else fmt_ts(t).replace(":", "m")
    zdir = out / "zoom" / zid
    (zdir / "frames").mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", "vod.mp4", "-an",
         "-vf", f"fps={fps},scale=640:-2", "-q:v", "3", str(zdir / "frames" / "%04d.jpg")], cwd=out)
    frames = sorted((zdir / "frames").glob("*.jpg"))
    items = [(p, f"{fmt_ts(start + k / fps)}.{int((k % fps) * 10 / fps)}") for k, p in enumerate(frames)]
    sheets = []
    for i in range(0, len(items), 16):
        target = zdir / f"sheet_{i // 16 + 1:02d}.jpg"
        compose_sheet(items[i:i + 16], target, cols=4, tile_w=384)
        sheets.append(target.name)

    a, b = int(start), int(start + dur)
    spoken = load_json(out / "transcript.json", {"segments": []})
    en = load_json(out / "transcript_en.json", {"segments": []})
    events = load_json(out / "sound_events.json")
    notes = [f"# Moment {fmt_ts(t)} ({t}s), window {fmt_ts(a)}-{fmt_ts(b)}",
             f"Sheets ({fps} fps, 16 frames = {16 / fps:.0f}s each): {', '.join(sheets)}", "",
             f"## Speech ({spoken.get('language') or 'unknown language'}, word times)"]
    for s in spoken["segments"]:
        if s["end"] >= a and s["start"] <= b:
            words = " ".join(f"{w['w']}@{w['s']:.1f}" for w in s.get("words", [])) or s["text"]
            notes.append(f"- [{fmt_ts(s['start'])}] {s['text']}\n  - words: {words}")
    notes += ["", "## Rough English (Whisper)"]
    notes += [f"- [{fmt_ts(s['start'])}-{fmt_ts(s['end'])}] {s['text']}" for s in en["segments"]
              if s["end"] >= a and s["start"] <= b] or ["- (none)"]
    notes += ["", "## Sounds", *sound_lines(events, a, b), "", "## Local model notes"]
    for seg in load_json(out / "segments.json", []):
        data = load_json(out / "watch_log" / f"{seg['id']}.json")
        for w in (data or {}).get("windows", []):
            if w["end"] >= a and w["start"] <= b:
                notes.append(f"- {fmt_ts(w['start'])}-{fmt_ts(w['end'])}: {w['summary']}")
                notes += [f"  - {fmt_ts(e['t'])} [{e['type']} {e['score']:.0f}] {e['what']}" for e in w["events"]]
    (zdir / "notes.md").write_text("\n".join(notes), encoding="utf-8")
    print(f"{fmt_ts(t)}: {zdir}  ({len(sheets)} sheets)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--at", required=True, help="comma-separated times, e.g. 1:02:10,1:15:40")
    ap.add_argument("--before", type=float, default=25)
    ap.add_argument("--after", type=float, default=15)
    ap.add_argument("--fps", type=float, default=2)
    args = ap.parse_args()
    out = stream_dir(args.name)
    for s in args.at.split(","):
        zoom_one(out, int(parse_ts(s, 0)), args.before, args.after, args.fps)


if __name__ == "__main__":
    main()
