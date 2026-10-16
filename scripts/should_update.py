#!/usr/bin/env python3
"""Cheap check run by the workflow before it does any real work (stdlib only).

GitHub cannot start a workflow here when you push to *another* repository, so the
workflow polls instead. This script answers one question: is there a new day of
pushes that the profile does not show yet, and has the profile not been
refreshed today? If so it writes `update=true` to $GITHUB_OUTPUT.

  * new day of pushes  = the newest push to a public, non-fork repo (excluding the
    profile repo itself, which the workflow commits to) falls on a different UTC
    day than the "latest public push" shown in the README;
  * once per day       = skipped when the README already says "Stats refreshed automatically on"
    today, so further pushes the same day wait for the next day;
  * FORCE=true         = always update (manual run and the weekly safety net).

Offline testing: PROFILE_CHECK_DATA=<repos.json> and PROFILE_TODAY="Oct 22, 2026".
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USER = "hallzyx"
README = Path(__file__).resolve().parent.parent / "README.md"
DATE = r"([A-Z][a-z]{2} \d{1,2}, \d{4})"


def parse(text):
    return datetime.strptime(text, "%b %d, %Y").date()


def repos():
    path = os.environ.get("PROFILE_CHECK_DATA")
    if path:
        data = json.load(open(path, encoding="utf-8"))
        return data["r"] if isinstance(data, dict) else data
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hallzyx-profile-bot"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    url = f"https://api.github.com/users/{USER}/repos?per_page=100&type=owner&sort=pushed&direction=desc"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


def decide():
    if os.environ.get("FORCE", "").lower() == "true":
        return True, "forced run"
    readme = README.read_text(encoding="utf-8")
    shown = re.search(r"latest public push " + DATE, readme)
    verified = re.search(r"(?:Data verified on|Stats refreshed automatically on) " + DATE, readme)
    if not shown or not verified:
        return True, "could not read the state from the README"
    if os.environ.get("PROFILE_TODAY"):
        today = parse(os.environ["PROFILE_TODAY"])
    else:
        today = datetime.now(timezone.utc).date()
    own = [r for r in repos() if not r.get("fork") and r["name"] != USER]
    newest = max(r["pushed_at"] for r in own)
    pushed_day = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").date()
    if pushed_day == parse(shown.group(1)):
        return False, f"no new push day (latest push {pushed_day} is already shown)"
    if parse(verified.group(1)) == today:
        return False, "already refreshed today, waiting for tomorrow"
    return True, f"new push day {pushed_day} (shown: {shown.group(1)})"


def main():
    update, why = decide()
    print(f"update={'true' if update else 'false'}: {why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"update={'true' if update else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
