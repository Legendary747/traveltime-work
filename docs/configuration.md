# Configuration

[English README](../README.md) · [中文说明](README.zh-CN.md)

Copy [`config.example.json`](../config.example.json) to `config.local.json`. Fill the two empty location fields; keep the API key in `.env` or the process environment.

```sh
python -m traveltime --config config.local.json doctor
python -m traveltime --config config.local.json run
```

You can instead set `TRAVELTIME_CONFIG_FILE=config.local.json`. `--config` takes precedence over that path. For individual settings, the precedence is **process environment → `.env` → JSON → defaults**. Existing environment-only setups continue to work. `.env` does not overwrite a value already set in the process environment.

JSON must contain `"schema_version": 1`; other fields are optional. Unknown fields, incorrect types and invalid schedules are rejected. JSON does not accept comments. An API key is deliberately not a valid JSON field. `doctor` checks setup without contacting Google and reports only whether sensitive values are configured.

## Locations and schedule

| JSON field | Environment variable | Default / format |
|---|---|---|
| `home` | `COMMUTE_HOME` | Required for collection; address, `latitude,longitude`, or `place_id:...` |
| `work` | `COMMUTE_WORK` | Required for collection; same formats |
| `timezone` | `COMMUTE_TIMEZONE` | `Europe/Amsterdam`; IANA timezone name |
| `weekdays` | `MONITOR_WEEKDAYS` | `[0,1,2,3,4]`; Monday = 0, Sunday = 6; environment: `0,1,2,3,4` |
| `am_start` / `am_end` | `AM_START` / `AM_END` | `06:00` / `09:00`; outbound home → work |
| `pm_start` / `pm_end` | `PM_START` / `PM_END` | `15:00` / `18:00`; return work → home |
| `interval_minutes` | `SAMPLE_INTERVAL_MINUTES` | `3`; integer from 1 to 60 |
| `start_date` / `end_date` | `MONITOR_START_DATE` / `MONITOR_END_DATE` | `null`; optional inclusive `YYYY-MM-DD` dates |
| `exclude_dates` | `EXCLUDE_DATES` | `[]`; ISO dates; comma-separated in environment |

The names `home` and `work` identify the two endpoints; they can be any two places. The `am` and `pm` identifiers select direction, and their clock times are configurable.

Both windows must stay within the same local calendar day, with start earlier than end and no overlap. End times are exclusive. Slots are anchored to each window's start: a `07:10`–`08:00` window at a 15-minute interval means `07:10`, `07:25`, `07:40`, `07:55`. Overnight windows are not supported. Holidays remain eligible unless you exclude them.

Timezone rules follow the IANA database. Nonexistent local times during a spring clock change are skipped; a repeated local slot during a fall clock change is collected once, at its first occurrence. Optional Cloud Scheduler follows its own wall-clock delivery behaviour, so gaps around a clock change can still occur.

For example, this smaller file changes the schedule while retaining other defaults:

```json
{
  "schema_version": 1,
  "home": "YOUR_ORIGIN",
  "work": "YOUR_DESTINATION",
  "timezone": "Europe/London",
  "weekdays": [0, 2, 4],
  "am_start": "07:10",
  "am_end": "09:00",
  "pm_start": "16:30",
  "pm_end": "18:30",
  "interval_minutes": 10,
  "ndw_enabled": false
}
```

Replace the location placeholders before collection. This example schedules 11 outbound + 12 return slots on each eligible day.

## Limits and report settings

| JSON field | Environment variable | Default / meaning |
|---|---|---|
| `max_lateness_seconds` | `MAX_LATENESS_SECONDS` | `60`, or `59` with a one-minute interval; must be shorter than the interval |
| `monthly_request_limit` | `MONTHLY_REQUEST_LIMIT` | `4500`; maximum recorded Google request attempts per billing month |
| `ndw_enabled` | `NDW_ENABLED` | `true`; fetch Netherlands incident observations; set `false` outside its coverage |
| `ndw_corridor_meters` | `NDW_CORRIDOR_METERS` | `250`; maximum distance from route for incident matching |
| `delay_threshold_minutes` | `DELAY_THRESHOLD_MINUTES` | `15`; report threshold for traffic duration minus static duration |
| `trip_threshold_minutes` | `TRIP_THRESHOLD_MINUTES` | `75`; report threshold for total estimated driving time |

The lateness default is computed when omitted. If you explicitly set it to 60 and reduce the interval to one minute, reduce lateness too or omit that field. Booleans are JSON `true`/`false`; use `true`/`false` in environment configuration as well.

Late observations are skipped instead of querying later and labelling the result with an earlier time. The app reserves an attempt before a Google request; failures count conservatively against its ceiling and are not automatically retried. See [cost controls](google-setup.md#cost-controls).

NDW observations are optional context for routes in the Netherlands. A feed older than ten minutes is marked stale; unavailable and missing data are not treated as “no incidents.” Matching considers a corridor around the returned route, not proof that an event affected your lane or caused your delay. [NDW documentation](https://docs.ndw.nu/producten/situatieberichten-v3/)

## Existing installations and data

The original defaults are unchanged. You can continue using your existing `.env` without a JSON file. Preserve your database and configuration when upgrading.

Changing endpoints, timezone, weekdays, collection windows or interval changes the observation series. Use a new `--db` path for local collection, or a new cloud `--experiment` / `CLOUD_EXPERIMENT`, instead of combining incompatible observations. The collector rejects a mismatched stored configuration. Do not delete old metadata to bypass that check.

Generate a report using the configuration that produced its observations. Local and cloud collection have separate attempt counters and deduplication; running both for the same route can make duplicate paid requests. Multiple cloud experiments in the same Firestore project share the cloud monthly counter.
