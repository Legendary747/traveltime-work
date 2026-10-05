"""Build the service and reconcile two local-time Scheduler jobs."""
import json
import os
from pathlib import Path
import tempfile

from traveltime.config import Config
from .common import Gcloud, SECRETS, account, arguments, validate


def runtime_environment(args):
    config = Config.from_env()
    if config.interval_minutes != 3:
        raise ValueError("Cloud deployment uses a fixed 3-minute schedule.")
    return {
        "GOOGLE_CLOUD_PROJECT": args.project, "CLOUD_EXPERIMENT": args.experiment,
        "COLLECTION_ENABLED": str(args.enable_schedule).lower(),
        "SAMPLE_INTERVAL_MINUTES": "3",
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


def deploy(args, cloud=None):
    validate(args)
    environment = runtime_environment(args)
    cloud = cloud or Gcloud(args.project)
    jobs = cloud("scheduler", "jobs", "list", f"--location={args.region}") or []
    known = {job["name"].rsplit("/", 1)[-1]: job for job in jobs}
    # Pause old jobs before changing runtime settings. A failed deploy stays paused
    # and preserves Firestore history, rather than collecting with half-updated settings.
    for direction in ("am", "pm"):
        name = f"{args.service}-{direction}"
        if name in known and known[name].get("state") == "ENABLED":
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
    for direction, schedule in (("am", "*/3 6-8 * * 1-5"), ("pm", "*/3 15-17 * * 1-5")):
        name = f"{args.service}-{direction}"
        operation = "update" if name in known else "create"
        cloud("scheduler", "jobs", operation, "http", name, f"--location={args.region}",
              f"--schedule={schedule}", "--time-zone=Europe/Amsterdam",
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
               "Weekdays Europe/Amsterdam: 06:00–09:00 and 15:00–18:00, every 3 minutes.\n"
               "API Key and endpoints remain in Secret Manager.\n")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("~~~text\n" + summary + "~~~\n")
    return url


def main():
    parser = arguments("Deploy Google Cloud collection; local mode is independent")
    parser.add_argument("--enable-schedule", action="store_true",
                        help="Start/restart automatic sampling after deployment")
    args = parser.parse_args()
    try:
        deploy(args)
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
