#!/usr/bin/env python3
"""Twitch clip -> YouTube Short, fully automated.

    twitch.discover()  ->  yt-dlp  ->  ffmpeg 9:16  ->  youtube_upload.py

Runs in batches: each scheduled run fills up to BATCH_SIZE of the day's unfilled
slots and hands each video to YouTube's scheduler via publishAt. Six runs a day
at 3 each covers 11 slots with room to spare, so a run that fails is simply
absorbed by the next one — the gap is measured from videos actually posted, not
from whether a workflow run went green. State is committed back by the workflow.

    python make_clip.py --fill-day          # the CI path (3 slots)
    python make_clip.py --fill-day --max 0  # everything still missing today
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

import facecam
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
# Eleven a day: three morning, three afternoon, five at night (night is when
# Shorts watch time peaks). The API is not the limit here — the project allows
# 100 uploads/day and an upload costs 1 query of 10,000, so this is a content
# decision, not a technical one. What it does cost is per-video strength: at 11
# a day we reach down to the 11th-best clip of the week, and the view-count
# curve is steep (top clip ~92k, 11th under 20k).
PUBLISH_SLOTS = [
    (7, 0), (9, 0), (11, 0),               # morning
    (13, 0), (15, 0), (17, 0),             # afternoon
    (19, 0), (20, 0), (21, 0), (22, 0), (23, 0),  # night
]
# Slots built per run. Eleven uploads in one burst is the shape YouTube's spam
# heuristics look at, and a single long run is a single point of failure. Six
# scheduled runs a day at 3 each gives 18 of capacity for 11 slots, so a failed
# run is absorbed by the next one rather than costing the day.
BATCH_SIZE = 3

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
CREDIT_TOP_Y = 110                       # above the hook; YouTube's UI covers the bottom
HOOK_HOLD = 3.0                          # seconds the hook and credit stay up
HOOK_FADE = 0.5                          # seconds to fade them out
# Split layout: facecam on top, gameplay below. 35/65 puts the face big enough
# to read at thumbnail size while leaving the game the majority of the frame.
# Up to four cams share that band: two or three in a row, four as a 2x2 grid.
# Past four the tiles stop being big enough to read a face in, so those clips
# get normal framing instead.
MAX_CAMS = 4
SPLIT_TOP_H = int(H * 0.35) // 2 * 2      # 672, kept even for libx264
SPLIT_BOTTOM_H = H - SPLIT_TOP_H          # 1248
STYLE = "auto"        # "auto" | "split" | "fill" (full-frame) | "blur" (letterboxed)

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


def _drawtext(textfile, size, y):
    """One drawtext filter, held for HOLD seconds then faded out.

    The hook earns its keep in the first couple of seconds; after that it is
    just covering the footage. Both hook and credit fade together.

    No box behind the text: `boxcolor` is a fixed colour, not an expression, so
    a plate cannot fade with the glyphs and would be left hanging over the
    video. A heavy border plus a drop shadow does the same readability job and
    fades with everything else, since alpha applies to the whole glyph render.

    Commas inside the alpha expression are escaped — the filtergraph parser
    treats a bare comma as the end of this filter.
    """
    fade_end = HOOK_HOLD + HOOK_FADE
    alpha = (f"if(lt(t\\,{HOOK_HOLD})\\,1\\,"
             f"if(lt(t\\,{fade_end})\\,({fade_end}-t)/{HOOK_FADE}\\,0))")
    return (f"drawtext=fontfile='{FONT}':textfile='{textfile}':"
            f"fontcolor=white:fontsize={size}:borderw=10:bordercolor=black:"
            f"shadowcolor=black@0.6:shadowx=0:shadowy=4:"
            f"alpha='{alpha}':line_spacing=0:x=(w-text_w)/2:y={y}")


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
        f"{_drawtext('credit.txt', CREDIT_SIZE, CREDIT_TOP_Y)}[v]"
    )


def _split_chain(hook_size, cams):
    """Facecam(s) stacked on top of the gameplay — the standard clip look.

    cams is a list of (x, y, w, h) boxes in source pixels, left to right. One
    cam fills the top band; a co-stream's two or three share it as a row and
    four as a 2x2 grid, so nobody gets cropped out of their own clip. Each tile is cover-cropped,
    which keeps the middle of a webcam — where a face sits — and discards the
    edges of the room.

    The gameplay below is centre-cropped from a shorter box than full-frame
    fill, so it keeps ~49% of the source width instead of ~32%.
    """
    n = len(cams)
    # Up to three sit in one row. Four go 2x2: a single row of four would be
    # 270px tiles, too narrow to keep a face inside a cover-crop.
    cols = n if n <= 3 else 2
    rows = -(-n // cols)
    tile_w = (W // cols) // 2 * 2            # even, for libx264
    tile_h = (SPLIT_TOP_H // rows) // 2 * 2
    labels = "".join(f"[c{i}]" for i in range(n))

    parts = [f"[0:v]split={n + 1}{labels}[game];"]
    for i, (cx, cy, cw, ch) in enumerate(cams):
        # Last tile in a row absorbs rounding so the row is exactly W wide;
        # the bottom row absorbs it likewise so the band is exactly SPLIT_TOP_H.
        last_col = (i % cols == cols - 1) or i == n - 1
        w = W - tile_w * (cols - 1) if last_col and cols > 1 else tile_w
        h = (SPLIT_TOP_H - tile_h * (rows - 1)
             if rows > 1 and i // cols == rows - 1 else tile_h)
        parts.append(
            f"[c{i}]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={w}:{h}:force_original_aspect_ratio=increase:"
            f"flags=lanczos,crop={w}:{h}[t{i}];")

    if n == 1:
        parts.append("[t0]null[camrow];")
    elif rows == 1:
        parts.append("".join(f"[t{i}]" for i in range(n))
                     + f"hstack=inputs={n}[camrow];")
    else:
        for r in range(rows):
            members = [i for i in range(n) if i // cols == r]
            parts.append("".join(f"[t{i}]" for i in members)
                         + f"hstack=inputs={len(members)}[r{r}];")
        parts.append("".join(f"[r{r}]" for r in range(rows))
                     + f"vstack=inputs={rows}[camrow];")
    parts.append(
        f"[game]scale={W}:{SPLIT_BOTTOM_H}:force_original_aspect_ratio=increase:"
        f"flags=lanczos,crop={W}:{SPLIT_BOTTOM_H}[gamef];"
        f"[camrow][gamef]vstack=2[base];"
        f"[base]{_drawtext('credit.txt', CREDIT_SIZE, CREDIT_TOP_Y)},"
        f"{_drawtext('hook.txt', hook_size, SPLIT_TOP_H + 40)}[v]")
    return "".join(parts)


def _fill_chain(hook_size):
    """Clip scaled to cover the whole 9:16 frame, centre-cropped.

    Fills the screen, which is what reads as 'a real Short' rather than a
    reposted strip. The cost is real: a 16:9 source keeps only its middle ~32%
    horizontally, so action at the frame edges is gone.
    """
    return (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H},"
        f"{_drawtext('credit.txt', CREDIT_SIZE, CREDIT_TOP_Y)},"
        f"{_drawtext('hook.txt', hook_size, '210')}[v]"
    )


# Categories where the PERSON is the content, not a game: reactions, IRL,
# podcasts, desktops. A 9:16 crop of 16:9 footage keeps only the middle ~32% of
# the width, and facecams live in the corners — so filling the frame here
# throws away the thing the clip is about. These keep the letterbox, which
# shows the whole frame at the cost of height.
#
# Everything else is gameplay, where the game is the content and the facecam is
# a small corner overlay worth losing to fill the screen.
FACE_IS_THE_CONTENT = {
    # camera on a person
    "Just Chatting", "IRL", "Always On", "Special Events", "Sports",
    "Talk Shows & Podcasts", "Travel & Outdoors", "Music", "ASMR", "Art",
    "Politics", "Food & Drink", "Fitness & Health",
    "Animals, Aquariums, and Zoos", "Makers & Crafting", "Beauty & Body Art",
    "DJs", "Pools, Hot Tubs, and Beaches",
    # a desktop, a canvas, a dashboard — full width matters, centre crop lands
    # on chat or on nothing
    "Watch Parties", "Science & Technology", "Software and Game Development",
    "Games + Demos", "Crypto",
}
# Gambling categories never reach here — twitch.BLOCKED_CATEGORIES drops them
# during discovery.


def style_for(clip):
    """Pick framing per clip: fill for gameplay, blur when the person matters."""
    if STYLE != "auto":
        return STYLE
    return "blur" if clip.get("game_name") in FACE_IS_THE_CONTENT else "fill"


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
        lines = wrapped.split("\n")
        if len(lines) > max_lines:
            continue
        # A one- or two-character last line ("...HARD / R") reads as a typo.
        if len(lines) > 1 and len(lines[-1]) <= 2:
            continue
        return wrapped, size
    per_line = max(8, int(1000 / (HOOK_SIZES[-1] * 0.58)))
    return textwrap.fill(trimmed, per_line), HOOK_SIZES[-1]


def render(src, run_dir, copy, out_name="short.mp4", style=None, cams=None):
    """One ffmpeg pass to a finished 1080x1920 Short.

    Text comes from files rather than inline strings so drawtext's escaping
    never has to survive a shell round-trip, and ffmpeg runs with cwd set to
    the run folder so those paths stay relative (no Windows drive colons).
    """
    style = style or ("fill" if STYLE == "auto" else STYLE)
    hook_text, hook_size = fit_hook(copy["hook"])
    # newline="\n": on Windows, text mode would write CRLF and drawtext renders
    # the stray CR as a glyph, opening a phantom gap between wrapped lines.
    with open(os.path.join(run_dir, "hook.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(hook_text)
    with open(os.path.join(run_dir, "credit.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(copy["credit"])

    if style == "split" and cams:
        vf = _split_chain(hook_size, cams)
    elif style == "fill":
        vf = _fill_chain(hook_size)
    else:
        vf = _blur_chain(hook_size)
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


def clips_ever_posted():
    """Every clip id in the upload log — the second line of defence on repeats.

    state.json is the primary record, but it lives in one file that a failed
    persist can lose. That is exactly how the same xQc clip went up twice. The
    history log is written on the same commit, so seeding from both means a
    clip has to vanish from two places before it can be posted again.
    """
    if not os.path.exists(HISTORY_CSV):
        return set()
    ids = set()
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[1] and row[1] != "clip_id":
                ids.add(row[1])
    return ids


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
            style = args.style or style_for(clip)
            cams = None
            if style == "fill":
                # Only gameplay: chat and IRL clips are letterboxed already, so
                # the cam is on screen and there is nothing to look for.
                found = facecam.detect_all(src)
                if 1 <= len(found) <= MAX_CAMS:
                    cams, style = found, "split"
                    log(f"  {len(found)} facecam(s) {found} -> split layout")
                elif len(found) > MAX_CAMS:
                    log(f"  {len(found)} facecams — too many to tile, "
                        "using full-frame")
                else:
                    log("  no facecam found -> full-frame")
            video = render(src, run_dir, copy, style=style, cams=cams)
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
    ap.add_argument("--style", default=None, choices=["split", "fill", "blur"],
                    help="force a framing; default picks per clip — fill "
                         "(centre-cropped, fills the frame) for gameplay, blur "
                         "(letterboxed) for chat/IRL categories")
    ap.add_argument("--fill-day", action="store_true",
                    help="fill today's unfilled slots and hand them to "
                         "YouTube's scheduler")
    ap.add_argument("--streamer", metavar="LOGIN",
                    help="pull from one streamer's clips instead of the top "
                         "games pool (e.g. --streamer ishowspeed)")
    ap.add_argument("--max", type=int, default=BATCH_SIZE, metavar="N",
                    help=f"most slots to fill in one run (default {BATCH_SIZE}); "
                         "0 means no limit")
    args = ap.parse_args()

    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not acquire_lock():
        return
    try:
        cleanup_runs()
        state = load_json(STATE_FILE, {"seen": []})
        # Seed from the upload log as well as state.json, so losing one file
        # cannot resurrect an already-posted clip.
        spent = set(state.get("seen", [])) | clips_ever_posted()
        if args.streamer:
            log(f"Discovering clips from {args.streamer}...")
            candidates = twitch.discover_streamer(args.streamer, seen=spent)
        else:
            log("Discovering clips...")
            candidates = twitch.discover(seen=spent)
        log(f"{len(candidates)} candidates ({len(spent)} clips already spent).")
        if not candidates:
            log("Nothing new passed the filter — a later run will retry.")
            return

        if not args.fill_day:
            produce_one(candidates, state, args, publish_at=None)
            return

        total = len(PUBLISH_SLOTS)
        done = slots_filled_today()
        if done >= total:
            log(f"All {total} slots already filled today; nothing to do.")
            return

        # Whatever is missing gets picked up here, whether it was never built
        # or a previous run died mid-slot — the gap is measured from videos
        # actually posted, not from whether a workflow run went green.
        missing = total - done
        want = missing if args.max <= 0 else min(args.max, missing)
        log(f"{done}/{total} slots filled today; building {want} now"
            f"{' (catching up)' if done and missing > args.max > 0 else ''}.")
        for idx in range(done, done + want):
            publish_at = slot_publish_time(idx)
            log(f"--- slot {idx + 1}/{total} -> "
                f"{publish_at or 'now (slot already passed)'}")
            try:
                produce_one(candidates, state, args, publish_at=publish_at)
            except Exception as e:  # noqa: BLE001
                log(f"slot {idx + 1} failed ({e}); a later batch will retry it.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
