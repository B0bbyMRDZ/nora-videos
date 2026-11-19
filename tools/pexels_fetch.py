#!/usr/bin/env python3
"""
Download the B-roll clips a video needs from Pexels.

Usage (from the folder that holds your .env file):
    python pexels_fetch.py video01_clips.csv
    python pexels_fetch.py video01_clips.csv video02_clips.csv
    python pexels_fetch.py video01_clips.csv --candidates 1

What it does, for every row in the CSV:
  1. Searches Pexels videos (portrait only) for the row's search_query,
     then its fallback_queries if needed.
  2. Skips clips that are too short for the slot or already used in this video.
  3. Downloads the best ~1080-wide portrait file for the top N matches
     ("candidates", default 2) so there is a spare if one doesn't fit.
  4. Writes a manifest.csv (clip, file, Pexels link, photographer) and
     zips everything into  pexels_downloads/videoNN.zip  ready to upload.

Re-running is safe: files that already exist are not downloaded again.

Needs Python 3.8+ and nothing else (standard library only).
The .env file must contain one line like:
    PEXELS_API_KEY=your_key_here
(PEXELS_KEY or PEXELS_API also work.)
"""

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API_BASE = os.environ.get("PEXELS_API_BASE", "https://api.pexels.com")
USER_AGENT = "puppy-shorts-fetcher/1.0 (+python urllib)"
KEY_NAMES = ("PEXELS_API_KEY", "PEXELS_KEY", "PEXELS_API")
TARGET_WIDTH = 1080          # ideal file width for a 1080x1920 Short
MIN_WIDTH = 720              # anything narrower looks soft on a phone
EXTRA_SECONDS = 1.0          # source clip must be at least slot length + this
MIN_SOURCE_SECONDS = 4.0     # never take a clip shorter than this


# ---------------------------------------------------------------- setup ----

def load_env_key():
    """Find the Pexels key in the environment or a .env file."""
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name].strip()

    search_dirs = [Path.cwd(), Path(__file__).resolve().parent]
    for folder in search_dirs:
        env_path = folder / ".env"
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name in KEY_NAMES and value:
                return value

    sys.exit(
        "Couldn't find your Pexels key.\n"
        "Put a .env file next to this script (or run from its folder) containing:\n"
        "    PEXELS_API_KEY=your_key_here"
    )


def slugify(text, limit=40):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:limit].rstrip("-") or "clip"


def read_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"video", "clip", "duration_sec", "search_query"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        sys.exit(f"{csv_path}: missing column(s): {', '.join(sorted(missing))}")
    return rows


# ------------------------------------------------------------- pexels ----

def api_get(path, params, key, retries=4):
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                remaining = resp.headers.get("X-Ratelimit-Remaining")
                data = json.loads(resp.read().decode("utf-8"))
                return data, remaining
        except urllib.error.HTTPError as e:
            if e.code == 401 or e.code == 403:
                sys.exit("Pexels rejected the API key (HTTP %d). Check the key in your .env file." % e.code)
            if e.code == 429 or e.code >= 500:
                wait = 10 * (attempt + 1)
                print(f"    Pexels busy (HTTP {e.code}); waiting {wait}s and retrying...")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            wait = 5 * (attempt + 1)
            print(f"    Network problem ({e.reason}); retrying in {wait}s...")
            time.sleep(wait)
    sys.exit("Gave up talking to Pexels after several retries. Try again in a few minutes.")


def best_file(video):
    """Pick the portrait MP4 whose width is closest to 1080 (not tiny, not 4K)."""
    files = [
        f for f in video.get("video_files", [])
        if f.get("link") and (f.get("file_type") or "video/mp4") == "video/mp4"
        and (f.get("height") or 0) > (f.get("width") or 0)
    ]
    if not files:
        return None
    good = [f for f in files if (f.get("width") or 0) >= MIN_WIDTH]
    pool = good or files
    return min(pool, key=lambda f: (abs((f.get("width") or 0) - TARGET_WIDTH), -(f.get("width") or 0)))


