from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from traveltime.cloud import create_app, scheduled_slot
from traveltime.cloud_export import merge_snapshot
from traveltime.db import SQLiteStore, connect, read_samples
from traveltime.schedule import slots_on
from test_monitor import CONFIG, ROUTE


class ConfiguredCloudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cloud.sqlite3"

    def test_custom_interval_zone_and_weekend_collect_the_correct_direction(self):
        config = replace(CONFIG, timezone="Asia/Tokyo", weekdays=(5,), interval_minutes=7,
                         am_start="08:17", am_end="09:06", pm_start="17:02", pm_end="17:11")
        slot = slots_on(date(2026, 10, 10), config)[-1]
        now = slot.when.astimezone(timezone.utc) + timedelta(seconds=10)
        routes = Mock(return_value=[ROUTE])
        client = create_app(config, SQLiteStore(self.path), clock=lambda: now,
                            routes_client=routes).test_client()
        response = client.post("/collect", headers={"X-CloudScheduler-ScheduleTime": slot.key})
        self.assertEqual(response.json["status"], "ok")
        routes.assert_called_once_with(config, "pm")
        row = read_samples(self.path)[0]
        self.assertEqual(row["local_date"], "2026-10-10")
        self.assertEqual(row["scheduled_utc"], "2026-10-10T08:09:00+00:00")
        with connect(self.path) as db:
            metadata = dict(db.execute("SELECT key,value FROM meta"))
        self.assertEqual(metadata["interval_minutes"], "7")
        self.assertEqual(metadata["schedule_json"], config.schedule_json())
        invalid_grid = datetime(2026, 10, 10, 8, 7, tzinfo=timezone.utc)
        self.assertIsNone(scheduled_slot(invalid_grid.isoformat(), invalid_grid, config))

    def test_fall_repeated_hour_is_not_accepted_as_another_scheduled_instant(self):
        config = replace(CONFIG, weekdays=(6,), interval_minutes=30,
                         am_start="01:30", am_end="03:31")
        first = datetime(2026, 10, 25, 2, tzinfo=config.zone, fold=0)
        second = first.replace(fold=1)
        self.assertIsNotNone(scheduled_slot(first.isoformat(), first, config))
        self.assertIsNone(scheduled_slot(second.isoformat(), second, config))
        self.assertIsNone(scheduled_slot(first.isoformat(), second, config))

    def test_export_preserves_custom_schedule_and_rejects_mixing_before_writes(self):
        config = replace(CONFIG, timezone="Asia/Tokyo", weekdays=(5,), interval_minutes=7,
                         am_start="08:17", am_end="09:06")
        slot = slots_on(date(2026, 10, 10), config)[0]
        metadata = {"route_fingerprint": config.fingerprint(), "interval_minutes": "7",
                    "schedule_json": config.schedule_json()}
        row = {"slot_id": slot.key, "scheduled_utc": slot.key, "queried_utc": slot.key,
               "local_date": "2026-10-10", "direction": "am", "kind": "scheduled", "status": "pending",
               "quota_reserved": True}
        self.assertEqual(merge_snapshot(self.path, metadata, [row], "firestore:test/custom"), 1)
        with connect(self.path) as db:
            stored = dict(db.execute("SELECT key,value FROM meta"))
            self.assertEqual(db.execute("SELECT billing_month FROM requests").fetchone()[0], "2026-10")
        self.assertEqual(stored["schedule_json"], config.schedule_json())
        self.assertEqual(stored["interval_minutes"], "7")
        changed = dict(metadata, schedule_json=replace(config, am_start="08:18").schedule_json())
        with self.assertRaisesRegex(ValueError, "another schedule"):
            merge_snapshot(self.path, changed, [dict(row, status="error")], "firestore:test/custom")
        self.assertEqual(read_samples(self.path)[0]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "metadata"):
            merge_snapshot(self.path, dict(metadata, schedule_json=json.dumps({"timezone": "UTC"})),
                           [], "firestore:test/custom")

    def test_export_migrates_legacy_default_schedule_and_rejects_invalid_interval(self):
        metadata = {"route_fingerprint": CONFIG.fingerprint(), "interval_minutes": "3"}
        with connect(self.path) as db:
            db.executemany("INSERT INTO meta VALUES (?,?)", metadata.items())
            db.commit()
        merge_snapshot(self.path, metadata, [], "firestore:test/legacy")
        with connect(self.path) as db:
            self.assertEqual(dict(db.execute("SELECT key,value FROM meta"))["schedule_json"],
                             CONFIG.schedule_json())
        for interval in ("0", "61", "invalid", 3):
            with self.subTest(interval=interval), self.assertRaisesRegex(ValueError, "interval"):
                merge_snapshot(self.path, dict(metadata, interval_minutes=interval), [], "firestore:test/legacy")


if __name__ == "__main__":
    unittest.main()
