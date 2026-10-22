from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from traveltime.config import Config, waypoint
from traveltime.schedule import AMSTERDAM, Slot, slots_on, due_slot, next_slot, peak_end
from traveltime.db import claim, complete, connect, read_samples
from traveltime.google import request_routes, parse_routes, QueryError, decode_polyline
from traveltime.collector import collect
from traveltime.incidents import parse_feed, match_nearby
from traveltime.reports import day_summary, profile


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=AMSTERDAM)


CONFIG = Config(api_key="test-placeholder", home="52.0,4.3", work="52.1,5.1", ndw_enabled=False)
ROUTE = {
    "duration_s": 4500, "static_s": 3000, "distance_m": 65000, "route_key": "test-route",
    "description": "Fixture", "labels": ["DEFAULT_ROUTE"], "points": [],
}


class ScheduleTests(unittest.TestCase):
    def test_exact_120_slots_and_exclusive_endpoints(self):
        slots = slots_on(date(2026, 10, 5), CONFIG)
        self.assertEqual(len(slots), 120)
        self.assertEqual([s.when.strftime("%H:%M") for s in [slots[0], slots[59], slots[60], slots[-1]]],
                         ["06:00", "08:57", "15:00", "17:57"])
        self.assertIsNone(due_slot(dt("2026-10-05T09:00:00"), CONFIG))
        self.assertIsNone(due_slot(dt("2026-10-05T18:00:00"), CONFIG))

    def test_dst_preserves_local_hours(self):
        summer = slots_on(date(2026, 10, 23), CONFIG)[0]
        winter = slots_on(date(2026, 10, 26), CONFIG)[0]
        self.assertEqual(summer.key, "2026-10-23T04:00:00+00:00")
        self.assertEqual(winter.key, "2026-10-26T05:00:00+00:00")

    def test_late_restart_is_not_backfilled(self):
        self.assertIsNone(due_slot(dt("2026-10-05T06:01:01"), CONFIG))
        self.assertEqual(next_slot(dt("2026-10-05T06:01:01"), CONFIG).when.minute, 3)
        self.assertIsNotNone(due_slot(dt("2026-10-05T06:00:59"), CONFIG))

    def test_weekend_end_date_and_exclusion(self):
        config = replace(CONFIG, end_date=date(2026, 10, 12), exclude_dates=(date(2026, 10, 12),))
        self.assertEqual(slots_on(date(2026, 10, 10), config), [])
        self.assertIsNone(next_slot(dt("2026-10-09T18:00:00"), config))

    def test_peak_does_not_wait_until_afternoon(self):
        self.assertIsNotNone(peak_end(dt("2026-10-05T05:55:00"), CONFIG))
        self.assertIsNone(peak_end(dt("2026-10-05T10:00:00"), CONFIG))
        self.assertEqual(peak_end(dt("2026-10-05T16:00:00"), CONFIG).hour, 18)

    def test_coordinate_validation(self):
        with self.assertRaises(ValueError):
            waypoint("nan,4")
        self.assertIn("address", waypoint("Example Street, Netherlands"))

    def test_custom_window_grid_is_anchored_at_start_and_weekdays_are_configured(self):
        config = replace(CONFIG, timezone="Asia/Tokyo", weekdays=(5,), interval_minutes=7,
                         am_start="08:17", am_end="09:06", pm_start="17:02", pm_end="17:11")
        slots = slots_on(date(2026, 10, 10), config)
        self.assertEqual([slot.when.strftime("%H:%M") for slot in slots],
                         ["08:17", "08:24", "08:31", "08:38", "08:45", "08:52", "08:59",
                          "17:02", "17:09"])
        self.assertEqual(slots[0].key, "2026-10-09T23:17:00+00:00")
        self.assertEqual(slots_on(date(2026, 10, 9), config), [])
        due = datetime(2026, 10, 9, 23, 24, 30, tzinfo=timezone.utc)
        self.assertEqual(due_slot(due, config).when.strftime("%H:%M"), "08:24")
        self.assertEqual(next_slot(due, config).when.strftime("%H:%M"), "08:31")
        self.assertEqual(peak_end(due, config).strftime("%H:%M"), "09:06")

    def test_direction_names_do_not_require_morning_and_afternoon_order(self):
        config = replace(CONFIG, am_start="19:00", am_end="20:00", pm_start="07:00", pm_end="08:00")
        slots = slots_on(date(2026, 10, 5), config)
        self.assertEqual(slots[0].direction, "pm")
        self.assertEqual(next_slot(dt("2026-10-05T08:00:00"), config).direction, "am")

    def test_partial_interval_window_end_remains_exclusive(self):
        config = replace(CONFIG, am_end="06:04")
        self.assertIsNotNone(due_slot(dt("2026-10-05T06:03:59"), config))
        self.assertIsNone(due_slot(dt("2026-10-05T06:04:00"), config))

    def test_dst_transition_skips_missing_hour_and_samples_repeated_hour_once(self):
        config = replace(CONFIG, weekdays=(6,), interval_minutes=30,
                         am_start="01:30", am_end="03:31")
        spring = [slot for slot in slots_on(date(2026, 3, 29), config) if slot.direction == "am"]
        self.assertEqual([slot.when.strftime("%H:%M") for slot in spring], ["01:30", "03:00", "03:30"])
        fall = [slot for slot in slots_on(date(2026, 10, 25), config) if slot.direction == "am"]
        self.assertEqual(len(fall), 5)
        self.assertEqual(len({slot.key for slot in fall}), 5)
        first = datetime(2026, 10, 25, 2, 0, 20, tzinfo=AMSTERDAM, fold=0)
        second = first.replace(fold=1)
        self.assertIsNotNone(due_slot(first, config))
        self.assertIsNone(due_slot(second, config))
        self.assertEqual(next_slot(second, config).when.strftime("%H:%M"), "03:00")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "data.sqlite3"
        self.slot = slots_on(date(2026, 10, 5), CONFIG)[0]

    def tearDown(self):
        self.directory.cleanup()

    def test_two_process_like_claims_only_one_request(self):
        def job(_):
            return claim(self.path, CONFIG, self.slot, self.slot.when)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(job, range(2)))
        self.assertCountEqual(result, ["pending", "duplicate"])

    def test_budget_reservation_survives_error(self):
        config = replace(CONFIG, monthly_request_limit=1)
        def bad(*_):
            raise QueryError("google_http_429")
        self.assertEqual(collect(self.path, config, self.slot, self.slot.when, routes_client=bad), "error")
        slot2 = slots_on(date(2026, 10, 5), CONFIG)[1]
        self.assertEqual(claim(self.path, config, slot2, slot2.when), "budget_skipped")
        self.assertIsNone(read_samples(self.path)[0]["duration_s"])

    def test_endpoint_change_requires_new_experiment(self):
        claim(self.path, CONFIG, self.slot, self.slot.when)
        with self.assertRaisesRegex(ValueError, "new --db"):
            claim(self.path, replace(CONFIG, home="51,4"), self.slot, self.slot.when)

    def test_schedule_change_requires_new_experiment_before_any_write(self):
        claim(self.path, CONFIG, self.slot, self.slot.when)
        for config in (replace(CONFIG, timezone="UTC"), replace(CONFIG, am_start="06:01"),
                       replace(CONFIG, weekdays=(0, 1))):
            with self.assertRaisesRegex(ValueError, "new --db"):
                claim(self.path, config, self.slot, self.slot.when)
        self.assertEqual(len(read_samples(self.path)), 1)

    def test_legacy_metadata_accepts_same_default_schedule_and_adds_schedule(self):
        with connect(self.path) as db:
            db.executemany("INSERT INTO meta VALUES (?,?)", [
                ("route_fingerprint", CONFIG.fingerprint()), ("interval_minutes", "3")])
            db.commit()
        self.assertEqual(claim(self.path, CONFIG, self.slot, self.slot.when), "pending")
        with connect(self.path) as db:
            self.assertEqual(dict(db.execute("SELECT key,value FROM meta"))["schedule_json"],
                             CONFIG.schedule_json())

    def test_storage_local_date_uses_configured_zone_but_quota_stays_pacific(self):
        config = replace(CONFIG, timezone="Asia/Tokyo", monthly_request_limit=1)
        first = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
        second = first + timedelta(minutes=3)
        self.assertEqual(claim(self.path, config, Slot(first, "am"), first), "pending")
        self.assertEqual(read_samples(self.path)[0]["local_date"], "2026-10-01")
        self.assertEqual(claim(self.path, config, Slot(second, "am"), second), "budget_skipped")

    def test_manual_test_does_not_fill_scheduled_slot(self):
        collect(self.path, CONFIG, self.slot, self.slot.when, manual=True,
                routes_client=lambda *_: [ROUTE])
        self.assertEqual(claim(self.path, CONFIG, self.slot, self.slot.when), "pending")
        rows = read_samples(self.path)
        self.assertEqual(sum(r["kind"] == "manual" for r in rows), 1)

    def test_quota_month_uses_pacific_boundary(self):
        config = replace(CONFIG, monthly_request_limit=1)
        # Both requests are still in the same Pacific month, although UTC rolled over.
        first = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
        second = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
        claim(self.path, config, Slot(first.astimezone(AMSTERDAM), "am"), first)
        self.assertEqual(claim(self.path, config, Slot(second.astimezone(AMSTERDAM), "am"), second),
                         "budget_skipped")

    def test_geometry_not_persisted(self):
        collect(self.path, CONFIG, self.slot, self.slot.when,
                routes_client=lambda *_: [dict(ROUTE, points=[(52, 4), (52.1, 4.1)])])
        row = read_samples(self.path)[0]
        self.assertNotIn("points", json.loads(row["alternatives_json"])[0])

    def test_optional_feed_crash_does_not_discard_eta(self):
        def broken(*_):
            raise RuntimeError("Synthetic parser failure")
        status = collect(self.path, replace(CONFIG, ndw_enabled=True), self.slot, self.slot.when,
                         routes_client=lambda *_: [ROUTE], incidents_client=broken)
        row = read_samples(self.path)[0]
        self.assertEqual(status, "ok")
        self.assertEqual(row["duration_s"], 4500)
        self.assertEqual(row["ndw_status"], "unavailable")


