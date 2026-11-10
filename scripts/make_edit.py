"""Build one vertical edit (meme / compilation) from a list of beats.

Usage:
  python make_edit.py --name <stream-name> --edit edit_meme.json

streams/<name>/<edit>.json:
  {"out": "meme", "music": "music/track.mp3", "music_volume": 0.2, "music_start": 0, "source_volume": 1,
   "game": "pubg_mobile",                                    # optional; defaults to the stream's settings.json
   "title": ["LINE ONE", "LINE TWO"],
   "beats": [{"start": 799, "dur": 5, "mode": "square",      # square = crop around the crosshair (crop_x moves it), fit = whole screen
              "hud": "panel",                                 # enlarge a HUD region from the game profile's OVERLAYS
                                                              # under the footage ("panel": true / "feed": true also work)
              "freeze": 1.0,                                  # hold the last frame, with a thud
              "transition": {"type": "fadeblack", "dur": 0.4},  # how this beat comes in (default a 0.2 s fade)
              "zoom": [{"at": 4, "add": 0.5, "ramp": 0.2}], "cx": 540, "cy": 540,  # zoom steps, centre in the 1080 crop
              "text": [{"a": 0, "b": 2, "text": "Caption 😭", "y": 1320, "size": 76, "color": "white"}],
              "arrows": [{"a": 0, "b": 2, "x": 150, "y": 560}]}]}
Text times are relative to the beat. Text is drawn with PIL so emojis keep their colour (needs a colour emoji
font; set CLUTCHCUT_FONT_EMOJI if none is found). "music" paths are relative to the ClutchCut folder.
Output: streams/<name>/edits/<out>.mp4
"""
import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from common import (ROOT, load_font, load_json, probe_duration, run, stream_dir, stream_settings,  # noqa: E402
                    tools, video_encoder)

import games  # noqa: E402

W, H = 1080, 1920
EMOJI = re.compile("([\U0001F300-\U0001FAFF☀-➿]+️?)")
THUD = "sin(2*PI*(90-70*t)*t)*exp(-7*t)"
LEGACY_HUD = {"panel": "pubg_mobile", "feed": "valorant"}  # old beat keys -> the profile that defines them


def runs(text, size):
    """Split text into (string, font, is_emoji) pieces and measure them."""
    out = []
    for k, part in enumerate(EMOJI.split(text)):
        if part:
            is_emoji = k % 2 == 1
            font = load_font("emoji" if is_emoji else "text", size)
            out.append((part, font, is_emoji, font.getlength(part)))
    return out


def hud_overlay(spec, beat, settings):
    """ffmpeg filters that enlarge one HUD region from the game profile under the footage, or None."""
    region = beat.get("hud") or next((k for k in LEGACY_HUD if beat.get(k)), None)
    if not region:
        return None
    profile = games.get(spec.get("game") or settings.get("game") or LEGACY_HUD.get(region))
    box = getattr(profile, "OVERLAYS", {}).get(region)
    if not box:
        sys.exit(f"Game profile {profile.NAME} has no HUD overlay '{region}'.")
    x, y, w, h = box["crop"]  # on a 1920x1080 reference, so any source resolution works
    sw, sh = box["scale"]
    ax, ay = box["at"]
    return (f"[c]crop=iw*{w}/1920:ih*{h}/1080:iw*{x}/1920:ih*{y}/1080,scale={sw}:{sh}[pn]",
            f"overlay={ax}:{ay}")


