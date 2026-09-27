"""REAL boards: convert open-source KiCad .kicad_pcb designs from GitHub into REV Board JSON (SPEC section 1),
then build one engineering-change task per real board (same row format as rev/data/fleet.json).

  python3 -m rev.real_boards download   # search hits (vendor/kicad/search_hits.json) -> licensed raw files in vendor/kicad/
  python3 -m rev.real_boards build      # parse vendor/kicad/*.kicad_pcb -> rev/data/real_boards.json, real_test.jsonl, real_fleet.json

Parsing heuristics (see REAL_DATA.md): outline = largest closed Edge.Cuts loop (must fill >= 85% of its bbox and be the
only board in the file), thickness from (general (thickness)), holes = MountingHole footprints (or all-NPTH footprints
with drill >= 2.2), connectors = footprint names matched to kernel.CONNECTOR_SPECS types whose body (courtyard/fab/pads
bbox) reaches within 6 mm of a board edge. KiCad y grows downward, so board-local y = ymax - y.
"""
import copy
import glob
import json
import math
import os
import random
import re
import sys
from collections import Counter

from rev import kernel
from rev.kernel import CONNECTOR_SPECS, HOUSE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor", "kicad")
DATA_DIR = os.path.join(ROOT, "rev", "data")
MANIFEST = os.path.join(VENDOR, "manifest.json")

EDGE_TOL = 6.0          # connector body must reach within this distance of a board edge
MIN_FILL = 0.85         # outline area / bbox area
MIN_DIM, MAX_DIM = 15.0, 250.0
SYNTH_INSET = 3.5
# repos whose boards are generated/tool fixtures or vendored copies, not a hardware project of their own
EXCLUDE_REPOS = re.compile(r"image-gen|pcb-skill|ai-agent|kicad-source-mirror|freerouting", re.I)
EXCLUDE_PATHS = re.compile(r"-master/|-main/", re.I)
GENERIC_REPO_NAMES = {"pcb", "pcbs", "hardware", "hw", "electrical", "electronics", "hacks", "varios", "various_pcb",
                      "version2", "kicad", "boards", "projects"}


def r2(v):
    return round(float(v) + 0.0, 2)


# ---------------------------------------------------------------- s-expression parsing

_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text):
    stack, cur = [], []
    for m in _TOK.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            if not stack:
                break
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            if t[0] == '"':
                t = t[1:-1].replace('\\"', '"')
            cur.append(t)
    return cur[0] if cur else []


def head(n):
    return n[0] if isinstance(n, list) and n and isinstance(n[0], str) else None


def kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and head(c) == name]


def kid(n, name):
    for c in n[1:]:
        if isinstance(c, list) and head(c) == name:
            return c
    return None


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def xy(n, name):
    k = kid(n, name)
    if not k or len(k) < 3:
        return None
    return (fnum(k[1]), fnum(k[2]))


def layer_of(n):
    k = kid(n, "layer")
    return k[1] if k and len(k) > 1 else None


# ---------------------------------------------------------------- geometry

def arc_points_v5(center, start, angle_deg, steps=12):
    cx, cy = center
    sx, sy = start
    r = math.hypot(sx - cx, sy - cy)
    a0 = math.atan2(sy - cy, sx - cx)
    return [(cx + r * math.cos(a0 + math.radians(angle_deg) * i / steps),
             cy + r * math.sin(a0 + math.radians(angle_deg) * i / steps)) for i in range(steps + 1)]


def arc_points_3(p1, pm, p2, steps=12):
    (x1, y1), (x2, y2), (x3, y3) = p1, pm, p2
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [p1, p2]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    a1 = math.atan2(y1 - uy, x1 - ux)
    am = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    r = math.hypot(x1 - ux, y1 - uy)

    def norm(a):
        return a % (2 * math.pi)
    sweep = norm(a3 - a1)
    if norm(am - a1) > sweep:  # mid not on the ccw path -> go the other way
        sweep = sweep - 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / steps), uy + r * math.sin(a1 + sweep * i / steps))
            for i in range(steps + 1)]


def edge_polylines(pcb):
    """All Edge.Cuts primitives as polylines (lists of points) in KiCad coords."""
    out = []
    for n in pcb[1:]:
        if not isinstance(n, list) or layer_of(n) != "Edge.Cuts":
            continue
        h = head(n)
        if h == "gr_line":
            out.append([xy(n, "start"), xy(n, "end")])
        elif h == "gr_rect":
            (x1, y1), (x2, y2) = xy(n, "start"), xy(n, "end")
            out.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)])
        elif h == "gr_arc":
            if kid(n, "mid"):
                out.append(arc_points_3(xy(n, "start"), xy(n, "mid"), xy(n, "end")))
            else:
                ang = kid(n, "angle")
                out.append(arc_points_v5(xy(n, "start"), xy(n, "end"), fnum(ang[1]) if ang else 90.0))
        elif h == "gr_circle":
            c, e = xy(n, "center"), xy(n, "end")
            out.append(arc_points_v5(c, e, 360.0, steps=24))
        elif h in ("gr_poly",):
            pts = kid(n, "pts")
            if pts:
                p = [(fnum(k[1]), fnum(k[2])) for k in kids(pts, "xy")]
                if p:
                    out.append(p + [p[0]])
        elif h == "gr_curve":
            pts = kid(n, "pts")
            if pts:
                p = [(fnum(k[1]), fnum(k[2])) for k in kids(pts, "xy")]
                if len(p) >= 2:
                    out.append([p[0], p[-1]])
    return [p for p in out if p and all(q is not None for q in p)]