class GoogleTests(unittest.TestCase):
    def test_request_direction_traffic_and_pro_only_fields(self):
        def fake(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload["origin"], waypoint(CONFIG.work))
            self.assertEqual(payload["destination"], waypoint(CONFIG.home))
            self.assertEqual(payload["routingPreference"], "TRAFFIC_AWARE_OPTIMAL")
            self.assertNotIn("departureTime", payload)
            self.assertNotIn("extraComputations", payload)
            self.assertNotIn("*", request.get_header("X-goog-fieldmask"))
            body = {"routes": [{"duration": "4200s", "staticDuration": "3000s",
                                "distanceMeters": 60000, "routeLabels": ["DEFAULT_ROUTE"]}]}
            return io.BytesIO(json.dumps(body).encode())
        self.assertEqual(request_routes(CONFIG, "pm", opener=fake)[0]["duration_s"], 4200)

    def test_default_route_is_not_assumed_first(self):
        result = parse_routes({"routes": [
            {"duration": "3000s", "staticDuration": "2000s", "distanceMeters": 50,
             "routeLabels": ["DEFAULT_ROUTE_ALTERNATE"]},
            {"duration": "3100s", "staticDuration": "2500s", "distanceMeters": 45,
             "routeLabels": ["DEFAULT_ROUTE"]},
        ]})
        self.assertEqual(result[0]["duration_s"], 3100)

    def test_key_and_address_not_exposed_in_http_errors(self):
        def fake(*args, **kwargs):
            raise HTTPError("https://example.invalid?key=secret", 403, "private address",
                            None, io.BytesIO(b"secret"))
        with self.assertRaisesRegex(QueryError, "^google_http_403$"):
            request_routes(CONFIG, "am", opener=fake)

    def test_polyline_decoding_and_truncation(self):
        self.assertEqual(decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq" + chr(96) + "@"),
                         [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])
        with self.assertRaises(QueryError):
            decode_polyline("_")


