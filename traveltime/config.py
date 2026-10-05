from dataclasses import dataclass
from datetime import date
from pathlib import Path
import hashlib
import json
import math
import os


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
    if not value.strip():
        raise ValueError("COMMUTE_HOME and COMMUTE_WORK must both be configured.")
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
                pass  # An ordinary address can contain a comma.
            else:
                if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Invalid latitude/longitude.")
                return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
    return {"address": value.strip()}


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

    @classmethod
    def from_env(cls):
        def date_or_none(key):
            return date.fromisoformat(os.environ[key]) if os.getenv(key) else None
        flag = os.getenv("NDW_ENABLED", "true").lower()
        if flag not in ("true", "false"):
            raise ValueError("NDW_ENABLED must be true or false.")
        value = cls(
            api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
            home=os.getenv("COMMUTE_HOME", "").strip(),
            work=os.getenv("COMMUTE_WORK", "").strip(),
            interval_minutes=int(os.getenv("SAMPLE_INTERVAL_MINUTES", "3")),
            max_lateness_seconds=int(os.getenv("MAX_LATENESS_SECONDS", "60")),
            monthly_request_limit=int(os.getenv("MONTHLY_REQUEST_LIMIT", "4500")),
            ndw_enabled=flag == "true",
            ndw_corridor_meters=float(os.getenv("NDW_CORRIDOR_METERS", "250")),
            delay_threshold_minutes=float(os.getenv("DELAY_THRESHOLD_MINUTES", "15")),
            trip_threshold_minutes=float(os.getenv("TRIP_THRESHOLD_MINUTES", "75")),
            start_date=date_or_none("MONITOR_START_DATE"),
            end_date=date_or_none("MONITOR_END_DATE"),
            exclude_dates=tuple(date.fromisoformat(x.strip()) for x in
                                os.getenv("EXCLUDE_DATES", "").split(",") if x.strip()),
        )
        value.validate()
        return value

    def validate(self, credentials=False):
        if self.interval_minutes not in (1, 3, 5):
            raise ValueError("SAMPLE_INTERVAL_MINUTES must be 1, 3 or 5.")
        if not 0 <= self.max_lateness_seconds < self.interval_minutes * 60:
            raise ValueError("MAX_LATENESS_SECONDS must be less than the interval.")
        if self.monthly_request_limit <= 0:
            raise ValueError("MONTHLY_REQUEST_LIMIT must be positive.")
        for x in (self.ndw_corridor_meters, self.delay_threshold_minutes,
                  self.trip_threshold_minutes):
            if not math.isfinite(x) or x <= 0:
                raise ValueError("Thresholds must be positive finite numbers.")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("MONITOR_START_DATE is after MONITOR_END_DATE.")
        if credentials:
            if not self.api_key:
                raise ValueError("Set GOOGLE_MAPS_API_KEY in .env or environment.")
            waypoint(self.home)
            waypoint(self.work)
            if self.home == self.work:
                raise ValueError("Home and work must be different.")

    def fingerprint(self):
        # No addresses or credentials are written to metadata/logs.
        content = json.dumps([waypoint(self.home), waypoint(self.work),
                              self.interval_minutes, "Europe/Amsterdam"],
                             sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()

    def allowed_date(self, day):
        return (day.weekday() < 5 and day not in self.exclude_dates
                and (self.start_date is None or day >= self.start_date)
                and (self.end_date is None or day <= self.end_date))
