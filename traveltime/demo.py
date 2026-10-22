"""Deterministic synthetic data, isolated from actual observations."""
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import math
import random

from .config import Config
from .db import claim, complete, fail
from .reports import create_report
from .schedule import slots_on


def generate(db_path, output, days=10, config=None):
    if Path(db_path).exists():
        raise ValueError("Demo requires a new --db path; existing data will not be overwritten.")
    if not 1 <= days <= 60:
        raise ValueError("Demo days must be between 1 and 60.")
    config = replace(config or Config(), api_key="simulation-only", home="Demo origin",
                     work="Demo destination", monthly_request_limit=100000)
    config.validate()
    current = config.start_date or date(2026, 9, 7)
    monitored_days = []
    last = config.end_date or date.max
    while len(monitored_days) < days:
        if current > last:
            raise ValueError("Demo date range does not contain enough configured monitoring dates.")
        if config.allowed_date(current) and slots_on(current, config):
            monitored_days.append(current)
        if len(monitored_days) == days:
            break
        if current == last:
            raise ValueError("Demo date range does not contain enough configured monitoring dates.")
        current += timedelta(days=1)
    rng = random.Random(747)
    for count, current in enumerate(monitored_days):
        daily = rng.gauss(0, 5)
        for index, slot in enumerate(slots_on(current, config)):
            # Retain one record on every requested date, even for very short windows.
            if rng.random() < .06 and index > 0:
                continue
            claim(db_path, config, slot, slot.when + timedelta(seconds=2))
            if rng.random() < .015:
                fail(db_path, slot.key, "simulated_network_error")
                continue
            minute = slot.when.hour * 60 + slot.when.minute
            start, end = config.window_minutes(slot.direction)
            width = end - start
            center = start + width * (105 / 180 if slot.direction == "am" else 115 / 180)
            peak = (28 if slot.direction == "am" else 37) * math.exp(-((minute-center)/(width * 51 / 180))**2)
            accident = (config.ndw_enabled and count in (2, 7) and slot.direction == "pm"
                        and start + width * 80 / 180 <= minute <= start + width * 120 / 180)
            duration = max(47, 49 + peak + daily + rng.gauss(0, 1.8) + (19 if accident else 0))
            routes = [{
                "duration_s": duration * 60, "static_s": 48 * 60, "distance_m": 65000,
                "route_key": "demo-route", "description": "SIMULATED DATA",
                "labels": ["DEFAULT_ROUTE"], "points": [],
            }]
            ndw = {"status": "ok" if config.ndw_enabled else "disabled", "events": ([{
                "id": f"demo-accident-{count}", "category": "accident",
                "direction": "unknown", "match": "near_route_candidate", "distance_m": 80,
                "start": slot.key, "end": None,
            }] if accident else [])}
            complete(db_path, slot.key, routes, ndw)
    result = create_report(db_path, output, config, demo=True)
    print(f"SIMULATED demo only: {result}")
    return result
