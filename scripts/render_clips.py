"""Render picked highlights as vertical Shorts and/or horizontal clips, with burned-in captions.

Usage:
  python render_clips.py --name <stream-name> [--picks picks.json] [--vertical-mode center|crop|fit] [--no-captions]

streams/<name>/picks.json looks like:
  {"clips": [{"id": "c03", "start": 1234.5, "end": 1262.0, "title": "Squad wipe from the rooftop",
              "reason": "4 kills in 10s, team screaming", "formats": ["vertical", "horizontal"],
              "vertical_mode": "fit",                       # optional, overrides --vertical-mode
              "captions": [{"start": 1236.2, "end": 1238.0, "text": "Bro he's right behind you!"}]}]}
Clips with "captions" get those phrases (e.g. translated into your audience's language); without, word-by-word
captions from transcript.json in the spoken language. Captions are burned in with libass, so the font must
cover the script you caption in (set a font in ass_header() for non-Latin scripts if glyphs show as boxes).

Output: streams/<name>/clips/<id>_vertical.mp4, <id>_horizontal.mp4 and streams/<name>/review.html
"""
import argparse
import html
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fmt_ts, load_json, probe_size, run, stream_dir, tools  # noqa: E402

V_W, V_H = 1080, 1920
CAPTION_FONT = os.environ.get("CLUTCHCUT_CAPTION_FONT", "Arial Black")  # any font name libass can find
H_W, H_H = 1920, 1080


# ---------------------------------------------------------------- captions

def ass_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def chunk_words(words, max_words=3, max_gap=0.6):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > max_gap or re.search(r"[.!?]$", cur[-1]["w"])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def ass_header(play_w, play_h, font_size, margin_v):
    return [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {play_w}", f"PlayResY: {play_h}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{CAPTION_FONT},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,"
        f"{max(4, font_size // 14)},2,2,60,60,{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def write_phrase_ass(path, captions, start, end, play_w, play_h, font_size, margin_v):
    """Translated captions: whole phrases (from picks.json, absolute VOD times), auto-wrapped."""
    lines = ass_header(play_w, play_h, font_size, margin_v)
    n = 0
    for c in captions:
        s, e = float(c["start"]) - start, float(c["end"]) - start
        if e <= 0 or s >= end - start or not c.get("text"):
            continue
        lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(min(e, end - start))},Cap,,0,0,0,,"
                     f"{ass_escape(c['text'].upper())}")
        n += 1
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return n > 0


def write_ass(path, words, start, end, play_w, play_h, font_size, margin_v):
    """Word-by-word captions: 1-3 words on screen, the spoken word highlighted yellow."""
    clip_words = [dict(w, s=w["s"] - start, e=w["e"] - start) for w in words
                  if w["e"] > start and w["s"] < end and w["w"]]
    lines = ass_header(play_w, play_h, font_size, margin_v)
    chunks = chunk_words(clip_words)
    for ci, chunk in enumerate(chunks):
        chunk_end = chunk[-1]["e"]
        if ci + 1 < len(chunks) and chunks[ci + 1][0]["s"] - chunk_end < 0.35:
            chunk_end = chunks[ci + 1][0]["s"]
        for wi, w in enumerate(chunk):
            w_start = w["s"] if wi else chunk[0]["s"]
            w_end = chunk[wi + 1]["s"] if wi + 1 < len(chunk) else chunk_end
            if w_end <= w_start:
                continue
            parts = []
            for wj, other in enumerate(chunk):
                token = ass_escape(other["w"].upper())
                parts.append("{\\c&H00FFFF&}" + token + "{\\c&HFFFFFF&}" if wj == wi else token)
            lines.append(f"Dialogue: 0,{ass_time(w_start)},{ass_time(w_end)},Cap,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return bool(chunks)


# ---------------------------------------------------------------- encoding

_ENCODER = None


def video_codec_args():
    global _ENCODER
    if _ENCODER is None:
        ffmpeg, _, _ = tools()
        try:
            run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=0.2", "-c:v", "h264_nvenc",
                 "-f", "null", "-"])
            _ENCODER = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0",
                        "-pix_fmt", "yuv420p"]
            print("Encoding on the GPU (NVENC).")
        except RuntimeError:
            _ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
            print("Encoding on the CPU (x264).")
    return _ENCODER