def chain_loops(polys, tol=0.05):
    """Greedy-join polylines into closed loops. Returns list of point lists (closed)."""
    segs = [list(p) for p in polys if len(p) >= 2]
    loops = []

    def close(a, b):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
    while segs:
        cur = segs.pop(0)
        grew = True
        while grew and not close(cur[0], cur[-1]):
            grew = False
            for i, s in enumerate(segs):
                if close(cur[-1], s[0]):
                    cur += s[1:]
                elif close(cur[-1], s[-1]):
                    cur += s[::-1][1:]
                elif close(cur[0], s[-1]):
                    cur = s[:-1] + cur
                elif close(cur[0], s[0]):
                    cur = s[::-1][:-1] + cur
                else:
                    continue
                segs.pop(i)
                grew = True
                break
        if len(cur) >= 4 and close(cur[0], cur[-1]):
            loops.append(cur)
    return loops


def poly_area(p):
    return abs(sum(p[i][0] * p[i + 1][1] - p[i + 1][0] * p[i][1] for i in range(len(p) - 1))) / 2


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------- footprints

def fp_name(fp):
    return fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""


def fp_ref(fp):
    for t in kids(fp, "fp_text"):
        if len(t) > 2 and t[1] == "reference":
            return t[2]
    for p in kids(fp, "property"):
        if len(p) > 2 and p[1] == "Reference":
            return p[2]
    return ""


def fp_value(fp):
    for t in kids(fp, "fp_text"):
        if len(t) > 2 and t[1] == "value":
            return t[2]
    for p in kids(fp, "property"):
        if len(p) > 2 and p[1] == "Value":
            return p[2]
    return ""


def fp_at(fp):
    a = kid(fp, "at")
    if not a:
        return 0.0, 0.0, 0.0
    return fnum(a[1]), fnum(a[2]), fnum(a[3]) if len(a) > 3 else 0.0


def to_board(fp, lx, ly):
    """Footprint-local -> KiCad board coords (y down; positive angle = CCW on screen)."""
    x, y, rot = fp_at(fp)
    t = math.radians(rot)
    return (x + lx * math.cos(t) + ly * math.sin(t), y - lx * math.sin(t) + ly * math.cos(t))


