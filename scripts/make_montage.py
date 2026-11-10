"""Build a horizontal, beat-synced kill montage: speed ramp into each kill, slow motion on the hit, fast transitions.

Usage:
  python make_montage.py --spec montage.json --out video.mp4

montage.json:
  {"music": "music/track.mp3", "music_volume": 0.8, "source_volume": 0.45, "music_at": 60,
   "shots": [{"video": "a.mp4", "hit": 123.4, "kind": "long|short|intro|outro", "text": ["TITLE"]}]}
"hit" is the kill's moment in the source, from refine_kills.py. Each hit lands on a beat of the music.
music_at: start looking for a downbeat from here in the track.
"""
import argparse
import sys
from pathlib import Path

import librosa
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, load_font, load_json, run, tools, video_encoder  # noqa: E402

W, H, FPS = 1920, 1080, 60
XD = 0.18  # transition length
TRANSITIONS = ["zoomin", "hblur", "smoothleft", "fadewhite", "radial", "smoothright"]
# kind: (beats in the shot, beats until the hit, ramp speed before the hit, slow seconds before/after the hit)
KINDS = {"long": (6, 4, 1.5, 0.4, 0.2), "short": (4, 2, 1.5, 0.2, 0.1), "intro": (12, 10, 1.0, 0.4, 0.2)}
SLOW = 0.5
GRADE = "eq=saturation=1.35:contrast=1.08,unsharp=5:5:0.6"


