from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import csv
import html
import json
import math
import statistics

from .db import read_samples, connect
from .schedule import AMSTERDAM, slots_on


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


def profile(rows, weekday, direction):
    """One observation per date/slot. P90 across DAYS, not pooled peak samples."""
    buckets = defaultdict(dict)
    for row in rows:
        day = date.fromisoformat(row["local_date"])
        if (row["status"] != "ok" or row["kind"] != "scheduled"
                or day.weekday() != weekday or row["direction"] != direction):
            continue
        local = datetime.fromisoformat(row["scheduled_utc"]).astimezone(AMSTERDAM)
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

    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    rows = read_samples(db_path)
    with connect(db_path) as db:
        meta = dict(db.execute("SELECT key,value FROM meta"))
    if meta.get("interval_minutes") and int(meta["interval_minutes"]) != config.interval_minutes:
        raise ValueError("Report interval differs from stored experiment.")
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
        "is_demo": demo, "timezone": "Europe/Amsterdam",
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
        ticks = list(range(start, end + 1, 30))
        ax.set_xticks(ticks, [f"{m // 60:02}:{m % 60:02}" for m in ticks])
        ax.set_xlabel("Departure time · Europe/Amsterdam")
        ax.set_ylabel("Estimated driving time (min)")
        ax.grid(axis="y")

    for day in days:
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), constrained_layout=True)
        for ax, direction, start in zip(axes, ("am", "pm"), (360, 900)):
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
            ax.set_title(f"{'Home → work' if direction == 'am' else 'Work → home'}"
                         f"  ·  {valid}/{len(expected_slots)} samples", loc="left",
                         fontweight="bold")
            format_axes(ax, start, start + 180)
            ax.legend(fontsize=8, loc="upper left")
        fig.suptitle(f"{day:%A, %d %B %Y}   |   {watermark}", fontsize=12, fontweight="bold")
        name = f"daily-{day.isoformat()}.png"
        fig.savefig(out / name)
        plt.close(fig)
        images.append((day.isoformat(), name))

    if days:
        fig, axes = plt.subplots(5, 2, figsize=(13, 16), constrained_layout=True)
        for weekday in range(5):
            for col, direction in enumerate(("am", "pm")):
                ax = axes[weekday, col]
                values = profile(selected, weekday, direction)
                start = 360 if direction == "am" else 900
                indexed = {v["minute"]: v for v in values}
                x = list(range(start, start + 180, config.interval_minutes))
                med = [indexed[m]["p50"] if m in indexed else np.nan for m in x]
                tail = [(indexed[m]["p90"] if indexed[m]["p90"] is not None else np.nan)
                        if m in indexed else np.nan for m in x]
                ax.plot(x, med, color=blue, lw=2, label="P50 across dates")
                ax.plot(x, tail, color=orange, lw=1.5, label="P90 (≥5 dates per time)")
                ax.fill_between(x, med, tail, color=orange, alpha=.1)
                observed_days = {r["local_date"] for r in selected if r["status"] == "ok"
                                 and date.fromisoformat(r["local_date"]).weekday() == weekday
                                 and r["direction"] == direction}
                label = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")[weekday]
                ax.set_title(f"{label} · {'AM' if direction == 'am' else 'PM'}"
                             f" · {len(observed_days)} observed dates", loc="left",
                             fontweight="bold")
                format_axes(ax, start, start + 180)
                ax.legend(fontsize=8, loc="upper left")
        fig.suptitle(f"Weekday commute profiles\n{watermark}", fontsize=16, fontweight="bold")
        fig.savefig(out / "weekday-profiles.png")
        plt.close(fig)

        recent = days[-40:]
        fig, axes = plt.subplots(1, 2, figsize=(13, max(4.5, len(recent) * .28)),
                                 constrained_layout=True)
        norm = Normalize(vmin=30, vmax=max(100, config.trip_threshold_minutes + 20))
        for ax, direction in zip(axes, ("am", "pm")):
            matrix = np.full((len(recent), 180 // config.interval_minutes), np.nan)
            for i, day in enumerate(recent):
                for row in grouped[(day.isoformat(), direction)]:
                    if row["status"] == "ok":
                        local = datetime.fromisoformat(row["scheduled_utc"]).astimezone(AMSTERDAM)
                        start = 360 if direction == "am" else 900
                        col = (local.hour * 60 + local.minute - start) // config.interval_minutes
                        if 0 <= col < matrix.shape[1]:
                            matrix[i, col] = row["duration_s"] / 60
            cmap = plt.get_cmap("YlOrRd").copy()
            cmap.set_bad("#dce3ec")
            im = ax.imshow(matrix, aspect="auto", interpolation="nearest", cmap=cmap, norm=norm)
            ax.set_yticks(range(len(recent)), [d.strftime("%m-%d %a") for d in recent], fontsize=8)
            ticks = list(range(0, matrix.shape[1], 30 // config.interval_minutes))
            start = 360 if direction == "am" else 900
            ax.set_xticks(ticks, [f"{(start + t * config.interval_minutes) // 60:02}:"
                                 f"{(start + t * config.interval_minutes) % 60:02}" for t in ticks])
            ax.set_title("Home → work" if direction == "am" else "Work → home",
                         fontweight="bold", loc="left")
            ax.set_xlabel("Departure time · Europe/Amsterdam")
        fig.colorbar(im, ax=axes, label="Estimated driving minutes", shrink=.7)
        fig.suptitle(f"Last 40 monitored weekdays · grey = missing\n{watermark}",
                     fontsize=14, fontweight="bold")
        fig.savefig(out / "heatmap.png")
        plt.close(fig)

    title = "演示数据 · 非真实通勤" if demo else "通勤时间观察"
    valid_total = sum(r["status"] == "ok" for r in selected)
    daily_cards = "".join(
        f'<details><summary>{html.escape(day)}</summary>'
        f'<img loading="lazy" src="{name}" alt="{day} 早晚高峰"></details>'
        for day, name in reversed(images))
    def fmt(v, suffix=""):
        return "—" if v is None else f"{v:.1f}{suffix}"
    table_rows = "".join(
        "<tr><td>" + s["date"] + "</td><td>" + ("早" if s["direction"] == "am" else "晚")
        + "</td><td>" + str(s["valid_samples"]) + "/" + str(s["expected_slots"])
        + "</td><td>" + fmt(s["median_minutes"]) + "</td><td>" + fmt(s["p90_minutes"])
        + "</td><td>" + fmt(s["delay_sample_pct"], "%") + "</td><td>"
        + fmt(s["trip_over_threshold_pct"], "%") + "</td><td>"
        + fmt(s["nearby_accident_report_sample_pct"], "%") + "</td></tr>"
        for s in reversed(summaries))
    overview = ('<img src="heatmap.png" alt="每天各出发时间预计车程">'
                '<img loading="lazy" src="weekday-profiles.png" alt="按星期汇总的车程分位数">'
                if days else '<p class="notice">暂无定时采样。完成配置后运行采集器；手动测试不会计入图表。</p>')
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
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
<p class="sub">HOME ↔ A.S.R. · EUROPE/AMSTERDAM</p><h1>{title}</h1>
<p class="sub">工作日 06:00–09:00 / 15:00–18:00 · 每 {config.interval_minutes} 分钟</p>
<div class="stats"><div class="stat"><b>{len(days)}</b>观察日期范围内工作日</div>
<div class="stat"><b>{valid_total}</b>有效定时样本</div>
<div class="stat"><b>{len(unique_events)}</b>附近事件（按方向去重，含非事故）</div></div>
<div class="card"><p class="notice">{html.escape(watermark)}。这是查询时刻的预计车程，
不含停车和步行；不是实际驾驶测量。曲线允许导航改道，路线变化可查 samples.csv。</p>
{overview}</div>
<div class="card"><h2>每天的采样质量和延误</h2>
<p>分母为该方向完整三小时窗口；当天尚未结束时覆盖率自然偏低。中位数/P90 是当日不同出发时刻的分布；
上方星期图则是同一出发时刻跨日期的分布。两者含义不同。</p>
<div class="scroll"><table><thead><tr><th>日期</th><th>方向</th><th>样本</th><th>中位数/分</th>
<th>P90/分</th><th>延误 ≥{config.delay_threshold_minutes:g}分</th>
<th>车程 &gt;{config.trip_threshold_minutes:g}分</th><th>附近事故报告占比</th></tr></thead><tbody>{table_rows}</tbody></table></div>
<p>“附近事故报告占比”只以 NDW 数据新鲜且查询成功的样本为分母；无可用数据时显示 —，不是 0%。</p></div>
<div class="card"><h2>事故与缺测的解释</h2><p>菱形表示 NDW 在路线附近报告事故，不能视为确认影响本车道，
也不是个人事故概率。已知明显反向报告会排除；方向未知的候选仍可保留。未覆盖的道路、未知位置、数据源延迟
和改道都会影响检出。请求失败、陈旧数据与未采集均不等于“畅通”或“无事故”。</p>
<p>每个星期几观察天数较少时，尾部估计不稳定；同一出发时刻不足 5 天不画 P90。
相邻三分钟样本相关，不应当成独立的通勤天数。</p>
<p><a href="samples.csv">逐次样本 CSV</a> · <a href="daily_summary.csv">每日汇总 CSV</a> ·
<a href="nearby_events.csv">附近事件 CSV</a> · <a href="summary.json">汇总 JSON</a></p></div>
<h2>每日早晚高峰</h2>{daily_cards}
<small>Data: Google Maps / NDW · Report generated {datetime.now(AMSTERDAM):%Y-%m-%d %H:%M %Z}</small>
</main></html>"""
    (out / "index.html").write_text(document, encoding="utf-8")
    return out / "index.html"
