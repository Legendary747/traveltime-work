"""Read Firestore into a local SQLite mirror for the existing charts and backup."""
import argparse
from datetime import datetime
import json
import os
from zoneinfo import ZoneInfo

from .db import connect
from .config import Config


def merge_snapshot(path, metadata, rows, source):
    """Atomic merge: failed downloads or a different experiment cannot erase history."""
    if not metadata:
        raise ValueError("No cloud observations yet. Wait for the first scheduled sample.")
    required = {"route_fingerprint", "interval_minutes"}
    if not required <= metadata.keys():
        raise ValueError("Invalid cloud experiment metadata.")
    interval = metadata["interval_minutes"]
    if not isinstance(interval, str) or not interval.isdigit() or not 1 <= int(interval) <= 60:
        raise ValueError("Cloud experiment has an invalid sampling interval.")
    schedule_json = metadata.get("schedule_json", Config().schedule_json())
    try:
        schedule = json.loads(schedule_json)
        if not isinstance(schedule, dict) or set(schedule) != set(Config().schedule_metadata()):
            raise ValueError()
        settings = Config(**schedule)
        settings.validate()
        schedule_json = settings.schedule_json()
    except (TypeError, ValueError):
        raise ValueError("Invalid cloud schedule metadata.") from None
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = dict(db.execute("SELECT key,value FROM meta"))
        for key in required:
            if key in existing and existing[key] != metadata[key]:
                raise ValueError("Existing SQLite data belongs to another route/schedule/interval.")
        existing_schedule = existing.get("schedule_json", Config().schedule_json())
        if existing and existing_schedule != schedule_json:
            raise ValueError("Existing SQLite data belongs to another schedule.")
        if existing.get("cloud_source", source) != source:
            raise ValueError("Existing SQLite data belongs to another cloud experiment.")
        for key, value in {**{k: metadata[k] for k in required}, "cloud_source": source,
                           "schedule_json": schedule_json}.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        columns = [row[1] for row in db.execute("PRAGMA table_info(samples)")]
        placeholders = ",".join("?" for _ in columns)
        for row in rows:
            # Never discard a completed local observation because an export caught an
            # in-flight cloud transaction. Otherwise refresh from the durable source.
            current = db.execute("SELECT status FROM samples WHERE slot_id=?",
                                 (row["slot_id"],)).fetchone()
            if current and current["status"] == "ok" and row["status"] == "pending":
                continue
            db.execute(f"INSERT OR REPLACE INTO samples ({','.join(columns)}) "
                       f"VALUES ({placeholders})", [row.get(k) for k in columns])
            if row.get("quota_reserved"):
                month = row.get("billing_month") or datetime.fromisoformat(
                    row["queried_utc"]).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")
                db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?)",
                           (row["slot_id"], row["queried_utc"], month))
        db.commit()
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export cloud observations; no Routes API calls")
    parser.add_argument("--db", default="data/cloud-commute.sqlite3")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT"), required=False)
    parser.add_argument("--experiment", default=os.getenv("CLOUD_EXPERIMENT", "commute-v1"))
    args = parser.parse_args(argv)
    if not args.project:
        parser.error("--project or GOOGLE_CLOUD_PROJECT is required")
    try:
        from .firestore_store import FirestoreStore
        store = FirestoreStore(args.project, args.experiment)
        metadata, rows = store.snapshot()  # Finish reading before changing the local backup.
        count = merge_snapshot(args.db, metadata, rows,
                               f"firestore:{args.project}/(default)/{store.experiment}")
        print(f"Exported {count} cloud observations. No Google Routes requests were made.")
    except ValueError as exc:
        print(str(exc))
        return 2
    except Exception:
        print("Cloud export failed. Check authentication, Firestore access and connectivity.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