def fp_body_points(fp):
    """Points of the footprint body: courtyard, else fab, else pads (board coords)."""
    for layers in (("F.CrtYd", "B.CrtYd"), ("F.Fab", "B.Fab")):
        pts = []
        for g in fp[1:]:
            if not isinstance(g, list) or head(g) not in ("fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle"):
                continue
            if layer_of(g) not in layers:
                continue
            if head(g) == "fp_poly":
                pk = kid(g, "pts")
                loc = [(fnum(k[1]), fnum(k[2])) for k in kids(pk, "xy")] if pk else []
            elif head(g) == "fp_circle":
                c, e = xy(g, "center"), xy(g, "end")
                if not c or not e:
                    continue
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                loc = [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
            else:
                loc = [q for q in (xy(g, "start"), xy(g, "end"), xy(g, "mid")) if q]
            if head(g) == "fp_rect" and len(loc) >= 2:
                (x1, y1), (x2, y2) = loc[0], loc[1]
                loc = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            pts += [to_board(fp, *q) for q in loc]
        if len(pts) >= 2:
            return pts
    pts = []
    for p in kids(fp, "pad"):
        a = kid(p, "at")
        s = kid(p, "size")
        if not a:
            continue
        cx, cy = fnum(a[1]), fnum(a[2])
        sw, sh = (fnum(s[1]), fnum(s[2])) if s and len(s) > 2 else (0.0, 0.0)
        for dx, dy in ((-sw / 2, -sh / 2), (sw / 2, sh / 2)):
            pts.append(to_board(fp, cx + dx, cy + dy))
    return pts


def pad_drills(fp):
    out = []
    for p in kids(fp, "pad"):
        d = kid(p, "drill")
        if not d or len(d) < 2:
            continue
        vals = [fnum(v, None) for v in d[1:] if isinstance(v, str)]
        vals = [v for v in vals if v is not None]
        if vals:
            out.append((p[2] if len(p) > 2 else "", min(vals)))
    return out


def classify_connector(name):
    """name = footprint name WITHOUT the library prefix (libs like 'USB-C-foo:' would mislabel every part)."""
    s = base = name.lower()
    if "vertical" in base or "_vert" in base:
        return None
    if re.search(r"usb[_\- ]?c\b|usb[_\- ]?c[_\-]|type[_\- ]?c|usb_c_", s) and "micro" not in base:
        return "usb_c"
    if "hdmi" in base and not any(k in base for k in ("micro", "mini")):
        return "hdmi"
    if "rj45" in base or "8p8c" in base or "magjack" in base:
        return "rj45"
    if "barrel" in base or "dc_jack" in base or "dcjack" in base or "dc-jack" in base:
        return "barrel"
    if re.search(r"micro[_\-]?sd|sd[_\-]?card|tf[_\-]?card|microsd", base):
        return "sd"
    if re.search(r"usb[_\-]?a\b|usb[_\-]?a[_\-]|usb_a_", base) and "usb_a" in CONNECTOR_SPECS:
        return "usb_a"
    return None


def height_hint(name):
    """Heuristic tallest-part estimate (mm above board top) from footprint library names."""
    s = name.lower()
    if "relay" in s:
        return 15.5
    if "cp_radial" in s or "c_radial" in s:
        m = re.search(r"d(\d+(?:\.\d+)?)mm", s)
        d = float(m.group(1)) if m else 6.3
        return min(20.0, 1.3 * d + 2.0)
    if "terminalblock" in s or "screw_terminal" in s:
        return 10.0
    if "pinsocket" in s or "pin_socket" in s:
        return 8.5
    if "pinheader" in s or "pin_header" in s:
        return 8.5 if "vertical" in s else 3.0
    if "crystal" in s and "hc49" in s:
        return 4.0
    if "inductor" in s or s.startswith("l_"):
        return 5.0
    if "buzzer" in s or "speaker" in s:
        return 9.5
    if "battery" in s or "batteryholder" in s:
        return 8.0
    if "module" in s or "esp32" in s or "wroom" in s:
        return 3.5
    return 0.0


# ---------------------------------------------------------------- board conversion

class Reject(Exception):
    pass


def convert(text, name="board"):
    pcb = parse_sexpr(text)
    if head(pcb) != "kicad_pcb":
        raise Reject("not a kicad_pcb")
    ver = kid(pcb, "version")
    version = ver[1] if ver else "?"
    polys = edge_polylines(pcb)
    if not polys:
        raise Reject("no Edge.Cuts outline")
    loops = chain_loops(polys)
    if not loops:
        raise Reject("Edge.Cuts outline not closed")
    loops.sort(key=poly_area, reverse=True)
    outer = loops[0]
    x0, y0, x1, y1 = bbox([q for p in polys for q in p])
    w, d = x1 - x0, y1 - y0
    if not (MIN_DIM <= w <= MAX_DIM and MIN_DIM <= d <= MAX_DIM):
        raise Reject(f"size {w:.1f}x{d:.1f} out of range")
    fill = poly_area(outer) / (w * d)
    if fill < MIN_FILL:
        raise Reject(f"non-rectangular outline (fills {fill:.2f} of bbox)")
    # any second loop that is not inside the outer loop's bbox => panel / multiple boards
    ox0, oy0, ox1, oy1 = bbox(outer)
    for lp in loops[1:]:
        a, b, c, e = bbox(lp)
        if a < ox0 - 0.5 or b < oy0 - 0.5 or c > ox1 + 0.5 or e > oy1 + 0.5:
            raise Reject("multiple boards / panel")
    gen = kid(pcb, "general")
    th = kid(gen, "thickness") if gen else None
    t = fnum(th[1], 1.6) if th else 1.6
    if not (0.4 <= t <= 3.2):
        t = 1.6

    def local(p):
        return (p[0] - x0, y1 - p[1])

    fps = [n for n in pcb[1:] if isinstance(n, list) and head(n) in ("module", "footprint")]
    holes, conns, heights, skipped = [], [], [], Counter()
    refs_seen = set()
    for fp in fps:
        nm = fp_name(fp)
        lname = nm.lower()
        lay = layer_of(fp) or "F.Cu"
        drills = pad_drills(fp)
        pads = kids(fp, "pad")
        is_mh = "mountinghole" in lname.replace("_", "").replace(" ", "") or "mounting_hole" in lname
        all_npth = pads and all(len(p) > 2 and p[2] == "np_thru_hole" for p in pads)
        if is_mh or (all_npth and drills and max(dd for _, dd in drills) >= 2.2):
            m = re.search(r"(\d+(?:\.\d+)?)\s*mm", nm)
            dia = float(m.group(1)) if m else (max(dd for _, dd in drills) if drills else 3.2)
            if not (1.5 <= dia <= 6.5):
                dia = max(dd for _, dd in drills) if drills else 3.2
            if not (1.5 <= dia <= 6.5):
                continue
            x, y, _ = fp_at(fp)
            lx, ly = local((x, y))
            if not (0 <= lx <= w and 0 <= ly <= d):
                continue
            if any(math.hypot(h["x"] - lx, h["y"] - ly) < 0.5 for h in holes):
                continue
            holes.append({"x": r2(lx), "y": r2(ly), "dia": r2(round(dia, 1))})
            continue
        heights.append(height_hint(nm))
        ctype = classify_connector(nm.split(":")[-1])
        if not ctype:
            continue
        if ctype not in CONNECTOR_SPECS:
            skipped["unsupported_type"] += 1
            continue
        if not lay.startswith("F."):
            skipped["bottom_side"] += 1
            continue
        pts = [local(p) for p in fp_body_points(fp)]
        if not pts:
            continue
        bx0, by0, bx1, by1 = bbox(pts)
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        if not (-EDGE_TOL <= cx <= w + EDGE_TOL and -EDGE_TOL <= cy <= d + EDGE_TOL):
            skipped["outside_board"] += 1
            continue
        dist = {"front": by0, "back": d - by1, "left": bx0, "right": w - bx1}
        side = min(dist, key=dist.get)
        if dist[side] > EDGE_TOL:
            skipped["not_at_edge"] += 1
            continue
        spec = CONNECTOR_SPECS[ctype]
        L = w if side in ("front", "back") else d
        pos = cx if side in ("front", "back") else cy
        pos = min(max(pos, spec["w"] / 2), L - spec["w"] / 2)
        ref = fp_ref(fp)
        cid = ref if re.fullmatch(r"[A-Za-z]{1,6}\d{1,4}", ref or "") and ref not in refs_seen else None
        if cid:
            refs_seen.add(cid)
        conns.append({"id": cid, "type": ctype, "side": side, "pos": r2(pos),
                      "z": spec["z"], "w": spec["w"], "h": spec["h"], "_fp": nm})
        heights.append(spec["z"] + spec["h"] / 2)
    if not conns:
        raise Reject("no supported edge connector")
    # connectors overlapping each other on the same side (e.g. stacked dual USB) -> keep first
    kept = []
    for c in conns:
        if any(o["side"] == c["side"] and abs(o["pos"] - c["pos"]) < (o["w"] + c["w"]) / 2 for o in kept):
            skipped["overlapping"] += 1
            continue
        kept.append(c)
    conns = kept
    n = 0
    for c in conns:
        if not c["id"]:
            while True:
                n += 1
                if f"J{n}" not in refs_seen:
                    break
            c["id"] = f"J{n}"
            refs_seen.add(c["id"])
    synthesized = False
    if len(holes) < 2:
        synthesized = True
        dia = holes[0]["dia"] if holes else 3.2
        holes = [{"x": r2(x), "y": r2(y), "dia": dia} for x, y in
                 ((SYNTH_INSET, SYNTH_INSET), (w - SYNTH_INSET, SYNTH_INSET),
                  (w - SYNTH_INSET, d - SYNTH_INSET), (SYNTH_INSET, d - SYNTH_INSET))]
    holes.sort(key=lambda h: (round(h["y"] / 5), h["x"]))
    for i, h in enumerate(holes):
        h["id"] = f"H{i + 1}"
    holes = [{"id": h["id"], "x": h["x"], "y": h["y"], "dia": h["dia"]} for h in holes]
    mh = max([6.0] + heights)
    mh = math.ceil(mh * 2) / 2
    board = {"rev": "A", "name": name, "w": r2(w), "d": r2(d), "t": r2(t), "max_component_h": r2(mh),
             "holes": holes,
             "connectors": [{k: c[k] for k in ("id", "type", "side", "pos", "z", "w", "h")} for c in conns]}
    meta = {"kicad_version": version, "outline_fill": round(fill, 3), "holes_synthesized": synthesized,
            "n_footprints": len(fps), "connector_footprints": [c["_fp"] for c in conns],
            "skipped_connectors": dict(skipped)}
    return board, meta


# ---------------------------------------------------------------- download

def _slug(repo, path):
    return (repo.replace("/", "__") + "__" + path.replace("/", "__"))[:180]


def download(max_per_repo=2, workers=12):
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    import requests
    hits = json.load(open(os.path.join(VENDOR, "search_hits.json")))
    bad = re.compile(r"(^|/)(qa|test|tests|fixtures|benchmark|demo|demos|examples?|backup|old|archive)(/|$)|-bak|_bak|autosave",
                     re.I)
    skip_repos = {"KiCad/kicad-source-mirror", "freerouting/freerouting"}
    per = Counter()
    picks = []
    for h in hits:
        if h["fork"] or h["repo"] in skip_repos or bad.search(h["path"]):
            continue
        if per[h["repo"]] >= max_per_repo:
            continue
        per[h["repo"]] += 1
        picks.append(h)
    repos = sorted({h["repo"] for h in picks})

    def lic(repo):
        r = subprocess.run(["gh", "api", f"repos/{repo}", "--jq",
                            "[.license.spdx_id // \"\", .license.name // \"\", .private, .default_branch] | @json"],
                           capture_output=True, text=True)
        try:
            spdx, lname, private, branch = json.loads(r.stdout.strip())
            return repo, {"spdx": spdx, "name": lname, "private": private}
        except Exception:
            return repo, {"spdx": "", "name": "", "private": None}
    with ThreadPoolExecutor(workers) as ex:
        lics = dict(ex.map(lic, repos))
    ok = [h for h in picks if lics[h["repo"]]["spdx"] and lics[h["repo"]]["private"] is False]

    def get(h):
        raw = h["html_url"].replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/", 1)
        fn = os.path.join(VENDOR, _slug(h["repo"], h["path"]))
        if not os.path.exists(fn):
            try:
                r = requests.get(raw, timeout=30)
                if r.status_code != 200 or len(r.content) > 8_000_000:
                    return None
                open(fn, "wb").write(r.content)
            except Exception:
                return None
        return {"file": os.path.basename(fn), "repo": h["repo"], "path": h["path"], "url": h["html_url"],
                "raw_url": raw, "license": lics[h["repo"]]["spdx"], "license_name": lics[h["repo"]]["name"],
                "query": h["queries"]}
    with ThreadPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(get, ok) if r]
    man = {"search_hits": len(hits), "candidates_after_filters": len(picks), "repos": len(repos),
           "licensed": len(ok), "downloaded": len(rows),
           "license_counts": dict(Counter(lics[h["repo"]]["spdx"] or "NONE" for h in picks)), "files": rows}
    json.dump(man, open(MANIFEST, "w"), indent=1)
    print(json.dumps({k: v for k, v in man.items() if k != "files"}))
    return man


