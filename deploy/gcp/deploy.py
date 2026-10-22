"""Build the service and reconcile exact local-time Scheduler jobs."""
import json
import os
import re
from pathlib import Path
import tempfile

from traveltime.config import Config, load_dotenv
from .common import Gcloud, SECRETS, account, arguments, validate


def runtime_environment(args, config=None):
    config = config or Config.from_env(getattr(args, "config", None))
    return {
        "GOOGLE_CLOUD_PROJECT": args.project, "CLOUD_EXPERIMENT": args.experiment,
        "COLLECTION_ENABLED": str(args.enable_schedule).lower(),
        "SAMPLE_INTERVAL_MINUTES": str(config.interval_minutes),
        "COMMUTE_TIMEZONE": config.timezone,
        "MONITOR_WEEKDAYS": ",".join(map(str, config.weekdays)),
        "AM_START": config.am_start, "AM_END": config.am_end,
        "PM_START": config.pm_start, "PM_END": config.pm_end,
        "MAX_LATENESS_SECONDS": str(config.max_lateness_seconds),
        "MONTHLY_REQUEST_LIMIT": str(config.monthly_request_limit),
        "NDW_ENABLED": str(config.ndw_enabled).lower(),
        "NDW_CORRIDOR_METERS": str(config.ndw_corridor_meters),
        "DELAY_THRESHOLD_MINUTES": str(config.delay_threshold_minutes),
        "TRIP_THRESHOLD_MINUTES": str(config.trip_threshold_minutes),
        "MONITOR_START_DATE": config.start_date.isoformat() if config.start_date else "",
        "MONITOR_END_DATE": config.end_date.isoformat() if config.end_date else "",
        "EXCLUDE_DATES": ",".join(d.isoformat() for d in config.exclude_dates),
    }


def _cron_field(values, lower, upper):
    values = sorted(set(values))
    if values == list(range(lower, upper + 1)):
        return "*"
    if len(values) > 1:
        step = values[1] - values[0]
        if lower == 0 and values == list(range(0, upper + 1, step)):
            return f"*/{step}"
        if values == list(range(values[0], values[-1] + 1)):
            return f"{values[0]}-{values[-1]}"
    return ",".join(map(str, values))


