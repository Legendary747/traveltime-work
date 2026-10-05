from datetime import datetime, timezone
import logging
import uuid

from .db import claim, complete, fail
from .google import QueryError, request_routes
from .incidents import fetch_nearby

log = logging.getLogger(__name__)


def collect(path, config, slot, now=None, *, manual=False,
            routes_client=request_routes, incidents_client=fetch_nearby):
    config.validate(credentials=True)
    now = now or datetime.now(timezone.utc)
    manual_id = "manual:" + str(uuid.uuid4()) if manual else None
    identity = manual_id or slot.key
    status = claim(path, config, slot, now, manual_id=manual_id)
    if status != "pending":
        return status
    try:
        routes = routes_client(config, slot.direction)
        # Refresh time after route query; never use the slot's nominal timestamp.
        ndw_now = datetime.now(timezone.utc)
        ndw = {"status": "disabled", "events": []}
        if config.ndw_enabled:
            try:
                ndw = incidents_client(routes[0]["points"], ndw_now, config.ndw_corridor_meters)
            except Exception:
                # An optional feed/parser failure must not discard a valid paid ETA.
                ndw = {"status": "unavailable", "events": []}
        complete(path, identity, routes, ndw)
        log.info("%s %s: %.1f min; NDW %s", slot.when.isoformat(), slot.direction,
                 routes[0]["duration_s"] / 60, ndw["status"])
        return "ok"
    except QueryError as exc:
        fail(path, identity, str(exc))
        log.warning("%s %s: %s", slot.when.isoformat(), slot.direction, str(exc))
        return "error"
    except Exception:
        # Preserve the gap, fail visibly, but don't print potentially sensitive values.
        fail(path, identity, "unexpected_collection_error")
        log.error("Unexpected collection error. Sample marked as missing.")
        return "error"
