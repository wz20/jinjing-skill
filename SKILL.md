---
name: jinjing
description: Use when planning 高德/Amap driving routes, Amap share links, or navigation links that must avoid traffic-enforcement coordinates from a JSON file, especially /Users/wangze/skills/jinjing/jinjing365_points.json.
---

# Jinjing Route Planner

Use this skill to turn a route request into a verified 高德 route/link that avoids coordinates from a JSON file.

## Required behavior

1. Parse the user's origin, destination, city, and optional avoidance JSON path.
2. If origin or destination is unclear, ask the user before planning.
3. Before loading the default avoidance JSON, refresh it with `scripts/update_jinjing_points.py`. The updater keeps only 六环内 points by removing rows whose `name` contains `六环外` or whose `category` is `6`, and preserves cached validity fields.
4. Load the avoidance JSON. Default to `jinjing365_points.json` in this skill directory.
5. When a route conflicts with a point from the default JSON, confirm that point's current validity from the detail-page comments before treating it as a real conflict. A recent one-month comment explicitly saying it will not shoot (`不会拍`/`不拍了`/`不再拍`) makes the point invalid. Otherwise use the newest decisive comment: clear `被拍`/`拍了`/`会拍` means valid; pure questions do not decide.
6. Use conflict-driven waypoint search by default. Never add avoided coordinates as waypoints; waypoints are forced-through points, not forbidden points.
7. Default to the sixth-ring escape strategy when useful: only solve the protected route from the origin to a verified 六环/六环外 exit point, because 六环路 itself is allowed and does not count as 六环内.
8. After reaching 六环路 or a verified 六环外 exit point, use 高德 fastest route for the tail to the destination.
9. Verify each protected segment polyline, then verify the full waypoint route against the 六环内 avoidance file. If it differs and conflicts, output the verified per-leg links instead.
10. Output both iPhone and Android 高德 App links when the full waypoint route is verified, plus the waypoints used and a short verification result.

## Algorithm contract

Use this loop for automatic planning:

1. Ask 高德 for the best route for the user's selected strategy without waypoints.
2. Verify the returned polyline against the avoidance JSON. Sort conflicts by route order, not by nearest distance alone.
3. If clean, return it. This is the best 高德 route under the selected strategy.
4. If dirty and conflicts span a long route corridor, first try corridor-level detours: 2-3 sparse side-offset waypoints around the whole conflict corridor, then verify the full 高德 waypoint route. This avoids slow one-camera-at-a-time detours on roads like 北清路.
5. If corridor detours do not verify clean, take the first route-order conflict cluster only. Do not try to solve every avoided point at once.
6. Generate candidate detour waypoints around that cluster: left/right perpendicular offsets plus forward/backward blends, using increasing distances from `--detour-distance-m`.
7. Evaluate candidates with a small beam search. Rank verified routes first; otherwise prefer routes whose first remaining conflict is farther along the route, then fewer conflicts, then lower duration/distance.
8. Stop only when the full returned polyline is verified clean, the waypoint limit is reached, or the search budget is exhausted.

Hard limits:

- 高德 Web服务驾车规划 supports up to 16 waypoints. Keep waypoints sparse and conflict-driven.
- A route with remaining strictly-六环内 conflicts is not safe. Say it is not verified instead of presenting it as usable.
- 六环路、六环入口、六环出口、六环匝道、六环交叉口 conflicts are allowed only after confirming they are on/at 六环.
- Cached point validity must be reclassified with the current comment rules before treating it as truth; old cached `isValid: true` values can be wrong after the rules change.

## Tooling

If the user explicitly asks for MCP, first use the configured `amap-maps` MCP where it helps: `maps_geo` for place to coordinate, `maps_regeocode` for coordinate checks, and `maps_distance` for quick distance checks. Route safety still depends on the returned driving polyline; use `scripts/plan_amap_route.py` with 高德 Web服务 route data unless a driving-route MCP tool is exposed in the current session.

Refresh the bundled 六环内 point file:

```bash
python3 scripts/update_jinjing_points.py
```

