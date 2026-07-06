#!/usr/bin/env python3
"""Headless check runner for cloud (GitHub Actions) mode.

Runs ONE pass over every target in targets.yml, compares each result to the
last known state in state.json, and pushes an ntfy alert on an
UNAVAILABLE/UNKNOWN -> AVAILABLE transition. No Flask, no scheduler, no
database — the scheduling is done by the GitHub Actions cron, and state is
persisted in state.json (committed back by the workflow).

Reuses the same fetch + detection logic as the local app (detection.py), so
keyword/diff detection and the Playwright fallback behave identically.

Config (from environment / GitHub Actions secrets):
    NTFY_SERVER   default https://ntfy.sh
    NTFY_TOPIC    required to actually send (otherwise pushes are skipped)
    NTFY_TOKEN    optional, for protected topics
"""
import datetime
import json
import os
import re
import sys

import yaml

import detection

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TARGETS_FILE = os.path.join(BASE_DIR, "targets.yml")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

STATE_AVAILABLE = "available"


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "item"


def load_targets():
    if not os.path.exists(TARGETS_FILE):
        print(f"No targets file at {TARGETS_FILE}", file=sys.stderr)
        return []
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        print("targets.yml must be a YAML list of targets.", file=sys.stderr)
        return []
    return data


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def send_ntfy(title, message, click_url=None, priority="default", tags=None):
    """Post to ntfy. Returns True on success. Headers kept ASCII (HTTP/latin-1);
    emoji is delivered via the Tags header and the UTF-8 body."""
    import requests

    server = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    token = (os.getenv("NTFY_TOKEN") or "").strip()
    if not topic:
        print("  [ntfy] NTFY_TOPIC not set — skipping push.")
        return False

    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15
        )
        resp.raise_for_status()
        print(f"  [ntfy] pushed to {server}/{topic}")
        return True
    except Exception as exc:
        print(f"  [ntfy] FAILED: {exc}", file=sys.stderr)
        return False


def main():
    targets = load_targets()
    state = load_state()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if not targets:
        print("No targets to check.")
        return 0

    changed = False
    notified = 0
    print(f"== Website Tracker cloud run @ {now} — {len(targets)} target(s) ==")

    for t in targets:
        name = t.get("name") or t.get("url") or "unnamed"
        if not t.get("active", True):
            print(f"- {name}: (paused)")
            continue
        if not t.get("url"):
            print(f"- {name}: SKIP (no url)")
            continue

        key = slugify(name)
        prev = state.get(key, {})
        prev_state = prev.get("state", "unknown")

        item = {
            "url": t["url"],
            "detection_mode": t.get("mode", "keyword"),
            "available_phrases": t.get("available_phrases") or [],
            "unavailable_phrases": t.get("unavailable_phrases") or [],
            "last_snapshot_hash": prev.get("hash"),
        }

        result = detection.run_detection(item)

        if result["error"]:
            # Keep the previous known state on error; just log it.
            print(f"- {name}: {prev_state} (ERROR: {result['error']})")
            continue

        new_state = result["state"]
        new_hash = result.get("snapshot_hash") or prev.get("hash")

        # Notify only on transition INTO available.
        if new_state == STATE_AVAILABLE and prev_state != STATE_AVAILABLE:
            send_ntfy(
                title=f"ON SALE: {name}",
                message=f"{name} appears to be AVAILABLE.\n{t['url']}\n(detected {now})",
                click_url=t["url"],
                priority="urgent",
                tags=["tada"],
            )
            notified += 1

        method = result.get("fetch_method") or "n/a"
        arrow = "->" if new_state != prev_state else "=="
        print(f"- {name}: {prev_state} {arrow} {new_state} ({method})")

        # Persist only the fields needed for transition detection so state.json
        # (and its commit history) stays quiet unless something actually changes.
        new_entry = {"state": new_state}
        if new_hash:
            new_entry["hash"] = new_hash
        if new_entry != prev:
            state[key] = new_entry
            changed = True

    save_state(state)
    print(f"== done: {notified} notification(s); state {'updated' if changed else 'unchanged'} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