def schedule_jobs(config):
    """Exact wall-clock slots, including offset windows and non-divisor intervals.

    Cron */N resets at each hour; grouping the actual minute sets avoids that
    mismatch. Ordinary default windows still produce just the two original jobs.
    """
    config.validate()
    weekdays = _cron_field(((day + 1) % 7 for day in config.weekdays), 0, 6)
    result = {}
    for direction, start, end in config.windows:
        hours = {}
        for minute in range(start, end, config.interval_minutes):
            hours.setdefault(minute // 60, []).append(minute % 60)
        groups = {}
        for hour, minutes in hours.items():
            groups.setdefault(tuple(minutes), []).append(hour)
        for index, (minutes, grouped_hours) in enumerate(groups.items(), 1):
            suffix = direction if len(groups) == 1 else f"{direction}-{index:02d}"
            result[suffix] = (f"{_cron_field(minutes, 0, 59)} "
                              f"{_cron_field(grouped_hours, 0, 23)} * * {weekdays}")
    return result


def deploy(args, cloud=None):
    validate(args)
    load_dotenv(Path(getattr(args, "env_file", ".env")))
    config = Config.from_env(getattr(args, "config", None))
    environment = runtime_environment(args, config)
    schedules = schedule_jobs(config)
    cloud = cloud or Gcloud(args.project)
    jobs = cloud("scheduler", "jobs", "list", f"--location={args.region}") or []
    known = {job["name"].rsplit("/", 1)[-1]: job for job in jobs}
    # Pause old jobs before changing runtime settings. A failed deploy stays paused
    # and preserves Firestore history, rather than collecting with half-updated settings.
    owned_name = re.compile(re.escape(args.service) + r"-(?:am|pm)(?:-\d{2})?$")
    for name, job in known.items():
        if owned_name.fullmatch(name) and job.get("state") == "ENABLED":
            cloud("scheduler", "jobs", "pause", name, f"--location={args.region}")
    refs = []
    for env_name, suffix in SECRETS.items():
        secret = f"{args.service}-{suffix}"
        versions = cloud("secrets", "versions", "list", secret,
                         "--filter=state:ENABLED", "--sort-by=~createTime", "--limit=1") or []
        if not versions:
            raise ValueError(f"No enabled version of {secret}. Run bootstrap first.")
        version = versions[0]["name"].rsplit("/", 1)[-1]
        refs.append(f"{env_name}={secret}:{version}")
    with tempfile.TemporaryDirectory(prefix="traveltime-deploy-") as directory:
        env_file = Path(directory) / "runtime.json"
        # JSON is valid YAML and handles commas in EXCLUDE_DATES without CLI splitting.
        env_file.write_text(json.dumps(environment), encoding="utf-8")
        cloud("run", "deploy", args.service, "--source=.", f"--region={args.region}",
              f"--service-account={account(args, 'runtime')}",
              f"--build-service-account=projects/{args.project}/serviceAccounts/{account(args, 'builder')}",
              "--no-allow-unauthenticated", "--invoker-iam-check", "--min=0", "--max=1",
              "--concurrency=1", "--cpu=1", "--memory=512Mi", "--timeout=120s",
              "--cpu-throttling", "--no-cpu-boost",
              f"--env-vars-file={env_file}", "--set-secrets=" + ",".join(refs))
    service = cloud("run", "services", "describe", args.service, f"--region={args.region}")
    url = service["status"]["url"]
    cloud("run", "services", "add-iam-policy-binding", args.service,
          f"--region={args.region}", f"--member=serviceAccount:{account(args, 'scheduler')}",
          "--role=roles/run.invoker")
    for suffix, schedule in schedules.items():
        name = f"{args.service}-{suffix}"
        operation = "update" if name in known else "create"
        cloud("scheduler", "jobs", operation, "http", name, f"--location={args.region}",
              f"--schedule={schedule}", f"--time-zone={config.timezone}",
              f"--uri={url}/collect", "--http-method=POST", "--attempt-deadline=120s",
              "--max-retry-attempts=0", "--max-retry-duration=0s",
              f"--oidc-service-account-email={account(args, 'scheduler')}",
              f"--oidc-token-audience={url}")
        # Fresh jobs start enabled; updated jobs retain their previous paused state.
        if args.enable_schedule and name in known:
            cloud("scheduler", "jobs", "resume", name, f"--location={args.region}")
        elif not args.enable_schedule and name not in known:
            cloud("scheduler", "jobs", "pause", name, f"--location={args.region}")
    summary = (f"Cloud Run: {url}\n"
               f"Experiment: {args.experiment}\n"
               f"Schedule: {'ENABLED' if args.enable_schedule else 'PAUSED'}\n"
               f"Timezone: {config.timezone}; weekdays (Mon=0): {config.weekdays}; "
               f"windows: {config.am_start}–{config.am_end} / {config.pm_start}–{config.pm_end}; "
               f"every {config.interval_minutes} minutes.\n"
               f"Managed scheduler jobs: {len(schedules)}; obsolete jobs remain paused.\n"
               "API Key and endpoints remain in Secret Manager.\n")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("~~~text\n" + summary + "~~~\n")
    return url


def main():
    parser = arguments("Deploy Google Cloud collection; local mode is independent")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", help="Optional local JSON settings file")
    parser.add_argument("--enable-schedule", action="store_true",
                        help="Start/restart automatic sampling after deployment")
    args = parser.parse_args()
    try:
        deploy(args)
    except OSError:
        print("Cannot read local deployment configuration; check the selected files and permissions.")
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
