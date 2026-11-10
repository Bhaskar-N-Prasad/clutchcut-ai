"""Find moments by reading the game's HUD from the 1 fps frames: no GPU, seconds to a minute for 2 hours.

Usage:
  python hud_scan.py --name <stream-name> [--game pubg|valorant] [--from 18:00] [--until 1:50:00]
  python hud_scan.py --list                       # show the available game profiles

The game defaults to the one given to prepare.py (--game). Writes streams/<name>/<profile OUTPUT>
(e.g. knocks.json / kills.json) as [{"t": seconds, "type": ...}] and prints a summary.
Add a game: docs/adding-a-game.md.
"""
import argparse
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from common import parse_ts, save_json, stream_dir, stream_settings  # noqa: E402

import games  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="stream name (folder under streams/)")
    ap.add_argument("--game", help="game profile; defaults to the stream's settings.json")
    ap.add_argument("--from", dest="start", default="0", help="ignore everything before this, e.g. a warm-up")
    ap.add_argument("--until", default=None, help="stop here, e.g. to skip a deathmatch at the end")
    ap.add_argument("--list", action="store_true", help="list game profiles and exit")
    args = ap.parse_args()

    if args.list:
        for name, m in sorted(games.available().items()):
            print(f"{name:14s} {m.TITLE}  (aliases: {', '.join(getattr(m, 'ALIASES', ())) or '-'})")
        return

    out = stream_dir(args.name)
    profile = games.get(args.game or stream_settings(out).get("game"))
    if profile is None:
        sys.exit("No game profile set. Pass --game (see --list), or skip HUD scanning for this stream.")
    frames = sorted((out / "frames").glob("*.jpg"))
    if not frames:
        sys.exit(f"No frames in {out / 'frames'}. Run prepare.py first.")
    size = Image.open(frames[0]).size
    if tuple(size) != tuple(profile.REF_SIZE):
        print(f"Warning: frames are {size[0]}x{size[1]} but {profile.NAME} was measured on "
              f"{profile.REF_SIZE[0]}x{profile.REF_SIZE[1]}; results may be wrong.")

    t0 = time.time()
    start = parse_ts(args.start, 0)
    until = parse_ts(args.until, None) if args.until else None
    events = profile.scan(frames, start, until)
    save_json(out / profile.OUTPUT, events)
    print(f"{profile.TITLE}: {len(events)} events in {time.time() - t0:.0f}s -> {out / profile.OUTPUT}")
    if hasattr(profile, "after_scan"):
        profile.after_scan(out, frames, events)
    if hasattr(profile, "report"):
        profile.report(events)


if __name__ == "__main__":
    main()
