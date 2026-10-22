import argparse
from datetime import datetime, timezone
import logging
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import Config, load_dotenv
from .collector import collect
from .schedule import Slot, due_slot, next_slot, peak_end

log = logging.getLogger(__name__)


def run_loop(config, path, *, deadline=None, checkpoint=None, on_peak_finished=None):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    last_checkpoint = time.monotonic()
    report_due = None
    log.info("Collector running: weekdays %s; %s–%s / %s–%s %s; every %s minutes.",
             ",".join(str(day) for day in sorted(config.weekdays)), config.am_start, config.am_end,
             config.pm_start, config.pm_end, config.timezone, config.interval_minutes)
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        if report_due and now >= report_due:
            if on_peak_finished:
                on_peak_finished()
            report_due = None
        if deadline and now >= deadline:
            break
        slot = due_slot(now, config)
        if slot:
            report_due = peak_end(slot.when, config)
            status = collect(path, config, slot, now)
            if status == "budget_skipped":
                log.warning("Local monthly request ceiling reached; no HTTP request sent.")
            if checkpoint and time.monotonic() - last_checkpoint >= 900:
                checkpoint()
                last_checkpoint = time.monotonic()
        upcoming = next_slot(datetime.now(timezone.utc), config)
        if upcoming is None:
            log.info("Configured observation period finished.")
            break
        remaining = (upcoming.when - datetime.now(timezone.utc)).total_seconds()
        # Recheck the wall clock at most every 30 seconds; survive NTP changes and DST.
        if deadline:
            remaining = min(remaining, (deadline - datetime.now(timezone.utc)).total_seconds())
        stop.wait(max(.1, min(30, remaining)))
    if checkpoint:
        checkpoint()
    log.info("Collector stopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configurable commute time monitor")
    parser.add_argument("--config", help="Local JSON settings file (or TRAVELTIME_CONFIG_FILE)")
    parser.add_argument("--db", default="data/commute.sqlite3")
    parser.add_argument("--output", default="reports")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check configuration without making paid API requests")
    one = sub.add_parser("once", help="Collect the currently due slot; otherwise skip")
    one.add_argument("--test-direction", choices=["am", "pm"],
                     help="Query now for setup testing, excluded from statistics")
    for name in ("run", "peak"):
        child = sub.add_parser(name, help=("Run continuously" if name == "run"
                                         else "Run this peak only (or start within 10 minutes)"))
        child.add_argument("--checkpoint-state", help="GitHub Actions state worktree")
    sub.add_parser("report", help="Generate charts, HTML and CSV without API calls")
    demo = sub.add_parser("demo", help="Generate clearly labeled simulated data; no network")
    demo.add_argument("--days", type=int, default=10)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_dotenv()
        config = Config.from_env(args.config)
        if args.command == "doctor":
            print(f"Timezone: {config.timezone}; weekdays (Monday=0): "
                  + ",".join(str(day) for day in sorted(config.weekdays)))
            print(f"Home → work: {config.am_start}–{config.am_end}; "
                  f"work → home: {config.pm_start}–{config.pm_end}")
            print(f"Interval: {config.interval_minutes} minutes")
            for key, value in (("GOOGLE_MAPS_API_KEY", config.api_key),
                               ("COMMUTE_HOME", config.home), ("COMMUTE_WORK", config.work)):
                print(f"{key}: {'configured' if value else 'MISSING'}")
            config.validate(credentials=True)
            print("Configuration valid. No network requests made.")
            return 0
        if args.command == "demo":
            from .demo import generate
            generate(args.db, args.output, args.days, config=config)
            return 0
        if args.command == "report":
            from .reports import create_report
            print(create_report(args.db, args.output, config))
            return 0
        config.validate(credentials=True)
        now = datetime.now(timezone.utc)
        if args.command == "once":
            if args.test_direction:
                slot = Slot(now.astimezone(config.zone), args.test_direction)
                status = collect(args.db, config, slot, now, manual=True)
            else:
                slot = due_slot(now, config)
                if not slot:
                    log.info("No current eligible slot; nothing queried or backfilled.")
                    return 0
                status = collect(args.db, config, slot, now)
            return 1 if status in ("error", "budget_skipped") else 0
        deadline = peak_end(now, config) if args.command == "peak" else None
        if args.command == "peak" and deadline is None:
            log.info("Outside a peak and its 10-minute preparation window; nothing queried.")
            return 0
        def checkpoint():
            if args.checkpoint_state:
                result = subprocess.run(
                    [sys.executable, "scripts/state.py", "save", args.checkpoint_state],
                    check=False, capture_output=True, text=True)
                if result.returncode:
                    log.warning("State checkpoint failed; local data retained. See final save step.")
        from .reports import create_report
        run_loop(config, args.db, deadline=deadline,
                 checkpoint=checkpoint if args.checkpoint_state else None,
                 on_peak_finished=lambda: create_report(args.db, args.output, config))
        print(create_report(args.db, args.output, config))
        return 0
    except (ValueError, OSError) as exc:
        # Configuration errors are written by us and never contain the secret values.
        log.error("%s", str(exc) if isinstance(exc, ValueError)
                  else "Local file/process error. Check paths and permissions.")
        return 2
