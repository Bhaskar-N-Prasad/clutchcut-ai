"""PUBG: Battlegrounds (PC) squad panel: a knocked player's health bar (bottom left) turns solid red.

Measured on 1080p PUBG PC recordings with the default HUD layout, scaled to the 960x540 frames prepare.py
writes. A different HUD scale, an ultrawide or non-16:9 recording, or another game will need the coordinates
below re-measured (docs/adding-a-game.md shows how).
"""
from PIL import Image

NAME = "pubg"
ALIASES = ("pubg_pc", "battlegrounds")
TITLE = "PUBG: knocks from the squad panel health bars (PUBG: Battlegrounds, PC)"
REF_SIZE = (960, 540)
OUTPUT = "knocks.json"

# The health-bar line of each of the 4 squad rows, and how far it runs.
ROW_Y = [467, 486, 505, 524]
BAR_X = (17, 128)
PANEL_BOX = (0, 450, 170, 535)  # the whole panel, for the knock contact sheet

WATCH_HINTS = """This is PUBG: Battlegrounds on PC (battle royale, squads of 4). Useful things to look for on screen:
- Kill/knock notifications ("You knocked out <name>", "You finished <name>", "<name> knocked out <name> with <gun>"),
  headshot markers, the streamer's kill counter.
- Fights: aiming down sights, muzzle flash, damage numbers, enemies visible, hit markers, throwables, vehicle chases.
- The streamer getting knocked or dying (screen goes grey/red, "You were knocked out", spectating a teammate,
  result screen with a rank like #12). A teammate's health bar turning red in the squad panel = they are knocked.
- "Winner Winner Chicken Dinner" screen, final circle, players-alive count getting low.
- Quiet stuff: lobby, menus, parachuting, looting, running, driving with nothing happening."""

# Regions make_edit.py can enlarge under the footage, as fractions of the source video (any resolution).
# crop = (x, y, w, h) on a 1920x1080 reference; scale/at are on the 1080x1920 vertical canvas.
OVERLAYS = {"panel": {"crop": (0, 864, 346, 216), "scale": (692, 432), "at": (194, 1490)}}


def row_red(px, y):
    """Longest count of strongly red pixels on the row's health-bar line."""
    best = 0
    for yy in (y - 1, y, y + 1):
        # The 2 px line fades at this frame size: red reads about (130, 15, 30), not pure red.
        n = sum(1 for x in range(*BAR_X) if px[x, yy][0] > 100 and px[x, yy][1] < 50 and px[x, yy][2] < 60)
        best = max(best, n)
    return best


def scan(frames, start=0, until=None):
    """A row counts as knocked once it's been red for 2 s (not a flicker). Rows are reshuffled every match,
    so the result says which row, not who: read the names off the contact sheet."""
    state = [0, 0, 0, 0]
    knocks = []
    for k, path in enumerate(frames):  # frame k (0-based) shows second k
        if until is not None and k >= until:
            break
        if k < start:
            continue
        px = Image.open(path).convert("RGB").load()
        for r, y in enumerate(ROW_Y):
            if row_red(px, y) >= 8:
                state[r] += 1
                if state[r] == 2:
                    knocks.append({"t": k - 1, "type": "knock", "row": r + 1})
            else:
                state[r] = 0
    return knocks


def after_scan(out, frames, events):
    """knocks.jpg: the panel at each knock, so names can be read."""
    from common import compose_sheet, fmt_ts

    crops = out / "knock_panels"
    crops.mkdir(exist_ok=True)
    items = []
    for ev in events:
        im = Image.open(frames[min(ev["t"] + 2, len(frames) - 1)]).crop(PANEL_BOX)
        p = crops / f"{ev['t']}.jpg"
        im.resize((340, 170)).save(p, quality=90)
        items.append((str(p), f"{fmt_ts(ev['t'])} r{ev['row']}"))
    if items:
        compose_sheet(items, out / "knocks.jpg", cols=7, tile_w=260)
        print(f"Panel sheet: {out / 'knocks.jpg'}")


def report(events):
    from common import fmt_ts

    print(f"{len(events)} knocks")
    for ev in events:
        print(" ", fmt_ts(ev["t"]), "row", ev["row"])
