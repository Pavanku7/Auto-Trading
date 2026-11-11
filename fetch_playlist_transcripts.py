"""
================================================================
  fetch_playlist_transcripts.py
  Download + clean captions for every video in a YouTube playlist
================================================================

  WHY
  ─────────────────────────────────────────────
  Claude can't watch videos, but it CAN read text. This script
  pulls the auto-generated (or manual) captions for every video in
  a playlist, strips the VTT timing/markup junk, and writes one
  clean .txt transcript per video into ./transcripts/.

  Point Claude at that folder afterwards and it will extract each
  strategy's exact rules (entry, stop, target, filters, sizing) so
  they can be backtested with backtest_v4.py.

  SETUP
  ─────────────────────────────────────────────
    pip install yt-dlp

  ⚠️ YOUTUBE BLOCKS ANONYMOUS CAPTION DOWNLOADS (HTTP 429)
  ─────────────────────────────────────────────
  YouTube rate-limits caption requests that aren't logged in. On
  most networks a plain run returns "0 transcripts" with a 429.
  The fix is to pass your logged-in cookies. Two ways:

    A) From your browser (must be FULLY CLOSED — it locks the
       cookie DB while open):
         python fetch_playlist_transcripts.py --cookies-from-browser chrome

    B) From an exported cookies.txt (works with the browser open):
       install a "Get cookies.txt" browser extension, export
       youtube.com, save it, then:
         python fetch_playlist_transcripts.py --cookies cookies.txt

  USAGE
  ─────────────────────────────────────────────
    # Whole playlist (default = the one from the chat) + cookies:
    python fetch_playlist_transcripts.py --cookies-from-browser edge

    # A specific playlist or single video:
    python fetch_playlist_transcripts.py "<url>" --cookies cookies.txt

    # Different language / output dir:
    python fetch_playlist_transcripts.py "<url>" --lang en --out transcripts

  NOTES
  ─────────────────────────────────────────────
  - Uses yt-dlp as a library; no ffmpeg or video download needed.
  - Auto-captions can be messy/duplicated — we de-duplicate lines.
  - If a video has no captions in the chosen language, it's skipped
    and reported at the end (nothing to extract from those).
================================================================
"""

import argparse
import glob
import os
import re
import sys

# Windows consoles default to cp1252 and choke on ✓/✗. Force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_PLAYLIST = (
    "https://www.youtube.com/playlist?list=PLnn1F0U-9KimoxKVEJVVVOqB3CGpav_LB"
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ================================================================
# VTT / SRV cleaning
# ================================================================
_TAG_RE   = re.compile(r"<[^>]+>")                 # <c>, <00:00:01.000> inline tags
_TS_RE    = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")   # cue timing line
_NUM_RE   = re.compile(r"^\d+$")                   # bare cue index
_HEADERS  = ("WEBVTT", "Kind:", "Language:", "NOTE")


def clean_vtt(raw: str) -> str:
    """Turn raw VTT caption text into plain, de-duplicated prose."""
    lines_out = []
    prev = None
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(_HEADERS):
            continue
        if _TS_RE.match(s) or _NUM_RE.match(s) or "-->" in s:
            continue
        s = _TAG_RE.sub("", s)                     # drop inline karaoke tags
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        # Auto-captions repeat each line as it scrolls — skip immediate dupes
        if s == prev:
            continue
        prev = s
        lines_out.append(s)

    # Collapse into readable paragraphs (~cap line length)
    text = " ".join(lines_out)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_filename(name: str, limit: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:limit].rstrip(" .")