# ---------------------------------------------------------------- tasks on real boards

TASK_KINDS = ["resize", "move_connector", "add_connector", "taller_component", "move_hole", "remove_connector"]
CORNER = 3.0   # min distance between a connector body and a board corner for moves/adds
GAP = 2.0


def _conn_fits(b, c, ignore=None):
    L = b["w"] if c["side"] in ("front", "back") else b["d"]
    lo, hi = c["pos"] - c["w"] / 2, c["pos"] + c["w"] / 2
    if lo < CORNER - 1e-9 or hi > L - CORNER + 1e-9:
        return False
    for o in b["connectors"]:
        if o["id"] in (c["id"], ignore) or o["side"] != c["side"]:
            continue
        if lo < o["pos"] + o["w"] / 2 + GAP and hi > o["pos"] - o["w"] / 2 - GAP:
            return False
    return True


def _hole_clear(b, h):
    if not (2.5 <= h["x"] <= b["w"] - 2.5 and 2.5 <= h["y"] <= b["d"] - 2.5):
        return False
    return all(math.hypot(o["x"] - h["x"], o["y"] - h["y"]) >= 6.0 for o in b["holes"] if o["id"] != h["id"])


def _g05(v):
    return r2(round(float(v) * 2) / 2)


def real_change(rng, b, kind):
    """Mutate board_b with one engineering change. Returns (summary, kind) or None."""
    from rev import tasks
    if kind == "resize":
        ax = rng.choice(["w", "d"])
        delta = _g05(rng.uniform(3, 12))
        old = b[ax]
        edge = old
        b[ax] = r2(old + delta)
        key = "x" if ax == "w" else "y"
        side = "right" if ax == "w" else "back"
        moved = []
        for h in b["holes"]:
            if h[key] > edge - 8.0:           # holes near the moved edge travel with it
                h[key] = r2(h[key] + delta)
                moved.append(h["id"])
        for c in b["connectors"]:
            if c["side"] in (("front", "back") if ax == "w" else ("left", "right")) and c["pos"] > edge - 12.0:
                c["pos"] = r2(c["pos"] + delta)  # connectors hugging the moved corner move with it
        word = "widened" if ax == "w" else "deepened"
        s = f"Board {word} {delta:.1f} mm at the {side} edge"
        if moved:
            s += f" ({', '.join(moved)} moved with the edge)"
        return s
    if kind == "move_connector":
        cands = list(b["connectors"])
        rng.shuffle(cands)
        for c in cands:
            for _ in range(30):
                delta = _g05(rng.uniform(3, 15)) * rng.choice([1, -1])
                nc = dict(c, pos=r2(c["pos"] + delta))
                if _conn_fits(b, nc):
                    c["pos"] = nc["pos"]
                    dirn = ("right" if delta > 0 else "left") if c["side"] in ("front", "back") else \
                        ("back" if delta > 0 else "front")
                    return f"{c['id']} ({c['type']}) moved {abs(delta):.1f} mm toward {dirn}"
        return None
    if kind == "add_connector":
        present = {c["type"] for c in b["connectors"]}
        pool = [t for t in tasks.TYPES if t not in present] or list(tasks.TYPES)
        for _ in range(60):
            t = rng.choice(pool)
            side = rng.choice(kernel.SIDES)
            L = b["w"] if side in ("front", "back") else b["d"]
            wv = CONNECTOR_SPECS[t]["w"]
            lo, hi = CORNER + wv / 2, L - CORNER - wv / 2
            if hi < lo:
                continue
            cid = tasks.next_id(b["connectors"], "J")
            while any(c["id"] == cid for c in b["connectors"]):
                cid = "J" + str(int(cid[1:]) + 1)
            c = tasks.make_connector(cid, t, side, _g05(rng.uniform(lo, hi)))
            if _conn_fits(b, c):
                b["connectors"].append(c)
                if t in ("rj45", "barrel"):
                    b["max_component_h"] = max(b["max_component_h"], math.ceil((c["z"] + c["h"] / 2) * 2) / 2)
                return f"added {cid} ({t}) on {side} edge"
        return None
    if kind == "taller_component":
        old = b["max_component_h"]
        b["max_component_h"] = r2(old + _g05(rng.uniform(1.5, 8)))
        return f"tallest component now {b['max_component_h']:.1f} mm (was {old:.1f})"
    if kind == "move_hole":
        cands = list(b["holes"])
        rng.shuffle(cands)
        for h in cands:
            for _ in range(30):
                key = rng.choice(["x", "y"])
                delta = _g05(rng.uniform(1, 6)) * rng.choice([1, -1])
                nh = dict(h, **{key: r2(h[key] + delta)})
                if _hole_clear(b, nh):
                    h[key] = nh[key]
                    dirn = ("right" if delta > 0 else "left") if key == "x" else ("back" if delta > 0 else "front")
                    return f"mounting hole {h['id']} moved {abs(delta):.1f} mm toward {dirn}"
        return None
    if kind == "remove_connector":
        if len(b["connectors"]) < 2:
            return None
        c = rng.choice(b["connectors"])
        b["connectors"].remove(c)
        return f"removed {c['id']} ({c['type']})"
    return None


