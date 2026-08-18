# twitch-clip-autopost

Unattended YouTube Shorts pipeline built from Twitch's top clips. GitHub
Actions finds the clips, cuts them to 9:16, and posts them — nothing runs on a
local machine.

Twitch Helix (top games → their top clips of the last 7 days) → filter and rank
by views → `yt-dlp` → hook and title derived from the clip's own metadata →
ffmpeg (blurred 9:16 backdrop, clip centered, burned hook + streamer credit) →
YouTube upload. The workflow commits spent clip ids back to the repo after each
run.

- **Schedule (EDT):** 12 PM, 3 PM, 6 PM, 9 PM — see `.github/workflows/post.yml`.
  Crons are UTC: **in November (DST ends) bump each cron hour +1** or posts shift
  an hour early.
- **Secrets (repo settings):** `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`,
  `CLIENT_SECRET_JSON`, `TOKEN_JSON`.
- **Manual run:** Actions → post → Run workflow (choose privacy for tests).

## Local runs

Credentials come from `.env` (gitignored):

```
TWITCH_CLIENT_ID=xxxx
TWITCH_CLIENT_SECRET=xxxx
```

```
python twitch.py                 # what would it pick right now?
python test_render.py            # render check, no network needed
python make_clip.py --no-upload  # build one real Short, upload nothing
python make_clip.py --fill-day   # the CI path
```

## Keeping private data out

This repo is public. `check_private.py` matches credential and personal-data
*shapes* (emails, OAuth secrets, local user paths) rather than a list of real
values, since a deny-list holding the real values would itself be the leak.

It runs two places: as a CI step before anything builds, and as a pre-commit
hook. The hook lives in `.git/hooks/` and is not cloned, so install it once per
machine:

```
printf '#!/bin/sh
exec python check_private.py --staged
' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

Note that scrubbing a file does not scrub git history — a value that was ever
pushed stays reachable until the history itself is rewritten.

## Why it is built this way

- **Twitch, not YouTube, as the source.** Clips are already 10–60s and already
  curated by view count, which deletes the "find the good moment" problem
  entirely. Downloads are ~5–20 MB. And `yt-dlp` against YouTube from a
  datacenter IP hits bot checks; Twitch has no such wall.
- **No global clips endpoint exists.** Helix `/clips` requires a `game_id` or a
  `broadcaster_id`, so `twitch.py` pulls the top games first and merges each
  one's clips.
- **The clip is never cropped.** It is scaled to fit a fixed 1242×698 box, so an
  odd-shaped source comes out smaller rather than overflowing into the hook and
  credit bands. Center-cropping to full-frame would throw away the left and
  right thirds, where a lot of Twitch payoffs live.
- **One run per day, YouTube schedules the rest.** GitHub cron drops and delays
  runs, so a single run builds every remaining slot and hands each video to
  YouTube's `publishAt`. Two later crons only catch up if the first was dropped.
- **A bad clip never costs a slot.** Download or render failure marks the clip
  spent and moves to the next candidate; the pool is hundreds deep.

## Not built yet

- **Burned captions.** `faster-whisper` would add real CI minutes, and top-clip
  gameplay is mostly action rather than talking. Worth adding if talking-heavy
  clips start underperforming.
- **DST handling.** Same manual cron bump as the other channels.
