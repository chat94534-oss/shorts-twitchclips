#!/usr/bin/env python3
"""Write the burned-in hook and the YouTube title with Gemini.

The Twitch title is what the streamer typed for their own chat, so it names
the moment without selling it ("Nat totals the cost of Ubers"). A Short lives
or dies on whether the first frame stops a thumb, so the hook is worth one
model call.

Free tier, one call per video, ~11 a day. Set GEMINI_API_KEY (env or .env).
Model is a flash-lite on purpose. The free tier allows 20 requests a DAY
per model, which 11 uploads fit inside and a bulk backfill does not;
each model name carries its own daily bucket if one runs dry.
Without a key — or on any error, timeout, or refusal — this returns None and
make_clip falls back to the clip's own title.

    python hooks.py            # self-check, plus a live call if a key is set
"""
import json
import os
import time
import urllib.error
import urllib.request

import twitch  # for _load_env

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
TIMEOUT = 25
RETRY_WAIT = 20
HOOK_LIMIT = 30
TITLE_LIMIT = 70

PROMPT = """You write copy for a YouTube Shorts channel that reposts the top \
Twitch clips.

Clip:
  streamer: {streamer}
  game: {game}
  streamer's own title: {title}

Return JSON with two fields:

"hook" - text burned onto the first seconds of the video. HARD LIMIT {hook_limit} \
characters. Make a viewer stop scrolling: tease the payoff, never state it. No \
emoji, no hashtags, no quotes, no streamer name, no ending punctuation. Plain \
words a 13-year-old says out loud.

"title" - the YouTube title, under {title_limit} characters. Same tease, but it \
may name the streamer or game since people search those. No emoji, no \
hashtags, no clickbait that the clip does not deliver.

Both must be honest about what the clip shows."""


def _clean(text, limit):
    text = " ".join(str(text or "").split())
    for ch in '"“”\'`*#':
        text = text.replace(ch, "")
    return text.strip(" .!,-:").strip()[:limit]


def write(streamer, game, title, key=None):
    """{"hook", "title"} from Gemini, or None if anything at all goes wrong."""
    twitch._load_env()
    if key is None:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None

    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT.format(
            streamer=streamer, game=game, title=title,
            hook_limit=HOOK_LIMIT, title_limit=TITLE_LIMIT)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 1.0,
        },
    }).encode()

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    })
    try:
        for attempt in (0, 1):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                # 429 is the per-minute cap, not the daily one. One wait clears it.
                if e.code != 429 or attempt:
                    raise
                time.sleep(RETRY_WAIT)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(text)
        hook = _clean(out.get("hook"), HOOK_LIMIT)
        headline = _clean(out.get("title"), TITLE_LIMIT)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            KeyError, IndexError, ValueError, TypeError) as e:
        # A blocked or malformed response costs the nicer hook, not the video.
        print(f"  gemini: {type(e).__name__}: {e}", flush=True)
        return None

    # A two-word hook is worse than the streamer's own title.
    if len(hook) < 8 or len(headline) < 8:
        return None
    return {"hook": hook, "title": headline}


def demo():
    assert _clean('  "he DIDN\'T see it"  ', 30) == "he DIDNT see it"
    assert len(_clean("x" * 80, HOOK_LIMIT)) == HOOK_LIMIT
    assert _clean(None, 30) == ""
    assert write("s", "g", "t", key="") is None, "no key must fall back"
    print("hooks.py self-check: OK")


if __name__ == "__main__":
    demo()
    got = write("T10Nat", "Just Chatting",
                "Nat totals the cost of Ubers in USA for 2.5 weeks")
    print(got if got else "no GEMINI_API_KEY set — fallback path in use")