def make_real_task(rng, tid, board_a, k):
    from rev import tasks
    order = TASK_KINDS[k % len(TASK_KINDS):] + TASK_KINDS[:k % len(TASK_KINDS)]
    for kind in order:
        for _ in range(5):
            b = copy.deepcopy(board_a)
            b["rev"] = "B"
            s = real_change(rng, b, kind)
            if not s:
                continue
            enc_a = kernel.enclosure_for(board_a)
            gold = tasks.gold_calls(board_a, enc_a, b)
            enc_b, errs = kernel.apply_calls(b, enc_a, gold)
            if errs or not kernel.passes(b, enc_b) or kernel.passes(b, enc_a):
                continue
            new_type = kind == "add_connector" and b["connectors"][-1]["type"] not in {c["type"] for c in board_a["connectors"]}
            return {"id": tid, "board_a": board_a, "enclosure_a": enc_a, "board_b": b,
                    "change_summary": s[0].upper() + s[1:], "gold_calls": gold, "kinds": [kind],
                    "new_type_added": new_type}
    return None


def _short_name(repo, path):
    stem = os.path.splitext(os.path.basename(path))[0]
    rname = repo.split("/")[1]
    nm = rname if stem.lower() in (rname.lower(), "main", "board", "pcb") or stem.lower() in rname.lower() else f"{rname}/{stem}"
    return nm[:40]


