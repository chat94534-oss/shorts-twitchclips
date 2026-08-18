#!/usr/bin/env python3
"""Twitch clip -> YouTube Short, fully automated.

    twitch.discover()  ->  yt-dlp  ->  ffmpeg 9:16  ->  youtube_upload.py

One run builds every remaining slot for the day and hands each video to
YouTube's own scheduler via publishAt, so GitHub's flaky cron only has to
fire once. State (which clips are spent) is committed back by the workflow.

    python make_clip.py --fill-day          # the CI path
    python make_clip.py --no-upload         # render one locally, upload nothing
    python make_clip.py --privacy unlisted  # one video, now, unlisted
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from zoneinfo import ZoneInfo

import twitch

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = HERE
STATE_FILE = os.path.join(HERE, "state.json")
RUNS_DIR = os.path.join(HERE, "runs")
LOGS_DIR = os.path.join(HERE, "logs")
HISTORY_CSV = os.path.join(LOGS_DIR, "history.csv")
TOKEN_EXPIRED_FLAG = os.path.join(LOGS_DIR, "TOKEN_EXPIRED.txt")
LOCK_FILE = os.path.join(LOGS_DIR, "run.lock")
KEEP_RUNS_DAYS = 3

TZ = ZoneInfo("America/New_York")
PUBLISH_SLOTS = [(12, 0), (15, 0), (18, 0), (21, 0)]

W, H, FPS = 1080, 1920, 30
# The clip sits in a fixed box, scaled to fit (never distorted, never cropped).
# 1242 is 1.15x the frame width, so it fills more height and the blur bars stay
# thin. force_original_aspect_ratio=decrease means an odd-shaped source only
# ever comes out SMALLER than this box, so the text positions below stay clear.
FG_W, FG_H = 1242, 698
FG_TOP = (H - FG_H) // 2                 # 611
HOOK_Y = FG_TOP - 40                     # baseline solved in ffmpeg as HOOK_Y-text_h
CREDIT_Y = FG_TOP + FG_H + 40            # 1349
CREDIT_SIZE = 44
HOOK_SIZES = [96, 84, 72, 64]            # tried largest-first until it fits
HOOK_MAX_CHARS = 30                      # a hook is a glance, not a sentence
# Words that read as broken when a trim lands on them.
HOOK_TAIL_STOPWORDS = {
    "THE", "A", "AN", "AND", "OR", "TO", "AT", "OF", "IN", "ON", "FOR",
    "WITH", "BUT", "IS", "WAS", "HIS", "HER", "THEIR", "MY",
}
STYLE = "fill"                           # "fill" (full-frame) or "blur" (letterboxed)

PRESET = os.environ.get("X264_PRESET", "medium")
FONT = (r"C\:/Windows/Fonts/arialbd.ttf" if os.name == "nt"
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")


# --------------------------------------------------------------------------- #
# helpers (same shape as the other shorts-* channels)
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n"
            f"STDERR:\n{p.stderr[-2000:]}"
        )
    return p


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def cleanup_runs(days=KEEP_RUNS_DAYS):
    if not os.path.isdir(RUNS_DIR):
        return
    cutoff = time.time() - days * 86400
    for name in os.listdir(RUNS_DIR):
        path = os.path.join(RUNS_DIR, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


_LOCK_OWNED = False


def acquire_lock(stale_minutes=45):
    global _LOCK_OWNED
    try:
        if os.path.exists(LOCK_FILE):
            if time.time() - os.path.getmtime(LOCK_FILE) < stale_minutes * 60:
                log("Another run is active — skipping this one.")
                return False
            log("Removing stale lock from a crashed run.")
            os.remove(LOCK_FILE)
        with open(LOCK_FILE, "x", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {dt.datetime.now().isoformat()}\n")
        _LOCK_OWNED = True
        return True
    except (FileExistsError, OSError):
        return False


def release_lock():
    global _LOCK_OWNED
    if _LOCK_OWNED:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
        _LOCK_OWNED = False


def clear_flag(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "clip"


# --------------------------------------------------------------------------- #
# copy: hook + metadata
# --------------------------------------------------------------------------- #
def _clean(text, limit):
    """Strip the emote spam and punctuation noise typical of Twitch titles."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"[^\w\s'!?.,&:-]", "", text)
    return text[:limit].strip(" -:,")


# Used only when a clip's own title is too thin to be a hook ("Cops", "LUL").
# Deliberately gender-neutral: we have no idea who is on screen.
GENERIC_HOOKS = [
    "WAIT FOR IT",
    "WATCH THE END",
    "NOBODY EXPECTED THIS",
    "THIS GOT WILD FAST",
    "IT ONLY GETS WORSE",
]


def write_copy(clip):
    """Hook line for the video, plus title/description/tags for YouTube.

    ponytail: the hook is the clip's own title, cleaned. Twitch titles are
    written by a human who just watched the moment, so they are usually already
    hooks ("shroud lost it", "aimed for the egg, hit the bystanders"). Thin
    ones fall back to a generic hook picked deterministically by clip id, so a
    rerun of the same clip produces the same video. Upgrade path: if hooks ever
    measurably cap retention, swap this for a real LLM call — Pollinations' free
    text tier returns 402 now, so that means an API key.
    """
    streamer = clip["broadcaster_name"]
    game = clip.get("game_name", "Twitch")
    original = _clean(clip.get("title"), 90)

    hook = original if len(original) >= 10 and len(original.split()) >= 2 else ""
    if not hook:
        idx = sum(ord(c) for c in clip.get("id", "")) % len(GENERIC_HOOKS)
        hook = GENERIC_HOOKS[idx]
    hook = hook[:46]

    title = original or f"{streamer} - {game}"
    title = f"{title[:70]} | {streamer} #shorts"[:100]

    description = (
        f"{original}\n\n"
        f"{streamer} playing {game}\n"
        f"Original clip: {clip['url']}\n"
        f"Watch {streamer} live: https://twitch.tv/{streamer}"
    )
    tags = [t for t in [game, streamer, "twitch", "twitch clips", "gaming",
                        "shorts", "funny moments"] if t]
    return {"hook": hook, "title": title, "description": description,
            "tags": tags, "credit": f"@{streamer}"}


# --------------------------------------------------------------------------- #
# fetch + render
# --------------------------------------------------------------------------- #
def download_clip(url, out_path):
    run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet",
         "-f", "mp4/best", "-o", out_path, url])
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 10_000:
        raise RuntimeError(f"download produced nothing usable for {url}")


