"""Print when people actually speak in a stretch of a stream, for timing captions.

Usage:
  python speech_times.py --name <stream-name> --at 1:02:07-1:02:20 1:27:40-1:27:45

Reads voice16.wav from prepare.py and lists speech bursts (start-end, in VOD seconds), louder than the
stretch's own background, plus any transcript words that fall inside them.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
from common import fmt_ts, load_json, parse_ts, stream_dir  # noqa: E402

STEP = 0.05  # seconds per loudness reading
MIN_GAP = 0.25  # pauses shorter than this stay inside one burst
MIN_LEN = 0.15


def bursts(wav, sr, a, b):
    data, _ = sf.read(str(wav), start=int(a * sr), stop=int(b * sr), dtype="float32")
    hop = int(STEP * sr)
    rms = np.array([np.sqrt(np.mean(data[i:i + hop] ** 2)) + 1e-6 for i in range(0, len(data) - hop, hop)])
    db = 20 * np.log10(rms)
    floor = np.percentile(db, 20)
    on = db > max(floor + 12, -45)
    found, start, last = [], None, None
    for k, v in enumerate(on):
        t = a + k * STEP
        if v:
            if start is None:
                start = t
            elif t - last > MIN_GAP:
                found.append((start, last))
                start = t
            last = t
    if start is not None:
        found.append((start, last))
    return [(s, e + STEP) for s, e in found if e - s >= MIN_LEN]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--at", nargs="+", required=True, help="one or more start-end spans, e.g. 1:02:07-1:02:20")
    args = ap.parse_args()

    out = stream_dir(args.name)
    sr = sf.info(str(out / "voice16.wav")).samplerate
    words = [w for s in load_json(out / "transcript.json")["segments"] for w in s.get("words", [])]
    for span in args.at:
        a, b = (parse_ts(x, 0) for x in span.split("-"))
        print(f"== {fmt_ts(a)}-{fmt_ts(b)}")
        for s, e in bursts(out / "voice16.wav", sr, a, b):
            said = " ".join(w["w"].strip() for w in words if s - 0.2 <= w["s"] <= e)
            print(f"  {s:.2f}-{e:.2f}  {said}")


if __name__ == "__main__":
    main()
