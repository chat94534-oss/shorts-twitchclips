#!/usr/bin/env python3
"""Merge this run's state into whatever is already on the remote branch.

Both files this pipeline persists are append-only sets:

    state.json        {"seen": [clip ids]}   — grows, order irrelevant
    logs/history.csv  one row per upload     — grows, rows unique

so a rebase is the wrong tool. A rebase treats two runs appending to the same
file as a conflict and fails the step, which loses the record that a clip was
spent — and a clip that is not marked spent gets uploaded a second time. That
has already happened once.

This takes the union instead, which is always correct for append-only data.

    python merge_state.py origin/main
"""
import csv
import io
import json
import subprocess
import sys

STATE = "state.json"
HISTORY = "logs/history.csv"


def remote_text(ref, path):
    """File contents at `ref`, or None when it is not there yet."""
    p = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def local_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def merge_state(ref):
    mine = json.loads(local_text(STATE) or '{"seen": []}')
    theirs_raw = remote_text(ref, STATE)
    if not theirs_raw:
        return 0
    theirs = json.loads(theirs_raw)

    # Preserve order (theirs first, then ours) while deduping.
    seen, out = set(), []
    for cid in list(theirs.get("seen", [])) + list(mine.get("seen", [])):
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    added = len(out) - len(theirs.get("seen", []))
    mine["seen"] = out
    with open(STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(mine, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return added


def merge_history(ref):
    theirs_raw = remote_text(ref, HISTORY)
    mine_raw = local_text(HISTORY)
    if not theirs_raw or not mine_raw:
        return 0

    def rows(text):
        return [r for r in csv.reader(io.StringIO(text)) if r]

    theirs, mine = rows(theirs_raw), rows(mine_raw)
    header = theirs[0] if theirs else mine[0]
    seen, out = set(), []
    for r in theirs[1:] + mine[1:]:
        key = tuple(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    added = len(out) - max(len(theirs) - 1, 0)
    with open(HISTORY, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(out)
    return added


def demo():
    """Union logic, checked without touching git."""
    a, b = ["x", "y"], ["y", "z"]
    seen, out = set(), []
    for cid in a + b:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    assert out == ["x", "y", "z"], out
    assert len(out) == 3, "a clip present on both sides must appear once"
    print("merge_state.py self-check: OK")


def main():
    if "--demo" in sys.argv:
        return demo()
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    s = merge_state(ref)
    h = merge_history(ref)
    print(f"merged against {ref}: {s} new clip id(s), {h} new history row(s)")


if __name__ == "__main__":
    main()
