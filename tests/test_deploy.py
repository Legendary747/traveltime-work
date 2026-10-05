from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.gcp.bootstrap import bootstrap
from deploy.gcp.deploy import deploy
from test_monitor import CONFIG


def options(**overrides):
    values = dict(project="sample-project", region="europe-west4", service="traveltime",
                  experiment="commute-v1", enable_schedule=True,
                  repository="Legendary747/traveltime-work", repository_id="1405350006",
                  owner_id="96092356", env_file="nonexistent-test-env", refresh_secrets=False)
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
        for value in ("1405350006", "96092356", "refs/heads/main",
                      "/commute.yml@", "/deploy-cloud.yml@"):
            self.assertIn(value, condition)
        self.assertIn("/providers/github", result["GCP_WORKLOAD_IDENTITY_PROVIDER"])
        # Bootstrap creates identities, not a paid navigation query or active schedule.
        self.assertFalse(any(c[:2] == ("run", "deploy") for c in cloud.calls))
        self.assertFalse(any(c[:2] == ("scheduler", "jobs") for c in cloud.calls))
