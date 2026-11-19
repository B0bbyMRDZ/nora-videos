"""Make contact sheets: one row per clip, 4 frames across. Usage: contact_sheet.py <clip_dir> <out_prefix>"""
import subprocess, glob, os, sys, tempfile
from PIL import Image, ImageDraw

clip_dir, prefix = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.join(clip_dir, "clip*.mp4")))
tmp = tempfile.mkdtemp()
FW, FH, N = 90, 160, 4
rows = []
for f in files:
    tag = os.path.basename(f)[4:7]
    d = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f]))
    row = Image.new("RGB", (50 + FW * N, FH), "black")
    ImageDraw.Draw(row).text((4, 70), tag, fill="yellow")
    for k in range(N):
        t = round(d * (k + 0.5) / N, 1)
        out = os.path.join(tmp, f"{tag}_{k}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", f, "-frames:v", "1", "-vf", f"scale={FW}:{FH}", out])
        if os.path.exists(out):
            im = Image.open(out); ImageDraw.Draw(im).text((2, FH - 12), str(t), fill="yellow")
            row.paste(im, (50 + FW * k, 0))
    rows.append(row)
per = 18  # rows per sheet: 3 columns x 6
W = rows[0].width
for s in range(0, len(rows), per):
    chunk = rows[s:s + per]
    sheet = Image.new("RGB", (W * 3, FH * 6), "white")
    for i, r in enumerate(chunk):
        sheet.paste(r, ((i // 6) * W, (i % 6) * FH))
    sheet.save(f"{prefix}{s // per + 1}.jpg", quality=78)
print(len(rows), "clips")
