"""Task generator: synthetic "past revisions" (Rev A -> Rev B engineering changes) with gold tool calls.

Run: python3 -m rev.tasks   (writes rev/data/train.jsonl, test.jsonl, demo.json and prints stats)
"""
import copy
import json
import os
import random
from collections import Counter

from rev import kernel
from rev.kernel import HOUSE, CONNECTOR_SPECS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Connector types that are NOT part of the company history (taught later from a one-shot correction, see rev/learn.py).
LESSON_TYPES = ("usb_a",)
TYPES = [t for t in CONNECTOR_SPECS if t not in LESSON_TYPES]  # history (train/test) types
EDGE_MARGIN = 7.0   # connector body must stay this far from board corners
CONN_GAP = 2.0      # min gap between connectors on the same side
HOLE_INSET = 3.5
KINDS = ["resize", "move_connector", "add_connector", "remove_connector",
         "move_hole", "add_hole", "remove_hole", "taller_component"]
PRODUCTS = ["Sensor Hub", "Edge Gateway", "Motor Driver", "LoRa Node", "Camera Bridge", "Power Monitor",
            "Audio Interface", "Display Controller", "Robot Base", "Weather Station", "Dev Kit", "Smart Relay"]


def r2(v):
    return round(float(v) + 0.0, 2)


def g05(v):
    """Snap to 0.5 mm grid."""
    return r2(round(float(v) * 2) / 2)


def make_connector(cid, ctype, side, pos):
    s = CONNECTOR_SPECS[ctype]
    return {"id": cid, "type": ctype, "side": side, "pos": r2(pos), "z": s["z"], "w": s["w"], "h": s["h"]}


def side_len(board, side):
    return board["w"] if side in ("front", "back") else board["d"]


def conn_ok(board, c, ignore_id=None):
    """Connector inside its edge (with corner margin) and not overlapping others on the same side."""
    L = side_len(board, c["side"])
    lo, hi = c["pos"] - c["w"] / 2, c["pos"] + c["w"] / 2
    if lo < EDGE_MARGIN - 1e-9 or hi > L - EDGE_MARGIN + 1e-9:
        return False
    for o in board["connectors"]:
        if o["id"] in (c["id"], ignore_id) or o["side"] != c["side"]:
            continue
        if lo < o["pos"] + o["w"] / 2 + CONN_GAP and hi > o["pos"] - o["w"] / 2 - CONN_GAP:
            return False
    return True


def hole_ok(board, h, ignore_id=None):
    if not (HOLE_INSET - 1e-9 <= h["x"] <= board["w"] - HOLE_INSET + 1e-9):
        return False
    if not (HOLE_INSET - 1e-9 <= h["y"] <= board["d"] - HOLE_INSET + 1e-9):
        return False
    for o in board["holes"]:
        if o["id"] in (h["id"], ignore_id):
            continue
        if (o["x"] - h["x"]) ** 2 + (o["y"] - h["y"]) ** 2 < 8.0 ** 2:
            return False
    return True


def board_valid(board):
    return (all(conn_ok(board, c) for c in board["connectors"]) and
            all(hole_ok(board, h) for h in board["holes"]))


def next_id(items, prefix):
    n = 0
    for it in items:
        try:
            n = max(n, int(it["id"][len(prefix):]))
        except ValueError:
            pass
    return f"{prefix}{n + 1}"


def random_pos(rng, board, ctype, side, cid, tries=40):
    L = side_len(board, side)
    w = CONNECTOR_SPECS[ctype]["w"]
    lo, hi = EDGE_MARGIN + w / 2, L - EDGE_MARGIN - w / 2
    if hi < lo:
        return None
    for _ in range(tries):
        c = make_connector(cid, ctype, side, g05(rng.uniform(lo, hi)))
        if conn_ok(board, c):
            return c
    return None