Use `scripts/plan_amap_route.py` when a Web服务 API key is available:

```bash
AMAP_KEY=... python3 scripts/plan_amap_route.py \
  --origin "START_LON,START_LAT" \
  --destination "DEST_LON,DEST_LAT" \
  --avoid-json /Users/wangze/skills/jinjing/jinjing365_points.json \
  --beam-width 3 \
  --candidate-limit 8
```

Verify a manually segmented route with semicolon-separated intermediate points:

```bash
AMAP_KEY=... python3 scripts/plan_amap_route.py \
  --origin "START_LON,START_LAT" \
  --destination "DEST_LON,DEST_LAT" \
  --segments "MID1_LON,MID1_LAT;MID2_LON,MID2_LAT" \
  --image route-preview.svg
```

Inputs:

- `--origin` and `--destination` accept `lon,lat` coordinates or place names.
- Place names require `AMAP_KEY`; add `--city 北京` or the user's city.
- `--avoid-radius-m` defaults to `80`.
- `--detour-distance-m` defaults to `600`.
- `--max-rounds` defaults to `4`.
- `--beam-width` defaults to `3`.
- `--candidate-limit` defaults to `8`.
- `--strategy` defaults to `4` for protected escape segments.
- `--tail-strategy` defaults to `0` for the final 六环外 tail and the full App URI in segmented mode.
- `--image` writes a static SVG route image with the actual verified polyline, nearby avoided points, waypoints, and conflicts. Prefer this over HTML.
- `--html` exists only for manual browser debugging; do not include it in normal user-facing output.

If no API key is available, do not pretend the route is verified. Create only a 高德 URI from known coordinates and state that collision verification needs `AMAP_KEY` or an MCP route-planning tool.

## Route workflow

Use automatic conflict-driven search first. Use manual `--segments` only when automatic search cannot verify a usable route, or when the user supplies required waypoints.

For routes that start inside 六环 and end outside 六环:

1. Find a nearby safe escape point from the origin. Test short hops first; long hops often force 高德 back onto major roads with avoided points.
2. Continue outward with verified safe segment endpoints until reaching 六环路 or a practical 六环外 exit point.
3. After that point, add only the destination and let 高德 use fastest route; do not keep optimizing against 六环路 or 六环外 point density.
4. For every protected candidate segment, call 高德 driving route and reject it unless its polyline has zero avoided-point conflicts.
5. After building a chain, run `--segments` with all intermediate coordinates. Accept only when:
   - every leg has `verified: true`;
   - `merged_verified` is `true`;
   - `full_waypoint_route_verified` is `true`, or per-leg URLs are output instead of a single full-route link.
6. Do not claim a route is safe when any remaining strictly-六环内 conflict exists. 六环路、六环入口、六环出口、六环匝道、六环交叉口 conflicts are allowed after confirming they are on/at 六环.

## Output format

Keep the final answer concise:

- `高德链接: <url or verified per-leg urls>`
- `iPhone App URI: <iosamap://path?...>`
- `Android App URI: <amapuri://route/plan/?>`
- `避让文件: <path>`
- `验证: 未经过 <radius>m 内避让点 / or list conflicts`
- `途径点: <lon,lat list or 无>`
- `搜索: beam_width/candidate_limit/max_rounds, when automatic search was used`
- `路线图片: <local svg path, when generated>`

## Notes

- 高德 URI `https://uri.amap.com/navigation` supports a shareable route link. Its `via` parameter only supports one displayed via point, so when more than one waypoint is needed, also output iPhone `iosamap://path` and Android `amapuri://route/plan/` App URIs plus the waypoint list.
- 高德 Web服务驾车路径规划 supports up to 16 `waypoints`; use those same waypoints for verification so the shared route matches the checked route as closely as 高德 allows. For inside-to-outside trips, prefer fewer waypoints: enough to reach 六环/exit 六环 safely, then fastest tail.
- Never ignore remaining conflicts. If the script reports conflicts after the retry limit, tell the user the route is not safe to use as an avoidance route.
