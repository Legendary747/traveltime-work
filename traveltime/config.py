from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hashlib
import json
import math
import os
import re

DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)
DEFAULT_TIMES = ("06:00", "09:00", "15:00", "18:00")


def load_dotenv(path=Path(".env")):
    """Small literal KEY=VALUE loader. No shell execution or variable expansion."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError("Invalid .env entry; use KEY=VALUE.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def waypoint(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("COMMUTE_HOME and COMMUTE_WORK must both be configured.")
    value = value.strip()
    if value.startswith("place_id:"):
        if not value[9:].strip():
            raise ValueError("Empty place ID.")
        return {"placeId": value[9:].strip()}
    if "," in value:
        parts = value.split(",")
        if len(parts) == 2:
            try:
                lat, lon = (float(x) for x in parts)
            except ValueError:
                pass
            else:
                if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Invalid latitude/longitude.")
                return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
    return {"address": value}


def minute_of_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Schedule times must use HH:MM from 00:00 to 23:59.")
    return int(value[:2]) * 60 + int(value[3:])


def _date(value, name):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} must be an ISO date or null.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date.") from None


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    home: str = ""
    work: str = ""
    interval_minutes: int = 3
    max_lateness_seconds: int = 60
    monthly_request_limit: int = 4500
    ndw_enabled: bool = True
    ndw_corridor_meters: float = 250
    delay_threshold_minutes: float = 15
    trip_threshold_minutes: float = 75
    start_date: date | None = None
    end_date: date | None = None
    exclude_dates: tuple[date, ...] = ()
    timezone: str = "Europe/Amsterdam"
    weekdays: tuple[int, ...] = DEFAULT_WEEKDAYS
    am_start: str = "06:00"
    am_end: str = "09:00"
    pm_start: str = "15:00"
    pm_end: str = "18:00"

    @classmethod
    def from_env(cls, config_file=None):
        """Defaults < optional local JSON < explicitly supplied environment values."""
        source = config_file if config_file is not None else os.getenv("TRAVELTIME_CONFIG_FILE")
        settings = {}
        if source:
            try:
                raw = Path(source).read_text(encoding="utf-8-sig")
                if len(raw) > 65536:
                    raise ValueError("Configuration file is too large.")
                settings = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("Configuration file must contain valid JSON.") from None
            allowed = set(cls.__dataclass_fields__) - {"api_key"}
            if not isinstance(settings, dict) or type(settings.get("schema_version")) is not int or settings.get("schema_version") != 1:
                raise ValueError("Configuration must be an object with schema_version: 1.")
            if set(settings) - allowed - {"schema_version"}:
                raise ValueError("Unknown configuration field; credentials belong in environment variables only.")
            settings = dict(settings)
            settings.pop("schema_version")
        names = {
            "home": "COMMUTE_HOME", "work": "COMMUTE_WORK", "timezone": "COMMUTE_TIMEZONE",
            "am_start": "AM_START", "am_end": "AM_END", "pm_start": "PM_START", "pm_end": "PM_END",
            "interval_minutes": "SAMPLE_INTERVAL_MINUTES", "max_lateness_seconds": "MAX_LATENESS_SECONDS",
            "monthly_request_limit": "MONTHLY_REQUEST_LIMIT", "ndw_enabled": "NDW_ENABLED",
            "ndw_corridor_meters": "NDW_CORRIDOR_METERS", "delay_threshold_minutes": "DELAY_THRESHOLD_MINUTES",
            "trip_threshold_minutes": "TRIP_THRESHOLD_MINUTES", "start_date": "MONITOR_START_DATE",
            "end_date": "MONITOR_END_DATE", "exclude_dates": "EXCLUDE_DATES", "weekdays": "MONITOR_WEEKDAYS",
        }
        integer_fields = {"interval_minutes", "max_lateness_seconds", "monthly_request_limit"}
        float_fields = {"ndw_corridor_meters", "delay_threshold_minutes", "trip_threshold_minutes"}
        for field, env in names.items():
            if env not in os.environ:
                continue
            value = os.environ[env].strip()
            try:
                if field in integer_fields:
                    value = int(value)
                elif field in float_fields:
                    value = float(value)
                elif field == "ndw_enabled":
                    if value.lower() not in ("true", "false"):
                        raise ValueError()
                    value = value.lower() == "true"
                elif field == "weekdays":
                    value = [int(part.strip()) for part in value.split(",")]
                elif field == "exclude_dates":
                    value = [part.strip() for part in value.split(",") if part.strip()]
            except ValueError:
                raise ValueError(f"Invalid {env} value.") from None
            settings[field] = value
        for field in ("start_date", "end_date"):
            if field in settings:
                settings[field] = _date(settings[field], field)
        if "exclude_dates" in settings:
            if not isinstance(settings["exclude_dates"], (list, tuple)):
                raise ValueError("exclude_dates must be a list of ISO dates.")
            settings["exclude_dates"] = tuple(_date(value, "exclude_dates") for value in settings["exclude_dates"])
            if None in settings["exclude_dates"]:
                raise ValueError("exclude_dates entries must be valid dates.")
        if "weekdays" in settings:
            if not isinstance(settings["weekdays"], (list, tuple)):
                raise ValueError("weekdays must be a list of integers, Monday=0 through Sunday=6.")
            settings["weekdays"] = tuple(settings["weekdays"])
        if "max_lateness_seconds" not in settings and type(settings.get("interval_minutes", 3)) is int:
            settings["max_lateness_seconds"] = min(60, settings.get("interval_minutes", 3) * 60 - 1)
        settings["api_key"] = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
        value = cls(**settings)
        value.validate()
        return value

    def validate(self, credentials=False):
        for field in ("home", "work", "api_key"):
            if not isinstance(getattr(self, field), str):
                raise ValueError(f"{field} must be a string.")
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 60:
            raise ValueError("SAMPLE_INTERVAL_MINUTES must be an integer from 1 to 60.")
        if type(self.max_lateness_seconds) is not int or not 0 <= self.max_lateness_seconds < self.interval_minutes * 60:
            raise ValueError("MAX_LATENESS_SECONDS must be nonnegative and less than the interval.")
        if type(self.monthly_request_limit) is not int or self.monthly_request_limit <= 0:
            raise ValueError("MONTHLY_REQUEST_LIMIT must be a positive integer.")
        if type(self.ndw_enabled) is not bool:
            raise ValueError("NDW_ENABLED must be true or false.")
        for x in (self.ndw_corridor_meters, self.delay_threshold_minutes, self.trip_threshold_minutes):
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
                raise ValueError("Thresholds must be positive finite numbers.")
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ValueError("COMMUTE_TIMEZONE must be a valid IANA time zone.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("COMMUTE_TIMEZONE must be a valid IANA time zone.") from None
        if (not isinstance(self.weekdays, (list, tuple)) or not self.weekdays
                or any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays)
                or len(set(self.weekdays)) != len(self.weekdays)):
            raise ValueError("MONITOR_WEEKDAYS must contain unique integers, Monday=0 through Sunday=6.")
        windows = self.windows
        if any(start >= end for _, start, end in windows):
            raise ValueError("Each sampling window must end after it starts on the same day; overnight windows are not supported.")
        if max(windows[0][1], windows[1][1]) < min(windows[0][2], windows[1][2]):
            raise ValueError("Sampling windows must not overlap.")
        if any(value is not None and type(value) is not date for value in (self.start_date, self.end_date)):
            raise ValueError("Monitor start/end dates must be valid dates.")
        if not isinstance(self.exclude_dates, (tuple, list)) or any(type(value) is not date for value in self.exclude_dates):
            raise ValueError("Excluded dates must be valid dates.")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("MONITOR_START_DATE is after MONITOR_END_DATE.")
        if credentials:
            if not self.api_key:
                raise ValueError("Set GOOGLE_MAPS_API_KEY in .env or environment.")
            if waypoint(self.home) == waypoint(self.work):
                raise ValueError("Home and work must be different.")

    @property
    def zone(self):
        return ZoneInfo(self.timezone)

    @property
    def windows(self):
        return (("am", minute_of_day(self.am_start), minute_of_day(self.am_end)),
                ("pm", minute_of_day(self.pm_start), minute_of_day(self.pm_end)))

    def window_minutes(self, direction):
        return next((start, end) for name, start, end in self.windows if name == direction)

    def schedule_metadata(self):
        return {"timezone": self.timezone, "weekdays": sorted(self.weekdays),
                "am_start": self.am_start, "am_end": self.am_end,
                "pm_start": self.pm_start, "pm_end": self.pm_end}

    def schedule_json(self):
        return json.dumps(self.schedule_metadata(), sort_keys=True, separators=(",", ":"))

    def default_schedule(self):
        return (self.timezone == "Europe/Amsterdam" and tuple(sorted(self.weekdays)) == DEFAULT_WEEKDAYS
                and (self.am_start, self.am_end, self.pm_start, self.pm_end) == DEFAULT_TIMES)

    def fingerprint(self):
        # Preserve v0.1 fingerprints for unchanged default schedules; no secrets are stored.
        content = [waypoint(self.home), waypoint(self.work), self.interval_minutes, self.timezone]
        if not self.default_schedule():
            content.append(self.schedule_metadata())
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def allowed_date(self, day):
        return (day.weekday() in self.weekdays and day not in self.exclude_dates
                and (self.start_date is None or day >= self.start_date)
                and (self.end_date is None or day <= self.end_date))
