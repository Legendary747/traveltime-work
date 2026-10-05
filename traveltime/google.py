import hashlib
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import waypoint

ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
# Deliberately no TRAFFIC_ON_POLYLINE, tolls, steps, or wildcard fields.
FIELD_MASK = ",".join((
    "routes.duration", "routes.staticDuration", "routes.distanceMeters",
    "routes.description", "routes.routeLabels", "routes.polyline.encodedPolyline",
))


class QueryError(Exception):
    """Sanitized code only; API bodies can echo addresses or credentials."""


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(\.\d+)?s", value):
        raise QueryError("invalid_duration")
    result = float(value[:-1])
    if not math.isfinite(result) or result < 0:
        raise QueryError("invalid_duration")
    return result


def decode_polyline(encoded):
    index, lat, lon, result = 0, 0, 0, []
    while index < len(encoded):
        values = []
        for _ in range(2):
            number, shift = 0, 0
            while True:
                if index >= len(encoded) or shift > 30:
                    raise QueryError("invalid_polyline")
                part = ord(encoded[index]) - 63
                index += 1
                if not 0 <= part <= 63:
                    raise QueryError("invalid_polyline")
                number |= (part & 31) << shift
                shift += 5
                if part < 32:
                    break
            values.append(~(number >> 1) if number & 1 else number >> 1)
        lat += values[0]
        lon += values[1]
        if not (-9000000 <= lat <= 9000000 and -18000000 <= lon <= 18000000):
            raise QueryError("invalid_polyline")
        result.append((lat / 1e5, lon / 1e5))
    return result


def parse_routes(payload):
    raw = payload.get("routes", [])
    if not raw:
        raise QueryError("no_route")
    routes = []
    try:
        for route in raw:
            polyline = route.get("polyline", {}).get("encodedPolyline", "")
            distance = int(route["distanceMeters"])
            if distance < 0:
                raise QueryError("invalid_distance")
            routes.append({
                "duration_s": seconds(route["duration"]),
                "static_s": seconds(route["staticDuration"]),
                "distance_m": distance,
                "description": route.get("description", ""),
                "labels": route.get("routeLabels", []),
                "route_key": hashlib.sha256(polyline.encode()).hexdigest()[:16],
                # Geometry is transient: used for NDW matching, not saved to the DB.
                "points": decode_polyline(polyline) if polyline else [],
            })
    except (KeyError, TypeError, ValueError) as exc:
        raise QueryError("invalid_response") from exc
    routes.sort(key=lambda r: "DEFAULT_ROUTE" not in r["labels"])
    return routes


def request_routes(config, direction, opener=urlopen):
    origin, destination = ((config.home, config.work) if direction == "am"
                           else (config.work, config.home))
    payload = {
        "origin": waypoint(origin),
        "destination": waypoint(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "computeAlternativeRoutes": True,
        "polylineQuality": "HIGH_QUALITY",
        "languageCode": "en",
        "units": "METRIC",
    }
    # Omitting departureTime means departure at request time, never historical backfill.
    request = Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }, method="POST")
    try:
        with opener(request, timeout=35) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise QueryError("response_too_large")
        return parse_routes(json.loads(body))
    except HTTPError as exc:
        # Do not log str(exc) or response body.
        raise QueryError(f"google_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise QueryError("google_network_error") from None
    except (ValueError, AttributeError):
        raise QueryError("google_invalid_json") from None
