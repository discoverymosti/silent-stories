"""
Is a video due right now? Runs first in every automatic run, in seconds, before anything heavy.

GitHub starts scheduled workflows late, or drops them, when it is busy, so the workflow is
started several times per publishing window (and optionally by an external timer). The first
run that gets through makes the video; the others see it in done.txt and stop. Result: one video
per window, never zero because GitHub skipped a slot, never two.

Manual runs (Run workflow) always go ahead with the format chosen in the form, unless "only_if_due"
(timer mode) is ticked: then they are judged like a timer tick, so an external timer can never double up.
Writes go=true|false and format=short|film to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANNEL = (os.getenv("CHANNEL") or "main").strip().lower()
CH_DIR = ROOT if CHANNEL == "main" else ROOT / "channels" / CHANNEL
DONE = CH_DIR / "done.txt"

# publishing windows in UTC: (start hour, end hour, weekdays with Monday = 0)
WINDOWS = {
    "film": [(16, 22, {6})],                     # Sunday evening, worldwide prime time
    "short": [(1, 7, {0, 1, 2, 3, 4, 5}),        # Mon-Sat: Asia morning / Americas evening
              (14, 20, {0, 1, 2, 3, 4, 5, 6})],  # daily: Asia/Europe evening / Americas morning
}
if CHANNEL == "tech":  # Discovery Mosti TECH: two Shorts a day, between the main channel's windows
    WINDOWS = {"film": [],
               "short": [(7, 13, set(range(7))),     # 12:30-18:30 IST: India afternoon / Europe morning
                         (19, 24, set(range(7)))]}   # 00:30-05:30 IST: Americas afternoon-evening


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


def _cron_values(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        if part == "*":
            out |= set(range(lo, hi + 1))
        elif "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def planned_time(cron: str, now: datetime) -> datetime | None:
    """When a scheduled run was meant to start. GitHub has been starting scheduled runs 4-5+ hours
    late (Oct 2026), so a late try must count for the window it was planned for, not the one it
    lands in. Supports the 'M H * * DOW' crons used in publish.yml."""
    try:
        minute, hours, _, _, dows = cron.split()
        hour_set, dow_set = _cron_values(hours, 0, 23), _cron_values(dows, 0, 6)
        t = now.replace(minute=int(minute), second=0, microsecond=0)
    except (ValueError, AttributeError):
        return None
    for back in range(0, 30):  # the latest matching time in the last 30 hours
        c = t - timedelta(hours=back)
        if c <= now and c.hour in hour_set and (c.weekday() + 1) % 7 in dow_set:
            return c
    return None


def decide(now: datetime, event: str, manual_format: str, schedule: str = "",
           only_if_due: bool = False) -> tuple[bool, str, str]:
    if event not in ("schedule", "repository_dispatch") and not only_if_due:
        return True, manual_format or "short", "manual run"
    when = (planned_time(schedule, now) if event == "schedule" else None) or now
    late = f", started {(now - when).total_seconds() / 3600:.1f} h late" if now - when > timedelta(minutes=30) else ""
    for fmt in ("film", "short"):  # on Sunday evening the film goes first, the Short after it
        for start, end, days in WINDOWS[fmt]:
            if when.weekday() not in days or not start <= when.hour < end:
                continue
            opened = when.replace(hour=start, minute=0, second=0, microsecond=0)
            last = last_upload(fmt)
            if last and last >= opened:
                break  # this window's video is already up
            return True, fmt, f"{fmt} due ({start:02d}:00-{end:02d}:00 UTC window{late})"
    return False, "short", f"nothing due (outside a window, or this window's video is already up{late})"


def main() -> None:
    if not (CH_DIR / "yt_token.enc").exists() and not os.getenv("YT_REFRESH_TOKEN"):
        go, fmt, why = False, "short", f"the {CHANNEL} YouTube channel is not connected yet (Connect YouTube channel)"
    else:
        go, fmt, why = decide(datetime.now(timezone.utc), os.getenv("EVENT", ""), os.getenv("MANUAL_FORMAT", ""),
                              os.getenv("SCHEDULE", ""), os.getenv("ONLY_IF_DUE", "").lower() == "true")
    print(f"{'GO' if go else 'SKIP'}: {why}")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\nformat={fmt}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"**{'Making a ' + fmt if go else 'Nothing to do'}**: {why}\n")


if __name__ == "__main__":
    main()