def build(seed=7, n_fleet=48):
    man = json.load(open(MANIFEST))
    boards, rejects = [], Counter()
    seen_geom = set()
    for f in man["files"]:
        if EXCLUDE_REPOS.search(f["repo"]) or EXCLUDE_PATHS.search(f["path"]):
            rejects["generated/tool fixture or vendored copy"] += 1
            continue
        if f["license"] in ("", "NOASSERTION", "NONE"):
            rejects["license not recognized by GitHub (NOASSERTION)"] += 1
            continue
        fn = os.path.join(VENDOR, f["file"])
        try:
            text = open(fn, encoding="utf-8", errors="replace").read()
            name = _short_name(f["repo"], f["path"])
            board, meta = convert(text, name)
        except Reject as e:
            rejects[re.sub(r"\d+(\.\d+)?", "N", str(e))] += 1
            continue
        except Exception as e:  # malformed files
            rejects[f"parse error ({type(e).__name__})"] += 1
            continue
        g = json.dumps([board["w"], board["d"], board["holes"], board["connectors"]])
        if g in seen_geom:
            rejects["duplicate geometry"] += 1
            continue
        seen_geom.add(g)
        # the board itself must be enclosure-able by the kernel
        if not kernel.passes(board, kernel.enclosure_for(board)):
            rejects["kernel self-check failed"] += 1
            continue
        boards.append({"source": {"repo": f["repo"], "path": f["path"], "url": f["url"], "license": f["license"],
                                  "license_name": f["license_name"]},
                       "meta": meta, "board": board})
    per_repo = Counter(rb["source"]["repo"] for rb in boards)
    names = Counter()
    for rb in boards:
        repo, path = rb["source"]["repo"], rb["source"]["path"]
        nm = repo.split("/")[1]
        stem = os.path.splitext(os.path.basename(path))[0]
        if nm.lower() in GENERIC_REPO_NAMES:
            nm = stem
        elif per_repo[repo] > 1:
            nm += " / " + stem
        nm = nm[:40].strip()
        names[nm] += 1
        if names[nm] > 1:
            nm = f"{nm[:36]} #{names[nm]}"
        rb["board"]["name"] = nm
    rng = random.Random(seed)
    rows = []
    for i, rb in enumerate(boards):
        t = make_real_task(rng, f"real-{i:03d}", rb["board"], i)
        if not t:
            rb["task"] = None
            continue
        t["name"] = rb["board"]["name"]
        t["code"] = f"RB-{i + 1:02d}"
        t["family"] = "Open-source KiCad"
        t["usb_a"] = any(c["type"] == "usb_a" for c in t["board_b"]["connectors"])
        t["source"] = rb["source"]
        rb["task"] = t["id"]
        rows.append(t)
    # verify
    for t in rows:
        enc, errs = kernel.apply_calls(t["board_b"], t["enclosure_a"], t["gold_calls"])
        assert not errs and kernel.passes(t["board_b"], enc), t["id"]
        assert not kernel.passes(t["board_b"], t["enclosure_a"]), t["id"]
    stats = {"files_downloaded": len(man["files"]), "parsed_kept": len(boards), "rejects": dict(rejects),
             "tasks": len(rows), "kinds": dict(Counter(t["kinds"][0] for t in rows)),
             "connector_types": dict(Counter(c["type"] for b in boards for c in b["board"]["connectors"])),
             "holes_synthesized": sum(b["meta"]["holes_synthesized"] for b in boards),
             "licenses": dict(Counter(b["source"]["license"] for b in boards))}
    json.dump({"stats": stats, "boards": boards}, open(os.path.join(DATA_DIR, "real_boards.json"), "w"), indent=1)
    with open(os.path.join(DATA_DIR, "real_test.jsonl"), "w") as fh:
        for t in rows:
            fh.write(json.dumps(t, separators=(",", ":")) + "\n")
    # fleet: moderate-size boards with real (not synthesized) holes, one per repo, round-robin over connector types
    by_id = {rb["task"]: rb for rb in boards if rb.get("task")}

    def fleet_ok(t):
        B, m = t["board_a"], by_id[t["id"]]["meta"]
        return (not m["holes_synthesized"] and 20 <= min(B["w"], B["d"]) and max(B["w"], B["d"]) <= 160
                and len(B["connectors"]) <= 6)
    pool = [t for t in rows if fleet_ok(t)]
    buckets = {}
    for t in pool:
        types = sorted({c["type"] for c in t["board_a"]["connectors"]}, key=lambda x: stats["connector_types"].get(x, 0))
        buckets.setdefault(types[0], []).append(t)
    fleet, used = [], set()
    order = sorted(buckets, key=lambda x: len(buckets[x]))
    while len(fleet) < n_fleet and any(buckets.values()):
        for ty in order:
            while buckets[ty]:
                t = buckets[ty].pop(0)
                if t["source"]["repo"] in used:
                    continue
                fleet.append(t)
                used.add(t["source"]["repo"])
                break
            if len(fleet) >= n_fleet:
                break
    for t in rows:  # fill (should rarely be needed)
        if len(fleet) >= n_fleet:
            break
        if t not in fleet:
            fleet.append(t)
    fleet = fleet[:n_fleet]
    fleet_rows = []
    for j, t in enumerate(fleet):
        t = copy.deepcopy(t)
        t["id"] = f"real-fleet-{j:02d}"
        t["code"] = f"RB-{j + 1:02d}"
        fleet_rows.append(t)
    json.dump(fleet_rows, open(os.path.join(DATA_DIR, "real_fleet.json"), "w"), indent=1)
    write_doc(man, stats, boards, fleet_rows)
    print(json.dumps(stats))
    print(f"real_test.jsonl: {len(rows)}  real_fleet.json: {len(fleet_rows)}")
    return stats