def text_png(lines, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for line in lines:
        font = load_font("text", line["size"])
        x = (W - font.getlength(line["text"])) / 2
        d.text((x, line["y"]), line["text"], font=font, fill="white", stroke_width=max(2, line["size"] // 30),
               stroke_fill="black")
    im.save(path)


def render_shot(ffmpeg, shot, beat, n, work):
    """Returns (path, seconds to the next shot's start)."""
    if shot["kind"] == "outro":
        beats, to_hit, ramp, before, after = 7, 6, 1.0, 0.4, 0.2
    else:
        beats, to_hit, ramp, before, after = KINDS[shot["kind"]]
    length, hit_at = beats * beat, to_hit * beat
    a_out = hit_at - before / SLOW
    a_src = a_out * ramp
    b_src = before + after
    b_out = b_src / SLOW
    c_out = max(0.1, length + XD - a_out - b_out)
    freeze = shot.get("freeze", 0)
    start = shot["hit"] - before - a_src
    total_src = a_src + b_src + c_out
    blur = ",tmix=frames=3" if ramp > 1.2 else ""
    flash = f"eq=brightness='0.14*between(t,{hit_at:.3f},{hit_at + 0.07:.3f})':eval=frame"
    pop = "zoompan=z='1+0.1*max(0,1-in_time/0.3)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=60"
    hold = f",tpad=stop_mode=clone:stop_duration={freeze}" if freeze else ""
    tempo = f"atempo={ramp}" if ramp != 1 else "anull"
    g = [f"[0:v]fps={FPS},scale={W}:{H},format=yuv420p,split=3[a][b][c]",
         f"[a]trim=0:{a_src:.3f},setpts=(PTS-STARTPTS)/{ramp}{blur}[a1]",
         f"[b]trim={a_src:.3f}:{a_src + b_src:.3f},setpts=(PTS-STARTPTS)/{SLOW}[b1]",
         f"[c]trim={a_src + b_src:.3f}:{total_src:.3f},setpts=PTS-STARTPTS[c1]",
         "[0:a]aresample=48000,asplit=3[x][y][z]",
         f"[x]atrim=0:{a_src:.3f},asetpts=PTS-STARTPTS,{tempo}[x1]",
         f"[y]atrim={a_src:.3f}:{a_src + b_src:.3f},asetpts=PTS-STARTPTS,atempo={SLOW}[y1]",
         f"[z]atrim={a_src + b_src:.3f}:{total_src:.3f},asetpts=PTS-STARTPTS[z1]",
         "[a1][x1][b1][y1][c1][z1]concat=n=3:v=1:a=1[v0][a0]",
         # zoompan turns each input frame into one output frame, so retime first or slow motion gets lost
         f"[v0]fps={FPS},{pop},{GRADE},{flash}{hold}[v1]"]
    last, inputs = "v1", ["-ss", f"{start:.3f}", "-t", f"{total_src + 0.2:.3f}", "-i", str(shot["video"])]
    if shot.get("text"):
        png = work / f"text{n}.png"
        text_png(shot["text"], png)
        inputs += ["-i", str(png)]
        a, b = shot.get("text_at", (0, 99))
        g.append(f"[{last}][1:v]overlay=0:0:enable='between(t,{a},{b})'[v2]")
        last = "v2"
    total = a_out + b_out + c_out + freeze
    g.append(f"[{last}]format=yuv420p,setsar=1[vout]")
    g.append(f"[a0]apad=whole_dur={total:.3f}[aout]")
    out = work / f"shot{n:02d}.mp4"
    run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]",
         "-t", f"{total:.3f}", "-r", str(FPS), *video_encoder(19),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)])
    return out, length + freeze


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ffmpeg, _, _ = tools()
    spec = load_json(args.spec)
    out = Path(args.out)
    work = out.parent / (out.stem + "_work")
    work.mkdir(parents=True, exist_ok=True)

    music = ROOT / spec["music"]
    y, sr = librosa.load(str(music), sr=22050, mono=True)
    _, grid = librosa.beat.beat_track(y=y, sr=sr, units="time")
    beat = float(np.median(np.diff(grid)))
    while beat < 0.33:
        beat *= 2
    while beat > 0.55:
        beat /= 2
    music_start = float(grid[np.argmin(np.abs(grid - spec.get("music_at", 0)))])
    print(f"beat {beat:.3f}s ({60 / beat:.0f} BPM), track {len(y) / sr:.0f}s, music from {music_start:.1f}s", flush=True)

    parts, cum = [], []
    for n, shot in enumerate(spec["shots"]):
        path, step = render_shot(ffmpeg, shot, beat, n, work)
        parts.append(path)
        cum.append(step)
        print(f"shot {n + 1}/{len(spec['shots'])}", flush=True)

    inputs, joins = ["-i", str(parts[0])], []
    vlast, alast, offset = "0:v", "0:a", 0.0
    for n, path in enumerate(parts[1:], start=1):
        offset += cum[n - 1]
        inputs += ["-i", str(path)]
        kind = TRANSITIONS[(n - 1) % len(TRANSITIONS)]
        joins.append(f"[{vlast}][{n}:v]xfade=transition={kind}:duration={XD}:offset={offset:.3f}[v{n}]")
        joins.append(f"[{alast}][{n}:a]acrossfade=d={XD}[a{n}]")
        vlast, alast = f"v{n}", f"a{n}"
    total = offset + cum[-1]
    joins.append(f"[{vlast}]fade=t=out:st={total - 1:.3f}:d=1,format=yuv420p,setsar=1[vj]")
    joined = work / "joined.mp4"
    run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(joins), "-map", "[vj]", "-map", f"[{alast}]",
         "-t", f"{total:.3f}", "-r", str(FPS), *video_encoder(19), "-c:a", "aac",
         "-b:a", "192k", str(joined)])

    mix = (f"[1:a]volume={spec.get('music_volume', 0.8)},afade=t=out:st={total - 2:.3f}:d=2[m];"
           f"[0:a]volume={spec.get('source_volume', 0.45)}[s];"
           f"[s][m]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5[out]")
    run([ffmpeg, "-y", "-v", "error", "-i", str(joined), "-stream_loop", "-1", "-ss", f"{music_start:.3f}", "-i", str(music),
         "-filter_complex", mix, "-map", "0:v", "-map", "[out]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", "-t", f"{total:.3f}", str(out)])
    print(f"Done: {out} ({total:.1f}s)")


if __name__ == "__main__":
    main()
