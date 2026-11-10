"""Game profiles: cheap, GPU-free scanners that read a game's HUD from the 1 fps frames.

A profile is a module in this folder. See docs/adding-a-game.md for the contract; in short it defines:

  NAME          short id used on the command line (--game NAME)
  ALIASES       other names that select it (optional)
  TITLE         one line for humans
  REF_SIZE      (width, height) of the frames its pixel coordinates were measured on
  OUTPUT        file written to streams/<name>/ by hud_scan.py, e.g. "knocks.json"
  WATCH_HINTS   what the local vision model should look for in this game (text, optional)
  OVERLAYS      HUD regions make_edit.py can enlarge under the footage (optional)
  scan(frames, start, until) -> list of {"t": seconds, "type": str, ...}
  report(events)                    print a summary (optional)
  after_scan(out, frames, events)   extra outputs, e.g. a contact sheet (optional)
  my_kills(px) -> int               streamer's kill rows visible in one frame; enables refine_kills.py (optional)
  MY_KILL_TYPE                      event type of the streamer's own kills in scan() output (with my_kills)
"""
import importlib
import pkgutil
from pathlib import Path

GENERIC_HINTS = """Useful things to look for on screen:
- Kill / knock / elimination notifications, headshot markers, the streamer's kill counter.
- Fights: aiming down sights, muzzle flash, damage numbers, enemies visible, hit markers, abilities, explosions.
- The streamer dying (screen goes grey/red, spectating a teammate, a death recap or results screen).
- Wins and round/match end screens.
- Quiet stuff: lobby, menus, loading, buying, looting, running around with nothing happening."""


def _modules():
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not info.name.startswith("_"):
            yield importlib.import_module(f"{__name__}.{info.name}")


def available():
    return {m.NAME: m for m in _modules()}


def get(name):
    """The profile for `name` (or one of its aliases); None for no/unknown game."""
    if not name:
        return None
    key = name.lower()
    for m in _modules():
        if key == m.NAME or key in getattr(m, "ALIASES", ()):
            return m
    raise SystemExit(f"Unknown game profile '{name}'. Available: {', '.join(sorted(available()))}. "
                     f"See docs/adding-a-game.md to add one.")


def guess_from_title(title):
    """Profile names whose NAME, aliases or game name appear in a stream title, e.g. 'Valorant ranked' -> ['valorant']."""
    import re
    words = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    found = []
    for name, m in sorted(available().items()):
        keys = {name, *getattr(m, "ALIASES", ())}
        keys |= set(re.findall(r"[a-z0-9]+", m.TITLE.split(":")[0].lower())) - {"mobile", "pc", "the"}
        if words & keys:
            found.append(name)
    return found


def watch_hints(profile):
    return getattr(profile, "WATCH_HINTS", None) or GENERIC_HINTS