def _drawtext(textfile, size, y, plate=False):
    """One drawtext filter. plate=True adds a dark slab behind the text, which
    is what keeps it readable when it sits directly on top of footage."""
    box = ("box=1:boxcolor=black@0.5:boxborderw=16:" if plate else "")
    return (f"drawtext=fontfile='{FONT}':textfile='{textfile}':"
            f"fontcolor=white:fontsize={size}:borderw=6:bordercolor=black:"
            f"{box}line_spacing=0:x=(w-text_w)/2:y={y}")


def _blur_chain(hook_size):
    """Clip letterboxed into a blurred copy of itself. Shows the whole 16:9
    frame, but the video only occupies ~36% of the screen height."""
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H},boxblur=40:2[bgb];"
        f"[fg]scale={FG_W}:{FG_H}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]{_drawtext('hook.txt', hook_size, f'{HOOK_Y}-text_h')},"
        f"{_drawtext('credit.txt', CREDIT_SIZE, CREDIT_Y)}[v]"
    )


def _fill_chain(hook_size):
    """Clip scaled to cover the whole 9:16 frame, centre-cropped.

    Fills the screen, which is what reads as 'a real Short' rather than a
    reposted strip. The cost is real: a 16:9 source keeps only its middle ~32%
    horizontally, so action at the frame edges is gone. Text sits on the
    footage, hence the plate behind it.
    """
    return (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H},"
        f"{_drawtext('hook.txt', hook_size, '200', plate=True)},"
        f"{_drawtext('credit.txt', CREDIT_SIZE, H - 260, plate=True)}[v]"
    )


