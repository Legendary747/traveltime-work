from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples(
    slot_id TEXT PRIMARY KEY, scheduled_utc TEXT NOT NULL, queried_utc TEXT NOT NULL,
    local_date TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, error_code TEXT, duration_s REAL, static_s REAL,
    distance_m INTEGER, route_key TEXT, description TEXT,
    alternatives_json TEXT, ndw_status TEXT, ndw_json TEXT
);
CREATE TABLE IF NOT EXISTS requests(
    slot_id TEXT PRIMARY KEY, attempted_utc TEXT NOT NULL, billing_month TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_by_date ON samples(local_date, direction);
"""


@contextmanager
def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    # DELETE journal gives a single portable DB for GitHub checkpoints after commits.
    db.execute("PRAGMA journal_mode=DELETE")
    db.executescript(SCHEMA)
    try:
        yield db
    finally:
        db.close()


def claim(path, config, slot, queried, *, manual_id=None):
    """Atomically deduplicate and reserve quota BEFORE the HTTP request."""
    identity = manual_id or slot.key
    utc = queried.astimezone(timezone.utc)
    # Google usage day/month accounting: US Pacific. No dependence on host TZ.
    from zoneinfo import ZoneInfo
    billing_month = utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        fingerprint = db.execute("SELECT value FROM meta WHERE key='route_fingerprint'").fetchone()
        if fingerprint and fingerprint[0] != config.fingerprint():
            db.rollback()
            raise ValueError("Route/interval changed. Use a new --db to keep experiments separate.")
        if db.execute("SELECT 1 FROM samples WHERE slot_id=?", (identity,)).fetchone():
            db.rollback()
            return "duplicate"
        for key, value in (("route_fingerprint", config.fingerprint()),
                           ("interval_minutes", str(config.interval_minutes))):
            db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
        used = db.execute("SELECT COUNT(*) FROM requests WHERE billing_month=?",
                          (billing_month,)).fetchone()[0]
        status = "pending" if used < config.monthly_request_limit else "budget_skipped"
        db.execute("""INSERT INTO samples
            (slot_id,scheduled_utc,queried_utc,local_date,direction,kind,status)
            VALUES (?,?,?,?,?,?,?)""",
                   (identity, slot.key, utc.isoformat(), slot.when.date().isoformat(),
                    slot.direction, "manual" if manual_id else "scheduled", status))
        if status == "pending":
            db.execute("INSERT INTO requests VALUES (?,?,?)",
                       (identity, utc.isoformat(), billing_month))
        db.commit()
        return status


def complete(path, identity, routes, ndw):
    main = routes[0]
    safe_routes = [{k: v for k, v in r.items() if k != "points"} for r in routes]
    with connect(path) as db:
        db.execute("""UPDATE samples SET status='ok',duration_s=?,static_s=?,distance_m=?,
            route_key=?,description=?,alternatives_json=?,ndw_status=?,ndw_json=?
            WHERE slot_id=?""",
                   (main["duration_s"], main["static_s"], main["distance_m"],
                    main["route_key"], main["description"], json.dumps(safe_routes),
                    ndw["status"], json.dumps(ndw), identity))
        db.commit()


def fail(path, identity, code):
    with connect(path) as db:
        db.execute("UPDATE samples SET status='error',error_code=? WHERE slot_id=?",
                   (code, identity))
        db.commit()


def read_samples(path):
    with connect(path) as db:
        return [dict(x) for x in db.execute("SELECT * FROM samples ORDER BY scheduled_utc")]