def text_layer(item, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    size = item.get("size", 76)
    while sum(r[3] for r in runs(item["text"], size)) > W - 60 and size > 18:
        size -= 2
    pieces = runs(item["text"], size)
    x = (W - sum(p[3] for p in pieces)) / 2
    for part, font, is_emoji, width in pieces:
        if is_emoji:
            d.text((x, item["y"]), part, font=font, embedded_color=True)
        else:
            d.text((x, item["y"]), part, font=font, fill=item.get("color", "white"),
                   stroke_width=max(2, size // 24), stroke_fill="black")
        x += width
    im.save(path)


def arrow_layer(item, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x, y = item["x"], item["y"]
    ImageDraw.Draw(im).polygon([(x - 36, y - 50), (x + 36, y - 50), (x, y + 12)], fill="#FFD400", outline="black", width=5)
    im.save(path)


def transition(spec, n):
    """(type, seconds) for how beat n comes in; a top-level "transition_dur" is the default length."""
    tr = spec["beats"][n].get("transition", {})
    return tr.get("type", "fade"), tr.get("dur", spec.get("transition_dur", 0.2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--edit", required=True)
    args = ap.parse_args()

    ffmpeg, _, _ = tools()
    base = stream_dir(args.name)
    spec = load_json(base / args.edit)
    settings = stream_settings(base)
    work = base / "edits" / (spec["out"] + "_work")
    work.mkdir(parents=True, exist_ok=True)

    title_layers = []
    for k, line in enumerate(spec.get("title", [])):
        p = work / f"title{k}.png"
        text_layer({"text": line, "y": 170 + k * 90, "size": 66, "color": "#FFD400"}, p)
        title_layers.append(p)

    parts, hits = [], []
    for n, b in enumerate(spec["beats"]):
        freeze, length = b.get("freeze", 0), b["dur"]
        planned = length + freeze
        # Extra footage for the next beat to fade over, so the fade doesn't eat this beat's ending.
        tail = transition(spec, n + 1)[1] if n + 1 < len(spec["beats"]) else 0
        if freeze:
            freeze += tail
        else:
            length += tail
        total = length + freeze
        layers = [(p, 0, total) for p in title_layers]
        for k, item in enumerate(b.get("text", [])):
            p = work / f"b{n}_t{k}.png"
            text_layer(item, p)
            layers.append((p, item["a"], item["b"]))
        for k, item in enumerate(b.get("arrows", [])):
            p = work / f"b{n}_a{k}.png"
            arrow_layer(item, p)
            layers.append((p, item["a"], item["b"]))

        # Text that lasted to the end of the beat stays up through the fade.
        layers = [(p, a, total if e >= planned - 0.01 else e) for p, a, e in layers]

        hold = f",tpad=stop_mode=clone:stop_duration={freeze}" if freeze else ""
        if b.get("mode", "square") == "fit":
            frame, top = "scale=1080:-2", 656
        else:
            frame, top = f"crop=1080:1080:{b.get('crop_x', 420)}:0", 420
            if b.get("zoom"):
                # Upscale first: zoompan rounds to whole pixels and jitters on slow moves at output size.
                z = "1" + "".join(f"+{e['add']}*clip((in_time-{e['at']})/{e.get('ramp', 0.2)},0,1)" for e in b["zoom"])
                cx, cy = 2 * b.get("cx", 540), 2 * b.get("cy", 540)
                frame += (f",scale=2160:2160,zoompan=z='{z}':x='clip({cx}-iw/zoom/2,0,iw-iw/zoom)'"
                          f":y='clip({cy}-ih/zoom/2,0,ih-ih/zoom)':d=1:s=1080x1080:fps=30")
        speed = b.get("speed", 1)  # below 1 = slow motion; dur and text times are in output seconds
        graph = [f"[0:v]setpts=PTS/{speed},fps=30{hold},split=3[a][b][c]",
                 f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2,eq=brightness=-0.25[bg]",
                 f"[b]{frame}[fg]", f"[bg][fg]overlay=0:{top}[v0]"]
        last = "v0"
        hud = hud_overlay(spec, b, settings)
        if hud:
            graph += [hud[0], f"[{last}][pn]{hud[1]}[v1]"]
            last = "v1"
        else:
            graph.append("[c]nullsink")
        cmd = [ffmpeg, "-y", "-v", "error", "-ss", str(b["start"]), "-t", str(length * speed), "-i", str(base / "vod.mp4")]
        for k, (p, a, e) in enumerate(layers, start=1):
            cmd += ["-i", str(p)]
            graph.append(f"[{last}][{k}:v]overlay=0:0:enable='between(t,{a},{e})'[o{k}]")
            last = f"o{k}"
        graph.append(f"[{last}]format=yuv420p[vout]")
        slow = f"atempo={speed}," if speed != 1 else ""
        graph.append(f"[0:a]{slow}aresample=48000,apad=whole_dur={total}[aout]")
        part = work / f"part{n:02d}.mp4"
        run(cmd + ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", str(total),
                   "-r", "30", *video_encoder(20), "-c:a", "aac", "-b:a", "192k", str(part)])
        parts.append(part)
        if freeze or b.get("thud"):
            hits.append((n, b.get("thud_at", b["dur"] if freeze else 0)))
        print(f"beat {n + 1}/{len(spec['beats'])} done", flush=True)

    # Join with transitions: each beat fades in over the end of the one before it.
    starts, now, joins, inputs = [0.0], probe_duration(parts[0]), [], ["-i", str(parts[0])]
    vlast, alast = "0:v", "0:a"
    for n, part in enumerate(parts[1:], start=1):
        kind, dur = transition(spec, n)
        start = now - dur
        starts.append(start)
        inputs += ["-i", str(part)]
        joins.append(f"[{vlast}][{n}:v]xfade=transition={kind}:duration={dur}:offset={start:.3f}[v{n}]")
        joins.append(f"[{alast}][{n}:a]acrossfade=d={dur}[a{n}]")
        vlast, alast = f"v{n}", f"a{n}"
        now = start + probe_duration(part)
    joined = work / "joined.mp4"
    # xfade outputs 4:4:4, which many phones and players can't play.
    joins.append(f"[{vlast}]format=yuv420p,setsar=1[vjoined]")
    run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(joins), "-map", "[vjoined]",
         "-map", f"[{alast}]", "-r", "30", *video_encoder(20), "-c:a", "aac", "-b:a", "192k", str(joined)])
    thuds = [starts[n] + at for n, at in hits]

    # Stream audio + quiet music + synthesised thuds (generated here, so nothing to license).
    inputs = ["-i", str(joined), "-stream_loop", "-1", "-ss", str(spec.get("music_start", 0)), "-i", str(ROOT / spec["music"])]
    fade = spec.get("music_fade", 0)  # seconds; lets a track end softly instead of being cut
    fade_out = f",afade=t=out:st={now - fade:.3f}:d={fade}" if fade else ""
    mix = [f"[1:a]volume={spec.get('music_volume', 0.2)}{fade_out}[m]", f"[0:a]volume={spec.get('source_volume', 1)}[s]"]
    labels = "[s][m]"
    for k, t in enumerate(thuds):
        inputs += ["-f", "lavfi", "-t", "0.6", "-i", f"aevalsrc='{THUD}':s=48000"]
        mix.append(f"[{k + 2}:a]adelay={int(t * 1000)}:all=1,volume=1.6[t{k}]")
        labels += f"[t{k}]"
    count = 2 + len(thuds)
    mix.append(f"{labels}amix=inputs={count}:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5[out]")
    final = base / "edits" / f"{spec['out']}.mp4"
    run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(mix), "-map", "0:v", "-map", "[out]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", str(now), str(final)])
    print(f"Done: {final} ({now:.1f}s)")


if __name__ == "__main__":
    main()
