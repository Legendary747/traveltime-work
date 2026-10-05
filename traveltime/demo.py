"""Deterministic synthetic data, isolated from actual observations."""
from datetime import date, timedelta
from pathlib import Path
import math
import random

from .config import Config
from .db import claim, complete, fail
from .reports import create_report
from .schedule import slots_on


def generate(db_path, output, days=10):
    if Path(db_path).exists():
        raise ValueError("Demo requires a new --db path; existing data will not be overwritten.")
    if not 1 <= days <= 60:
        raise ValueError("Demo days must be between 1 and 60.")
    config = Config(api_key="simulation-only", home="Demo origin", work="Demo destination",
                    monthly_request_limit=100000)
    rng = random.Random(747)
    current = date(2026, 9, 7)
    count = 0
    while count < days:
        if current.weekday() < 5:
            daily = rng.gauss(0, 5)
            for slot in slots_on(current, config):
                if rng.random() < .06:
                    continue
                claim(db_path, config, slot, slot.when + timedelta(seconds=2))
                if rng.random() < .015:
                    fail(db_path, slot.key, "simulated_network_error")
                    continue
                minute = slot.when.hour * 60 + slot.when.minute
                center = 465 if slot.direction == "am" else 1015
                peak = (28 if slot.direction == "am" else 37) * math.exp(-((minute-center)/51)**2)
                accident = count in (2, 7) and slot.direction == "pm" and 980 <= minute <= 1020
                duration = max(47, 49 + peak + daily + rng.gauss(0, 1.8) + (19 if accident else 0))
                routes = [{
                    "duration_s": duration * 60, "static_s": 48 * 60, "distance_m": 65000,
                    "route_key": "demo-route", "description": "SIMULATED DATA",
                    "labels": ["DEFAULT_ROUTE"], "points": [],
                }]
                ndw = {"status": "ok", "events": ([{
                    "id": f"demo-accident-{count}", "category": "accident",
                    "direction": "unknown", "match": "near_route_candidate", "distance_m": 80,
                    "start": slot.key, "end": None,
                }] if accident else [])}
                complete(db_path, slot.key, routes, ndw)
            count += 1
        current += timedelta(days=1)
    result = create_report(db_path, output, config, demo=True)
    print(f"SIMULATED demo only: {result}")
