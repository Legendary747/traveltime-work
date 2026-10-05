from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from traveltime.cloud import create_app, scheduled_slot
from traveltime.cloud_export import merge_snapshot
from traveltime.db import SQLiteStore, connect, read_samples
from traveltime.firestore_store import FirestoreStore
from traveltime.google import QueryError
from traveltime.schedule import slots_on
from test_monitor import CONFIG, ROUTE


class CloudHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "cloud.sqlite3"
        self.slot = slots_on(date(2026, 10, 5), CONFIG)[0]
        self.now = self.slot.when + timedelta(seconds=8)
        self.route = Mock(return_value=[ROUTE])
        self.app = create_app(CONFIG, SQLiteStore(self.path), clock=lambda: self.now,
                              routes_client=self.route)
        self.client = self.app.test_client()
        self.header = {"X-CloudScheduler-ScheduleTime": self.slot.key}

    def tearDown(self):
        self.directory.cleanup()

    def test_http_duplicate_delivery_queries_once(self):
        self.assertEqual(self.client.post("/collect", headers=self.header).json["status"], "ok")
        self.assertEqual(self.client.post("/collect", headers=self.header).json["status"], "duplicate")
        self.route.assert_called_once_with(CONFIG, "am")
        self.assertEqual(read_samples(self.path)[0]["scheduled_utc"], self.slot.key)

    def test_retry_of_old_slot_does_not_sample_new_slot(self):
        self.now = self.slot.when + timedelta(minutes=3)
        self.assertEqual(self.client.post("/collect", headers=self.header).json["status"],
                         "skipped_outside_slot")
        self.route.assert_not_called()

    def test_missing_naive_and_malformed_timestamp_are_rejected(self):
        for value in (None, "2026-10-05T06:00:00", "invalid"):
            headers = {} if value is None else {"X-CloudScheduler-ScheduleTime": value}
            self.assertEqual(self.client.post("/collect", headers=headers).status_code, 400)
        self.route.assert_not_called()

    def test_non_grid_time_future_weekend_and_window_end_are_skipped(self):
        for value in ("2026-10-05T04:01:00+00:00", "2026-10-05T07:00:00+00:00",
                      "2026-10-10T04:00:00+00:00"):
            self.now = datetime.fromisoformat(value)
            self.assertIsNone(scheduled_slot(value, self.now, CONFIG))
        self.assertIsNone(scheduled_slot(self.slot.key, self.slot.when - timedelta(seconds=1), CONFIG))

    def test_winter_and_afternoon_keep_amsterdam_hours(self):
        winter = datetime(2026, 10, 26, 14, tzinfo=timezone.utc)
        slot = scheduled_slot(winter.isoformat(), winter, CONFIG)
        self.assertEqual((slot.direction, slot.when.hour), ("pm", 15))
        self.assertIsNone(scheduled_slot(winter.isoformat(), winter,
                                        replace(CONFIG, end_date=date(2026, 10, 25))))

    def test_storage_delay_past_deadline_does_not_make_paid_request(self):
        clock = Mock(side_effect=[self.now, self.slot.when + timedelta(seconds=61)])
        client = create_app(CONFIG, SQLiteStore(self.path), clock=clock,
                            routes_client=self.route).test_client()
        self.assertEqual(client.post("/collect", headers=self.header).json["status"], "error")
        self.route.assert_not_called()
        self.assertEqual(read_samples(self.path)[0]["error_code"], "late_before_query")

    def test_failed_query_durable_and_not_retried(self):
        self.route.side_effect = QueryError("google_http_429")
        self.assertEqual(self.client.post("/collect", headers=self.header).json["status"], "error")
        self.assertEqual(self.client.post("/collect", headers=self.header).json["status"], "duplicate")
        self.route.assert_called_once()
        self.assertEqual(read_samples(self.path)[0]["status"], "error")

    def test_paused_service_rejects_even_in_flight_scheduler_request(self):
        with patch.dict("os.environ", {"COLLECTION_ENABLED": "false"}):
            response = self.client.post("/collect", headers=self.header)
        self.assertEqual(response.json["status"], "collection_paused")
        self.route.assert_not_called()

    def test_health_check_and_storage_errors_never_query(self):
        self.assertEqual(self.client.get("/healthz").json, {"status": "ready"})
        store = Mock()
        store.claim.side_effect = RuntimeError("sensitive synthetic detail")
        client = create_app(CONFIG, store, clock=lambda: self.now,
                            routes_client=self.route).test_client()
        result = client.post("/collect", headers=self.header)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn(b"sensitive", result.data)
        self.route.assert_not_called()


