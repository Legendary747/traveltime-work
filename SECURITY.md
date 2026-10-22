# Security and sensitive data

Do not put API keys, service-account credentials, real endpoints or commute records in a public issue or pull request. Redact route descriptions, timestamps and nearby incident details from examples as well.

For a vulnerability, use [GitHub's private vulnerability reporting](https://github.com/Legendary747/traveltime-work/security/advisories/new) if it is enabled. If it is unavailable, open an issue requesting a private contact channel without disclosing the vulnerability or sensitive details.

If a key has been exposed, revoke or rotate it in its provider console. Removing it from the latest file does not remove Git history, forks, logs or existing artifacts. Review those locations separately.

The project reads its Google API key from the environment or a local `.env` file. Optional cloud deployment uses Secret Manager and GitHub Workload Identity Federation. Local outputs are ignored by Git, but ignore rules do not protect files that were already committed. Before changing a repository's visibility, inspect every branch, commit, release attachment and Actions artifact for personal data. A data branch has the same visibility as its repository.

Real report automation refuses to run for public repositories. This protects future workflow writes; it does not remove previously published files or replace your own access and retention controls. See the [Google and data-handling guide](docs/google-setup.md).
