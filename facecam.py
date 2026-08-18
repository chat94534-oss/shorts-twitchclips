#!/usr/bin/env python3
"""Locate a streamer's webcam overlay inside a 16:9 gameplay clip.

Two signals, because neither is enough alone:

1. A Haar face detector says WHICH CORNER the streamer is in. It is reliable
   about position and useless about size — on a test clip it returned a
   184x103 box for a cam that was roughly 330x330.

2. The cam's BORDER is then found by persistence. An overlay is pinned to the
   same pixels in every frame, while gameplay edges move constantly. Summing
   edge maps across sampled frames makes the static rectangle stand out and
   washes the game out, so the cam's inner edges show up as the strongest
   straight lines in that corner.

Only worth running on gameplay: chat and IRL clips are letterboxed anyway, so
the cam is already on screen and there is nothing to find.

    python facecam.py CLIP.mp4              # print the box
    python facecam.py CLIP.mp4 --debug OUT.png   # draw it

ponytail: the fallback is the point. Anything uncertain — no face, no clean
border, implausible geometry — returns None and the clip gets normal framing.
A wrong crop ships a broken video; no crop just ships the old look.
"""
import sys

# Imported lazily inside detect(): the split layout is an enhancement, and a
# missing or broken OpenCV should cost the nicer framing, not the whole run.
cv2 = None
np = None

SAMPLES = 12           # frames to sample across the clip
MIN_HITS = 3           # face must recur this often to count as a cam
CLUSTER_TOL = 0.06     # fraction of frame width for grouping detections
MIN_FACE = 0.04        # face size bounds, as a fraction of frame width
MAX_FACE = 0.42

# Plausible webcam overlay geometry, as fractions of the frame.
CAM_W_RANGE = (0.10, 0.42)
CAM_H_RANGE = (0.12, 0.60)
EDGE_MARGIN = 0.02     # keep the border search off the very frame edge


def _cascade():
    path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    c = cv2.CascadeClassifier(path)
    if c.empty():
        raise RuntimeError(f"could not load cascade at {path}")
    return c


def _sample_frames(path, n=SAMPLES):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames = []
    try:
        if total <= 0:
            return []
        # Skip the first and last tenth: clips often open or close on a cut.
        for i in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES,
                    int(total * (0.1 + 0.8 * i / max(n - 1, 1))))
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
    finally:
        cap.release()
    return frames


def _faces(frame, cascade):
    # No equalizeHist: gameplay is mostly dark, and stretching the histogram
    # across the whole frame washes out the lit webcam corner. On a test clip
    # it cut detections from 14 to 2.
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    w = gray.shape[1]
    found = cascade.detectMultiScale(
        gray, scaleFactor=1.08, minNeighbors=5,
        minSize=(int(w * MIN_FACE),) * 2, maxSize=(int(w * MAX_FACE),) * 2)
    return [tuple(map(int, f)) for f in found]


