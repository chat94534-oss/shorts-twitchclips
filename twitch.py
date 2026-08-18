#!/usr/bin/env python3
"""Twitch clip discovery — the top clips of the last day across the hottest games.

Helix has no "all of Twitch" clips endpoint: /helix/clips demands a game_id or a
broadcaster_id. So we ask for the currently hottest games, pull each one's top
clips for the window, and merge the pools. ~21 requests, all free.

Credentials come from TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET (env, or a local
.env file for running on your own machine).

Run standalone to see exactly what the pipeline would pick next:
    python twitch.py
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")

# Candidate filter. Shorts hard-caps at 3 minutes, but clips over ~60s bleed
# retention badly, and under ~8s there is no story to tell.
MIN_DURATION = 8.0
MAX_DURATION = 60.0
MIN_VIEWS = 500
LANGUAGE = "en"

TOP_GAMES = 40      # how many hot games to sample
CLIPS_PER_GAME = 30  # Helix returns these already sorted by view count
# A week, not a day. Clips need time to accumulate views, so a 24h window's
# best clip runs ~2k views while a 7-day window's runs ~90k — and a Shorts
# viewer has no idea (or interest in) how old a Twitch clip is. Measured
# pools: 24h/20 games = 29 candidates, 168h/40 games = 356.
WINDOW_HOURS = 168


def _load_env():
    """Read .env into os.environ (CI sets real env vars and skips this)."""
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials():
    _load_env()
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise SystemExit(
            "Missing TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET.\n"
            f"Set them as environment variables, or put them in {ENV_FILE}:\n"
            "    TWITCH_CLIENT_ID=xxxx\n"
            "    TWITCH_CLIENT_SECRET=xxxx"
        )
    return cid, secret


def _request(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Twitch API {e.code} on {url}\n{body}") from e


def app_token(cid, secret):
    """Client-credentials flow: no user login, no redirect, valid ~60 days.

    We fetch a fresh one each run rather than storing it — it is one request.
    """
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": secret,
        "grant_type": "client_credentials",
    }).encode()
    return _request("https://id.twitch.tv/oauth2/token", data=body)["access_token"]


def _helix(path, params, cid, token):
    url = f"https://api.twitch.tv/helix/{path}?{urllib.parse.urlencode(params)}"
    return _request(url, headers={
        "Client-ID": cid,
        "Authorization": f"Bearer {token}",
    })


def top_games(cid, token, n=TOP_GAMES):
    data = _helix("games/top", {"first": min(n, 100)}, cid, token)["data"]
    return [(g["id"], g["name"]) for g in data]


def clips_for_game(cid, token, game_id, started_at, first=CLIPS_PER_GAME):
    return _helix("clips", {
        "game_id": game_id,
        "started_at": started_at,
        "first": min(first, 100),
    }, cid, token)["data"]


def keep(clip, seen):
    """Pure filter — the one piece worth testing without touching the network."""
    if clip.get("id") in seen:
        return False
    if clip.get("language") != LANGUAGE:
        return False
    dur = float(clip.get("duration") or 0)
    if not (MIN_DURATION <= dur <= MAX_DURATION):
        return False
    return int(clip.get("view_count") or 0) >= MIN_VIEWS


def discover(seen=(), hours=WINDOW_HOURS, games=TOP_GAMES):
    """Return candidate clips, most-viewed first, already filtered and deduped."""
    cid, secret = credentials()
    token = app_token(cid, secret)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    started_at = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    seen = set(seen)
    pool, by_id = [], set()
    for game_id, game_name in top_games(cid, token, games):
        try:
            raw = clips_for_game(cid, token, game_id, started_at)
        except RuntimeError as e:
            print(f"  skip game {game_name}: {e}", file=sys.stderr)
            continue
        for c in raw:
            if c["id"] in by_id or not keep(c, seen):
                continue
            by_id.add(c["id"])
            c["game_name"] = game_name
            pool.append(c)

    pool.sort(key=lambda c: int(c.get("view_count") or 0), reverse=True)
    return pool


def demo():
    """Assert-based self-check on the filter. Runs offline."""
    base = {"id": "abc", "language": "en", "duration": 30.0, "view_count": 5000}
    assert keep(base, set())
    assert not keep(base, {"abc"}), "seen clips must be skipped"
    assert not keep({**base, "language": "de"}, set()), "non-en must be skipped"
    assert not keep({**base, "duration": 4.0}, set()), "too short must be skipped"
    assert not keep({**base, "duration": 120.0}, set()), "too long must be skipped"
    assert not keep({**base, "view_count": 10}, set()), "low views must be skipped"
    assert not keep({**base, "duration": None, "view_count": None}, set())
    print("twitch.py self-check: OK")


def main():
    if "--demo" in sys.argv:
        return demo()
    demo()
    clips = discover()
    print(f"\n{len(clips)} candidates in the last {WINDOW_HOURS}h. Top 10:\n")
    for i, c in enumerate(clips[:10], 1):
        print(f"{i:2}. {int(c['view_count']):>7,} views  {float(c['duration']):>5.1f}s  "
              f"{c['broadcaster_name']} / {c['game_name']}")
        print(f"    {c['title'][:70]}")
        print(f"    {c['url']}")


if __name__ == "__main__":
    main()