def find_candidates(row, key, used_ids, how_many):
    slot = float(row["duration_sec"] or 0)
    needed = max(slot + EXTRA_SECONDS, MIN_SOURCE_SECONDS)
    queries = [row["search_query"].strip()]
    queries += [q.strip() for q in (row.get("fallback_queries") or "").split("|") if q.strip()]

    picks = []
    for query in queries:
        data, remaining = api_get(
            "/videos/search",
            {"query": query, "orientation": "portrait", "size": "medium", "per_page": 40},
            key,
        )
        for video in data.get("videos", []):
            vid = video.get("id")
            if vid in used_ids or any(p["video"]["id"] == vid for p in picks):
                continue
            if (video.get("duration") or 0) < needed:
                continue
            chosen = best_file(video)
            if not chosen:
                continue
            picks.append({"video": video, "file": chosen, "query": query})
            if len(picks) >= how_many:
                return picks, remaining
    return picks, remaining


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return "already had it"
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, length=1024 * 512)
            tmp.replace(dest)
            return "downloaded"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"    download hiccup ({e}); retrying...")
            time.sleep(5 * (attempt + 1))
    if tmp.exists():
        tmp.unlink()
    return "FAILED"


# --------------------------------------------------------------- main ----

def process_csv(csv_path, out_root, key, how_many, make_zip):
    rows = read_rows(csv_path)
    video_no = str(rows[0]["video"]).zfill(2)
    folder = out_root / f"video{video_no}"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(csv_path, folder / Path(csv_path).name)

    print(f"\n=== Video {video_no}: {len(rows)} clip slots -> {folder}")
    used_ids = set()
    manifest = []
    problems = []
    remaining = None

    for row in rows:
        clip_no = str(row["clip"]).zfill(2)
        label = row["search_query"]
        print(f"  Clip {clip_no} ({row['duration_sec']}s): {label}")
        picks, remaining = find_candidates(row, key, used_ids, how_many)
        if not picks:
            print("    !! nothing suitable found - try a different search term in the CSV")
            problems.append(f"clip {clip_no}: no match for '{label}' or its fallbacks")
            continue
        if len(picks) < how_many:
            problems.append(f"clip {clip_no}: only {len(picks)} of {how_many} candidates found")

        for i, pick in enumerate(picks):
            letter = "abcdefghij"[i]
            video, f = pick["video"], pick["file"]
            used_ids.add(video["id"])
            name = f"clip{clip_no}{letter}_{slugify(pick['query'])}_{video['id']}.mp4"
            status = download(f["link"], folder / name)
            print(f"    {letter}: {name}  [{video.get('duration')}s, {f.get('width')}x{f.get('height')}] {status}")
            if status == "FAILED":
                problems.append(f"clip {clip_no}{letter}: download failed")
                continue
            manifest.append({
                "clip": clip_no,
                "candidate": letter,
                "file": name,
                "slot_start": row.get("start", ""),
                "slot_end": row.get("end", ""),
                "slot_seconds": row["duration_sec"],
                "source_seconds": video.get("duration"),
                "width": f.get("width"),
                "height": f.get("height"),
                "query_used": pick["query"],
                "pexels_id": video["id"],
                "pexels_url": video.get("url", ""),
                "photographer": (video.get("user") or {}).get("name", ""),
            })

    with open(folder / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["clip", "candidate", "file", "slot_start", "slot_end", "slot_seconds",
                  "source_seconds", "width", "height", "query_used", "pexels_id",
                  "pexels_url", "photographer"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest)

    zip_path = None
    if make_zip:
        zip_path = out_root / f"video{video_no}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as z:
            for item in sorted(folder.iterdir()):
                if item.is_file() and not item.name.endswith(".part"):
                    z.write(item, arcname=f"video{video_no}/{item.name}")

    print(f"  -> {len(manifest)} files saved.")
    if remaining is not None:
        print(f"  (Pexels requests left this hour: {remaining})")
    if problems:
        print("  Check these:")
        for p in problems:
            print(f"    - {p}")
    if zip_path:
        size_mb = zip_path.stat().st_size / 1_000_000
        print(f"  Upload this file to Claude: {zip_path.resolve()}  ({size_mb:.0f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Download Pexels B-roll listed in a clips CSV.")
    parser.add_argument("csv_files", nargs="+", help="one or more videoNN_clips.csv files")
    parser.add_argument("--out", default="pexels_downloads", help="output folder (default: pexels_downloads)")
    parser.add_argument("--candidates", type=int, default=2,
                        help="clips to download per slot, so there's a spare (default: 2)")
    parser.add_argument("--no-zip", action="store_true", help="skip making the .zip")
    args = parser.parse_args()

    key = load_env_key()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    for csv_file in args.csv_files:
        if not Path(csv_file).is_file():
            print(f"Skipping {csv_file}: file not found")
            continue
        process_csv(csv_file, out_root, key, max(1, args.candidates), not args.no_zip)
    print("\nDone.")


if __name__ == "__main__":
    main()