def fit_hook(text, max_lines=2):
    """Trim and size a hook so it lands in at most two lines of large type.

    A hook is a glance, not a sentence. Three lines of shouting reads as a
    wall of text and buries the footage, so anything long is cut at a word
    boundary and the type size is picked to fit what remains. Returns
    (wrapped_text, fontsize).
    """
    words = text.upper().split()
    trimmed = ""
    for w in words:
        candidate = f"{trimmed} {w}".strip()
        if len(candidate) > HOOK_MAX_CHARS:
            break
        trimmed = candidate
    trimmed = trimmed or text.upper()[:HOOK_MAX_CHARS]

    # Trimming mid-phrase leaves danglers like "...HIT THE". Drop them.
    parts = trimmed.split()
    while len(parts) > 1 and parts[-1].strip(",.-") in HOOK_TAIL_STOPWORDS:
        parts.pop()
    trimmed = " ".join(parts).strip(" ,.-")

    for size in HOOK_SIZES:
        # Arial Bold averages ~0.58em per character; 1000px is the usable width.
        per_line = max(8, int(1000 / (size * 0.58)))
        wrapped = textwrap.fill(trimmed, per_line)
        if wrapped.count("\n") + 1 <= max_lines:
            return wrapped, size
    per_line = max(8, int(1000 / (HOOK_SIZES[-1] * 0.58)))
    return textwrap.fill(trimmed, per_line), HOOK_SIZES[-1]