def vertical_filter(mode, src_w, src_h, subs):
    blur_bg = (f"[0:v]split[a][b];[a]scale={V_W}:{V_H}:force_original_aspect_ratio=increase,crop={V_W}:{V_H},"
               f"boxblur=30:2,eq=brightness=-0.12[bg];")
    if mode == "crop":
        # Full-height 9:16 slice around the crosshair.
        chain = f"[0:v]crop=ih*9/16:ih,scale={V_W}:{V_H}"
    elif mode == "fit":
        # Whole frame (nothing lost), letterboxed over a blurred copy.
        chain = blur_bg + f"[b]scale={V_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-120"
    else:
        # center (default): square crop around the crosshair, a little above middle, captions underneath.
        side = min(src_w, src_h)
        chain = blur_bg + f"[b]crop={side}:{side},scale={V_W}:{V_W}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-160"
    if subs:
        chain += f",subtitles={subs}"
    return chain + ",setsar=1[out]"


def horizontal_filter(subs):
    chain = (f"[0:v]scale={H_W}:{H_H}:force_original_aspect_ratio=decrease,"
             f"pad={H_W}:{H_H}:(ow-iw)/2:(oh-ih)/2")
    if subs:
        chain += f",subtitles={subs}"
    return chain + ",setsar=1[out]"


def render(out, clip, fmt, mode, words, captions):
    ffmpeg, _, _ = tools()
    src_w, src_h = probe_size(out / "vod.mp4")
    start, end = float(clip["start"]), float(clip["end"])
    cid = clip["id"]
    mode = clip.get("vertical_mode", mode)
    subs = None
    if captions:
        subs = f"clips/{cid}_{fmt}.ass"
        if fmt == "vertical":
            size, w, h, margin = 74, V_W, V_H, (360 if mode == "center" else 300)
        else:
            size, w, h, margin = 58, H_W, H_H, 70
        if clip.get("captions"):
            ok = write_phrase_ass(out / subs, clip["captions"], start, end, w, h, size, margin)
        else:
            ok = write_ass(out / subs, words, start, end, w, h, size + 12, margin)
        if not ok:
            subs = None
    graph = vertical_filter(mode, src_w, src_h, subs) if fmt == "vertical" else horizontal_filter(subs)
    target = f"clips/{cid}_{fmt}.mp4"
    run([ffmpeg, "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", "vod.mp4",
         "-filter_complex", graph, "-map", "[out]", "-map", "0:a?",
         *video_codec_args(),
         "-c:a", "aac", "-b:a", "192k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-movflags", "+faststart", target], cwd=out)
    return target


# ---------------------------------------------------------------- review page

