"""NDW DATEX II v2/v3. Spatial candidates, never a personal accident probability."""
from datetime import datetime, timezone
import gzip
import io
import math
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

NDW_URL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


def text(node, name):
    value = node.find(".//{*}" + name)
    return value.text.strip() if value is not None and value.text else None


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    if result is not None and result.tzinfo is None:
        raise ValueError("NDW timestamp has no timezone")
    return result


def geometry(node):
    points = []
    for coord in node.findall(".//{*}pointCoordinates"):
        try:
            points.append((float(text(coord, "latitude")), float(text(coord, "longitude"))))
        except (ValueError, TypeError):
            continue
    # NDW's WGS 84 DATEX posList order is latitude, longitude.
    for line in node.findall(".//{*}gmlLineString"):
        if line.get("srsName") not in ("WGS 84", "urn:ogc:def:crs:EPSG::4326"):
            continue
        raw = text(line, "posList")
        try:
            values = [float(x) for x in raw.split()]
            if len(values) % 2 == 0:
                points.extend(zip(values[::2], values[1::2]))
        except (TypeError, AttributeError, ValueError):
            continue
    return [(a, b) for a, b in points if math.isfinite(a) and math.isfinite(b)
            and -90 <= a <= 90 and -180 <= b <= 180]


def parse_feed(data, now):
    if len(data) > 30_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Unsafe or oversized NDW XML.")
    root = ET.fromstring(data)
    published = timestamp(text(root, "publicationTime"))
    if published is None:
        raise ValueError("No NDW publication timestamp.")
    age = (now - published).total_seconds()
    # Stale/future-dated feeds are not interpreted as an absence of incidents.
    status = "ok" if -120 <= age <= 600 else "stale"
    events, unlocated, invalid = [], 0, 0
    for situation in root.findall(".//{*}situation"):
        info = text(situation, "informationStatus")
        if info and info != "real":
            continue
        for record in situation.findall("./{*}situationRecord"):
            try:
                valid = text(record, "validityStatus")
                if valid == "suspended":
                    continue
                start = timestamp(text(record, "overallStartTime"))
                end = timestamp(text(record, "overallEndTime"))
                if start and start > now or end and end <= now:
                    continue
                kind = record.get(XSI_TYPE, "").split(":")[-1]
                if kind == "Accident" or text(record, "causeType") == "accident":
                    category = "accident"
                elif kind == "AbnormalTraffic":
                    category = "congestion"
                elif kind in ("RoadOrCarriagewayOrLaneManagement", "ReroutingManagement"):
                    category = "road_management"
                elif kind in ("VehicleObstruction", "GeneralObstruction"):
                    category = "obstruction"
                else:
                    continue
                points = geometry(record)
                if not points:
                    unlocated += 1
                    continue
                bearing_text = text(record, "bearing")
                bearing = float(bearing_text) % 360 if bearing_text else None
                event_id = situation.get("id") or record.get("id")
                if not event_id:
                    invalid += 1
                    continue
                events.append({
                    "id": event_id, "record_id": record.get("id"), "category": category,
                    "record_type": kind, "start": start.isoformat() if start else None,
                    "end": end.isoformat() if end else None, "bearing": bearing,
                    "points": points,
                })
            except (ValueError, TypeError):
                invalid += 1
    return {
        "status": status, "published": published.isoformat(),
        "unlocated_records": unlocated, "invalid_records": invalid, "events": events,
    }


def distance_to_route(point, route):
    """Local metric projection, adequate for short-distance matching in NL."""
    lat, lon = point
    scale = 111320 * math.cos(math.radians(lat))
    nearest, heading = math.inf, None
    for a, b in zip(route, route[1:]):
        ax, ay = (a[1] - lon) * scale, (a[0] - lat) * 111320
        bx, by = (b[1] - lon) * scale, (b[0] - lat) * 111320
        dx, dy = bx - ax, by - ay
        norm = dx * dx + dy * dy
        if norm < 1:
            continue
        t = min(1, max(0, -(ax * dx + ay * dy) / norm))
        distance = math.hypot(ax + t * dx, ay + t * dy)
        if distance < nearest:
            nearest, heading = distance, math.degrees(math.atan2(dx, dy)) % 360
    return nearest, heading


def match_nearby(feed, route, corridor_meters=250):
    result = {k: v for k, v in feed.items() if k != "events"}
    result["events"] = []
    if feed["status"] != "ok" or len(route) < 2:
        if len(route) < 2:
            result["status"] = "no_route_geometry"
        return result
    south, north = min(x[0] for x in route), max(x[0] for x in route)
    west, east = min(x[1] for x in route), max(x[1] for x in route)
    margin_lat = corridor_meters / 111000
    margin_lon = corridor_meters / (111000 * math.cos(math.radians((south + north) / 2)))
    selected = {}
    for event in feed["events"]:
        candidates = [p for p in event["points"]
                      if south - margin_lat <= p[0] <= north + margin_lat
                      and west - margin_lon <= p[1] <= east + margin_lon]
        if not candidates:
            continue
        # Candidate marker only: line crossings without a nearby vertex may be missed.
        distance, heading = min((distance_to_route(p, route) for p in candidates),
                                key=lambda x: x[0])
        if distance > corridor_meters:
            continue
        direction = "unknown"
        if event["bearing"] is not None and heading is not None:
            difference = abs((event["bearing"] - heading + 180) % 360 - 180)
            if difference > 100:
                continue  # Clearly opposite carriageway.
            if difference <= 60:
                direction = "consistent"
        value = {k: v for k, v in event.items() if k != "points"}
        value.update(distance_m=round(distance), direction=direction,
                     match="near_route_candidate")
        # Several records/versions in one situation are one observed event.
        previous = selected.get(event["id"])
        if not previous or (value["category"] == "accident" and previous["category"] != "accident"):
            selected[event["id"]] = value
    result["events"] = list(selected.values())
    return result


def fetch_nearby(route, now, corridor_meters=250, opener=urlopen):
    try:
        request = Request(NDW_URL, headers={"User-Agent": "traveltime-work/0.1"})
        with opener(request, timeout=20) as response:
            compressed = response.read(5_000_001)
        if len(compressed) > 5_000_000:
            raise ValueError("NDW download too large.")
        if compressed[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as data:
                raw = data.read(30_000_001)
        else:
            raw = compressed
        return match_nearby(parse_feed(raw, now), route, corridor_meters)
    except (URLError, HTTPError, OSError, ValueError, ET.ParseError, EOFError):
        return {"status": "unavailable", "events": []}