def random_board(rng, types):
    w = g05(rng.uniform(55, 120))
    d = g05(rng.uniform(42, 90))
    dia = rng.choice([2.2, 2.7, 2.7, 3.2])
    board = {"rev": "A", "name": rng.choice(PRODUCTS), "w": w, "d": d,
             "t": rng.choice([1.0, 1.2, 1.6, 1.6, 1.6, 2.0]),
             "max_component_h": g05(rng.uniform(11, 22)), "holes": [], "connectors": []}
    corners = [(HOLE_INSET, HOLE_INSET), (w - HOLE_INSET, HOLE_INSET),
               (w - HOLE_INSET, d - HOLE_INSET), (HOLE_INSET, d - HOLE_INSET)]
    for i, (x, y) in enumerate(corners):
        board["holes"].append({"id": f"H{i + 1}", "x": r2(x), "y": r2(y), "dia": dia})
    for t in types:
        for _ in range(10):
            side = rng.choice(kernel.SIDES)
            c = random_pos(rng, board, t, side, next_id(board["connectors"], "J"))
            if c:
                board["connectors"].append(c)
                break
    return board


# ---------------------------------------------------------------- changes (mutate board_b, return summary or None)

def ch_resize(rng, b):
    axis = rng.choice(["w", "d", "w", "both"])
    old = copy.deepcopy(b)
    parts = []
    for ax in (["w", "d"] if axis == "both" else [axis]):
        for _ in range(20):
            delta = g05(rng.uniform(2, 14)) * rng.choice([1, 1, -1])
            new = r2(old[ax] + delta)
            if not (45 <= new <= 130):
                continue
            b[ax] = new
            # holes on the moved edge (right edge for w, back edge for d) move with it
            for h in b["holes"]:
                key = "x" if ax == "w" else "y"
                if abs(h[key] - (old[ax] - HOLE_INSET)) < 1e-6:
                    h[key] = r2(new - HOLE_INSET)
            if board_valid(b):
                word = ("widened" if delta > 0 else "narrowed") if ax == "w" else ("deepened" if delta > 0 else "made shallower")
                parts.append(f"Board {word} {abs(delta):.1f} mm")
                old = copy.deepcopy(b)
                break
            b.clear(); b.update(copy.deepcopy(old))
    return "; ".join(parts) if parts else None


def ch_move_connector(rng, b, touched):
    cands = [c for c in b["connectors"] if c["id"] not in touched]
    rng.shuffle(cands)
    for c in cands:
        for _ in range(20):
            delta = g05(rng.uniform(3, 20)) * rng.choice([1, -1])
            nc = dict(c, pos=r2(c["pos"] + delta))
            if conn_ok(b, nc):
                c["pos"] = nc["pos"]
                touched.add(c["id"])
                if c["side"] in ("front", "back"):
                    d = "right" if delta > 0 else "left"
                else:
                    d = "back" if delta > 0 else "front"
                return f"{c['id']} ({c['type']}) moved {abs(delta):.1f} mm toward {d}"
    return None


def ch_add_connector(rng, b, touched, new_type_only, types_a, types=None):
    types = TYPES if types is None else types
    pool = [t for t in types if t not in types_a] if new_type_only else types
    if not pool:
        return None
    for _ in range(20):
        t = rng.choice(pool)
        side = rng.choice(kernel.SIDES)
        cid = next_id(b["connectors"], "J")
        c = random_pos(rng, b, t, side, cid)
        if c:
            b["connectors"].append(c)
            touched.add(cid)
            return f"added {cid} ({t}) on {side} edge"
    return None


def ch_remove_connector(rng, b, touched):
    cands = [c for c in b["connectors"] if c["id"] not in touched]
    if not cands:
        return None
    c = rng.choice(cands)
    b["connectors"].remove(c)
    touched.add(c["id"])
    return f"removed {c['id']} ({c['type']})"


