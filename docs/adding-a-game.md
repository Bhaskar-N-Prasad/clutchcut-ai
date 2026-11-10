# Adding a game profile

A game profile teaches ClutchCut to read one game's HUD from the 1 fps frames `prepare.py` writes. It is the
cheapest, most reliable signal in the whole pipeline: the PUBG Mobile panel scan lists the knocks in a 2-hour
stream in seconds, where watching everything with a vision model took hours. Two profiles ship today:

| Profile | Reads | Output |
|---|---|---|
| `pubg_mobile` (aliases `bgmi`, `pubg`) | squad panel health bars turning red (bottom left) | `knocks.json` |
| `valorant` | kill-feed row colours and the streamer's outline (top right) | `kills.json` |

Both were measured on **one creator's 1080p recordings with default HUD settings**. If yours differ, expect to
re-measure coordinates.

## 1. Find a stable HUD signal
Good signals are **pixel-simple** and **always in the same place**: a bar that changes colour, a feed row that
appears, an icon that lights up, a counter box. Avoid anything that needs OCR or depends on the map.

Grab a few frames around known events (kills, deaths, round end) from `streams/<name>/frames/` (frame N shows
second N-1), open them in an image editor at 960x540 and note:
- the pixel rectangle where the signal appears,
- its colour when "on" (sample several frames: compression and HUD transparency shift colours),
- what it looks like when "off", and what else (skins, explosions, scopes) can look similar.

## 2. Write `games/<your_game>.py`

```python
"""<Game>: <what it reads>. Measured on <resolution, HUD settings>."""
from PIL import Image

NAME = "mygame"                 # used as --game mygame
ALIASES = ("mg",)               # optional
TITLE = "My Game: deaths from the red vignette"
REF_SIZE = (960, 540)           # frame size your coordinates are for (prepare.py's default for 16:9)
OUTPUT = "deaths.json"          # written to streams/<name>/

WATCH_HINTS = """This is My Game (...). Useful things to look for on screen: ..."""  # optional, for local_watch.py

# Optional: regions make_edit.py can enlarge under the footage ("hud": "feed" in a beat).
# crop = (x, y, w, h) on a 1920x1080 reference; scale and at are on the 1080x1920 vertical canvas.
OVERLAYS = {"feed": {"crop": (1400, 80, 500, 200), "scale": (1080, 432), "at": (0, 1480)}}


def scan(frames, start=0, until=None):
    """frames: sorted list of jpg paths, frame k shows second k. Return [{"t": seconds, "type": str, ...}]."""
    events, was_on = [], False
    for t, path in enumerate(frames):
        if t < start or (until is not None and t >= until):
            continue
        px = Image.open(path).convert("RGB").load()
        r, g, b = px[480, 20]
        on = r > 180 and g < 60 and b < 60
        if on and not was_on:          # count rising edges, not every frame it stays on
            events.append({"t": t, "type": "death"})
        was_on = on
    return events


def report(events):                    # optional: print a summary
    print(len(events), "deaths")

# Optional extras:
# def after_scan(out, frames, events): ...   # e.g. a contact sheet of each event for checking by eye
# def my_kills(px) -> int: ...                # the streamer's kill rows in one frame; enables refine_kills.py
```

Any module in `games/` not starting with `_` is picked up automatically; check with
`python scripts/hud_scan.py --list`.

## 3. Test it honestly
1. Run `python scripts/hud_scan.py --name <stream> --game mygame` on a stream where you know the answers.
2. Compare against a hand count for one match: false positives (explosions, skins, menus) and misses.
3. Add debounce (require the signal for 2 frames), thresholds or a second check pixel until both are low.
4. Write in the module docstring what you measured on (resolution, HUD scale, colour settings).

Pull requests with new profiles are welcome; include the docstring notes and, if you can, a few 960x540 test
frames from your own footage under `games/testdata/<game>/` (only footage you own).
