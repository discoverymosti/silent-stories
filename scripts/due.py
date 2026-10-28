"""
Is a video due right now? Runs first in every automatic run, in seconds, before anything heavy.

GitHub starts scheduled workflows late, or drops them, when it is busy, so the workflow is
started several times per publishing window (and optionally by an external timer). The first
run that gets through makes the video; the others see it in done.txt and stop. Result: one video
per window, never zero because GitHub skipped a slot, never two.

Manual runs (Run workflow) always go ahead with the format chosen in the form.
Writes go=true|false and format=short|film to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

DONE = Path(__file__).resolve().parent.parent / "done.txt"

# publishing windows in UTC: (start hour, end hour, weekdays with Monday = 0)
WINDOWS = {
    "film": [(16, 22, {6})],                     # Sunday evening, worldwide prime time
    "short": [(1, 7, {0, 1, 2, 3, 4, 5}),        # Mon-Sat: Asia morning / Americas evening
              (14, 20, {0, 1, 2, 3, 4, 5, 6})],  # daily: Asia/Europe evening / Americas morning
}


def last_upload(fmt: str) -> datetime | None:
    """Time of the newest upload of this format in done.txt (lines: idea | url | time | format)."""
    if not DONE.exists():
        return None
    for line in reversed(DONE.read_text(encoding="utf-8").splitlines()):
        parts = [x.strip() for x in line.split(" | ")]
        if len(parts) < 4 or parts[-1] != fmt or not parts[-3].startswith("http"):
            continue
        try:
            return datetime.strptime(parts[-2], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def decide(now: datetime, event: str, manual_format: str) -> tuple[bool, str, str]:
    if event not in ("schedule", "repository_dispatch"):
        return True, manual_format or "short", "manual run"
    for fmt in ("film", "short"):  # on Sunday evening the film goes first, the Short after it
        for start, end, days in WINDOWS[fmt]:
            if now.weekday() not in days or not start <= now.hour < end:
                continue
            opened = now.replace(hour=start, minute=0, second=0, microsecond=0)
            last = last_upload(fmt)
            if last and last >= opened:
                break  # this window's video is already up
            return True, fmt, f"{fmt} due ({start:02d}:00-{end:02d}:00 UTC window)"
    return False, "short", "nothing due right now (outside a window, or this window's video is already up)"


def main() -> None:
    go, fmt, why = decide(datetime.now(timezone.utc), os.getenv("EVENT", ""), os.getenv("MANUAL_FORMAT", ""))
    print(f"{'GO' if go else 'SKIP'}: {why}")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\nformat={fmt}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"**{'Making a ' + fmt if go else 'Nothing to do'}**: {why}\n")


if __name__ == "__main__":
    main()