def ndw_xml(records, published="2026-10-05T06:00:00Z"):
    return (f'<messageContainer xmlns="http://datex2.eu/schema/3/situation" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><payload>'
            f'<publicationTime>{published}</publicationTime><situation id="one-event">'
            f'<informationStatus>real</informationStatus>{records}</situation>'
            f'</payload></messageContainer>').encode()


def ndw_record(identifier, bearing="90", start="2026-10-05T05:00:00Z", end=""):
    return f"""<situationRecord id="{identifier}" xsi:type="sit:Accident">
      <validity><validityStatus>definedByValidityTimeSpec</validityStatus>
      <validityTimeSpecification><overallStartTime>{start}</overallStartTime>
      {f'<overallEndTime>{end}</overallEndTime>' if end else ''}
      </validityTimeSpecification></validity>
      <locationReference><pointByCoordinates>
      {f'<bearing>{bearing}</bearing>' if bearing else ''}
      <pointCoordinates><latitude>52</latitude><longitude>4.01</longitude></pointCoordinates>
      </pointByCoordinates></locationReference></situationRecord>"""


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 6, 1, tzinfo=timezone.utc)
        self.route = [(52, 4), (52, 4.02)]

    def test_repeat_records_one_event_and_reverse_excluded(self):
        feed = parse_feed(ndw_xml(ndw_record("a") + ndw_record("b")), self.now)
        self.assertEqual(len(match_nearby(feed, self.route)["events"]), 1)
        self.assertEqual(match_nearby(feed, list(reversed(self.route)))["events"], [])

    def test_future_and_expired_records_not_current_incidents(self):
        feed = parse_feed(ndw_xml(ndw_record("future", start="2026-10-06T00:00:00Z")
                                + ndw_record("expired", end="2026-10-05T05:30:00Z")), self.now)
        self.assertEqual(feed["events"], [])

    def test_unknown_direction_is_explicit(self):
        result = match_nearby(parse_feed(ndw_xml(ndw_record("a", bearing="")), self.now), self.route)
        self.assertEqual(result["events"][0]["direction"], "unknown")

    def test_stale_feed_is_unknown_not_clear(self):
        feed = parse_feed(ndw_xml(ndw_record("a"), "2026-10-05T04:00:00Z"), self.now)
        self.assertEqual(match_nearby(feed, self.route)["status"], "stale")

    def test_gml_lat_lon_and_unsafe_xml(self):
        from traveltime.incidents import geometry
        import xml.etree.ElementTree as ET
        node = ET.fromstring('<location><gmlLineString srsName="WGS 84">'
                             '<posList>52 4 52.1 4.1</posList></gmlLineString></location>')
        self.assertEqual(geometry(node), [(52, 4), (52.1, 4.1)])
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE a><a/>', self.now)


