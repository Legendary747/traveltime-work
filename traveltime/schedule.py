from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
WINDOWS = (("am", 6, 9), ("pm", 15, 18))


@dataclass(frozen=True)
class Slot:
    when: datetime
    direction: str

    @property
    def key(self):
        return self.when.astimezone(UTC).isoformat(timespec="seconds")


def aware(now):
    if now.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required.")
    return now.astimezone(AMSTERDAM)


def slots_on(day, config):
    if not config.allowed_date(day):
        return []
    slots = []
    for direction, start, end in WINDOWS:
        value = datetime.combine(day, time(start), AMSTERDAM)
        stop = datetime.combine(day, time(end), AMSTERDAM)
        while value < stop:
            slots.append(Slot(value, direction))
            value += timedelta(minutes=config.interval_minutes)
    return slots


def due_slot(now, config):
    local = aware(now)
    for slot in reversed(slots_on(local.date(), config)):
        lateness = (local - slot.when).total_seconds()
        if 0 <= lateness <= config.max_lateness_seconds:
            return slot
    return None


def next_slot(now, config):
    local = aware(now)
    day = max(local.date(), config.start_date or local.date())
    # Date exclusions can be arbitrarily long. Advance until a valid weekday.
    while config.end_date is None or day <= config.end_date:
        for slot in slots_on(day, config):
            if slot.when > local:
                return slot
        day += timedelta(days=1)
    return None


def peak_end(now, config):
    """Current peak or a peak beginning within 10 minutes; never wait all day."""
    local = aware(now)
    if not config.allowed_date(local.date()):
        return None
    for _, start, end in WINDOWS:
        first = datetime.combine(local.date(), time(start), AMSTERDAM)
        last = datetime.combine(local.date(), time(end), AMSTERDAM)
        if first - timedelta(minutes=10) <= local < last:
            return last
    return None