def review_page(out, clips, rendered):
    meta = load_json(out / "meta.json", {}) or {}
    cards = []
    for c in clips:
        vids = "".join(
            f'<figure class="{fmt}"><video controls preload="metadata" src="{html.escape(path)}"></video>'
            f'<figcaption>{fmt} &middot; <a href="{html.escape(path)}" download>download</a></figcaption></figure>'
            for fmt, path in rendered.get(c["id"], []))
        cards.append(
            f'<article><header><label><input type="checkbox" checked data-id="{c["id"]}"> '
            f'<b>{html.escape(c.get("title", c["id"]))}</b></label>'
            f'<span class="t">{c["id"]} &middot; {fmt_ts(c["start"])}&ndash;{fmt_ts(c["end"])} '
            f'({c["end"] - c["start"]:.0f}s)</span></header>'
            f'<p>{html.escape(c.get("reason", ""))}</p><div class="vids">{vids}</div>'
            f'<input class="note" data-id="{c["id"]}" placeholder="Note for Claude (optional): why keep / why not">'
            f'</article>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Stream Highlights</title>
<style>
:root{{--bg:#fafafa;--fg:#111;--card:#fff;--muted:#666;--line:#e3e3e3;--accent:#e11d48}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f0f10;--fg:#eee;--card:#18181b;--muted:#9a9a9a;--line:#2a2a2e}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px;font-size:24px}} .sub{{color:var(--muted);margin:0 0 20px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}}
header{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between}}
.t{{color:var(--muted);font-variant-numeric:tabular-nums}} p{{margin:6px 0 12px;color:var(--muted)}}
.vids{{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}} figure{{margin:0}}
figure.vertical video{{width:240px;aspect-ratio:9/16}} figure.horizontal video{{width:min(560px,100%);aspect-ratio:16/9}}
video{{background:#000;border-radius:8px;display:block}} figcaption{{color:var(--muted);font-size:13px;margin-top:4px}}
a{{color:var(--accent)}} button{{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}} input{{accent-color:var(--accent)}}
input.note{{width:100%;box-sizing:border-box;margin-top:12px;padding:8px 10px;border-radius:8px;
border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit}}
</style></head><body><main>
<h1>Stream highlights</h1>
<p class="sub">{html.escape(meta.get("title") or "")} &middot; {len(clips)} clips. Untick the ones you don't want,
add notes, then copy your feedback and paste it to Claude. <button id="copy">Copy feedback</button></p>
{''.join(cards)}
</main><script>
document.getElementById('copy').onclick=()=>{{
 const lines=[...document.querySelectorAll('input[type=checkbox][data-id]')].map(x=>{{
  const note=document.querySelector('input.note[data-id="'+x.dataset.id+'"]').value.trim();
  return x.dataset.id+': '+(x.checked?'KEEP':'REJECT')+(note?' - '+note:'');}});
 navigator.clipboard.writeText('Clip feedback:\\n'+lines.join('\\n'));
 document.getElementById('copy').textContent='Copied - paste it to Claude';}};
</script></body></html>"""
    (out / "review.html").write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="stream name (folder under streams/)")
    ap.add_argument("--picks", default="picks.json")
    ap.add_argument("--vertical-mode", default="center", choices=["center", "crop", "fit"])
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--only", help="comma-separated clip ids to (re)render")
    args = ap.parse_args()

    out = stream_dir(args.name)
    picks = load_json(out / args.picks) or load_json(args.picks)
    if not picks:
        sys.exit(f"No picks file found at {out / args.picks}")
    clips = picks["clips"]
    (out / "clips").mkdir(exist_ok=True)
    transcript = load_json(out / "transcript.json", {"segments": []})
    words = [w for s in transcript["segments"] for w in s.get("words", [])]
    # Fine-tuned models give chunk times but no word times: caption whole chunks instead.
    chunk_captions = None if words else [{"start": s["start"], "end": s["end"], "text": s["text"]}
                                         for s in transcript["segments"]]
    only = set(args.only.split(",")) if args.only else None

    rendered = {}
    for c in clips:
        for fmt in c.get("formats", ["vertical", "horizontal"]):
            target = f"clips/{c['id']}_{fmt}.mp4"
            if only is None or c["id"] in only or not (out / target).exists():
                print(f"Rendering {c['id']} {fmt} ({fmt_ts(c['start'])}-{fmt_ts(c['end'])}): {c.get('title', '')}")
                clip = c if c.get("captions") or not chunk_captions else dict(c, captions=chunk_captions)
                render(out, clip, fmt, args.vertical_mode, words, not args.no_captions)
            rendered.setdefault(c["id"], []).append((fmt, target))
    review_page(out, clips, rendered)
    print(f"Done. Review page: {out / 'review.html'}")


if __name__ == "__main__":
    main()