# ================================================================
# MAIN
# ================================================================
def main():
    ap = argparse.ArgumentParser(description="Download + clean YouTube playlist transcripts.")
    ap.add_argument("url", nargs="?", default=DEFAULT_PLAYLIST,
                    help="Playlist or video URL (default: the beginners-intraday playlist).")
    ap.add_argument("--lang", default="en", help="Caption language code (default: en).")
    ap.add_argument("--out", default="transcripts", help="Output folder (default: transcripts).")
    ap.add_argument("--cookies-from-browser", dest="cookies_browser",
                    help="Read cookies from a browser, e.g. chrome/edge/firefox/brave. "
                         "The browser must be CLOSED (it locks its cookie DB while open).")
    ap.add_argument("--cookies", dest="cookies_file",
                    help="Path to a cookies.txt file (Netscape format). Alternative to "
                         "--cookies-from-browser; works even with the browser open.")
    args = ap.parse_args()

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        print("ERROR: yt-dlp is not installed.\n  Run:  pip install yt-dlp")
        sys.exit(1)

    out_dir = os.path.join(BASE_DIR, args.out)
    raw_dir = os.path.join(out_dir, "_raw")       # yt-dlp writes .vtt files here
    os.makedirs(raw_dir, exist_ok=True)

    # ── Let yt-dlp do the downloading. It handles YouTube's headers,
    #    retries and rate-limiting (429s) that a bare urlopen can't.
    #    We only skip the actual VIDEO; subtitles still download.
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,               # no video/audio, captions only
        "writesubtitles": True,              # manual subs if present
        "writeautomaticsub": True,           # else auto-generated
        "subtitleslangs": [f"{args.lang}.*", args.lang],
        "subtitlesformat": "vtt",
        "nocheckcertificate": True,          # this network intercepts SSL
        "ignoreerrors": True,                # one bad video won't abort the run
        "sleep_interval_subtitles": 1,       # be polite → avoid 429
        "retries": 5,
        "outtmpl": os.path.join(raw_dir, "%(playlist_index)03d-%(id)s.%(ext)s"),
    }

    # YouTube 429-blocks anonymous caption downloads. Logged-in cookies fix it.
    if args.cookies_file:
        ydl_opts["cookiefile"] = args.cookies_file
        print(f"Using cookies file: {args.cookies_file}")
    elif args.cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (args.cookies_browser,)
        print(f"Using cookies from browser: {args.cookies_browser} "
              f"(make sure it is fully closed)")

    print(f"Downloading captions for: {args.url}")
    print("(yt-dlp handles rate-limiting; this can take a minute)\n")
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([args.url])

    # ── Clean every .vtt yt-dlp wrote into readable .txt transcripts.
    vtts = sorted(glob.glob(os.path.join(raw_dir, "*.vtt")))
    ok, empty = [], []
    for vtt in vtts:
        base = os.path.basename(vtt)
        with open(vtt, encoding="utf-8", errors="replace") as f:
            text = clean_vtt(f.read())

        # Strip yt-dlp's ".en"/".en-orig" language tag from the stem
        stem = re.sub(r"\.[A-Za-z-]+$", "", os.path.splitext(base)[0])
        out_name = f"{safe_filename(stem)}.txt"
        out_path = os.path.join(out_dir, out_name)

        if len(text) < 50:
            empty.append(base)
            continue

        header = (
            f"SOURCE: {base}\n"
            f"WORDS: {len(text.split())}\n"
            f"{'='*60}\n\n"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header + text + "\n")
        print(f"  ✓ {len(text.split()):>5} words → {out_name}")
        ok.append(out_name)

    # ── Summary
    print(f"\n{'='*60}")
    print(f"  Done. {len(ok)} transcript(s) saved to: {out_dir}")
    print(f"  (raw .vtt files kept in {raw_dir} — safe to delete)")
    if empty:
        print(f"  {len(empty)} had no usable captions: {', '.join(empty)}")
    if not vtts:
        print("  No caption files were downloaded — the videos may have")
        print("  captions disabled, or YouTube blocked the request.")
    print(f"{'='*60}")
    print("\nNext: tell Claude 'read the transcripts folder' and it will")
    print("extract each strategy's rules so they can be backtested.")


if __name__ == "__main__":
    main()