def ch_move_hole(rng, b, touched):
    cands = [h for h in b["holes"] if h["id"] not in touched]
    rng.shuffle(cands)
    for h in cands:
        for _ in range(20):
            key = rng.choice(["x", "y"])
            delta = g05(rng.uniform(1, 8)) * rng.choice([1, -1])
            nh = dict(h, **{key: r2(h[key] + delta)})
            if hole_ok(b, nh):
                h[key] = nh[key]
                touched.add(h["id"])
                d = ("right" if delta > 0 else "left") if key == "x" else ("back" if delta > 0 else "front")
                return f"mounting hole {h['id']} moved {abs(delta):.1f} mm toward {d}"
    return None


def ch_add_hole(rng, b, touched):
    hid = next_id(b["holes"], "H")
    dia = b["holes"][0]["dia"] if b["holes"] else 2.7
    for _ in range(40):
        h = {"id": hid, "x": g05(rng.uniform(10, b["w"] - 10)), "y": g05(rng.uniform(10, b["d"] - 10)), "dia": dia}
        if hole_ok(b, h):
            b["holes"].append(h)
            touched.add(hid)
            return f"added mounting hole {hid}"
    return None


def ch_remove_hole(rng, b, touched):
    cands = [h for h in b["holes"] if h["id"] not in touched]
    if len(b["holes"]) <= 2 or not cands:
        return None
    h = rng.choice(cands)
    b["holes"].remove(h)
    touched.add(h["id"])
    return f"removed mounting hole {h['id']}"


def ch_taller(rng, b):
    old = b["max_component_h"]
    b["max_component_h"] = r2(old + g05(rng.uniform(1, 8)))
    return f"tallest component now {b['max_component_h']:.1f} mm (was {old:.1f})"


# ---------------------------------------------------------------- gold

def gold_calls(board_a, enc_a, board_b):
    """Minimal diff: fit_cavity if dims changed, then removals, then places (house values)."""
    calls = []
    enc = copy.deepcopy(enc_a)
    if any(board_a[k] != board_b[k] for k in ("w", "d", "t", "max_component_h")):
        calls.append({"tool": "fit_cavity", "args": {"clearance": HOUSE["clearance"], "headroom": HOUSE["headroom"]}})
        enc = kernel.apply_call(board_b, enc, calls[-1])
    hole_ids = {h["id"] for h in board_b["holes"]}
    conn_ids = {c["id"] for c in board_b["connectors"]}
    removes = [{"tool": "remove_standoff", "args": {"hole": s["hole"]}} for s in enc["standoffs"] if s["hole"] not in hole_ids]
    removes += [{"tool": "remove_opening", "args": {"connector": o["connector"]}} for o in enc["openings"] if o["connector"] not in conn_ids]
    target = kernel.enclosure_for(board_b)
    places = []
    for s in target["standoffs"]:
        i = kernel._find(enc["standoffs"], "hole", s["hole"])
        if i < 0 or enc["standoffs"][i] != s:
            places.append({"tool": "place_standoff", "args": {"hole": s["hole"]}})
    for o in target["openings"]:
        i = kernel._find(enc["openings"], "connector", o["connector"])
        if i < 0 or enc["openings"][i] != o:
            c = next(c for c in board_b["connectors"] if c["id"] == o["connector"])
            places.append({"tool": "place_opening", "args": {"connector": o["connector"], "tolerance": HOUSE["tolerance"][c["type"]]}})
    return calls + removes + places


# ---------------------------------------------------------------- task

