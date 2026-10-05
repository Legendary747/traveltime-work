"""Cloud Run HTTP entry point; authentication is enforced by Cloud Run IAM."""
from datetime import datetime, timezone
import logging
import os

from flask import Flask, jsonify, request

from .collector import collect_store
from .config import Config
from .google import QueryError, request_routes
from .incidents import fetch_nearby
from .schedule import AMSTERDAM, slots_on

log = logging.getLogger(__name__)


def scheduled_slot(value, now, config):
    """Use Scheduler's original time, including on retries; never backfill traffic."""
    try:
        scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("Missing or invalid X-CloudScheduler-ScheduleTime.") from None
    if scheduled.tzinfo is None or scheduled.utcoffset() is None:
        raise ValueError("Scheduler timestamp must include a timezone.")
    scheduled = scheduled.astimezone(AMSTERDAM)
    lateness = (now - scheduled).total_seconds()
    if not 0 <= lateness <= config.max_lateness_seconds:
        return None
    return next((slot for slot in slots_on(scheduled.date(), config)
                 if slot.when == scheduled), None)


def create_app(config=None, store=None, *, clock=None,
               routes_client=request_routes, incidents_client=fetch_nearby):
    config = config or Config.from_env()
    config.validate(credentials=True)
    if config.interval_minutes != 3:
        raise ValueError("Cloud Scheduler deployment requires SAMPLE_INTERVAL_MINUTES=3.")
    if store is None:
        from .firestore_store import FirestoreStore
        store = FirestoreStore(project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                               experiment=os.getenv("CLOUD_EXPERIMENT", "commute-v1"))
    clock = clock or (lambda: datetime.now(timezone.utc))
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024

    @app.get("/healthz")
    def health():
        return jsonify(status="ready")

    @app.post("/collect")
    def sample():
        if os.getenv("COLLECTION_ENABLED", "true").lower() != "true":
            return jsonify(status="collection_paused")
        now = clock()
        try:
            slot = scheduled_slot(request.headers.get("X-CloudScheduler-ScheduleTime"),
                                  now, config)
        except ValueError:
            return jsonify(status="invalid_scheduler_timestamp"), 400
        if slot is None:
            return jsonify(status="skipped_outside_slot")

        def timely_routes(settings, direction):
            # A slow cold start/transaction must not turn into historical "backfill".
            if (clock() - slot.when).total_seconds() > config.max_lateness_seconds:
                raise QueryError("late_before_query")
            return routes_client(settings, direction)

        try:
            result = collect_store(store, config, slot, now, routes_client=timely_routes,
                                   incidents_client=incidents_client)
        except ValueError:
            log.error("Cloud experiment configuration mismatch; no query sent.")
            return jsonify(status="configuration_error"), 409
        except Exception:
            # No tracebacks containing request URLs, auth tokens or personal endpoints.
            log.error("Cloud storage operation failed; inspect IAM and Firestore availability.")
            return jsonify(status="storage_error"), 503
        # An observed API error is already durable; redelivery must not re-query Google.
        return jsonify(status=result)

    return app