def write_doc(man, stats, boards, fleet_rows):
    in_fleet = {t["source"]["repo"] + "/" + t["source"]["path"]: t["code"] for t in fleet_rows}
    rj = stats["rejects"]
    lines = []
    A = lines.append
    A("# REAL_DATA — open-source KiCad boards as REV test data")
    A("")
    A("_Generated by `python3 -m rev.real_boards build` (edit the prose in `rev/real_boards.py:write_doc`)._")
    A("")
    A("## What this is")
    A("")
    A(f"**{stats['parsed_kept']} real, openly-licensed KiCad PCB designs from {len({b['source']['repo'] for b in boards})} "
      "public GitHub repos**, converted into REV Board JSON (SPEC section 1). Each board gets one engineering change "
      "(Rev A -> Rev B) with gold tool calls, verified by `rev/kernel.py` (gold passes 100%, stale enclosure fails 100%).")
    A("")
    A("| file | contents |")
    A("|---|---|")
    A(f"| `rev/data/real_boards.json` | `stats` + every kept board: `board` (REV JSON), `source` (repo, path, url, license), `meta` (parse notes) |")
    A(f"| `rev/data/real_test.jsonl` | {stats['tasks']} task rows (same row format as `test.jsonl` + `name`/`code`/`family`/`usb_a`/`source`) |")
    A(f"| `rev/data/real_fleet.json` | {len(fleet_rows)} tasks, same keys as `rev/data/fleet.json` plus `source` (attribution) |")
    A("| `vendor/kicad/manifest.json` | every downloaded file: repo, path, commit URL, license (the `.kicad_pcb` files themselves are git-ignored; re-fetch with `python3 -m rev.real_boards download`) |")
    A("")
    A("## Funnel (honest numbers)")
    A("")
    A(f"1. GitHub code search (`<connector> MountingHole extension:kicad_pcb` for USB_C_Receptacle, RJ45, BarrelJack, HDMI, microSD, USB_A): **{man['search_hits']} unique .kicad_pcb hits**.")
    A(f"2. Dropped forks, QA/test/fixture/example/backup paths, and capped at 2 files per repo: **{man['candidates_after_filters']}** files from {man['repos']} repos.")
    A(f"3. Kept only repos where GitHub reports a license (no-license repos dropped): **{man['downloaded']} files downloaded** (raw, pinned to the commit in the search result).")
    A(f"4. Parsed and filtered -> **{stats['parsed_kept']} boards kept**. Rejections:")
    A("")
    for k, v in sorted(rj.items(), key=lambda kv: -kv[1]):
        A(f"   - {k}: {v}")
    A("")
    A(f"Connectors on kept boards: " + ", ".join(f"{k} {v}" for k, v in sorted(stats['connector_types'].items(), key=lambda kv: -kv[1])) + ".")
    A(f" Change kinds in `real_test.jsonl`: " + ", ".join(f"{k} {v}" for k, v in stats['kinds'].items()) + ".")
    A(f" Boards whose mounting holes were synthesized (fewer than 2 real holes found): {stats['holes_synthesized']} (excluded from the fleet).")
    A("")
    A("## Parsing heuristics (what is real vs. estimated)")
    A("")
    A("**Taken from the real design files:**")
    A("- Board size `w`,`d`: bounding box of all `Edge.Cuts` primitives (`gr_line`, `gr_rect`, `gr_arc` in both the KiCad 5 center/angle and KiCad 6+ start/mid/end forms, `gr_circle`, `gr_poly`). Segments are chained into closed loops; the board is kept only if the largest loop fills >= 85% of its bounding box (so rounded corners are fine, L-shapes/round boards are rejected) and no other loop lies outside it (panels rejected). 15-250 mm per side.")
    A("- Thickness `t`: `(general (thickness ...))`, default 1.6.")
    A("- Mounting holes: footprints whose name contains `MountingHole` (diameter from the `3.2mm`-style name, else the pad drill), or footprints made only of NPTH pads with drill >= 2.2 mm. Deduplicated within 0.5 mm. Hole ids are renumbered H1..Hn.")
    A("- Connectors: front-side footprints whose footprint name (library prefix ignored) matches USB-C / USB-A / HDMI (full size only) / RJ45 / 8P8C / barrel or DC jack / microSD or SD/TF card; vertical parts skipped. The connector body = courtyard outline (else fab layer, else pad extents) transformed by the footprint's `(at x y rot)`. It must reach within 6 mm of a board edge; `side` = nearest edge, `pos` = body centre along that edge. The original reference designator (J1, USB1, CN6, ...) is kept as the id when unique.")
    A("- Coordinates: KiCad y grows downward, so board-local `y = ymax - y_kicad` (origin = bottom-left, front edge = the bottom edge of the KiCad view).")
    A("")
    A("**Estimated / normalized (not in the files):**")
    A("- Connector `w`,`h`,`z` come from `kernel.CONNECTOR_SPECS` for the type, not from the actual part (a real USB-C receptacle is ~8.9 mm wide, so this is close; RJ45 variants differ more).")
    A("- `max_component_h` is a heuristic: max of 6.0 mm and per-footprint estimates (connector z+h/2, e.g. RJ45 13.5, barrel 11.0, USB-A 5.7; relays 15.5; radial electrolytics ~1.3xD+2; terminal blocks 10; pin sockets / vertical headers 8.5; batteries 8; buzzers 9.5), rounded up to 0.5 mm. No 3D models are read.")
    A("- Boards with < 2 detected holes get 4 corner holes inset 3.5 mm (`meta.holes_synthesized = true`).")
    A("- The engineering change (Rev B) is synthetic: one of resize (edge + nearby holes/connectors move), move a connector along its edge, add a connector of a new type, taller component, move a hole, remove a connector. Gold calls come from `rev.tasks.gold_calls` (same minimal-diff logic as the synthetic data).")
    A("")
    A("## Limitations")
    A("")
    A("- Bottom-side connectors, micro-USB, mini/micro-HDMI, DE-9, audio jacks, headers etc. are ignored (REV has no type for them); a board with only those is rejected.")
    A("- Side assignment is by proximity only: a connector near a corner could be assigned to the wrong edge; connectors that overlap on the same edge (stacked/dual ports) keep only the first.")
    A("- Outline is the bounding box of the cut layer; board-edge notches and cut-outs inside the board are ignored.")
    A("- Licenses are GitHub's SPDX detection for the repo; repos GitHub marks NOASSERTION (license file present but unrecognized) were excluded to stay conservative. Several licenses (CC-BY-SA, CERN-OHL-S, GPL) require attribution/share-alike: the derived board JSON keeps `source` for that reason.")
    A("- Board names are the repo name (plus file name when a repo contributes two boards) - no owner names in UI strings; owners appear only in the attribution URLs below.")
    A("- A handful of kept designs may be course exercises or hobby prototypes rather than shipped products; they are still real, human-made KiCad layouts.")
    A("")
    A("## Attribution (every kept board)")
    A("")
    A("| # | board (REV name) | repo | license | file | fleet |")
    A("|---|---|---|---|---|---|")
    for i, b in enumerate(boards):
        src = b["source"]
        key = src["repo"] + "/" + src["path"]
        path = src["path"].replace("|", "\\|")
        A(f"| {i + 1} | {b['board']['name']} | [{src['repo']}](https://github.com/{src['repo']}) | {src['license']} | "
          f"[{path}]({src['url'].replace(' ', '%20')}) | {in_fleet.get(key, '')} |")
    A("")
    open(os.path.join(ROOT, "REAL_DATA.md"), "w").write("\n".join(lines))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "download":
        download()
    elif cmd == "build":
        build()
    elif cmd == "one":
        b, m = convert(open(sys.argv[2]).read(), os.path.basename(sys.argv[2]))
        print(json.dumps(b, indent=1)); print(json.dumps(m))
