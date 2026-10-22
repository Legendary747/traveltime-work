# Optional Google Cloud deployment

[Main README](../../README.md) · [Configuration](../../docs/configuration.md) · [Google setup and data policies](../../docs/google-setup.md)

Local collection is the simplest way to start. This optional deployment runs a private Cloud Run service, triggers it with Cloud Scheduler and stores observations in Firestore. A code push runs verification; collection requires a manual deployment with scheduling enabled. You supply your own billing-enabled Google Cloud project and credentials.

The defaults remain `europe-west4`, resource prefix `traveltime`, experiment `commute-v1`, and Monday–Friday 06:00–09:00 / 15:00–18:00 in Europe/Amsterdam at three-minute intervals. These settings can be changed. Cloud services and API requests can incur charges; review [cost and retention requirements](../../docs/google-setup.md) before enabling collection.

## 1. Prepare your project and configuration

Use an account with the required administrative access to your own Google Cloud project. [Cloud Shell](https://shell.cloud.google.com/) includes `gcloud` and Python; clone or upload this repository there, or use your own authenticated development environment. Run commands from the repository root.

```sh
cp .env.example .env
cp config.example.json config.local.json
```

Edit `.env` to provide `GOOGLE_MAPS_API_KEY`. Edit the ignored JSON file to provide the two locations and your schedule. See the [Google setup guide](../../docs/google-setup.md): the Maps key authenticates route requests; it does not give deployment permissions. Do not put keys or personal locations directly in command arguments.

Bootstrap also configures GitHub Workload Identity Federation for **your** repository. Obtain its immutable numeric identifiers from GitHub, for example with an authenticated GitHub CLI:

```sh
gh api repos/YOUR_OWNER/YOUR_REPO --jq '{repository: .full_name, repository_id: .id, owner_id: .owner.id}'
```

Replace the placeholders below with your project ID, repository name and returned numeric IDs:

```sh
python3 -m deploy.gcp.bootstrap \
  --project YOUR_PROJECT_ID \
  --repository YOUR_OWNER/YOUR_REPO \
  --repository-id YOUR_REPOSITORY_ID \
  --owner-id YOUR_OWNER_ID \
  --config config.local.json
```

All three GitHub identity arguments are required; there are no maintainer-account defaults. Bootstrap:

- Enables the required APIs and creates the default Native-mode Firestore database when needed, with deletion protection for a new database.
- Creates separate runtime, scheduler, builder, reporter and deployer service accounts.
- Writes the Maps key and two locations to Secret Manager through standard input, not command arguments.
- Scopes GitHub federation to your repository and owner IDs, `main`, and the deployment/report workflows. No long-lived service-account JSON key is needed.
- Prints the GitHub Actions variables for optional workflow use.

It does not create the project, attach billing, create your Maps key, deploy the service or start route collection. Existing secrets are reused. To rotate them, update local settings and rerun with `--refresh-secrets`, then redeploy to pin the latest enabled numeric secret versions. Use `--region`, `--service` and `--experiment` consistently for customized resources.

## 2. Deploy, then choose when to start

For a configuration-driven deployment from Cloud Shell:

```sh
python3 -m deploy.gcp.deploy --project YOUR_PROJECT_ID --config config.local.json
```

This deploys with collection disabled and scheduler jobs paused. To start or resume the selected schedule, run:

```sh
python3 -m deploy.gcp.deploy --project YOUR_PROJECT_ID --config config.local.json --enable-schedule
```

Both scripts read `.env` by default; use `--env-file` for another path. Explicit environment settings override JSON. Runtime credentials still come from Secret Manager. The strict build allowlists upload only the Dockerfile, cloud dependencies and application Python sources; local JSON, `.env`, databases and reports are excluded.

Cloud Run requires IAM authentication. Scheduler calls it with OIDC; an unauthenticated browser receiving HTTP 403 is expected. The service uses zero minimum instances, one maximum instance and one request at a time. Each invocation handles a slot and exits rather than waiting through a collection window.

Scheduler jobs represent the configured windows exactly. The default creates `traveltime-am` and `traveltime-pm`. Offset times or intervals that do not divide an hour can need several jobs such as `traveltime-am-01`; this affects Scheduler usage and quotas. The script pauses its existing jobs before deployment, updates the current set and leaves retired jobs paused. A failed deployment leaves collection paused until you fix it and deploy again.

The runtime checks the original scheduled time, rejects late/out-of-scope slots and deduplicates repeated deliveries. Missing past traffic is not reconstructed. Retry attempts for failed collection are disabled. Cloud Scheduler's wall-clock/DST behaviour can still cause missing deliveries around clock changes; use the stored gaps when interpreting reports. Arbitrary Scheduler “Force run” invocations may be rejected because they are not valid slots.

## Optional deployment through GitHub Actions

Under repository **Settings → Secrets and variables → Actions → Variables**, set the bootstrap outputs:

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | Your project ID |
| `GCP_REGION` | Your region; default `europe-west4` |
| `GCP_SERVICE` | Your resource prefix; default `traveltime` |
| `CLOUD_EXPERIMENT` | Your observation series; default `commute-v1` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Bootstrap's full provider path |
| `GCP_REPORT_SERVICE_ACCOUNT` | Bootstrap's reporter identity |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Bootstrap's deployment identity |
| `ENABLE_CLOUD_REPORTS` | `true` enables scheduled reports **only in a private repository** |

Set schedule variables using the [environment names in the configuration guide](../../docs/configuration.md). The workflow supports `COMMUTE_TIMEZONE`, `MONITOR_WEEKDAYS`, `AM_START`, `AM_END`, `PM_START`, `PM_END`, `SAMPLE_INTERVAL_MINUTES`, date filters, `MAX_LATENESS_SECONDS`, `MONTHLY_REQUEST_LIMIT`, NDW options and report thresholds. Leave `MAX_LATENESS_SECONDS` unset to use the interval-aware default. Use a suitable `MONITOR_END_DATE` for a finite experiment.

Your local JSON and `.env` are **not copied to Actions**. Set these variables explicitly when using workflow deployments; otherwise they use defaults. The API key and endpoint locations stay in Secret Manager, not GitHub variables. Updating a schedule variable takes effect after redeployment.

Open **Actions → Deploy Google Cloud → Run workflow**, choose `main` and decide whether to check `enable_schedule`. It is unchecked by default. A successful enabled deployment starts collecting at the next eligible slot; observations appear under `commute_experiments/<experiment>/samples` in Firestore. Identity and permission changes can take time to propagate after first setup.

## View reports without publishing observations

For either public or private source repositories, export cloud observations to your own machine:

```sh
python -m pip install -r requirements.txt -r requirements-cloud.txt
gcloud auth application-default login
python -m traveltime.cloud_export --project YOUR_PROJECT_ID --experiment commute-v1 --db data/cloud-commute.sqlite3
python -m traveltime --config config.local.json --db data/cloud-commute.sqlite3 --output reports/cloud report
```

Use an identity with Firestore read access and the configuration that produced the data. Exporting reads existing observations and makes no Routes API request. Open `reports/cloud/index.html` locally. An empty experiment reports “No cloud observations yet” rather than overwriting a backup with an empty dataset. Repeated exports merge observations; a different experiment requires a different database path.

The **Daily commute report** workflow is optional and private-repository-only. It checks live visibility before accessing observations and before writing its `commute-data` branch. It does not upload real-data Actions artifacts. In a private repository, download or check out that branch privately to view its reports. Synthetic CI artifacts remain available separately.

The included reporting trigger is Monday–Friday at **18:15 Europe/Amsterdam**, independent of collection settings. For other windows/timezones, run the report workflow manually after collection or adapt its trigger. A delayed report does not stop Firestore collection. `ENABLE_CLOUD_REPORTS=false` disables scheduled reporting only; manual reports remain available in a private repository.

Merge the public-data safeguards before making the source repository public. Public repositories skip this report workflow; cloud collection can continue independently and you can export locally. Review existing branches, history, release attachments and Actions artifacts before changing visibility. **A data branch has the same visibility as the repository.** The guards do not remove historical data or resolve provider content-retention requirements.

## Pause, resume and change configuration

To pause immediately, list jobs in your project and pause every active job belonging to this deployment. For the unchanged default:

```sh
gcloud scheduler jobs pause traveltime-am --location=europe-west4 --project=YOUR_PROJECT_ID
gcloud scheduler jobs pause traveltime-pm --location=europe-west4 --project=YOUR_PROJECT_ID
```

For a customized schedule, inspect `gcloud scheduler jobs list --location=YOUR_REGION --project=YOUR_PROJECT_ID` and include its numbered jobs. Already running requests may finish. Redeploy with `--enable-schedule` to reconcile and resume only the current job set; do not resume retired jobs.

Deploying without `--enable-schedule` also disables collection inside the service. In that case, merely resuming a Scheduler job is insufficient: redeploy with the flag. Setting `ENABLE_CLOUD_REPORTS=false` never stops collection. An end date stops further Routes queries after that date; pause Scheduler too if you want to stop empty triggers.

Use a new experiment name for changed locations, timezone, weekdays, windows or interval. Preserve previous experiments and use separate local export databases. All experiments in the same Firestore project share the cloud monthly attempt counter. A code update does not clear history or reset that counter. Avoid simultaneously collecting the same route locally and in the cloud: their deduplication and local counters are separate.

## Troubleshooting

| Symptom | Check |
|---|---|
| Deployment authentication fails | Project/provider/service-account variables, your repository IDs, `main` branch and IAM propagation |
| Service cannot start | Secret versions, runtime access and Cloud Run logs |
| Firestore access denied | Default Native database and runtime/reporter roles |
| `configuration_error` | Stored route/schedule differs; select a new experiment instead of deleting metadata |
| Gaps in charts | Scheduler delivery, lateness, `pending`/`error` statuses and excluded dates |
| Private backup refused | Live repository visibility, GitHub authentication, push origin and branch permissions |
| Reports skipped after publication | Expected safeguard; use a local cloud export |

Official references: [Cloud Scheduler authentication](https://docs.cloud.google.com/scheduler/docs/http-target-auth), [Cloud Run secrets](https://docs.cloud.google.com/run/docs/configuring/services/secrets), [GitHub federation action](https://github.com/google-github-actions/auth), [Cloud Run pricing](https://cloud.google.com/run/pricing), [Scheduler pricing](https://cloud.google.com/scheduler/pricing), [Firestore quotas](https://docs.cloud.google.com/firestore/quotas).
