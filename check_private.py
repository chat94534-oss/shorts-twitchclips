#!/usr/bin/env python3
"""Refuse to commit personal data or credentials into a public repo.

Runs as a pre-commit hook (on staged content) and as the first CI step (on the
tracked tree). Exits non-zero with the offending file and line.

Deliberately matches SHAPES, not literals: a deny-list containing the real
email or the real key would itself be the leak. Adding a new rule means adding
a pattern, never a secret.

    python check_private.py            # scan tracked files
    python check_private.py --staged   # scan what is about to be committed
"""
import re
import subprocess
import sys

# (name, pattern, why it matters)
RULES = [
    ("email address",
     r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}",
     "personal accounts should not be named in a public repo"),
    ("Google OAuth client secret",
     r"GOCSPX-[\w-]{10,}",
     "grants access to the OAuth app"),
    ("Google OAuth client id",
     r"\d{8,}-[a-z0-9]{16,}\.apps\.googleusercontent\.com",
     "identifies the project and pairs with the secret"),
    ("OAuth token material",
     r'"(refresh_token|client_secret|access_token)"\s*:\s*"[^"]{8,}"',
     "a live credential, not a variable name"),
    ("GitHub token",
     r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
     "grants repo access"),
    ("local Windows user path",
     r"[Cc]:[\\/]+Users[\\/]+[A-Za-z0-9._-]+",
     "leaks the machine's account name"),
    ("Twitch app credential",
     r"(?i)twitch[_-]?client[_-]?(id|secret)\s*[:=]\s*['\"][A-Za-z0-9]{15,}",
     "a live credential, not an env lookup"),
]

# Shapes that look like hits but are generic infrastructure, not personal data.
ALLOW = [
    r"actions@users\.noreply\.github\.com",   # GitHub's own bot identity
    r"noreply@anthropic\.com",                # commit trailer
    r"\d+\+[\w-]+@users\.noreply\.github\.com",  # GitHub noreply, not a real inbox
    r"user\.email\s+\"",                      # the git config line itself
    r"paste-your-",                           # .env placeholders
    r"xxxx",                                  # README credential examples
]
ALLOW_RE = re.compile("|".join(ALLOW))

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".mp4", ".webm", ".gif", ".ico")


def files_to_scan(staged):
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines()
            if f and not f.lower().endswith(SKIP_SUFFIXES)]


def content(path, staged):
    if staged:
        p = subprocess.run(["git", "show", f":{path}"],
                           capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def main():
    staged = "--staged" in sys.argv
    hits = []
    for path in files_to_scan(staged):
        if path == "check_private.py":
            continue  # this file is nothing but patterns
        for n, line in enumerate(content(path, staged).splitlines(), 1):
            if ALLOW_RE.search(line):
                continue
            for name, pattern, why in RULES:
                m = re.search(pattern, line)
                if m:
                    hits.append((path, n, name, why, m.group(0)[:60]))
                    break

    if not hits:
        print(f"check_private: clean ({'staged' if staged else 'tracked'} files)")
        return 0

    print("check_private: BLOCKED — private data found\n", file=sys.stderr)
    for path, n, name, why, sample in hits:
        print(f"  {path}:{n}  {name}", file=sys.stderr)
        print(f"    {sample}", file=sys.stderr)
        print(f"    {why}\n", file=sys.stderr)
    print("Remove it, or add a pattern to ALLOW in check_private.py if this is "
          "genuinely generic.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
