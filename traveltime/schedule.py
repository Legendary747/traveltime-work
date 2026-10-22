from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")  # Compatibility export for existing clients.
UTC = timezone.utc
WINDOWS = (("am", 6, 9), ("pm", 15, 18))  # Legacy default; runtime uses Config.windows.


@dataclass(frozen=True)
class Slot:
    when: datetime
    direction: str

    @property
    def key(self):
        return self.when.astimezone(UTC).isoformat(timespec="seconds")


def aware(now, config=None):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return now.astimezone(config.zone if config is not None else AMSTERDAM)


def slots_on(day, config):
    if not config.allowed_date(day):
        return []
    slots = []
    for direction, start, end in config.windows:
        for minute in range(start, end, config.interval_minutes):
            # One observation per wall-clock slot. Skip nonexistent spring-forward
            # times; the first occurrence (fold=0) is used during a repeated hour.
            value = datetime.combine(day, time(minute // 60, minute % 60), config.zone)
            if value.astimezone(UTC).astimezone(config.zone).replace(tzinfo=None) != value.replace(tzinfo=None):
                continue
            slots.append(Slot(value, direction))
    return sorted(slots, key=lambda slot: slot.when.astimezone(UTC))


def due_slot(now, config):
    local = aware(now, config)
    utc = now.astimezone(UTC)
    for slot in reversed(slots_on(local.date(), config)):
        start, end = config.window_minutes(slot.direction)
        if not start <= local.hour * 60 + local.minute < end:
            continue
        lateness = (utc - slot.when.astimezone(UTC)).total_seconds()
        if 0 <= lateness <= config.max_lateness_seconds:
            return slot
    return None


def next_slot(now, config):
    local = aware(now, config)
    utc = now.astimezone(UTC)
    day = max(local.date(), config.start_date or local.date())
    while config.end_date is None or day <= config.end_date:
        for slot in slots_on(day, config):
            if slot.when.astimezone(UTC) > utc:
                return slot
        day += timedelta(days=1)
    return None


def peak_end(now, config):
    """Current window or one beginning within 10 minutes; never wait all day."""
    local = aware(now, config)
    if not config.allowed_date(local.date()):
        return None
    utc = now.astimezone(UTC)
    for _, start, end in sorted(config.windows, key=lambda window: window[1]):
        first = datetime.combine(local.date(), time(start // 60, start % 60), config.zone)
        last = datetime.combine(local.date(), time(end // 60, end % 60), config.zone)
        if first.astimezone(UTC) - timedelta(minutes=10) <= utc < last.astimezone(UTC):
            return last
    return None
