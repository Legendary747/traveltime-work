"""Small gcloud wrapper: argument arrays, never shell interpolation."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SECRETS = {
    "GOOGLE_MAPS_API_KEY": "google-maps-api-key",
    "COMMUTE_HOME": "home",
    "COMMUTE_WORK": "work",
}


def arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project", required=True, help="Billing-enabled Google Cloud project ID")
    parser.add_argument("--region", default="europe-west4")
    parser.add_argument("--service", default="traveltime")
    parser.add_argument("--experiment", default="commute-v1")
    return parser


def validate(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        raise ValueError("Use the Google Cloud project ID, not its display name or number.")
    if not re.fullmatch(r"[a-z](?:[a-z0-9-]{0,17}[a-z0-9])?", args.service):
        raise ValueError("--service must be a lowercase identifier of at most 19 characters.")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", args.region):
        raise ValueError("Use a regional Google Cloud location, for example europe-west4.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.experiment):
        raise ValueError("Invalid experiment ID.")


class Gcloud:
    def __init__(self, project):
        self.project = project

    def __call__(self, *args, stdin=None):
        # Values sent on stdin (secret payloads) are never echoed or put in process args.
        print("gcloud " + " ".join(args[:3]), flush=True)
        try:
            result = subprocess.run(
                ["gcloud", *args, "--project", self.project, "--quiet", "--format=json"],
                input=stdin, capture_output=True, text=True, cwd=ROOT, check=False)
        except FileNotFoundError:
            raise ValueError("gcloud is required. Run this in Google Cloud Shell.") from None
        if result.returncode:
            if stdin is None:
                print(result.stderr.strip())
            raise ValueError("gcloud operation failed. Fix the reported access/configuration issue and rerun.")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def project_role(self, email, role):
        self("projects", "add-iam-policy-binding", self.project,
             f"--member=serviceAccount:{email}", f"--role={role}", "--condition=None")


def account(args, suffix):
    return f"{args.service}-{suffix}@{args.project}.iam.gserviceaccount.com"
