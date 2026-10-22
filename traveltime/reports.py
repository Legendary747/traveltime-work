from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import csv
import html
import json
import math
import statistics

from .db import read_samples, connect
from .config import Config
from .schedule import slots_on


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DIRECTION_NAMES = {"am": "Home → work", "pm": "Work → home"}


def quantile(values, p):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def events(row):
    if row.get("ndw_status") != "ok":
        return []
    try:
        return json.loads(row.get("ndw_json") or "{}").get("events", [])
    except (ValueError, AttributeError):
        return []


def day_summary(rows, expected, config):
    good = [r for r in rows if r["status"] == "ok"]
    times = [r["duration_s"] / 60 for r in good]
    fresh = [r for r in good if r["ndw_status"] == "ok"]
    accidents = {e["id"] for r in fresh for e in events(r) if e["category"] == "accident"}
    delayed = sum(r["duration_s"] - r["static_s"] >= config.delay_threshold_minutes * 60
                  for r in good)
    return {
        "expected_slots": expected, "valid_samples": len(good),
        "coverage_pct": 100 * len(good) / expected if expected else None,
        "median_minutes": quantile(times, .5), "p90_minutes": quantile(times, .9),
        "max_minutes": max(times) if times else None,
        "delay_sample_pct": 100 * delayed / len(good) if good else None,
        "trip_over_threshold_pct": (
            100 * sum(t > config.trip_threshold_minutes for t in times) / len(times)
            if times else None),
        "ndw_fresh_samples": len(fresh),
        "nearby_accident_events": len(accidents) if fresh else None,
        "nearby_accident_report_sample_pct": (
            100 * sum(any(e["category"] == "accident" for e in events(r)) for r in fresh)
            / len(fresh) if fresh else None),
    }


def profile(rows, weekday, direction, config=None):
    """One observation per date/slot. P90 across DAYS, not pooled peak samples."""
    config = config or Config()
    buckets = defaultdict(dict)
    for row in rows:
        day = date.fromisoformat(row["local_date"])
        if (row["status"] != "ok" or row["kind"] != "scheduled"
                or day.weekday() != weekday or row["direction"] != direction):
            continue
        local = datetime.fromisoformat(row["scheduled_utc"]).astimezone(config.zone)
        minute = local.hour * 60 + local.minute
        buckets[minute][day] = row["duration_s"] / 60
    return [{
        "minute": minute, "n_days": len(by_day),
        "p50": quantile(list(by_day.values()), .5),
        # Small samples are visible but are not advertised as reliable tail estimates.
        "p90": quantile(list(by_day.values()), .9) if len(by_day) >= 5 else None,
    } for minute, by_day in sorted(buckets.items())]


def safe_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: safe_cell(row.get(k)) for k in fields})


