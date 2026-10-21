#!/usr/bin/env python3
"""
Turn a GPX file into a STRIDE route, and optionally put it on the console.

    python3 tools/gpx2route.py walk.gpx                  print the route JSON
    python3 tools/gpx2route.py walk.gpx --name "River"   name it yourself
    python3 tools/gpx2route.py walk.gpx --adb            add it to the console
    python3 tools/gpx2route.py walk.gpx --adb --restart  ...and reload the app
    python3 tools/gpx2route.py --selftest

A route on the console is not a track. It is a gradient profile — `[startM,
endM, incline%]` against distance travelled — which is what the deck replays
(see Routes.kt). So the GPX is reduced to that: distance along the track,
elevation smoothed (DEM elevation from route planners steps and jitters, and
read raw it turns a flat street into a sawtooth of 4% ramps), then cut into
~100 m segments whose grade is rounded to half a percent. Neighbours with the
same grade are merged, so a flat kilometre is one segment, not ten.

The board clamps whatever it is given to its own range. On a deck that stops
at 0%, downhills are walked flat.

`--adb` merges into `files/routes.json` on the console, replacing any route
with the same id and keeping the rest. The app reads that file at start, so it
needs `--restart` (or a relaunch) to show up. **Do not restart mid-workout** —
the belt is the motor board's, and it does not stop because the app did.

If MQTT is set up, the retained `stride/routes` topic replaces this file the
next time the console connects; publish there instead (`mosquitto_pub -r`).

Uses $ANDROID_SERIAL like adb does, when there is more than one device.
"""
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

STEP_M = 10          # resolution the elevation is resampled to
SMOOTH_M = 60        # moving-average window over that
SEG_M = 100          # target segment length
GRADE_STEP = 0.5     # incline resolution, %
APP = "dev.stride.hud"


def points(gpx_text):
    """(lat, lon, ele) for every track or route point that has an elevation."""
    root = ET.fromstring(gpx_text)
    out = []
    for el in root.iter():
        if el.tag.split('}')[-1] not in ("trkpt", "rtept"):
            continue
        ele = next((c.text for c in el if c.tag.split('}')[-1] == "ele"), None)
        if ele is None:
            continue
        out.append((float(el.get("lat")), float(el.get("lon")), float(ele)))
    return out


def name_of(gpx_text, fallback):
    m = re.search(r"<name>([^<]+)</name>", gpx_text)
    return m.group(1).strip() if m else fallback


