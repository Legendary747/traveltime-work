"""One-time setup from an authenticated Cloud Shell. No Google Routes requests."""
import os
from pathlib import Path
import re

from traveltime.config import Config, load_dotenv
from .common import Gcloud, SECRETS, account, arguments, validate

APIS = (
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com", "firestore.googleapis.com",
    "secretmanager.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
    "sts.googleapis.com", "routes.googleapis.com",
)


def bootstrap(args, cloud=None):
    validate(args)
    if not (args.repository_id.isdigit() and args.owner_id.isdigit()
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)):
        raise ValueError("A repository owner/name and immutable numeric GitHub IDs are required.")
    load_dotenv(Path(args.env_file))
    config = Config.from_env()
    config.validate(credentials=True)
    cloud = cloud or Gcloud(args.project)
    cloud("services", "enable", *APIS)
    project = cloud("projects", "describe", args.project)
    number = str(project["projectNumber"])

    databases = cloud("firestore", "databases", "list") or []
    if isinstance(databases, dict):
        databases = databases.get("databases", [])
    default = next((d for d in databases if d["name"].endswith("/(default)")), None)
    if default:
        if default.get("type") != "FIRESTORE_NATIVE":
            raise ValueError("The default Firestore database must use Native mode; use a dedicated project.")
    else:
        cloud("firestore", "databases", "create", "--database=(default)",
              f"--location={args.region}", "--type=firestore-native", "--delete-protection")

    existing_accounts = {x["email"] for x in cloud("iam", "service-accounts", "list") or []}
    for suffix in ("runtime", "scheduler", "builder", "reporter", "deployer"):
        if account(args, suffix) not in existing_accounts:
            cloud("iam", "service-accounts", "create", f"{args.service}-{suffix}",
                  f"--display-name=Traveltime {suffix}")
    cloud.project_role(account(args, "runtime"), "roles/datastore.user")
    cloud.project_role(account(args, "reporter"), "roles/datastore.viewer")
    cloud.project_role(account(args, "builder"), "roles/run.builder")
    for role in ("roles/run.admin", "roles/run.sourceDeveloper",
                 "roles/cloudscheduler.admin", "roles/serviceusage.serviceUsageConsumer"):
        cloud.project_role(account(args, "deployer"), role)
    for suffix in ("runtime", "scheduler", "builder"):
        cloud("iam", "service-accounts", "add-iam-policy-binding", account(args, suffix),
              f"--member=serviceAccount:{account(args, 'deployer')}",
              "--role=roles/iam.serviceAccountUser")

    secrets = {x["name"].rsplit("/", 1)[-1] for x in cloud("secrets", "list") or []}
    for env_name, suffix in SECRETS.items():
        secret = f"{args.service}-{suffix}"
        if secret not in secrets:
            cloud("secrets", "create", secret, "--replication-policy=user-managed",
                  f"--locations={args.region}", "--data-file=-", stdin=os.environ[env_name].strip())
        elif args.refresh_secrets:
            cloud("secrets", "versions", "add", secret, "--data-file=-",
                  stdin=os.environ[env_name].strip())
        for identity, role in (("runtime", "roles/secretmanager.secretAccessor"),
                               ("deployer", "roles/secretmanager.viewer")):
            cloud("secrets", "add-iam-policy-binding", secret,
                  f"--member=serviceAccount:{account(args, identity)}", f"--role={role}")

    pool = f"{args.service}-github"
    pools = cloud("iam", "workload-identity-pools", "list", "--location=global") or []
    if not any(x["name"].endswith("/" + pool) for x in pools):
        cloud("iam", "workload-identity-pools", "create", pool, "--location=global",
              "--display-name=Traveltime GitHub")
    workflow_prefix = f"{args.repository}/.github/workflows"
    reporter = f"{workflow_prefix}/commute.yml@refs/heads/main"
    deployer = f"{workflow_prefix}/deploy-cloud.yml@refs/heads/main"
    condition = (
        f"assertion.repository_id == '{args.repository_id}' && "
        f"assertion.repository_owner_id == '{args.owner_id}' && "
        "assertion.ref == 'refs/heads/main' && "
        f"assertion.workflow_ref in ['{reporter}', '{deployer}']")
    mapping = ("google.subject=assertion.sub,"
               f"attribute.access=assertion.workflow_ref == '{reporter}' ? 'reporter' : 'deployer'")
    providers = cloud("iam", "workload-identity-pools", "providers", "list",
                      "--location=global", f"--workload-identity-pool={pool}") or []
    operation = "update-oidc" if any(x["name"].endswith("/github") for x in providers) else "create-oidc"
    cloud("iam", "workload-identity-pools", "providers", operation, "github",
          "--location=global", f"--workload-identity-pool={pool}",
          "--issuer-uri=https://token.actions.githubusercontent.com",
          f"--attribute-mapping={mapping}", f"--attribute-condition={condition}")
    pool_path = f"projects/{number}/locations/global/workloadIdentityPools/{pool}"
    for suffix in ("reporter", "deployer"):
        cloud("iam", "service-accounts", "add-iam-policy-binding", account(args, suffix),
              f"--member=principalSet://iam.googleapis.com/{pool_path}/attribute.access/{suffix}",
              "--role=roles/iam.workloadIdentityUser")
    variables = {
        "GCP_PROJECT_ID": args.project, "GCP_REGION": args.region,
        "GCP_SERVICE": args.service, "CLOUD_EXPERIMENT": args.experiment,
        "GCP_WORKLOAD_IDENTITY_PROVIDER": pool_path + "/providers/github",
        "GCP_REPORT_SERVICE_ACCOUNT": account(args, "reporter"),
        "GCP_DEPLOY_SERVICE_ACCOUNT": account(args, "deployer"),
        "ENABLE_CLOUD_REPORTS": "true",
    }
    print("\nAdd these repository Actions VARIABLES (not secrets):")
    for key, value in variables.items():
        print(f"{key}={value}")
    print("\nSetup complete. No collection service or schedule has started.")
    print("Run the Deploy Google Cloud workflow on main. IAM propagation can take several minutes.")
    return variables


def main():
    parser = arguments("Bootstrap private Cloud Run, Firestore, Secret Manager and GitHub federation")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--refresh-secrets", action="store_true",
                        help="Add new versions from .env to existing secrets")
    parser.add_argument("--repository", default="Legendary747/traveltime-work")
    parser.add_argument("--repository-id", default="1405350006")
    parser.add_argument("--owner-id", default="96092356")
    args = parser.parse_args()
    try:
        bootstrap(args)
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