def create_report(db_path, output, config, *, demo=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    import numpy as np

    rows = read_samples(db_path)
    with connect(db_path) as db:
        meta = dict(db.execute("SELECT key,value FROM meta"))
    if meta.get("interval_minutes") and int(meta["interval_minutes"]) != config.interval_minutes:
        raise ValueError("Report interval differs from stored experiment.")
    stored_schedule = meta.get("schedule_json")
    if (stored_schedule is not None and stored_schedule != config.schedule_json()) or (
            stored_schedule is None and meta and not config.default_schedule()):
        raise ValueError("Report schedule differs from stored experiment. Use its original configuration.")
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    selected = [r for r in rows if r["kind"] == "scheduled"]
    days = []
    if selected:
        first = date.fromisoformat(min(r["local_date"] for r in selected))
        last = date.fromisoformat(max(r["local_date"] for r in selected))
        day = first
        while day <= last:
            if config.allowed_date(day):
                days.append(day)
            day += timedelta(days=1)
    grouped = defaultdict(list)
    for row in selected:
        grouped[(row["local_date"], row["direction"])].append(row)
    summaries = []
    for day in days:
        for direction in ("am", "pm"):
            expected = len([s for s in slots_on(day, config) if s.direction == direction])
            summary = day_summary(grouped[(day.isoformat(), direction)], expected, config)
            summary.update(date=day.isoformat(), weekday=day.strftime("%A"), direction=direction)
            summaries.append(summary)
    fields = ["date", "weekday", "direction", "expected_slots", "valid_samples", "coverage_pct",
              "median_minutes", "p90_minutes", "max_minutes", "delay_sample_pct",
              "trip_over_threshold_pct", "ndw_fresh_samples", "nearby_accident_events",
              "nearby_accident_report_sample_pct"]
    write_csv(out / "daily_summary.csv", summaries, fields)
    sample_fields = ["scheduled_utc", "queried_utc", "local_date", "direction", "kind", "status",
                     "error_code", "duration_s", "static_s", "distance_m", "route_key",
                     "description", "ndw_status"]
    write_csv(out / "samples.csv", rows, sample_fields)
    unique_events = {}
    for row in selected:
        for event in events(row):
            key = (event["id"], row["direction"])
            if key not in unique_events:
                unique_events[key] = dict(event, direction_of_commute=row["direction"],
                                          first_observed=row["queried_utc"])
            unique_events[key]["last_observed"] = row["queried_utc"]
    write_csv(out / "nearby_events.csv", list(unique_events.values()),
              ["id", "category", "direction_of_commute", "first_observed", "last_observed",
               "start", "end", "match", "direction", "distance_m"])
    (out / "summary.json").write_text(json.dumps({
        "is_demo": demo, **config.schedule_metadata(),
        "interval_minutes": config.interval_minutes,
        "delay_threshold_minutes": config.delay_threshold_minutes,
        "trip_threshold_minutes": config.trip_threshold_minutes,
        "daily": summaries,
    }, indent=2, allow_nan=False), encoding="utf-8")

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.facecolor": "#f6f8fb", "figure.facecolor": "white",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.spines.left": False, "axes.spines.bottom": False,
        "axes.labelcolor": "#46566b", "text.color": "#18283c",
        "xtick.color": "#46566b", "ytick.color": "#46566b",
        "grid.color": "#dce3ec", "grid.alpha": .7,
        "savefig.dpi": 150,
    })
    blue, orange, gray = "#1366c7", "#db8b12", "#8a98ab"
    watermark = "SIMULATED DEMO — NOT YOUR COMMUTE" if demo else "Google Maps estimates · NDW nearby reports"
    images = []

    def format_axes(ax, start, end):
        ax.set_xlim(start, end)
        tick_step = max(30, math.ceil((end - start) / 180) * 30)
        ticks = list(range(start, end + 1, tick_step))
        ax.set_xticks(ticks, [f"{m // 60:02}:{m % 60:02}" for m in ticks])
        ax.set_xlabel(f"Departure time · {config.timezone}")
        ax.set_ylabel("Estimated driving time (min)")
        ax.grid(axis="y")

    for day in days:
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), constrained_layout=True)
        for ax, (direction, start, end) in zip(axes, config.windows):
            day_rows = grouped[(day.isoformat(), direction)]
            by_slot = {r["scheduled_utc"]: r for r in day_rows}
            expected_slots = [s for s in slots_on(day, config) if s.direction == direction]
            x, y, baseline, incident_x, incident_y = [], [], [], [], []
            for slot in expected_slots:
                minute = slot.when.hour * 60 + slot.when.minute
                row = by_slot.get(slot.key)
                good = row and row["status"] == "ok"
                x.append(minute)
                y.append(row["duration_s"] / 60 if good else np.nan)
                baseline.append(row["static_s"] / 60 if good else np.nan)
                if good and any(e["category"] == "accident" for e in events(row)):
                    incident_x.append(minute)
                    incident_y.append(row["duration_s"] / 60)
            ax.plot(x, y, color=blue, lw=2, label="Live-traffic estimate")
            ax.plot(x, baseline, color=gray, lw=1.3, ls="--", label="Static route baseline")
            ax.axhline(config.trip_threshold_minutes, color=orange, lw=1, ls=":",
                       label=f"{config.trip_threshold_minutes:g}-min threshold")
            if incident_x:
                ax.scatter(incident_x, incident_y, s=36, marker="D", color=orange,
                           zorder=4, label="Nearby accident report")
            valid = sum(r["status"] == "ok" for r in day_rows)
            ax.set_title(f"{DIRECTION_NAMES[direction]}"
                         f"  ·  {valid}/{len(expected_slots)} samples", loc="left",
                         fontweight="bold")
            format_axes(ax, start, end)
            ax.legend(fontsize=8, loc="upper left")
        fig.suptitle(f"{day:%A, %d %B %Y}   |   {watermark}", fontsize=12, fontweight="bold")
        name = f"daily-{day.isoformat()}.png"
        fig.savefig(out / name)
        plt.close(fig)
        images.append((day.isoformat(), name))

    if days:
        weekdays = sorted(config.weekdays)
        fig, axes = plt.subplots(len(weekdays), 2, figsize=(13, max(4.5, 3.2 * len(weekdays))),
                                 squeeze=False, constrained_layout=True)
        for row_index, weekday in enumerate(weekdays):
            for col, direction in enumerate(("am", "pm")):
                ax = axes[row_index, col]
                values = profile(selected, weekday, direction, config)
                start, end = config.window_minutes(direction)
                indexed = {v["minute"]: v for v in values}
                x = list(range(start, end, config.interval_minutes))
                med = [indexed[m]["p50"] if m in indexed else np.nan for m in x]
                tail = [(indexed[m]["p90"] if indexed[m]["p90"] is not None else np.nan)
                        if m in indexed else np.nan for m in x]
                ax.plot(x, med, color=blue, lw=2, label="P50 across dates")
                ax.plot(x, tail, color=orange, lw=1.5, label="P90 (≥5 dates per time)")
                ax.fill_between(x, med, tail, color=orange, alpha=.1)
                observed_days = {r["local_date"] for r in selected if r["status"] == "ok"
                                 and date.fromisoformat(r["local_date"]).weekday() == weekday
                                 and r["direction"] == direction}
                label = WEEKDAY_NAMES[weekday]
                ax.set_title(f"{label} · {DIRECTION_NAMES[direction]}"
                             f" · {len(observed_days)} observed dates", loc="left",
                             fontweight="bold")
                format_axes(ax, start, end)
                ax.legend(fontsize=8, loc="upper left")
        fig.suptitle(f"Commute profiles by day of week\n{watermark}", fontsize=16, fontweight="bold")
        fig.savefig(out / "weekday-profiles.png")
        plt.close(fig)

        recent = days[-40:]
        fig, axes = plt.subplots(1, 2, figsize=(13, max(4.5, len(recent) * .28)),
                                 constrained_layout=True)
        norm = Normalize(vmin=30, vmax=max(100, config.trip_threshold_minutes + 20))
        for ax, direction in zip(axes, ("am", "pm")):
            start, end = config.window_minutes(direction)
            minutes = list(range(start, end, config.interval_minutes))
            columns = {minute: i for i, minute in enumerate(minutes)}
            matrix = np.full((len(recent), len(minutes)), np.nan)
            for i, day in enumerate(recent):
                for row in grouped[(day.isoformat(), direction)]:
                    if row["status"] == "ok":
                        local = datetime.fromisoformat(row["scheduled_utc"]).astimezone(config.zone)
                        col = columns.get(local.hour * 60 + local.minute)
                        if col is not None:
                            matrix[i, col] = row["duration_s"] / 60
            cmap = plt.get_cmap("YlOrRd").copy()
            cmap.set_bad("#dce3ec")
            im = ax.imshow(matrix, aspect="auto", interpolation="nearest", cmap=cmap, norm=norm)
            ax.set_yticks(range(len(recent)), [d.strftime("%m-%d %a") for d in recent], fontsize=8)
            tick_step = max(1, math.ceil(30 / config.interval_minutes), math.ceil(len(minutes) / 7))
            ticks = list(range(0, len(minutes), tick_step))
            ax.set_xticks(ticks, [f"{minutes[t] // 60:02}:{minutes[t] % 60:02}" for t in ticks])
            ax.set_title(DIRECTION_NAMES[direction],
                         fontweight="bold", loc="left")
            ax.set_xlabel(f"Departure time · {config.timezone}")
        fig.colorbar(im, ax=axes, label="Estimated driving minutes", shrink=.7)
        fig.suptitle(f"Last 40 monitored dates · grey = missing\n{watermark}",
                     fontsize=14, fontweight="bold")
        fig.savefig(out / "heatmap.png")
        plt.close(fig)

    title = "Simulated commute demo" if demo else "Commute time observations"
    weekday_label = ", ".join(WEEKDAY_NAMES[day] for day in sorted(config.weekdays))
    schedule_label = (f"Home → work {config.am_start}–{config.am_end}; "
                      f"work → home {config.pm_start}–{config.pm_end}")
    valid_total = sum(r["status"] == "ok" for r in selected)
    daily_cards = "".join(
        f'<details><summary>{html.escape(day)}</summary>'
        f'<img loading="lazy" src="{name}" alt="{day} commute estimates"></details>'
        for day, name in reversed(images))
    def fmt(v, suffix=""):
        return "—" if v is None else f"{v:.1f}{suffix}"
    table_rows = "".join(
        "<tr><td>" + s["date"] + "</td><td>" + DIRECTION_NAMES[s["direction"]]
        + "</td><td>" + str(s["valid_samples"]) + "/" + str(s["expected_slots"])
        + "</td><td>" + fmt(s["median_minutes"]) + "</td><td>" + fmt(s["p90_minutes"])
        + "</td><td>" + fmt(s["delay_sample_pct"], "%") + "</td><td>"
        + fmt(s["trip_over_threshold_pct"], "%") + "</td><td>"
        + fmt(s["nearby_accident_report_sample_pct"], "%") + "</td></tr>"
        for s in reversed(summaries))
    overview = ('<img src="heatmap.png" alt="Driving estimates by date and departure time">'
                '<img loading="lazy" src="weekday-profiles.png" alt="Travel-time percentiles by day of week">'
                if days else '<p class="notice">No scheduled samples yet. Configure and run the collector; '
                'manual tests are excluded from these charts.</p>')
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#eef2f7;color:#18283c;font:16px/1.65 system-ui,sans-serif}}
main{{max-width:1180px;margin:32px auto;padding:0 24px}}
h1{{font-size:36px;letter-spacing:-1px;margin-bottom:4px}}
.sub{{color:#56667b}} .card,details{{background:white;border-radius:16px;padding:24px;margin:18px 0}}
.stats{{display:flex;gap:16px;flex-wrap:wrap}} .stat{{flex:1;min-width:140px;background:#18283c;
color:white;border-radius:14px;padding:18px}} .stat b{{display:block;font-size:30px}}
img{{width:100%;height:auto}} summary{{cursor:pointer;font-weight:700}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{padding:10px;border-bottom:1px solid #e5ebf2;text-align:left}}
.scroll{{overflow:auto}} a{{color:#1366c7}} .notice{{border-left:4px solid #db8b12;padding:12px 18px;
background:#fff8ea}} small{{color:#56667b}} </style><main>
<p class="sub">HOME ↔ WORK · {html.escape(config.timezone)}</p><h1>{title}</h1>
<p class="sub">{html.escape(weekday_label)}<br>{html.escape(schedule_label)} · every {config.interval_minutes} minutes</p>
<div class="stats"><div class="stat"><b>{len(days)}</b>Scheduled dates in the observed range</div>
<div class="stat"><b>{valid_total}</b>Valid scheduled samples</div>
<div class="stat"><b>{len(unique_events)}</b>Nearby events, deduplicated per direction</div></div>
<div class="card"><p class="sub">{"All data on this page is synthetic." if demo else "Travel-time estimates: Google Maps. Nearby traffic reports: NDW."}</p>
<p class="notice">{html.escape(watermark)}. These are driving-time estimates at the time of the query,
excluding parking and walking, rather than measured journeys. Navigation may choose different routes;
route changes are recorded in samples.csv.</p>
{overview}</div>
<div class="card"><h2>Daily coverage and delays</h2>
<p>The coverage denominator is the full configured window for each direction, including slots still to come today.
Daily median/P90 summarize different departure times on one date. The profiles above summarize the same
departure time across dates; these describe different distributions.</p>
<div class="scroll"><table><thead><tr><th>Date</th><th>Direction</th><th>Samples</th><th>Median / min</th>
<th>P90 / min</th><th>Delay ≥{config.delay_threshold_minutes:g} min</th>
<th>Trip &gt;{config.trip_threshold_minutes:g} min</th><th>Nearby accident reports</th></tr></thead><tbody>{table_rows}</tbody></table></div>
<p>The nearby accident-report percentage uses only successful samples with fresh NDW data.
Unavailable data appears as —, not 0%.</p></div>
<div class="card"><h2>Understanding incidents and missing data</h2><p>Diamonds indicate NDW accident reports near the route;
they do not establish an effect on your lane or represent personal accident risk. Clearly opposite-direction reports
are excluded, while candidates with unknown direction may remain. Road coverage, unknown locations, delayed feeds,
and rerouting affect detection. Failed requests, stale feeds, and missing samples cannot establish clear traffic.</p>
<p>Tail estimates are unstable when few dates have been observed. P90 is hidden for departure times with fewer
than five dates. Adjacent samples are correlated and should not be counted as independent commute days.</p>
<p><a href="samples.csv">Samples CSV</a> · <a href="daily_summary.csv">Daily summary CSV</a> ·
<a href="nearby_events.csv">Nearby events CSV</a> · <a href="summary.json">Summary JSON</a></p></div>
<h2>Daily departure windows</h2>{daily_cards}
<small>Data: {"Synthetic simulation" if demo else "Google Maps / NDW"} · Report generated {datetime.now(config.zone):%Y-%m-%d %H:%M %Z}</small>
</main></html>"""
    (out / "index.html").write_text(document, encoding="utf-8")
    return out / "index.html"