def haversine(a, b):
    r = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def profile(pts):
    """Elevation every STEP_M metres along the track, smoothed."""
    dist, ele = [0.0], [pts[0][2]]
    for a, b in zip(pts, pts[1:]):
        d = haversine(a, b)
        if d < 0.5:            # repeated points, which planners emit at turns
            continue
        dist.append(dist[-1] + d)
        ele.append(b[2])
    total = dist[-1]
    if total < SEG_M:
        raise SystemExit("track is shorter than %d m" % SEG_M)

    n = int(total // STEP_M) + 1
    xs = [min(i * STEP_M, total) for i in range(n)]
    if xs[-1] < total:
        xs.append(total)
    ys, j = [], 0
    for x in xs:
        while j < len(dist) - 2 and dist[j + 1] < x:
            j += 1
        span = dist[j + 1] - dist[j]
        t = (x - dist[j]) / span if span else 0.0
        ys.append(ele[j] + t * (ele[j + 1] - ele[j]))

    w = max(1, SMOOTH_M // STEP_M // 2)
    sm = [sum(ys[max(0, i - w):i + w + 1]) / len(ys[max(0, i - w):i + w + 1])
          for i in range(len(ys))]
    return xs, sm


def segments(xs, ys):
    """[startM, endM, incline%] runs of ~SEG_M, same-grade neighbours merged."""
    total = xs[-1]
    count = max(1, round(total / SEG_M))
    edges = [total * i / count for i in range(count + 1)]

    def at(x):
        i = min(int(x // STEP_M), len(xs) - 2)
        t = (x - xs[i]) / ((xs[i + 1] - xs[i]) or 1)
        return ys[i] + t * (ys[i + 1] - ys[i])

    out = []
    for a, b in zip(edges, edges[1:]):
        g = (at(b) - at(a)) / (b - a) * 100
        g = round(g / GRADE_STEP) * GRADE_STEP + 0.0   # +0.0 turns -0.0 into 0.0
        if out and out[-1][2] == g:
            out[-1][1] = round(b)
        else:
            out.append([round(a), round(b), g])
    return out


def route(gpx_text, name):
    xs, ys = profile(points(gpx_text))
    segs = segments(xs, ys)
    climb = sum(max(0.0, s[2]) / 100 * (s[1] - s[0]) for s in segs)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "route"
    return {"id": "gpx-" + slug, "name": name, "distance_m": round(xs[-1]),
            "climb_m": round(climb), "segments": segs}


def adb(*args, data=None):
    r = subprocess.run(["adb", *args], input=data, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def push(new, restart):
    r = subprocess.run(["adb", "shell", "run-as %s cat files/routes.json" % APP],
                       capture_output=True, text=True)
    try:
        have = json.loads(r.stdout) if r.returncode == 0 else []   # no file yet
    except json.JSONDecodeError:
        have = []
    routes = [r for r in have if r.get("id") != new["id"]] + [new]
    adb("shell", "run-as %s sh -c 'cat > files/routes.json'" % APP, data=json.dumps(routes))
    print("console now has %d route(s)" % len(routes), file=sys.stderr)
    if restart:
        adb("shell", "am", "force-stop", APP)
        adb("shell", "am", "start", "-n", APP + "/.MainActivity")
        print("app restarted", file=sys.stderr)
    else:
        print("relaunch the app to see it (or rerun with --restart)", file=sys.stderr)


def selftest():
    # 1 km due north at the equator: 500 m flat, then 500 m rising 15 m (3%).
    pts = []
    for i in range(101):
        d = i * 10
        pts.append('<trkpt lat="%f" lon="0"><ele>%f</ele></trkpt>'
                   % (d / 111195.0, 0 if d <= 500 else (d - 500) * 0.03))
    gpx = '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>%s</trkseg></trk></gpx>' % "".join(pts)
    r = route(gpx, "Test hill")
    assert abs(r["distance_m"] - 1000) <= 2, r["distance_m"]
    assert r["id"] == "gpx-test-hill"
    assert r["segments"][0][:1] == [0] and r["segments"][-1][1] == r["distance_m"]
    # Smoothing rounds off the corners; the flat start and the body of the
    # climb must still come through exactly.
    grade_at = lambda m: next(g for a, b, g in r["segments"] if a <= m < b)
    assert grade_at(100) == 0.0 and grade_at(750) == 3.0, r["segments"]
    assert 13 <= r["climb_m"] <= 17, r["climb_m"]
    print("ok", r["segments"])


def main():
    args = sys.argv[1:]
    if args == ["--selftest"]:
        return selftest()
    if not args or args[0].startswith("-"):
        raise SystemExit(__doc__)
    path = args[0]
    text = open(path, encoding="utf-8").read()
    name = args[args.index("--name") + 1] if "--name" in args else \
        name_of(text, re.sub(r"\.gpx$", "", path.split("/")[-1], flags=re.I))
    r = route(text, name)
    print(json.dumps(r, indent=1))
    print("%s: %.2f km, %d m climb, %d segments, %.1f%% to %.1f%%"
          % (r["name"], r["distance_m"] / 1000, r["climb_m"], len(r["segments"]),
             min(s[2] for s in r["segments"]), max(s[2] for s in r["segments"])),
          file=sys.stderr)
    if "--adb" in args:
        push(r, "--restart" in args)


if __name__ == "__main__":
    main()
