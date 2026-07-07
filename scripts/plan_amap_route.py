#!/usr/bin/env python3
"""Plan an Amap driving route while avoiding JSON coordinates."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from update_jinjing_points import (
    SOURCE_URL,
    classify_comment,
    fetch,
    is_recent_invalid_override,
    keep_inside_sixth_ring,
    merge_cached_validity,
    parse_points,
    read_points,
    update_validity_for_ids,
    write_json,
)


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AVOID_JSON = os.path.join(HERE, "jinjing365_points.json")
AMAP = "https://restapi.amap.com/v3"
MAX_AMAP_WAYPOINTS = 16
VALIDITY_CONFIRM_CACHE: dict[tuple[str, int], dict] = {}


def parse_coord(value: str) -> tuple[float, float] | None:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) < 2:
        return None
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -180 <= lon <= 180 and -90 <= lat <= 90:
        return lon, lat
    return None


def amap_get(path: str, params: dict[str, str]) -> dict:
    url = f"{AMAP}/{path}?{urllib.parse.urlencode(params)}"
    last_data = None
    for attempt in range(5):
        if attempt:
            time.sleep(2**attempt)
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("status") == "1":
            return data
        last_data = data
        if data.get("info") != "CUQPS_HAS_EXCEEDED_THE_LIMIT":
            break
    raise SystemExit(f"Amap API failed: {last_data.get('info') if isinstance(last_data, dict) else last_data or 'unknown'}")


def geocode(place: str, city: str, key: str) -> tuple[float, float]:
    data = amap_get("geocode/geo", {"address": place, "city": city, "key": key})
    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise SystemExit(f"No geocode result for: {place}")
    coord = parse_coord(geocodes[0]["location"])
    if coord is None:
        raise SystemExit(f"Bad geocode location for: {place}")
    return coord


def resolve_point(value: str, city: str, key: str | None) -> tuple[float, float]:
    coord = parse_coord(value)
    if coord:
        return coord
    if not key:
        raise SystemExit(f"AMAP_KEY is required to geocode place name: {value}")
    return geocode(value, city, key)


def load_avoid_points(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("features", data) if isinstance(data, dict) else data
    points = []
    for item in rows:
        props = item.get("properties", item) if isinstance(item, dict) else {}
        coord = None
        if isinstance(item, dict) and item.get("geometry", {}).get("type") == "Point":
            raw = item["geometry"].get("coordinates") or []
            if len(raw) >= 2:
                coord = (float(raw[0]), float(raw[1]))
        elif "longitude" in props and "latitude" in props:
            coord = (float(props["longitude"]), float(props["latitude"]))
        elif "lng" in props and "lat" in props:
            coord = (float(props["lng"]), float(props["lat"]))
        if coord:
            point = {**props, "coord": coord, "name": props.get("name") or props.get("id") or ""}
            points.append(point)
    return points


def fmt(coord: tuple[float, float]) -> str:
    return f"{coord[0]:.6f},{coord[1]:.6f}"


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def project_xy(origin: tuple[float, float], p: tuple[float, float]) -> tuple[float, float]:
    lon0, lat0 = map(math.radians, origin)
    lon, lat = map(math.radians, p)
    return ((lon - lon0) * math.cos(lat0) * 6371000, (lat - lat0) * 6371000)


def distance_point_segment_m(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    px, py = project_xy(point, point)
    ax, ay = project_xy(point, a)
    bx, by = project_xy(point, b)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return haversine_m(point, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    closest = (ax + t * dx, ay + t * dy)
    return math.hypot(px - closest[0], py - closest[1])


def route(key: str, origin: tuple[float, float], dest: tuple[float, float], waypoints: list[tuple[float, float]], strategy: str = "4") -> dict:
    time.sleep(0.25)
    params = {
        "key": key,
        "origin": fmt(origin),
        "destination": fmt(dest),
        "strategy": strategy,
        "extensions": "all",
        "output": "json",
    }
    if waypoints:
        params["waypoints"] = ";".join(fmt(p) for p in waypoints[:MAX_AMAP_WAYPOINTS])
    return amap_get("direction/driving", params)


def polyline(data: dict) -> list[tuple[float, float]]:
    paths = data.get("route", {}).get("paths") or []
    if not paths:
        raise SystemExit("Amap returned no driving path")
    points = []
    for step in paths[0].get("steps", []):
        for raw in (step.get("polyline") or "").split(";"):
            coord = parse_coord(raw)
            if coord:
                points.append(coord)
    return points


def conflicts(line: list[tuple[float, float]], avoid: list[dict], radius_m: float) -> list[dict]:
    if len(line) < 2:
        return []
    origin = line[0]
    cell_m = max(radius_m * 2, 200)
    indexed_segments: dict[tuple[int, int], list[tuple[int, tuple[float, float], tuple[float, float]]]] = {}
    projected_line = [project_xy(origin, p) for p in line]
    for idx, (a, b) in enumerate(zip(projected_line, projected_line[1:])):
        min_x, max_x = sorted((a[0], b[0]))
        min_y, max_y = sorted((a[1], b[1]))
        x0 = math.floor((min_x - radius_m) / cell_m)
        x1 = math.floor((max_x + radius_m) / cell_m)
        y0 = math.floor((min_y - radius_m) / cell_m)
        y1 = math.floor((max_y + radius_m) / cell_m)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                indexed_segments.setdefault((gx, gy), []).append((idx, a, b))
    found = []
    for item in avoid:
        p = item["coord"]
        px, py = project_xy(origin, p)
        gx = math.floor(px / cell_m)
        gy = math.floor(py / cell_m)
        segments = indexed_segments.get((gx, gy), [])
        if not segments:
            continue
        best = None
        best_idx = 0
        for idx, a, b in segments:
            ax, ay = a
            bx, by = b
            dx, dy = bx - ax, by - ay
            if dx == 0 and dy == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
                distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if best is None or distance < best:
                best = distance
                best_idx = idx
        if best is None:
            continue
        if best <= radius_m:
            found.append({**item, "distance_m": round(best, 1), "segment_index": best_idx})
    return sorted(found, key=lambda x: (x["segment_index"], x["distance_m"]))


def path_metrics(data: dict) -> tuple[int, int]:
    path = (data.get("route", {}).get("paths") or [{}])[0]
    return int(path.get("distance") or 0), int(path.get("duration") or 0)


def offset_coord(center: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
    lon, lat = center
    return (
        lon + east_m / (111_320 * max(0.2, math.cos(math.radians(lat)))),
        lat + north_m / 111_320,
    )


def first_conflict_cluster(bad: list[dict], segment_window: int = 4, distance_m: float = 250) -> list[dict]:
    if not bad:
        return []
    first = bad[0]
    first_idx = int(first.get("segment_index", 0))
    first_coord = first["coord"]
    cluster = [
        item
        for item in bad
        if abs(int(item.get("segment_index", 0)) - first_idx) <= segment_window
        or haversine_m(item["coord"], first_coord) <= distance_m
    ]
    return cluster[:12]


def cluster_center(cluster: list[dict]) -> tuple[float, float]:
    return (
        sum(p["coord"][0] for p in cluster) / len(cluster),
        sum(p["coord"][1] for p in cluster) / len(cluster),
    )


def segment_unit_vector(line: list[tuple[float, float]], segment_index: int) -> tuple[float, float]:
    idx = max(0, min(segment_index, len(line) - 2))
    a, b = line[idx], line[idx + 1]
    bx, by = project_xy(a, b)
    length = math.hypot(bx, by) or 1
    return bx / length, by / length


def cumulative_distances(line: list[tuple[float, float]]) -> list[float]:
    distances = [0.0]
    for a, b in zip(line, line[1:]):
        distances.append(distances[-1] + haversine_m(a, b))
    return distances


def point_at_route_distance(
    line: list[tuple[float, float]],
    distances: list[float],
    target_m: float,
) -> tuple[tuple[float, float], int]:
    if target_m <= 0:
        return line[0], 0
    if target_m >= distances[-1]:
        return line[-1], max(0, len(line) - 2)
    for idx, (start_m, end_m) in enumerate(zip(distances, distances[1:])):
        if start_m <= target_m <= end_m:
            ratio = 0.0 if end_m == start_m else (target_m - start_m) / (end_m - start_m)
            a, b = line[idx], line[idx + 1]
            return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio), idx
    return line[-1], max(0, len(line) - 2)


def point_is_clear(point: tuple[float, float], avoid: list[dict], radius_m: float) -> bool:
    return all(haversine_m(point, item["coord"]) > radius_m for item in avoid)


def detour_candidates(
    cluster: list[dict],
    line: list[tuple[float, float]],
    avoid: list[dict],
    radius_m: float,
    base_m: float,
    limit: int = 8,
) -> list[tuple[float, float]]:
    if not cluster or len(line) < 2:
        return []
    center = cluster_center(cluster)
    vx, vy = segment_unit_vector(line, int(cluster[0].get("segment_index", 0)))
    nx, ny = -vy, vx
    vectors = [
        (nx, ny),
        (-nx, -ny),
        (nx + vx * 0.45, ny + vy * 0.45),
        (-nx + vx * 0.45, -ny + vy * 0.45),
        (nx - vx * 0.45, ny - vy * 0.45),
        (-nx - vx * 0.45, -ny - vy * 0.45),
    ]
    distances = [base_m, base_m * 1.5, base_m * 2.25]
    candidates: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for meters in distances:
        for dx, dy in vectors:
            length = math.hypot(dx, dy) or 1
            point = offset_coord(center, dx / length * meters, dy / length * meters)
            key = (round(point[0], 5), round(point[1], 5))
            if key in seen:
                continue
            seen.add(key)
            if point_is_clear(point, avoid, radius_m):
                candidates.append(point)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def corridor_detour_waypoint_sets(
    bad: list[dict],
    line: list[tuple[float, float]],
    avoid: list[dict],
    radius_m: float,
    base_m: float,
    limit: int = 8,
) -> list[tuple[tuple[float, float], ...]]:
    if len(bad) < 3 or len(line) < 2:
        return []
    distances = cumulative_distances(line)
    first_idx = int(bad[0].get("segment_index", 0))
    last_idx = int(bad[-1].get("segment_index", 0))
    if last_idx - first_idx < 20:
        return []
    first_m = distances[max(0, min(first_idx, len(distances) - 1))]
    last_m = distances[max(0, min(last_idx, len(distances) - 1))]
    span_m = last_m - first_m
    if span_m < 1500:
        return []

    anchor_sets = [
        (
            max(0.0, first_m - min(4000.0, span_m * 0.35)),
            first_m + span_m * 0.25,
            last_m,
        ),
        (
            max(0.0, first_m - min(2500.0, span_m * 0.25)),
            first_m + span_m * 0.45,
            last_m,
        ),
    ]
    offset_distances = [base_m * 2.0, base_m * 3.0, base_m * 4.5, base_m * 6.0]
    waypoint_sets: list[tuple[tuple[float, float], ...]] = []
    seen: set[tuple[str, ...]] = set()
    for anchors in anchor_sets:
        for side in (1, -1):
            for offset_m in offset_distances:
                waypoints: list[tuple[float, float]] = []
                for anchor_m in anchors:
                    anchor, segment_idx = point_at_route_distance(line, distances, anchor_m)
                    vx, vy = segment_unit_vector(line, segment_idx)
                    nx, ny = -vy * side, vx * side
                    point = offset_coord(anchor, nx * offset_m, ny * offset_m)
                    if point_is_clear(point, avoid, radius_m):
                        waypoints.append(point)
                if len(waypoints) < 2:
                    continue
                deduped: list[tuple[float, float]] = []
                for point in waypoints:
                    if not deduped or haversine_m(deduped[-1], point) > 500:
                        deduped.append(point)
                key = tuple(fmt(point) for point in deduped)
                if key in seen:
                    continue
                seen.add(key)
                waypoint_sets.append(tuple(deduped))
                if len(waypoint_sets) >= limit:
                    return waypoint_sets
    return waypoint_sets


def state_score(state: dict) -> tuple:
    bad = state["bad"]
    if not bad:
        return (0, state["duration_s"], state["distance_m"], len(state["waypoints"]), 0)
    progress = int(bad[0].get("segment_index", 0))
    return (1, -progress, len(bad), state["duration_s"], state["distance_m"], len(state["waypoints"]))


def evaluate_route_state(
    key: str,
    origin: tuple[float, float],
    dest: tuple[float, float],
    waypoints: tuple[tuple[float, float], ...],
    strategy: str,
    avoid: list[dict],
    radius_m: float,
    avoid_json_path: str,
) -> dict:
    result = route(key, origin, dest, list(waypoints), strategy)
    line = polyline(result)
    distance, duration = path_metrics(result)
    return {
        "result": result,
        "line": line,
        "bad": confirm_conflicts(conflicts(line, avoid, radius_m), avoid_json_path),
        "waypoints": waypoints,
        "distance_m": distance,
        "duration_s": duration,
    }


def search_safe_route(
    key: str,
    origin: tuple[float, float],
    dest: tuple[float, float],
    strategy: str,
    avoid: list[dict],
    radius_m: float,
    detour_distance_m: float,
    max_rounds: int,
    beam_width: int,
    candidate_limit: int,
    avoid_json_path: str,
    max_waypoints: int = MAX_AMAP_WAYPOINTS,
) -> dict:
    seen: set[tuple[str, ...]] = {()}
    initial = evaluate_route_state(key, origin, dest, (), strategy, avoid, radius_m, avoid_json_path)
    if not initial["bad"]:
        return initial
    if max_rounds <= 0:
        return initial
    best = initial
    beam = [initial]
    corridor_states = []
    for waypoints in corridor_detour_waypoint_sets(initial["bad"], initial["line"], avoid, radius_m, detour_distance_m):
        waypoint_key = tuple(fmt(p) for p in waypoints)
        if waypoint_key in seen:
            continue
        seen.add(waypoint_key)
        state = evaluate_route_state(key, origin, dest, waypoints, strategy, avoid, radius_m, avoid_json_path)
        corridor_states.append(state)
        if state_score(state) < state_score(best):
            best = state
    safe_corridor_states = [state for state in corridor_states if not state["bad"]]
    if safe_corridor_states:
        return min(safe_corridor_states, key=state_score)
    if corridor_states:
        beam = sorted([initial, *corridor_states], key=state_score)[: max(1, beam_width)]
    for _ in range(max_rounds):
        expanded = []
        for state in beam:
            if len(state["waypoints"]) >= max_waypoints:
                continue
            cluster = first_conflict_cluster(state["bad"])
            for candidate in detour_candidates(cluster, state["line"], avoid, radius_m, detour_distance_m, candidate_limit):
                next_waypoints = (*state["waypoints"], candidate)
                waypoint_key = tuple(fmt(p) for p in next_waypoints)
                if waypoint_key in seen:
                    continue
                seen.add(waypoint_key)
                expanded.append(evaluate_route_state(key, origin, dest, next_waypoints, strategy, avoid, radius_m, avoid_json_path))
        if not expanded:
            break
        expanded.sort(key=state_score)
        if state_score(expanded[0]) < state_score(best):
            best = expanded[0]
        safe = [state for state in expanded if not state["bad"]]
        if safe:
            return min(safe, key=state_score)
        beam = expanded[: max(1, beam_width)]
    return best


def nearby_points(line: list[tuple[float, float]], points: list[dict], margin: float = 0.02) -> list[dict]:
    min_lon = min(p[0] for p in line) - margin
    max_lon = max(p[0] for p in line) + margin
    min_lat = min(p[1] for p in line) - margin
    max_lat = max(p[1] for p in line) + margin
    return [p for p in points if min_lon <= p["coord"][0] <= max_lon and min_lat <= p["coord"][1] <= max_lat]


def mercator(coord: tuple[float, float]) -> tuple[float, float]:
    lon, lat = coord
    lat = max(-85.0, min(85.0, lat))
    return (
        6378137 * math.radians(lon),
        6378137 * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)),
    )


def write_route_svg(
    path: str,
    line: list[tuple[float, float]],
    waypoints: list[tuple[float, float]],
    avoid: list[dict],
    bad: list[dict],
    radius_m: float = 80,
) -> None:
    if not line:
        return
    width, height, pad = 1600, 1000, 70
    marks = nearby_points(line, avoid)[:800]
    active_marks = [p for p in marks if p.get("isValid") is not False]
    invalid_marks = [p for p in marks if p.get("isValid") is False]
    all_coords = line + waypoints + [p["coord"] for p in marks] + [p["coord"] for p in bad]
    projected = [mercator(p) for p in all_coords]
    min_x, max_x = min(x for x, _ in projected), max(x for x, _ in projected)
    min_y, max_y = min(y for _, y in projected), max(y for _, y in projected)
    scale = min((width - pad * 2) / max(1, max_x - min_x), (height - pad * 2) / max(1, max_y - min_y))
    ox = pad + (width - pad * 2 - (max_x - min_x) * scale) / 2
    oy = pad + (height - pad * 2 - (max_y - min_y) * scale) / 2

    def xy(coord: tuple[float, float]) -> tuple[float, float]:
        x, y = mercator(coord)
        return ox + (x - min_x) * scale, height - oy - (y - min_y) * scale

    def points(coords: list[tuple[float, float]]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(p) for p in coords))

    def dot(coord: tuple[float, float], cls: str, r: float, label: str = "") -> str:
        x, y = xy(coord)
        text = f'<text class="label" x="{x + r + 7:.1f}" y="{y + 4:.1f}">{html_lib.escape(label)}</text>' if label else ""
        return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>{text}'

    conflict_radius_px = max(4, radius_m * scale)
    conflict_rings = []
    for item in bad:
        x, y = xy(item["coord"])
        conflict_rings.append(f'<circle class="radius" cx="{x:.1f}" cy="{y:.1f}" r="{conflict_radius_px:.1f}"/>')

    waypoint_dots = [dot(p, "waypoint", 9, str(i)) for i, p in enumerate(waypoints, 1)]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>
.bg{{fill:#f6f8fb}}.grid{{stroke:#dbe3ee;stroke-width:1}}.halo{{fill:none;stroke:white;stroke-width:12;stroke-linejoin:round;stroke-linecap:round}}.route{{fill:none;stroke:#2563eb;stroke-width:6;stroke-linejoin:round;stroke-linecap:round}}.active{{fill:#6b7280;opacity:.45}}.invalid{{fill:#16a34a;opacity:.35}}.waypoint{{fill:#f59e0b;stroke:white;stroke-width:2}}.conflict{{fill:#dc2626;stroke:white;stroke-width:2}}.radius{{fill:#dc26261a;stroke:#dc262688;stroke-width:1}}.start{{fill:#16a34a;stroke:white;stroke-width:3}}.end{{fill:#111827;stroke:white;stroke-width:3}}.label{{font:700 15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#111827;paint-order:stroke;stroke:white;stroke-width:4}}.title{{font:700 26px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#111827}}.stat{{font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#374151}}.legend{{font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#4b5563}}
</style>
<rect class="bg" width="100%" height="100%"/>
{''.join(f'<line class="grid" x1="{x}" y1="0" x2="{x}" y2="{height}"/>' for x in range(100, width, 100))}
{''.join(f'<line class="grid" x1="0" y1="{y}" x2="{width}" y2="{y}"/>' for y in range(100, height, 100))}
<text class="title" x="40" y="45">Jinjing Verified Route</text>
<text class="stat" x="40" y="74">route points: {len(line)} · waypoints: {len(waypoints)} · conflicts: {len(bad)} · avoid radius: {radius_m:g}m</text>
<polyline class="halo" points="{points(line)}"/>
<polyline class="route" points="{points(line)}"/>
{''.join(dot(p["coord"], "invalid", 3) for p in invalid_marks)}
{''.join(dot(p["coord"], "active", 3) for p in active_marks)}
{''.join(conflict_rings)}
{''.join(dot(p["coord"], "conflict", 9) for p in bad)}
{''.join(waypoint_dots)}
{dot(line[0], "start", 10, "S")}
{dot(line[-1], "end", 10, "E")}
<g transform="translate(40 {height - 95})">
  <circle class="start" cx="0" cy="0" r="7"/><text class="legend" x="14" y="5">start</text>
  <circle class="waypoint" cx="90" cy="0" r="7"/><text class="legend" x="104" y="5">waypoint</text>
  <circle class="active" cx="215" cy="0" r="5"/><text class="legend" x="229" y="5">active avoid</text>
  <circle class="invalid" cx="355" cy="0" r="5"/><text class="legend" x="369" y="5">ignored invalid</text>
  <circle class="conflict" cx="500" cy="0" r="7"/><text class="legend" x="514" y="5">conflict</text>
</g>
</svg>
'''
    Path(path).write_text(svg, encoding="utf-8")


def write_html(
    path: str,
    line: list[tuple[float, float]],
    waypoints: list[tuple[float, float]],
    avoid: list[dict],
    bad: list[dict],
    radius_m: float = 80,
) -> None:
    if not line:
        return
    marks = nearby_points(line, avoid)[:800]
    active_marks = [p for p in marks if p.get("isValid") is not False]
    invalid_marks = [p for p in marks if p.get("isValid") is False]
    payload = {
        "route": [{"lon": lon, "lat": lat} for lon, lat in line],
        "waypoints": [{"lon": lon, "lat": lat, "label": f"{i}"} for i, (lon, lat) in enumerate(waypoints, 1)],
        "activeAvoid": [
            {
                "lon": p["coord"][0],
                "lat": p["coord"][1],
                "name": p.get("name", ""),
                "status": p.get("validityStatus") or "unknown",
            }
            for p in active_marks
        ],
        "invalidAvoid": [
            {
                "lon": p["coord"][0],
                "lat": p["coord"][1],
                "name": p.get("name", ""),
                "status": p.get("validityStatus") or "invalid",
            }
            for p in invalid_marks
        ],
        "conflicts": [
            {
                "lon": p["coord"][0],
                "lat": p["coord"][1],
                "name": p.get("name", ""),
                "distance_m": p.get("distance_m"),
                "status": p.get("validityStatus") or "valid",
            }
            for p in bad
        ],
        "avoidRadiusM": radius_m,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = f"""<!doctype html>