def _face_anchors(frames, cascade):
    """Every face that keeps reappearing in one place — one per webcam."""
    fw = frames[0].shape[1]
    hits = []
    for f in frames:
        hits.extend(_faces(f, cascade))
    if not hits:
        return []

    tol = fw * CLUSTER_TOL
    groups = []
    for x, y, w, h in hits:
        cx, cy = x + w / 2, y + h / 2
        for g in groups:
            if abs(cx - g["cx"]) <= tol and abs(cy - g["cy"]) <= tol:
                g["boxes"].append((x, y, w, h))
                n = len(g["boxes"])
                g["cx"] += (cx - g["cx"]) / n
                g["cy"] += (cy - g["cy"]) / n
                break
        else:
            groups.append({"cx": cx, "cy": cy, "boxes": [(x, y, w, h)]})

    faces = []
    for g in groups:
        if len(g["boxes"]) < MIN_HITS:
            continue         # a face that never stays put is not a webcam
        n = len(g["boxes"])
        faces.append(tuple(sum(b[i] for b in g["boxes"]) // n for i in range(4)))
    return faces


def _persistent_edges(frames):
    """Edge map summed across frames: static borders survive, gameplay does not."""
    acc = None
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        e = cv2.Canny(gray, 60, 180).astype(np.float32) / 255.0
        acc = e if acc is None else acc + e
    return acc / len(frames)      # 1.0 = an edge in every single frame


def _border(profile, lo, hi, prefer_high):
    """Strongest straight line in [lo, hi); prefer the one nearest the corner."""
    if hi <= lo:
        return None
    window = profile[lo:hi]
    if window.max() <= 0:
        return None
    # Anything within 80% of the best counts; take the one closest to the
    # frame edge so we get the cam's outer rectangle, not a feature inside it.
    good = np.flatnonzero(window >= window.max() * 0.8)
    idx = good[-1] if prefer_high else good[0]
    return int(lo + idx)


def _cam_from_edges(edges, face, fw, fh):
    """Grow the face into the overlay rectangle its borders describe."""
    x, y, w, h = face
    right = (x + w / 2) >= fw / 2
    bottom = (y + h / 2) >= fh / 2

    # Vertical border: sum vertical-edge energy per column, over the rows the
    # cam plausibly spans. Horizontal border: the same by row.
    row_lo, row_hi = (int(fh * 0.5), fh) if bottom else (0, int(fh * 0.5))
    col_lo, col_hi = (int(fw * 0.5), fw) if right else (0, int(fw * 0.5))

    col_profile = edges[row_lo:row_hi, :].sum(axis=0)
    row_profile = edges[:, col_lo:col_hi].sum(axis=1)

    m = int(fw * EDGE_MARGIN)
    if right:
        # left edge of the cam: between the frame middle and the face
        bx = _border(col_profile, int(fw * (1 - CAM_W_RANGE[1])),
                     max(x, int(fw * (1 - CAM_W_RANGE[1]))) + 1, prefer_high=True)
        if bx is None:
            return None
        cam_x, cam_w = bx, fw - bx
    else:
        bx = _border(col_profile, min(x + w, int(fw * CAM_W_RANGE[1])),
                     int(fw * CAM_W_RANGE[1]) + 1, prefer_high=False)
        if bx is None:
            return None
        cam_x, cam_w = 0, bx

    if bottom:
        by = _border(row_profile, int(fh * (1 - CAM_H_RANGE[1])),
                     max(y, int(fh * (1 - CAM_H_RANGE[1]))) + 1, prefer_high=True)
        if by is None:
            return None
        cam_y, cam_h = by, fh - by
    else:
        by = _border(row_profile, min(y + h, int(fh * CAM_H_RANGE[1])),
                     int(fh * CAM_H_RANGE[1]) + 1, prefer_high=False)
        if by is None:
            return None
        cam_y, cam_h = 0, by

    if not (fw * CAM_W_RANGE[0] <= cam_w <= fw * CAM_W_RANGE[1]):
        return None
    if not (fh * CAM_H_RANGE[0] <= cam_h <= fh * CAM_H_RANGE[1]):
        return None
    # The box has to actually contain the face it was built around.
    if not (cam_x - m <= x and x + w <= cam_x + cam_w + m
            and cam_y - m <= y and y + h <= cam_y + cam_h + m):
        return None
    return int(cam_x), int(cam_y), int(cam_w), int(cam_h)


def _load_cv2():
    global cv2, np
    if cv2 is None:
        import cv2 as _cv2
        import numpy as _np
        cv2, np = _cv2, _np
    return cv2


def detect_all(path):
    """Every webcam overlay found, ordered left to right.

    A co-stream carries two or three cams; they share the top band so nobody
    gets cropped out. Past three the tiles are too narrow to read a face in, so
    the caller falls back to normal framing.
    """
    try:
        _load_cv2()
    except ImportError:
        return []
    frames = _sample_frames(path)
    if len(frames) < 3:
        return []
    faces = _face_anchors(frames, _cascade())
    if not faces:
        return []
    fh, fw = frames[0].shape[:2]
    edges = _persistent_edges(frames)
    boxes = [b for b in (_cam_from_edges(edges, f, fw, fh) for f in faces) if b]
    # Two faces inside one cam would otherwise yield near-identical boxes.
    unique = []
    for b in sorted(boxes, key=lambda b: b[2] * b[3], reverse=True):
        if not any(_overlaps(b, u) for u in unique):
            unique.append(b)
    return sorted(unique, key=lambda b: b[0])


def _overlaps(a, b, frac=0.5):
    """True when two boxes cover much the same area — the same cam, twice."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter >= frac * min(aw * ah, bw * bh)


def detect(path):
    """The single most likely host cam, or None. Kept for the CLI."""
    try:
        _load_cv2()
    except ImportError:
        return None
    frames = _sample_frames(path)
    if len(frames) < 3:
        return None
    boxes = detect_all(path)
    if not boxes:
        return None
    # Largest wins: on someone's own channel their cam is the bigger one, and
    # the credit burned into the video names the broadcaster.
    return max(boxes, key=lambda b: b[2] * b[3])


def main():
    _load_cv2()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("usage: python facecam.py CLIP.mp4 [--debug OUT.png]")
    path = args[0]
    boxes = detect_all(path)
    print(f"{path}: {len(boxes)} cam(s) {boxes if boxes else '- none found'}")
    box = max(boxes, key=lambda b: b[2] * b[3]) if boxes else None

    if "--debug" in sys.argv and box:
        out = args[1] if len(args) > 1 else "facecam_debug.png"
        frames = _sample_frames(path)
        f = frames[len(frames) // 2]
        x, y, w, h = box
        cv2.rectangle(f, (x, y), (x + w, y + h), (0, 0, 255), 6)
        cv2.imwrite(out, cv2.resize(f, (760, 428)))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