class StatisticsTests(unittest.TestCase):
    def row(self, day, minutes, status="ok"):
        local = datetime.combine(day, datetime.min.time(), AMSTERDAM).replace(hour=7)
        return dict(status=status, kind="scheduled", local_date=day.isoformat(), direction="am",
                    scheduled_utc=local.astimezone(timezone.utc).isoformat(),
                    duration_s=minutes * 60, static_s=50 * 60, ndw_status="unavailable")

    def test_errors_and_missing_are_not_zeros_or_no_accidents(self):
        rows = [self.row(date(2026, 10, 5), 60), self.row(date(2026, 10, 5), 90),
                self.row(date(2026, 10, 5), 0, "error")]
        value = day_summary(rows, 60, CONFIG)
        self.assertEqual(value["median_minutes"], 75)
        self.assertEqual(value["delay_sample_pct"], 50)
        self.assertIsNone(value["nearby_accident_events"])
        self.assertEqual(value["valid_samples"], 2)

    def test_profile_has_one_value_per_day_and_small_n_hides_p90(self):
        rows = [self.row(date(2026, 10, 5), 60)] * 50
        result = profile(rows, 0, "am")[0]
        self.assertEqual(result["n_days"], 1)
        self.assertIsNone(result["p90"])
        rows += [self.row(date(2026, 10, 5) + timedelta(days=7*i), 60+10*i) for i in range(1, 5)]
        result = profile(rows, 0, "am")[0]
        self.assertEqual(result["n_days"], 5)
        self.assertEqual(result["p90"], 96)


if __name__ == "__main__":
    unittest.main()