<meta charset="utf-8">
<title>Jinjing Route Preview</title>
<style>
html, body {{ height: 100%; }}
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #111827; }}
.layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 340px; height: 100%; }}
.map {{ min-width: 0; background: #eef2f6; }}
.side {{ border-left: 1px solid #d1d5db; background: #fff; overflow: auto; padding: 16px; }}
h1 {{ font-size: 17px; margin: 0 0 12px; }}
h2 {{ font-size: 13px; margin: 18px 0 8px; color: #374151; }}
.stat {{ display: grid; grid-template-columns: 1fr auto; gap: 6px 12px; font-size: 13px; }}
.stat div:nth-child(even) {{ font-variant-numeric: tabular-nums; color: #111827; }}
.legend {{ display: grid; gap: 7px; font-size: 13px; }}
.key {{ display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 7px; vertical-align: -1px; }}
.route-key {{ border-radius: 3px; background: #2563eb; }}
.wp-key {{ background: #f59e0b; }}
.avoid-key {{ background: #6b7280; }}
.invalid-key {{ background: #22c55e; }}
.conflict-key {{ background: #dc2626; }}
ol {{ padding-left: 21px; margin: 0; font-size: 13px; }}
li {{ margin: 0 0 6px; overflow-wrap: anywhere; }}
.note {{ font-size: 12px; color: #6b7280; line-height: 1.45; }}
svg {{ display: block; width: 100%; height: 100%; }}
.grid {{ stroke: #d7dde6; stroke-width: 1; }}
.route {{ fill: none; stroke: #2563eb; stroke-width: 5; stroke-linejoin: round; stroke-linecap: round; }}
.route-halo {{ fill: none; stroke: #fff; stroke-width: 9; stroke-linejoin: round; stroke-linecap: round; }}
.avoid {{ fill: #6b7280; opacity: .42; }}
.invalid {{ fill: #22c55e; opacity: .38; }}
.conflict {{ fill: #dc2626; stroke: #fff; stroke-width: 2; }}
.radius {{ fill: rgba(220, 38, 38, .08); stroke: rgba(220, 38, 38, .35); stroke-width: 1; }}
.start {{ fill: #16a34a; stroke: #fff; stroke-width: 2; }}
.end {{ fill: #111827; stroke: #fff; stroke-width: 2; }}
.waypoint {{ fill: #f59e0b; stroke: #fff; stroke-width: 2; }}
.label {{ font-size: 12px; font-weight: 700; fill: #111827; paint-order: stroke; stroke: #fff; stroke-width: 4px; }}
@media (max-width: 900px) {{
  .layout {{ grid-template-columns: 1fr; grid-template-rows: 66vh auto; }}
  .side {{ border-left: 0; border-top: 1px solid #d1d5db; }}
}}
</style>
<div class="layout">
  <main class="map">
    <svg id="map" viewBox="0 0 1200 820" role="img" aria-label="Route preview"></svg>
  </main>
  <aside class="side">
    <h1>Jinjing Route Preview</h1>
    <div class="stat">
      <div>Route points</div><div id="routeCount">-</div>
      <div>Waypoints</div><div id="waypointCount">-</div>
      <div>Active avoid points</div><div id="activeCount">-</div>
      <div>Ignored invalid points</div><div id="invalidCount">-</div>
      <div>Conflicts</div><div id="conflictCount">-</div>
    </div>
    <h2>Legend</h2>
    <div class="legend">
      <div><span class="key route-key"></span>Verified route polyline</div>
      <div><span class="key wp-key"></span>Forced waypoint</div>
      <div><span class="key avoid-key"></span>Active/unknown avoid point near route</div>
      <div><span class="key invalid-key"></span>Invalid point ignored by verifier</div>
      <div><span class="key conflict-key"></span>Remaining conflict and {payload["avoidRadiusM"]}m radius</div>
    </div>
    <h2>Waypoints</h2>
    <ol id="waypoints"></ol>
    <h2>Conflicts</h2>
    <ol id="conflicts"></ol>
    <p class="note">Projection uses Web Mercator and a single meter scale, so shape and distances are not stretched independently by longitude/latitude bounds. This preview renders the exact polyline returned by Amap WebService.</p>
  </aside>
</div>
<script>
const DATA = {data_json};
const svg = document.getElementById("map");
const NS = "http://www.w3.org/2000/svg";
const W = 1200, H = 820, PAD = 50, R = 6378137;

function project(p) {{
  const lon = "lon" in p ? p.lon : p[0];
  const lat = "lat" in p ? p.lat : p[1];
  const x = R * lon * Math.PI / 180;
  const y = R * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  return [x, y];
}}

const all = [...DATA.route, ...DATA.waypoints, ...DATA.activeAvoid, ...DATA.invalidAvoid, ...DATA.conflicts];
const projected = all.map(project);
const xs = projected.map(p => p[0]);
const ys = projected.map(p => p[1]);
const minX = Math.min(...xs), maxX = Math.max(...xs);
const minY = Math.min(...ys), maxY = Math.max(...ys);
const scale = Math.min((W - PAD * 2) / Math.max(1, maxX - minX), (H - PAD * 2) / Math.max(1, maxY - minY));
const usedW = (maxX - minX) * scale;
const usedH = (maxY - minY) * scale;
const ox = PAD + (W - PAD * 2 - usedW) / 2;
const oy = PAD + (H - PAD * 2 - usedH) / 2;

function xy(p) {{
  const [x, y] = project(p);
  return [ox + (x - minX) * scale, H - oy - (y - minY) * scale];
}}

function el(name, attrs = {{}}, text = "") {{
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text) node.textContent = text;
  return node;
}}

function title(node, text) {{
  node.appendChild(el("title", {{}}, text));
  return node;
}}

function addGrid() {{
  for (let x = 100; x < W; x += 100) svg.appendChild(el("line", {{ class: "grid", x1: x, y1: 0, x2: x, y2: H }}));
  for (let y = 100; y < H; y += 100) svg.appendChild(el("line", {{ class: "grid", x1: 0, y1: y, x2: W, y2: y }}));
}}

function polyline(points, className) {{
  const pts = points.map(p => xy(p).map(v => v.toFixed(1)).join(",")).join(" ");
  svg.appendChild(el("polyline", {{ class: className, points: pts }}));
}}

function dot(point, className, radius, label = "", tip = "") {{
  const [x, y] = xy(point);
  const g = el("g");
  const c = el("circle", {{ class: className, cx: x.toFixed(1), cy: y.toFixed(1), r: radius }});
  g.appendChild(title(c, tip || `${{point.lon.toFixed(6)}},${{point.lat.toFixed(6)}}`));
  if (label) {{
    g.appendChild(el("text", {{ class: "label", x: (x + radius + 5).toFixed(1), y: (y + 4).toFixed(1) }}, label));
  }}
  svg.appendChild(g);
}}

addGrid();
polyline(DATA.route, "route-halo");
polyline(DATA.route, "route");
DATA.invalidAvoid.forEach(p => dot(p, "invalid", 3, "", `${{p.name}}\\n${{p.lon.toFixed(6)}},${{p.lat.toFixed(6)}}\\n${{p.status}}`));
DATA.activeAvoid.forEach(p => dot(p, "avoid", 3, "", `${{p.name}}\\n${{p.lon.toFixed(6)}},${{p.lat.toFixed(6)}}\\n${{p.status}}`));
DATA.conflicts.forEach(p => {{
  const [x, y] = xy(p);
  svg.appendChild(el("circle", {{ class: "radius", cx: x.toFixed(1), cy: y.toFixed(1), r: Math.max(3, DATA.avoidRadiusM * scale).toFixed(1) }}));
  dot(p, "conflict", 8, "", `${{p.name}}\\n${{p.distance_m}}m`);
}});
DATA.waypoints.forEach(p => dot(p, "waypoint", 8, p.label, `${{p.label}}: ${{p.lon.toFixed(6)}},${{p.lat.toFixed(6)}}`));
dot(DATA.route[0], "start", 9, "S", "start");
dot(DATA.route[DATA.route.length - 1], "end", 9, "E", "end");

document.getElementById("routeCount").textContent = DATA.route.length;
document.getElementById("waypointCount").textContent = DATA.waypoints.length;
document.getElementById("activeCount").textContent = DATA.activeAvoid.length;
document.getElementById("invalidCount").textContent = DATA.invalidAvoid.length;
document.getElementById("conflictCount").textContent = DATA.conflicts.length;
document.getElementById("waypoints").innerHTML = DATA.waypoints.map(p => `<li>${{p.lon.toFixed(6)}},${{p.lat.toFixed(6)}}</li>`).join("") || "<li>None</li>";
document.getElementById("conflicts").innerHTML = DATA.conflicts.map(p => `<li>${{p.name}} · ${{p.distance_m}}m · ${{p.lon.toFixed(6)}},${{p.lat.toFixed(6)}}</li>`).join("") || "<li>None</li>";
</script>
"""
    Path(path).write_text(html, encoding="utf-8")


def share_url(origin: tuple[float, float], dest: tuple[float, float], via: tuple[float, float] | None) -> str:
    params = {
        "from": f"{fmt(origin)},start",
        "to": f"{fmt(dest)},end",
        "mode": "car",
        "policy": "1",
        "src": "jinjing",
        "callnative": "0",
    }
    if via:
        params["via"] = f"{fmt(via)},detour"
    return "https://uri.amap.com/navigation?" + urllib.parse.urlencode(params, safe=",")


def parse_segments(value: str) -> list[tuple[float, float]]:
    points = []
    for raw in value.split(";"):
        coord = parse_coord(raw.strip())
        if coord is None:
            raise SystemExit(f"Bad segment coordinate: {raw}")
        points.append(coord)
    return points


def app_uri(origin: tuple[float, float], dest: tuple[float, float], waypoints: list[tuple[float, float]], platform: str, strategy: str = "4") -> str:
    params = {
        "sourceApplication": "jinjing",
        "sid": "",
        "slat": f"{origin[1]:.6f}",
        "slon": f"{origin[0]:.6f}",
        "sname": "start",
        "did": "",
        "dlat": f"{dest[1]:.6f}",
        "dlon": f"{dest[0]:.6f}",
        "dname": "end",
        "dev": "0",
        "t": "0",
        "m": strategy,
    }
    if waypoints:
        params["vian"] = str(len(waypoints))
        params["vialons"] = "|".join(f"{p[0]:.6f}" for p in waypoints)
        params["vialats"] = "|".join(f"{p[1]:.6f}" for p in waypoints)
        params["vianames"] = "|".join(f"detour{i + 1}" for i in range(len(waypoints)))
    if platform == "android":
        return "amapuri://route/plan/?" + urllib.parse.urlencode(params, safe="|")
    return "iosamap://path?" + urllib.parse.urlencode(params, safe="|")


def app_uri_waypoint_counts(uri: str) -> tuple[int, int, int, int]:
    query = urllib.parse.urlparse(uri).query
    params = urllib.parse.parse_qs(query)
    return (
        int(params.get("vian", ["0"])[0]),
        len(params.get("vialons", [""])[0].split("|")) if params.get("vialons", [""])[0] else 0,
        len(params.get("vialats", [""])[0].split("|")) if params.get("vialats", [""])[0] else 0,
        len(params.get("vianames", [""])[0].split("|")) if params.get("vianames", [""])[0] else 0,
    )


def refresh_default_avoid_json(path: str) -> None:
    if os.path.abspath(path) == os.path.abspath(DEFAULT_AVOID_JSON):
        points = parse_points(fetch(SOURCE_URL))
        target = Path(path)
        write_json(target, merge_cached_validity([p for p in points if keep_inside_sixth_ring(p)], read_points(target)))


def reclassify_cached_validity(item: dict) -> dict | None:
    latest = item.get("latestComment")
    decisive = item.get("latestDecisiveComment")
    if isinstance(latest, dict) and is_recent_invalid_override(latest):
        return {
            **item,
            "isValid": False,
            "validityStatus": "invalid",
            "latestDecisiveComment": latest,
            "validityReason": "cached_recent_invalid_comment",
        }
    if not isinstance(decisive, dict) or not decisive.get("text"):
        return None
    status = classify_comment(decisive["text"])
    if status == "unknown":
        return None
    return {
        **item,
        "isValid": status == "valid",
        "validityStatus": status,
        "validityReason": "cached_reclassified_comment",
    }


def confirm_conflicts(raw: list[dict], avoid_json_path: str) -> list[dict]:
    if os.path.abspath(avoid_json_path) != os.path.abspath(DEFAULT_AVOID_JSON):
        return raw
    abs_path = os.path.abspath(avoid_json_path)
    ids = {int(item["id"]) for item in raw if item.get("id") is not None}
    for item in raw:
        point_id = item.get("id")
        if point_id is None or (abs_path, int(point_id)) in VALIDITY_CONFIRM_CACHE:
            continue
        cached = reclassify_cached_validity(item)
        if cached:
            VALIDITY_CONFIRM_CACHE[(abs_path, int(point_id))] = cached
    missing = {point_id for point_id in ids if (abs_path, point_id) not in VALIDITY_CONFIRM_CACHE}
    updated = update_validity_for_ids(avoid_json_path, missing) if missing else {}
    for point_id, point in updated.items():
        VALIDITY_CONFIRM_CACHE[(abs_path, point_id)] = point
    confirmed = []
    for item in raw:
        point_id = item.get("id")
        if point_id is not None:
            cached = VALIDITY_CONFIRM_CACHE.get((abs_path, int(point_id)))
            if cached:
                item.update({k: v for k, v in cached.items() if k != "coord"})
        if item.get("isValid") is not False:
            confirmed.append(item)
    return confirmed


def plan_segmented(args: argparse.Namespace) -> dict:
    key = args.key or os.environ.get("AMAP_KEY")
    if not key:
        raise SystemExit("Set AMAP_KEY or pass --key to call Amap route planning")
    refresh_default_avoid_json(args.avoid_json)
    origin = resolve_point(args.origin, args.city, key)
    dest = resolve_point(args.destination, args.city, key)
    avoid = load_avoid_points(args.avoid_json)
    waypoints = parse_segments(args.segments)
    chain = [origin, *waypoints, dest]
    merged: list[tuple[float, float]] = []
    route_lines: list[list[tuple[float, float]]] = []
    legs = []
    total_distance = 0
    total_duration = 0
    for idx, (start, end) in enumerate(zip(chain, chain[1:]), 1):
        strategy = args.tail_strategy if idx == len(chain) - 1 else args.strategy
        result = route(key, start, end, [], strategy)
        line = polyline(result)
        route_lines.append(line)
        bad = confirm_conflicts(conflicts(line, avoid, args.avoid_radius_m), args.avoid_json)
        distance, duration = path_metrics(result)
        total_distance += distance
        total_duration += duration
        merged.extend(line if not merged else line[1:])
        legs.append(
            {
                "leg": idx,
                "origin": fmt(start),
                "destination": fmt(end),
                "share_url": share_url(start, end, None),
                "verified": not bad,
                "conflicts": bad[:20],
                "distance_m": distance,
                "duration_s": duration,
                "strategy": strategy,
            }
        )
    merged_bad = confirm_conflicts(conflicts(merged, avoid, args.avoid_radius_m), args.avoid_json)
    full_bad: list[dict] = []
    full_line: list[tuple[float, float]] = []
    full_distance = None
    full_duration = None
    if len(waypoints) <= 16:
        full_result = route(key, origin, dest, waypoints, args.tail_strategy)
        full_line = polyline(full_result)
        full_bad = confirm_conflicts(conflicts(full_line, avoid, args.avoid_radius_m), args.avoid_json)
        full_distance, full_duration = path_metrics(full_result)
    preview_line = full_line if full_line and not full_bad else merged
    preview_bad = full_bad if full_bad else merged_bad
    if args.html:
        write_html(args.html, preview_line, waypoints, avoid, preview_bad, args.avoid_radius_m)
    if args.image:
        write_route_svg(args.image, preview_line, waypoints, avoid, preview_bad, args.avoid_radius_m)
    output = {
        "origin": fmt(origin),
        "destination": fmt(dest),
        "avoid_json": os.path.abspath(args.avoid_json),
        "avoid_radius_m": args.avoid_radius_m,
        "waypoints": [fmt(p) for p in waypoints],
        "ios_app_uri": app_uri(origin, dest, waypoints, "ios", args.tail_strategy) if len(waypoints) <= 16 else None,
        "android_app_uri": app_uri(origin, dest, waypoints, "android", args.tail_strategy) if len(waypoints) <= 16 else None,
        "legs": legs,
        "merged_verified": not merged_bad,
        "merged_conflicts": merged_bad[:20],
        "full_waypoint_route_verified": len(waypoints) <= 16 and not full_bad,
        "full_waypoint_route_conflicts": full_bad[:20],
        "full_waypoint_route_strategy": args.tail_strategy,
        "full_waypoint_route_distance_m": full_distance,
        "full_waypoint_route_duration_s": full_duration,
        "total_distance_m": total_distance,
        "total_duration_s": total_duration,
    }
    if args.image:
        output["image"] = os.path.abspath(args.image)
    if args.html:
        output["html"] = os.path.abspath(args.html)
    return output


def plan(args: argparse.Namespace) -> dict:
    if args.segments:
        return plan_segmented(args)
    key = args.key or os.environ.get("AMAP_KEY")
    if not key:
        raise SystemExit("Set AMAP_KEY or pass --key to call Amap route planning")
    refresh_default_avoid_json(args.avoid_json)
    origin = resolve_point(args.origin, args.city, key)
    dest = resolve_point(args.destination, args.city, key)
    avoid = load_avoid_points(args.avoid_json)
    state = search_safe_route(
        key,
        origin,
        dest,
        args.strategy,
        avoid,
        args.avoid_radius_m,
        args.detour_distance_m,
        args.max_rounds,
        args.beam_width,
        args.candidate_limit,
        args.avoid_json,
    )
    waypoints = list(state["waypoints"])
    bad = state["bad"]
    line = state["line"]
    if args.html:
        write_html(args.html, line, waypoints, avoid, bad, args.avoid_radius_m)
    if args.image:
        write_route_svg(args.image, line, waypoints, avoid, bad, args.avoid_radius_m)
    output = {
        "share_url": share_url(origin, dest, waypoints[0] if waypoints else None),
        "ios_app_uri": app_uri(origin, dest, waypoints, "ios", args.strategy),
        "android_app_uri": app_uri(origin, dest, waypoints, "android", args.strategy),
        "origin": fmt(origin),
        "destination": fmt(dest),
        "avoid_json": os.path.abspath(args.avoid_json),
        "avoid_radius_m": args.avoid_radius_m,
        "waypoints": [fmt(p) for p in waypoints],
        "remaining_conflicts": bad[:20],
        "verified": not bad,
        "distance_m": state["distance_m"],
        "duration_s": state["duration_s"],
        "search": {
            "beam_width": args.beam_width,
            "candidate_limit": args.candidate_limit,
            "max_rounds": args.max_rounds,
        },
    }
    if args.image:
        output["image"] = os.path.abspath(args.image)
    if args.html:
        output["html"] = os.path.abspath(args.html)
    return output


def self_test() -> None:
    assert parse_coord("116.1,39.9") == (116.1, 39.9)
    assert round(haversine_m((116.0, 40.0), (116.0, 40.001))) == 111
    line = [(116.0, 40.0), (116.01, 40.0)]
    distances = cumulative_distances(line)
    midpoint, midpoint_idx = point_at_route_distance(line, distances, distances[-1] / 2)
    assert midpoint_idx == 0
    assert abs(midpoint[0] - 116.005) < 0.0001
    bad = conflicts(line, [{"coord": (116.005, 40.0), "name": "x"}], 5)
    assert bad and bad[0]["name"] == "x"
    ordered = conflicts(
        [(116.0, 40.0), (116.005, 40.0), (116.01, 40.0)],
        [{"coord": (116.009, 40.0), "name": "late"}, {"coord": (116.002, 40.00004), "name": "early"}],
        10,
    )
    assert ordered[0]["name"] == "early"
    clustered = first_conflict_cluster(bad)
    assert clustered and clustered[0]["name"] == "x"
    candidates = detour_candidates(clustered, line, [{"coord": (116.005, 40.0), "name": "x"}], 5, 300)
    assert len(candidates) >= 2
    assert all(haversine_m(p, (116.005, 40.0)) > 5 for p in candidates)
    original_route = globals()["route"]
    try:
        def fake_route(key: str, origin: tuple[float, float], dest: tuple[float, float], waypoints: list[tuple[float, float]], strategy: str = "4") -> dict:
            middle_lat = 40.001 if waypoints else 40.0
            return {
                "route": {
                    "paths": [
                        {
                            "distance": "1000",
                            "duration": "60",
                            "steps": [{"polyline": f"{fmt(origin)};116.005000,{middle_lat:.6f};{fmt(dest)}"}],
                        }
                    ]
                }
            }

        globals()["route"] = fake_route
        state = search_safe_route(
            "key",
            (116.0, 40.0),
            (116.01, 40.0),
            "4",
            [{"coord": (116.005, 40.0), "name": "x"}],
            20,
            300,
            2,
            2,
            4,
            "/tmp/not-default.json",
        )
        assert not state["bad"]
        assert state["waypoints"]
    finally:
        globals()["route"] = original_route
    uri = app_uri((116, 40), (115, 39), [(116.1, 40.1), (116.2, 40.2)], "android")
    assert app_uri_waypoint_counts(uri) == (2, 2, 2, 2)
    cached = reclassify_cached_validity({"latestDecisiveComment": {"publishedAt": "2026/5/21 11:55:56", "text": "经常去三元加油，没有收到罚单"}})
    assert cached and cached["isValid"] is False
    with tempfile.NamedTemporaryFile(suffix=".svg") as tmp:
        write_route_svg(tmp.name, line, [(116.004, 40.001)], [{"coord": (116.005, 40.0), "name": "x"}], [], 80)
        assert "<svg" in Path(tmp.name).read_text(encoding="utf-8")
    assert load_avoid_points(DEFAULT_AVOID_JSON)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin")
    parser.add_argument("--destination")
    parser.add_argument("--city", default="北京")
    parser.add_argument("--avoid-json", default=DEFAULT_AVOID_JSON)
    parser.add_argument("--avoid-radius-m", type=float, default=80)
    parser.add_argument("--detour-distance-m", type=float, default=600)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--beam-width", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=8)
    parser.add_argument("--segments", help="Semicolon-separated intermediate coordinates for segmented verification")
    parser.add_argument("--strategy", default="4", help="Amap driving strategy for protected legs")
    parser.add_argument("--tail-strategy", default="0", help="Amap driving strategy for the final leg and full App URI in segmented mode")
    parser.add_argument("--html", help="Write a local SVG route preview HTML for browser screenshot review")
    parser.add_argument("--image", help="Write a static SVG route image")
    parser.add_argument("--key")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("self-test ok")
        return
    if not args.origin or not args.destination:
        parser.error("--origin and --destination are required")
    print(json.dumps(plan(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
