#!/usr/bin/env python3
"""
The clock.

Five jobs, not one:

  open       09:35 New York, weekdays. What changed overnight, and did anything
             gap through a level that matters.
  close      16:05 New York, weekdays. The main review, on settled closes.
  overnight  02:00 New York, daily. Crypto trades while the equity market
             sleeps, and this is when Asia and Europe have already moved.
  trackers   03:20 New York, daily. Re-reads the disclosed books behind the
             leaderboard. Without it the board is only rebuilt when somebody
             opens the dashboard after its cache has gone cold, which is not a
             schedule.
  news       every PM_NEWS_INTERVAL_MIN minutes. Checks headlines and runs the
             event path, which is the only path that can trade by itself.

Times are held in New York local time and converted per run, so the schedule
tracks daylight saving instead of drifting an hour twice a year.

The full reviews propose and wait. Only the news job can execute, and only when
autopilot is armed in the dashboard.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

NEWS_INTERVAL_MIN = int(os.environ.get("PM_NEWS_INTERVAL_MIN", "20"))
NEWS_QUIET_INTERVAL_MIN = int(os.environ.get("PM_NEWS_QUIET_INTERVAL_MIN", "60"))
RUN_ON_BOOT = os.environ.get("PM_RUN_ON_BOOT", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s scheduler  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger()

#: consecutive failures per job, so a permanently broken job gets louder
FAILURES: dict[str, int] = {}


@dataclass
class Job:
    name: str
    script: str
    args: list[str]
    hour: int
    minute: int
    weekdays_only: bool
    timeout: int = 900


DAILY_JOBS = [
    Job("open", "run_daily.py", ["--tag", "open"], 9, 35, True),
    Job("close", "run_daily.py", ["--tag", "close"], 16, 5, True),
    Job("overnight", "run_daily.py", ["--tag", "overnight"], 2, 0, False),
    # Sixteen CIKs at the SEC's ten requests a second, then a few hundred
    # symbols of daily prices, so it gets a longer leash than a review does.
    Job("trackers", "refresh_trackers.py", ["--force"], 3, 20, False,
        timeout=1800),
]


def next_fire(job: Job, after: dt.datetime) -> dt.datetime:
    """
    Next occurrence of this job's local time, as an aware UTC datetime.

    Built in New York local time so the UTC offset moves with daylight saving
    rather than the market opening an hour early for half the year.
    """
    local = after.astimezone(NY)
    candidate = local.replace(hour=job.hour, minute=job.minute,
                              second=0, microsecond=0)
    if candidate <= local:
        candidate += dt.timedelta(days=1)
    while job.weekdays_only and candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    # Re-localise: adding days across a DST boundary can otherwise land an
    # hour out.
    candidate = candidate.replace(tzinfo=None).replace(tzinfo=NY)
    return candidate.astimezone(UTC)


def market_hours_now() -> bool:
    """Roughly, is the US market in its extended session right now."""
    local = dt.datetime.now(NY)
    if local.weekday() >= 5:
        return False
    return dt.time(4, 0) <= local.time() <= dt.time(20, 0)


def news_interval() -> int:
    """Poll harder while the market is open, back off overnight."""
    return NEWS_INTERVAL_MIN if market_hours_now() else NEWS_QUIET_INTERVAL_MIN


def run(script: str, args: list[str], label: str, timeout: int = 900) -> int:
    log.info("[%s] starting %s", label, script)
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / script), *args],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.error("[%s] exceeded %ds and was killed", label, timeout)
        return 124
    output = (proc.stderr or proc.stdout).strip()
    for line in output.splitlines()[-25:]:
        log.info("[%s]   %s", label, line)

    if proc.returncode == 0:
        log.info("[%s] finished with code 0", label)
        FAILURES.pop(label, None)
        return 0

    # A job that cannot start looks exactly like a job with nothing to do if
    # both are logged at INFO. run_event.py was missing from the image once and
    # failed every 20 minutes for 17 hours without anyone noticing.
    FAILURES[label] = FAILURES.get(label, 0) + 1
    log.error("[%s] FAILED with code %d (%d in a row): %s",
              label, proc.returncode, FAILURES[label],
              output.splitlines()[-1] if output else "no output")
    if FAILURES[label] in (3, 10) or FAILURES[label] % 25 == 0:
        log.error("[%s] has now failed %d consecutive times. This job is not "
                  "running at all; check the command and the image.",
                  label, FAILURES[label])
    return proc.returncode


def main() -> int:
    log.info("scheduler up")
    for job in DAILY_JOBS:
        log.info("  %-10s %02d:%02d New York%s",
                 job.name, job.hour, job.minute,
                 ", weekdays only" if job.weekdays_only else ", every day")
    log.info("  %-10s every %d minutes while the market is open, %d otherwise",
             "news", NEWS_INTERVAL_MIN, NEWS_QUIET_INTERVAL_MIN)

    now = dt.datetime.now(UTC)
    schedule = {job.name: next_fire(job, now) for job in DAILY_JOBS}
    next_news = now + dt.timedelta(minutes=1)

    for name, when in sorted(schedule.items(), key=lambda kv: kv[1]):
        log.info("next %s at %s UTC", name, when.strftime("%Y-%m-%d %H:%M"))

    if RUN_ON_BOOT:
        run("run_daily.py", ["--tag", "boot"], "boot")

    while True:
        now = dt.datetime.now(UTC)
        due_at = min(min(schedule.values()), next_news)
        wait = (due_at - now).total_seconds()
        if wait > 0:
            # Sleep in chunks so a clock jump or a container pause cannot
            # strand the timer past its window.
            import time
            time.sleep(min(wait, 300))
            continue

        for job in DAILY_JOBS:
            if schedule[job.name] <= now:
                try:
                    run(job.script, job.args, job.name, job.timeout)
                except Exception as e:  # noqa: BLE001 - one bad run must not stop the clock
                    log.error("[%s] crashed: %s", job.name, e)
                schedule[job.name] = next_fire(job, dt.datetime.now(UTC))
                log.info("next %s at %s UTC", job.name,
                         schedule[job.name].strftime("%Y-%m-%d %H:%M"))

        if next_news <= now:
            try:
                run("run_event.py", [], "news", timeout=600)
            except Exception as e:  # noqa: BLE001
                log.error("[news] crashed: %s", e)
            next_news = dt.datetime.now(UTC) + dt.timedelta(minutes=news_interval())


if __name__ == "__main__":
    raise SystemExit(main())
