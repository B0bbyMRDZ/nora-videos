#!/usr/bin/env python3
"""Compose a 9:16 Short: avatar hook -> timed B-roll -> avatar CTA, with captions and title overlays."""
import json, subprocess, sys, os
from pathlib import Path

cfg = json.load(open(sys.argv[1]))
work = Path(cfg["workdir"]); tmp = work / "tmp"; tmp.mkdir(exist_ok=True)
FPS = 30
W, H = 1080, 1920

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(" ".join(map(str, cmd))); print(r.stderr[-3000:]); sys.exit(1)

def fr(t):  # seconds -> frame index on the 30fps grid
    return int(round(t * FPS))

fill = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=yuv420p"

# ---- 1. normalise avatar to 30fps, video only ----
avatar = tmp / "avatar30.mp4"
run(["ffmpeg", "-y", "-v", "error", "-i", cfg["avatar"], "-an", "-vf", fill,
     "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(avatar)])
avatar_dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                            "-of", "csv=p=0", cfg["avatar"]]).decode())
end_f = fr(avatar_dur)

# ---- 2. build the timeline pieces ----
pieces = []
hook_end = fr(cfg["broll"][0]["at"])
pieces.append(("avatar", 0, hook_end))
for i, b in enumerate(cfg["broll"]):
    a = fr(b["at"])
    z = fr(cfg["broll"][i + 1]["at"]) if i + 1 < len(cfg["broll"]) else fr(cfg["cta_at"])
    pieces.append(("clip", a, z, b))
pieces.append(("avatar", fr(cfg["cta_at"]), end_f))

parts = []
for n, pc in enumerate(pieces):
    out = tmp / f"p{n:02d}.mp4"
    if pc[0] == "avatar":
        _, a, z = pc
        vf = f"trim=start_frame={a}:end_frame={z},setpts=PTS-STARTPTS"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(avatar), "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(out)])
    else:
        _, a, z, b = pc
        frames = z - a
        src = str(work / b["file"])
        zoom = b.get("zoom", 1.0)
        vf = fill
        if zoom != 1.0:   # gentle push-in for static shots
            vf += (f",scale={int(W*zoom)}:{int(H*zoom)},"
                   f"crop={W}:{H}:(iw-{W})/2:(ih-{H})/2")
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(b["in"]), "-i", src, "-an", "-vf", vf,
             "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(out)])
    parts.append(out)

listfile = tmp / "list.txt"
listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
joined = tmp / "joined.mp4"
run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(joined)])

# ---- 3. captions (word timing spread across each spoken phrase) ----
def ass_time(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"

words = []
for seg in cfg["speech"]:
    s, e, text = seg
    toks = text.split()
    weights = [len(w.strip(",.?!:")) + 2 for w in toks]
    tot = sum(weights); t = s
    for w, wt in zip(toks, weights):
        d = (e - s) * wt / tot
        words.append([t, t + d, w, seg]); t += d

# group into chunks of <=3 words that never cross a phrase
chunks, cur = [], []
for w in words:
    if cur and (len(cur) == 3 or cur[-1][3] is not w[3]):
        chunks.append(cur); cur = []
    cur.append(w)
chunks.append(cur)

YELLOW, WHITE = r"{\c&H004FD5FF&}", r"{\c&H00FFFFFF&}"
events = []
for ci, ch in enumerate(chunks):
    start = ch[0][0]
    nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else avatar_dur
    end = nxt if nxt - ch[-1][1] < 0.45 else ch[-1][1] + 0.25
    for wi, w in enumerate(ch):
        ws = w[0] if wi else start
        we = ch[wi + 1][0] if wi + 1 < len(ch) else end
        line = " ".join((YELLOW + x[2] + WHITE) if j == wi else x[2] for j, x in enumerate(ch))
        events.append(f"Dialogue: 1,{ass_time(ws)},{ass_time(we)},Caption,,0,0,0,,{line}")

for t in cfg["titles"]:
    events.append(f"Dialogue: 2,{ass_time(t[0])},{ass_time(t[1])},{t[3] if len(t) > 3 else 'Title'},,0,0,0,,{t[2]}")

ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Inter ExtraBold,82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,7,3,2,110,110,560,1
Style: Title,Inter ExtraBold,72,&H00FFFFFF,&H00FFFFFF,&H10786F1F,&H00000000,0,0,0,0,100,100,1,0,3,22,0,8,90,90,230,1
Style: Hook,Inter ExtraBold,80,&H00FFFFFF,&H00FFFFFF,&H10786F1F,&H00000000,0,0,0,0,100,100,1,0,3,24,0,8,80,80,210,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"
(tmp / "overlay.ass").write_text(ass)

# ---- 4. final render: overlays + avatar audio, loudness-normalised ----
out = work / cfg["output"]
run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-i", cfg["avatar"],
     "-filter_complex", f"[0:v]ass={tmp/'overlay.ass'}[v];[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]",
     "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
     "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)])
print("wrote", out)
