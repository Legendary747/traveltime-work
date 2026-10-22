from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from deploy.gcp.bootstrap import bootstrap
from deploy.gcp.deploy import deploy, schedule_jobs
from traveltime.config import Config
from test_monitor import CONFIG


def options(**overrides):
    values = dict(project="sample-project", region="europe-west4", service="traveltime",
                  experiment="commute-v1", enable_schedule=True,
                  repository="example-owner/travel-time", repository_id="12345678",
                  owner_id="87654321", env_file="nonexistent-test-env", refresh_secrets=False)
    return SimpleNamespace(**{**values, **overrides})


class FakeGcloud:
    def __init__(self, jobs=(), build_failure=False):
        self.calls = []
        self.jobs = list(jobs)
        self.build_failure = build_failure
        self.runtime = None
        self.secret_payloads = []

    def __call__(self, *args, stdin=None):
        self.calls.append(args)
        if stdin:
            self.secret_payloads.append(stdin)
        if args[:2] == ("projects", "describe"):
            return {"projectNumber": "1234567890"}
        if args[:3] == ("scheduler", "jobs", "list"):
            return self.jobs
        if args[:3] == ("secrets", "versions", "list"):
            return [{"name": f"projects/sample-project/secrets/{args[3]}/versions/7"}]
        if args[:2] == ("run", "deploy"):
            self.runtime = json.loads(Path(next(x.split("=", 1)[1] for x in args
                                                if x.startswith("--env-vars-file="))).read_text())
            if self.build_failure:
                raise ValueError("Synthetic deployment failure")
        if args[:3] == ("run", "services", "describe"):
            return {"status": {"url": "https://test-example.run.app"}}
        return []

    def project_role(self, email, role):
        self("project_role", email, role)


