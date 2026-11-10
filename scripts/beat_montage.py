"""Kill montage: full speed into each kill, then a snap zoom and slow motion that start on the kill, on a drum beat.

Usage:
  python beat_montage.py --spec spec.json --out video.mp4

spec.json: {"music": "track.mp3", "drop": 49.8, "lead": 3.7, "seconds": 15, "beats_per_kill": 2, "music_volume": 0.7,
            "source_volume": 0.9, "shots": [{"video": "a.mp4", "hit": 123.4}]}
"hit" comes from refine_kills.py. The first kill lands on the drop, "lead" seconds into the video.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import librosa
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, load_json, run, tools, video_encoder  # noqa: E402

W, H, FPS = 1920, 1080, 60
XD = 0.07  # a very short dissolve; the impact effects hide the join
FINISH = 0.5  # seconds the final shot keeps playing after its slow motion
EDGE = 0.08  # seconds of motion blur at the start of a shot
SW, SH = 2016, 1134  # slightly oversized frame, so the screen shake never shows an edge
BOOM = "0.9*sin(2*PI*(95-60*t)*t)*exp(-9*t)"  # a low hit under each kill, generated here so there's nothing to license
SLOW_RATE = 0.45  # speed just after each kill
SLOW_OUT = 0.55  # seconds of slow motion in the video, starting on the kill
ENEMY_ZOOM = 0.45  # how far it snaps toward the crosshair on the kill
GRADE = "eq=saturation=1.2:contrast=1.05"


def last_gunshot(ffmpeg, video, hit):
    """The strongest transient just before the kill feed updates."""
    a = max(0, hit - 0.6)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(a), "-t", "0.65", "-i", str(video), "-ac", "1", "-ar", "22050",
                        str(wav)], check=True)
        y, sr = librosa.load(str(wav), sr=None)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=128)
    # A spray has many shots: take the last one that is nearly as strong as the loudest.
    return a + float(np.nonzero(env >= 0.6 * env.max())[0][-1]) * 128 / sr


def render(ffmpeg, video, gun, pre, length, out, power=1.0):
    """Full speed up to the kill, then on the kill: snap zoom to the crosshair, slow motion, shake, flash, colour split.

    Audio stays at real speed, so the real gunshot lands on the beat with the zoom.
    """
    slow_src = SLOW_OUT * SLOW_RATE
    t1, t2 = pre + slow_src, pre + slow_src + max(0.0, length - pre - SLOW_OUT)
    tail = length > pre + SLOW_OUT + 0.01
    since = f"(in_time-{pre:.3f})"
    zoom = f"1+gte(in_time,{pre:.3f})*({ENEMY_ZOOM}*(1-exp(-{since}/0.035))+0.1*{since})"
    pad_x, pad_y = (SW - W) // 2, (SH - H) // 2
    amp_x, amp_y = int(min(pad_x, 34 * power)), int(min(pad_y, 22 * power))
    hit = f"gte(t,{pre:.3f})*exp(-(t-{pre:.3f})/0.11)"
    g = [f"[0:v]scale={W}:{H},format=yuv420p,split={3 if tail else 2}[raw]{'[rt]' if tail else ''}[rs]",
         f"[raw]fps={FPS},split=2[a][b]",
         f"[a]trim=0:{EDGE},setpts=PTS-STARTPTS,tmix=frames=5[a1]",
         f"[b]trim={EDGE}:{pre:.3f},setpts=PTS-STARTPTS[b1]",
         # Motion interpolation invents the in-between frames, so slow motion stays smooth on 30 fps clips too.
         f"[rs]trim={pre:.3f}:{t1:.3f},setpts=(PTS-STARTPTS)/{SLOW_RATE},minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:vsbmc=1,"
         f"split=2[s0][s9]",
         f"[s0]trim=0:0.12,setpts=PTS-STARTPTS,rgbashift=rh={-int(9 * power)}:bh={int(9 * power)}[s1]",
         "[s9]trim=0.12,setpts=PTS-STARTPTS[s2]"]
    if tail:
        g.append(f"[rt]fps={FPS},trim={t1:.3f}:{t2:.3f},setpts=PTS-STARTPTS[t1]")
    g += [f"[a1][b1][s1][s2]{'[t1]' if tail else ''}concat=n={5 if tail else 4}:v=1:a=0,fps={FPS},"
          f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={SW}x{SH}:fps={FPS},"
          f"crop={W}:{H}:x='{pad_x}+{amp_x}*sin(t*85)*{hit}':y='{pad_y}+{amp_y}*cos(t*100)*{hit}',"
          f"{GRADE},eq=brightness='{0.2 * power}*gte(t,{pre:.3f})*exp(-(t-{pre:.3f})/0.06)':eval=frame,format=yuv420p,setsar=1,"
          # Interpolation can lose a few frames; a short shot would break the join.
          "tpad=stop_mode=clone:stop_duration=0.3[v]",
          f"[0:a]aresample=48000,apad=whole_dur={length:.3f}[au]"]
    run([ffmpeg, "-y", "-v", "error", "-ss", f"{gun - pre:.3f}", "-t", f"{length + 0.1:.3f}", "-i", str(video),
         "-filter_complex", ";".join(g), "-map", "[v]", "-map", "[au]", "-t", f"{length:.3f}", "-r", str(FPS),
         *video_encoder(19), "-c:a", "aac", "-b:a", "192k", str(out)])


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
    lead = spec.get("lead", 0)
    drop = float(grid[np.argmin(np.abs(grid - spec["drop"]))])
    music_start = drop - lead
    beats = grid[grid >= music_start] - music_start  # beat times on the video's clock
    limit = spec.get("seconds", 999)

    # Every kill is on a beat: the zoom and slow motion start there, then the next shot takes over.
    every = spec.get("beats_per_kill", 2)
    index = int(np.argmin(np.abs(beats - lead))) if lead else every
    parts, offsets, kills, end = [], [], [], 0.0
    for n, shot in enumerate(spec["shots"]):
        if n:
            index += shot.get("beats", every)
        if index >= len(beats) or end >= limit:
            break
        # "kill" is the moment checked by eye; the kill feed and gunshot can run up to half a second late.
        gun = shot["kill"] if "kill" in shot else last_gunshot(ffmpeg, shot["video"], shot["hit"])
        target = float(beats[index])
        is_last = n == len(spec["shots"]) - 1 or index + every >= len(beats) or float(beats[index + every]) > limit
        offset = max(0.0, end - XD)
        pre = target - offset
        length = pre + SLOW_OUT + (FINISH if is_last else 0)
        path = work / f"shot{n:02d}.mp4"
        render(ffmpeg, shot["video"], gun, pre, length, path, 1.5 if n == 0 else 1.0)  # the drop hits hardest
        parts.append(path)
        offsets.append(offset)
        kills.append(target)
        end = offset + length
        print(f"{n:2d} gunshot {gun:8.2f} before kill {pre:.2f} len {length:.2f} kill at video {target:6.2f}s", flush=True)
        if is_last:
            break

    inputs, joins = ["-i", str(parts[0])], []
    vlast, alast = "0:v", "0:a"
    for n, path in enumerate(parts[1:], start=1):
        inputs += ["-i", str(path)]
        joins.append(f"[{vlast}][{n}:v]xfade=transition=dissolve:duration={XD}:offset={offsets[n]:.3f}[v{n}]")
        vlast = f"v{n}"
    # Each shot's audio goes at its exact time: chained crossfades drifted against the picture.
    tracks = "".join(f"[d{n}]" for n in range(len(parts)))
    joins += [f"[{n}:a]afade=t=in:d=0.02,adelay={int(offsets[n] * 1000)}:all=1[d{n}]" for n in range(len(parts))]
    joins.append(f"{tracks}amix=inputs={len(parts)}:duration=longest:normalize=0[amixed]")
    alast = "amixed"
    total = end
    joins.append(f"[{vlast}]fade=t=out:st={total - 0.5:.3f}:d=0.5,format=yuv420p,setsar=1[vj]")
    joined = work / "joined.mp4"
    run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(joins), "-map", "[vj]", "-map", f"[{alast}]",
         "-t", f"{total:.3f}", "-r", str(FPS), *video_encoder(19), "-c:a", "aac", "-b:a", "192k",
         str(joined)])
    mix = (f"[1:a]volume={spec.get('music_volume', 0.7)},afade=t=out:st={total - 0.8:.3f}:d=0.8[m];"
           f"[0:a]volume={spec.get('source_volume', 0.9)}[s];"
           )
    booms = []
    labels = "[s][m]"
    for k, t in enumerate(kills):
        booms += ["-f", "lavfi", "-t", "0.5", "-i", f"aevalsrc='{BOOM}':s=48000"]
        mix += f"[{k + 2}:a]adelay={int(t * 1000)}:all=1,volume={spec.get('boom_volume', 0.8)}[b{k}];"
        labels += f"[b{k}]"
    mix += f"{labels}amix=inputs={2 + len(kills)}:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5[o]"
    run([ffmpeg, "-y", "-v", "error", "-i", str(joined), "-ss", f"{music_start:.3f}", "-i", str(music), *booms,
         "-filter_complex", mix, "-map", "0:v", "-map", "[o]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-t", f"{total:.3f}", str(out)])
    print(f"Done: {out} ({total:.1f}s), music from {music_start:.2f}s, drop at {lead:.2f}s")


if __name__ == "__main__":
    main()