def render(src, run_dir, copy, out_name="short.mp4", style=None):
    """One ffmpeg pass to a finished 1080x1920 Short.

    Text comes from files rather than inline strings so drawtext's escaping
    never has to survive a shell round-trip, and ffmpeg runs with cwd set to
    the run folder so those paths stay relative (no Windows drive colons).
    """
    style = style or STYLE
    hook_text, hook_size = fit_hook(copy["hook"])
    # newline="\n": on Windows, text mode would write CRLF and drawtext renders
    # the stray CR as a glyph, opening a phantom gap between wrapped lines.
    with open(os.path.join(run_dir, "hook.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(hook_text)
    with open(os.path.join(run_dir, "credit.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(copy["credit"])

    vf = (_fill_chain(hook_size) if style == "fill"
          else _blur_chain(hook_size))
    run(["ffmpeg", "-y", "-i", os.path.basename(src),
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-r", str(FPS), "-c:v", "libx264", "-preset", PRESET, "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", out_name], cwd=run_dir)
    return os.path.join(run_dir, out_name)


# --------------------------------------------------------------------------- #
# upload + bookkeeping
# --------------------------------------------------------------------------- #
def upload(video_path, copy, privacy, publish_at=None):
    cmd = [sys.executable, os.path.join(PROJECT_ROOT, "youtube_upload.py"),
           video_path,
           "--title", copy["title"],
           "--description", copy["description"],
           "--tags", ",".join(copy["tags"]),
           "--privacy", privacy,
           "--category", "20"]  # 20 = Gaming
    if publish_at:
        cmd += ["--publish-at", publish_at]
    p = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        low = out.lower()
        if "uploadlimitexceeded" in low or "exceeded the number of videos" in low:
            raise RuntimeError(
                "DAILY UPLOAD LIMIT reached (YouTube caps uploads per 24h, "
                "stricter for new channels). The clip stays unspent and a later "
                "run retries.")
        if any(k in low for k in ("invalid_grant", "refresherror", "expired",
                                  "insufficient", "token has been expired",
                                  "re-run: python youtube_authorize")):
            with open(TOKEN_EXPIRED_FLAG, "w", encoding="utf-8") as f:
                f.write(dt.datetime.now().isoformat() + "\n")
                f.write("YouTube token expired. Run: python youtube_authorize.py\n")
            raise RuntimeError(f"TOKEN EXPIRED — run youtube_authorize.py.\n{out[-1500:]}")
        raise RuntimeError(f"upload failed:\n{out[-1500:]}")
    clear_flag(TOKEN_EXPIRED_FLAG)
    url = ""
    for line in out.splitlines():
        if "youtu.be/" in line or "youtube.com/watch" in line:
            url = line.strip().split()[-1]
    return url or "(uploaded; url not parsed)"


def append_history(clip_id, url, privacy):
    new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "clip_id", "privacy", "url"])
        w.writerow([dt.datetime.now(TZ).date().isoformat(), clip_id, privacy, url])


def slots_filled_today():
    if not os.path.exists(HISTORY_CSV):
        return 0
    today = dt.datetime.now(TZ).date().isoformat()
    n = 0
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and row[0] == today and row[2] in ("public", "scheduled"):
                n += 1
    return n


def slot_publish_time(idx):
    h, m = PUBLISH_SLOTS[idx]
    today = dt.datetime.now(TZ).date()
    when = dt.datetime(today.year, today.month, today.day, h, m, tzinfo=TZ)
    if when <= dt.datetime.now(TZ) + dt.timedelta(minutes=2):
        return None
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# produce one short
# --------------------------------------------------------------------------- #
def produce_one(candidates, state, args, publish_at):
    """Take the best unspent candidate and ship it.

    A clip that fails to download or render is marked spent and we drop to the
    next one — the pool is hundreds deep, so one bad clip never costs a slot.
    """
    while candidates:
        clip = candidates.pop(0)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = os.path.join(RUNS_DIR, f"{stamp}-{_slug(clip['broadcaster_name'])}")
        os.makedirs(run_dir, exist_ok=True)
        log(f"Clip: {clip['broadcaster_name']} / {clip.get('game_name')} "
            f"({int(clip['view_count']):,} views, {float(clip['duration']):.0f}s)")
        try:
            src = os.path.join(run_dir, "source.mp4")
            download_clip(clip["url"], src)
            copy = write_copy(clip)
            log(f"  hook: {copy['hook']}")
            video = render(src, run_dir, copy, style=args.style)
        except Exception as e:  # noqa: BLE001
            log(f"  clip failed ({e}); marking spent and taking the next one.")
            state.setdefault("seen", []).append(clip["id"])
            save_json(STATE_FILE, state)
            continue

        state.setdefault("seen", []).append(clip["id"])
        save_json(STATE_FILE, state)

        if args.no_upload:
            log(f"Built (upload skipped): {video}")
            return video

        privacy = "private" if publish_at else args.privacy
        url = upload(video, copy, privacy, publish_at)
        append_history(clip["id"], url, "scheduled" if publish_at else privacy)
        log(f"Posted: {url}")
        return video

    raise RuntimeError("no usable candidates left in the pool")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--privacy", default="public",
                    choices=["public", "unlisted", "private"])
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--style", default=STYLE, choices=["fill", "blur"],
                    help="fill = clip covers the whole frame (centre-cropped); "
                         "blur = whole 16:9 frame letterboxed into a blurred copy")
    ap.add_argument("--fill-day", action="store_true",
                    help="build every remaining slot today and hand them to "
                         "YouTube's scheduler")
    args = ap.parse_args()

    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not acquire_lock():
        return
    try:
        cleanup_runs()
        state = load_json(STATE_FILE, {"seen": []})
        log("Discovering clips...")
        candidates = twitch.discover(seen=state.get("seen", []))
        log(f"{len(candidates)} candidates.")
        if not candidates:
            log("Nothing new passed the filter — a later run will retry.")
            return

        if not args.fill_day:
            produce_one(candidates, state, args, publish_at=None)
            return

        total = len(PUBLISH_SLOTS)
        done = slots_filled_today()
        if done >= total:
            log(f"All {total} slots already scheduled today; nothing to do.")
            return
        log(f"Filling {total - done} of {total} slots...")
        for idx in range(done, total):
            publish_at = slot_publish_time(idx)
            log(f"--- slot {idx + 1}/{total} -> "
                f"{publish_at or 'now (slot already passed)'}")
            try:
                produce_one(candidates, state, args, publish_at=publish_at)
            except Exception as e:  # noqa: BLE001
                log(f"slot {idx + 1} failed ({e}); a catch-up run will retry.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
