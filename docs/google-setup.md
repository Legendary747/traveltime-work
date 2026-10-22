# Your Google project, costs and data

[English README](../README.md) · [Configuration](configuration.md)

## Set up the Routes API

For live collection, use a Google Cloud project you control:

1. Follow Google's [Routes API setup](https://developers.google.com/maps/documentation/routes/get-api-key), including billing and enabling the Routes API.
2. Create a Google Maps Platform API key. Restrict it to Routes API and apply the appropriate application restrictions for your server environment. Browser-referrer restrictions are unsuitable for this Python server client; IP restrictions require a matching outbound IP.
3. Store it in the ignored `.env` file as `GOOGLE_MAPS_API_KEY=...`, or inject that environment variable through your own secret management. Never put it in the JSON config, a command argument, a screenshot or a commit.
4. Run `python -m traveltime --config config.local.json doctor`. This validates local configuration without checking the key against Google or charging a request.

The next real collection command contacts Google. An optional `once --test-direction am` request is useful for checking access, but is potentially billable and excluded from scheduled statistics. See Google's [API security guidance](https://developers.google.com/maps/api-security-best-practices) when selecting restrictions or rotating keys.

**A Maps API key is not a cloud deployment credential.** Local use needs no service-account JSON file, Firestore database or Cloud Run deployment. Optional cloud deployment separately uses your `gcloud` identity to provision resources, Secret Manager for the Maps key and endpoints, and Workload Identity Federation for GitHub Actions. See the [cloud guide](../deploy/gcp/README.md).

## Cost controls

The application calls Compute Routes with `TRAFFIC_AWARE_OPTIMAL`, which Google's [usage and billing documentation](https://developers.google.com/maps/documentation/routes/usage-and-billing) lists under the Pro feature tier. Consult the [current pricing page](https://developers.google.com/maps/billing-and-pricing/pricing) for your billing region and account. This project promises neither a free API allowance nor zero-cost cloud hosting.

The unchanged default schedule produces **120 scheduled attempts per eligible day**, or 2,640 for 22 days, before failures, manual tests or other applications are considered. Shorter intervals increase usage. Count each window's slots as `ceil(window_minutes / interval_minutes)`.

The application defaults to 4,500 attempts per billing month. It reserves an attempt before making a request, so failures and interrupted attempts can count against that ceiling. This counter:

- Uses `America/Los_Angeles` for month boundaries, independently of your collection timezone.
- Applies to one local database, or to the shared counter within one cloud Firestore project.
- Does not account for other databases, projects, applications or manual requests made outside this program.
- Is not an account-wide spending cap, a free quota, or a guarantee about the provider's final bill.

Set suitable provider-side API quotas and monitor billing in Google Cloud as well. Optional cloud services, builds and storage have their own pricing. Keep a finite observation period when you only need a short experiment.

## Data and service policies

This application retains numeric route observations, route descriptions, hashes and optional nearby incidents in SQLite or Firestore, then creates charts and exports. It does not retain the raw route polyline: that geometry is used transiently for incident matching. These choices reduce stored detail; they do not establish a right to retain provider content.

Google's [Routes API policies](https://developers.google.com/maps/documentation/routes/policies) restrict storage and caching of most content, with an exception for place IDs. Check the terms applicable to your account and intended retention before enabling live collection or distributing outputs; a personal key and private storage are not exemptions. EEA billing addresses have separate applicable terms. Reports include Google Maps attribution, but attribution alone does not grant storage or redistribution rights. Synthetic demo data is provided for trying the analysis without fetching provider content.

Keep real outputs under your control. Even without the exact configured endpoints, timestamps, distances, route descriptions and incident locations can reveal a routine. The source-code MIT license does not license Google or NDW data. Follow each provider's terms when retaining or sharing observations, and keep required attribution with any permitted use.

The repository ignores local configuration, databases and report directories. A public GitHub repository should contain source code and synthetic demonstrations only. The real-data report workflow runs only in private repositories, and its state helper checks live repository visibility before reading or writing a data branch. It does not upload real-report Actions artifacts. Merge these safeguards before changing visibility.

Older branches, Git history, release files and Actions artifacts are not removed by an upgrade. Review them before making any repository public: a branch called `commute-data` is not independently private. See [SECURITY.md](../SECURITY.md) for handling accidental disclosure.
