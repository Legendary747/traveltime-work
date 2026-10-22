from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traveltime.cli import main
from traveltime.config import Config, waypoint


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def settings(self, **values):
        self.path.write_text(json.dumps({"schema_version": 1, **values}), encoding="utf-8")
        return self.path

    def test_legacy_environment_defaults_and_fingerprint_are_preserved(self):
        os.environ.update(GOOGLE_MAPS_API_KEY="synthetic-key", COMMUTE_HOME="52.0,4.3",
                          COMMUTE_WORK="52.1,5.1")
        config = Config.from_env()
        config.validate(credentials=True)
        self.assertEqual(config.interval_minutes, 3)
        self.assertEqual(config.max_lateness_seconds, 60)
        self.assertEqual(config.weekdays, (0, 1, 2, 3, 4))
        self.assertEqual(config.windows, (("am", 360, 540), ("pm", 900, 1080)))
        legacy = [waypoint(config.home), waypoint(config.work), 3, "Europe/Amsterdam"]
        self.assertEqual(config.fingerprint(), hashlib.sha256(
            json.dumps(legacy, sort_keys=True).encode()).hexdigest())
        self.assertEqual(config.fingerprint(), replace(config, api_key="rotated").fingerprint())

    def test_file_then_explicit_environment_precedence(self):
        self.settings(home="Origin Station", work="Destination Station", timezone="Asia/Tokyo",
                      weekdays=[5, 6], am_start="08:17", am_end="09:52", interval_minutes=7,
                      monthly_request_limit=99, ndw_enabled=False)
        os.environ.update(TRAVELTIME_CONFIG_FILE=str(self.path), COMMUTE_WORK="Another Station",
                          SAMPLE_INTERVAL_MINUTES="11", MONITOR_WEEKDAYS="1,3,5")
        config = Config.from_env()
        self.assertEqual(config.home, "Origin Station")
        self.assertEqual(config.work, "Another Station")
        self.assertEqual(config.timezone, "Asia/Tokyo")
        self.assertEqual(config.weekdays, (1, 3, 5))
        self.assertEqual(config.am_start, "08:17")
        self.assertEqual(config.interval_minutes, 11)
        self.assertEqual(config.monthly_request_limit, 99)
        self.assertFalse(config.ndw_enabled)
        os.environ["COMMUTE_HOME"] = ""
        self.assertEqual(Config.from_env().home, "")  # Explicit emptiness must not reveal a file value.

    def test_explicit_path_wins_and_dates_are_typed(self):
        os.environ["TRAVELTIME_CONFIG_FILE"] = "not-a-real-file.json"
        config = Config.from_env(self.settings(start_date="2026-10-01", end_date="2026-11-01",
                                               exclude_dates=["2026-10-05"]))
        self.assertEqual(config.start_date, date(2026, 10, 1))
        self.assertEqual(config.exclude_dates, (date(2026, 10, 5),))
        self.assertFalse(config.allowed_date(date(2026, 10, 5)))

    def test_one_minute_default_lateness_is_valid_but_explicit_invalid_is_rejected(self):
        config = Config.from_env(self.settings(interval_minutes=1))
        self.assertEqual(config.max_lateness_seconds, 59)
        with self.assertRaisesRegex(ValueError, "less than the interval"):
            Config.from_env(self.settings(interval_minutes=1, max_lateness_seconds=60))
        os.environ["MAX_LATENESS_SECONDS"] = "0"
        self.assertEqual(Config.from_env(self.settings(interval_minutes=1)).max_lateness_seconds, 0)

    def test_unknown_fields_and_file_credentials_are_rejected(self):
        for content in ({"api_key": "secret-never-print"}, {"GOOGLE_MAPS_API_KEY": "secret-never-print"},
                        {"typo_interval": 7}):
            with self.subTest(content=list(content)), self.assertRaises(ValueError) as error:
                Config.from_env(self.settings(**content))
            self.assertNotIn("secret-never-print", str(error.exception))

    def test_invalid_settings_fail_before_collection(self):
        invalid = [
            {"interval_minutes": 0}, {"interval_minutes": 61}, {"interval_minutes": 3.0},
            {"interval_minutes": True}, {"interval_minutes": "7"},
            {"max_lateness_seconds": -1}, {"monthly_request_limit": 0},
            {"ndw_enabled": "false"}, {"delay_threshold_minutes": float("nan")},
            {"timezone": "Not/AZone"}, {"weekdays": []}, {"weekdays": [0, 0]},
            {"weekdays": [7]}, {"weekdays": [True]}, {"weekdays": "0,1"},
            {"am_start": "6:00"}, {"am_end": "24:00"},
            {"am_start": "23:00", "am_end": "02:00"},
            {"am_start": "15:30", "am_end": "16:00"},
            {"start_date": "20261005"}, {"start_date": "2026-02-30"},
            {"start_date": "2026-10-05", "end_date": "2026-10-01"},
            {"exclude_dates": [None]}, {"home": None},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config.from_env(self.settings(**values))

    def test_malformed_json_and_schema_are_rejected(self):
        for content in ("{", "[]", "null", "{}", '{"schema_version": true}', '{"schema_version": 2}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ValueError):
                Config.from_env(self.path)

    def test_changed_route_schedule_or_interval_is_separate_but_weekday_order_is_not(self):
        config = Config(home="Origin Station", work="Destination Station")
        for values in ({"home": "Another Station"}, {"interval_minutes": 7},
                       {"timezone": "UTC"}, {"weekdays": (0, 1)}, {"am_start": "06:01"}):
            self.assertNotEqual(config.fingerprint(), replace(config, **values).fingerprint())
        self.assertEqual(config.fingerprint(), replace(config, weekdays=(4, 3, 2, 1, 0)).fingerprint())

    def test_doctor_uses_file_without_network_or_disclosing_credentials_or_locations(self):
        path = self.settings(home="private-origin-fixture", work="private-destination-fixture",
                             timezone="Asia/Tokyo", weekdays=[5], am_start="08:17", am_end="09:52",
                             interval_minutes=7)
        os.environ["GOOGLE_MAPS_API_KEY"] = "private-api-key-fixture"
        output = io.StringIO()
        with patch("traveltime.cli.load_dotenv"), patch("urllib.request.urlopen") as network, redirect_stdout(output):
            self.assertEqual(main(["--config", str(path), "doctor"]), 0)
        network.assert_not_called()
        text = output.getvalue()
        self.assertIn("Asia/Tokyo", text)
        self.assertIn("08:17", text)
        self.assertIn("7 minutes", text)
        self.assertNotIn("private-", text)


if __name__ == "__main__":
    unittest.main()
