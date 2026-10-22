from dataclasses import replace
from datetime import date, datetime, timezone
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traveltime.config import Config
from traveltime.db import claim, complete, connect, read_samples
from traveltime.demo import generate
from traveltime.reports import create_report, profile
from traveltime.schedule import slots_on


CONFIG = Config(
    api_key="private-key", home="Private origin", work="Private destination",
    timezone="Asia/Tokyo", weekdays=(5,), interval_minutes=47,
    am_start="07:10", am_end="09:15", pm_start="21:05", pm_end="22:06",
    ndw_enabled=False,
)
ROUTE = {
    "duration_s": 3600, "static_s": 3000, "distance_m": 25000,
    "route_key": "synthetic-route", "description": "SYNTHETIC FIXTURE",
    "labels": ["DEFAULT_ROUTE"], "points": [],
}


class ConfiguredReportsTests(unittest.TestCase):
    def test_profile_uses_configured_timezone(self):
        local = datetime(2026, 10, 10, 7, 10, tzinfo=CONFIG.zone)
        row = dict(status="ok", kind="scheduled", local_date="2026-10-10", direction="am",
                   scheduled_utc=local.astimezone(timezone.utc).isoformat(), duration_s=3600)
        self.assertEqual(profile([row], 5, "am", CONFIG), [
            {"minute": 430, "n_days": 1, "p50": 60, "p90": None},
        ])

    def test_custom_weekend_report_has_correct_coverage_and_safe_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "samples.sqlite3"
            for slot in slots_on(date(2026, 10, 10), CONFIG):
                claim(db_path, CONFIG, slot, slot.when)
                complete(db_path, slot.key, [ROUTE], {"status": "disabled", "events": []})
            with connect(db_path) as db:
                metadata = dict(db.execute("SELECT key,value FROM meta"))
            self.assertEqual(metadata["schedule_json"], CONFIG.schedule_json())
            report = create_report(db_path, Path(folder) / "report", CONFIG)
            summary = json.loads(report.with_name("summary.json").read_text())
            self.assertEqual(summary["timezone"], "Asia/Tokyo")
            self.assertEqual(summary["weekdays"], [5])
            self.assertEqual(summary["interval_minutes"], 47)
            self.assertEqual([row["expected_slots"] for row in summary["daily"]], [3, 2])
            self.assertEqual([row["coverage_pct"] for row in summary["daily"]], [100, 100])
            document = report.read_text()
            for value in ("Saturday", "Asia/Tokyo", "07:10–09:15", "21:05–22:06", "Google Maps"):
                self.assertIn(value, document)
            for value in ("A.S.R.", "Private origin", "Private destination", "private-key"):
                self.assertNotIn(value, document)
            self.assertTrue(report.with_name("heatmap.png").is_file())
            self.assertTrue(report.with_name("weekday-profiles.png").is_file())

    def test_report_rejects_schedule_change_and_supports_default_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "legacy.sqlite3"
            with connect(db_path) as db:
                db.execute("INSERT INTO meta VALUES ('interval_minutes', '3')")
                db.commit()
            create_report(db_path, Path(folder) / "legacy-report", Config())
            with self.assertRaisesRegex(ValueError, "schedule differs"):
                create_report(db_path, Path(folder) / "wrong-report",
                              replace(CONFIG, interval_minutes=3))
            self.assertFalse((Path(folder) / "wrong-report").exists())
            with connect(db_path) as db:
                db.execute("INSERT INTO meta VALUES (?,?)", ("schedule_json", Config().schedule_json()))
                db.commit()
            with self.assertRaisesRegex(ValueError, "schedule differs"):
                create_report(db_path, Path(folder) / "changed-report",
                              replace(Config(), timezone="UTC"))


class ConfiguredDemoTests(unittest.TestCase):
    def test_demo_honors_schedule_and_isolates_credentials_and_locations(self):
        config = replace(CONFIG, start_date=date(2026, 10, 9), end_date=date(2026, 10, 24),
                         exclude_dates=(date(2026, 10, 17),))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.sqlite3"
            output = Path(folder) / "report"
            with patch("traveltime.demo.create_report", return_value=output / "index.html") as report, \
                    patch("traveltime.google.request_routes", side_effect=AssertionError("No live API")), \
                    contextlib.redirect_stdout(io.StringIO()):
                generate(path, output, days=2, config=config)
            used = report.call_args.args[2]
            self.assertEqual(used.timezone, config.timezone)
            self.assertEqual(used.schedule_json(), config.schedule_json())
            self.assertEqual((used.home, used.work, used.api_key),
                             ("Demo origin", "Demo destination", "simulation-only"))
            self.assertTrue(report.call_args.kwargs["demo"])
            rows = read_samples(path)
            self.assertEqual({row["local_date"] for row in rows}, {"2026-10-10", "2026-10-24"})
            expected = {slot.key for day in (date(2026, 10, 10), date(2026, 10, 24))
                        for slot in slots_on(day, config)}
            self.assertTrue({row["scheduled_utc"] for row in rows} <= expected)
            self.assertNotIn(b"private-key", path.read_bytes())
            self.assertNotIn(b"Private origin", path.read_bytes())

    def test_insufficient_demo_date_range_fails_before_creating_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.sqlite3"
            config = replace(CONFIG, start_date=date(2026, 10, 10), end_date=date(2026, 10, 10))
            with self.assertRaisesRegex(ValueError, "enough configured monitoring dates"):
                generate(path, Path(folder) / "report", days=2, config=config)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