def make_task(rng, tid, types=None):
    """types: allowed connector types (default: history TYPES, which exclude LESSON_TYPES like usb_a)."""
    types = TYPES if types is None else list(types)
    for _ in range(200):
        n_types = rng.choice([1, 2, 2, 3, 3, 4])
        types_a = rng.sample(types, n_types)
        if rng.random() < 0.2:  # sometimes a duplicate type (e.g. two USB-C)
            types_a.append(rng.choice(types_a))
        board_a = random_board(rng, types_a)
        if not board_a["connectors"]:
            continue
        present = {c["type"] for c in board_a["connectors"]}
        board_b = copy.deepcopy(board_a)
        board_b["rev"] = "B"
        k = rng.choice([1, 2, 2, 3, 3])
        kinds = []
        if rng.random() < 0.7 and len(present) < len(types):
            kinds.append("add_connector_new")
        others = KINDS + ["resize", "resize", "move_connector"]
        while len(kinds) < k:
            x = rng.choice(others)
            if x in kinds or (x == "add_connector" and "add_connector_new" in kinds):
                continue
            kinds.append(x)
        # resize first so later placements respect the new outline; taller anywhere
        kinds.sort(key=lambda x: 0 if x == "resize" else 1)
        touched, summaries, ok = set(), [], True
        for kind in kinds:
            if kind == "resize":
                s = ch_resize(rng, board_b)
            elif kind == "move_connector":
                s = ch_move_connector(rng, board_b, touched)
            elif kind == "add_connector_new":
                s = ch_add_connector(rng, board_b, touched, True, present, types)
            elif kind == "add_connector":
                s = ch_add_connector(rng, board_b, touched, False, present, types)
            elif kind == "remove_connector":
                s = ch_remove_connector(rng, board_b, touched)
            elif kind == "move_hole":
                s = ch_move_hole(rng, board_b, touched)
            elif kind == "add_hole":
                s = ch_add_hole(rng, board_b, touched)
            elif kind == "remove_hole":
                s = ch_remove_hole(rng, board_b, touched)
            elif kind == "taller_component":
                s = ch_taller(rng, board_b)
            if not s:
                ok = False
                break
            summaries.append(s)
        if not ok or not board_valid(board_b):
            continue
        enc_a = kernel.enclosure_for(board_a)
        gold = gold_calls(board_a, enc_a, board_b)
        enc_b, errs = kernel.apply_calls(board_b, enc_a, gold)
        if errs or not kernel.passes(board_b, enc_b) or kernel.passes(board_b, enc_a):
            continue
        summary = "; ".join(summaries)
        summary = summary[0].upper() + summary[1:]
        return {"id": tid, "board_a": board_a, "enclosure_a": enc_a, "board_b": board_b,
                "change_summary": summary, "gold_calls": gold,
                "kinds": ["add_connector" if x == "add_connector_new" else x for x in kinds],
                "new_type_added": "add_connector_new" in kinds}
    raise RuntimeError("could not generate task")


def _key(t):
    return json.dumps([t["board_a"], t["board_b"]], sort_keys=True)


def generate(n, seed, prefix, exclude=None, types=None):
    rng = random.Random(seed)
    exclude = exclude or set()
    out, seen = [], set(exclude)
    while len(out) < n:
        t = make_task(rng, f"{prefix}-{len(out):03d}", types)
        k = _key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def demo_task():
    a = {"rev": "A", "name": "Sensor Hub", "w": 85.0, "d": 56.0, "t": 1.6, "max_component_h": 12.0,
         "holes": [{"id": "H1", "x": 3.5, "y": 3.5, "dia": 2.7}, {"id": "H2", "x": 81.5, "y": 3.5, "dia": 2.7},
                   {"id": "H3", "x": 81.5, "y": 52.5, "dia": 2.7}, {"id": "H4", "x": 3.5, "y": 52.5, "dia": 2.7}],
         "connectors": [make_connector("J1", "usb_c", "front", 34.0), make_connector("J2", "hdmi", "back", 42.0),
                        make_connector("J3", "barrel", "left", 28.0)]}
    b = copy.deepcopy(a)
    b["rev"] = "B"
    b["w"] = 91.0
    for h in b["holes"]:
        if h["x"] == 81.5:
            h["x"] = 87.5
    b["connectors"][0]["pos"] = 20.0
    b["connectors"].append(make_connector("J4", "rj45", "right", 28.0))
    enc_a = kernel.enclosure_for(a)
    return {"board_a": a, "enclosure_a": enc_a, "board_b": b,
            "change_summary": "Board widened 6.0 mm; J1 (usb_c) moved 14.0 mm toward left; added J4 (rj45) on right edge",
            "gold_calls": gold_calls(a, enc_a, b)}


