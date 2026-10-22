# Travel Time Analyser

[![Tests](https://github.com/Legendary747/traveltime-work/actions/workflows/ci.yml/badge.svg)](https://github.com/Legendary747/traveltime-work/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Understand how departure time changes your commute.**

Travel Time Analyser is a configurable Python project for exploring traffic-aware driving estimates between two places. It grew from a practical question: *would leaving a little earlier make the journey more predictable?* It turns repeated observations into daily curves, weekday comparisons, heatmaps and CSV exports, with an optional Netherlands traffic-incident overlay.

[中文说明](docs/README.zh-CN.md) · [Configuration](docs/configuration.md) · [Google setup](docs/google-setup.md) · [Optional cloud deployment](deploy/gcp/README.md)

## What it does

- Uses the official Google Routes API with your own API key and billing project.
- Lets you choose two locations, a timezone, weekdays, outbound/return windows and sampling frequency in a local JSON file.
- Compares traffic-aware estimates with the same route's static-duration baseline; reports median and P90 patterns across days.
- Records missing and failed observations explicitly, without backfilling past traffic or silently retrying a billable request.
- Runs locally, or optionally on Cloud Run + Cloud Scheduler + Firestore.
- Includes a deterministic, clearly labelled synthetic demo and automated tests that need no Google credentials.

This is an analysis tool, not measured journey tracking: estimates do not include parking or walking, and nearby incidents do not establish the cause of a delay. Live observations are retained by the application; review Google's applicable [content-storage policies](docs/google-setup.md#data-and-service-policies) before using that mode. The MIT license covers this project's code, not provider data.

## Try the demo first

Use Python 3.11 or newer; Python 3.12 is used in CI. From the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m traveltime --db demo-output/demo.sqlite3 --output demo-output/report demo --days 10
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell. Open `demo-output/report/index.html` in your browser. The demo uses no network, credentials or paid services. Use a new database path when generating another demo.

## Use your own locations

1. Follow the [Google setup guide](docs/google-setup.md) to enable Routes API in your own billing-enabled project and create a restricted API key.
2. Copy the examples:

   ```sh
   cp .env.example .env
   cp config.example.json config.local.json
   ```

3. Put the key in `.env` as `GOOGLE_MAPS_API_KEY=...`. Edit `config.local.json` to fill `home` and `work`, and choose your schedule. These local files are ignored by Git. Locations can be an address, `latitude,longitude`, or `place_id:...`; using a suitable vehicle entrance makes results more relevant.
4. Validate without making a request:

   ```sh
   python -m traveltime --config config.local.json doctor
   ```

5. Start collecting when you are ready to make potentially billable API requests:

   ```sh
   python -m traveltime --config config.local.json run
   ```

Keep the computer awake and connected. Stop with Ctrl+C. The default schedule is Monday–Friday, **06:00–09:00 home → work** and **15:00–18:00 work → home**, every **3 minutes**, in **Europe/Amsterdam**. End times are exclusive. The configuration guide covers custom times, weekends, date ranges and frequency.

Environment variables take precedence over JSON, so keep only the API key active in `.env` when configuring everything else in JSON. Existing environment-only installations remain supported.

## Read the results

The default local database is `data/commute.sqlite3`. Reports are refreshed after a collection window and when the collector stops. You can also generate them without contacting an API:

```sh
python -m traveltime --config config.local.json --db data/commute.sqlite3 --output reports report
```

Open `reports/index.html`. The output includes:

| Output | What it helps you see |
|---|---|
| Daily direction charts | Estimated duration, static baseline, thresholds and nearby incident observations |
| Weekday profiles | Median and P90 for the same departure minute across dates; P90 requires at least five observed days |
| Heatmaps | Changes across dates and departure times, with missing samples shown explicitly |
| CSV / JSON exports | `samples.csv`, `daily_summary.csv`, `nearby_events.csv` and `summary.json` for your own analysis |

Keep real databases and reports private: timestamps, distances, route descriptions and incident locations can reveal a travel pattern. Publishing source code does not make those outputs safe to publish. The cloud report workflow is restricted to private repositories; see [data handling](docs/google-setup.md#data-and-service-policies).

## More controls

| Command | Behaviour |
|---|---|
| `doctor` | Validate configuration; no network requests |
| `once` | Collect only if a scheduled slot is currently due |
| `once --test-direction am` | Make a real manual outbound request now; excluded from scheduled statistics |
| `peak` | Run the current window, or one beginning within ten minutes |
| `run` | Continue across the configured schedule |
| `report` | Build reports from an existing database; no API requests |
| `demo --days 10` | Generate synthetic observations in a new database |

Global options such as `--config`, `--db` and `--output` go **before** the command. Use `pm` for a manual return request. Each real Google request may incur charges; the default local ceiling of 4,500 attempts per billing month is an application limit, not a free allowance or an account-wide spending cap. [Cost controls and data policies →](docs/google-setup.md)

## Development

```sh
python -m pip install -r requirements.txt -r requirements-cloud.txt
python -m unittest discover -s tests -v
```

CI runs the test suite and generates a synthetic demo. The badge above reports the actual GitHub workflow status. See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for safe issue reporting.

Made by [Legendary747](https://github.com/Legendary747). Also see [H2S](https://github.com/Legendary747/H2S), a browser-based housing-search assistant.
