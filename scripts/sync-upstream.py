#!/usr/bin/env python3
"""Sync the auto_cut_bot fork with upstream HKUDS/nanobot.

Codifies the procedure proven in the 2026-09-05 upstream sync (c27b1f14 -> 45553316).

Background: local main == upstream@c27b1f14 + package rename (nanobot -> auto_cut_bot).
Because of the rename, `git merge upstream/main` alone produces chimeric files
(ours import blocks + upstream bodies) that only fail at runtime. This script
replaces merge content for framework files with a deterministic, verifiable
conversion, and only preserves files the fork genuinely owns.

Usage:
    # 1. start a real merge so upstream becomes an ancestor (keeps history linear
    #    for future merges; rerere will replay today's resolutions):
    git fetch upstream --prune
    git checkout main
    git merge upstream/main --no-commit --no-ff || true   # conflicts expected

    # 2. align working-tree content with upstream (converted):
    python3 scripts/sync-upstream.py sync

    # 3. verify, then finish the merge:
    python3 scripts/sync-upstream.py verify [--full]
    git add -A && git commit --no-edit
    git push origin main

Conversion rules (derived from the fork's rename precedent):
    nanobot  -> auto_cut_bot   (lowercase identifiers, import paths)
    NANOBOT_ -> AUTO_CUT_BOT_  (env-var contract)
    Nanobot  (CamelCase class names) is intentionally kept as-is.

Files under LOCAL_KEEP are never overwritten; they carry fork-specific behavior
and must be re-ported by hand when upstream changes them (expect test failures
pointing at them).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPSTREAM_REMOTE = "upstream"
UPSTREAM_PREFIX = "nanobot/"
LOCAL_PREFIX = "auto_cut_bot/"

# Fork-owned files: never auto-overwritten from upstream.
LOCAL_KEEP: set[str] = {
    # local add-ons (no upstream counterpart) are skipped automatically;
    # list here only files that DO exist upstream but must keep local content:
}

CONVERSIONS = [
    (re.compile(r"\bNANOBOT_\b"), "AUTO_CUT_BOT_"),
    (re.compile(r"\bnanobot\b"), "auto_cut_bot"),
]


def convert(text: str) -> str:
    for pat, repl in CONVERSIONS:
        text = pat.sub(repl, text)
    return text


def git(*args: str, text: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=text, check=True).stdout


def upstream_ref() -> str:
    return f"{UPSTREAM_REMOTE}/main"


def upstream_files(prefix: str) -> list[str]:
    return git("ls-tree", "-r", "--name-only", upstream_ref(), prefix).split()


def local_counterpart(upstream_path: str) -> str | None:
    if upstream_path.startswith("tests/"):
        return upstream_path
    if upstream_path.startswith(UPSTREAM_PREFIX):
        return LOCAL_PREFIX + upstream_path[len(UPSTREAM_PREFIX):]
    return None


def mb_ref() -> str:
    # last fork-sync point; update after each completed sync
    return git("merge-base", "HEAD", upstream_ref()).strip()


def sync() -> None:
    mb = mb_ref()
    # Files upstream changed or deleted since the last sync point.
    changed = set(git("diff", "--name-only", mb, upstream_ref(), "--", UPSTREAM_PREFIX, "tests/").split())
    written = deleted = 0
    for up in sorted(upstream_files(UPSTREAM_PREFIX) + upstream_files("tests/")):
        if up in LOCAL_KEEP:
            continue
        target = local_counterpart(up)
        if target is None:
            continue
        blob = subprocess.run(
            ["git", "show", f"{upstream_ref()}:{up}"], cwd=REPO, capture_output=True, text=True
        )
        if blob.returncode != 0:
            continue  # removed upstream
        Path(target).write_text(convert(blob.stdout))
        written += 1

    # Upstream deletions since the sync point: drop local counterparts that are
    # pure rename products (content equals converted merge-base content).
    mb_files = set(git("ls-tree", "-r", "--name-only", mb, UPSTREAM_PREFIX).split())
    up_set = set(upstream_files(UPSTREAM_PREFIX))
    for mf in sorted(mb_files - up_set):
        target = local_counterpart(mf)
        if target is None:
            continue
        p = Path(target)
        if not p.exists():
            continue
        mb_blob = subprocess.run(["git", "show", f"{mb}:{mf}"], cwd=REPO, capture_output=True, text=True)
        if mb_blob.returncode == 0 and p.read_text() == convert(mb_blob.stdout):
            p.unlink()
            deleted += 1
        else:
            print(f"KEEP (fork-modified, upstream deleted): {target}")
    print(f"sync: wrote {written}, deleted {deleted} files. Review with `git status`.")


def verify(full: bool) -> int:
    # rename-consistency: no stray nanobot module refs left in package/tests
    stray = subprocess.run(
        ["grep", "-rn", "-e", "from nanobot\\.", "-e", "import nanobot\\b",
         "--include=*.py", "auto_cut_bot/", "tests/", "conftest.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    if stray.stdout.strip():
        print("STRAY nanobot references found — run the conversion before continuing:")
        print(stray.stdout[:2000])
        return 1
    print("stray-reference check: OK")
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
           "--collect-only"] + (["--ignore=tests/"] if False else [])
    r = subprocess.run(["uv", "run", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
                        "--collect-only"], cwd=REPO)
    if r.returncode != 0:
        print("collect failed — fix import-time breakage first")
        return r.returncode
    if full:
        return subprocess.run(["uv", "run", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
                              cwd=REPO).returncode
    print("collect OK; run `--full` for the whole suite")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("sync", "verify"))
    ap.add_argument("--full", action="store_true", help="verify: run the full test suite")
    args = ap.parse_args()
    if args.action == "sync":
        sync()
        return 0
    return verify(args.full)


if __name__ == "__main__":
    sys.exit(main())
