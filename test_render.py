#!/usr/bin/env python3
"""Render check: synthesize a 16:9 clip, run it through render(), assert 9:16 out.

No network, no Twitch credentials, no YouTube. This is the smallest thing that
fails if the ffmpeg filter chain, the drawtext escaping, or the font path break.

    python test_render.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import make_clip


def probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not on PATH — install it first.")

    work = tempfile.mkdtemp(prefix="clipcheck-")
    try:
        src = os.path.join(work, "source.mp4")
        # A 16:9 source with audio, standing in for a real Twitch clip.
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", src], check=True)

        copy = {"hook": "he didnt see it coming",
                "credit": "@teststreamer"}
        out = make_clip.render(src, work, copy)

        assert os.path.exists(out), "render produced no file"
        assert os.path.getsize(out) > 10_000, "render output suspiciously small"
        w, h = probe_size(out)
        assert (w, h) == (make_clip.W, make_clip.H), f"expected 1080x1920, got {w}x{h}"

        # A vertical source must survive too — it should shrink inside the box
        # rather than overflow into the hook/credit bands.
        tall = os.path.join(work, "tall.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=480x854:rate=30:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             tall], check=True)
        out2 = make_clip.render(tall, work, copy, out_name="tall_short.mp4")
        assert probe_size(out2) == (make_clip.W, make_clip.H)

        # The split chain builds a different filtergraph per cam count, and a
        # tile-width rounding error only shows up as an ffmpeg failure, so walk
        # every supported count against a 16:9 source.
        boxes = [(40, 20, 400, 225), (840, 20, 400, 225), (440, 20, 400, 225)]
        for n in range(1, make_clip.MAX_CAMS + 1):
            out_n = make_clip.render(src, work, copy, out_name=f"split{n}.mp4",
                                     style="split", cams=boxes[:n])
            assert probe_size(out_n) == (make_clip.W, make_clip.H), \
                f"{n}-cam split came out the wrong size"
        print(f"split check: OK (1..{make_clip.MAX_CAMS} cams)")

        print(f"render check: OK  ({w}x{h}, {os.path.getsize(out):,} bytes)")
        print(f"sample kept at: {out}")
        shutil.copy(out, os.path.join(make_clip.HERE, "sample_short.mp4"))
        print(f"copied to: {os.path.join(make_clip.HERE, 'sample_short.mp4')}")
    finally:
        pass  # leave the temp dir so the sample can be eyeballed


if __name__ == "__main__":
    main()
