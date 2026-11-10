"""Valorant kill feed (top right): who killed whom, from row colours alone.

Each feed row ends with the victim's name: red background = enemy died, teal = teammate died.
A lime outline/arrow marks the streamer's end of a row, so:
  red + lime on the left = you got a kill, teal + lime on the right = you died,
  red only = teammate got a kill, teal only = teammate died.
Measured on 1080p recordings with default HUD and enemy-highlight colours, scaled to 960x540 frames. Changed
colour settings (e.g. colour-blind modes) will need the thresholds in colour() adjusted.
"""
from PIL import Image

NAME = "valorant"
ALIASES = ("valo",)
TITLE = "Valorant: kills and deaths from the kill feed"
REF_SIZE = (960, 540)
OUTPUT = "kills.json"

# Feed rows grow downward from the top right, about 20 px apart.
ROW_Y = [56, 76, 95, 115, 134]
VICTIM_X = range(892, 915, 3)  # the victim's name; their portrait sits to the right of it
ARROW_X = range(945, 956, 2)
KILLER_X = range(700, 880)

WATCH_HINTS = """This is Valorant (5v5 tactical shooter, rounds). Useful things to look for on screen:
- The kill feed (top right): red rows = enemies dying, teal rows = teammates dying; the streamer's own rows are
  outlined. Multi-kills, aces, clutches (last player alive winning the round).
- Fights: aiming, abilities, hit markers, spike plant/defuse.
- The streamer dying (death recap, spectating a teammate).
- Round won/lost banners, match result screen.
- Quiet stuff: buy phase, menus, agent select, walking with nothing happening."""

# Enlarged kill feed under the footage in make_edit.py (fractions of a 1920x1080 reference).
OVERLAYS = {"feed": {"crop": (1360, 85, 560, 170), "scale": (1080, 328), "at": (0, 1500)}}

MY_KILL_TYPE = "you_kill"  # the event type autorun.py hands to refine_kills.py
KINDS = {("red", True): "you_kill", ("teal", True): "you_die", ("red", False): "team_kill", ("teal", False): "team_die"}


def colour(p):
    r, g, b = p
    if r > 170 and g < 115 and b < 125:
        return "red"
    if g > 150 and b > 110 and r < 150 and g > r + 40:
        return "teal"
    return None


def lime(p):
    r, g, b = p
    return r > 150 and g > 170 and b < 140


def read_rows(px):
    """(colour, is_yours) for each feed row in one frame."""
    rows = []
    for centre in ROW_Y:
        ys = range(centre - 5, centre + 6)
        votes = [colour(px[x, y]) for x in VICTIM_X for y in ys]
        red, teal = votes.count("red"), votes.count("teal")
        if max(red, teal) < 20:  # no row here, and none further down
            break
        victim = "red" if red >= teal else "teal"
        if victim == "red":
            # Your kill rows have a long outline. A skin's scope ring only crosses a row briefly.
            arrow = any(sum(lime(px[x, y]) for x in KILLER_X) >= 30 for y in range(centre - 10, centre + 11))
        else:
            arrow = any(lime(px[x, y]) for x in ARROW_X for y in ys)
        rows.append((victim, arrow))
    return rows


def my_kills(px):
    """How many of the streamer's kill rows are in the feed right now (refine_kills.py watches this rise)."""
    return sum(1 for r in read_rows(px) if r == ("red", True))


def scan(frames, start=0, until=None):
    events = []
    prev = {k: 0 for k in KINDS.values()}
    for t, path in enumerate(frames):  # frame t (0-based) shows second t
        if until is not None and t >= until:
            break
        if t < start:
            continue
        px = Image.open(path).convert("RGB").load()
        now = {k: 0 for k in KINDS.values()}
        for row in read_rows(px):
            now[KINDS[row]] += 1
        for kind, n in now.items():
            events += [{"t": t, "type": kind}] * max(0, n - prev[kind])
        prev = now
    return events


def report(events):
    from common import fmt_ts

    for kind in KINDS.values():
        print(kind, sum(1 for e in events if e["type"] == kind))
    mine = [e["t"] for e in events if e["type"] == "you_kill"]
    streak, runs = [], []
    for t in mine:
        if streak and t - streak[-1] > 12:
            runs.append(streak)
            streak = []
        streak.append(t)
    if streak:
        runs.append(streak)
    print("multi-kills (12 s apart at most):")
    for r in runs:
        if len(r) >= 2:
            print(f"  {fmt_ts(r[0])}  {len(r)} kills  {[fmt_ts(x) for x in r]}")
    print("your deaths:", [fmt_ts(e["t"]) for e in events if e["type"] == "you_die"])
