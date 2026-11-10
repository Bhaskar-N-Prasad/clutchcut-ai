"""Find each kill's exact moment (to 0.1 s) by watching the kill feed at 10 fps around a rough time.

Usage:
  python refine_kills.py --list kills.json --out hits.json [--game valorant] [--sheet hits]

--list is [{"video": "path/to/video.mp4", "t": seconds}, ...]. The result adds "hit" (when your new feed row
appears, or null if none was seen). --sheet also saves <name>_N.jpg contact sheets of each hit.
Needs a game profile with my_kills() (currently: valorant). See docs/adding-a-game.md.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from common import compose_sheet, fmt_ts, load_json, save_json, tools  # noqa: E402

import games  # noqa: E402

FPS = 10


def extract(ffmpeg, video, start, dur, folder, size):
    w, h = size
    subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", str(video), "-an",
                    "-vf", f"fps={FPS},scale={w}:{h}", "-q:v", "4", str(Path(folder) / "%04d.jpg")], check=True)
    return sorted(Path(folder).glob("*.jpg"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--game", default="valorant")
    ap.add_argument("--sheet")
    args = ap.parse_args()

    profile = games.get(args.game)
    if not hasattr(profile, "my_kills"):
        sys.exit(f"The {profile.NAME} profile has no my_kills(); refine_kills.py can't use it.")
    ffmpeg, _, _ = tools()
    items, tiles, seen = [], [], set()
    with tempfile.TemporaryDirectory() as tmp:
        for k, c in enumerate(load_json(args.list)):
            start = max(0, c["t"] - 5)
            folder = Path(tmp) / str(k)
            folder.mkdir()
            frames = extract(ffmpeg, c["video"], start, 7, folder, profile.REF_SIZE)
            prev, rises = None, []
            for n, path in enumerate(frames):
                mine = profile.my_kills(Image.open(path).convert("RGB").load())
                if prev is not None and mine > prev:
                    rises.append(n)
                prev = mine
            near = [start + n / FPS for n in rises]
            hit = min(near, key=lambda x: abs(x - c["t"])) if near else None
            key = (c["video"], round(hit, 1)) if hit is not None else None
            if key is not None and key in seen:
                continue
            seen.add(key)
            items.append(dict(c, hit=round(hit, 1) if hit is not None else None))
            at = (hit if hit is not None else c["t"])
            n = min(len(frames) - 1, max(0, int((at - 0.2 - start) * FPS)))
            keep = Path(args.out).resolve().parent / f"_tile_{k}.jpg"
            Image.open(frames[n]).save(keep, quality=85)
            tiles.append((str(keep), f"{len(items) - 1} {Path(c['video']).stem[:10]} {fmt_ts(at)}"
                                     f"{'' if hit is not None else '?'}"))
            print(len(items) - 1, Path(c["video"]).name, c["t"], "->", hit, flush=True)
    save_json(args.out, items)
    if args.sheet:
        for s in range(0, len(tiles), 42):
            compose_sheet(tiles[s:s + 42], f"{args.sheet}_{s // 42}.jpg", cols=7, tile_w=270)
    for path, _ in tiles:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