class MemoryFirestore:
    """Deterministic transactional double; enforces Firestore's read-before-write rule."""
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def collection(self, name):
        client = self

        class Collection:
            def document(self, identity):
                return MemoryDocument(client, name + "/" + identity)
        return Collection()

    def transaction(self, **_):
        return MemoryTransaction(self)


class MemoryDocument:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def collection(self, name):
        return self.client.collection(self.path + "/" + name)

    def get(self, transaction=None):
        if transaction and transaction.writes:
            raise AssertionError("Firestore forbids reads after writes.")
        value = deepcopy(self.client.data.get(self.path))
        return SimpleNamespace(exists=value is not None, to_dict=lambda: value)

    def update(self, values):
        self.client.data[self.path].update(deepcopy(values))


class MemoryTransaction:
    def __init__(self, client):
        self.client = client
        self.writes = {}

    def set(self, ref, value):
        self.writes[ref.path] = deepcopy(value)


def memory_transactional(function):
    def execute(transaction):
        with transaction.client.lock:
            result = function(transaction)
            transaction.client.data.update(transaction.writes)
            return result
    return execute


class FirestoreReservationTests(unittest.TestCase):
    def setUp(self):
        self.client = MemoryFirestore()
        self.store = FirestoreStore(experiment="test-a", client=self.client)
        self.slot = slots_on(date(2026, 10, 5), CONFIG)[0]
        self.decorator = patch("traveltime.firestore_store.firestore.transactional",
                               memory_transactional)
        self.decorator.start()
        self.addCleanup(self.decorator.stop)

    def test_concurrent_claims_reserve_one_request_and_omit_locations(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.store.claim(CONFIG, self.slot, self.slot.when), range(2)))
        self.assertCountEqual(results, ["pending", "duplicate"])
        self.assertEqual(self.client.data["commute_request_budgets/2026-10"]["attempts"], 1)
        encoded = json.dumps(self.client.data)
        self.assertNotIn(CONFIG.api_key, encoded)
        self.assertNotIn(CONFIG.home, encoded)

    def test_limit_shared_across_experiments_and_failure(self):
        limited = replace(CONFIG, monthly_request_limit=1)
        self.store.claim(limited, self.slot, self.slot.when)
        self.store.fail(self.slot.key, "google_timeout")
        other = FirestoreStore(experiment="test-b", client=self.client)
        self.assertEqual(other.claim(limited, self.slot, self.slot.when), "budget_skipped")
        self.assertEqual(self.store.claim(limited, self.slot, self.slot.when), "duplicate")

    def test_month_rolls_in_pacific_and_new_month_resumes(self):
        limited = replace(CONFIG, monthly_request_limit=1)
        slots = slots_on(date(2026, 10, 1), CONFIG)
        first = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
        second = datetime(2026, 10, 1, 6, 59, tzinfo=timezone.utc)
        third = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(self.store.claim(limited, slots[0], first), "pending")
        self.assertEqual(self.store.claim(limited, slots[1], second), "budget_skipped")
        self.assertEqual(self.store.claim(limited, slots[2], third), "pending")

    def test_route_change_rejected_before_budget_or_sample_write(self):
        self.store.claim(CONFIG, self.slot, self.slot.when)
        before = deepcopy(self.client.data)
        with self.assertRaisesRegex(ValueError, "CLOUD_EXPERIMENT"):
            self.store.claim(replace(CONFIG, home="51.1,4.2"), self.slot, self.slot.when)
        self.assertEqual(before, self.client.data)

    def test_completed_firestore_row_exports_into_existing_report_schema(self):
        self.store.claim(CONFIG, self.slot, self.slot.when)
        self.store.complete(self.slot.key, [dict(ROUTE, points=[(52, 4)])],
                            {"status": "disabled", "events": []})
        metadata = self.store.root.get().to_dict()
        row = self.store.sample_ref(self.slot.key).get().to_dict()
        self.assertNotIn("points", row["alternatives_json"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mirror.sqlite3"
            merge_snapshot(path, metadata, [row], "firestore:project/test-a")
            merge_snapshot(path, metadata, [row], "firestore:project/test-a")
            rows = read_samples(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["status"], rows[0]["duration_s"]), ("ok", 4500))
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            with self.assertRaises(ValueError):
                merge_snapshot(path, metadata, [row], "firestore:project/test-b")
            bad_row = dict(row, slot_id="invalid", direction=None)
            with self.assertRaises(Exception):
                merge_snapshot(path, metadata, [dict(row, duration_s=9999), bad_row],
                               "firestore:project/test-a")
            self.assertEqual(read_samples(path)[0]["duration_s"], 4500)  # Entire merge rolled back.