def load(split):
    """split: 'train' | 'test' -> list of tasks; 'demo' -> dict."""
    if split == "demo":
        with open(os.path.join(DATA_DIR, "demo.json")) as f:
            return json.load(f)
    with open(os.path.join(DATA_DIR, f"{split}.jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]


def build():
    train = generate(200, 1, "train")
    test = generate(30, 2, "test", exclude={_key(t) for t in train})
    return train, test, demo_task()


def write_all():
    train, test, demo = build()
    os.makedirs(DATA_DIR, exist_ok=True)
    for name, rows in (("train", train), ("test", test)):
        with open(os.path.join(DATA_DIR, f"{name}.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    with open(os.path.join(DATA_DIR, "demo.json"), "w") as f:
        json.dump(demo, f, indent=1)
    return train, test, demo


def wrong_generic_calls(calls):
    """Same calls as gold but with generic (wrong) values: clearance 2.0, headroom 5.0, tolerance 0.5."""
    out = []
    for c in calls:
        c = copy.deepcopy(c)
        if c["tool"] == "fit_cavity":
            c["args"] = {"clearance": 2.0, "headroom": 5.0}
        elif c["tool"] == "place_opening":
            c["args"]["tolerance"] = 0.5
        out.append(c)
    return out


def inferred_calls(task):
    """Same calls as gold but with values a smart model could read off Rev A (unknown types -> 0.5)."""
    a, enc = task["board_a"], task["enclosure_a"]
    cl = enc["board_origin"]["x"]
    hr = r2(enc["cavity"]["h"] - enc["standoff_h"] - a["t"] - a["max_component_h"])
    tol = {}
    for o in enc["openings"]:
        c = next(c for c in a["connectors"] if c["id"] == o["connector"])
        tol[c["type"]] = r2((o["w"] - c["w"]) / 2)
    out = []
    for c in task["gold_calls"]:
        c = copy.deepcopy(c)
        if c["tool"] == "fit_cavity":
            c["args"] = {"clearance": cl, "headroom": hr}
        elif c["tool"] == "place_opening":
            ct = next(x for x in task["board_b"]["connectors"] if x["id"] == c["args"]["connector"])["type"]
            c["args"]["tolerance"] = tol.get(ct, 0.5)
        out.append(c)
    return out


def pass_rate(tasks, fn):
    ok = 0
    for t in tasks:
        enc, _ = kernel.apply_calls(t["board_b"], t["enclosure_a"], fn(t))
        ok += kernel.passes(t["board_b"], enc)
    return ok / len(tasks)


def stats(tasks):
    from rev import prompts
    kinds = Counter(k for t in tasks for k in t["kinds"])
    nchg = Counter(len(t["kinds"]) for t in tasks)
    added_types = Counter(c["type"] for t in tasks for c in t["board_b"]["connectors"]
                          if c["id"] not in {x["id"] for x in t["board_a"]["connectors"]})
    plen = sum(len(prompts.SYSTEM) + len(prompts.user_prompt(t)) for t in tasks) / len(tasks)
    clen = sum(len(prompts.completion(t)) for t in tasks) / len(tasks)
    return {
        "n": len(tasks),
        "avg_prompt_chars": round(plen),
        "avg_completion_chars": round(clen),
        "kinds": dict(kinds),
        "n_changes": dict(sorted(nchg.items())),
        "added_types": dict(added_types),
        "new_type_added_frac": round(sum(t["new_type_added"] for t in tasks) / len(tasks), 3),
        "gold_pass": pass_rate(tasks, lambda t: t["gold_calls"]),
        "stale_pass": pass_rate(tasks, lambda t: []),
        "wrong_generic_pass": round(pass_rate(tasks, lambda t: wrong_generic_calls(t["gold_calls"])), 3),
        "infer_from_rev_a_pass": round(pass_rate(tasks, inferred_calls), 3),
    }


if __name__ == "__main__":
    train, test, demo = write_all()
    print("train", json.dumps(stats(train)))
    print("test ", json.dumps(stats(test)))
    print("demo gold", json.dumps(demo["gold_calls"], separators=(",", ":")))