class DeploymentTests(unittest.TestCase):
    def test_cron_jobs_match_exact_configured_slots_without_duplicates(self):
        def expand(field, upper):
            if field == "*":
                return set(range(upper + 1))
            if field.startswith("*/"):
                return set(range(0, upper + 1, int(field[2:])))
            if "-" in field:
                start, end = map(int, field.split("-"))
                return set(range(start, end + 1))
            return {int(value) for value in field.split(",")}

        # Every supported interval, including those that do not divide an hour,
        # must stay anchored to the beginning of each configured window.
        for interval in range(1, 61):
            config = replace(Config(), interval_minutes=interval, max_lateness_seconds=0,
                             am_start="05:07", am_end="09:43", pm_start="17:22",
                             pm_end="22:19", weekdays=(0, 2, 6), timezone="Asia/Tokyo")
            jobs = schedule_jobs(config)
            actual = set()
            for suffix, expression in jobs.items():
                minute, hour, monthday, month, weekday = expression.split()
                self.assertEqual((monthday, month), ("*", "*"))
                for day in expand(weekday, 6):
                    for h in expand(hour, 23):
                        for m in expand(minute, 59):
                            slot = (suffix.split("-")[0], (day + 6) % 7, h * 60 + m)
                            self.assertNotIn(slot, actual)
                            actual.add(slot)
            expected = {(direction, day, minute)
                        for direction, start, end in config.windows
                        for day in config.weekdays
                        for minute in range(start, end, interval)}
            with self.subTest(interval=interval):
                self.assertEqual(actual, expected)

    def test_cloud_uses_two_three_minute_jobs_and_pinned_secret_versions(self):
        cloud = FakeGcloud()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()):
            deploy(options(), cloud)
        build = next(c for c in cloud.calls if c[:2] == ("run", "deploy"))
        self.assertIn("--no-allow-unauthenticated", build)
        self.assertIn("--invoker-iam-check", build)
        self.assertIn("--cpu-throttling", build)
        self.assertIn("--min=0", build)
        self.assertIn("--max=1", build)
        secret_arg = next(c for c in build if c.startswith("--set-secrets="))
        self.assertNotIn("latest", secret_arg)
        self.assertEqual(secret_arg.count(":7"), 3)
        created = [c for c in cloud.calls if c[:3] == ("scheduler", "jobs", "create")]
        self.assertEqual(len(created), 2)
        self.assertIn("--schedule=*/3 6-8 * * 1-5", created[0])
        self.assertIn("--schedule=*/3 15-17 * * 1-5", created[1])
        for job in created:
            self.assertIn("--time-zone=Europe/Amsterdam", job)
            self.assertIn("--oidc-token-audience=https://test-example.run.app", job)
            self.assertIn("--max-retry-attempts=0", job)

    def test_paused_deployment_disables_runtime_and_both_new_jobs(self):
        cloud = FakeGcloud()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()):
            deploy(options(enable_schedule=False), cloud)
        self.assertEqual(cloud.runtime["COLLECTION_ENABLED"], "false")
        self.assertEqual(len([c for c in cloud.calls if c[:3] == ("scheduler", "jobs", "pause")]), 2)
        self.assertFalse(any(c[:3] == ("scheduler", "jobs", "resume") for c in cloud.calls))

    def test_failed_redeploy_leaves_existing_schedules_paused(self):
        jobs = [{"name": f"projects/sample/locations/europe-west4/jobs/traveltime-{d}",
                 "state": "ENABLED"} for d in ("am", "pm")]
        cloud = FakeGcloud(jobs, build_failure=True)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            deploy(options(), cloud)
        self.assertEqual(len([c for c in cloud.calls if c[:3] == ("scheduler", "jobs", "pause")]), 2)
        self.assertFalse(any(c[:3] == ("scheduler", "jobs", "resume") for c in cloud.calls))

    def test_custom_config_reconciles_only_current_jobs_and_propagates_settings(self):
        jobs = [{"name": f"projects/sample/locations/europe-west4/jobs/{name}", "state": "ENABLED"}
                for name in ("traveltime-am", "traveltime-pm", "traveltime-am-01",
                             "traveltime-am-23", "another-service-am")]
        cloud = FakeGcloud(jobs)
        settings = {"schema_version": 1, "timezone": "Asia/Tokyo", "weekdays": [1, 4, 6],
                    "am_start": "06:05", "am_end": "09:11", "pm_start": "16:13",
                    "pm_end": "18:31", "interval_minutes": 7, "ndw_enabled": False}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(settings))
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()):
                config = Config.from_env(path)
                deploy(options(config=path), cloud)
        self.assertEqual(cloud.runtime["SAMPLE_INTERVAL_MINUTES"], "7")
        self.assertEqual(cloud.runtime["COMMUTE_TIMEZONE"], "Asia/Tokyo")
        self.assertEqual(cloud.runtime["MONITOR_WEEKDAYS"], "1,4,6")
        self.assertEqual(cloud.runtime["AM_START"], "06:05")
        self.assertEqual(cloud.runtime["NDW_ENABLED"], "false")
        paused = {c[3] for c in cloud.calls if c[:3] == ("scheduler", "jobs", "pause")}
        self.assertEqual(paused, {"traveltime-am", "traveltime-pm", "traveltime-am-01", "traveltime-am-23"})
        resumed = {c[3] for c in cloud.calls if c[:3] == ("scheduler", "jobs", "resume")}
        self.assertEqual(resumed, {"traveltime-am-01"})
        scheduled = {c[4]: next(a[11:] for a in c if a.startswith("--schedule="))
                     for c in cloud.calls if c[:3] in (("scheduler", "jobs", "update"),
                                                      ("scheduler", "jobs", "create"))}
        self.assertEqual(scheduled, {f"traveltime-{name}": cron for name, cron in schedule_jobs(config).items()})
        build_index = next(i for i, c in enumerate(cloud.calls) if c[:2] == ("run", "deploy"))
        self.assertTrue(all(i < build_index for i, c in enumerate(cloud.calls)
                            if c[:3] == ("scheduler", "jobs", "pause")))

    def test_bootstrap_secrets_only_stdin_and_federation_bound_to_main_and_repo_ids(self):
        cloud = FakeGcloud()
        env = {"GOOGLE_MAPS_API_KEY": CONFIG.api_key, "COMMUTE_HOME": CONFIG.home,
               "COMMUTE_WORK": CONFIG.work}
        with patch.dict("os.environ", env, clear=True), redirect_stdout(io.StringIO()):
            result = bootstrap(options(), cloud)
        self.assertCountEqual(cloud.secret_payloads, env.values())
        command_text = json.dumps(cloud.calls)
        for value in env.values():
            self.assertNotIn(value, command_text)
        provider = next(c for c in cloud.calls if "create-oidc" in c)
        condition = next(x for x in provider if x.startswith("--attribute-condition="))
        for value in ("12345678", "87654321", "refs/heads/main",
                      "/commute.yml@", "/deploy-cloud.yml@"):
            self.assertIn(value, condition)
        self.assertIn("/providers/github", result["GCP_WORKLOAD_IDENTITY_PROVIDER"])
        # Bootstrap creates identities, not a paid navigation query or active schedule.
        self.assertFalse(any(c[:2] == ("run", "deploy") for c in cloud.calls))
        self.assertFalse(any(c[:2] == ("scheduler", "jobs") for c in cloud.calls))

    def test_bootstrap_uploads_json_endpoints_and_environment_key(self):
        cloud = FakeGcloud()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            settings = {"schema_version": 1, "home": "Example home", "work": "Example work"}
            path.write_text(json.dumps(settings))
            with patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}, clear=True), redirect_stdout(io.StringIO()):
                bootstrap(options(config=path), cloud)
        self.assertCountEqual(cloud.secret_payloads, ["test-key", "Example home", "Example work"])
        for value in cloud.secret_payloads:
            self.assertNotIn(value, json.dumps(cloud.calls))
